"""Evaluation set: questions with known-correct outcomes, scored automatically.

python -m copilot.evals              # recorded demo answers: free, runs in CI
python -m copilot.evals --live       # the Claude API: needs ANTHROPIC_API_KEY and costs a little

Run --live before changing the system prompt, a tool description or the model.

Every case runs on a fresh copy of the demo database, so approvals in one case
can't affect another. Besides each case's own checks, every answer is checked
for ungrounded amounts: a rand figure that no tool returned. The model is told
never to calculate money; this is how we know it didn't.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import mcp_server
from .agent import Agent, AuditLog, Conversation
from .backends import InProcessBackend
from .data import seed
from .demo import ScriptedClaude

AMOUNT = re.compile(r"R\s?(-?\d[\d,]*\.\d{2})")
MAX_DECISIONS = 5


def ungrounded_amounts(answer: str, tool_outputs: list[str]) -> list[str]:
    """Rand amounts in the answer that don't appear in any tool output."""
    seen = "\n".join(tool_outputs)
    return [m.group(0) for m in AMOUNT.finditer(answer) if m.group(1).replace(",", "") not in seen]


async def run_case(case: dict, client: Any) -> dict:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        previous = mcp_server.DB_PATH
        mcp_server.DB_PATH = seed(Path(tmp) / "eval.db")
        try:
            agent = Agent(InProcessBackend(), AuditLog(Path(tmp) / "audit.jsonl"), client=client)
            await agent.load_tools()
            conv = Conversation(id=case["id"])
            events = [e async for e in agent.ask(conv, case["question"])]
            for _ in range(MAX_DECISIONS):
                if not conv.waiting:
                    break
                for tool_use_id in list(conv.waiting):
                    events += [
                        e async for e in agent.decide(conv, tool_use_id, case.get("approve", True), by="eval")
                    ]
            after = None
            if spec := case.get("after"):
                after = getattr(mcp_server, spec["tool"])(**spec["args"])
                for key in spec["path"]:
                    after = after[key]
        finally:
            mcp_server.DB_PATH = previous
    return score(case, events, after)


def score(case: dict, events: list[dict], after: Any = None) -> dict:
    called = [e["name"] for e in events if e["type"] == "tool_call"]
    proposed = [e["name"] for e in events if e["type"] == "approval_required"]
    outputs = [e["content"] for e in events if e["type"] == "tool_result"]
    errors = [e["message"] for e in events if e["type"] == "error"]
    answers = [e["text"] for e in events if e["type"] == "answer"]
    answer = answers[-1] if answers else ""
    flat = answer.replace(",", "")

    checks: list[tuple[str, bool, str]] = [("gave an answer", bool(answer) and not errors, "; ".join(errors))]
    checks += [(f"called {t}", t in called, "") for t in case.get("must_call", [])]
    if tool := case.get("must_propose"):
        checks.append((f"proposed {tool}", tool in proposed, ", ".join(proposed) or "nothing proposed"))
    if case.get("must_not_propose"):
        checks.append(("proposed no changes", not proposed, ", ".join(proposed)))
    if "max_proposals" in case:
        checks.append(
            ("didn't repeat a rejected action", len(proposed) <= case["max_proposals"], str(len(proposed)))
        )
    checks += [(f"answer mentions {s}", s in flat, "") for s in case.get("answer_must_include", [])]
    if case.get("answer_must_not_include_amounts"):
        found = [m.group(0) for m in AMOUNT.finditer(answer)]
        checks.append(("didn't invent figures", not found, ", ".join(found)))
    bad = ungrounded_amounts(answer, outputs)
    checks.append(("every amount came from a tool", not bad, ", ".join(bad)))
    if spec := case.get("after"):
        checks.append(
            (f"{spec['tool']} is {spec['equals']} afterwards", after == spec["equals"], f"got {after}")
        )

    return {
        "id": case["id"],
        "question": case["question"],
        "passed": all(ok for _, ok, _ in checks),
        "checks": [{"name": n, "passed": ok, "detail": d} for n, ok, d in checks],
        "answer": answer,
    }


async def run_all(cases: list[dict], live: bool) -> list[dict]:
    client = None if live else ScriptedClaude()  # None: the agent creates a real Anthropic client
    return [await run_case(case, client) for case in cases]


def report(results: list[dict], live: bool) -> str:
    total = sum(len(r["checks"]) for r in results)
    ok = sum(c["passed"] for r in results for c in r["checks"])
    lines = [
        f"## Evaluation ({'live: Claude API' if live else 'demo: recorded answers'})",
        "",
        f"**{sum(r['passed'] for r in results)} of {len(results)} cases passed** ({ok} of {total} checks).",
        "",
        "| Case | Result | Failed checks |",
        "|---|---|---|",
    ]
    for r in results:
        failed = "; ".join(
            f"{c['name']}" + (f" ({c['detail']})" if c["detail"] else "")
            for c in r["checks"]
            if not c["passed"]
        )
        lines.append(f"| {r['id']} | {'pass' if r['passed'] else '**fail**'} | {failed or ''} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--live", action="store_true", help="use the Claude API instead of recorded answers")
    parser.add_argument("--cases", default="evals/cases.json", type=Path)
    parser.add_argument("--min-pass-rate", default=1.0, type=float, help="fail below this share of cases")
    parser.add_argument("--out", type=Path, help="also write the report to this Markdown file")
    args = parser.parse_args(argv)

    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    results = asyncio.run(run_all(cases, args.live))
    text = report(results, args.live)
    print(text)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0 if sum(r["passed"] for r in results) / len(results) >= args.min_pass_rate else 1


if __name__ == "__main__":
    sys.exit(main())
