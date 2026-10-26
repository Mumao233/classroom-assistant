"""v1.2 study snapshots and destination consent: synthetic, no external AI."""
import copy
import struct
import threading
import zlib

import pytest
from fastapi import HTTPException

from app import Classroom
from test_app import FakeASR, FakeBridge, launch, wait_for, room_factory, local_client


def png(color=80):
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 2, 2, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\0' + bytes([color, 20, 30]) * 2) * 2)) + chunk(b'IEND', b''))


class VisualBridge(FakeBridge):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def ask_visual(self, context, question, images, *, explain=False, cancel_event=None):
        self.calls.append({'context': context, 'images': copy.deepcopy(images), 'explain': explain})
        self.entered.set()
        assert self.release.wait(5)
        # Intentionally ignore cancellation to prove stale results cannot overwrite state.
        return '供给增加使价格下降、数量上升。[S0001] [P0001]'


def seed(room, images=2):
    s = room.get()
    s['settings']['auto_analyze'] = False
    room.append_text(s['id'], 'The supply curve moves right.', 0, 12, 'asr')
    room.append_text(s['id'], 'Demand stays the same.', 12, 24, 'asr')
    for i in range(images):
        room.add_slide(png(80 + i), start=i * 6, sid=s['id'], title=f'Slide {i+1}')
    return s


def prepare(room, **extra):
    return room.prepare_study({'session_id': room.current, 'mode': 'recent30', 'anchor': 24,
                               'explain': True, **extra})


def send(room, q, **extra):
    return room.send_study(q['id'], {'session_id': room.current,
                                   'destination_token': q['destination']['token'], **extra})


def test_prepare_freezes_two_images_and_text_without_enqueuing(room_factory):
    room = room_factory()
    s = seed(room)
    q = prepare(room)
    assert q['status'] == 'prepared' and q['requires_confirmation']
    assert len(q['evidence']['slides']) == 2
    assert not room.bridge.calls and room.ai_queue.empty()
    snapshot = copy.deepcopy(q['evidence'])
    room.edit_segment(s['id'], 'S0001', 'New corrected text.')
    room.append_text(s['id'], 'Later teaching must not leak.', 24, 36, 'asr')
    room.update_slide('P0001', {'session_id': s['id'], 'start': 1, 'end': 5})
    assert room.study_question(s, q['id'])['evidence'] == snapshot


def test_direct_send_requires_matching_explicit_consent(room_factory):
    room = room_factory()
    seed(room)
    q = prepare(room)
    with pytest.raises(HTTPException) as error:
        send(room, q)
    assert error.value.status_code == 409
    with pytest.raises(HTTPException):
        send(room, q, confirm_images=True, destination_token='stale')
    assert room.ai_queue.empty()
    send(room, q, confirm_images=True)
    assert room.study_question(room.get(), q['id'])['status'] == 'pending'
    with pytest.raises(HTTPException):
        send(room, q, confirm_images=True)


def test_approval_scope_is_classroom_image_and_destination(room_factory):
    room = room_factory()
    seed(room, 1)
    q = prepare(room)
    send(room, q, confirm_images=True, remember=True)
    assert prepare(room)['requires_confirmation'] is False
    room.add_slide(png(90), start=20, sid=room.current)
    assert prepare(room)['requires_confirmation'] is True
    room.new_session('another')
    seed(room, 1)
    assert prepare(room)['requires_confirmation'] is True


def test_normal_switch_blocked_force_invalidates_unsent_approval(room_factory):
    room = room_factory()
    seed(room)
    q = prepare(room)
    send(room, q, confirm_images=True, remember=True)
    with pytest.raises(HTTPException) as error:
        room.configure_ai({'provider': 'api', 'model': 'synthetic', 'api_key': 'test-only'})
    assert error.value.status_code == 409
    room.configure_ai({'provider': 'api', 'model': 'synthetic', 'api_key': 'test-only', 'force': True})
    stored = room.study_question(room.get(), q['id'])
    assert stored['status'] == 'prepared'
    with pytest.raises(HTTPException):
        send(room, q, confirm_images=True)
    current = room.study_public(stored, room.current)
    assert current['destination']['provider'] == 'api' and current['requires_confirmation']


def test_force_switch_ignores_late_answer_and_keeps_snapshot(room_factory):
    bridge = VisualBridge()
    bridge.release.clear()
    room = room_factory(bridge=bridge)
    seed(room)
    q = prepare(room)
    send(room, q, confirm_images=True)
    launch(room, '_ai_loop')
    assert bridge.entered.wait(2)
    room.configure_ai({'provider': 'api', 'model': 'synthetic', 'api_key': 'test-only', 'force': True})
    bridge.release.set()
    wait_for(lambda: room.active_ai is None)
    stored = room.study_question(room.get(), q['id'])
    assert stored['status'] == 'prepared' and stored['answer'] == ''
    assert stored['evidence'] == q['evidence']


