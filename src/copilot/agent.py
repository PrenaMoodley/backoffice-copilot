"""The copilot's agent loop: Claude + MCP tools + human approval for anything that writes.

Design:
- Tools come from an MCP server. Their MCP annotations decide the policy: tools
  marked readOnlyHint run automatically; every other tool pauses the loop until
  a person approves or rejects it in the UI. Unknown means "ask".
- The loop is written by hand (not the SDK's tool runner) because it must be
  able to stop mid-turn, wait for a person, and resume with the same history.
- History is append-only and keeps Claude's content blocks exactly as returned.
- Every tool call, approval and rejection is written to an audit log.
- Each step is yielded as an event, so the UI can show what the agent is doing.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

import anthropic

MODEL = os.environ.get("COPILOT_MODEL", "claude-opus-5-5")
MAX_ROUNDS = 10

SYSTEM_PROMPT = """\
You are the back-office copilot for the finance team of a hotel (fictional demo data).
You answer questions about room statistics, POS takings, room charges between the POS and
the PMS, card settlements, bank reconciliation, supplier invoices and advertising spend by
calling the tools provided.

How to work:
- Get every figure from a tool. Quote amounts exactly as the tools return them, in rand
  (R1,234.56). Do not add up or recalculate figures yourself; the tools already have.
- If the tools can't answer something, say so plainly instead of guessing.
- Investigate before concluding: when you find a difference, look for the cause in the
  other tools (for example bank exceptions for a card shortfall).
- Tools that change data (journal adjustments, posting a room charge to a folio, putting an
  invoice on hold) are reviewed by a person before they run. Only propose one when you have
  found the cause, and explain in one or two sentences what it does and why. If a person
  rejects it, accept that and do not propose the same action again unless they ask.
