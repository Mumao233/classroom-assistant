"""v1.2.1 request identity and immutable-context regressions, synthetic only."""
import copy
import json

import pytest
from fastapi import HTTPException

from codex_bridge import CodexBridgeError
from test_app import launch, room_factory, wait_for, local_client
from test_study_workflow import VisualBridge, png, prepare, seed, send


def test_upload_identity_survives_lost_response_restart_and_class_switch(room_factory):
    room = room_factory()
    sid = room.current
    original = room.add_slide(png(), sid=sid, start=7, title='Original', request_id='upload-identity-1')
    room.new_session('Other classroom')
    duplicate = room.add_slide(png(), sid=sid, start=7, title='Original', request_id='upload-identity-1')
    assert duplicate['id'] == original['id']
    assert len(room.get(sid)['slides']) == 1 and not room.get()['slides']
    restored = room_factory(folder=room.data)
    found = restored.lookup_slide_request('upload-identity-1', sid)
    assert found['found'] and found['available'] and found['slide']['id'] == original['id']
    assert not restored.lookup_slide_request('upload-identity-1', room.current)['found']
    with pytest.raises(HTTPException) as conflict:
        restored.add_slide(png(90), sid=sid, start=7, title='Original', request_id='upload-identity-1')
    assert conflict.value.status_code == 409


def test_upload_save_failure_does_not_publish_receipt(room_factory, monkeypatch):
    room = room_factory()
    with monkeypatch.context() as patcher:
        patcher.setattr(room, 'save', lambda s: (_ for _ in ()).throw(OSError('synthetic disk full')))
        with pytest.raises(HTTPException):
            room.add_slide(png(), sid=room.current, start=0, request_id='upload-save-failure')
    assert not room.lookup_slide_request('upload-save-failure', room.current)['found']
    assert not room.get()['slides']


def test_prepare_identity_is_durable_and_conflicting_payload_is_rejected(room_factory):
    room = room_factory()
    seed(room)
    q = prepare(room, request_id='prepare-identity-1')
    room.append_text(room.current, 'New teaching after response was lost.', 24, 30, 'asr')
    duplicate = prepare(room, request_id='prepare-identity-1')
    assert duplicate['id'] == q['id'] and duplicate['evidence'] == q['evidence']
    restored = room_factory(folder=room.data)
    found = restored.lookup_study_request('prepare-identity-1', room.current)
    assert found['found'] and found['question']['id'] == q['id']
    with pytest.raises(HTTPException) as conflict:
        prepare(restored, request_id='prepare-identity-1', anchor=30)
    assert conflict.value.status_code == 409 and len(restored.get()['questions']) == 1


def test_same_send_returns_existing_task_and_never_enqueues_again(room_factory):
    room = room_factory()
    seed(room)
    q = prepare(room)
    first = send(room, q, confirm_images=True)
    second = send(room, q, confirm_images=True)
    assert first['id'] == second['id'] and first['job_id'] == second['job_id']
    assert room.ai_queue.qsize() == 1


def test_edit_before_send_requires_current_source_acknowledgement(room_factory):
    room = room_factory()
    seed(room)
    q = prepare(room)
    original = copy.deepcopy(q['evidence'])
    room.edit_segment(room.current, 'S0001', 'Corrected source.')
    with pytest.raises(HTTPException) as changed:
        send(room, q, confirm_images=True)
    assert changed.value.status_code == 409 and room.ai_queue.empty()
    current = room.study_public(room.study_question(room.get(), q['id']), room.current)
    assert current['source_status']['changed']
    with pytest.raises(HTTPException):
        send(room, q, confirm_images=True, confirm_original=True, source_token='stale')
    accepted = send(room, q, confirm_images=True, confirm_original=True,
                    source_token=current['source_status']['token'])
    assert accepted['status'] == 'pending' and accepted['evidence'] == original


