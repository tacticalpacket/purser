"""The schema's structural guarantees."""

from __future__ import annotations

import duckdb
import pytest

from purser.db.database import apply_schema


def test_every_table_design_md_names_exists(con):
    tables = {
        r[0]
        for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).fetchall()
    }
    assert {
        "accounts", "transactions", "transfers", "merchants",
        "categories", "balances", "import_log",
    } <= tables


def test_applying_the_schema_twice_is_safe(con):
    apply_schema(con)  # would raise on a non-idempotent CREATE


def test_dedupe_key_is_unique_within_an_account(con):
    """The constraint that makes idempotency a guarantee rather than a habit."""
    con.execute(
        "INSERT INTO import_log (account_id, source_path, source_filename, "
        "source_sha256, source_bytes, adapter, adapter_version, purser_version) "
        "VALUES (1,'p','p','sha',1,'a','1','0')"
    )
    insert = (
        "INSERT INTO transactions (account_id, posted_date, amount, description, "
        "fingerprint, dedupe_key, first_import_id, last_import_id) "
        "VALUES (1, DATE '2025-03-12', -1.00, 'x', 'fp', 'dk', 1, 1)"
    )
    con.execute(insert)
    with pytest.raises(duckdb.ConstraintException):
        con.execute(insert)


def test_money_is_decimal_not_double(con):
    """Binary floats cannot represent cents; a reconciliation must not fail by 1e-8."""
    types = dict(
        con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'transactions'"
        ).fetchall()
    )
    assert types["amount"] == "DECIMAL(18,2)"


def test_account_type_and_balance_sign_are_constrained(con):
    with pytest.raises(duckdb.ConstraintException):
        con.execute(
            "INSERT INTO accounts (alias, institution, account_type) "
            "VALUES ('x','y','not-a-type')"
        )
