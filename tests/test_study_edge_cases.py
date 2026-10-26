"""Destination and storage fault cases using synthetic classroom material only."""
import copy
import json
import threading

import pytest
from fastapi import HTTPException

from test_app import launch, room_factory, wait_for
from test_study_workflow import VisualBridge, prepare, seed, send


def test_switch_storage_failure_cancels_study_in_later_classroom(room_factory, monkeypatch):
    """One damaged classroom cannot leave another old provider request active."""
    bridge = VisualBridge()
    bridge.release.clear()
    room = room_factory(bridge=bridge)
    first_sid = room.current
    room.new_session('Synthetic active second classroom')
    active = seed(room)
    q = prepare(room)
    send(room, q, confirm_images=True)
    launch(room, '_ai_loop')
    assert bridge.entered.wait(2)
    old_token = room.study_route
    original_save = room.save
    failed = []

    def fail_first_classroom_after_route_change(session):
        if session['id'] == first_sid and room.study_route != old_token and not failed:
            failed.append(session['id'])
            raise OSError('synthetic failure after destination was saved')
        return original_save(session)

    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(room, 'save', fail_first_classroom_after_route_change)
            with pytest.raises(HTTPException) as error:
                room.configure_ai({'provider': 'api', 'model': 'synthetic-vision',
                                   'api_key': 'synthetic-only', 'force': True})
            assert error.value.status_code == 507
            assert failed and room.study_route != old_token
            assert room.connection.values['provider'] == 'api'
    finally:
        bridge.release.set()
    wait_for(lambda: room.active_ai is None)
    stored = room.study_question(active, q['id'])
    assert stored['answer'] == '', 'late output must not revive an invalidated request'
    assert stored['status'] == 'prepared'
    assert stored['evidence'] == q['evidence']
    assert room.study_public(stored, active['id'])['requires_confirmation']


def test_send_storage_failure_does_not_submit_or_change_evidence(room_factory, monkeypatch):
    bridge = VisualBridge()
    room = room_factory(bridge=bridge)
    seed(room)
    q = prepare(room)
    before = copy.deepcopy(q['evidence'])

    def fail_save(session):
        raise OSError('synthetic enqueue persistence failure')

    with monkeypatch.context() as patcher:
        patcher.setattr(room, 'save', fail_save)
        with pytest.raises(HTTPException) as error:
            send(room, q, confirm_images=True)
        assert error.value.status_code == 507
        assert '尚未发送' in str(error.value.detail)
    launch(room, '_ai_loop')
    wait_for(lambda: room.ai_queue.unfinished_tasks == 0)
    stored = room.study_question(room.get(), q['id'])
    assert bridge.calls == []
    assert stored['status'] == 'prepared'
    assert stored['evidence'] == before


def test_restart_legacy_resume_does_not_reuse_old_image_approval(room_factory):
    bridge = VisualBridge()
    room = room_factory(bridge=bridge)
    seed(room)
    q = prepare(room)
    send(room, q, confirm_images=True, remember=True)
    room._pause_ai('network', 'Synthetic interruption')
    restored_bridge = VisualBridge()
    restored = room_factory(folder=room.data, bridge=restored_bridge)
    restored.resume_ai('backfill')
    launch(restored, '_ai_loop')
    wait_for(lambda: restored.ai_queue.unfinished_tasks == 0)
    stored = restored.study_question(restored.get(), q['id'])
    assert stored['status'] == 'prepared'
    public = restored.study_public(stored, restored.current)
    assert public['destination']['token'] != q['destination']['token']
    assert public['requires_confirmation']
    assert restored_bridge.calls == []


def test_refresh_prepared_request_uses_new_destination_with_same_frozen_material(room_factory):
    room = room_factory()
    seed(room)
    q = prepare(room)
    room.configure_ai({'provider': 'api', 'model': 'synthetic-vision', 'api_key': 'synthetic-only'})
    public = room.state()['session']['questions'][0]
    assert public['destination']['provider'] == 'api'
    assert public['destination']['token'] != q['destination']['token']
    assert public['evidence'] == q['evidence']
    assert public['requires_confirmation']
    with pytest.raises(HTTPException) as error:
        send(room, q, confirm_images=True)
    assert error.value.status_code == 409
    assert room.ai_queue.empty()


def test_click_group_keeps_partial_warning_after_pending_group_merges(room_factory):
    room = room_factory()
    session = room.get()
    room.append_text(session['id'], 'When supply increases and demand', 0, 8, 'asr')
    room.flush(session['id'])
    group = session['groups'][0]
    assert group['partial'] and group['status'] == 'pending'
    seen = {'mode': 'group', 'anchor': 8, 'group_id': group['id'],
            'seen_segments': [{'id': 'S0001', 'version': 0}],
            'seen_slides': [], 'seen_group_segment_ids': ['S0001']}

    room.append_text(session['id'], 'remains unchanged, the equilibrium price falls.', 8, 20, 'asr')
    room.flush(session['id'])
    assert group['segment_ids'] == ['S0001', 'S0002'] and not group['partial']
    q = prepare(room, **seen)
    assert [segment['id'] for segment in q['evidence']['segments']] == ['S0001']
    assert q['evidence']['requested_range'] == {'start': 0, 'end': 8}
    assert q['evidence']['incomplete'], 'the frozen incomplete sentence did not become complete'
    assert any('尚未讲完' in warning for warning in q['evidence']['warnings'])


