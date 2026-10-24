"""v1.1.1 adjacent-flow regressions; all records and services are synthetic."""
import copy
import json

import pytest
from fastapi.testclient import TestClient

from app import create_app
from test_app import FakeASR, FakeBridge, launch, room_factory, wait_for


class GlossaryBridge(FakeBridge):
    def generate_glossary(self, name, syllabus):
        self.calls.append({"kind": "glossary", "name": name, "syllabus": syllabus})
        return [{"term": "derivative", "meaning": "导数", "aliases": []}]


def test_paused_glossary_survives_restart_and_explicit_resume(room_factory, tmp_path):
    """An unsent paused glossary must remain eligible for the recovery button."""
    folder = tmp_path / "paused-glossary"
    original = room_factory(folder=folder, bridge=GlossaryBridge())
    source_session = original.current
    course = original.add_course("Synthetic calculus")
    original._pause_ai("network", "Synthetic unavailable service")
    original.generate_glossary(course["id"], "Synthetic derivative lesson")
    assert original.course(course["id"])["status"] == "paused"
    other_session = original.new_session("A different synthetic lesson")["id"]
    assert other_session != source_session
    original.stop()

    bridge = GlossaryBridge()
    restored = room_factory(folder=folder, bridge=bridge)
    assert restored.ai["paused"] and bridge.calls == []
    assert restored.course(course["id"])["ai_session_id"] == source_session
    restored.resume_ai("backfill")
    assert restored.course(course["id"])["status"] == "pending"
    job_id = restored.course(course["id"])["job_id"]
    assert restored.jobs[job_id]["session_id"] == source_session
    launch(restored, "_ai_loop")
    wait_for(lambda: restored.course(course["id"])["status"] == "ready")
    assert len(bridge.calls) == 1
    assert bridge.calls[0]["syllabus"] == "Synthetic derivative lesson"


@pytest.mark.parametrize("interrupted_status", ["pending", "running"])
def test_unpaused_interrupted_glossary_still_requires_regeneration(room_factory, tmp_path, interrupted_status):
    folder = tmp_path / ("glossary-" + interrupted_status)
    original = room_factory(folder=folder, bridge=GlossaryBridge())
    course = original.add_course("Synthetic glossary interrupted before shutdown")
    original.generate_glossary(course["id"], "Synthetic topic")
    original.course(course["id"])["status"] = interrupted_status
    original.save_courses()
    original.stop()

    bridge = GlossaryBridge()
    restored = room_factory(folder=folder, bridge=bridge)
    assert restored.course(course["id"])["status"] == "error"
    assert "中断" in restored.course(course["id"])["error"]
    restored.resume_ai("backfill")
    assert restored.course(course["id"])["status"] == "error"
    assert restored.ai_queue.qsize() == 0 and bridge.calls == []


def test_corrected_source_does_not_reintroduce_stale_answer_as_unmarked_history(room_factory):
    """Historical answers invalidated by an edit must not silently guide new ones."""
    bridge = FakeBridge(["OLD_FACT: The interest rate is ninety percent."])
    room = room_factory(bridge=bridge)
    room.settings({"auto_analyze": False})
    room.append_text(room.current, "The interest rate is ninety percent.", 0, 5, "microphone")
    room.ask("What rate did the lecturer state?")
    launch(room, "_ai_loop")
    wait_for(lambda: room.get()["questions"][0]["status"] == "done")
    room.edit_segment(room.current, "S0001", "The interest rate is nineteen percent.")
    old_question = room.get()["questions"][0]
    assert old_question["stale"]
    room.ask("Explain the stated rate again.")
    followup = room.get()["questions"][-1]
    assert "nineteen percent" in followup["context_snapshot"]
    history = json.dumps(followup["history_snapshot"], ensure_ascii=False)
    # Retaining an explicitly marked stale history would also be reasonable;
    # current runtime only forwards role/content, so this contract excludes it.
    assert "OLD_FACT" not in history


def test_repeated_edits_restart_and_both_exports_keep_machine_original(tmp_path):
    folder = tmp_path / "edits-and-exports"
    application = create_app({}, folder, engine=FakeASR(), bridge=FakeBridge())
    room = application.state.room
    room.settings({"auto_analyze": False})
    sid = room.current
    room.append_text(sid, "Original machine transcript. 机器原稿。", 2, 8, "microphone")
    room.flush(sid)
    room.edit_segment(sid, "S0001", "First correction. 第一次修正。")
    room.edit_segment(sid, "S0001", "Final correction. 最后修正。")
    expected = copy.deepcopy(room.get(sid)["segments"][0])
    room.stop()

    restored_app = create_app({}, folder, engine=FakeASR(), bridge=FakeBridge())
    restored = restored_app.state.room
    try:
        client = TestClient(restored_app)
        state = client.get("/api/state").json()["session"]
        assert state["segments"][0] == expected
        assert state["groups"][0]["text"] == expected["text"]
        exported = client.get("/api/export", params={"format": "json", "session_id": sid})
        assert exported.status_code == 200
        record = exported.json()["segments"][0]
        assert record["machine_text"] == "Original machine transcript. 机器原稿。"
        assert [edit["text"] for edit in record["edits"]] == [record["machine_text"], "First correction. 第一次修正。"]
        assert record["text"] == expected["text"]
        markdown = client.get("/api/export", params={"format": "markdown", "session_id": sid}).text
        assert expected["text"] in markdown and "修改前的机器识别：" + record["machine_text"] in markdown
        assert "00:02–00:08" in markdown and "待核对" in markdown
    finally:
        restored.stop()


def test_restart_preserves_interrupted_draft_but_excludes_it_from_context(room_factory, tmp_path):
    folder = tmp_path / "interrupted-draft"
    original = room_factory(folder=folder)
    original.settings({"auto_analyze": False})
    original.append_text(original.current, "Synthetic original statement.", 0, 5)
    original.flush()
    group = original.get()["groups"][0]
    group.update(status="running", stream_status="streaming", streaming_text="DRAFT_ONLY_CONTENT",
                 stream_provider="api", stream_model="synthetic-model")
    original.get()["ai_usage"]["requests"] = 1
    original.save(original.get())
    sid = original.current
    original.stop()

    restored = room_factory(folder=folder)
    snapshot = restored.state()["session"]
    assert snapshot["id"] == sid
    assert snapshot["groups"][0]["status"] == "error"
    assert snapshot["groups"][0]["stream_status"] == "interrupted"
    assert snapshot["groups"][0]["streaming_text"] == "DRAFT_ONLY_CONTENT"
    assert snapshot["groups"][0]["analysis"] == ""
    assert snapshot["ai_usage"]["unknown"] == 1
    assert "DRAFT_ONLY_CONTENT" not in restored.context(restored.get())
    markdown = restored.markdown(restored.get())
    assert "未完成草稿（尚未校验，不作为正式答案" in markdown
    assert "DRAFT_ONLY_CONTENT" in markdown
    assert restored.ai_queue.qsize() == 0 and restored.bridge.calls == []
