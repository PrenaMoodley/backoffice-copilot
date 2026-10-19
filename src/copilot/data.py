"""Demo back-office database for a fictional hotel.

The data model mirrors the other projects in this portfolio: POS takings
(pos-to-ledger), card settlements and bank reconciliation (recon-dashboard),
supplier invoices filed from email (m365-mail-intake) and ad spend
(marketing-spend-sync). The PMS tables are shaped like a hotel PMS such as
Opera (daily room statistics, charges received from the POS interface) and the
room-charge checks like a POS such as Simphony. Everything is fictional and
seeded with a few deliberate problems for the copilot to find.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

HOTEL_NAME = "Tidewater Hotel"
ROOMS_AVAILABLE = 120

# The planted interface failure: a pool bar check charged to room 214 just before
# midnight on 11 October never reached the PMS, so the guest won't be billed for it.
MISSED_CHECK = ("2026-10-11", "50608", "Pool Bar", "214", "385.00", "23:47")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (number TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pos_sales (
    day TEXT NOT NULL, outlet TEXT NOT NULL, gross_incl_vat TEXT NOT NULL, PRIMARY KEY (day, outlet));
CREATE TABLE IF NOT EXISTS pos_tenders (
    day TEXT NOT NULL, tender TEXT NOT NULL, amount TEXT NOT NULL, PRIMARY KEY (day, tender));
CREATE TABLE IF NOT EXISTS card_settlements (
    day TEXT PRIMARY KEY, settled TEXT NOT NULL, fees TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bank_exceptions (
    bank_ref TEXT PRIMARY KEY, day TEXT NOT NULL, description TEXT NOT NULL, amount TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', note TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS supplier_invoices (
    id INTEGER PRIMARY KEY, received TEXT NOT NULL, supplier TEXT NOT NULL, invoice_no TEXT NOT NULL,
    amount_incl_vat TEXT NOT NULL, due TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'filed',
    flag_reason TEXT NOT NULL DEFAULT '', file_path TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ad_spend (
    day TEXT NOT NULL, platform TEXT NOT NULL, spend TEXT NOT NULL, PRIMARY KEY (day, platform));
CREATE TABLE IF NOT EXISTS ad_budgets (
    month TEXT NOT NULL, platform TEXT NOT NULL, budget TEXT NOT NULL, PRIMARY KEY (month, platform));
CREATE TABLE IF NOT EXISTS journal_adjustments (
    id INTEGER PRIMARY KEY, day TEXT NOT NULL, debit_account TEXT NOT NULL, credit_account TEXT NOT NULL,
    amount TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS pms_daily (
    day TEXT PRIMARY KEY, rooms_available INTEGER NOT NULL, rooms_sold INTEGER NOT NULL,
    room_revenue_excl_vat TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pos_room_charges (
    check_no TEXT PRIMARY KEY, day TEXT NOT NULL, revenue_centre TEXT NOT NULL, room TEXT NOT NULL,
    amount TEXT NOT NULL, closed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pms_interface_postings (
    id INTEGER PRIMARY KEY, day TEXT NOT NULL, check_no TEXT NOT NULL UNIQUE, room TEXT NOT NULL,
    amount TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'POS interface');
"""

ACCOUNTS = [
    ("1010", "Cash on hand", "Asset"),
    ("1020", "Card clearing", "Asset"),
    ("1100", "Guest ledger", "Asset"),
    ("2200", "VAT output", "Liability"),
    ("4100", "Restaurant sales", "Income"),
    ("4200", "Pool bar sales", "Income"),
    ("4300", "Room service sales", "Income"),
    ("4900", "Sales refunds", "Income"),
    ("6150", "Card fees", "Expense"),
    ("6900", "Cash over/short", "Expense"),
]


def _days(start: date, n: int) -> list[str]:
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


def _room_charge_checks(day_index: int, day: str, total: str) -> list[tuple]:
    """Split a day's room-charge tender into individual checks that add up to it exactly."""
    planted = [MISSED_CHECK] if day == MISSED_CHECK[0] else []
    remaining = Decimal(total) - sum((Decimal(c[4]) for c in planted), Decimal("0"))
    weights = [7, 11, 9, 14, 6, 12, 10]
    centres = ["Restaurant", "Pool Bar", "Room Service"]
    checks = []
    for i, w in enumerate(weights):
        amount = (
            (remaining * w / sum(weights)).quantize(Decimal("0.01"))
            if i < len(weights) - 1
            else remaining - sum((Decimal(c[4]) for c in checks), Decimal("0"))
        )
        room = f"{1 + (day_index + i) % 3}{(day_index * 5 + i * 7) % 30 + 1:02d}"
        closed = f"{12 + i * 1 + (day_index % 3)}:{(i * 13 + day_index * 7) % 60:02d}"
        checks.append(
            (
                day,
                f"{50000 + day_index * 100 + i + 1}",
                centres[(i + day_index) % 3],
                room,
                str(amount),
                closed,
            )
        )
    return checks + planted