def test_edit_after_queue_before_dispatch_pauses_without_model_call(room_factory):
    bridge = VisualBridge()
    room = room_factory(bridge=bridge)
    seed(room)
    q = prepare(room)
    send(room, q, confirm_images=True)
    room.edit_segment(room.current, 'S0001', 'Edited while queued.')
    launch(room, '_ai_loop')
    wait_for(lambda: room.ai_queue.unfinished_tasks == 0)
    stored = room.study_question(room.get(), q['id'])
    assert stored['status'] == 'prepared' and not bridge.calls
    assert stored['evidence'] == q['evidence'] and not stored.get('dispatch_started_at')


def test_normal_new_lecture_keeps_frozen_range_and_does_not_pause(room_factory):
    bridge = VisualBridge()
    room = room_factory(bridge=bridge)
    seed(room)
    q = prepare(room)
    token = q['source_status']['token']
    send(room, q, confirm_images=True)
    room.append_text(room.current, 'Later unrelated lecture.', 24, 35, 'asr')
    assert room.study_public(room.study_question(room.get(), q['id']), room.current)['source_status']['token'] == token
    launch(room, '_ai_loop')
    wait_for(lambda: room.study_question(room.get(), q['id'])['status'] == 'done')
    assert len(bridge.calls) == 1 and 'Later unrelated' not in bridge.calls[0]['context']


class TimeoutVisual(VisualBridge):
    def ask_visual(self, *args, **kwargs):
        self.calls.append({'kind': 'visual'})
        raise CodexBridgeError('synthetic timeout', kind='timeout')


def test_timeout_is_unknown_and_recovery_never_resubmits(room_factory):
    bridge = TimeoutVisual()
    room = room_factory(bridge=bridge)
    seed(room)
    q = prepare(room)
    send(room, q, confirm_images=True)
    launch(room, '_ai_loop')
    wait_for(lambda: room.ai_queue.unfinished_tasks == 0)
    stored = room.study_question(room.get(), q['id'])
    assert stored['result_unknown']
    assert stored['error'] == '请求超时、无法确定服务商是否已处理'
    room.resume_ai('backfill')
    wait_for(lambda: room.ai_queue.unfinished_tasks == 0)
    assert len(bridge.calls) == 1
    restored = room_factory(folder=room.data, bridge=TimeoutVisual())
    assert restored.study_question(restored.get(), q['id'])['result_unknown']
    restored.resume_ai('backfill')
    assert restored.ai_queue.empty()
    with pytest.raises(HTTPException) as denied:
        restored.clone_study(q['id'], restored.current, request_id='retry-identity-1')
    assert denied.value.status_code == 409
    retry = restored.clone_study(q['id'], restored.current, request_id='retry-identity-1', confirm_duplicate=True)
    same = restored.clone_study(q['id'], restored.current, request_id='retry-identity-1', confirm_duplicate=True)
    assert retry['id'] == same['id'] and retry['evidence'] == q['evidence']
    assert retry['duplicate_billing_risk']
    with pytest.raises(HTTPException):
        send(restored, retry, confirm_images=True)


def test_dispatch_marker_written_before_call_and_restart_is_unknown(room_factory):
    bridge = VisualBridge()
    bridge.release.clear()
    room = room_factory(bridge=bridge)
    seed(room)
    q = prepare(room)
    send(room, q, confirm_images=True)
    launch(room, '_ai_loop')
    try:
        assert bridge.entered.wait(2)
        disk = json.loads((room.data / room.current / 'session.json').read_text(encoding='utf-8'))
        assert disk['questions'][0]['dispatch_started_at']
        restored = room_factory(folder=room.data)
        interrupted = restored.study_question(restored.get(), q['id'])
        assert interrupted['result_unknown'] and interrupted['status'] == 'error'
        restored.resume_ai('backfill')
        assert restored.ai_queue.empty()
    finally:
        bridge.release.set()


