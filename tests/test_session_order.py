"""Session recovery uses creation order, never the last selected classroom.

All records are synthetic tmp_path fixtures; no microphone or AI is started.
"""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app as app_module
from app import Classroom, create_app
from test_app import FakeASR, FakeBridge, room_factory


SECOND = "2026-10-24T10:00:00+08:00"


def freeze_creation(monkeypatch, ids):
    identifiers = iter(ids)
    monkeypatch.setattr(app_module, "stamp", lambda: SECOND)
    monkeypatch.setattr(app_module, "uuid", SimpleNamespace(
        uuid4=lambda: SimpleNamespace(hex=next(identifiers))))


def synthetic_legacy(folder, sid, created_at=SECOND, **extra):
    record = {"id": sid, "title": "Synthetic legacy classroom", "demo": False,
              "created_at": created_at, "segments": [], "groups": [],
              "questions": [], "audio": [], "revisions": [],
              "settings": {"terms": "", "auto_analyze": False},
              "pending_ids": [], "flush_requested": False, **extra}
    path = folder / sid / "session.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def create_demo_and_normal(room, demo_first):
    initial = room.current
    if demo_first:
        older = room.demo()["session"]["id"]
        newer = room.new_session("Synthetic normal classroom")["id"]
    else:
        older = room.new_session("Synthetic normal classroom")["id"]
        newer = room.demo()["session"]["id"]
    return [newer, older, initial]


@pytest.mark.parametrize("demo_first", [True, False])
def test_same_second_list_matches_creation_order(room_factory, monkeypatch, demo_first):
    freeze_creation(monkeypatch, ["f" * 32, "a" * 32, "e" * 32])
    room = room_factory()
    expected = create_demo_and_normal(room, demo_first)
    assert room.current == expected[0]
    assert [s["id"] for s in room.state()["sessions"]] == expected


@pytest.mark.parametrize("demo_first", [True, False])
def test_restart_selects_latest_same_second_classroom(room_factory, monkeypatch, demo_first):
    freeze_creation(monkeypatch, ["f" * 32, "a" * 32, "e" * 32])
    room = room_factory()
    expected = create_demo_and_normal(room, demo_first)
    restored = room_factory(folder=room.data)
    assert restored.current == expected[0]
    assert [s["id"] for s in restored.state()["sessions"]] == expected


def test_same_second_order_continues_after_each_restart(room_factory, monkeypatch):
    freeze_creation(monkeypatch, ["a" * 32, "b" * 32, "c" * 32, "d" * 32])
    room = room_factory()
    expected = [room.current]
    for index in range(3):
        room = room_factory(folder=room.data)
        expected.insert(0, room.new_session(f"Synthetic classroom {index}")["id"])
        room = room_factory(folder=room.data)
        assert room.current == expected[0]
        assert [s["id"] for s in room.state()["sessions"]] == expected


def test_selecting_old_classroom_does_not_change_restart_default(tmp_path):
    folder = tmp_path / "selection"
    older = synthetic_legacy(folder, "a" * 32)
    newer = synthetic_legacy(folder, "b" * 32, "2026-10-24T10:00:01+08:00")
    original_bytes = {path: path.read_bytes() for path in (older, newer)}
    application = create_app({}, folder, engine=FakeASR(), bridge=FakeBridge())
    # No lifespan means no background ASR/AI workers are started.
    client = TestClient(application)
    try:
        for selected in (older, newer, older, newer, older):
            assert client.post(f"/api/sessions/{selected.parent.name}/select").status_code == 200
            state = client.get("/api/state").json()
            assert state["session"]["id"] == selected.parent.name
            assert [s["id"] for s in state["sessions"]] == [newer.parent.name, older.parent.name]
            assert all(path.read_bytes() == original_bytes[path] for path in original_bytes)
        restored = Classroom(folder, {}, FakeASR(), FakeBridge())
        assert restored.current == newer.parent.name
        assert [s["id"] for s in restored.state()["sessions"]] == [newer.parent.name, older.parent.name]
        assert all(path.read_bytes() == original_bytes[path] for path in original_bytes)
    finally:
        client.close()


def test_legacy_same_second_fallback_is_stable_without_rewriting(room_factory, tmp_path):
    folder = tmp_path / "legacy-ties"
    # Write the larger ID first: directory iteration / insertion is not evidence.
    paths = [synthetic_legacy(folder, sid) for sid in ("f" * 32, "a" * 32)]
    original_bytes = {path: path.read_bytes() for path in paths}
    for _ in range(2):
        room = room_factory(folder=folder)
        assert room.current == "a" * 32
        assert [s["id"] for s in room.state()["sessions"]] == ["a" * 32, "f" * 32]
    assert all(path.read_bytes() == original_bytes[path] for path in paths)


def test_new_same_second_classroom_sorts_after_legacy_without_rewriting(room_factory, tmp_path, monkeypatch):
    folder = tmp_path / "mixed"
    legacy = synthetic_legacy(folder, "a" * 32)
    original_bytes = legacy.read_bytes()
    freeze_creation(monkeypatch, ["f" * 32])
    room = room_factory(folder=folder)
    newest = room.new_session("Synthetic new classroom")["id"]
    restored = room_factory(folder=folder)
    assert restored.current == newest
    assert [s["id"] for s in restored.state()["sessions"]] == [newest, legacy.parent.name]
    assert legacy.read_bytes() == original_bytes


def test_creation_timestamp_remains_primary_over_tie_breaker(room_factory, tmp_path):
    folder = tmp_path / "timestamp-first"
    older = synthetic_legacy(folder, "a" * 32, creation_order=100)
    newer = synthetic_legacy(folder, "b" * 32, "2026-10-24T10:00:01+08:00")
    room = room_factory(folder=folder)
    assert room.current == newer.parent.name
    assert [s["id"] for s in room.state()["sessions"]] == [newer.parent.name, older.parent.name]


def test_restart_falls_back_when_latest_session_file_is_missing(room_factory, monkeypatch):
    # No classroom deletion API exists: simulate an isolated missing disk file.
    freeze_creation(monkeypatch, ["a" * 32, "b" * 32, "c" * 32])
    room = room_factory()
    initial = room.current
    previous = room.new_session("Synthetic previous classroom")["id"]
    latest = room.new_session("Synthetic missing classroom")["id"]
    (room.data / latest / "session.json").unlink()
    restored = room_factory(folder=room.data)
    assert restored.current == previous
    assert [s["id"] for s in restored.state()["sessions"]] == [previous, initial]


def test_empty_session_directory_does_not_block_fresh_default(room_factory, tmp_path):
    folder = tmp_path / "empty"
    (folder / ("a" * 32)).mkdir(parents=True)
    room = room_factory(folder=folder)
    assert room.current != "a" * 32
    assert room.get()["demo"] is False
    assert len(room.state()["sessions"]) == 1
