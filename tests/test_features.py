"""State-machine checks for failures, revisions and edits; no live mic or AI."""
import json
import threading
import time

import pytest
from fastapi import HTTPException

from test_app import FakeASR, FakeBridge, launch, room_factory, local_client, synthetic_wav, wait_for
from codex_bridge import CodexBridgeError


class FullASR(FakeASR):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mode = "auto"
        self.high_quality_calls = 0

    def set_mode(self, mode):
        self.mode = mode
        return self.status()

    def status(self):
        return {**super().status(), "requested_mode": self.mode, "high_quality_available": True}

    def transcribe_high_quality(self, path, prompt=""):
        self.high_quality_calls += 1
        return self.transcribe(path, prompt)


class BlockingBridge(FakeBridge):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def analyze(self, context, new_text):
        self.entered.set()
        assert self.release.wait(3)
        return super().analyze(context, new_text)


@pytest.mark.parametrize("kind", ["network", "quota", "auth", "timeout"])
def test_ai_pauses_without_losing_questions_or_stopping_audio(room_factory, kind):
    bridge = FakeBridge([CodexBridgeError("Offline", kind=kind), "Recovered"])
    room = room_factory(bridge=bridge)
    first = room.ask("First question")
    room.ask("Second question")
    launch(room, "_ai_loop")
    wait_for(lambda: room.ai["paused"])
    room.ask("Asked offline")
    room.settings({"auto_analyze": False})
    room.accept_audio(room.current, 1, 0, synthetic_wav())
    launch(room, "_audio_loop")
    wait_for(lambda: room.get()["audio"][0]["status"] == "done")
    assert len(bridge.calls) == 1
    assert all(q["status"] == "paused" for q in room.get()["questions"])
    room.resume_ai("latest")
    wait_for(lambda: all(q["status"] == "done" for q in room.get()["questions"]))
    assert len(bridge.calls) == 4
    assert room.jobs[first["id"]]["status"] == "superseded"


def test_paused_restart_and_latest_preserve_explicit_questions(room_factory, tmp_path):
    folder = tmp_path / "restart"
    room = room_factory(folder=folder)
    room._pause_ai("network", "offline")
    for n in range(3):
        room.append_text(room.current, f"Point {n}.", n * 130, n * 130 + 30)
    room.ask("Keep my question")
    room.stop()
    restored = room_factory(folder=folder)
    assert restored.ai["paused"]
    restored.resume_ai("latest")
    assert [g["status"] for g in restored.get()["groups"]] == ["skipped", "skipped", "pending"]
    launch(restored, "_ai_loop")
    wait_for(lambda: restored.get()["questions"][0]["status"] == "done")
    wait_for(lambda: restored.get()["groups"][-1]["status"] == "done")


def test_backlog_merges_only_unstarted_automatic_groups(room_factory):
    room = room_factory()
    room.append_text(room.current, "First sentence.", 0, 30)
    room.append_text(room.current, "Second sentence.", 30, 60)
    groups = room.get()["groups"]
    assert len(groups) == 1
    assert groups[0]["segment_ids"] == ["S0001", "S0002"]
    assert groups[0]["merged_count"] == 2
    assert room.ai_queue.qsize() == 1
    groups[0]["status"] = "running"
    room.append_text(room.current, "Third sentence.", 60, 90)
    assert len(groups) == 2


def test_edit_invalidates_pending_analysis_and_preserves_machine_text(room_factory):
    room = room_factory()
    room.append_text(room.current, "Demand rises.", 0, 30)
    group = room.get()["groups"][0]
    old_job = group["job_id"]
    room.edit_segment(room.current, "S0001", "Supply rises.")
    room.edit_segment(room.current, "S0001", "Supply falls.")
    segment = room.get()["segments"][0]
    assert segment["machine_text"] == "Demand rises."
    assert len(segment["edits"]) == 2
    assert group["stale"] and group["status"] == "saved"
    launch(room, "_ai_loop")
    wait_for(lambda: room.ai_queue.unfinished_tasks == 0)
    assert not room.bridge.calls
    assert room.jobs[old_job]["status"] == "cancelled"
    room.retry("analysis", group["id"])
    wait_for(lambda: group["status"] == "done")
    assert not group["stale"]
    assert "Supply falls." in room.bridge.calls[0]["new_text"]


