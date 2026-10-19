"""The morning dashboard: headline numbers and the checks finance runs first thing.

Built on the same functions the MCP server exposes as tools, so the dashboard and
the copilot always agree about a number. Each check carries the question to ask the
copilot when it isn't green.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from . import mcp_server as tools
from .data import HOTEL_NAME

QUESTIONS = {
    "room_charges": "Why don't yesterday's room charges in the POS match the PMS?",
    "card_clearing": "Why is yesterday's card clearing short?",
    "invoices": "Any supplier invoices this week I should check before paying?",
    "ad_spend": "Are we on track with this month's ad budget?",
    "trading": "How did the hotel trade yesterday?",
}


def rand(value: str | Decimal) -> str:
    """R1,234.56 (and -R20.00), the way finance writes it."""
    amount = Decimal(value)
    return f"{'-' if amount < 0 else ''}R{abs(amount):,.2f}"


def _check(id: str, title: str, status: str, headline: str, detail: str, ask: bool = True) -> dict:
    return {
        "id": id,
        "title": title,
        "status": status,  # ok | warn | fail
        "headline": headline,
        "detail": detail,
        "question": QUESTIONS.get(id) if ask and status != "ok" else None,
    }


def room_charges(day: str) -> dict:
    rc = tools.pos_to_pms_room_charges(day)
    if rc.get("error"):
        return _check("room_charges", "Room charges: POS → PMS", "warn", "No data", rc["error"], ask=False)
    if rc["status"] == "balanced":
        return _check(
            "room_charges",
            "Room charges: POS → PMS",
            "ok",
            "All posted",
            f"{rc['pos_room_charge_checks']} checks, {rand(rc['pos_room_charge_total'])}, all on folios.",
        )
    missing = rc["unposted_checks"]
    first = missing[0]
    return _check(
        "room_charges",
        "Room charges: POS → PMS",
        "fail",
        f"{rand(sum(Decimal(c['amount']) for c in missing))} not on folios",
        f"Check {first['check_no']} ({first['revenue_centre']}, room {first['room']}, {first['closed_at']}) "
        f"never reached the PMS{' (and ' + str(len(missing) - 1) + ' more)' if len(missing) > 1 else ''}.",
    )


def card_clearing(day: str) -> dict:
    row = tools.card_clearing_reconciliation(day, day)["days"]
    if not row or "unexplained_difference" not in row[0]:
        return _check(
            "card_clearing", "Card clearing", "warn", "Not settled yet", "No acquirer settlement.", False
        )
    r = row[0]
    detail = (
        f"POS card {rand(r['pos_card_takings'])}; "
        f"settled {rand(r['settled_to_bank'])} + fees {rand(r['acquirer_fees'])}"
    )
    if Decimal(r["journal_adjustments"]):
        detail += f"; adjustments {rand(r['journal_adjustments'])}"
    if r["status"] == "balanced":
        return _check("card_clearing", "Card clearing", "ok", "Balanced", detail + ".")
    return _check(
        "card_clearing",
        "Card clearing",
        "fail",
        f"{rand(r['unexplained_difference'])} {r['status']}",
        detail + ".",
    )


def cash(day: str) -> dict:
    s = tools.pos_day_summary(day)
    if s.get("error"):
        return _check("cash", "POS tenders", "warn", "No data", s["error"], ask=False)
    over_short = Decimal(s["over_short"])
    status = "ok" if over_short == 0 else "warn"
    return _check(
        "cash",
        "POS tenders",
        status,
        "Balanced" if status == "ok" else f"{rand(over_short)} over/short",
        f"Sales {rand(s['total_sales_incl_vat'])} incl. VAT = tenders {rand(s['total_tenders'])}.",
        ask=False,
    )


def invoices(day: str) -> dict:
    week_start = (date.fromisoformat(day) - timedelta(days=6)).isoformat()
    result = tools.supplier_invoices(week_start, day)
    status_of = {i["invoice_no"]: i["status"] for i in result["invoices"]}
    open_pairs = {
        tuple(sorted([i["invoice_no"], *i["possible_duplicate_of"]]))
        for i in result["invoices"]
        if i["possible_duplicate_of"]
        and all(status_of.get(n) != "on_hold" for n in [i["invoice_no"], *i["possible_duplicate_of"]])
    }
    held = sum(1 for s in status_of.values() if s == "on_hold")
    summary = f"{result['count']} invoices this week, {rand(result['total_incl_vat'])}"
    if not open_pairs:
        return _check(
            "invoices",
            "Supplier invoices",
            "ok",
            "No duplicates",
            summary + (f"; {held} on hold." if held else "."),
        )
    pair = sorted(open_pairs)[0]
    return _check(
        "invoices",
        "Supplier invoices",
        "warn",
        "Possible duplicate",
        f"{' and '.join(pair)}: same supplier and amount within a week. {summary}.",
    )


def bank(day: str) -> dict:
    ex = tools.bank_exceptions()
    if not ex["count"]:
        return _check("bank", "Bank reconciliation", "ok", "All matched", "No open exceptions.", ask=False)
    lines = "; ".join(f"{e['bank_ref']} {rand(e['amount'])}" for e in ex["exceptions"])
    return _check(
        "bank", "Bank reconciliation", "warn", f"{ex['count']} lines need a person", lines + ".", ask=False
    )


def ad_spend(day: str) -> dict:
    month = day[:7]
    over = [
        p for p in tools.ad_spend_vs_budget(month)["platforms"] if Decimal(p["projected_over_budget_by"]) > 0
    ]
    if not over:
        return _check("ad_spend", "Ad spend", "ok", "On budget", "All platforms projected within budget.")
    p = over[0]
    name = {"google_ads": "Google Ads", "meta": "Meta"}.get(p["platform"], p["platform"])
    return _check(
        "ad_spend",
        "Ad spend",
        "warn",
        f"{name} {rand(p['projected_over_budget_by'])} over",
        f"Projected {rand(p['projected_month_end'])} against a {rand(p['budget'])} budget.",
    )


def overview(business_day: date) -> dict:
    day = business_day.isoformat()
    stats = tools.hotel_day_statistics(day)
    kpis = []
    if not stats.get("error"):
        rooms = f"{stats['rooms_sold']} of {stats['rooms_available']} rooms"
        kpis = [
            {"label": "Occupancy", "value": f"{stats['occupancy_pct']}%", "note": rooms},
            {"label": "ADR", "value": rand(stats["adr"]), "note": "Average daily rate, excl. VAT"},
            {"label": "RevPAR", "value": rand(stats["revpar"]), "note": "Revenue per available room"},
            {"label": "F&B revenue", "value": rand(stats["fnb_revenue"]), "note": "POS, excl. VAT"},
        ]
    checks = [room_charges(day), card_clearing(day), cash(day), invoices(day), bank(day), ad_spend(day)]
    return {
        "hotel": HOTEL_NAME,
        "business_date": day,
        "kpis": kpis,
        "checks": checks,
        "suggestions": [QUESTIONS["trading"], *(c["question"] for c in checks if c["question"])],
    }
