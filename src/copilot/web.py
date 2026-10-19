"""Web app: morning dashboard, chat UI, and a Server-Sent Events stream of everything the agent does.

uvicorn copilot.web:app --reload        # then open http://localhost:8000

Modes (COPILOT_MODE):
- demo: recorded answers (copilot.demo), real tools and data. No API key, no cost.
- live: the Claude API. Needs ANTHROPIC_API_KEY; usage is capped per session and per day.
If COPILOT_MODE is not set, the app runs live when an API key is present, otherwise demo.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import checks, mcp_server
from .agent import Agent, AuditLog, Conversation
from .backends import StdioBackend
from .data import seed
from .demo import ScriptedClaude

STATIC = Path(__file__).parent / "static"
DATA_DIR = Path(os.environ.get("COPILOT_DATA_DIR", "."))
MAX_SESSIONS = 500
MESSAGES_PER_SESSION = int(os.environ.get("COPILOT_MESSAGES_PER_SESSION", "12"))
MESSAGES_PER_DAY = int(os.environ.get("COPILOT_MESSAGES_PER_DAY", "300"))


def default_mode() -> str:
    return os.environ.get("COPILOT_MODE") or ("live" if os.environ.get("ANTHROPIC_API_KEY") else "demo")


class ChatIn(BaseModel):
    session_id: str | None = None
    message: str = Field(min_length=1, max_length=2000)


class DecisionIn(BaseModel):
    session_id: str
    tool_use_id: str
    approved: bool


def sse(events: AsyncIterator[dict], first: dict | None = None) -> StreamingResponse:
    async def stream():
        if first:
            yield f"data: {json.dumps(first)}\n\n"
        async for event in events:
            yield f"data: {json.dumps(event, default=str)}\n\n"
        yield 'data: {"type": "done"}\n\n'

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


async def one(event: dict) -> AsyncIterator[dict]:
    yield event


def create_app(agent: Agent | None = None, mode: str | None = None) -> FastAPI:
    mode = mode or default_mode()
    state: dict = {"agent": agent, "sessions": {}, "locks": {}, "counts": {}, "day": None, "today_count": 0}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        backend = None
        if state["agent"] is None:
            db = DATA_DIR / "backoffice_demo.db"
            seed(db)  # every start begins from the same demo data
            mcp_server.DB_PATH = db  # the dashboard reads the same database as the MCP server process
            backend = await StdioBackend(str(db)).start()
            client = ScriptedClaude() if mode == "demo" else None
            state["agent"] = Agent(backend, AuditLog(DATA_DIR / "audit.jsonl"), client=client)
        await state["agent"].load_tools()
        yield
        if backend:
            await backend.close()

    app = FastAPI(title="Back-office copilot", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def session(session_id: str | None) -> tuple[Conversation, asyncio.Lock]:
        sid = session_id or uuid.uuid4().hex
        sessions = state["sessions"]
        if sid not in sessions and len(sessions) >= MAX_SESSIONS:
            oldest = next(iter(sessions))  # demo sessions live in memory; drop the oldest
            for d in (sessions, state["locks"], state["counts"]):
                d.pop(oldest, None)
        conv = sessions.setdefault(sid, Conversation(id=sid))
        lock = state["locks"].setdefault(sid, asyncio.Lock())
        return conv, lock

    def over_limit(sid: str) -> str | None:
        """Live mode spends real money, so cap it. Set a spending limit on the API key as well."""
        if mode != "live":
            return None
        today = date.today()
        if state["day"] != today:
            state["day"], state["today_count"] = today, 0
        if state["counts"].get(sid, 0) >= MESSAGES_PER_SESSION:
            limit = MESSAGES_PER_SESSION
            return f"This conversation has reached its {limit}-message limit. Reload to start again."
        if state["today_count"] >= MESSAGES_PER_DAY:
            return "The live demo has reached today's usage limit. Please try again tomorrow."
        state["counts"][sid] = state["counts"].get(sid, 0) + 1
        state["today_count"] += 1
        return None

    async def locked(lock: asyncio.Lock, events: AsyncIterator[dict]) -> AsyncIterator[dict]:
        if lock.locked():
            yield {"type": "error", "message": "Still working on the previous message."}
            return
        async with lock:
            async for e in events:
                yield e

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/overview")
    def overview() -> dict:
        a: Agent = state["agent"]
        return {**checks.overview(a.today - timedelta(days=1)), "mode": mode}

    @app.get("/api/tools")
    def tools() -> list[dict]:
        a: Agent = state["agent"]
        return [{"name": t["name"], "needs_approval": t["name"] in a.needs_approval} for t in a.tools]

    @app.post("/api/chat")
    def chat(body: ChatIn) -> StreamingResponse:
        conv, lock = session(body.session_id)
        first = {"type": "session", "id": conv.id}
        if problem := over_limit(conv.id):
            return sse(one({"type": "error", "message": problem}), first=first)
        return sse(locked(lock, state["agent"].ask(conv, body.message)), first=first)

    @app.post("/api/decision")
    def decision(body: DecisionIn) -> StreamingResponse:
        if body.session_id not in state["sessions"]:
            raise HTTPException(404, "Unknown session")
        conv, lock = session(body.session_id)
        # In a real deployment "by" is the signed-in user from SSO; the demo has no login.
        events = state["agent"].decide(conv, body.tool_use_id, body.approved, by="demo user")
        return sse(locked(lock, events))

    @app.get("/api/audit")
    def audit() -> list[dict]:
        return state["agent"].audit.recent()

    @app.post("/api/reset")
    def reset() -> dict:
        """Put the fictional demo data back to its starting state, problems and all."""
        if any(lock.locked() for lock in state["locks"].values()):
            raise HTTPException(409, "The copilot is busy. Try again in a moment.")
        seed(mcp_server.DB_PATH)
        state["agent"].audit.path.unlink(missing_ok=True)
        for d in (state["sessions"], state["locks"], state["counts"]):
            d.clear()
        return {"reset": True}

    return app


app = create_app()
