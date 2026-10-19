import asyncio
import json
import sqlite3

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from copilot import mcp_server as s
from copilot.backends import InProcessBackend


def test_pos_day_summary_balances():
    day = s.pos_day_summary("2026-10-11")
    assert day["total_sales_incl_vat"] == "40920.50"
    assert day["total_tenders"] == "40920.50"
    assert day["over_short"] == "0.00"
    assert day["output_vat"] == "5337.46"


def test_pos_day_without_data():
    assert "error" in s.pos_day_summary("2026-01-01")


def test_card_clearing_finds_the_r20_shortfall():
    rec = s.card_clearing_reconciliation("2026-10-05", "2026-10-11")
    assert rec["days_with_differences"] == ["2026-10-11"]
    day = rec["days"][-1]
    assert (day["unexplained_difference"], day["status"]) == ("-20.00", "short")


def test_bank_exceptions_include_the_terminal_refund():
    refs = {e["bank_ref"]: e for e in s.bank_exceptions()["exceptions"]}
    assert refs["FNB-1011-014"]["amount"] == "-20.00"


def test_supplier_invoices_flag_possible_duplicate():
    result = s.supplier_invoices("2026-10-05", "2026-10-11")
    by_no = {i["invoice_no"]: i for i in result["invoices"]}
    assert by_no["CLS-20958"]["possible_duplicate_of"] == ["CLS-20931"]
    assert by_no["BFP-7712"]["possible_duplicate_of"] == []
    assert result["count"] == 5
    assert s.supplier_invoices("2026-10-01", "2026-10-31", supplier="linen")["count"] == 2


def test_ad_spend_projects_google_over_budget():
    platforms = {p["platform"]: p for p in s.ad_spend_vs_budget("2026-10")["platforms"]}
    google, meta = platforms["google_ads"], platforms["meta"]
    assert google["spend_to_date"] == "6820.00"
    assert google["projected_month_end"] == "19220.00"  # 620/day x 31
    assert google["projected_over_budget_by"] == "1220.00"
    assert meta["projected_over_budget_by"] == "0.00"


def test_bad_inputs_are_explained():
    with pytest.raises(ToolError, match="2026-10-11"):
        s.pos_day_summary("yesterday")
    with pytest.raises(ToolError, match="2026-10"):
        s.ad_spend_vs_budget("October")


def test_journal_adjustment_rules_and_write(demo_db):
    with pytest.raises(ToolError, match="R1000.00"):
        s.post_journal_adjustment("2026-10-11", "4900", "1020", "5000", "Too large for the copilot")
    with pytest.raises(ToolError, match="does not exist"):
        s.post_journal_adjustment("2026-10-11", "9999", "1020", "20", "Unknown account test")
    done = s.post_journal_adjustment(
        "2026-10-11", "4900", "1020", "20", "Card refund on terminal 2 not in POS"
    )
    assert done["posted"] and done["amount"] == "20.00"
    with sqlite3.connect(demo_db) as con:
        assert con.execute("SELECT amount, reason FROM journal_adjustments").fetchone()[0] == "20.00"


def test_flag_invoice(demo_db):
    s.flag_invoice("CLS-20958", "Possible duplicate of CLS-20931")
    with sqlite3.connect(demo_db) as con:
        assert (
            con.execute("SELECT status FROM supplier_invoices WHERE invoice_no='CLS-20958'").fetchone()[0]
            == "on_hold"
        )
    with pytest.raises(ToolError, match="No invoice"):
        s.flag_invoice("NOPE-1", "does not exist")


def test_hotel_day_statistics():
    stats = s.hotel_day_statistics("2026-10-11")
    assert (stats["rooms_sold"], stats["occupancy_pct"]) == (91, "75.8")
    assert (stats["adr"], stats["revpar"]) == ("1990.00", "1509.08")
    # F&B is the POS total less output VAT: 40,920.50 - 5,337.46
    assert stats["fnb_revenue"] == "35583.04"
    assert stats["total_revenue"] == "216673.04"
    assert "error" in s.hotel_day_statistics("2026-01-01")


def test_room_charges_find_the_check_that_missed_the_pms():
    rc = s.pos_to_pms_room_charges("2026-10-11")
    assert rc["status"] == "difference"
    assert (rc["pos_room_charge_total"], rc["difference"]) == ("8955.00", "-385.00")
    assert rc["unposted_checks"] == [
        {
            "check_no": "50608",
            "revenue_centre": "Pool Bar",
            "room": "214",
            "amount": "385.00",
            "closed_at": "23:47",
        }
    ]
    # every other day the POS room-charge checks add up to the room-charge tender and all reached the PMS
    for day in ["2026-10-05", "2026-10-08", "2026-10-10"]:
        other = s.pos_to_pms_room_charges(day)
        tender = {t["tender"]: t["amount"] for t in s.pos_day_summary(day)["tenders"]}["Room Charge"]
        assert other["status"] == "balanced" and other["pos_room_charge_total"] == tender


def test_post_room_charge_to_folio_bills_once():
    done = s.post_room_charge_to_folio("50608", "Interface was down at 23:47")
    assert (done["room"], done["amount"]) == ("214", "385.00")
    assert s.pos_to_pms_room_charges("2026-10-11")["status"] == "balanced"
    with pytest.raises(ToolError, match="bill twice"):
        s.post_room_charge_to_folio("50608", "Interface was down at 23:47")
    with pytest.raises(ToolError, match="No POS check"):
        s.post_room_charge_to_folio("99999", "Does not exist at all")


def test_journal_adjustment_clears_card_clearing_and_bank_line():
    s.post_journal_adjustment(
        "2026-10-11", "4900", "1020", "20", "Refund on terminal not in POS", "FNB-1011-014"
    )
    day = s.card_clearing_reconciliation("2026-10-11", "2026-10-11")["days"][0]
    assert (day["journal_adjustments"], day["unexplained_difference"], day["status"]) == (
        "-20.00",
        "0.00",
        "balanced",
    )
    assert "FNB-1011-014" not in {e["bank_ref"] for e in s.bank_exceptions()["exceptions"]}
    with pytest.raises(ToolError, match="not an open bank exception"):
        s.post_journal_adjustment(
            "2026-10-11", "4900", "1020", "20", "Same line a second time", "FNB-1011-014"
        )


def test_annotations_mark_what_needs_approval():
    tools = asyncio.run(s.mcp.list_tools())
    read_only = {t.name for t in tools if t.annotations and t.annotations.read_only_hint}
    assert {"post_journal_adjustment", "post_room_charge_to_folio", "flag_invoice"}.isdisjoint(read_only)
    assert {"card_clearing_reconciliation", "pos_to_pms_room_charges", "hotel_day_statistics"} <= read_only


def test_tool_errors_reach_the_model_with_their_message():
    backend = InProcessBackend()
    result = asyncio.run(backend.call_tool("pos_day_summary", {"day": "not-a-date"}))
    assert result.is_error
    assert "must be a date like 2026-10-11" in result.content[0].text
    ok = asyncio.run(backend.call_tool("bank_exceptions", {}))
    assert json.loads(ok.content[0].text)["count"] == 3
