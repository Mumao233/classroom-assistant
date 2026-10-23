"""Backend acceptance tests using only generated silent WAVs and fake services.

No microphone, existing recording, local Whisper model or paid AI is touched.
"""
import io
import json
import queue
import sys
import threading
import time
import wave
from collections import deque
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Classroom, boundary_after, boundary_before, create_app


class FakeASR:
    def __init__(self, responses=None, blocked=False):
        self.responses = deque(responses or ["A short classroom sentence."])
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        if not blocked:
            self.release.set()

    def status(self):
        return {"ready": True, "available": True, "device": "fake", "busy": False}

    def transcribe(self, path, prompt=""):
        self.calls.append((str(path), prompt))
        self.entered.set()
        if not self.release.wait(4):
            raise RuntimeError("test gate was not released")
        value = self.responses.popleft() if self.responses else "Another sentence."
        if isinstance(value, BaseException):
            raise value
        return {"text": value, "elapsed": 0.01, "device": "fake", "language": "en"}


class FakeBridge:
    def __init__(self, responses=None):
        self.responses = deque(responses or [])
        self.calls = []

    def status(self):
        return {"installed": True, "logged_in": True, "inference_verified": False}

    def _result(self):
        value = self.responses.popleft() if self.responses else "解释 [S0001]"
        if isinstance(value, BaseException):
            raise value
        return value

    def analyze(self, context, new_text):
        self.calls.append({"kind": "analysis", "context": context, "new_text": new_text})
        return self._result()

    def ask(self, context, question, history):
        self.calls.append({"kind": "question", "context": context, "question": question, "history": history})
        return self._result()


def synthetic_wav(seconds=1.0, rate=16000, channels=1):
    data = io.BytesIO()
    with wave.open(data, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"\0\0" * round(rate * seconds) * channels)
    return data.getvalue()


