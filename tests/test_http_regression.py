"""Real loopback HTTP round trip; model/ASR dependencies remain synthetic."""
from contextlib import contextmanager
import socket
import threading

import httpx
import uvicorn

from app import create_app
from test_app import FakeASR, FakeBridge, wait_for


class SyntheticBridge(FakeBridge):
    def generate_glossary(self, name, syllabus):
        return [{"term": "cost", "meaning": "成本", "aliases": []}]


@contextmanager
def serve(data):
    application = create_app({}, data, engine=FakeASR(), bridge=SyntheticBridge())
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(application, log_level="critical", timeout_graceful_shutdown=2))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        wait_for(lambda: server.started)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            yield client, application.state.room
    finally:
        server.should_exit = True
        thread.join(timeout=8)
        sock.close()
        assert not thread.is_alive(), "isolated HTTP server did not shut down"


def test_http_limits_edit_export_and_restart(tmp_path):
    data = tmp_path / "http-classroom"
    with serve(data) as (client, room):
        state = client.get("/api/state").json()
        assert state["input_limits"] == {"text": 12000, "question": 4000, "syllabus": 12000}
        sid = state["session"]["id"]
        assert client.put("/api/settings", json={"auto_analyze": False}).status_code == 200
        original = "😀" * 12000
        assert client.post("/api/text", json={"text": original}).status_code == 200
        for route, field, value in [
            ("/api/text", "text", original + "中"),
            ("/api/segments/S0001/edit", "text", original + "中"),
            ("/api/ask", "question", "😀" * 4001),
        ]:
            response = client.post(route, json={field: value})
            assert response.status_code == 422
            assert str(len(value)) in response.json()["detail"]
            assert "Unicode" in response.json()["detail"]
        assert client.post("/api/ask", json={"question": " \t\u3000"}).status_code == 422
        corrected = "Corrected 中文😀."
        assert client.post("/api/segments/S0001/edit", json={"text": corrected}).status_code == 200
        course = client.post("/api/courses", json={"name": "Synthetic"}).json()
        route = f"/api/courses/{course['id']}/glossary"
        assert client.post(route, json={"syllabus": "中" * 12001}).status_code == 422
        assert client.post(route, json={"syllabus": "A中😀" * 4000}).status_code == 200
        wait_for(lambda: room.course(course["id"])["status"] == "ready")
        assert client.post("/api/ask", json={"question": "😀" * 4000}).status_code == 200
        wait_for(lambda: room.get()["questions"][0]["status"] == "done")
        exported = client.get("/api/export", params={"format": "json"}).json()
        assert exported["segments"][0]["text"] == corrected
        assert exported["segments"][0]["machine_text"] == original
        assert len(exported["questions"]) == 1
        assert exported["questions"][0]["question"] == "😀" * 4000
        markdown = client.get("/api/export").text
        assert corrected in markdown and original in markdown
        # Refresh uses the same persisted classroom and has no side effects.
        assert client.get("/api/state").json()["session"]["id"] == sid
    with serve(data) as (client, _):
        restored = client.get("/api/state").json()["session"]
        assert restored["id"] == sid
        assert restored["segments"] == exported["segments"]
        assert restored["questions"] == exported["questions"]
