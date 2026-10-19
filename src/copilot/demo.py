"""Demo mode: a stand-in for the Claude API that replays recorded answers.

Lets the app run in public with no API key and no cost. Only the model's words
are recorded: the tool calls still run against the real MCP server and demo
database, approvals still pause the agent, and the audit log is still written.
The evaluation set (`copilot.evals`) checks every amount in these answers
against what the tools actually returned.

Each script answers one suggested question. If the problem it is about has
already been fixed (someone approved the change), the script says so instead of
proposing the same change twice.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace

from .checks import QUESTIONS

# ------------------------------------------------------------------ response builders


def text(t: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=t)


def tool_use(id: str, name: str, input: dict) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def reply(content: list, stop_reason: str | None = None) -> SimpleNamespace:
    stop = stop_reason or ("tool_use" if any(b.type == "tool_use" for b in content) else "end_turn")
    usage = SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_input_tokens=0)
    return SimpleNamespace(content=content, stop_reason=stop, usage=usage)


# ------------------------------------------------------------------ scripts


@dataclass
class Script:
    steps: list[list]
    still_open: Callable[[list[dict]], bool] = lambda results: True  # given step 1's tool results
    resolved: str = "That's already been dealt with: the check shows no difference now."


def _payloads(results: list[dict]) -> list[dict]:
    out = []
    for r in results:
        try:
            out.append(json.loads(r["content"]))
        except (TypeError, ValueError):
            pass
    return out


def _card_short(results: list[dict]) -> bool:
    return any(p.get("days_with_differences") for p in _payloads(results))


def _room_charge_missing(results: list[dict]) -> bool:
    return any(p.get("unposted_checks") for p in _payloads(results))


def _duplicate_unheld(results: list[dict]) -> bool:
    for p in _payloads(results):
        status = {i["invoice_no"]: i["status"] for i in p.get("invoices", [])}
        if status.get("CLS-20958") == "filed" and status.get("CLS-20931") == "filed":
            return True
    return False


DAY = "2026-10-11"

SCRIPTS: dict[str, Script] = {
    QUESTIONS["card_clearing"]: Script(
        steps=[
            [
                text(
                    "I'll compare card takings with the acquirer's settlement and check the bank exceptions."
                ),
                tool_use("c1", "card_clearing_reconciliation", {"day_from": DAY, "day_to": DAY}),
                tool_use("c2", "bank_exceptions", {}),
            ],
            [
                text(
                    "The R20.00 matches bank line FNB-1011-014, a refund done on the pool bar card terminal "
                    "that never went through the POS. I'll propose the correcting journal."
                ),
                tool_use(
                    "c3",
                    "post_journal_adjustment",
                    {
                        "day": DAY,
                        "debit_account": "4900",
                        "credit_account": "1020",
                        "amount": "20.00",
                        "reason": "Card refund on pool bar terminal 2 (bank ref FNB-1011-014) was not captured in the POS",
                        "bank_ref": "FNB-1011-014",
                    },
                ),
            ],
            [
                text(
                    "**Card clearing for 11 October is now balanced.**\n\n"
                    "- POS card takings: R27,845.50\n"
                    "- Settled to bank: R27,302.00, plus acquirer fees of R523.50\n"
                    "- The unexplained difference was -R20.00: bank line FNB-1011-014, a card refund on the "
                    "pool bar terminal that wasn't put through the POS\n"
                    "- Posted: Dr 4900 Sales refunds / Cr 1020 Card clearing, R20.00, and the bank line is resolved\n\n"
                    "Worth asking the pool bar to process refunds through the POS so this doesn't happen again."
                )
            ],
        ],
        still_open=_card_short,
        resolved="Card clearing for 11 October is already balanced: the R20.00 refund was adjusted earlier. "
        "Use **Reset demo** to replay it.",
    ),  # fmt: skip
    QUESTIONS["room_charges"]: Script(
        steps=[
            [
                text(
                    "I'll compare the POS checks charged to rooms with what the PMS received through the interface."
                ),
                tool_use("r1", "pos_to_pms_room_charges", {"day": DAY}),
            ],
            [
                text(
                    "One check never reached the PMS: 50608 from the Pool Bar, R385.00 charged to room 214 "
                    "at 23:47. I'll propose posting it to the guest's folio so they are billed."
                ),
                tool_use(
                    "r2",
                    "post_room_charge_to_folio",
                    {
                        "check_no": "50608",
                        "reason": "Pool Bar check closed at 23:47 did not post through the POS interface",
                    },
                ),
            ],
            [
                text(
                    "**Room charges for 11 October now match: check 50608 is on room 214's folio.**\n\n"
                    "- POS room charges: R8,955.00 across 8 checks\n"
                    "- PMS had received R8,570.00, so -R385.00 was missing\n"
                    "- The missing check: 50608, Pool Bar, room 214, closed at 23:47\n\n"
                    "A check this close to midnight often means the interface dropped during the night audit. "
                    "Worth asking IT to check the interface log for 23:40 to 00:00."
                )
            ],
        ],
        still_open=_room_charge_missing,
        resolved="Room charges for 11 October already match: check 50608 was posted to room 214 earlier. "
        "Use **Reset demo** to replay it.",
    ),  # fmt: skip
    QUESTIONS["invoices"]: Script(
        steps=[
            [
                text("I'll look at this week's supplier invoices and check for duplicates."),
                tool_use("i1", "supplier_invoices", {"received_from": "2026-10-05", "received_to": DAY}),
            ],
            [
                text(
                    "CLS-20958 from Coastal Linen Services looks like a duplicate of CLS-20931: same supplier, "
                    "same R8,740.00, three days apart. I'll propose putting it on hold so it isn't paid."
                ),
                tool_use(
                    "i2",
                    "flag_invoice",
                    {
                        "invoice_no": "CLS-20958",
                        "reason": "Possible duplicate of CLS-20931: same supplier and amount, received three days apart",
                    },
                ),
            ],
            [
                text(
                    "**One to check before paying: CLS-20958 is now on hold.**\n\n"
                    "- Coastal Linen Services billed R8,740.00 on 5 October (CLS-20931) and again on 8 October (CLS-20958)\n"
                    "- The other invoices look normal: 5 invoices this week, R53,275.85 in total\n\n"
                    "Ask Coastal Linen whether there were two deliveries. If not, ask them for a credit note "
                    "and reject CLS-20958."
                )
            ],
        ],
        still_open=_duplicate_unheld,
        resolved="CLS-20958 is already on hold as a possible duplicate, so nothing else needs checking "
        "this week. Use **Reset demo** to replay it.",
    ),  # fmt: skip
    QUESTIONS["ad_spend"]: Script(
        steps=[
            [
                text("I'll check October's spend so far against each platform's budget."),
                tool_use("a1", "ad_spend_vs_budget", {"month": "2026-10"}),
            ],
            [
                text(
                    "**Google Ads is on course to overspend October's budget by R1,220.00. Meta is fine.**\n\n"
                    "- Google Ads: R6,820.00 spent in 11 days, projected R19,220.00 against R18,000.00\n"
                    "- Meta: R3,990.00 spent, projected R11,244.55 against R12,000.00\n\n"
                    "Lower Google's daily budget now, or move some of Meta's expected underspend across. "
                    "This is a decision for marketing, so I haven't changed anything."
                )
            ],
        ],
    ),
    QUESTIONS["trading"]: Script(
        steps=[
            [
                text("I'll pull yesterday's statistics from the PMS and POS."),
                tool_use("t1", "hotel_day_statistics", {"day": DAY}),
            ],
            [
                text(
                    "**Sunday 11 October: 75.8% occupancy at an ADR of R1,990.00.**\n\n"
                    "- Rooms sold: 91 of 120\n"
                    "- RevPAR: R1,509.08\n"
                    "- Room revenue: R181,090.00, F&B revenue: R35,583.04 (both excl. VAT)\n"
                    "- Total: R216,673.04"
                )
            ],
        ],
    ),
}

NOT_RECORDED = (
    "This public demo replays recorded answers, so it can only answer the suggested questions. "
    "The tools, data, approvals and audit log are all real. To ask anything, run it locally with "
    "your own Claude API key (see the README)."
)

REJECTED = "Understood, I haven't changed anything. The difference stays open until someone deals with it."


def _key(question: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", question.lower()).strip()


_BY_KEY = {_key(q): s for q, s in SCRIPTS.items()}


class ScriptedClaude:
    """Has the same `beta.messages.create` shape as the Anthropic client the agent uses."""

    def __init__(self) -> None:
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, *, messages: list[dict], **_: object) -> SimpleNamespace:
        start = max(
            i for i, m in enumerate(messages) if m["role"] == "user" and isinstance(m["content"], str)
        )
        question, tail = messages[start]["content"], messages[start + 1 :]
        step = sum(1 for m in tail if m["role"] == "assistant")
        last_results = tail[-1]["content"] if tail and tail[-1]["role"] == "user" else []

        if any(r.get("is_error") and "rejected" in str(r.get("content", "")) for r in last_results):
            return reply([text(REJECTED)])
        script = _BY_KEY.get(_key(question))
        if script is None:
            return reply([text(NOT_RECORDED)])
        if step == 1 and not script.still_open(last_results):
            return reply([text(script.resolved)])
        return reply(script.steps[min(step, len(script.steps) - 1)])
