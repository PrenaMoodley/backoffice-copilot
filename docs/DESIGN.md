# Design notes

## Why an agent here, and why not more

A finance question like "why is card clearing short?" doesn't have a fixed path: the
answer might be in settlements, bank exceptions, the POS or the ledger. Letting Claude
choose which tools to call is worth it here. But each tool is a fixed, tested query,
and every number comes from code. The agent decides *where to look*; it never decides
*what the numbers are*.

Where the path *is* fixed (posting a day's POS journal, matching bank lines), the other
projects in this portfolio use plain code instead of an agent.

## The dashboard and the copilot share one set of numbers

The morning checks (`checks.py`) call the same functions the MCP server exposes as tools.
There is one place where card clearing is reconciled or room charges are compared, so the
dashboard can't say "balanced" while the copilot says "short". When a person approves a
fix, the dashboard reloads and the check turns green, which is how they know it worked.

The checks are plain code, not AI: they run the same way every morning and cost nothing.
The copilot is only involved when a check isn't green and someone wants to know why.

## Approval comes from the tool, not the prompt

A system prompt that says "ask before writing" can be ignored or talked around. Here the
rule is enforced in code: the agent loop reads each MCP tool's annotations once at
start-up, and any tool not marked `readOnlyHint=True` is never executed without a
decision from a person. The prompt only explains the rule to Claude so it can describe
the action well.

## Why a hand-written loop

The SDK's tool runner would be less code, but this loop must be able to:

1. stop in the middle of a turn when an approval is needed,
2. keep the conversation exactly as it was (append-only, Claude's blocks unchanged),
3. resume with all of that turn's tool results in one message, in the order Claude asked,
4. emit an event for every step so the UI can show it.

## Prompt caching

Tools are sorted by name and the system prompt is static apart from the date, so the
prefix is stable. Automatic caching re-uses the whole conversation prefix on each tool
round, which matters because every round re-sends the history.

## Demo mode

A public link to an app that calls a paid API needs a plan for cost and abuse. In demo
mode (`demo.py`) a stand-in client replays recorded answers to the suggested questions.
Only the model's words are recorded: the agent loop, MCP tool calls, approvals, database
changes and audit log all run for real. Each script checks whether its problem is still
open before proposing a fix, so asking twice doesn't post the same journal twice.

Live mode caps messages per conversation and per day in the app; the API key's spending
limit in the Claude Console is the backstop.

## Evaluation

Unit tests check the tools and the loop. They can't tell whether Claude picks the right
tools, proposes the right fix, or slips in a number it worked out itself. The evaluation
set does: each case runs the whole agent on a fresh database and scores the tools called,
the change proposed (or correctly not proposed), the facts in the answer, the state of the
data afterwards, and whether every rand amount in the answer came from a tool result.

On recorded answers it runs in CI and keeps the demo scripts honest. With `--live` it
scores the real model, and should be run before any change to the system prompt, a tool
description or the model.

## Why plain JavaScript for the UI

One small page served by the same FastAPI app means one container to deploy and no build
step. A Next.js front end (as in recon-dashboard) is the better choice once there are
several pages, sign-in and shared components.

## Failure handling

| Failure | Behaviour |
|---|---|
| Tool raises a validation error | Returned to Claude as `is_error` with the message, so it can retry correctly |
| Tool crashes | Returned as a generic error; details stay in the server log |
| Claude refuses (`stop_reason: refusal`) | Server-side fallback model is tried; if that also refuses, the UI says so |
| Answer cut off (`max_tokens`) | Reported, not shown as a complete answer |
| API down or rate limited | SDK retries; then a clear error in the UI |
| More than 10 tool rounds | Stops and says so, instead of looping |
| Second message while an approval is pending | Refused until the approval is decided |

## What production would add

- Sign-in, so approvals record a real person, and role checks so only finance can approve journals.
- PostgreSQL and the real integrations (PMS, POS, bank feed, ERP) behind the same MCP tools,
  each with a read-only API user and a separate, narrower user for the approved writes.
- Sessions stored server-side with expiry.
- The morning checks run on a schedule before the team arrives, with a summary emailed to finance.
- Live evaluation results tracked over time, so a prompt or model change that makes answers worse is caught.
