"""Direct HTTP validation uses synthetic text and no workers, audio or live AI."""
import copy

import pytest
from fastapi.testclient import TestClient

from test_app import FakeASR, FakeBridge
from app import create_app


@pytest.fixture
def input_client(tmp_path):
    application = create_app({}, tmp_path / "inputs", engine=FakeASR(), bridge=FakeBridge())
    room = application.state.room
    room.settings({"auto_analyze": False})
    room.append_text(room.current, "Original machine transcript.")
    course = room.add_course("Synthetic input course")
    # No lifespan: requests validate and persist but cannot run any model worker.
    client = TestClient(application)
    yield client, room, course["id"]
    client.close()
    room.stop()


CASES = [
    ("/api/text", "text", 12000, "原文"),
    ("/api/segments/S0001/edit", "text", 12000, "修正后的原文"),
    ("/api/ask", "question", 4000, "问题"),
    ("/api/courses/{cid}/glossary", "syllabus", 12000, "大纲"),
]


def test_state_advertises_the_server_input_limits(input_client):
    client, _, _ = input_client
    assert client.get("/api/state").json()["input_limits"] == {
        "text": 12000, "question": 4000, "syllabus": 12000,
    }


@pytest.mark.parametrize("route,field,maximum,label", CASES)
@pytest.mark.parametrize("offset", [-1, 0, 1])
@pytest.mark.parametrize("pattern", ["a", "中文English", "😀", "中A😀e\u0301👩\u200d🔬"])
def test_http_unicode_boundaries(input_client, route, field, maximum, label, offset, pattern):
    client, room, cid = input_client
    count = maximum + offset
    value = (pattern * (count // len(pattern) + 1))[:count]
    before = copy.deepcopy((room.sessions, room.courses, room.jobs))
    response = client.post(route.format(cid=cid), json={field: value})
    if offset == 1:
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert label in detail
        assert str(maximum) in detail and str(count) in detail
        assert "Unicode" in detail
        assert value not in response.text
        assert (room.sessions, room.courses, room.jobs) == before
    else:
        assert response.status_code == 200, response.text
        if field == "question":
            assert room.get()["questions"][-1]["question"] == value
        elif field == "syllabus":
            assert room.course(cid)[field] == value
        elif "edit" in route:
            assert room.get()["segments"][0]["text"] == value
            assert room.get()["segments"][0]["machine_text"] == "Original machine transcript."
        else:
            assert room.get()["segments"][-1]["text"] == value


@pytest.mark.parametrize("route,field,maximum,label", CASES)
@pytest.mark.parametrize("value", ["", " \t\r\n\u3000", "\u0085\u00a0\u2028"])
def test_http_blank_required_fields_and_optional_syllabus(input_client, route, field, maximum, label, value):
    client, room, cid = input_client
    before = copy.deepcopy((room.sessions, room.courses, room.jobs))
    response = client.post(route.format(cid=cid), json={field: value})
    if field == "syllabus":
        assert response.status_code == 200
    else:
        assert response.status_code == 422
        assert label in response.json()["detail"]
        assert "空" in response.json()["detail"]
        assert (room.sessions, room.courses, room.jobs) == before


@pytest.mark.parametrize("route,field,maximum,label", CASES)
def test_length_counts_surrounding_whitespace_before_trimming(input_client, route, field, maximum, label):
    client, room, cid = input_client
    value = " " + "中" * (maximum - 1) + " "
    before = copy.deepcopy((room.sessions, room.courses, room.jobs))
    response = client.post(route.format(cid=cid), json={field: value})
    assert response.status_code == 422
    assert str(maximum + 1) in response.json()["detail"]
    assert (room.sessions, room.courses, room.jobs) == before


@pytest.mark.parametrize("route,field,maximum,label", CASES)
def test_wrong_types_are_rejected_without_echoing_input(input_client, route, field, maximum, label):
    client, _, cid = input_client
    response = client.post(route.format(cid=cid), json={field: {"private": "do-not-echo"}})
    assert response.status_code == 422
    assert "do-not-echo" not in response.text


def test_settings_validation_still_never_reflects_keys(input_client):
    client, _, _ = input_client
    secret = "synthetic-private-key" * 500
    response = client.put("/api/ai/settings", json={"api_key": secret})
    assert response.status_code == 422
    assert "synthetic-private-key" not in response.text