def test_edit_during_analysis_cannot_make_old_answer_fresh(room_factory):
    bridge = BlockingBridge()
    room = room_factory(bridge=bridge)
    room.append_text(room.current, "Original demand.", 0, 30)
    launch(room, "_ai_loop")
    assert bridge.entered.wait(2)
    room.edit_segment(room.current, "S0001", "Edited supply.")
    bridge.release.set()
    group = room.get()["groups"][0]
    wait_for(lambda: group["status"] == "done")
    assert group["stale"]
    assert "Original demand." in bridge.calls[0]["new_text"]
    assert "Edited supply." not in bridge.calls[0]["new_text"]


def test_edit_also_invalidates_analysis_that_used_it_as_context(room_factory):
    room = room_factory()
    room.append_text(room.current, "Earlier source.", 0, 30)
    launch(room, "_ai_loop")
    wait_for(lambda: room.get()["groups"][0]["status"] == "done")
    room.append_text(room.current, "Later source.", 30, 60)
    wait_for(lambda: room.get()["groups"][1]["status"] == "done")
    room.edit_segment(room.current, "S0001", "Corrected earlier source.")
    assert all(g["stale"] for g in room.get()["groups"])


def test_question_only_mode_cancels_unstarted_auto_notes(room_factory):
    room = room_factory()
    room.append_text(room.current, "A classroom point.", 0, 30)
    room.settings({"auto_analyze": False})
    room.ask("Still answer my explicit question")
    launch(room, "_ai_loop")
    wait_for(lambda: room.get()["questions"][0]["status"] == "done")
    assert room.get()["groups"][0]["status"] == "saved"
    assert [call["kind"] for call in room.bridge.calls] == ["question"]


def test_delete_recovers_when_registered_file_already_missing(room_factory):
    room = room_factory()
    room.accept_audio(room.current, 1, 0, synthetic_wav())
    room.get()["audio"][0]["status"] = "done"
    (room.data / room.current / "audio" / "1.wav").unlink()
    assert room.delete_audio(room.current)["deleted"] == 1
    assert room.get()["audio"][0]["deleted"]


def test_focused_old_group_avoids_unrelated_live_context(room_factory):
    room = room_factory()
    room.settings({"auto_analyze": False})
    for n in range(40):
        room.append_text(room.current, f"TOPIC-{n} material.", n * 30, n * 30 + 30)
    context = room.context(room.get(), "Explain this point", "G0002")
    assert "TOPIC-1 material." in context and "TOPIC-2 material." in context
    assert "TOPIC-39 material." not in context
    assert "00:30–01:00" in context


def test_soundcheck_is_independent_and_uses_same_audio_worker(room_factory):
    engine = FakeASR(["Test microphone only.", "Actual lecture."])
    room = room_factory(engine=engine)
    room.settings({"auto_analyze": False})
    job = room.accept_soundcheck(synthetic_wav(10))
    with pytest.raises(HTTPException):
        room.accept_soundcheck(synthetic_wav(10))
    launch(room, "_audio_loop")
    wait_for(lambda: room.soundcheck["status"] == "done")
    assert room.soundcheck["job_id"] == job["job_id"]
    assert room.soundcheck["text"] == "Test microphone only."
    assert room.get()["segments"] == room.get()["audio"] == []
    room.accept_audio(room.current, 1, 0, synthetic_wav())
    wait_for(lambda: len(room.get()["segments"]) == 1)
    assert room.get()["segments"][0]["text"] == "Actual lecture."
    assert room.state()["queue"]["ai"] == 0


