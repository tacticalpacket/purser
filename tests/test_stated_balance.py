"""Recording the one figure purser does not compute.

NFCU's CSV carries no balance and no new OFX will ever arrive, so the
authoritative ending balance is typed in by hand. These tests pin what that
entry has to guarantee: it corrects rather than accumulates, it normalizes the
sign once at the boundary, and it refuses a figure it would have to round.

Everything here runs against synthetic fixtures. No real balance, from any real
account, appears in this repository.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from purser import cli
from purser.core import paths, stated_balance
from purser.core.stated_balance import StatedBalanceError

REPO_ROOT = Path(__file__).resolve().parents[1]


def stated_rows(con):
    return con.execute(
        "SELECT account_id, CAST(as_of AS DATE), amount, note, balance_type "
        "FROM balances WHERE source_kind = 'stated' ORDER BY as_of"
    ).fetchall()


# --- what gets stored --------------------------------------------------------

def test_a_stated_balance_is_recorded_as_stated_not_as_a_file_figure(con):
    """`source_kind` is what keeps a typed figure from posing as a derived one."""
    stated_balance.record(
        con, account_alias="nfcu-checking", as_of="2025-03-31", amount="1855.03"
    )
    kind, btype = con.execute(
        "SELECT source_kind, balance_type FROM balances"
    ).fetchone()
    assert kind == "stated"
    assert btype == "ledger"  # never 'available': that nets pending holds


def test_the_amount_is_stored_as_decimal_not_as_a_float(con):
    stated_balance.record(
        con, account_alias="nfcu-checking", as_of="2025-03-31", amount="1855.03"
    )
    amount = con.execute("SELECT amount FROM balances").fetchone()[0]
    assert isinstance(amount, Decimal)
    assert amount == Decimal("1855.03")


def test_the_as_of_day_is_stored_canonically_in_utc(con):
    """A hand-entered balance is a fact about a day, encoded the same way anywhere."""
    recorded = stated_balance.record(
        con, account_alias="nfcu-checking", as_of="2025-03-31", amount="10.00"
    )
    assert recorded.as_of == date(2025, 3, 31)
    # Read back as text: fetching a TIMESTAMPTZ into Python needs pytz, and a
    # dependency is not worth adding to assert a string.
    stamp = con.execute("SELECT CAST(as_of AS VARCHAR) FROM balances").fetchone()[0]
    assert stamp.startswith("2025-03-31 00:00:00")


def test_a_note_is_kept_and_is_optional(con):
    stated_balance.record(
        con, account_alias="nfcu-checking", as_of="2025-03-31", amount="10.00",
        note="March statement p1",
    )
    stated_balance.record(
        con, account_alias="nfcu-checking", as_of="2025-04-30", amount="20.00"
    )
    notes = [row[3] for row in stated_rows(con)]
    assert notes == ["March statement p1", None]


# --- idempotency -------------------------------------------------------------

def test_re_entering_the_same_day_corrects_rather_than_duplicating(con):
    """Typing a balance twice is how a typo gets fixed, not how a rival row appears."""
    first = stated_balance.record(
        con, account_alias="nfcu-checking", as_of="2025-03-31", amount="1855.03",
        note="typo",
    )
    second = stated_balance.record(
        con, account_alias="nfcu-checking", as_of="2025-03-31", amount="1855.30",
        note="March statement p1",
    )

    assert not first.corrected
    assert second.corrected
    assert second.previous_amount == Decimal("1855.03")

    rows = stated_rows(con)
    assert len(rows) == 1
    assert rows[0][2] == Decimal("1855.30")
    assert rows[0][3] == "March statement p1"


def test_recording_the_identical_figure_twice_changes_nothing(con):
    for _ in range(3):
        stated_balance.record(
            con, account_alias="nfcu-checking", as_of="2025-03-31", amount="1855.03"
        )
    rows = stated_rows(con)
    assert len(rows) == 1
    assert rows[0][2] == Decimal("1855.03")


def test_a_different_day_is_a_different_figure(con):
    stated_balance.record(
        con, account_alias="nfcu-checking", as_of="2025-03-31", amount="1855.03"
    )
    stated_balance.record(
        con, account_alias="nfcu-checking", as_of="2025-04-30", amount="3270.40"
    )
    assert len(stated_rows(con)) == 2


def test_the_same_day_on_two_accounts_is_two_figures(con):
    stated_balance.record(
        con, account_alias="nfcu-checking", as_of="2025-03-31", amount="1855.03"
    )
    stated_balance.record(
        con, account_alias="nfcu-cc", as_of="2025-03-31", amount="1200.00"
    )
    assert len(stated_rows(con)) == 2


# --- signs, resolved once, at the boundary -----------------------------------

def test_an_asset_balance_is_stored_as_entered(con):
    recorded = stated_balance.record(
        con, account_alias="nfcu-checking", as_of="2025-03-31", amount="1855.03"
    )
    assert recorded.as_entered == Decimal("1855.03")
    assert recorded.amount == Decimal("1855.03")


def test_a_credit_card_balance_is_entered_as_owed_and_stored_negative(con):
    """The statement says "$1,200.00 balance". The ledger means "-1200.00 held"."""
    recorded = stated_balance.record(
        con, account_alias="nfcu-cc", as_of="2025-03-31", amount="1200.00"
    )
    assert recorded.balance_sign == "liability"
    assert recorded.as_entered == Decimal("1200.00")
    assert recorded.amount == Decimal("-1200.00")
    assert con.execute("SELECT amount FROM balances").fetchone()[0] == Decimal("-1200.00")


def test_an_overpaid_card_is_entered_negative_and_stored_positive(con):
    """The convention has to survive the unusual direction too, or it is a hack."""
    recorded = stated_balance.record(
        con, account_alias="nfcu-cc", as_of="2025-03-31", amount="-42.00"
    )
    assert recorded.amount == Decimal("42.00")


def test_showing_a_stored_figure_back_returns_what_was_entered(con):
    for alias, entered in (("nfcu-checking", "1855.03"), ("nfcu-cc", "1200.00")):
        recorded = stated_balance.record(
            con, account_alias=alias, as_of="2025-03-31", amount=entered
        )
        assert stated_balance.as_entered(recorded.amount, recorded.balance_sign) == (
            Decimal(entered)
        )


# --- what it refuses ---------------------------------------------------------

def test_a_third_decimal_place_is_refused_rather_than_rounded(con):
    """Money is DECIMAL(18,2). A silently rounded cent is an unfindable delta."""
    with pytest.raises(StatedBalanceError, match="two decimal places"):
        stated_balance.record(
            con, account_alias="nfcu-checking", as_of="2025-03-31", amount="1855.031"
        )
    assert stated_rows(con) == []


def test_a_typed_currency_figure_is_accepted(con):
    recorded = stated_balance.record(
        con, account_alias="nfcu-checking", as_of="2025-03-31", amount="$1,855.03"
    )
    assert recorded.amount == Decimal("1855.03")


def test_a_non_numeric_amount_is_refused(con):
    with pytest.raises(StatedBalanceError, match="not a number"):
        stated_balance.record(
            con, account_alias="nfcu-checking", as_of="2025-03-31", amount="about 1855"
        )


def test_a_malformed_date_is_refused(con):
    with pytest.raises(StatedBalanceError, match="YYYY-MM-DD"):
        stated_balance.record(
            con, account_alias="nfcu-checking", as_of="03/31/2025", amount="10.00"
        )


def test_an_undeclared_account_is_refused(con):
    with pytest.raises(KeyError):
        stated_balance.record(
            con, account_alias="not-an-account", as_of="2025-03-31", amount="10.00"
        )


# --- the same thing through the command line ---------------------------------

@pytest.fixture
def configured(private_home, monkeypatch):
    """A private home with a two-account registry, and nothing imported."""
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
    monkeypatch.chdir(REPO_ROOT)
    return private_home


def test_cli_records_a_stated_balance(configured, capsys):
    assert cli.main([
        "record-balance", "--account", "nfcu-checking",
        "--as-of", "2025-03-31", "--amount", "1855.03",
        "--note", "March statement p1",
    ]) == 0
    out = capsys.readouterr().out
    assert "stated ledger balance as of 2025-03-31 recorded: 1855.03" in out
    assert "March statement p1" in out


def test_cli_re_entry_reports_the_correction(configured, capsys):
    cli.main(["record-balance", "--account", "nfcu-checking",
              "--as-of", "2025-03-31", "--amount", "1855.03"])
    capsys.readouterr()
    assert cli.main(["record-balance", "--account", "nfcu-checking",
                     "--as-of", "2025-03-31", "--amount", "1855.30"]) == 0
    out = capsys.readouterr().out
    assert "corrected: 1855.30" in out
    assert "was 1855.03" in out


def test_cli_says_the_sign_flip_out_loud_for_a_card(configured, capsys):
    """An unexplained minus sign in a later report is how trust in a tool ends."""
    assert cli.main(["record-balance", "--account", "nfcu-cc",
                     "--as-of", "2025-03-31", "--amount", "1200.00"]) == 0
    assert "1200.00 owed (stored as -1200.00)" in capsys.readouterr().out


def test_cli_rejects_a_bad_figure_with_advice_not_a_traceback(configured, capsys):
    assert cli.main(["record-balance", "--account", "nfcu-checking",
                     "--as-of", "2025-03-31", "--amount", "1855.031"]) == 2
    assert "two decimal places" in capsys.readouterr().err


def test_cli_rejects_an_undeclared_account_with_advice(configured, capsys):
    assert cli.main(["record-balance", "--account", "nfcu-savings",
                     "--as-of", "2025-03-31", "--amount", "10.00"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("purser: account alias 'nfcu-savings' is not in the registry")
    assert "purser paths" in err


# --- the stored day must survive the host's timezone -------------------------

@pytest.mark.parametrize("session_tz", ["America/Los_Angeles", "Pacific/Honolulu"])
def test_the_stated_day_reads_back_unchanged_west_of_utc(con, session_tz):
    """`as_of` is a TIMESTAMPTZ encoding a *day*, and it must decode as that day.

    A bare `CAST(as_of AS DATE)` resolves in the session's timezone, so the
    canonical 00:00 UTC instant a day is written as reads back as the day
    before on any host behind UTC. That shifts every reconciliation boundary
    by one, both masking real edge-day errors and inventing others.

    `database.connect` pins the session to UTC, which is why this is not
    already breaking in ordinary use -- but the round trip must not *depend* on
    that, because the pin is one `SET TimeZone` away from being undone by
    anything else sharing the connection.
    """
    stated_balance.record(
        con, account_alias="nfcu-checking", as_of="2025-03-31", amount="1855.03"
    )
    account_id = con.execute(
        "SELECT account_id FROM accounts WHERE alias = 'nfcu-checking'"
    ).fetchone()[0]

    con.execute(f"SET TimeZone='{session_tz}'")
    assert [row[0] for row in stated_balance.series(con, account_id)] == [
        date(2025, 3, 31)
    ]


def test_the_month_boundary_does_not_move_with_the_session_timezone(con):
    """The failure the day shift causes: a month-end figure lands in the month before.

    A balance stated on the last day of March must be March's, whatever
    timezone the session carries. Read a day early it becomes 2025-03-30, and
    a figure stated on the 1st of a month would fall into the previous month
    entirely -- the report would attribute it to a month it was never about.
    """
    stated_balance.record(
        con, account_alias="nfcu-checking", as_of="2025-04-01", amount="1855.03"
    )
    account_id = con.execute(
        "SELECT account_id FROM accounts WHERE alias = 'nfcu-checking'"
    ).fetchone()[0]

    con.execute("SET TimeZone='America/Los_Angeles'")
    day = stated_balance.series(con, account_id)[0][0]
    assert (day.year, day.month) == (2025, 4)
