"""MCP server: the hotel back office as tools.

Read-only tools are annotated readOnlyHint=True. Tools that change data are
not, and the copilot's agent will not run them without a person's approval.
Every figure is calculated here, in Decimal, so the model only ever quotes
numbers; it never has to add them up.

Run on its own (stdio):  python -m copilot.mcp_server
"""

from __future__ import annotations

import calendar
import os
import sqlite3
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .data import seed

CENT = Decimal("0.01")
VAT_FRACTION = Decimal(15) / Decimal(115)
ADJUSTMENT_LIMIT = Decimal("1000.00")  # larger corrections go through the normal journal process
CARD_CLEARING = "1020"

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)

DB_PATH = Path(os.environ.get("BACKOFFICE_DB", "backoffice_demo.db"))

mcp = MCPServer(
    name="backoffice",
    instructions=(
        "Back office of a fictional hotel: PMS room statistics, POS takings and room charges, card "
        "settlements, bank reconciliation exceptions, supplier invoices and advertising spend. "
        "Amounts are ZAR; POS prices include 15% VAT; PMS room revenue excludes VAT."
    ),
)


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """A connection that commits on success, rolls back on error, and always closes."""
    if not DB_PATH.exists():
        seed(DB_PATH)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _d(value: str | Decimal) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _money(value: Decimal) -> str:
    return str(value.quantize(CENT, rounding=ROUND_HALF_UP))


def _check_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise ToolError(f"{field} must be a date like 2026-10-11, got '{value}'") from e


# ---------------------------------------------------------------- read-only tools


@mcp.tool(annotations=READ_ONLY)
def pos_day_summary(day: str) -> dict:
    """POS takings for one business day: sales by outlet (incl. and excl. VAT), output VAT,
    tenders (cash, card, room charge), and cash over/short. Day is YYYY-MM-DD."""
    _check_date(day, "day")
    with _db() as con:
        sales = con.execute(
            "SELECT outlet, gross_incl_vat FROM pos_sales WHERE day=? ORDER BY outlet", (day,)
        )
        sales = [(r["outlet"], _d(r["gross_incl_vat"])) for r in sales]
        tenders = con.execute("SELECT tender, amount FROM pos_tenders WHERE day=? ORDER BY tender", (day,))
        tenders = [(r["tender"], _d(r["amount"])) for r in tenders]
    if not sales:
        return {"day": day, "error": "No POS data for this day."}
    total_sales = sum((s for _, s in sales), Decimal("0"))
    total_tenders = sum((t for _, t in tenders), Decimal("0"))
    vat = _d(total_sales * VAT_FRACTION)
    return {
        "day": day,
        "sales_by_outlet": [
            {"outlet": o, "incl_vat": _money(s), "excl_vat": _money(s - s * VAT_FRACTION)} for o, s in sales
        ],
        "total_sales_incl_vat": _money(total_sales),
        "output_vat": _money(vat),
        "tenders": [{"tender": t, "amount": _money(a)} for t, a in tenders],
        "total_tenders": _money(total_tenders),
        "over_short": _money(total_tenders - total_sales),
    }


@mcp.tool(annotations=READ_ONLY)
def hotel_day_statistics(day: str) -> dict:
    """The hotel's headline numbers for one business day (YYYY-MM-DD): rooms sold, occupancy,
    average daily rate (ADR), revenue per available room (RevPAR), room revenue from the PMS
    and food & beverage revenue from the POS. All revenue excludes VAT."""
    _check_date(day, "day")
    with _db() as con:
        pms = con.execute("SELECT * FROM pms_daily WHERE day=?", (day,)).fetchone()
        pos = con.execute("SELECT gross_incl_vat FROM pos_sales WHERE day=?", (day,)).fetchall()
    if pms is None:
        return {"day": day, "error": "No PMS statistics for this day."}
    available, sold = pms["rooms_available"], pms["rooms_sold"]
    rooms = _d(pms["room_revenue_excl_vat"])
    fnb_incl = sum((_d(r["gross_incl_vat"]) for r in pos), Decimal("0"))
    fnb = fnb_incl - _d(fnb_incl * VAT_FRACTION)
    return {
        "day": day,
        "rooms_available": available,
        "rooms_sold": sold,
        "occupancy_pct": f"{Decimal(sold) / Decimal(available) * 100:.1f}",
        "adr": _money(rooms / sold) if sold else "0.00",
        "revpar": _money(rooms / available),
        "room_revenue": _money(rooms),
        "fnb_revenue": _money(fnb),
        "total_revenue": _money(rooms + fnb),
    }


