"""Review lifecycle acceptance; synthetic classroom and fake provider only."""
import copy
import json
import threading

import pytest
from fastapi import HTTPException

from app import Classroom
from codex_bridge import CodexBridgeError
from test_app import FakeASR, FakeBridge, launch, wait_for, room_factory
from test_study_workflow import png


class ReviewBridge(FakeBridge):
    context_chars = 12000

    def __init__(self, failure=None):
        super().__init__()
        self.failure = failure
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def generate_review(self, evidence, options, images, *, cancel_event=None):
        self.calls.append({'kind': 'review', 'evidence': copy.deepcopy(evidence)})
        self.entered.set()
        assert self.release.wait(5)
        if self.failure:
            raise self.failure
        refs = evidence['source_ids'][:1]
        return json.dumps({'schema_version': 1, 'cards': [
            {'type': 'concept', 'title': 'Opportunity cost', 'front': 'What is opportunity cost?',
             'back': 'The next best alternative.', 'supplement': '', 'english': '',
             'english_meaning': '', 'source_ids': refs, 'missing': []}],
            'notes': [{'section': 'structure', 'title': 'Opportunity cost', 'body': 'The next best alternative.',
                       'supplement': '', 'source_ids': refs, 'missing': []}]})


def seed(room, count=2):
    s = room.get()
    s['settings']['auto_analyze'] = False
    for i in range(count):
        room.append_text(s['id'], f'Opportunity cost is the next best alternative. Part {i}.', i * 12, (i + 1) * 12, 'asr')
    return s


def prepare(room, request='prepare-001', **kwargs):
    return room.prepare_review({'session_id': room.current, 'request_id': request,
                                'scope': 'all', 'card_types': ['concept'], 'english_answers': False, **kwargs})


def send(room, run, request='send-00001', **kwargs):
    return room.send_review(run['id'], {'session_id': room.current, 'request_id': request,
        'confirm_plan': True, 'destination_token': run['destination']['token'],
        'source_token': run['source_status']['token'], **kwargs})


def test_freeze_marks_and_content_identity_across_request_ids(room_factory):
    room = room_factory()
    s = seed(room)
    mark = room.set_review_mark({'session_id': s['id'], 'request_id': 'mark-00001',
        'target_kind': 'segment', 'target_id': 'S0001', 'kind': 'important', 'note': '考试重点'})
    run = prepare(room)
    same = prepare(room, 'prepare-002')
    assert same['id'] == run['id'] and not room.bridge.calls and room.ai_queue.empty()
    frozen = copy.deepcopy(run['evidence'])
    room.edit_segment(s['id'], 'S0001', 'Corrected text.')
    room.append_text(s['id'], 'New material.', 24, 30)
    public = room.review_public(s)
    assert public['runs'][0]['evidence'] == frozen
    assert public['runs'][0]['source_status']['changed']
    assert public['runs'][0]['source_status']['new_material']
    assert public['marks'][0]['source_status']['changed']
    assert mark['evidence']['segments'][0]['text'] != s['segments'][0]['text']


def test_request_id_reuse_rejected_and_required(room_factory):
    room = room_factory()
    seed(room)
    prepare(room)
    with pytest.raises(HTTPException) as error:
        prepare(room, card_types=['example'])
    assert error.value.status_code == 409
    with pytest.raises(HTTPException):
        room.prepare_review({'session_id': room.current})


def test_complete_cards_notes_and_cas_preserves_ai_original(room_factory):
    bridge = ReviewBridge()
    room = room_factory(bridge=bridge)
    s = seed(room)
    run = prepare(room)
    send(room, run)
    launch(room, '_ai_loop')
    wait_for(lambda: room.review_run(s, run['id'])['status'] == 'done')
    review = room.review_public(s)
    assert len(review['cards']) == len(review['notes']) == 1
    card = review['cards'][0]
    ai = copy.deepcopy(card['ai_content'])
    edited = room.edit_review_artifact('cards', card['id'], {'session_id': s['id'], 'request_id': 'edit-00001',
        'revision': 0, 'content': 'My own explanation', 'personal_note': 'remember'})
    assert edited['user_draft']['revision'] == 1 and edited['ai_content'] == ai
    with pytest.raises(HTTPException) as error:
        room.edit_review_artifact('cards', card['id'], {'session_id': s['id'], 'request_id': 'edit-00002',
            'revision': 0, 'content': 'Stale overwrite', 'personal_note': ''})
    assert error.value.status_code == 409
    repeated = prepare(room, 'prepare-003')
    assert repeated['id'] == run['id'] and len(bridge.calls) == 1