- Keep answers short: lead with the answer, then the evidence as a few bullet points.
"""


class ToolBackend(Protocol):
    async def list_tools(self) -> list[Any]: ...
    async def call_tool(self, name: str, arguments: dict) -> Any: ...


@dataclass
class PendingCall:
    tool_use_id: str
    name: str
    input: dict


@dataclass
class Conversation:
    id: str
    messages: list[dict] = field(default_factory=list)
    waiting: dict[str, PendingCall] = field(default_factory=dict)
    results: dict[str, dict] = field(default_factory=dict)  # tool_use_id -> tool_result block
    order: list[str] = field(default_factory=list)  # tool_use ids of the current assistant turn


class AuditLog:
    def __init__(self, path: Path):
        self.path = path

    def write(self, **entry: Any) -> None:
        entry = {"at": datetime.now(UTC).isoformat(timespec="seconds"), **entry}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def recent(self, limit: int = 50) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(line) for line in reversed(lines)]


def _result_text(result: Any) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content if getattr(c, "type", "") == "text")


class Agent:
    def __init__(
        self,
        backend: ToolBackend,
        audit: AuditLog,
        client: Any | None = None,
        today: date | None = None,
    ):
        self.backend = backend
        self.audit = audit
        self.client = client or anthropic.AsyncAnthropic()
        self.today = today or date.fromisoformat(os.environ.get("COPILOT_TODAY", "2026-10-12"))
        self.tools: list[dict] = []
        self.needs_approval: set[str] = set()

    async def load_tools(self) -> None:
        tools = sorted(
            await self.backend.list_tools(), key=lambda t: t.name
        )  # stable order keeps the cache warm
        self.tools = [
            {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
            for t in tools
        ]
        self.needs_approval = {
            t.name for t in tools if not (t.annotations and t.annotations.read_only_hint is True)
        }

    # ------------------------------------------------------------- public API

    async def ask(self, conv: Conversation, text: str) -> AsyncIterator[dict]:
        if conv.waiting:
            yield {"type": "error", "message": "Approve or reject the pending action first."}
            return
        conv.messages.append({"role": "user", "content": text})
        async for event in self._loop(conv):
            yield event

    async def decide(
        self, conv: Conversation, tool_use_id: str, approved: bool, by: str
    ) -> AsyncIterator[dict]:
        pending = conv.waiting.pop(tool_use_id, None)
        if pending is None:
            yield {"type": "error", "message": "That action is no longer waiting for approval."}
            return
        if approved:
            self.audit.write(session=conv.id, event="approved", tool=pending.name, input=pending.input, by=by)
            yield {
                "type": "tool_call",
                "id": pending.tool_use_id,
                "name": pending.name,
                "input": pending.input,
            }
            block = await self._execute(conv, pending.tool_use_id, pending.name, pending.input)
            yield self._result_event(block)
        else:
            self.audit.write(session=conv.id, event="rejected", tool=pending.name, input=pending.input, by=by)
            block = {
                "type": "tool_result",
                "tool_use_id": pending.tool_use_id,
                "content": f"The finance user ({by}) rejected this action. It was not carried out.",
                "is_error": True,
            }
            yield {"type": "rejected", "id": pending.tool_use_id, "name": pending.name}
        conv.results[pending.tool_use_id] = block
        if conv.waiting:
            yield {"type": "paused", "waiting": [p.__dict__ for p in conv.waiting.values()]}
            return
        self._append_results(conv)
        async for event in self._loop(conv):
            yield event

    # ------------------------------------------------------------- loop

    async def _loop(self, conv: Conversation) -> AsyncIterator[dict]:
        for _ in range(MAX_ROUNDS):
            try:
                response = await self.client.beta.messages.create(
                    model=MODEL,
                    max_tokens=16000,
                    system=f"{SYSTEM_PROMPT}\nToday is {self.today.isoformat()}.",
                    tools=self.tools,
                    messages=conv.messages,
                    output_config={"effort": "medium"},
                    cache_control={"type": "ephemeral"},  # history is re-sent every round; cache the prefix
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
            except anthropic.APIStatusError as e:
                yield {"type": "error", "message": f"Claude API error {e.status_code}: {e.message}"}
                return
            except anthropic.APIConnectionError:
                yield {"type": "error", "message": "Could not reach the Claude API. Check the network."}
                return

            u = response.usage
            yield {
                "type": "usage",
                "input": u.input_tokens,
                "output": u.output_tokens,
                "cache_read": u.cache_read_input_tokens or 0,
            }
            if response.stop_reason == "refusal":
                yield {"type": "error", "message": "Claude declined this request."}
                return
            if response.stop_reason == "max_tokens":
                yield {"type": "error", "message": "The answer was cut off (max_tokens)."}
                return

            conv.messages.append({"role": "assistant", "content": response.content})
            text = "\n".join(b.text for b in response.content if b.type == "text").strip()
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                yield {"type": "answer", "text": text}
                return
            if text:
                yield {"type": "note", "text": text}

            conv.order = [b.id for b in tool_uses]
            conv.results = {}
            for b in tool_uses:
                if b.name in self.needs_approval:
                    conv.waiting[b.id] = PendingCall(b.id, b.name, dict(b.input))
                    self.audit.write(session=conv.id, event="approval_requested", tool=b.name, input=b.input)
                    yield {"type": "approval_required", "id": b.id, "name": b.name, "input": b.input}
                else:
                    yield {"type": "tool_call", "id": b.id, "name": b.name, "input": b.input}
                    block = await self._execute(conv, b.id, b.name, dict(b.input))
                    conv.results[b.id] = block
                    yield self._result_event(block)
            if conv.waiting:
                yield {"type": "paused", "waiting": [p.__dict__ for p in conv.waiting.values()]}
                return
            self._append_results(conv)
        yield {"type": "error", "message": f"Stopped after {MAX_ROUNDS} tool rounds without an answer."}

    async def _execute(self, conv: Conversation, tool_use_id: str, name: str, arguments: dict) -> dict:
        try:
            result = await self.backend.call_tool(name, arguments)
            text, is_error = _result_text(result), bool(getattr(result, "is_error", False))
        except Exception as e:  # a broken tool must not break the conversation
            text, is_error = f"Tool failed: {e}", True
        self.audit.write(session=conv.id, event="tool_call", tool=name, input=arguments, ok=not is_error)
        return {"type": "tool_result", "tool_use_id": tool_use_id, "content": text, "is_error": is_error}

    @staticmethod
    def _result_event(block: dict) -> dict:
        return {
            "type": "tool_result",
            "id": block["tool_use_id"],
            "ok": not block["is_error"],
            "content": block["content"][:4000],
        }

    @staticmethod
    def _append_results(conv: Conversation) -> None:
        # All results for one assistant turn go back together, in the order Claude asked.
        conv.messages.append({"role": "user", "content": [conv.results[i] for i in conv.order]})
        conv.results, conv.order = {}, []