@mcp.tool(annotations=READ_ONLY)
def pos_to_pms_room_charges(day: str) -> dict:
    """For one business day (YYYY-MM-DD): POS checks charged to a guest room vs. the charges the
    PMS received through the POS interface. Lists any check that never reached the PMS (the
    guest would not be billed for it) and any PMS posting with no matching check."""
    _check_date(day, "day")
    with _db() as con:
        checks = [
            dict(r)
            for r in con.execute("SELECT * FROM pos_room_charges WHERE day=? ORDER BY check_no", (day,))
        ]
        postings = [dict(r) for r in con.execute("SELECT * FROM pms_interface_postings WHERE day=?", (day,))]
    if not checks and not postings:
        return {"day": day, "error": "No room charges for this day."}
    posted = {p["check_no"] for p in postings}
    known = {c["check_no"] for c in checks}
    pos_total = sum((_d(c["amount"]) for c in checks), Decimal("0"))
    pms_total = sum((_d(p["amount"]) for p in postings), Decimal("0"))
    unposted = [c for c in checks if c["check_no"] not in posted]
    orphans = [p for p in postings if p["check_no"] not in known]
    return {
        "day": day,
        "pos_room_charge_checks": len(checks),
        "pos_room_charge_total": _money(pos_total),
        "pms_interface_postings_total": _money(pms_total),
        "difference": _money(pms_total - pos_total),
        "unposted_checks": [
            {k: c[k] for k in ("check_no", "revenue_centre", "room", "amount", "closed_at")} for c in unposted
        ],
        "postings_without_a_check": [{k: p[k] for k in ("check_no", "room", "amount")} for p in orphans],
        "status": "balanced" if not unposted and not orphans else "difference",
    }


@mcp.tool(annotations=READ_ONLY)
def card_clearing_reconciliation(day_from: str, day_to: str) -> dict:
    """Per day: card takings in the POS vs. what the card acquirer settled to the bank, the
    acquirer's fees, approved journal adjustments to the card clearing account, and any
    unexplained difference left. A non-zero difference means money is missing from (or extra
    in) the card clearing account. Dates are YYYY-MM-DD, inclusive."""
    start, end = _check_date(day_from, "day_from"), _check_date(day_to, "day_to")
    if end < start or (end - start).days > 62:
        raise ToolError("Use a range of up to two months, with day_to on or after day_from.")
    rows = []
    with _db() as con:
        adjustments: dict[str, Decimal] = defaultdict(Decimal)
        for a in con.execute(
            "SELECT day, debit_account, credit_account, amount FROM journal_adjustments "
            "WHERE day BETWEEN ? AND ? AND ? IN (debit_account, credit_account)",
            (day_from, day_to, CARD_CLEARING),
        ):
            sign = 1 if a["debit_account"] == CARD_CLEARING else -1
            adjustments[a["day"]] += sign * _d(a["amount"])
        for r in con.execute(
            "SELECT t.day, t.amount AS card, s.settled, s.fees FROM pos_tenders t "
            "LEFT JOIN card_settlements s ON s.day = t.day "
            "WHERE t.tender = 'Card' AND t.day BETWEEN ? AND ? ORDER BY t.day",
            (day_from, day_to),
        ):
            card = _d(r["card"])
            if r["settled"] is None:
                rows.append({"day": r["day"], "pos_card_takings": _money(card), "status": "not settled yet"})
                continue
            settled, fees = _d(r["settled"]), _d(r["fees"])
            adjusted = adjustments[r["day"]]
            diff = settled + fees - (card + adjusted)
            rows.append(
                {
                    "day": r["day"],
                    "pos_card_takings": _money(card),
                    "settled_to_bank": _money(settled),
                    "acquirer_fees": _money(fees),
                    "journal_adjustments": _money(adjusted),
                    "unexplained_difference": _money(diff),
                    "status": "balanced" if diff == 0 else ("short" if diff < 0 else "over"),
                }
            )
    return {
        "days": rows,
        "days_with_differences": [r["day"] for r in rows if r.get("status") in ("short", "over")],
    }