def test_unknown_never_resumes_without_duplicate_ack(room_factory):
    room = room_factory(bridge=ReviewBridge(CodexBridgeError('timeout', kind='timeout')))
    s = seed(room)
    run = prepare(room)
    send(room, run)
    launch(room, '_ai_loop')
    wait_for(lambda: room.review_run(s, run['id'])['status'] == 'unknown')
    assert room.review_run(s, run['id'])['error'] == '请求超时、无法确定服务商是否已处理'
    with pytest.raises(HTTPException):
        room.send_review(run['id'], {'session_id': s['id'], 'request_id': 'continue-01',
            'confirm_plan': True, 'destination_token': room.study_route,
            'source_token': run['source_status']['token']}, continuing=True)
    assert len(room.bridge.calls) == 1


def test_restart_keeps_unsent_batches_inactive(room_factory):
    room = room_factory()
    seed(room)
    run = prepare(room)
    send(room, run)
    restored = room_factory(folder=room.data)
    assert restored.ai_queue.empty()
    state = restored.review_run(restored.get(), run['id'])
    assert state['status'] == 'paused'


def test_source_edit_after_confirmation_blocks_dispatch(room_factory):
    room = room_factory(bridge=ReviewBridge())
    s = seed(room)
    run = prepare(room)
    send(room, run)
    room.edit_segment(s['id'], 'S0001', 'Changed after confirmation.')
    launch(room, '_ai_loop')
    wait_for(lambda: room.review_run(s, run['id'])['status'] == 'paused')
    assert not room.bridge.calls


def test_five_images_partition_without_silent_omission(room_factory):
    room = room_factory(bridge=ReviewBridge())
    s = seed(room)
    for i in range(5):
        room.add_slide(png(50 + i), start=i * 3, sid=s['id'])
    run = prepare(room)
    assert len(run['batches']) >= 2
    seen = [x['id'] for b in run['batches'] for x in b['evidence']['slides']]
    assert sorted(seen) == ['P0001', 'P0002', 'P0003', 'P0004', 'P0005']
    assert all(len(b['evidence']['slides']) <= 4 for b in run['batches'])


