"""The dashboard document must not lie about spending, income or ambiguity.

Everything here is fabricated: invented merchants, invented amounts, two
accounts built row by row in memory. No fixture file, no real export, nothing
that resolves outside `tmp_path`.

The defects being falsified, in order:

  * DOUBLE COUNTING. A credit-card payment is in the ledger twice -- negative
    in checking, positive on the card. Neither leg is spending; the purchase it
    settles was already counted once, on the card. If either leg leaks into
    spending, every headline number is wrong.
  * FAKE INCOME. A drawdown from the owner's own certificate is his own money
    moving. Counting it as income says he is solvent when he is not.
  * AMBIGUITY SWALLOWED SILENTLY. A self-named ACH credit names no
    counterparty. It belongs in `ambiguous`, in neither total, and visible.
  * OVERCLAIMED RECURRENCE. Two occurrences is not a subscription.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from purser import analytics, cli
from purser.db.database import connect, sync_accounts

#: Two accounts, declared here so this file stands alone. Invented, like everything
#: else in it.
REGISTRY = [
    {"alias": "nfcu-checking", "institution": "Example Credit Union", "type": "checking",
     "currency": "USD", "balance_sign": "asset", "adapter": "nfcu_csv"},
    {"alias": "nfcu-cc", "institution": "Example Credit Union", "type": "credit_card",
     "currency": "USD", "balance_sign": "liability", "adapter": "nfcu_csv"},
]

CHECKING = "nfcu-checking"
CARD = "nfcu-cc"


def _import_log(con, alias: str) -> int:
    """One synthetic import run, so the transactions have a provenance FK."""
    account_id = con.execute(
        "SELECT account_id FROM accounts WHERE alias = ?", [alias]
    ).fetchone()[0]
    con.execute(
        """
        INSERT INTO import_log (account_id, source_path, source_filename, source_sha256,
                                source_bytes, adapter, adapter_version, purser_version,
                                status)
        VALUES (?, 'synthetic', 'synthetic.csv', 'synthetic', 0, 'synthetic', '1', '0', 'ok')
        """,
        [account_id],
    )
    return con.execute("SELECT max(import_id) FROM import_log").fetchone()[0]


def add(con, alias, on_date, amount, description, category=None, source_type=None):
    """Insert one fabricated ledger row, signed the way the adapter would sign it."""
    account_id = con.execute(
        "SELECT account_id FROM accounts WHERE alias = ?", [alias]
    ).fetchone()[0]
    import_id = _import_log(con, alias)
    key = f"{alias}|{on_date}|{amount}|{description}|{import_id}"
    con.execute(
        """
        INSERT INTO transactions (account_id, posted_date, transaction_date, amount,
                                  description, source_type, source_category,
                                  fingerprint, dedupe_key, first_import_id, last_import_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        [account_id, on_date, on_date, Decimal(amount), description,
         source_type, category, key, key, import_id, import_id],
    )


@pytest.fixture
def ledger(con):
    """Two accounts, one of every flow the classifier has to tell apart.

      checking -1000  card payment leg one
      card     +1000  card payment leg two            <- neither leg is spending
      checking  -500  transfer to his own certificate <- internal
      checking +2500  payroll                         <- the only income
      checking +3000  certificate drawdown            <- his own money, not income
      checking  +750  self-named ACH credit           <- ambiguous, in no total
      card       -42  an ordinary purchase            <- spending, counted once
      checking   -80  an ordinary bill                <- spending, counted once
    """
    sync_accounts(con, REGISTRY)
    add(con, CHECKING, "2025-04-09", "-1000.00", "Transfer To Card",
        category="Credit Card Payments", source_type="Transfer")
    add(con, CARD, "2025-04-09", "1000.00", "Payment Thank You",
        category="Payment", source_type="Payment")
    add(con, CHECKING, "2025-04-10", "-500.00", "Transfer To Certificate 12",
        category="Savings", source_type="Transfer")
    add(con, CHECKING, "2025-04-15", "2500.00", "Payroll Deposit Vulcan",
        category="Income", source_type="ACH Credit")
    add(con, CHECKING, "2025-04-16", "3000.00", "Certificate Maturity Payout",
        category="Savings", source_type="Transfer")
    add(con, CHECKING, "2025-04-17", "750.00", "Ach Credit Selfname Holdings",
        category="Other", source_type="ACH Credit")
    add(con, CARD, "2025-04-18", "-42.00", "Bramblewood Coffee - 4417",
        category="Dining", source_type="Purchase")
    add(con, CHECKING, "2025-04-19", "-80.00", "Thornfield Utilities",
        category="Utilities", source_type="ACH Debit")
    return con


def test_the_card_payment_is_excluded_from_spending_on_both_legs(ledger):
    document = analytics.build_document(ledger)
    counts = document["data_quality"]["reconciliation"]

    assert counts["card_payment"] == 2, "both legs must be classified as the payment"

    spending = sum(row["spending"] for row in document["cash_flow"])
    assert spending == 122.00, "only the two real purchases, each counted once"

    categories = {row["category"] for row in document["spend_by_category"]}
    assert "Credit Card Payments" not in categories
    assert "Payment" not in categories


