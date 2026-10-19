import asyncio
import json
import sqlite3

import pytest
from conftest import FakeClaude, reply, text, tool_use

from copilot.agent import Agent, AuditLog, Conversation
from copilot.backends import InProcessBackend

ADJUST = {
    "day": "2026-10-11",
    "debit_account": "4900",
    "credit_account": "1020",
    "amount": "20.00",
    "reason": "R20 card refund on terminal 2 was not captured in the POS",
}


def make_agent(tmp_path, *responses):
    claude = FakeClaude(*responses)
    agent = Agent(InProcessBackend(), AuditLog(tmp_path / "audit.jsonl"), client=claude)
    asyncio.run(agent.load_tools())
    return agent, claude


def collect(gen):
    async def run():
        return [e async for e in gen]

    return asyncio.run(run())


def types(events):
    return [e["type"] for e in events if e["type"] != "usage"]


def investigation():
    """Claude checks card clearing and bank exceptions together, then proposes a fix."""
    return (
        reply(
            [
                text("Checking card settlements and bank exceptions."),
                tool_use(
                    "t1", "card_clearing_reconciliation", {"day_from": "2026-10-11", "day_to": "2026-10-11"}
                ),
                tool_use("t2", "bank_exceptions", {}),
            ]
        ),
        reply([text("Found it: a terminal refund."), tool_use("t3", "post_journal_adjustment", ADJUST)]),
    )


def test_read_only_tools_run_and_write_tools_wait(tmp_path, demo_db):
    agent, claude = make_agent(tmp_path, *investigation())
    assert agent.needs_approval == {"post_journal_adjustment", "post_room_charge_to_folio", "flag_invoice"}
    conv = Conversation(id="s1")

    events = collect(agent.ask(conv, "Why is yesterday's card clearing short?"))
    assert types(events) == [
        "note", "tool_call", "tool_result", "tool_call", "tool_result", "note", "approval_required", "paused",
    ]  # fmt: skip
    first_result = json.loads(events[3]["content"])
    assert first_result["days"][0]["unexplained_difference"] == "-20.00"

    # Both read results went back to Claude in ONE user message, in call order.
    second_request = claude.requests[1]["messages"]
    assert [b["tool_use_id"] for b in second_request[-1]["content"]] == ["t1", "t2"]

    # Nothing has been written yet.
    with sqlite3.connect(demo_db) as con:
        assert con.execute("SELECT count(*) FROM journal_adjustments").fetchone()[0] == 0
    assert "t3" in conv.waiting


def test_approval_runs_the_tool_and_continues(tmp_path, demo_db):
    agent, claude = make_agent(
        tmp_path,
        *investigation(),
        reply([text("Posted: Dr 4900 / Cr 1020 R20.00. Card clearing now balances.")]),
    )
    conv = Conversation(id="s1")
    collect(agent.ask(conv, "Why is yesterday's card clearing short?"))

    events = collect(agent.decide(conv, "t3", approved=True, by="tester"))
    assert types(events) == ["tool_call", "tool_result", "answer"]
    assert events[-1]["text"].startswith("Posted")
    with sqlite3.connect(demo_db) as con:
        assert con.execute("SELECT amount FROM journal_adjustments").fetchone()[0] == "20.00"

    last_request = claude.requests[-1]["messages"]
    assert last_request[-1]["content"][0]["tool_use_id"] == "t3"
    assert not last_request[-1]["content"][0]["is_error"]

    audit = [(a["event"], a["tool"]) for a in agent.audit.recent()][::-1]
    assert ("approval_requested", "post_journal_adjustment") in audit
    assert ("approved", "post_journal_adjustment") in audit
    assert audit[-1] == ("tool_call", "post_journal_adjustment")


def test_rejection_does_not_write_and_tells_claude(tmp_path, demo_db):
    agent, claude = make_agent(tmp_path, *investigation(), reply([text("Understood, I won't post it.")]))
    conv = Conversation(id="s1")
    collect(agent.ask(conv, "Why is yesterday's card clearing short?"))

    events = collect(agent.decide(conv, "t3", approved=False, by="tester"))
    assert types(events) == ["rejected", "answer"]
    result = claude.requests[-1]["messages"][-1]["content"][0]
    assert result["is_error"] and "rejected" in result["content"]
    with sqlite3.connect(demo_db) as con:
        assert con.execute("SELECT count(*) FROM journal_adjustments").fetchone()[0] == 0


def test_new_message_is_refused_while_waiting(tmp_path):
    agent, _ = make_agent(tmp_path, *investigation())
    conv = Conversation(id="s1")
    collect(agent.ask(conv, "Why is yesterday's card clearing short?"))
    events = collect(agent.ask(conv, "Something else"))
    assert events == [{"type": "error", "message": "Approve or reject the pending action first."}]


def test_tool_error_is_returned_to_claude_not_raised(tmp_path):
    agent, claude = make_agent(
        tmp_path,
        reply([tool_use("t1", "pos_day_summary", {"day": "yesterday"})]),
        reply([text("Please give the date as YYYY-MM-DD.")]),
    )
    events = collect(agent.ask(Conversation(id="s1"), "Takings?"))
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["ok"] is False
    assert claude.requests[1]["messages"][-1]["content"][0]["is_error"] is True


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_stop_reasons_are_reported(tmp_path, stop):
    agent, _ = make_agent(tmp_path, reply([], stop_reason=stop))
    events = collect(agent.ask(Conversation(id="s1"), "Hi"))
    assert events[-1]["type"] == "error"


def test_request_shape(tmp_path):
    agent, claude = make_agent(tmp_path, reply([text("Hello")]))
    collect(agent.ask(Conversation(id="s1"), "Hi"))
    req = claude.requests[0]
    assert req["cache_control"] == {"type": "ephemeral"}
    assert "tool_choice" not in req
    assert [t["name"] for t in req["tools"]] == sorted(t["name"] for t in req["tools"])
    assert "Today is 2026-10-12" in req["system"]
