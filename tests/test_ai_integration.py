"""AI settings/queue/stream regression using synthetic credentials and services."""
import json
import os
import threading

import pytest
from fastapi import HTTPException

from app import Classroom
from ai_api import ApiBridgeError
from test_app import FakeASR, FakeBridge, launch, wait_for, room_factory, local_client
from test_features import FullASR


class StreamingAPI:
    entered = None
    release = None
    fail = False
    calls = []

    def __init__(self, config, api_key, on_delta=None):
        self.config = config
        self.key = api_key
        self.on_delta = on_delta

    def metadata(self):
        return {"provider": "api", "model": self.config["model"],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}}

    def ask(self, context, question, history):
        self.calls.append((self.config["base_url"], self.config["model"], question))
        if self.on_delta:
            self.on_delta("这是未完成的")
        if self.entered:
            self.entered.set()
        if self.release:
            assert self.release.wait(4)
        if self.fail:
            raise ApiBridgeError("测试连接中断。", kind="network")
        if self.on_delta:
            self.on_delta("回答。")
        return "这是完整回答。"

    def analyze(self, context, new_text):
        return self.ask(context, new_text, [])

    def generate_glossary(self, name, syllabus):
        return [{"term": "demand", "meaning": "需求", "aliases": []}]

    def test_connection(self):
        return {"ok": True, **self.metadata()}


def configure(room, **overrides):
    room.connection.api_factory = StreamingAPI
    return room.configure_ai({"provider": "api", "base_url": "https://example.invalid/v1",
                              "model": "test-model", "api_key": "synthetic-only-secret",
                              "remember_key": False, **overrides})


@pytest.fixture(autouse=True)
def reset_stream():
    StreamingAPI.entered = StreamingAPI.release = None
    StreamingAPI.fail = False
    StreamingAPI.calls = []


def test_settings_and_export_never_reveal_key(local_client):
    client, room, _, _ = local_client
    result = client.put("/api/ai/settings", json={"provider": "api", "base_url": "https://example.invalid/v1",
                                                "model": "test-model", "api_key": "synthetic-only-secret"})
    assert result.status_code == 200
    assert result.json()["key_configured"] is True
    assert room.get()["settings"]["auto_analyze"] is False
    for response in (result, client.get("/api/state"), client.get("/api/export?format=json"), client.get("/api/export")):
        assert "synthetic-only-secret" not in response.text
    for path in room.data.rglob("*.json"):
        assert "synthetic-only-secret" not in path.read_text(encoding="utf-8")
    assert client.get("/api/health").json()["version"] == "1.1.0"


def test_secret_not_echoed_on_validation_failure(local_client):
    client, _, _, _ = local_client
    secret = "synthetic-secret-" * 500
    result = client.put("/api/ai/settings", json={"api_key": secret})
    assert result.status_code == 422
    assert "synthetic-secret" not in result.text
    assert client.put("/api/ai/settings", headers={"Origin": "https://external.invalid"}, json={"provider": "api"}).status_code == 403


def test_changed_destination_cannot_reuse_previous_key(room_factory):
    room = room_factory()
    configure(room)
    original = room.ai_settings()
    assert original["key_configured"]
    result = room.configure_ai({"base_url": "https://different.invalid/v1"})
    assert result["key_configured"] is False
    with pytest.raises(HTTPException):
        room.test_ai({"provider": "api"})
    room.configure_ai({"api_key": "new-synthetic-key"})
    room.configure_ai({"clear_key": True})
    assert not room.ai_settings()["key_configured"]


def test_ephemeral_key_does_not_survive_restart_or_fallback(room_factory):
    room = room_factory()
    configure(room)
    restored = room_factory(folder=room.data)
    assert restored.ai_settings()["provider"] == "api"
    assert not restored.ai_settings()["key_configured"]
    restored.ask("Test question")
    launch(restored, "_ai_loop")
    wait_for(lambda: restored.ai["paused"])
    assert restored.bridge.calls == []
    assert restored.get()["ai_usage"]["requests"] == 0


