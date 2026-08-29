"""The ongoing reconciliation: derived movement against a stated balance.

This replaces the one-time OFX parity proof, so these tests carry the weight
that `tests/test_balance_check.py` used to: they have to show the check can
actually fail. A reconciliation that always passes is worse than none, because
it is believed.

The arithmetic under test, per month:

    derived = (nearest earlier stated figure) + (imported movement since it)
    delta   = derived - (this month's stated figure)

The two inputs come from different tables and different modules on purpose. If
the derived figure were computed from the same transactions it validates, delta
would be zero by construction.

All figures below come from the synthetic fixtures in `tests/fixtures/`:
the checking sample nets +855.03 over March 2025, and the two overlapping
April windows together net +1415.37. Nothing here is a real balance.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from purser import cli
from purser.core import monthly_check, paths, stated_balance
from purser.core.importer import import_file

REPO_ROOT = Path(__file__).resolve().parents[1]

MARCH_NET = Decimal("855.03")     # tests/fixtures/nfcu-checking-sample.csv
APRIL_NET = Decimal("1415.37")    # window-a + window-b, deduped
CARD_NET = Decimal("869.20")      # tests/fixtures/nfcu-cc-sample.csv


def by_month(results):
    return {row.month: row for row in results}


@pytest.fixture
def checking_ledger(con, checking_csv, window_a_csv, window_b_csv):
    """March and April 2025 imported, from three overlapping synthetic exports."""
    for path in (checking_csv, window_a_csv, window_b_csv):
        import_file(con, path, account_alias="nfcu-checking")
    return con


# --- the fixture arithmetic, stated once so the rest can lean on it ----------

def test_the_fixture_nets_are_what_the_rest_of_this_file_assumes(checking_ledger):
    """If a fixture changes, this fails first and says so, instead of everything."""
    march, april = checking_ledger.execute(
        "SELECT "
        " SUM(CASE WHEN posted_date < DATE '2025-04-01' THEN amount ELSE 0.00 END), "
        " SUM(CASE WHEN posted_date >= DATE '2025-04-01' THEN amount ELSE 0.00 END) "
        "FROM transactions"
    ).fetchone()
    assert Decimal(str(march)) == MARCH_NET
    assert Decimal(str(april)) == APRIL_NET


# --- the three non-verdicts --------------------------------------------------

def test_a_month_with_no_stated_figure_is_reported_as_not_stated(checking_ledger):
    """Not a pass and not a failure. An unchecked month must look unchecked."""
    stated_balance.record(
        checking_ledger, account_alias="nfcu-checking", as_of="2025-02-28",
        amount="1000.00",
    )
    stated_balance.record(
        checking_ledger, account_alias="nfcu-checking", as_of="2025-04-30",
        amount=str(Decimal("1000.00") + MARCH_NET + APRIL_NET),
    )
    months = by_month(monthly_check.check_account(
        checking_ledger, account_alias="nfcu-checking"))

    assert sorted(months) == ["2025-02", "2025-03", "2025-04"]
    assert months["2025-03"].status == monthly_check.NOT_STATED
    assert not months["2025-03"].checked
    assert not months["2025-03"].reconciles
    assert months["2025-03"].delta is None


def test_the_earliest_stated_figure_is_the_anchor_not_a_pass(checking_ledger):
    """Checking the baseline against itself is the circularity to be avoided."""
    stated_balance.record(
        checking_ledger, account_alias="nfcu-checking", as_of="2025-02-28",
        amount="1000.00",
    )
    months = by_month(monthly_check.check_account(
        checking_ledger, account_alias="nfcu-checking"))

    anchor = months["2025-02"]
    assert anchor.status == monthly_check.ANCHOR
    assert not anchor.checked
    assert anchor.stated == Decimal("1000.00")
    assert anchor.derived is None and anchor.delta is None


def test_an_account_with_neither_ledger_nor_stated_figure_reports_nothing(con):
    assert monthly_check.check_account(con, account_alias="nfcu-checking") == []


# --- the verdict that matters ------------------------------------------------

def test_a_correct_ledger_reconciles_against_the_stated_figure(checking_ledger):
    stated_balance.record(
        checking_ledger, account_alias="nfcu-checking", as_of="2025-03-31",
        amount="1855.03", note="March statement p1",
    )
    stated_balance.record(
        checking_ledger, account_alias="nfcu-checking", as_of="2025-04-30",
        amount=str(Decimal("1855.03") + APRIL_NET), note="April statement p1",
    )
    months = by_month(monthly_check.check_account(
        checking_ledger, account_alias="nfcu-checking"))

    april = months["2025-04"]
    assert april.status == monthly_check.OK
    assert april.reconciles
    assert april.anchor_as_of == date(2025, 3, 31)
    assert april.anchor_amount == Decimal("1855.03")
    assert april.net_movement == APRIL_NET
    assert april.derived == Decimal("3270.40")
    assert april.delta == Decimal("0.00")
    assert april.note == "April statement p1"


def test_a_dropped_row_is_caught(checking_ledger):
    """The whole reason the check exists: a bad import must not pass."""
    stated_balance.record(
        checking_ledger, account_alias="nfcu-checking", as_of="2025-03-31",
        amount="1855.03",
    )
    stated_balance.record(
        checking_ledger, account_alias="nfcu-checking", as_of="2025-04-30",
        amount=str(Decimal("1855.03") + APRIL_NET),
    )
    checking_ledger.execute(
        "DELETE FROM transactions WHERE description = 'Ironvale Hardware'"
    )

    april = by_month(monthly_check.check_account(
        checking_ledger, account_alias="nfcu-checking"))["2025-04"]
    assert april.status == monthly_check.MISMATCH
    assert not april.reconciles
    assert april.checked                      # it was compared, and it failed
    assert april.delta == Decimal("120.00")   # the missing debit, un-subtracted


def test_a_duplicated_row_is_caught(checking_ledger):
    stated_balance.record(
        checking_ledger, account_alias="nfcu-checking", as_of="2025-03-31",
        amount="1855.03",
    )
    stated_balance.record(
        checking_ledger, account_alias="nfcu-checking", as_of="2025-04-30",
        amount=str(Decimal("1855.03") + APRIL_NET),
    )
    checking_ledger.execute(
        "UPDATE transactions SET amount = amount * 2 "
        "WHERE description = 'Cinderhale Refund'"
    )
    april = by_month(monthly_check.check_account(
        checking_ledger, account_alias="nfcu-checking"))["2025-04"]
    assert april.status == monthly_check.MISMATCH
    assert april.delta == Decimal("250.00")


def test_a_mistyped_stated_figure_is_caught_too(checking_ledger):
    """The check compares two independent things; either side can be the wrong one."""
    stated_balance.record(
        checking_ledger, account_alias="nfcu-checking", as_of="2025-03-31",
        amount="1855.03",
    )
    stated_balance.record(
        checking_ledger, account_alias="nfcu-checking", as_of="2025-04-30",
        amount="3270.04",     # transposed from 3270.40
    )
    april = by_month(monthly_check.check_account(
        checking_ledger, account_alias="nfcu-checking"))["2025-04"]
    assert april.status == monthly_check.MISMATCH
    assert april.delta == Decimal("0.36")


def test_correcting_the_stated_figure_makes_the_month_reconcile(checking_ledger):
    """Idempotent re-entry and the check, working together as they will in use."""
    stated_balance.record(
        checking_ledger, account_alias="nfcu-checking", as_of="2025-03-31",
        amount="1855.03",
    )
    stated_balance.record(
        checking_ledger, account_alias="nfcu-checking", as_of="2025-04-30",
        amount="3270.04",
    )
    assert by_month(monthly_check.check_account(
        checking_ledger, account_alias="nfcu-checking"))["2025-04"].status == (
        monthly_check.MISMATCH)

    stated_balance.record(
        checking_ledger, account_alias="nfcu-checking", as_of="2025-04-30",
        amount="3270.40", note="re-read the statement",
    )
    april = by_month(monthly_check.check_account(
        checking_ledger, account_alias="nfcu-checking"))["2025-04"]
    assert april.status == monthly_check.OK
    assert april.note == "re-read the statement"


# --- the boundary of an inclusive as_of --------------------------------------

def test_the_anchor_day_is_not_counted_twice(checking_ledger):
    """`as_of` is inclusive, so the anchor already contains its own day.

    Anchoring on 2025-04-08 -- a day holding two identical transit rows -- and
    counting them again would be a silent one-day double count in every month
    that follows.
    """
    stated_balance.record(
        checking_ledger, account_alias="nfcu-checking", as_of="2025-04-08",
        amount="3189.05",   # 1855.03 + April movement posted through 04-08
    )
    stated_balance.record(
        checking_ledger, account_alias="nfcu-checking", as_of="2025-04-30",
        amount="3270.40",   # + 04-10, 04-12, 04-14 debits and the 04-15 credit
    )
    april = by_month(monthly_check.check_account(
        checking_ledger, account_alias="nfcu-checking"))["2025-04"]
    # Strictly after 04-08. Counting that day's two identical transit rows again
    # would make this 67.85 and the month would not reconcile.
    assert april.net_movement == Decimal("81.35")
    assert april.status == monthly_check.OK


# --- a liability reconciles with the same arithmetic -------------------------

def test_a_credit_card_reconciles_with_the_same_arithmetic(con, cc_csv):
    """No per-type branch. The sign was resolved when the figures were recorded.

    The captain enters what the statement says -- an amount owed, positive --
    for both months. Purser stores both negative, adds the same imported
    movement it would add for a checking account, and the delta is zero.
    """
    import_file(con, cc_csv, account_alias="nfcu-cc")
    stated_balance.record(
        con, account_alias="nfcu-cc", as_of="2025-03-31", amount="1200.00",
        note="March card statement",
    )
    stated_balance.record(
        con, account_alias="nfcu-cc", as_of="2025-04-30",
        amount=str(Decimal("1200.00") - CARD_NET), note="April card statement",
    )
    april = by_month(monthly_check.check_account(con, account_alias="nfcu-cc"))["2025-04"]

    assert april.balance_sign == "liability"
    assert april.anchor_amount == Decimal("-1200.00")
    assert april.net_movement == CARD_NET
    assert april.derived == Decimal("-330.80")
    assert april.delta == Decimal("0.00")
    assert april.status == monthly_check.OK
    # ...and shown back the way the statement says it: 330.80 owed.
    assert april.shown(april.stated) == Decimal("330.80")


def test_a_bad_card_import_is_caught_the_same_way(con, cc_csv):
    import_file(con, cc_csv, account_alias="nfcu-cc")
    stated_balance.record(
        con, account_alias="nfcu-cc", as_of="2025-03-31", amount="1200.00"
    )
    stated_balance.record(
        con, account_alias="nfcu-cc", as_of="2025-04-30",
        amount=str(Decimal("1200.00") - CARD_NET),
    )
    con.execute("DELETE FROM transactions WHERE description = 'Annual Membership'")

    april = by_month(monthly_check.check_account(con, account_alias="nfcu-cc"))["2025-04"]
    assert april.status == monthly_check.MISMATCH
    assert april.delta == Decimal("29.00")


# --- the separation that keeps the check non-circular ------------------------

def test_the_stated_series_never_reads_the_ledger(con, checking_csv):
    """The independent input must stay independent, structurally, not by habit."""
    import_file(con, checking_csv, account_alias="nfcu-checking")
    account_id = con.execute(
        "SELECT account_id FROM accounts WHERE alias = 'nfcu-checking'"
    ).fetchone()[0]
    assert stated_balance.series(con, account_id) == []

    stated_balance.record(
        con, account_alias="nfcu-checking", as_of="2025-03-31", amount="1855.03"
    )
    assert [row[1] for row in stated_balance.series(con, account_id)] == [
        Decimal("1855.03")
    ]


def test_a_stated_figure_is_ignored_by_the_derived_side(checking_ledger):
    """Recording an extra balance must not move the derived figure.

    If the derived side could see the balances table, the check would be
    comparing a number against itself and could never fail.
    """
    stated_balance.record(
        checking_ledger, account_alias="nfcu-checking", as_of="2025-03-31",
        amount="1855.03",
    )
    stated_balance.record(
        checking_ledger, account_alias="nfcu-checking", as_of="2025-04-30",
        amount="3270.40",
    )
    before = by_month(monthly_check.check_account(
        checking_ledger, account_alias="nfcu-checking"))["2025-04"].net_movement

    # A balance recorded under a different type/kind, as balance_check writes.
    checking_ledger.execute(
        "INSERT INTO balances (account_id, as_of, balance_type, source_kind, amount) "
        "SELECT account_id, TIMESTAMPTZ '2025-04-20 00:00:00+00', 'derived', 'file', "
        "999999.00 FROM accounts WHERE alias = 'nfcu-checking'"
    )
    after = by_month(monthly_check.check_account(
        checking_ledger, account_alias="nfcu-checking"))["2025-04"]
    assert after.net_movement == before
    assert after.status == monthly_check.OK


def test_only_stated_figures_are_treated_as_the_independent_input(checking_ledger):
    """A `file` or `derived` balance must never stand in for a stated one."""
    checking_ledger.execute(
        "INSERT INTO balances (account_id, as_of, balance_type, source_kind, amount) "
        "SELECT account_id, TIMESTAMPTZ '2025-03-31 00:00:00+00', 'ledger', 'file', "
        "1855.03 FROM accounts WHERE alias = 'nfcu-checking'"
    )
    months = by_month(monthly_check.check_account(
        checking_ledger, account_alias="nfcu-checking"))
    assert months["2025-03"].status == monthly_check.NOT_STATED


# --- across accounts and through the command line ----------------------------

def test_check_all_covers_every_declared_account(con, checking_csv, cc_csv):
    import_file(con, checking_csv, account_alias="nfcu-checking")
    import_file(con, cc_csv, account_alias="nfcu-cc")
    aliases = {row.account_alias for row in monthly_check.check_all(con)}
    assert aliases == {"nfcu-checking", "nfcu-cc"}


@pytest.fixture
def configured(private_home, monkeypatch, checking_csv, cc_csv):
    """A private home with two accounts and one synthetic export each."""
    registry = paths.config_home() / "accounts.yaml"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        yaml.safe_dump({"version": 1, "accounts": [
            {"alias": "nfcu-checking", "institution": "Example Credit Union",
             "type": "checking", "currency": "USD", "balance_sign": "asset",
             "adapter": "nfcu_csv"},
            {"alias": "nfcu-cc", "institution": "Example Credit Union",
             "type": "credit_card", "currency": "USD", "balance_sign": "liability",
             "adapter": "nfcu_csv"},
        ]}),
        encoding="utf-8",
    )
    for alias, source in (("nfcu-checking", checking_csv), ("nfcu-cc", cc_csv)):
        landing = paths.raw_root() / alias
        landing.mkdir(parents=True)
        (landing / "2025-04-30_sample.csv").write_bytes(source.read_bytes())
    monkeypatch.chdir(REPO_ROOT)
    return private_home


def test_cli_monthly_check_passes_and_marks_unstated_months(configured, capsys):
    cli.main(["ingest", "--account", "nfcu-checking"])
    cli.main(["record-balance", "--account", "nfcu-checking",
              "--as-of", "2025-02-28", "--amount", "1000.00"])
    cli.main(["record-balance", "--account", "nfcu-checking",
              "--as-of", "2025-03-31", "--amount", "1855.03"])
    capsys.readouterr()

    assert cli.main(["monthly-check", "--account", "nfcu-checking"]) == 0
    out = capsys.readouterr().out
    assert "nfcu-checking 2025-02  anchor" in out
    assert "nfcu-checking 2025-03  OK" in out
    assert "delta 0.00" in out


def test_cli_monthly_check_exits_nonzero_on_a_mismatch(configured, capsys):
    cli.main(["ingest", "--account", "nfcu-checking"])
    cli.main(["record-balance", "--account", "nfcu-checking",
              "--as-of", "2025-02-28", "--amount", "1000.00"])
    cli.main(["record-balance", "--account", "nfcu-checking",
              "--as-of", "2025-03-31", "--amount", "1855.04"])
    capsys.readouterr()

    assert cli.main(["monthly-check", "--account", "nfcu-checking"]) == 1
    assert "MISMATCH" in capsys.readouterr().out


def test_cli_monthly_check_shows_a_card_in_statement_terms(configured, capsys):
    cli.main(["ingest", "--account", "nfcu-cc"])
    cli.main(["record-balance", "--account", "nfcu-cc",
              "--as-of", "2025-03-31", "--amount", "1200.00"])
    cli.main(["record-balance", "--account", "nfcu-cc",
              "--as-of", "2025-04-30", "--amount", "330.80"])
    capsys.readouterr()

    assert cli.main(["monthly-check", "--account", "nfcu-cc"]) == 0
    out = capsys.readouterr().out
    assert "nfcu-cc 2025-04  OK" in out
    assert "stated 330.80" in out       # what the statement says, not -330.80


def test_cli_monthly_check_says_so_when_there_is_nothing_to_check(configured, capsys):
    assert cli.main(["monthly-check", "--account", "nfcu-checking"]) == 0
    assert "nothing to check" in capsys.readouterr().out
