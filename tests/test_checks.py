from datetime import date

from copilot import checks
from copilot import mcp_server as s

DAY = date(2026, 10, 11)


def statuses():
    return {c["id"]: c["status"] for c in checks.overview(DAY)["checks"]}


def test_rand_formats_like_finance():
    assert checks.rand("27845.5") == "R27,845.50"
    assert checks.rand("-20") == "-R20.00"


def test_overview_shows_the_planted_problems():
    o = checks.overview(DAY)
    assert o["business_date"] == "2026-10-11"
    assert [k["value"] for k in o["kpis"]] == ["75.8%", "R1,990.00", "R1,509.08", "R35,583.04"]
    assert statuses() == {
        "room_charges": "fail",
        "card_clearing": "fail",
        "cash": "ok",
        "invoices": "warn",
        "bank": "warn",
        "ad_spend": "warn",
    }
    by_id = {c["id"]: c for c in o["checks"]}
    assert by_id["room_charges"]["headline"] == "R385.00 not on folios"
    assert by_id["card_clearing"]["headline"] == "-R20.00 short"
    assert by_id["card_clearing"]["question"] == checks.QUESTIONS["card_clearing"]
    assert by_id["cash"]["question"] is None  # nothing to ask about a green check


def test_approved_fixes_turn_checks_green():
    s.post_room_charge_to_folio("50608", "Interface was down at 23:47")
    s.post_journal_adjustment(
        "2026-10-11", "4900", "1020", "20", "Refund on terminal not in POS", "FNB-1011-014"
    )
    s.flag_invoice("CLS-20958", "Possible duplicate of CLS-20931")
    after = statuses()
    assert after["room_charges"] == after["card_clearing"] == after["invoices"] == "ok"
    assert after["ad_spend"] == "warn"  # a decision for marketing, not something the copilot changes
    bank = {c["id"]: c for c in checks.overview(DAY)["checks"]}["bank"]
    assert bank["headline"] == "2 lines need a person"