def test_save_failure_does_not_dispatch(room_factory, monkeypatch):
    room = room_factory(bridge=ReviewBridge())
    seed(room)
    run = prepare(room)
    monkeypatch.setattr(room, 'save', lambda s: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(HTTPException) as error:
        send(room, run)
    assert error.value.status_code == 507 and not room.bridge.calls
    assert room.review_run(room.get(), run['id'])['status'] == 'prepared'


def test_invalid_output_keeps_raw_and_stops_following_batches(room_factory):
    class Invalid(ReviewBridge):
        def generate_review(self, *args, **kwargs):
            self.calls.append({'kind': 'review'})
            return '{not valid json'
    room = room_factory(bridge=Invalid())
    s = seed(room)
    for i in range(5):
        room.add_slide(png(40 + i), start=i, sid=s['id'])
    run = prepare(room)
    send(room, run)
    launch(room, '_ai_loop')
    wait_for(lambda: room.review_run(s, run['id'])['status'] == 'error')
    stored = room.review_run(s, run['id'])
    assert len(room.bridge.calls) == 1
    assert stored['batches'][0]['attempts'][0]['raw_ai_output'] == '{not valid json'
    assert not s['review']['cards'] and not s['review']['notes']
    assert stored['batches'][1]['status'] == 'prepared'


def test_two_batches_produce_one_note_and_distinct_same_source_cards(room_factory):
    class Several(ReviewBridge):
        def generate_review(self, *args, **kwargs):
            result = json.loads(super().generate_review(*args, **kwargs))
            extra = copy.deepcopy(result['cards'][0])
            extra['front'] = 'What is not opportunity cost?'
            extra['back'] = 'Not all alternatives added together.'
            result['cards'].extend([extra, copy.deepcopy(extra)])
            return json.dumps(result)
    room = room_factory(bridge=Several())
    s = seed(room)
    for i in range(5):
        room.add_slide(png(60 + i), start=i, sid=s['id'])
    run = prepare(room)
    send(room, run)
    launch(room, '_ai_loop')
    wait_for(lambda: room.review_run(s, run['id'])['status'] == 'done')
    assert len(room.bridge.calls) == len(run['batches'])
    assert len(s['review']['notes']) == 1
    assert len(s['review']['cards']) == 2  # Repeated nearby context does not duplicate exact cards.
    assert all(not x['source_status']['new_material'] for x in room.review_public(s)['cards'])


def test_returned_output_disk_failure_recovers_locally_without_new_call(room_factory, monkeypatch):
    room = room_factory(bridge=ReviewBridge())
    s = seed(room)
    run = prepare(room)
    send(room, run)
    original_save = room.save
    failed = []
    def bad_save(session):
        attempts = session['review']['runs'][0]['batches'][0]['attempts']
        if attempts and attempts[-1].get('raw_ai_output') and not failed:
            failed.append(True)
            raise OSError('synthetic full disk')
        return original_save(session)
    monkeypatch.setattr(room, 'save', bad_save)
    launch(room, '_ai_loop')
    wait_for(lambda: room.review_run(s, run['id'])['status'] == 'paused' and room.active_ai is None)
    stored = room.review_run_public(s, room.review_run(s, run['id']))
    room.send_review(run['id'], {'session_id': s['id'], 'request_id': 'continue-local',
        'confirm_plan': True, 'destination_token': stored['destination']['token'],
        'source_token': stored['source_status']['token']}, continuing=True)
    assert room.review_run(s, run['id'])['status'] == 'done'
    assert len(room.bridge.calls) == 1 and len(s['review']['cards']) == len(s['review']['notes']) == 1


def test_restart_with_saved_raw_recovers_without_provider_call(room_factory):
    room = room_factory(bridge=ReviewBridge())
    s = seed(room)
    run = prepare(room)
    send(room, run)
    batch = room.review_run(s, run['id'])['batches'][0]
    batch['status'] = 'running'
    attempt = batch['attempts'][0]
    attempt.update(dispatch_started_at='before', dispatch_completed_at='after',
        raw_ai_output=room.bridge.generate_review(batch['evidence'], run['options'], []))
    room.save(s)
    restored = room_factory(folder=room.data, bridge=ReviewBridge())
    public = restored.review_public(restored.get())['runs'][0]
    restored.send_review(public['id'], {'session_id': restored.current, 'request_id': 'restore-continue',
        'confirm_plan': True, 'destination_token': public['destination']['token'],
        'source_token': public['source_status']['token']}, continuing=True)
    assert restored.review_run(restored.get(), public['id'])['status'] == 'done'
    assert not restored.bridge.calls


def test_rebind_mark_is_explicit_and_unresolved_not_auto_cleared(room_factory):
    room = room_factory(bridge=ReviewBridge())
    s = seed(room)
    payload = {'session_id': s['id'], 'request_id': 'mark-unclear-1', 'target_kind': 'segment',
               'target_id': 'S0001', 'kind': 'unclear'}
    mark = room.set_review_mark(payload)
    room.edit_segment(s['id'], 'S0001', 'Changed original.')
    old = room.set_review_mark({**payload, 'request_id': 'mark-unclear-2', 'note': 'still unclear'})
    assert old['evidence'] == mark['evidence'] and old['source_status']['changed']
    rebound = room.set_review_mark({**payload, 'request_id': 'mark-unclear-3', 'rebind': True})
    assert rebound['evidence']['segments'][0]['text'] == 'Changed original.'
    assert len(rebound['history']) == 1 and not rebound['resolved']


def test_pending_review_prevents_unconfirmed_destination_switch(room_factory):
    room = room_factory(bridge=ReviewBridge())
    seed(room)
    run = prepare(room)
    send(room, run)
    with pytest.raises(HTTPException) as error:
        room.configure_ai({'provider': 'api', 'model': 'test-model', 'api_key': 'test-only'})
    assert error.value.status_code == 409


def test_regeneration_keeps_selected_note_and_manual_draft(room_factory):
    room = room_factory(bridge=ReviewBridge())
    s = seed(room)
    run = prepare(room)
    send(room, run)
    launch(room, '_ai_loop')
    wait_for(lambda: room.review_run(s, run['id'])['status'] == 'done')
    old = s['review']['selected_note_id']
    room.edit_review_artifact('notes', old, {'session_id': s['id'], 'request_id': 'edit-own-note',
        'revision': 0, 'content': 'My selected version', 'personal_note': ''})
    newer = prepare(room, 'regenerate-1', regenerate_of=run['id'])
    send(room, newer, 'send-newer-1')
    wait_for(lambda: room.review_run(s, newer['id'])['status'] == 'done')
    assert s['review']['selected_note_id'] == old and len(s['review']['notes']) == 2
    assert s['review']['notes'][0]['user_draft']['content'] == 'My selected version'
    assert len(s['review']['cards']) == 1


def test_real_routes_require_classroom_and_support_lookup(tmp_path):
    from app import create_app
    from fastapi.testclient import TestClient
    application = create_app({}, tmp_path / 'http-review', engine=FakeASR(), bridge=ReviewBridge())
    room = application.state.room
    s = seed(room)
    with TestClient(application) as client:
        assert client.post('/api/review/prepare', json={'request_id': 'missing-class'}).status_code == 422
        response = client.post('/api/review/prepare', json={'session_id': s['id'], 'request_id': 'prepare-http',
            'scope': 'all', 'card_types': ['concept'], 'english_answers': False})
        assert response.status_code == 200
        run = response.json()
        assert client.get('/api/review/requests/prepare-http', params={'session_id': s['id']}).json()['run']['id'] == run['id']
        assert client.get('/api/review', params={'session_id': s['id']}).json()['runs'][0]['id'] == run['id']
        assert client.get('/api/state').json()['session']['review']['runs'][0]['id'] == run['id']
        other = room.new_session('other')
        assert client.get('/api/review/requests/prepare-http', params={'session_id': other['id']}).status_code == 404


@pytest.mark.parametrize('field', ['confirm_plan', 'confirm_original', 'confirm_duplicate'])
def test_confirmation_requires_json_boolean(room_factory, field):
    room = room_factory(bridge=ReviewBridge())
    seed(room)
    run = prepare(room)
    with pytest.raises(HTTPException) as error:
        send(room, run, **{field: 'false'})
    assert error.value.status_code == 422
    assert room.ai_queue.empty()


def test_late_marked_slide_keeps_its_nearby_transcript(room_factory):
    room = room_factory(bridge=ReviewBridge())
    s = room.get()
    s['settings']['auto_analyze'] = False
    for i in range(10):
        room.append_text(s['id'], f'Topic {i}. ' + 'Classroom material. ' * 80, i * 10, (i+1) * 10, 'asr')
    room.add_slide(png(), start=90, sid=s['id'])
    room.set_review_mark({'session_id': s['id'], 'request_id': 'mark-late-slide',
        'target_kind': 'slide', 'target_id': 'P0001', 'kind': 'important'})
    run = prepare(room)
    assert 'P0001' in run['batches'][0]['evidence']['source_ids']
    assert 'S0010' in run['batches'][0]['evidence']['source_ids']
    primary = [x for b in run['batches'] for x in b['primary_source_ids']]
    assert sorted(primary) == sorted(run['evidence']['source_ids'])


def test_prepare_uses_clicked_sources_and_mark_revision(room_factory):
    room = room_factory()
    s = seed(room)
    mark = room.set_review_mark({'session_id': s['id'], 'request_id': 'click-mark-first',
        'target_kind': 'segment', 'target_id': 'S0001', 'kind': 'unclear', 'note': 'first note'})
    observed = {'seen_segments': [{'id': 'S0001', 'version': 0}, {'id': 'S0002', 'version': 0}],
                'seen_slides': [], 'seen_marks': [{'id': mark['id'], 'revision': mark['revision']}]}
    room.append_text(s['id'], 'New lesson after click.', 24, 36)
    room.edit_segment(s['id'], 'S0001', 'New text after click.')
    run = prepare(room, **observed)
    assert run['evidence']['source_ids'] == ['S0001', 'S0002']
    assert run['evidence']['segments'][0]['version'] == 0
    room.set_review_mark({'session_id': s['id'], 'request_id': 'click-mark-later',
        'target_kind': 'segment', 'target_id': 'S0001', 'kind': 'unclear', 'note': 'later note'})
    with pytest.raises(HTTPException) as error:
        prepare(room, 'prepare-click-stale', **observed)
    assert error.value.status_code == 409


def test_unresolved_marks_survive_model_omitting_questions(room_factory):
    room = room_factory(bridge=ReviewBridge())
    s = seed(room)
    mark = room.set_review_mark({'session_id': s['id'], 'request_id': 'mark-unresolved-1',
        'target_kind': 'segment', 'target_id': 'S0001', 'kind': 'unclear', 'note': 'Why the next best?'})
    run = prepare(room)
    send(room, run)
    launch(room, '_ai_loop')
    wait_for(lambda: room.review_run(s, run['id'])['status'] == 'done')
    assert s['review']['notes'][0]['unresolved_marks'][0]['id'] == mark['id']
    assert not s['review']['marks'][0]['resolved']


def test_failed_local_edit_during_provider_call_keeps_worker_record_identity(room_factory, monkeypatch):
    bridge = ReviewBridge()
    bridge.release.clear()
    room = room_factory(bridge=bridge)
    s = seed(room)
    run = prepare(room)
    send(room, run)
    launch(room, '_ai_loop')
    assert bridge.entered.wait(2)
    original = room.save
    monkeypatch.setattr(room, 'save', lambda session: (_ for _ in ()).throw(OSError('test storage')))
    with pytest.raises(HTTPException):
        room.set_review_mark({'session_id': s['id'], 'request_id': 'failed-mark-inflight',
            'target_kind': 'segment', 'target_id': 'S0001', 'kind': 'important'})
    monkeypatch.setattr(room, 'save', original)
    bridge.release.set()
    wait_for(lambda: room.active_ai is None)
    assert room.review_run(s, run['id'])['status'] == 'done'
    assert len(s['review']['notes']) == 1


def test_public_artifacts_wait_until_successful_disk_write(room_factory, monkeypatch):
    room = room_factory(bridge=ReviewBridge())
    s = seed(room)
    run = prepare(room)
    send(room, run)
    original = room.save
    def fail_official(session):
        if session.get('review', {}).get('cards'):
            raise OSError('full disk at official commit')
        return original(session)
    monkeypatch.setattr(room, 'save', fail_official)
    launch(room, '_ai_loop')
    wait_for(lambda: room.review_run(s, run['id'])['status'] == 'paused' and room.active_ai is None)
    assert not room.review_public(s)['cards'] and not room.review_public(s)['notes']
    monkeypatch.setattr(room, 'save', original)
    public = room.review_run_public(s, room.review_run(s, run['id']))
    room.send_review(run['id'], {'session_id': s['id'], 'request_id': 'commit-official',
        'confirm_plan': True, 'destination_token': public['destination']['token'],
        'source_token': public['source_status']['token']}, continuing=True)
    assert room.review_run(s, run['id'])['status'] == 'done'
    assert len(room.bridge.calls) == 1


@pytest.mark.parametrize('invalid', [
    {'schema_version': 99, 'runs': []}, {'schema_version': 1, 'runs': [{'id': 'broken'}]},
    {'schema_version': 1, 'cards': ['broken']}, 'not a review object'])
def test_bad_or_future_review_preserves_readable_classroom(room_factory, invalid):
    room = room_factory()
    s = seed(room)
    s['review'] = copy.deepcopy(invalid)
    room.save(s)
    original_file = (room.data / s['id'] / 'session.json').read_bytes()
    restored = room_factory(folder=room.data)
    assert restored.get(s['id'])['segments'][0]['text'] == s['segments'][0]['text']
    assert restored.get(s['id'])['review'] == invalid
    public = restored.review_public(restored.get(s['id']))
    assert public['error'] and public['read_only']
    assert (room.data / s['id'] / 'session.json').read_bytes() == original_file
    with pytest.raises(HTTPException):
        prepare(restored, 'invalid-review-create')


def test_legacy_read_does_not_add_review_to_saved_or_memory_classroom(room_factory):
    room = room_factory()
    s = seed(room)
    assert 'review' not in s
    before = (room.data / s['id'] / 'session.json').read_bytes()
    assert room.review_public(s)['runs'] == []
    assert 'review' not in s
    assert (room.data / s['id'] / 'session.json').read_bytes() == before


def test_live_question_runs_between_review_batches(room_factory):
    bridge = ReviewBridge()
    bridge.release.clear()
    room = room_factory(bridge=bridge)
    s = seed(room)
    for i in range(5):
        room.add_slide(png(70+i), start=i, sid=s['id'])
    run = prepare(room)
    send(room, run)
    launch(room, '_ai_loop')
    assert bridge.entered.wait(2)
    room.ask('Explain now', sid=s['id'])
    bridge.release.set()
    wait_for(lambda: room.review_run(s, run['id'])['status'] == 'done')
    assert [x['kind'] for x in bridge.calls] == ['review', 'question', 'review']


def test_cancel_running_request_keeps_unknown_and_never_publishes_late_result(room_factory):
    bridge = ReviewBridge()
    bridge.release.clear()
    room = room_factory(bridge=bridge)
    s = seed(room)
    run = prepare(room)
    send(room, run)
    launch(room, '_ai_loop')
    assert bridge.entered.wait(2)
    cancelled = room.cancel_review(run['id'], {'session_id': s['id'], 'request_id': 'cancel-running'})
    assert cancelled['status'] == 'cancelled' and cancelled['repeat_billing_required']
    bridge.release.set()
    wait_for(lambda: room.active_ai is None)
    stored = room.review_run(s, run['id'])
    assert stored['status'] == 'cancelled'
    assert stored['batches'][0]['attempts'][0]['raw_ai_output']
    assert not s['review']['cards'] and not s['review']['notes']


def test_force_destination_change_does_not_adopt_late_review_result(room_factory):
    bridge = ReviewBridge()
    bridge.release.clear()
    room = room_factory(bridge=bridge)
    s = seed(room)
    run = prepare(room)
    send(room, run)
    launch(room, '_ai_loop')
    assert bridge.entered.wait(2)
    room.configure_ai({'provider': 'api', 'model': 'test-model', 'api_key': 'test-only', 'force': True})
    bridge.release.set()
    wait_for(lambda: room.active_ai is None)
    stored = room.review_run(s, run['id'])
    assert stored['status'] == 'paused'
    assert stored['batches'][0]['attempts'][0]['raw_ai_output']
    assert not s['review']['cards']


def test_mark_cas_conflict_keeps_note(room_factory):
    room = room_factory()
    s = seed(room)
    payload = {'session_id': s['id'], 'request_id': 'mark-cas-first', 'target_kind': 'segment',
               'target_id': 'S0001', 'kind': 'important', 'revision': 0, 'note': 'Original'}
    mark = room.set_review_mark(payload)
    with pytest.raises(HTTPException) as error:
        room.set_review_mark({**payload, 'request_id': 'mark-cas-stale', 'note': 'Stale'})
    assert error.value.status_code == 409
    assert room.review_public(s)['marks'][0]['note'] == mark['note']


def test_external_import_rejects_review_records_runtime_cannot_read(room_factory):
    from portable_package import validate_session
    room = room_factory()
    seed(room)
    prepare(room)
    damaged = copy.deepcopy(room.get())
    del damaged['review']['runs'][0]['fingerprint']
    with pytest.raises(HTTPException) as error:
        validate_session(damaged)
    assert error.value.status_code == 422


def test_api_unknown_attempt_is_counted_as_unknown_not_known_failure(room_factory):
    class ApiReview(ReviewBridge):
        def metadata(self):
            return {'provider': 'api', 'model': 'synthetic', 'usage': None}
    bridge = ApiReview(CodexBridgeError('timeout', kind='timeout'))
    room = room_factory(bridge=bridge)
    room.connection.values.update(provider='api', model='synthetic')
    room.connection.key = 'synthetic-test-only'
    room.connection.api_factory = lambda *args, **kwargs: bridge
    s = seed(room)
    run = prepare(room)
    send(room, run)
    launch(room, '_ai_loop')
    wait_for(lambda: room.review_run(s, run['id'])['status'] == 'unknown' and room.active_ai is None)
    assert s['ai_usage']['requests'] == 1
    assert s['ai_usage']['unknown'] == 1 and s['ai_usage']['failed'] == 0


def test_reconstructed_same_text_version_reuses_existing_generation(room_factory):
    room = room_factory()
    s = seed(room)
    first = prepare(room)
    room.edit_segment(s['id'], 'S0001', 'Later correction.')
    replayed = prepare(room, 'same-observed-version', seen_segments=[
        {'id': 'S0001', 'version': 0}, {'id': 'S0002', 'version': 0}], seen_slides=[], seen_marks=[])
    assert replayed['id'] == first['id']
