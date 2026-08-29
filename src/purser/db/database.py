"""DuckDB connection handling and schema application."""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path

import duckdb

SCHEMA_RESOURCE = "schema.sql"


def schema_sql() -> str:
    """Return the packaged schema DDL."""
    return resources.files("purser.db").joinpath(SCHEMA_RESOURCE).read_text(encoding="utf-8")


def _statements(sql: str) -> list[str]:
    """Split DDL into statements, ignoring semicolons inside line comments."""
    stripped = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in stripped.split(";") if s.strip()]


def connect(db_path: str | Path | None = None) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection with the schema applied.

    ``None`` opens an in-memory database, which is what the tests use.
    """
    con = duckdb.connect(":memory:" if db_path is None else str(db_path))
    apply_schema(con)
    return con


def apply_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Apply the schema. Idempotent: every object is CREATE ... IF NOT EXISTS."""
    for statement in _statements(schema_sql()):
        con.execute(statement)


def sync_accounts(con: duckdb.DuckDBPyConnection, accounts: list[dict]) -> None:
    """Upsert the config/accounts.yaml registry into the accounts table.

    config is the source of truth; the table mirrors it so that everything
    downstream can join on account_id.
    """
    for acct in accounts:
        existing = con.execute(
            "SELECT account_id FROM accounts WHERE alias = ?", [acct["alias"]]
        ).fetchone()
        fields = (
            acct["institution"],
            acct["type"],
            acct.get("currency", "USD"),
            acct.get("balance_sign", "asset"),
            acct.get("adapter"),
        )
        if existing:
            con.execute(
                "UPDATE accounts SET institution = ?, account_type = ?, currency = ?, "
                "balance_sign = ?, adapter = ? WHERE alias = ?",
                [*fields, acct["alias"]],
            )
        else:
            con.execute(
                "INSERT INTO accounts (alias, institution, account_type, currency, "
                "balance_sign, adapter) VALUES (?, ?, ?, ?, ?, ?)",
                [acct["alias"], *fields],
            )


def account_id(con: duckdb.DuckDBPyConnection, alias: str) -> int:
    """Resolve an alias to its account_id, or fail loudly."""
    row = con.execute("SELECT account_id FROM accounts WHERE alias = ?", [alias]).fetchone()
    if row is None:
        raise KeyError(
            f"account alias {alias!r} is not in the registry; "
            f"declare it in config/accounts.yaml"
        )
    return int(row[0])
