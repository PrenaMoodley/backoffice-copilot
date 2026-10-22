import asyncio
import json

from conftest import FakeClaude, reply, text, tool_use
from fastapi.testclient import TestClient

from copilot import web
from copilot.agent import Agent, AuditLog
from copilot.backends import InProcessBackend
from copilot.checks import QUESTIONS
from copilot.demo import ScriptedClaude
from copilot.web import create_app


def events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def test_chat_and_approval_over_sse(tmp_path):
    claude = FakeClaude(
        reply([tool_use("t1", "flag_invoice", {"invoice_no": "CLS-20958", "reason": "Possible duplicate"})]),
        reply([text("CLS-20958 is on hold.")]),
    )
    agent = Agent(InProcessBackend(), AuditLog(tmp_path / "audit.jsonl"), client=claude)
    with TestClient(create_app(agent)) as client:
        tools = client.get("/api/tools").json()
        assert {"name": "flag_invoice", "needs_approval": True} in tools

        r = client.post("/api/chat", json={"message": "Hold the duplicate linen invoice"})
        assert r.headers["content-type"].startswith("text/event-stream")
        ev = events(r)
        session = ev[0]["id"]
        assert [e["type"] for e in ev if e["type"] != "usage"] == [
            "session",
            "approval_required",
            "paused",
            "done",
        ]

        r = client.post("/api/decision", json={"session_id": session, "tool_use_id": "t1", "approved": True})
        assert [e["type"] for e in events(r) if e["type"] != "usage"] == [
            "tool_call",
            "tool_result",
            "answer",
            "done",
        ]

        audit = client.get("/api/audit").json()
        assert audit[0]["event"] == "tool_call" and audit[0]["ok"] is True
        assert client.get("/").status_code == 200


def test_decision_for_unknown_session(tmp_path):
    agent = Agent(InProcessBackend(), AuditLog(tmp_path / "a.jsonl"), client=FakeClaude())
    with TestClient(create_app(agent)) as client:
        r = client.post("/api/decision", json={"session_id": "nope", "tool_use_id": "x", "approved": True})
        assert r.status_code == 404


def test_overview_and_reset(tmp_path, demo_db):
    agent = Agent(InProcessBackend(), AuditLog(tmp_path / "a.jsonl"), client=ScriptedClaude())
    with TestClient(create_app(agent, mode="demo")) as client:
        o = client.get("/api/overview").json()
        assert (o["mode"], o["business_date"]) == ("demo", "2026-10-11")
        assert {c["id"]: c["status"] for c in o["checks"]}["room_charges"] == "fail"

        r = client.post("/api/chat", json={"message": QUESTIONS["room_charges"]})
        session = events(r)[0]["id"]
        client.post("/api/decision", json={"session_id": session, "tool_use_id": "r2", "approved": True})
        o = client.get("/api/overview").json()
        assert {c["id"]: c["status"] for c in o["checks"]}["room_charges"] == "ok"
        assert client.get("/api/audit").json()

        assert client.post("/api/reset").json() == {"reset": True}
        o = client.get("/api/overview").json()
        assert {c["id"]: c["status"] for c in o["checks"]}["room_charges"] == "fail"
        assert client.get("/api/audit").json() == []


def test_health_reports_ok_and_what_failed(tmp_path, monkeypatch):
    agent = Agent(InProcessBackend(), AuditLog(tmp_path / "a.jsonl"), client=ScriptedClaude())
    with TestClient(create_app(agent, mode="demo")) as client:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "mode": "demo", "problems": []}

        monkeypatch.setattr(web.mcp_server, "DB_PATH", tmp_path / "missing.db")
        r = client.get("/health")
        assert r.status_code == 503
        assert r.json()["problems"] == ["database unreadable"]
        assert not (tmp_path / "missing.db").exists()  # a health check must not create the database


def test_streams_ask_proxies_not_to_buffer(tmp_path):
    agent = Agent(InProcessBackend(), AuditLog(tmp_path / "a.jsonl"), client=FakeClaude(reply([text("Hi")])))
    with TestClient(create_app(agent, mode="demo")) as client:
        r = client.post("/api/chat", json={"message": "Hello"})
        assert r.headers["x-accel-buffering"] == "no"


def test_live_mode_caps_messages_per_session(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "MESSAGES_PER_SESSION", 1)
    claude = FakeClaude(reply([text("First answer.")]))
    agent = Agent(InProcessBackend(), AuditLog(tmp_path / "a.jsonl"), client=claude)
    with TestClient(create_app(agent, mode="live")) as client:
        first = [
            e for e in events(client.post("/api/chat", json={"message": "Hello"})) if e["type"] != "usage"
        ]
        assert first[1] == {"type": "answer", "text": "First answer."}
        second = events(client.post("/api/chat", json={"session_id": first[0]["id"], "message": "Again"}))
        assert second[1]["type"] == "error" and "limit" in second[1]["message"]
        assert len(claude.requests) == 1  # the capped message never reached the API


def test_mode_follows_api_key(monkeypatch):
    monkeypatch.delenv("COPILOT_MODE", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert web.default_mode() == "demo"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    assert web.default_mode() == "live"
    monkeypatch.setenv("COPILOT_MODE", "demo")
    assert web.default_mode() == "demo"


def test_message_validation(tmp_path):
    agent = Agent(InProcessBackend(), AuditLog(tmp_path / "a.jsonl"), client=FakeClaude())
    asyncio.run(agent.load_tools())
    with TestClient(create_app(agent)) as client:
        assert client.post("/api/chat", json={"message": ""}).status_code == 422