@pytest.mark.skipif(os.name != "nt", reason="DPAPI requires Windows")
def test_remembered_key_is_encrypted_bound_and_clearable(room_factory):
    room = room_factory()
    configure(room, remember_key=True)
    encrypted = (room.data / "api-key.dpapi").read_bytes()
    assert b"synthetic-only-secret" not in encrypted
    restored = room_factory(folder=room.data)
    assert restored.ai_settings()["key_persisted"]
    assert restored.connection.key == "synthetic-only-secret"
    restored.configure_ai({"remember_key": False})
    assert restored.ai_settings()["key_configured"]
    assert not (room.data / "api-key.dpapi").exists()
    again = room_factory(folder=room.data)
    assert not again.ai_settings()["key_configured"]


@pytest.mark.skipif(os.name != "nt", reason="DPAPI requires Windows")
def test_settings_write_failure_rolls_back_encrypted_key(room_factory, monkeypatch):
    import ai_settings
    room = room_factory()
    configure(room, remember_key=True)
    old = room.ai_settings()
    old_secret = room.connection.store.load()
    def fail_write(*args):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr(ai_settings, "write_json", fail_write)
    with pytest.raises(HTTPException):
        room.configure_ai({"base_url": "https://another.invalid/v1", "api_key": "different-synthetic-key"})
    assert room.ai_settings() == old
    assert room.connection.store.load() == old_secret
    restored = room_factory(folder=room.data)
    assert restored.connection.key == "synthetic-only-secret"
    assert restored.ai_settings()["base_url"] == old["base_url"]


