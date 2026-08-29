"""Balance reconciliation.

Step 5 of DESIGN.md's first session: check the imported ledger against the
account's known ending balance.

What is and is not verifiable from the data on hand
---------------------------------------------------
Neither NFCU export carries an opening balance. The CSV has no balance column
at all, and the OFX carries only <LEDGERBAL>/<AVAILBAL> as of the download
instant. So the opening balance for the statement window is not an
independently supplied figure anywhere in the captain's data.

That makes the naive check -- "derive the opening balance from the ending
balance, then confirm it reproduces the ending balance" -- circular. It cannot
fail, so it proves nothing.

The check implemented here is the non-circular one available. The CSV and the
OFX are two independently generated exports of the same account and period,
and the OFX carries its own signed TRNAMT alongside the authoritative
<BALAMT>. So:

  expected_net = ledger_balance - opening_balance_implied_by_the_OFX
               = sum of the OFX's own signed transaction amounts

  observed_net = sum of the amounts the CSV adapter actually imported

  delta        = observed_net - expected_net

A zero delta means the adapter's parse, its sign derivation, and its dedupe
together reproduce the institution's own net movement over the window, row for
row -- so the ending balance it implies IS the institution's ending balance.
A non-zero delta is a real defect: a dropped row, a duplicated row, or an
inverted sign.

The opening balance is reported as a derived figure and recorded in `balances`
with balance_type='derived', clearly separated from the observed 'ledger' row,
so nothing downstream mistakes purser's arithmetic for the bank's statement.

Once a second export lands, this same check stops depending on the OFX at all:
the prior run's reconciled ending balance becomes the next window's opening
balance, and the check becomes a straightforward regression over time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import duckdb

from purser.db import database
from purser.ingest import ofx

ZERO = Decimal("0.00")


@dataclass(frozen=True)
class BalanceCheck:
    account_alias: str
    ledger_balance: Decimal        # observed, from OFX <LEDGERBAL><BALAMT>
    ledger_as_of: str
    observed_net: Decimal          # sum of imported transaction amounts
    expected_net: Decimal          # independent net, from the OFX's own TRNAMTs
    delta: Decimal                 # observed - expected; 0 means reconciled
    opening_balance: Decimal       # derived: ledger_balance - expected_net
    transaction_count: int
    source_transaction_count: int
    available_balance: Decimal | None
    # ledger - available, i.e. pending holds not yet posted. Only
    # meaningful on an asset account: on a credit card AVAILBAL is
    # available CREDIT, a different quantity entirely, so it is None there.
    available_gap: Decimal | None
    balance_sign: str = "asset"

    @property
    def reconciles(self) -> bool:
        return self.delta == ZERO


def _ofx_net(path: str | Path) -> tuple[Decimal, int]:
    """Sum the OFX's own signed TRNAMTs. The independent expectation."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    amounts = [Decimal(a.strip()) for a in re.findall(r"<TRNAMT>([^<]*)", text)]
    return sum(amounts, ZERO), len(amounts)


def check_account(
    con: duckdb.DuckDBPyConnection,
    *,
    account_alias: str,
    ofx_path: str | Path,
    record: bool = True,
) -> BalanceCheck:
    """Reconcile the imported ledger for one account against its OFX balance."""
    account_id = database.account_id(con, account_alias)
    balance_sign = con.execute(
        "SELECT balance_sign FROM accounts WHERE account_id = ?", [account_id]
    ).fetchone()[0]
    statement = ofx.read_statement(ofx_path)
    if statement.ledger is None:
        raise ofx.OfxParseError(f"{ofx_path}: no <LEDGERBAL> to reconcile against")

    row = con.execute(
        "SELECT COALESCE(SUM(amount), 0.00), COUNT(*) FROM transactions "
        "WHERE account_id = ?",
        [account_id],
    ).fetchone()
    observed_net = Decimal(str(row[0])).quantize(Decimal("0.01"))
    txn_count = int(row[1])

    expected_net, source_count = _ofx_net(ofx_path)
    ledger = statement.ledger
    opening = (ledger.amount - expected_net).quantize(Decimal("0.01"))
    available = statement.available.amount if statement.available else None
    # On a liability, AVAILBAL reports remaining credit, not a pending-adjusted
    # balance, so differencing it against the ledger is meaningless arithmetic
    # over two different quantities.
    available_gap = (
        (ledger.amount - available)
        if available is not None and balance_sign == "asset"
        else None
    )

    result = BalanceCheck(
        account_alias=account_alias,
        ledger_balance=ledger.amount,
        ledger_as_of=ledger.as_of.isoformat(sep=" "),
        observed_net=observed_net,
        expected_net=expected_net,
        delta=(observed_net - expected_net).quantize(Decimal("0.01")),
        opening_balance=opening,
        transaction_count=txn_count,
        source_transaction_count=source_count,
        available_balance=available,
        available_gap=available_gap,
        balance_sign=balance_sign,
    )

    if record:
        _record(con, account_id, statement, result)
    return result


def _record(con, account_id: int, statement, result: BalanceCheck) -> None:
    """Persist the observed balances and purser's derived opening figure."""
    rows = []
    if statement.ledger:
        rows.append((statement.ledger, "ledger", statement.ledger.amount))
    if statement.available:
        rows.append((statement.available, "available", statement.available.amount))
    for balance, kind, amount in rows:
        con.execute(
            "INSERT INTO balances (account_id, as_of, balance_type, amount, "
            "currency, source_file) VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            [account_id, balance.as_of, kind, amount, balance.currency,
             balance.source_file],
        )
    if statement.start_date is not None:
        con.execute(
            "INSERT INTO balances (account_id, as_of, balance_type, amount, "
            "currency, source_file) VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            [account_id, statement.start_date, "derived", result.opening_balance,
             statement.currency, str(statement.ledger.source_file)],
        )