def test_two_images_reach_bridge_and_original_receipt_survives_edit(room_factory):
    bridge = VisualBridge()
    room = room_factory(bridge=bridge)
    seed(room)
    q = prepare(room)
    send(room, q, confirm_images=True)
    launch(room, '_ai_loop')
    wait_for(lambda: room.study_question(room.get(), q['id'])['status'] == 'done')
    assert len(bridge.calls) == 1 and len(bridge.calls[0]['images']) == 2
    assert bridge.calls[0]['explain'] is True
    room.edit_segment(room.current, 'S0001', 'Edited after response.')
    stored = room.study_question(room.get(), q['id'])
    assert stored['stale'] and stored['evidence'] == q['evidence']
    assert 'moves right' in room.markdown(room.get())
    assert 'slides/' in room.markdown(room.get())


def test_only_text_is_explicit_new_snapshot_not_silent_fallback(room_factory):
    room = room_factory()
    seed(room)
    original = prepare(room)
    room.append_text(room.current, 'Not part of frozen text.', 24, 36, 'asr')
    text = room.clone_study(original['id'], room.current, text_only=True)
    assert text['id'] != original['id'] and text['evidence']['slides'] == []
    assert text['evidence']['segments'] == original['evidence']['segments']
    assert 'Not part' not in text['context_snapshot']
    assert text['requires_confirmation'] is False
    assert len(room.study_question(room.get(), original['id'])['evidence']['slides']) == 2


def test_restart_retains_images_and_prepared_snapshot_but_reconfirms_destination(room_factory):
    room = room_factory()
    seed(room)
    q = prepare(room)
    restored = room_factory(folder=room.data)
    public = restored.state()['session']['questions'][0]
    assert public['evidence'] == q['evidence'] and public['status'] == 'prepared'
    assert public['destination']['token'] != q['destination']['token']
    assert public['requires_confirmation']


def test_server_refuses_more_than_four_without_explicit_selection(room_factory):
    room = room_factory()
    seed(room, 5)
    with pytest.raises(HTTPException) as error:
        prepare(room, anchor=30)
    assert error.value.status_code == 422
    assert not room.get()['questions']
    q = prepare(room, anchor=30, slide_ids=['P0002', 'P0004'])
    assert [p['id'] for p in q['evidence']['slides']] == ['P0002', 'P0004']


def test_study_cannot_be_retried_through_legacy_endpoint(room_factory):
    room = room_factory()
    seed(room)
    q = prepare(room)
    with pytest.raises(HTTPException):
        room.retry('question', q['id'])
    assert room.ai_queue.empty()


def test_http_upload_prepare_consent_and_cross_classroom_boundaries(local_client):
    client, room, _, _ = local_client
    room.get()['settings']['auto_analyze'] = False
    room.append_text(room.current, 'Synthetic supply lecture.', 0, 12, 'asr')
    response = client.post('/api/slides', params={'session_id': room.current, 'start': 0},
                           content=png(), headers={'Content-Type': 'image/png'})
    assert response.status_code == 200, response.text
    q = client.post('/api/study/prepare', json={'session_id': room.current,
                    'mode': 'recent30', 'anchor': 12, 'explain': True}).json()
    assert q['status'] == 'prepared'
    denied = client.post(f"/api/study/{q['id']}/send", json={'session_id': room.current,
                         'destination_token': q['destination']['token']})
    assert denied.status_code == 409
    assert client.get('/api/slides/P0001', params={'session_id': room.current}).status_code == 200
    other = room.new_session('other')['id']
    assert client.get('/api/slides/P0001', params={'session_id': other}).status_code == 404
    assert client.post('/api/slides', params={'session_id': other, 'start': 0}, content=b'not-image').status_code == 422


def test_click_observation_excludes_late_transcript_and_restores_edited_version(room_factory):
    room = room_factory()
    s = seed(room)
    room.flush(s['id'])
    seen = {'seen_segments': [{'id': x['id'], 'version': x.get('version', 0)} for x in s['segments']],
            'seen_slides': copy.deepcopy(s['slides']),
            'seen_group_segment_ids': list(s['groups'][0]['segment_ids'])}
    room.edit_segment(s['id'], 'S0001', 'Changed after the click.')
    room.append_text(s['id'], 'Late ASR within the old range.', 2, 4, 'asr')
    room.update_slide('P0001', {'session_id': s['id'], 'start': 100, 'end': 120})
    q = prepare(room, **seen)
    assert [x['id'] for x in q['evidence']['segments']] == ['S0001', 'S0002']
    assert 'moves right' in q['context_snapshot'] and 'Changed after' not in q['context_snapshot']
    assert q['evidence']['slides'][0]['start'] == 0
    assert q['stale']


def test_click_observation_cannot_claim_unknown_source_version(room_factory):
    room = room_factory()
    seed(room)
    with pytest.raises(HTTPException) as error:
        prepare(room, seen_segments=[{'id': 'S0001', 'version': 999}])
    assert error.value.status_code == 409