def test_revision_retains_original_and_delete_only_registered_audio(room_factory):
    engine = FullASR(["Original sentence.", "Corrected sentence."])
    room = room_factory(engine=engine)
    room.settings({"auto_analyze": False})
    sid = room.current
    room.accept_audio(sid, 1, 0, synthetic_wav())
    launch(room, "_audio_loop")
    wait_for(lambda: room.get()["audio"][0]["status"] == "done")
    room.flush()
    original = json.loads(json.dumps(room.get()["segments"]))
    room.retranscribe(sid)
    wait_for(lambda: room.get()["revisions"][0]["status"] == "done")
    assert room.get()["segments"] == original
    assert room.get()["revisions"][0]["segments"][0]["text"] == "Corrected sentence."
    assert room.get()["groups"][0]["stale"]
    assert engine.high_quality_calls == 1
    other = room.data / sid / "audio" / "unregistered.wav"
    other.write_bytes(b"keep")
    room.delete_audio(sid)
    assert room.get()["audio"][0]["deleted"]
    assert room.get()["segments"] == original and other.exists()
    assert not (room.data / sid / "audio" / "1.wav").exists()


def test_delete_refuses_running_work_or_escape_path(room_factory):
    room = room_factory()
    room.accept_audio(room.current, 1, 0, synthetic_wav())
    with pytest.raises(HTTPException) as exc:
        room.delete_audio(room.current)
    assert exc.value.status_code == 409
    audio = room.get()["audio"][0]
    audio["status"] = "done"
    protected = room.data / "protected.wav"
    protected.write_bytes(b"protect")
    audio["path"] = "../protected.wav"
    with pytest.raises(HTTPException):
        room.delete_audio(room.current)
    assert protected.read_bytes() == b"protect"


def test_modes_courses_and_glossaries_survive_reload(room_factory, tmp_path):
    class GlossaryBridge(FakeBridge):
        def generate_glossary(self, name, syllabus):
            return [{"term": "derivative", "meaning": "导数", "aliases": []}]
    folder = tmp_path / "courses"
    room = room_factory(folder=folder, engine=FullASR(), bridge=GlossaryBridge())
    room.set_mode("battery")
    course = room.add_course("New course")
    room.rename_course(course["id"], "Calculus")
    room.select_course(course["id"])
    room.settings({"terms": "current topic"})
    room.generate_glossary(course["id"], "derivatives")
    launch(room, "_ai_loop")
    wait_for(lambda: room.course(course["id"])["status"] == "ready")
    assert room.asr_prompt(room.get()) == "current topic, derivative"
    room.stop()
    restored = room_factory(folder=folder, engine=FullASR())
    assert restored.config["mode"] == "battery"
    assert restored.course(course["id"])["glossary"][0]["meaning"] == "导数"
    assert restored.get()["course_id"] == course["id"]
    restored.select_course(None)
    assert restored.get()["course_id"] is None


def test_new_feature_api_contracts(local_client):
    client, room, _, _ = local_client
    c = client.post("/api/courses", json={"name": "Test"}).json()
    assert client.post("/api/course/select", json={"course_id": c["id"]}).status_code == 200
    assert client.post("/api/course/select", json={"course_id": None}).status_code == 200
    assert client.post("/api/mode", json={"mode": "invalid"}).status_code == 400
    assert client.post("/api/ai/resume", json={"strategy": "invalid"}).status_code == 400
    job = client.post("/api/soundcheck", content=synthetic_wav(1)).json()["job_id"]
    wait_for(lambda: room.soundcheck["status"] == "done")
    assert client.get(f"/api/soundcheck/{job}/audio").status_code == 200
    assert client.get("/api/soundcheck/unknown/audio").status_code == 404
    room.append_text(room.current, "Original.")
    result = client.post("/api/segments/S0001/edit", json={"text": "Fixed."})
    assert result.status_code == 200 and result.json()["machine_text"] == "Original."