def test_the_internal_transfer_is_in_neither_spending_nor_income(ledger):
    document = analytics.build_document(ledger)
    counts = document["data_quality"]["reconciliation"]

    # the certificate transfer out and the certificate payout back in
    assert counts["internal"] == 2

    assert sum(row["spending"] for row in document["cash_flow"]) == 122.00
    assert sum(row["income"] for row in document["cash_flow"]) == 2500.00


def test_payroll_is_income_and_the_certificate_drawdown_is_not(ledger):
    document = analytics.build_document(ledger)

    assert document["data_quality"]["reconciliation"]["income"] == 1
    assert sum(row["amount"] for row in document["income_detail"]) == 2500.00
    labels = {row["label"] for row in document["income_detail"]}
    assert labels == {"PAYROLL DEPOSIT VULCAN"}


def test_the_unattributable_credit_lands_in_ambiguous_and_in_no_total(ledger):
    document = analytics.build_document(ledger)
    counts = document["data_quality"]["reconciliation"]

    assert counts["ambiguous"] == 1
    assert counts["ambiguous_total_amount"] == 750.00

    # It is held out of both headline figures, not quietly added to either.
    assert sum(row["income"] for row in document["cash_flow"]) == 2500.00
    assert sum(row["spending"] for row in document["cash_flow"]) == 122.00


def test_rows_total_is_exactly_the_sum_of_the_five_buckets(ledger):
    counts = analytics.build_document(ledger)["data_quality"]["reconciliation"]

    assert counts["rows_total"] == 8
    assert counts["rows_total"] == (
        counts["card_payment"] + counts["internal"] + counts["income"]
        + counts["spending"] + counts["ambiguous"]
    )


def test_account_movement_includes_transfers_and_is_never_called_spending(ledger):
    document = analytics.build_document(ledger)
    month = document["cash_flow"][0]

    # -1000 +1000 -500 +2500 +3000 +750 -42 -80
    assert month["account_movement"] == 5628.00
    assert month["spending"] == 122.00


# ---------------------------------------------------------------------------
# Recurrence: two occurrences is not a subscription
# ---------------------------------------------------------------------------

def test_recurrence_confidence_follows_the_evidence(con):
    """Six stable months are high; three varying months are not; two is nothing."""
    sync_accounts(con, REGISTRY)

    for month in range(1, 7):  # stable amount, monthly cadence, six months
        add(con, CARD, f"2025-0{month}-14", "-12.99", "Larkspur Streaming",
            category="Entertainment", source_type="Purchase")

    for month, amount in ((1, "-40.00"), (2, "-95.00"), (3, "-210.00")):  # varying
        add(con, CARD, f"2025-0{month}-03", amount, "Ironvale Hardware",
            category="Home", source_type="Purchase")

    for month in (1, 2):  # two occurrences: not a subscription
        add(con, CARD, f"2025-0{month}-21", "-18.50", "Quillfeather Books",
            category="Shopping", source_type="Purchase")

    by_group = {r["group"]: r for r in analytics.build_document(con)["recurring"]}

    stable = by_group["LARKSPUR STREAMING"]
    assert stable["confidence"] == "high"
    assert stable["occurrences"] == 6 and stable["distinct_months"] == 6
    assert stable["cadence"] == "monthly"
    assert "6 occurrences across 6 distinct months" in stable["evidence"]

    varying = by_group["IRONVALE HARDWARE"]
    assert varying["confidence"] in {"medium", "low"}, "a varying amount is not high"

    assert "QUILLFEATHER BOOKS" not in by_group, "two occurrences is not recurrence"


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------

def test_dashboard_data_prints_the_contract_shape(tmp_path, capsys):
    db = tmp_path / "ledger.duckdb"
    con = connect(db)
    sync_accounts(con, REGISTRY)
    add(con, CARD, "2025-04-18", "-42.00", "Bramblewood Coffee - 4417",
        category="Dining", source_type="Purchase")
    con.close()

    assert cli.main(["--db", str(db), "dashboard-data"]) == 0
    document = json.loads(capsys.readouterr().out)

    assert set(document) == {
        "generated_at", "coverage", "accounts", "snapshot", "cash_flow",
        "income_detail", "spend_by_category", "category_trend", "top_merchants",
        "largest_transactions", "card", "recurring", "leaks", "data_quality",
    }
    assert set(document["data_quality"]["reconciliation"]) == {
        "rows_total", "card_payment", "internal", "income", "spending",
        "ambiguous", "ambiguous_total_amount",
    }
    assert set(document["card"]) >= {
        "balance", "monthly_spend", "top_merchants", "top_categories",
        "interest", "interest_total", "unavailable",
    }
    assert document["top_merchants"][0]["provenance"] == "inferred"
    assert document["top_merchants"][0]["raw_examples"] == ["Bramblewood Coffee - 4417"]
    assert document["spend_by_category"][0]["provenance"] == "institution"


def test_dashboard_data_never_writes_to_the_ledger(tmp_path):
    """The document is a read. The connection is opened read-only to prove it."""
    db = tmp_path / "ledger.duckdb"
    con = connect(db)
    sync_accounts(con, REGISTRY)
    con.close()

    read_only = analytics.open_read_only(db)
    try:
        with pytest.raises(Exception):
            read_only.execute("INSERT INTO accounts (alias, institution, account_type) "
                              "VALUES ('x', 'y', 'checking')")
    finally:
        read_only.close()
