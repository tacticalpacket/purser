"""Step 5: reconcile the imported ledger against the known ending balance."""

from __future__ import annotations

from decimal import Decimal

from purser.core.balance_check import check_account
from purser.core.importer import import_file
from purser.ingest.ofx import ledger_balance, read_statement


def test_ledger_balance_is_read_from_the_ofx(checking_ofx):
    balance = ledger_balance(checking_ofx)
    assert balance.amount == Decimal("1855.03")
    assert balance.as_of.date().isoformat() == "2025-03-25"
    assert balance.balance_type == "ledger"


def test_available_balance_is_a_different_figure(checking_ofx):
    """AVAILBAL nets pending holds, so it must not be used to reconcile."""
    statement = read_statement(checking_ofx)
    assert statement.available.amount == Decimal("1805.03")
    assert statement.available.amount != statement.ledger.amount


def test_imported_ledger_reconciles(con, checking_csv, checking_ofx):
    import_file(con, checking_csv, account_alias="nfcu-checking")
    result = check_account(con, account_alias="nfcu-checking", ofx_path=checking_ofx)

    assert result.observed_net == Decimal("855.03")
    assert result.expected_net == Decimal("855.03")
    assert result.delta == Decimal("0.00")
    assert result.reconciles
    assert result.opening_balance == Decimal("1000.00")


def test_reconciling_is_unaffected_by_a_second_import(con, checking_csv, checking_ofx):
    import_file(con, checking_csv, account_alias="nfcu-checking")
    import_file(con, checking_csv, account_alias="nfcu-checking")
    assert check_account(con, account_alias="nfcu-checking", ofx_path=checking_ofx).reconciles


def test_a_dropped_row_breaks_the_check(con, checking_csv, checking_ofx):
    """The check must be able to fail, or it proves nothing."""
    import_file(con, checking_csv, account_alias="nfcu-checking")
    con.execute("DELETE FROM transactions WHERE description = 'Larkspur Streaming'")
    result = check_account(con, account_alias="nfcu-checking", ofx_path=checking_ofx)
    assert not result.reconciles
    assert result.delta == Decimal("9.99")  # the missing debit, un-subtracted


def test_an_inverted_sign_breaks_the_check(con, checking_csv, checking_ofx):
    import_file(con, checking_csv, account_alias="nfcu-checking")
    con.execute(
        "UPDATE transactions SET amount = -amount WHERE description = 'Thornfield Utilities'"
    )
    result = check_account(con, account_alias="nfcu-checking", ofx_path=checking_ofx)
    assert not result.reconciles
    assert result.delta == Decimal("128.16")  # 2 x 64.08


def test_balances_are_recorded_with_the_derived_figure_kept_separate(
    con, checking_csv, checking_ofx
):
    import_file(con, checking_csv, account_alias="nfcu-checking")
    check_account(con, account_alias="nfcu-checking", ofx_path=checking_ofx)
    kinds = dict(
        con.execute("SELECT balance_type, amount FROM balances").fetchall()
    )
    assert Decimal(str(kinds["ledger"])) == Decimal("1855.03")     # observed
    assert Decimal(str(kinds["derived"])) == Decimal("1000.00")    # purser's arithmetic
    assert "available" in kinds


def test_available_gap_is_not_computed_for_a_credit_card(con, cc_csv, checking_ofx):
    """AVAILBAL on a card is available CREDIT, a different quantity; differencing
    it against the ledger balance would be meaningless."""
    import_file(con, cc_csv, account_alias="nfcu-cc")
    result = check_account(con, account_alias="nfcu-cc", ofx_path=checking_ofx)
    assert result.balance_sign == "liability"
    assert result.available_gap is None