def seed(path: Path) -> Path:
    """Create a fresh demo database. Safe to re-run: it replaces the file."""
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO accounts VALUES (?,?,?)", ACCOUNTS)

    week = _days(date(2026, 10, 5), 7)  # Mon 5 Oct .. Sun 11 Oct 2026
    base = [
        # (restaurant, pool bar, room service, cash, card, room charge)
        ("21500.00", "9100.00", "2400.00", "3900.00", "22300.00", "6800.00"),
        ("19850.00", "8200.00", "2150.00", "3550.00", "20650.00", "6000.00"),
        ("20400.00", "8650.00", "2300.00", "3700.00", "21150.00", "6500.00"),
        ("22900.00", "9800.00", "2550.00", "4100.00", "23600.00", "7550.00"),
        ("27300.00", "13250.00", "3100.00", "5200.00", "29350.00", "9100.00"),
        ("31200.00", "15400.00", "3600.00", "6050.00", "33600.00", "10550.00"),
        ("25320.50", "12960.00", "2640.00", "4120.00", "27845.50", "8955.00"),
    ]
    occupancy = [(78, 1850), (74, 1820), (80, 1840), (86, 1880), (104, 2150), (112, 2290), (91, 1990)]
    fee_rate = 0.0188
    for i, (day, (rest, pool, room, cash, card, charge)) in enumerate(zip(week, base, strict=True)):
        sold, rate = occupancy[i]
        con.execute(
            "INSERT INTO pms_daily VALUES (?,?,?,?)", (day, ROOMS_AVAILABLE, sold, f"{sold * rate}.00")
        )
        checks = _room_charge_checks(i, day, charge)
        con.executemany(
            "INSERT INTO pos_room_charges (day, check_no, revenue_centre, room, amount, closed_at) "
            "VALUES (?,?,?,?,?,?)",
            checks,
        )
        con.executemany(
            "INSERT INTO pms_interface_postings (day, check_no, room, amount) VALUES (?,?,?,?)",
            [(c[0], c[1], c[3], c[4]) for c in checks if c != MISSED_CHECK],
        )
        con.executemany(
            "INSERT INTO pos_sales VALUES (?,?,?)",
            [(day, "Restaurant", rest), (day, "Pool Bar", pool), (day, "Room Service", room)],
        )
        con.executemany(
            "INSERT INTO pos_tenders VALUES (?,?,?)",
            [(day, "Cash", cash), (day, "Card", card), (day, "Room Charge", charge)],
        )
        fees = round(float(card) * fee_rate, 2)
        settled = round(float(card) - fees, 2)
        if day == "2026-10-11":
            settled = round(settled - 20.00, 2)  # a R20 refund done on the card terminal, not in the POS
        con.execute("INSERT INTO card_settlements VALUES (?,?,?)", (day, f"{settled:.2f}", f"{fees:.2f}"))

    con.executemany(
        "INSERT INTO bank_exceptions (bank_ref, day, description, amount, note) VALUES (?,?,?,?,?)",
        [
            ("FNB-1011-014", "2026-10-11", "CARD REFUND TERMINAL 2 POOL BAR", "-20.00",
             "Refund on the card terminal; no matching refund in the POS"),
            ("FNB-1009-007", "2026-10-09", "CREDIT EFT PAYMENT", "2300.00",
             "Ambiguous: two open invoices for R2,300.00"),
            ("FNB-1010-011", "2026-10-10", "CREDIT UNKNOWN DEPOSIT", "640.00", "No open invoice fits"),
        ],
    )  # fmt: skip

    con.executemany(
        "INSERT INTO supplier_invoices (received, supplier, invoice_no, amount_incl_vat, due, file_path) "
        "VALUES (?,?,?,?,?,?)",
        [
            ("2026-10-05", "Coastal Linen Services", "CLS-20931", "8740.00", "2026-11-04", "Invoices/2026/10/coastallinen.example/2026-10-05_CLS-20931.pdf"),
            ("2026-10-06", "Berea Fresh Produce", "BFP-7712", "12385.60", "2026-10-20", "Invoices/2026/10/bereafresh.example/2026-10-06_BFP-7712.pdf"),
            ("2026-10-07", "Umhlanga Pool Care", "UPC-1180", "3450.00", "2026-11-06", "Invoices/2026/10/umhlangapool.example/2026-10-07_UPC-1180.pdf"),
            ("2026-10-08", "Coastal Linen Services", "CLS-20958", "8740.00", "2026-11-07", "Invoices/2026/10/coastallinen.example/2026-10-08_CLS-20958.pdf"),
            ("2026-10-09", "KZN Beverage Distributors", "KBD-55102", "19960.25", "2026-10-23", "Invoices/2026/10/kznbev.example/2026-10-09_KBD-55102.pdf"),
            ("2026-10-12", "Berea Fresh Produce", "BFP-7790", "11902.10", "2026-10-26", "Invoices/2026/10/bereafresh.example/2026-10-12_BFP-7790.pdf"),
        ],
    )  # fmt: skip

    october = _days(date(2026, 10, 1), 11)  # spend loaded up to yesterday (11 Oct)
    for i, day in enumerate(october):
        con.execute("INSERT INTO ad_spend VALUES (?,?,?)", (day, "meta", f"{340 + (i % 3) * 25:.2f}"))
        con.execute("INSERT INTO ad_spend VALUES (?,?,?)", (day, "google_ads", f"{560 + i * 12:.2f}"))
    con.executemany(
        "INSERT INTO ad_budgets VALUES (?,?,?)",
        [("2026-10", "meta", "12000.00"), ("2026-10", "google_ads", "18000.00")],
    )
    con.commit()
    con.close()
    return path