@mcp.tool(annotations=READ_ONLY)
def bank_exceptions(status: str = "open") -> dict:
    """Bank lines the reconciliation could not match automatically, with the reason.
    status is 'open' (default) or 'all'."""
    query = "SELECT * FROM bank_exceptions" + (" WHERE status='open'" if status == "open" else "")
    with _db() as con:
        rows = [dict(r) for r in con.execute(query + " ORDER BY day")]
    return {"exceptions": rows, "count": len(rows)}


@mcp.tool(annotations=READ_ONLY)
def supplier_invoices(received_from: str, received_to: str, supplier: str | None = None) -> dict:
    """Supplier invoices filed from the invoices mailbox, received in a date range (YYYY-MM-DD,
    inclusive), optionally for one supplier (partial name). Flags possible duplicates: the
    same supplier and amount within 7 days under a different invoice number."""
    _check_date(received_from, "received_from")
    _check_date(received_to, "received_to")
    sql = "SELECT * FROM supplier_invoices WHERE received BETWEEN ? AND ?"
    args: list = [received_from, received_to]
    if supplier:
        sql += " AND lower(supplier) LIKE ?"
        args.append(f"%{supplier.lower()}%")
    with _db() as con:
        rows = [dict(r) for r in con.execute(sql + " ORDER BY received, id", args)]
        everything = [dict(r) for r in con.execute("SELECT * FROM supplier_invoices")]

    for inv in rows:
        received = date.fromisoformat(inv["received"])
        twins = [
            o["invoice_no"]
            for o in everything
            if o["id"] != inv["id"]
            and o["supplier"] == inv["supplier"]
            and o["amount_incl_vat"] == inv["amount_incl_vat"]
            and abs((date.fromisoformat(o["received"]) - received).days) <= 7
        ]
        inv["possible_duplicate_of"] = twins
    total = sum((_d(r["amount_incl_vat"]) for r in rows), Decimal("0"))
    return {"invoices": rows, "count": len(rows), "total_incl_vat": _money(total)}


@mcp.tool(annotations=READ_ONLY)
def ad_spend_vs_budget(month: str) -> dict:
    """Advertising spend so far in a month (YYYY-MM) per platform against its budget, and the
    projected month-end spend at the current daily run-rate."""
    try:
        year, mon = (int(x) for x in month.split("-"))
        days_in_month = calendar.monthrange(year, mon)[1]
    except ValueError as e:
        raise ToolError(f"month must look like 2026-10, got '{month}'") from e
    with _db() as con:
        spend: dict[str, list[Decimal]] = defaultdict(list)
        for r in con.execute(
            "SELECT platform, spend FROM ad_spend WHERE day LIKE ? ORDER BY day", (f"{month}-%",)
        ):
            spend[r["platform"]].append(_d(r["spend"]))
        budgets = {
            r["platform"]: _d(r["budget"])
            for r in con.execute("SELECT platform, budget FROM ad_budgets WHERE month=?", (month,))
        }
    platforms = []
    for platform in sorted(set(spend) | set(budgets)):
        days = spend.get(platform, [])
        to_date = sum(days, Decimal("0"))
        projected = (to_date / len(days) * days_in_month) if days else Decimal("0")
        budget = budgets.get(platform)
        platforms.append(
            {
                "platform": platform,
                "days_with_data": len(days),
                "spend_to_date": _money(to_date),
                "budget": _money(budget) if budget is not None else None,
                "budget_used_pct": f"{to_date / budget * 100:.1f}" if budget else None,
                "projected_month_end": _money(projected),
                "projected_over_budget_by": _money(projected - budget)
                if budget and projected > budget
                else "0.00",
            }
        )
    return {"month": month, "days_in_month": days_in_month, "platforms": platforms}


@mcp.resource("backoffice://chart-of-accounts", mime_type="text/csv")
def chart_of_accounts() -> str:
    """The general ledger accounts the copilot may use."""
    with _db() as con:
        return "number,name,category\n" + "\n".join(
            f"{r['number']},{r['name']},{r['category']}" for r in con.execute("SELECT * FROM accounts")
        )


