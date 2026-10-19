import asyncio
import json
from pathlib import Path

import pytest

from copilot import demo, evals
from copilot.agent import Agent, AuditLog, Conversation
from copilot.backends import InProcessBackend
from copilot.checks import QUESTIONS

CASES = json.loads((Path(__file__).parents[1] / "evals" / "cases.json").read_text(encoding="utf-8"))


async def ask(agent, conv, question, approve=True):
    events = [e async for e in agent.ask(conv, question)]
    while conv.waiting:
        for tool_use_id in list(conv.waiting):
            events += [e async for e in agent.decide(conv, tool_use_id, approve, by="test")]
    return events


def answer(events):
    return [e["text"] for e in events if e["type"] == "answer"][-1]


@pytest.fixture
def agent(tmp_path):
    a = Agent(InProcessBackend(), AuditLog(tmp_path / "audit.jsonl"), client=demo.ScriptedClaude())
    asyncio.run(a.load_tools())
    return a


def test_every_dashboard_question_has_a_recorded_answer():
    assert set(QUESTIONS.values()) == set(demo.SCRIPTS)


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_eval_cases_pass_on_recorded_answers(case):
    result = asyncio.run(evals.run_case(case, demo.ScriptedClaude()))
    assert result["passed"], [c for c in result["checks"] if not c["passed"]]


def test_a_fixed_problem_is_not_fixed_twice(agent):
    conv = Conversation(id="x")
    asyncio.run(ask(agent, conv, QUESTIONS["room_charges"]))
    again = asyncio.run(ask(agent, conv, QUESTIONS["room_charges"]))
    assert "already match" in answer(again)
    assert not [e for e in again if e["type"] == "approval_required"]


def test_unknown_question_gets_an_honest_reply(agent):
    events = asyncio.run(ask(agent, Conversation(id="y"), "What's the weather in Umhlanga?"))
    assert answer(events) == demo.NOT_RECORDED
    assert not [e for e in events if e["type"] == "tool_call"]


def test_rejection_is_respected(agent):
    events = asyncio.run(ask(agent, Conversation(id="z"), QUESTIONS["invoices"], approve=False))
    assert answer(events) == demo.REJECTED


def test_ungrounded_amounts_are_caught():
    outputs = ['{"difference": "-20.00", "total": "27845.50"}']
    assert evals.ungrounded_amounts("Short by -R20.00 on R27,845.50", outputs) == []
    assert evals.ungrounded_amounts("About R27,800.00 in total", outputs) == ["R27,800.00"]


def test_eval_report_and_exit_code(tmp_path, capsys):
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps(CASES[:2]), encoding="utf-8")
    out = tmp_path / "report.md"
    assert evals.main(["--cases", str(cases), "--out", str(out)]) == 0
    assert "2 of 2 cases passed" in out.read_text(encoding="utf-8")
