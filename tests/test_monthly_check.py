"""The ongoing reconciliation: derived movement against a stated balance.

This replaces the one-time OFX parity proof, so these tests carry the weight
that `tests/test_balance_check.py` used to: they have to show the check can
actually fail. A reconciliation that always passes is worse than none, because
it is believed.

The arithmetic under test, once per consecutive pair of stated figures:

    derived = (the stated figure immediately before it) + (imported movement since)
    delta   = derived - (this stated figure)

The unit is the interval, not the calendar month: a month holding two stated
figures yields two rows, and both intervals are checked. Skipping one is the
defect these tests exist to prevent, because an import error landing inside a
skipped interval passes while the report still reads healthy.

The two inputs come from different tables and different modules on purpose. If
the derived figure were computed from the same transactions it validates, delta
would be zero by construction.

A zero delta means the interval's **net** movement reconciles. It is not proof
that every transaction was imported exactly once -- offsetting errors cancel
inside a sum -- and one of the tests below pins exactly that, so the stronger
claim cannot quietly return to the docstrings.

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


# --- no interval between two stated figures may go unchecked -----------------

def stated_intervals(results):
    """Every (anchor, stated) pair the check actually reconciled."""
    return [
        (row.anchor_as_of, row.stated_as_of)
        for row in results
        if row.checked
    ]


def test_every_consecutive_pair_of_stated_figures_is_reconciled(checking_ledger):
    """Three figures, two of them in one month. Both intervals must be checked.

    Deciding a month by the last figure in it and anchoring on the most recent
    figure strictly earlier by date leaves the interval *before* the same-month
    figure reconciled by nothing at all: the month reports the short interval,
    the months in between report `not stated`, and the long window vanishes.
    """
    for as_of, amount in (
        ("2025-02-28", "1000.00"),
        ("2025-04-08", "3189.05"),   # 1000.00 + March + April through 04-08
        ("2025-04-30", "3270.40"),
    ):
        stated_balance.record(
            checking_ledger, account_alias="nfcu-checking",
            as_of=as_of, amount=amount,
        )
    results = monthly_check.check_account(
        checking_ledger, account_alias="nfcu-checking")

    assert stated_intervals(results) == [
        (date(2025, 2, 28), date(2025, 4, 8)),
        (date(2025, 4, 8), date(2025, 4, 30)),
    ]
    assert all(row.reconciles for row in results if row.checked)


def test_an_import_error_inside_a_skipped_interval_does_not_pass_silently(checking_ledger):
    """The failure the gap causes, stated as the defect it is.

    March is spanned by the 2025-02-28 -> 2025-04-08 interval and by nothing
    else. Drop a March row and, if that interval is never reconciled, every
    figure still agrees and the whole report reads healthy while the ledger is
    wrong -- exactly the outcome this module exists to prevent.
    """
    for as_of, amount in (
        ("2025-02-28", "1000.00"),
        ("2025-04-08", "3189.05"),
        ("2025-04-30", "3270.40"),
    ):
        stated_balance.record(
            checking_ledger, account_alias="nfcu-checking",
            as_of=as_of, amount=amount,
        )
    # March only: the same synthetic merchant also posts in April, and the
    # April interval must stay clean so the localisation below means something.
    checking_ledger.execute(
        "DELETE FROM transactions WHERE description = 'Quillfeather Books' "
        "AND posted_date < DATE '2025-04-01'"
    )

    results = monthly_check.check_account(
        checking_ledger, account_alias="nfcu-checking")
    broken = [row for row in results if row.status == monthly_check.MISMATCH]

    assert [row.anchor_as_of for row in broken] == [date(2025, 2, 28)]
    assert broken[0].stated_as_of == date(2025, 4, 8)
    assert broken[0].delta == Decimal("18.99")   # the missing debit, un-subtracted
    # The interval that does not contain the dropped row still reconciles, so
    # the break is localised rather than smeared over everything after it.
    assert by_month(
        [row for row in results if row.stated_as_of == date(2025, 4, 30)]
    )["2025-04"].status == monthly_check.OK


def test_two_stated_figures_in_one_month_produce_two_rows(checking_ledger):
    """The interval is the unit of the check; the month only groups the report."""
    for as_of, amount in (
        ("2025-03-31", "1855.03"),
        ("2025-04-08", "3189.05"),
        ("2025-04-30", "3270.40"),
    ):
        stated_balance.record(
            checking_ledger, account_alias="nfcu-checking",
            as_of=as_of, amount=amount,
        )
    results = monthly_check.check_account(
        checking_ledger, account_alias="nfcu-checking")

    april = [row for row in results if row.month == "2025-04"]
    assert [row.stated_as_of for row in april] == [date(2025, 4, 8), date(2025, 4, 30)]
    assert all(row.status == monthly_check.OK for row in april)


def test_a_month_holding_no_figure_at_all_still_reports_not_stated(checking_ledger):
    """Reconciling every pair must not absorb an unchecked month into silence."""
    for as_of, amount in (
        ("2025-02-28", "1000.00"),
        ("2025-04-08", "3189.05"),
        ("2025-04-30", "3270.40"),
    ):
        stated_balance.record(
            checking_ledger, account_alias="nfcu-checking",
            as_of=as_of, amount=amount,
        )
    results = monthly_check.check_account(
        checking_ledger, account_alias="nfcu-checking")

    march = [row for row in results if row.month == "2025-03"]
    assert [row.status for row in march] == [monthly_check.NOT_STATED]
    assert not march[0].checked
    # ...and the rows stay in interval order across the whole report.
    assert [row.month for row in results] == [
        "2025-02", "2025-03", "2025-04", "2025-04"
    ]


def test_the_cli_shows_every_interval_it_reconciled(configured, capsys):
    """An unchecked interval must not be invisible in the output either.

    Two figures inside March, so the report has to print two rows for one
    month and say which window each of them reconciled.
    """
    cli.main(["ingest", "--account", "nfcu-checking"])
    for as_of, amount in (
        ("2025-02-28", "1000.00"),
        ("2025-03-14", "3354.50"),   # 1000.00 + March movement posted through 03-14
        ("2025-03-31", "1855.03"),
    ):
        cli.main(["record-balance", "--account", "nfcu-checking",
                  "--as-of", as_of, "--amount", amount])
    capsys.readouterr()

    assert cli.main(["monthly-check", "--account", "nfcu-checking"]) == 0
    out = capsys.readouterr().out
    assert "2025-02-28 -> 2025-03-14" in out
    assert "2025-03-14 -> 2025-03-31" in out
    assert out.count("nfcu-checking 2025-03  OK") == 2


# --- what a zero delta actually proves ---------------------------------------

def test_a_zero_delta_proves_the_net_reconciles_not_that_every_row_landed(checking_ledger):
    """The claim this check is allowed to make, pinned by its own counterexample.

    The check compares two sums, and offsetting errors cancel inside a sum. A
    dropped row and an equal-value duplicate net to zero, so the delta is zero
    and the interval reconciles -- while the ledger holds neither the right
    rows nor the right count of them. Transaction-level completeness would need
    a mechanism that compares rows rather than totals; there is deliberately
    none here, and the docstrings must not claim otherwise.
    """
    stated_balance.record(
        checking_ledger, account_alias="nfcu-checking", as_of="2025-03-31",
        amount="1855.03",
    )
    stated_balance.record(
        checking_ledger, account_alias="nfcu-checking", as_of="2025-04-30",
        amount="3270.40",
    )
    before = checking_ledger.execute(
        "SELECT COUNT(*) FROM transactions WHERE posted_date >= DATE '2025-04-01'"
    ).fetchone()[0]

    # One April debit dropped, and a different April debit of the same value
    # duplicated. Net movement is unchanged; the rows are wrong either way.
    dropped, duplicated = checking_ledger.execute(
        "SELECT description, amount FROM transactions "
        "WHERE posted_date >= DATE '2025-04-01' AND amount < 0 "
        "GROUP BY description, amount HAVING COUNT(*) = 1 "
        "ORDER BY description LIMIT 2"
    ).fetchall()[:2]
    assert dropped[1] != duplicated[1]        # different values, so scale one
    checking_ledger.execute(
        "DELETE FROM transactions WHERE description = ?", [dropped[0]]
    )
    checking_ledger.execute(
        "UPDATE transactions SET amount = amount + ? WHERE description = ?",
        [dropped[1], duplicated[0]],
    )

    april = [row for row in monthly_check.check_account(
        checking_ledger, account_alias="nfcu-checking") if row.checked][-1]
    assert april.delta == Decimal("0.00")
    assert april.status == monthly_check.OK    # the net reconciles...
    after = checking_ledger.execute(
        "SELECT COUNT(*) FROM transactions WHERE posted_date >= DATE '2025-04-01'"
    ).fetchone()[0]
    assert after != before                     # ...and the rows are still wrong.


def test_no_docstring_claims_transaction_level_completeness():
    """The overclaim is a correctness defect in its own right: it is believed.

    'Every transaction imported exactly once' is what a reader will act on when
    a delta is zero -- stopping the search for the offsetting pair that a net
    check cannot see. Pin the wording so the stronger claim cannot come back.
    """
    prose = "\n".join(
        (REPO_ROOT / "src" / "purser" / rel).read_text(encoding="utf-8")
        for rel in ("core/monthly_check.py", "cli.py")
    )
    assert "imported exactly once, with the right sign" not in prose
    assert "net" in monthly_check.__doc__
    assert "not** proof that every transaction was imported exactly once" in (
        monthly_check.__doc__
    )