@mcp.tool(annotations=READ_ONLY)
def list_accounts() -> dict:
    """General ledger accounts (number, name, category), for choosing journal accounts."""
    with _db() as con:
        return {"accounts": [dict(r) for r in con.execute("SELECT * FROM accounts ORDER BY number")]}


# ---------------------------------------------------------------- tools that change data (need approval)


@mcp.tool(annotations=WRITES)
def post_journal_adjustment(
    day: str, debit_account: str, credit_account: str, amount: str, reason: str, bank_ref: str | None = None
) -> dict:
    """Post a small balancing journal adjustment (one debit, one credit, same amount, ZAR,
    max R1,000). If it explains an open bank exception, pass its bank_ref and that exception
    is marked resolved. Requires a person's approval before it runs. Explain the reason clearly."""
    _check_date(day, "day")
    value = _d(amount)
    if value <= 0 or value > ADJUSTMENT_LIMIT:
        raise ToolError(
            f"Amount must be between R0.01 and R{ADJUSTMENT_LIMIT}; larger corrections need a full journal."
        )
    if debit_account == credit_account:
        raise ToolError("Debit and credit accounts must differ.")
    if len(reason.strip()) < 10:
        raise ToolError("Give a reason of at least a short sentence; it goes into the audit trail.")
    with _db() as con:
        known = {r["number"] for r in con.execute("SELECT number FROM accounts")}
        for acc in (debit_account, credit_account):
            if acc not in known:
                raise ToolError(f"Account {acc} does not exist. Use list_accounts.")
        if bank_ref:
            open_refs = {
                r["bank_ref"] for r in con.execute("SELECT bank_ref FROM bank_exceptions WHERE status='open'")
            }
            if bank_ref not in open_refs:
                raise ToolError(f"{bank_ref} is not an open bank exception. Use bank_exceptions.")
        cur = con.execute(
            "INSERT INTO journal_adjustments (day, debit_account, credit_account, amount, reason) "
            "VALUES (?,?,?,?,?)",
            (day, debit_account, credit_account, _money(value), reason.strip()),
        )
        if bank_ref:
            con.execute(
                "UPDATE bank_exceptions SET status='resolved', note=? WHERE bank_ref=?",
                (f"Resolved by journal adjustment #{cur.lastrowid}", bank_ref),
            )
    return {"posted": True, "adjustment_id": cur.lastrowid, "day": day, "debit": debit_account,
            "credit": credit_account, "amount": _money(value), "resolved_bank_ref": bank_ref}  # fmt: skip


@mcp.tool(annotations=WRITES)
def post_room_charge_to_folio(check_no: str, reason: str) -> dict:
    """Post a POS room-charge check that never reached the PMS to the guest's folio, so the
    guest is billed. Only for checks listed as unposted by pos_to_pms_room_charges.
    Requires a person's approval before it runs."""
    if len(reason.strip()) < 10:
        raise ToolError("Give a reason of at least a short sentence; it goes into the audit trail.")
    with _db() as con:
        check = con.execute("SELECT * FROM pos_room_charges WHERE check_no=?", (check_no,)).fetchone()
        if check is None:
            raise ToolError(f"No POS check {check_no}.")
        if con.execute("SELECT 1 FROM pms_interface_postings WHERE check_no=?", (check_no,)).fetchone():
            raise ToolError(
                f"Check {check_no} is already on a folio in the PMS; posting it again would bill twice."
            )
        con.execute(
            "INSERT INTO pms_interface_postings (day, check_no, room, amount, source) VALUES (?,?,?,?,?)",
            (check["day"], check_no, check["room"], check["amount"], f"Manual repost: {reason.strip()}"),
        )
    return {
        "posted": True,
        "check_no": check_no,
        "room": check["room"],
        "amount": _money(_d(check["amount"])),
    }


@mcp.tool(annotations=WRITES)
def flag_invoice(invoice_no: str, reason: str) -> dict:
    """Put a supplier invoice on hold for review (for example a possible duplicate), so it
    is not paid. Requires a person's approval before it runs."""
    with _db() as con:
        cur = con.execute(
            "UPDATE supplier_invoices SET status='on_hold', flag_reason=? WHERE invoice_no=?",
            (reason.strip(), invoice_no),
        )
        if cur.rowcount == 0:
            raise ToolError(f"No invoice {invoice_no}.")
    return {"on_hold": True, "invoice_no": invoice_no}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