def test_streamed_draft_is_visible_before_complete_and_only_then_final(room_factory):
    room = room_factory()
    configure(room)
    StreamingAPI.entered, StreamingAPI.release = threading.Event(), threading.Event()
    room.ask("Synthetic question")
    launch(room, "_ai_loop")
    assert StreamingAPI.entered.wait(2)
    q = room.state()["session"]["questions"][0]
    assert q["streaming_text"] == "这是未完成的"
    assert q["answer"] == ""
    assert q["stream_status"] == "streaming"
    StreamingAPI.release.set()
    wait_for(lambda: room.get()["questions"][0]["status"] == "done")
    q = room.get()["questions"][0]
    assert q["answer"] == "这是完整回答。" and q["streaming_text"] == ""
    assert q["ai_provider"] == "api" and q["ai_model"] == "test-model"
    assert room.get()["ai_usage"] == {"requests": 1, "completed": 1, "failed": 0, "unknown": 0,
                                     "prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20, "usage_reported_requests": 1}


def test_failed_retry_preserves_formal_answer_and_its_provider(room_factory):
    room = room_factory()
    room.ask("A question")
    launch(room, "_ai_loop")
    wait_for(lambda: room.get()["questions"][0]["status"] == "done")
    old = dict(room.get()["questions"][0])
    configure(room)
    StreamingAPI.fail = True
    room.retry("question", old["id"])
    wait_for(lambda: room.ai["paused"])
    q = room.get()["questions"][0]
    assert q["answer"] == old["answer"]
    assert q["ai_provider"] == "codex" and q["stream_provider"] == "api"
    assert q["stream_status"] == "interrupted" and q["streaming_text"]
    assert room.get()["ai_usage"]["failed"] == 1


def test_switch_finishes_current_request_then_requires_explicit_resume(room_factory):
    room = room_factory()
    configure(room)
    StreamingAPI.entered, StreamingAPI.release = threading.Event(), threading.Event()
    room.ask("first")
    room.ask("second")
    launch(room, "_ai_loop")
    assert StreamingAPI.entered.wait(2)
    result = room.configure_ai({"provider": "codex"})
    assert result["pending"] and result["active_provider"] == "api"
    assert room.get()["questions"][1]["status"] == "paused"
    with pytest.raises(HTTPException) as exc:
        room.resume_ai("backfill")
    assert exc.value.status_code == 409
    StreamingAPI.release.set()
    wait_for(lambda: not room.ai_settings()["pending"])
    assert room.get()["questions"][0]["ai_provider"] == "api"
    assert not room.bridge.calls
    assert room.ai["paused"]
    room.resume_ai("backfill")
    wait_for(lambda: room.get()["questions"][1]["status"] == "done")
    assert room.get()["questions"][1]["ai_provider"] == "codex"
    assert len(StreamingAPI.calls) == 1 and len(room.bridge.calls) == 1


def test_test_connection_is_draft_only_without_classroom_content(room_factory):
    room = room_factory()
    room.connection.api_factory = StreamingAPI
    result = room.test_ai({"provider": "api", "base_url": "https://test.invalid/v1", "api_key": "test-key", "model": "test"})
    assert result["ok"]
    assert room.ai_settings()["provider"] == "codex"
    assert not room.ai_settings()["key_configured"]
    assert not (room.data / "ai-settings.json").exists()
    assert room.get()["ai_usage"]["requests"] == 0


def test_mode_preserves_other_preferences(room_factory):
    room = room_factory(engine=FullASR())
    (room.data / "preferences.json").write_text(json.dumps({"unrelated": "preserve"}), encoding="utf-8")
    room.set_mode("battery")
    assert json.loads((room.data / "preferences.json").read_text(encoding="utf-8")) == {"unrelated": "preserve", "mode": "battery"}


def test_api_autorun_requires_explicit_opt_in_and_resets_on_model_change(room_factory):
    room = room_factory()
    configure(room)
    assert room.new_session()["settings"]["auto_analyze"] is False
    room.settings({"auto_analyze": True})
    assert room.get()["settings"]["auto_analyze"] is True
    room.configure_ai({"model": "another-model"})
    assert all(s["settings"]["auto_analyze"] is False for s in room.sessions.values())


def test_unexpected_exception_never_leaks_credentials(room_factory):
    room = room_factory()
    class BrokenAPI(StreamingAPI):
        def ask(self, *args):
            raise RuntimeError("Authorization: Bearer synthetic-only-secret")
    configure(room)
    room.connection.api_factory = BrokenAPI
    room.ask("Test")
    launch(room, "_ai_loop")
    wait_for(lambda: room.get()["questions"][0]["status"] == "error")
    assert "synthetic-only-secret" not in json.dumps(room.state())


def test_attempt_is_durable_before_first_delta_and_restart_marks_unknown(room_factory):
    room = room_factory()
    configure(room)
    entered, release = threading.Event(), threading.Event()
    class BeforeDeltaAPI(StreamingAPI):
        def ask(self, *args):
            entered.set()
            assert release.wait(4)
            return "Done"
    room.connection.api_factory = BeforeDeltaAPI
    room.ask("Test")
    launch(room, "_ai_loop")
    assert entered.wait(2)
    try:
        saved = json.loads((room.data / room.current / "session.json").read_text(encoding="utf-8"))
        assert saved["ai_usage"]["requests"] == 1
        restored = room_factory(folder=room.data)
        assert restored.get()["ai_usage"]["unknown"] == 1
        assert restored.get()["questions"][0]["status"] == "error"
        assert not restored.bridge.calls
    finally:
        release.set()


def test_paused_glossary_resumes_in_original_classroom(room_factory):
    room = room_factory()
    configure(room)
    original = room.current
    course = room.add_course("Synthetic test")
    room._pause_ai("network", "Synthetic pause")
    room.generate_glossary(course["id"], "")
    different = room.new_session("Different classroom")["id"]
    room.resume_ai("backfill")
    launch(room, "_ai_loop")
    wait_for(lambda: room.course(course["id"])["status"] == "ready")
    assert room.get(original)["ai_usage"]["requests"] == 1
    assert room.get(different)["ai_usage"]["requests"] == 0


def test_switch_disk_failure_cannot_redirect_queued_work(room_factory, monkeypatch):
    room = room_factory()
    room.connection.api_factory = StreamingAPI
    room.ask("Queued under Codex")
    with monkeypatch.context() as patcher:
        def fail_save(*args):
            raise OSError("synthetic disk failure")
        patcher.setattr(room, "save", fail_save)
        with pytest.raises(HTTPException) as exc:
            configure(room)
        assert exc.value.status_code == 507
    assert room.ai["paused"] and not room.ai_ready.is_set()
    assert room.ai_settings()["provider"] == "codex"
    assert room.get()["questions"][0]["status"] == "paused"
    assert StreamingAPI.calls == []
    room.resume_ai("backfill")
    launch(room, "_ai_loop")
    wait_for(lambda: room.get()["questions"][0]["status"] == "done")
    assert len(room.bridge.calls) == 1 and not StreamingAPI.calls


def test_api_success_status_belongs_only_to_current_configuration(room_factory):
    room = room_factory()
    configure(room)
    room.ask("Test")
    launch(room, "_ai_loop")
    wait_for(lambda: room.active_ai is None and room.get()["questions"][0]["status"] == "done")
    assert room.state()["status"]["ai"]["inference_verified"]
    room.configure_ai({"model": "unverified-model"})
    assert not room.state()["status"]["ai"].get("inference_verified")