def test_upload_http_identity_lookup_and_prepare_lookup(local_client):
    client, room, _, _ = local_client
    room.get()['settings']['auto_analyze'] = False
    room.append_text(room.current, 'Synthetic lecture.', 0, 10, 'asr')
    params = {'session_id': room.current, 'start': 0, 'request_id': 'http-upload-identity'}
    first = client.post('/api/slides', params=params, content=png())
    second = client.post('/api/slides', params=params, content=png())
    assert first.status_code == second.status_code == 200
    assert first.json()['id'] == second.json()['id']
    lookup = client.get('/api/slides/requests/http-upload-identity', params={'session_id': room.current})
    assert lookup.status_code == 200 and lookup.json()['available']
    request = {'session_id': room.current, 'anchor': 10, 'explain': True, 'request_id': 'http-prepare-identity'}
    first_q = client.post('/api/study/prepare', json=request).json()
    same_q = client.post('/api/study/prepare', json=request).json()
    assert first_q['id'] == same_q['id']
    lookup = client.get('/api/study/requests/http-prepare-identity', params={'session_id': room.current})
    assert lookup.status_code == 200 and lookup.json()['question']['id'] == first_q['id']


def test_plain_question_and_explicit_retry_are_idempotent(room_factory):
    room = room_factory()
    first = room.ask('Synthetic ordinary question', sid=room.current, request_id='plain-ask-identity')
    same = room.ask('Synthetic ordinary question', sid=room.current, request_id='plain-ask-identity')
    assert first['id'] == same['id'] and room.ai_queue.qsize() == 1
    launch(room, '_ai_loop')
    wait_for(lambda: room.ai_queue.unfinished_tasks == 0)
    q = room.get()['questions'][0]
    retry = room.retry('question', q['id'], request_id='plain-retry-identity')
    wait_for(lambda: room.ai_queue.unfinished_tasks == 0)
    same = room.retry('question', q['id'], request_id='plain-retry-identity')
    assert retry['id'] == same['id'] and len(room.bridge.calls) == 2
    restored = room_factory(folder=room.data)
    found = restored.lookup_request('plain-retry-identity', room.current)
    assert found['found'] and found['question']['id'] == q['id']
    assert not restored.ai_queue.qsize()


def test_plain_question_save_failure_is_retryable_without_ghost_job(room_factory, monkeypatch):
    room = room_factory()
    with monkeypatch.context() as patcher:
        patcher.setattr(room, 'save', lambda s: (_ for _ in ()).throw(OSError('synthetic disk full')))
        with pytest.raises(HTTPException) as failed:
            room.ask('Synthetic unsaved question', request_id='plain-save-failure')
        assert failed.value.status_code == 507
    assert not room.lookup_request('plain-save-failure', room.current)['found']
    assert not room.get()['questions']
    launch(room, '_ai_loop')
    wait_for(lambda: room.ai_queue.unfinished_tasks == 0)
    assert room.threads[-1].is_alive() and not room.bridge.calls


@pytest.mark.parametrize('status,error_kind', [('running', None), ('paused', 'timeout'), ('paused', 'network')])
def test_legacy_dispatched_records_are_conservatively_unknown_on_upgrade(room_factory, status, error_kind):
    room = room_factory()
    seed(room)
    q = prepare(room)
    item = room.study_question(room.get(), q['id'])
    item.update(status=status, error_kind=error_kind)
    # A 1.2.0 record has no dispatch_started_at marker.
    room.save(room.get())
    restored = room_factory(folder=room.data)
    item = restored.study_question(restored.get(), q['id'])
    assert item['result_unknown']
    restored.resume_ai('backfill')
    assert restored.ai_queue.empty()
    assert item['status'] == 'error'
    if error_kind == 'timeout':
        assert item['error'] == '请求超时、无法确定服务商是否已处理'


def test_backdated_pending_upload_keeps_its_original_time(room_factory):
    room = room_factory()
    sid = room.current
    later = room.add_slide(png(90), sid=sid, start=20, request_id='later-upload-id')
    recovered = room.add_slide(png(80), sid=sid, start=10, request_id='original-failed-upload')
    assert recovered['start'] == 10 and recovered['end'] == 20
    assert next(p for p in room.get(sid)['slides'] if p['id'] == later['id'])['start'] == 20


def test_explicit_settings_session_cannot_overwrite_new_classroom(local_client):
    client, room, _, _ = local_client
    original = room.current
    room.new_session('Other settings')
    response = client.put('/api/settings', json={'session_id': original, 'title': 'Original title edited'})
    assert response.status_code == 200
    assert room.get(original)['title'] == 'Original title edited'
    assert room.get()['title'] == 'Other settings'