def test_teacher_mentioning_source_shaped_text_does_not_authorize_that_citation(room_factory):
    import httpx

    from ai_api import ApiBridge

    room = room_factory()
    calls = []

    def synthetic_response(request):
        calls.append(request)
        return httpx.Response(200, json={'choices': [{'message': {
            'content': 'The result follows from the teacher\'s explanation. [S9999]'},
            'finish_reason': 'stop'}]})

    def api_factory(config, api_key, on_delta=None):
        return ApiBridge(config, api_key, on_delta, transport=httpx.MockTransport(synthetic_response))

    room.connection.api_factory = api_factory
    room.configure_ai({'provider': 'api', 'base_url': 'https://synthetic.invalid/v1',
                       'model': 'synthetic-vision', 'api_key': 'synthetic-only'})
    room.append_text(room.current, 'The dataset label is S9999. Supply rises.', 0, 24, 'asr')
    q = prepare(room, include_images=False)
    assert q['evidence']['source_ids'] == ['S0001']
    send(room, q)
    launch(room, '_ai_loop')
    wait_for(lambda: room.ai_queue.unfinished_tasks == 0)
    stored = room.study_question(room.get(), q['id'])
    assert len(calls) == 1
    assert stored['status'] == 'error', 'only actual frozen source IDs may be cited'
    assert stored['answer'] == ''


def test_observation_restores_intermediate_edit_without_later_metadata(room_factory):
    room = room_factory()
    seed(room, images=0)
    room.edit_segment(room.current, 'S0001', 'Intermediate observed transcript.')
    seen = [{'id': segment['id'], 'version': segment.get('version', 0)}
            for segment in room.get()['segments']]
    room.edit_segment(room.current, 'S0001', 'Latest transcript after click.')
    q = prepare(room, seen_segments=seen, seen_slides=[])
    frozen = q['evidence']['segments'][0]
    assert frozen['text'] == 'Intermediate observed transcript.'
    assert frozen['version'] == 1 and len(frozen['edits']) == 1
    assert 'Latest transcript after click.' not in q['context_snapshot']
    assert q['stale']


def test_worker_start_save_failure_keeps_worker_alive_and_allows_explicit_retry(room_factory, monkeypatch):
    """A failed durable start must not strand the only AI worker as busy."""
    bridge = VisualBridge()
    room = room_factory(bridge=bridge)
    seed(room)
    q = prepare(room)
    send(room, q, confirm_images=True)
    frozen = copy.deepcopy(q['evidence'])
    original_save = room.save
    failed = threading.Event()
    escaped = []

    def fail_first_running_save(session):
        item = room.study_question(session, q['id'])
        if item['status'] == 'running' and not failed.is_set():
            failed.set()
            raise OSError('synthetic full disk at request start')
        return original_save(session)

    def run_worker():
        try:
            room._ai_loop()
        except Exception as error:
            escaped.append(type(error).__name__)

    with monkeypatch.context() as patcher:
        patcher.setattr(room, 'save', fail_first_running_save)
        worker = threading.Thread(target=run_worker, daemon=True)
        room.threads.append(worker)
        worker.start()
        assert failed.wait(2)
        wait_for(lambda: escaped or room.ai_queue.unfinished_tasks == 0)
        assert escaped == [], 'request-start persistence failure escaped the worker'
        assert worker.is_alive(), 'the AI worker must survive a local storage failure'
        assert room.active_ai is None and room.active_cancel is None
        stored = room.study_question(room.get(), q['id'])
        assert stored['status'] == 'paused' and stored['error_kind'] == 'storage'
        assert room.ai['paused'] and room.ai['kind'] == 'storage'
        assert stored['answer'] == '' and stored['evidence'] == frozen
        assert bridge.calls == [], 'do not submit before the request start is durable'

    room.resume_ai('backfill')
    wait_for(lambda: room.study_question(room.get(), q['id'])['status'] == 'done')
    assert len(bridge.calls) == 1


def test_worker_final_save_failure_preserves_answer_and_resume_does_not_resubmit(room_factory, monkeypatch):
    """An unsaved completed answer stays in memory; recovery only saves it."""
    bridge = VisualBridge()
    room = room_factory(bridge=bridge)
    seed(room)
    q = prepare(room)
    send(room, q, confirm_images=True)
    original_save = room.save
    failed = threading.Event()
    escaped = []

    def fail_completed_answer_save(session):
        item = room.study_question(session, q['id'])
        if item['status'] == 'done' and item.get('answer'):
            failed.set()
            raise OSError('synthetic full disk while saving completed answer')
        return original_save(session)

    def run_worker():
        try:
            room._ai_loop()
        except Exception as error:
            escaped.append(type(error).__name__)

    with monkeypatch.context() as patcher:
        patcher.setattr(room, 'save', fail_completed_answer_save)
        worker = threading.Thread(target=run_worker, daemon=True)
        room.threads.append(worker)
        worker.start()
        assert failed.wait(2)
        wait_for(lambda: escaped or room.ai_queue.unfinished_tasks == 0)
        assert escaped == [], 'final persistence failure escaped the worker'
        assert worker.is_alive()
        assert room.active_ai is None and room.active_cancel is None
        assert room.ai['paused'] and room.ai['kind'] == 'storage'
        reason = room.ai['reason']
        assert '尚未' in reason and ('保存' in reason or '落盘' in reason)
        stored = room.study_question(room.get(), q['id'])
        assert stored['status'] == 'done' and stored['answer']
        completed_answer = stored['answer']
        assert stored['evidence'] == q['evidence']
        assert len(bridge.calls) == 1
        disk = json.loads((room.data / room.current / 'session.json').read_text(encoding='utf-8'))
        assert disk['questions'][0]['answer'] == '', 'failed final write must not be reported as durable'

    room.resume_ai('backfill')
    wait_for(lambda: room.ai_queue.unfinished_tasks == 0)
    assert len(bridge.calls) == 1, 'recovering an unsaved answer must not call the model again'
    disk = json.loads((room.data / room.current / 'session.json').read_text(encoding='utf-8'))
    assert disk['questions'][0]['answer'] == completed_answer