def wait_for(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "background work did not reach the expected state"


def launch(room, method):
    thread = threading.Thread(target=getattr(room, method), daemon=True)
    room.threads.append(thread)
    thread.start()


@pytest.fixture
def room_factory(tmp_path):
    rooms = []

    def factory(engine=None, bridge=None, folder=None):
        room = Classroom(folder or tmp_path / f"room-{len(rooms)}", {}, engine or FakeASR(), bridge or FakeBridge())
        rooms.append(room)
        return room

    yield factory
    for room in rooms:
        if hasattr(room.engine, "release"):
            room.engine.release.set()
        room.stop()


@pytest.fixture
def local_client(tmp_path):
    engine = FakeASR()
    bridge = FakeBridge()
    application = create_app({}, tmp_path / "web", engine=engine, bridge=bridge)
    with TestClient(application) as client:
        yield client, application.state.room, engine, bridge


def test_audio_upload_is_idempotent_before_and_after_processing(room_factory):
    engine = FakeASR()
    room = room_factory(engine=engine)
    sid = room.current
    room.settings({"auto_analyze": False})
    wav = synthetic_wav()
    accepted = room.accept_audio(sid, 41, 0, wav)
    duplicate = room.accept_audio(sid, 41, 0, wav)
    assert duplicate["duplicate"] is True
    assert duplicate["job_id"] == accepted["job_id"]
    assert room.audio_queue.qsize() == 1
    launch(room, "_audio_loop")
    wait_for(lambda: room.jobs[accepted["job_id"]]["status"] == "done")
    assert room.accept_audio(sid, 41, 0, wav)["duplicate"] is True
    assert len(engine.calls) == len(room.get(sid)["audio"]) == len(room.get(sid)["segments"]) == 1


def test_stop_flush_waits_for_all_asr_chunks_then_groups_once(room_factory):
    engine = FakeASR(["When the price", "increases, quantity demanded falls."], blocked=True)
    room = room_factory(engine=engine)
    sid = room.current
    room.settings({"auto_analyze": False})
    room.accept_audio(sid, 1, 0, synthetic_wav())
    room.accept_audio(sid, 2, 1, synthetic_wav())
    launch(room, "_audio_loop")
    assert engine.entered.wait(2)
    result = room.flush(sid)
    assert result["waiting_for_audio"] is True
    assert room.get(sid)["groups"] == []
    engine.release.set()
    wait_for(lambda: room.audio_queue.unfinished_tasks == 0)
    session = room.get(sid)
    assert session["flush_requested"] is False
    assert session["pending_ids"] == []
    assert len(session["groups"]) == 1
    assert session["groups"][0]["segment_ids"] == ["S0001", "S0002"]
    assert session["groups"][0]["text"] == "When the price increases, quantity demanded falls."


def test_inflight_audio_stays_with_original_class_after_switch(room_factory):
    engine = FakeASR(["Old class content."], blocked=True)
    room = room_factory(engine=engine)
    original = room.current
    room.settings({"auto_analyze": False, "terms": "old-only terminology"})
    room.accept_audio(original, 10, 0, synthetic_wav())
    launch(room, "_audio_loop")
    assert engine.entered.wait(2)
    newer = room.new_session("Second class")["id"]
    room.flush(original)
    engine.release.set()
    wait_for(lambda: room.audio_queue.unfinished_tasks == 0)
    assert room.current == newer
    assert room.get(newer)["segments"] == room.get(newer)["groups"] == []
    assert room.get(original)["segments"][0]["text"] == "Old class content."
    assert engine.calls[0][1] == "old-only terminology"


def test_ai_question_context_is_isolated_by_session(room_factory):
    bridge = FakeBridge()
    room = room_factory(bridge=bridge)
    first = room.current
    room.settings({"auto_analyze": False})
    room.append_text(first, "Alpha-only material.")
    second = room.new_session("Beta course")["id"]
    room.settings({"auto_analyze": False})
    room.append_text(second, "Beta-only material.")
    job = room.ask("Explain Alpha", sid=first)
    launch(room, "_ai_loop")
    wait_for(lambda: room.jobs[job["id"]]["status"] == "done")
    call = bridge.calls[0]
    assert "Alpha-only material." in call["context"]
    assert "Beta-only material." not in call["context"]
    assert room.get(second)["questions"] == []
    assert room.get(first)["questions"][0]["answer"]


def test_incomplete_sentence_waits_but_safety_cap_is_bounded():
    fragment = {"text": "An unfinished argument because", "start": 0, "end": 29}
    assert boundary_after([fragment]) is False
    assert boundary_after([{**fragment, "end": 44}]) is False
    assert boundary_after([{**fragment, "end": 45}]) is True
    complete = {"text": "This is a complete point.", "start": 0, "end": 29}
    assert boundary_after([complete]) is True


def test_topic_cue_does_not_split_incomplete_previous_sentence():
    old = {"text": " ".join(["word"] * 45), "start": 0, "end": 20}
    new = {"text": "Next, consider inflation.", "start": 20, "end": 25}
    assert boundary_before([old], new) is False
    assert boundary_before([{**old, "text": old["text"] + "."}], new) is True


def test_manual_flush_preserves_a_short_unfinished_fragment(room_factory):
    room = room_factory()
    room.settings({"auto_analyze": False})
    room.append_text(room.current, "Because of the")
    assert room.get()["groups"] == []
    room.flush()
    assert room.get()["groups"][0]["text"] == "Because of the"
    assert room.get()["groups"][0]["status"] == "saved"


def test_questions_jump_ahead_of_queued_automatic_analysis(room_factory):
    bridge = FakeBridge()
    room = room_factory(bridge=bridge)
    room.append_text(room.current, "The inflation rate has changed.")
    room.flush()
    room.ask("What is inflation?")
    launch(room, "_ai_loop")
    wait_for(lambda: room.ai_queue.unfinished_tasks == 0)
    assert [call["kind"] for call in bridge.calls] == ["question", "analysis"]
    assert all(item["status"] == "done" for item in room.get()["groups"] + room.get()["questions"])


@pytest.mark.parametrize("kind", ["analysis", "question"])
def test_failed_ai_item_can_retry_without_duplicate_record(room_factory, kind):
    bridge = FakeBridge([RuntimeError("temporary fake outage"), "Recovered [S0001]"])
    room = room_factory(bridge=bridge)
    room.settings({"auto_analyze": kind == "analysis"})
    room.append_text(room.current, "A recoverable classroom point.")
    if kind == "analysis":
        room.flush()
        items = room.get()["groups"]
    else:
        room.ask("Explain this.")
        items = room.get()["questions"]
    item = items[0]
    launch(room, "_ai_loop")
    wait_for(lambda: item["status"] == "error")
    old_job = item["job_id"]
    new_job = room.retry(kind, item["id"])
    assert new_job["id"] != old_job
    wait_for(lambda: item["status"] == "done")
    assert len(items) == 1
    assert "error" not in item
    assert room.jobs[old_job]["status"] == "error"
    assert room.jobs[new_job["id"]]["status"] == "done"


def test_pending_ai_item_cannot_be_enqueued_again(room_factory):
    room = room_factory()
    room.ask("Explain this.")
    with pytest.raises(HTTPException) as error:
        room.retry("question", "Q0001")
    assert error.value.status_code == 409
    assert room.ai_queue.qsize() == 1


def test_sessions_and_unfinished_work_survive_restart(room_factory, tmp_path):
    folder = tmp_path / "persisted"
    original = room_factory(folder=folder)
    sid = original.current
    original.append_text(sid, "Persistent source text.")
    original.flush()
    original.ask("Persistent question?")
    original.accept_audio(sid, 20, 12, synthetic_wav())
    original.stop()
    restored = room_factory(folder=folder)
    session = restored.get(sid)
    assert session["segments"][0]["text"] == "Persistent source text."
    assert session["groups"][0]["status"] == "error"
    assert session["questions"][0]["status"] == "error"
    assert "中断" in session["questions"][0]["error"]
    assert session["audio"][0]["status"] == "pending"
    restored.start()
    wait_for(lambda: session["audio"][0]["status"] == "done")
    wait_for(lambda: restored.audio_queue.unfinished_tasks == 0)
    assert len(session["segments"]) == 2
    saved = json.loads((folder / sid / "session.json").read_text(encoding="utf-8"))
    assert saved["audio"][0]["status"] == "done"
    assert saved["segments"][-1]["audio_seq"] == 20


def test_restart_recovers_more_pending_audio_than_queue_capacity(room_factory, tmp_path):
    folder = tmp_path / "backlog"
    original = room_factory(folder=folder)
    sid = original.current
    original.settings({"auto_analyze": False})
    audio_dir = folder / sid / "audio"
    audio_dir.mkdir()
    wav = synthetic_wav(seconds=0.1)
    # Represent a persisted backlog, e.g. one running and 120 waiting when the
    # prior process ended. Restore must not apply live-upload backpressure.
    for seq in range(121):
        (audio_dir / f"{seq}.wav").write_bytes(wav)
        original.get(sid)["audio"].append({"seq": seq, "start": seq,
            "duration": .1, "path": f"audio/{seq}.wav", "status": "pending"})
    original.save(original.get(sid))
    original.stop()
    engine = FakeASR(blocked=True)
    restored = room_factory(folder=folder, engine=engine)
    # Startup must not crash or silently drop saved recordings when replay fills
    # the same bounded queue used by live uploads.
    restored.start()
    engine.release.set()
    wait_for(lambda: all(item["status"] == "done" for item in restored.get(sid)["audio"]), timeout=12)
    assert len(restored.get(sid)["segments"]) == 121


def test_audio_failure_retry_uses_saved_synthetic_wav(local_client):
    client, room, engine, _ = local_client
    engine.responses = deque([RuntimeError("synthetic decoder outage"), "Recovered audio sentence."])
    room.settings({"auto_analyze": False})
    sid = room.current
    first = client.post(f"/api/audio?session_id={sid}&seq=8&start=0", content=synthetic_wav())
    assert first.status_code == 200
    wait_for(lambda: room.get(sid)["audio"][0]["status"] == "error")
    retry = client.post("/api/audio/8/retry", json={"session_id": sid})
    assert retry.status_code == 200
    wait_for(lambda: room.get(sid)["audio"][0]["status"] == "done")
    assert len(room.get(sid)["segments"]) == 1
    assert len(engine.calls) == 2
    assert "error" not in room.get(sid)["audio"][0]
    assert client.post("/api/audio/8/retry", json={"session_id": sid}).status_code == 409


@pytest.mark.parametrize("start", ["nan", "inf", "-inf", "-1", "604801"])
def test_audio_rejects_nonfinite_or_out_of_range_timestamp(local_client, start):
    client, room, _, _ = local_client
    response = client.post(f"/api/audio?session_id={room.current}&seq=1&start={start}", content=synthetic_wav())
    assert 400 <= response.status_code < 500
    assert room.get()["audio"] == []
    assert client.get("/api/state").status_code == 200


@pytest.mark.parametrize("body", [b"not a wav", synthetic_wav(rate=8000), synthetic_wav(channels=2), synthetic_wav(seconds=0.05)], ids=["invalid-header", "wrong-rate", "stereo", "too-short"])
def test_rejects_bad_audio_without_scheduling(local_client, body):
    client, room, _, _ = local_client
    response = client.post(f"/api/audio?session_id={room.current}&seq=2", content=body)
    assert response.status_code == 400
    assert room.get()["audio"] == []


def test_audio_queue_backpressure_does_not_write_or_acknowledge(room_factory):
    room = room_factory()
    room.accept_audio(room.current, 1, 0, synthetic_wav())
    for seq in range(119):
        room.audio_queue.put_nowait((room.current, seq + 100, "not-started"))
    with pytest.raises(HTTPException) as error:
        room.accept_audio(room.current, 2, 1, synthetic_wav())
    assert error.value.status_code == 429
    assert len(room.get()["audio"]) == 1
    assert not (room.data / room.current / "audio" / "2.wav").exists()


def test_foreign_origin_and_foreign_host_are_rejected(local_client):
    client, room, _, _ = local_client
    before = len(room.sessions)
    response = client.post("/api/sessions", json={"title": "blocked"}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert len(room.sessions) == before
    assert client.get("/api/state", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/api/state", headers={"Origin": "http://testserver"}).status_code == 200


def test_testserver_origin_is_not_a_production_cross_origin_exception(local_client):
    client, room, _, _ = local_client
    before = len(room.sessions)
    response = client.post("/api/sessions", json={"title": "cross-origin"}, headers={"Host": "127.0.0.1:8766", "Origin": "http://testserver"})
    assert response.status_code == 403
    assert len(room.sessions) == before


def test_demo_does_not_trigger_asr_or_paid_ai_and_exports(local_client):
    client, room, engine, bridge = local_client
    response = client.post("/api/demo")
    assert response.status_code == 200
    sid = response.json()["session"]["id"]
    assert room.get(sid)["demo"] is True
    assert client.post("/api/ask", json={"question": "Explain", "session_id": sid}).status_code == 400
    assert client.post(f"/api/audio?session_id={sid}&seq=1", content=synthetic_wav()).status_code == 400
    exported = client.get(f"/api/export?format=json&session_id={sid}")
    assert exported.status_code == 200
    assert len(exported.json()["segments"]) == 3
    markdown = client.get(f"/api/export?format=markdown&session_id={sid}")
    assert "S0001" in markdown.text and "固定演示内容" in markdown.text
    assert engine.calls == bridge.calls == []


def test_empty_or_missing_ids_do_not_create_work(local_client):
    client, room, _, _ = local_client
    assert client.post("/api/text", json={"text": ""}).status_code == 422
    assert client.post("/api/ask", json={"question": ""}).status_code == 422
    assert client.post("/api/ask", json={"question": "x", "session_id": "missing"}).status_code == 404
    assert client.post("/api/groups/G9999/retry", json={}).status_code == 404
    assert room.get()["segments"] == room.get()["questions"] == []


def test_restart_after_segment_saved_does_not_duplicate_transcript(room_factory, tmp_path):
    folder = tmp_path / "interrupted-marker"
    original = room_factory(folder=folder)
    sid = original.current
    original.settings({"auto_analyze": False})
    original.accept_audio(sid, 7, 0, synthetic_wav())
    original.append_text(sid, "Already committed text.", 0, 1, "microphone", 7)
    # Simulate the crash window after text was committed but before the audio
    # record received its completion marker.
    original.get(sid)["audio"][0]["status"] = "running"
    original.save(original.get(sid))
    original.stop()
    restored = room_factory(folder=folder, engine=FakeASR(["A second decoding must not replace it."]))
    restored.start()
    wait_for(lambda: restored.get(sid)["audio"][0]["status"] == "done")
    assert len(restored.get(sid)["segments"]) == 1
    assert restored.get(sid)["segments"][0]["text"] == "Already committed text."


def test_replay_registered_audio_uses_explicit_class_not_current_class(local_client):
    client, room, _, _ = local_client
    first = room.current
    wav = synthetic_wav(seconds=.2)
    room.accept_audio(first, 13, 0, wav)
    second = room.new_session("New current class")["id"]
    replay = client.get(f"/api/audio/13?session_id={first}")
    assert replay.status_code == 200
    assert replay.headers["content-type"] == "audio/wav"
    assert replay.content == wav
    assert replay.headers["cache-control"] == "no-store"
    assert client.get(f"/api/audio/13?session_id={second}").status_code == 404


def test_replay_rejects_unregistered_and_missing_files(local_client):
    client, room, _, _ = local_client
    sid = room.current
    folder = room.data / sid / "audio"
    folder.mkdir(exist_ok=True)
    (folder / "19.wav").write_bytes(synthetic_wav(seconds=.1))
    # File existence alone does not authorize this recording for replay.
    assert client.get(f"/api/audio/19?session_id={sid}").status_code == 404
    with room.lock:
        room.get(sid)["audio"].append({"seq": 20, "path": "audio/missing.wav", "status": "done"})
    assert client.get(f"/api/audio/20?session_id={sid}").status_code == 404
    assert client.get("/api/audio/19?session_id=not-a-course").status_code == 404
    assert client.get("/api/audio/19").status_code == 422


@pytest.mark.parametrize("escape", ["parent", "absolute", "other-course"])
def test_replay_rejects_registered_paths_outside_course_audio_folder(local_client, escape):
    client, room, _, _ = local_client
    sid = room.current
    outside = room.data / sid / "private.wav"
    outside.write_bytes(b"must not be served")
    if escape == "parent":
        path = "audio/../private.wav"
    elif escape == "absolute":
        path = str(outside.resolve())
    else:
        other = room.new_session("Other course")["id"]
        other_audio = room.data / other / "audio"
        other_audio.mkdir()
        (other_audio / "31.wav").write_bytes(b"other course recording")
        path = f"../{other}/audio/31.wav"
    with room.lock:
        room.get(sid)["audio"].append({"seq": 31, "path": path, "status": "done"})
    result = client.get(f"/api/audio/31?session_id={sid}")
    assert result.status_code == 404
    assert b"must not be served" not in result.content
    assert b"other course recording" not in result.content


def test_retry_done_audio_without_text_then_reject_after_text_exists(local_client):
    client, room, engine, _ = local_client
    sid = room.current
    room.settings({"auto_analyze": False})
    engine.responses = deque(["", "Now the classroom speech is audible."])
    response = client.post(f"/api/audio?session_id={sid}&seq=61&start=0", content=synthetic_wav())
    assert response.status_code == 200
    wait_for(lambda: room.get(sid)["audio"][0]["status"] == "done")
    assert room.get(sid)["segments"] == []
    assert client.post("/api/audio/61/retry", json={"session_id": sid}).status_code == 200
    wait_for(lambda: room.get(sid)["audio"][0]["status"] == "done" and len(engine.calls) == 2)
    assert len(room.get(sid)["segments"]) == 1
    assert room.get(sid)["segments"][0]["audio_seq"] == 61
    assert client.post("/api/audio/61/retry", json={"session_id": sid}).status_code == 409
    # Even an interrupted completion marker must not cause duplicate text.
    with room.lock:
        room.get(sid)["audio"][0]["status"] = "error"
    assert client.post("/api/audio/61/retry", json={"session_id": sid}).status_code == 409
    assert len(engine.calls) == 2


@pytest.mark.parametrize("prior_history", [False, True], ids=["empty-history", "existing-history"])
def test_queued_question_keeps_submission_context_and_history_snapshot(room_factory, prior_history):
    bridge = FakeBridge()
    room = room_factory(bridge=bridge)
    sid = room.current
    room.settings({"auto_analyze": False})
    room.append_text(sid, "Before asking: an increase in demand.")
    session = room.get(sid)
    if prior_history:
        session["questions"].append({"id": "Q0001", "question": "Previous question",
            "answer": "Original prior answer", "status": "done"})
    job = room.ask("Explain what the teacher just said.", sid=sid)
    item = session["questions"][-1]
    snapshot = item["context_snapshot"]
    expected_history = ([{"role": "user", "content": "Previous question"},
                         {"role": "assistant", "content": "Original prior answer"}] if prior_history else [])
    assert item["history_snapshot"] == expected_history
    persisted = json.loads((room.data / sid / "session.json").read_text(encoding="utf-8"))
    assert persisted["questions"][-1]["context_snapshot"] == snapshot
    assert persisted["questions"][-1]["history_snapshot"] == expected_history
    room.append_text(sid, "After asking: a later, unrelated topic about unemployment.")
    if prior_history:
        session["questions"][0]["answer"] = "Modified later answer"
    session["questions"].append({"id": "Q9999", "question": "Future question",
        "answer": "Future answer", "status": "done"})
    launch(room, "_ai_loop")
    wait_for(lambda: room.jobs[job["id"]]["status"] == "done")
    call = bridge.calls[0]
    assert call["context"] == snapshot
    assert "Before asking:" in call["context"]
    assert "After asking:" not in call["context"]
    assert call["history"] == expected_history


def test_question_retry_after_restart_keeps_original_context_snapshot(room_factory, tmp_path):
    folder = tmp_path / "snapshot-restart"
    original = room_factory(folder=folder)
    sid = original.current
    original.settings({"auto_analyze": False})
    original.append_text(sid, "Original moment: opportunity cost.")
    original.ask("Explain just now.")
    original.append_text(sid, "Later moment: gross domestic product.")
    original.stop()
    bridge = FakeBridge()
    restored = room_factory(folder=folder, bridge=bridge)
    assert restored.get(sid)["questions"][0]["status"] == "error"
    job = restored.retry("question", "Q0001", sid)
    launch(restored, "_ai_loop")
    wait_for(lambda: restored.jobs[job["id"]]["status"] == "done")
    assert "Original moment:" in bridge.calls[0]["context"]
    assert "Later moment:" not in bridge.calls[0]["context"]


def test_analysis_context_contains_only_prior_group_not_current_or_future(room_factory):
    bridge = FakeBridge()
    room = room_factory(bridge=bridge)
    sid = room.current
    room.settings({"auto_analyze": False})
    for text in ("PRIOR-TRANSCRIPT opportunity cost.", "CURRENT-TRANSCRIPT demand curve.", "FUTURE-TRANSCRIPT monetary policy."):
        room.append_text(sid, text)
        room.flush(sid)
    groups = room.get(sid)["groups"]
    for group, note in zip(groups, ("PRIOR-SUMMARY", "CURRENT-SUMMARY", "FUTURE-SUMMARY")):
        group["analysis"] = note
    job = room.retry("analysis", groups[1]["id"], sid)
    launch(room, "_ai_loop")
    wait_for(lambda: room.jobs[job["id"]]["status"] == "done")
    call = bridge.calls[0]
    assert call["kind"] == "analysis"
    assert "PRIOR-TRANSCRIPT" in call["context"]
    assert "PRIOR-SUMMARY" in call["context"]
    assert "CURRENT-TRANSCRIPT" not in call["context"]
    assert "CURRENT-SUMMARY" not in call["context"]
    assert "FUTURE-TRANSCRIPT" not in call["context"]
    assert "FUTURE-SUMMARY" not in call["context"]
    assert "[S0002] 00:02–00:04 CURRENT-TRANSCRIPT" in call["new_text"]
    assert "PRIOR-TRANSCRIPT" not in call["new_text"]
    assert "FUTURE-TRANSCRIPT" not in call["new_text"]
