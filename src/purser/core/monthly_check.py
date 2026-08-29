"""The monthly derived-versus-stated check: does the ledger still add up?

This is the ongoing reconciliation mechanism. The OFX `<LEDGERBAL><BALAMT>`
parity check in `purser.core.balance_check` was a one-time proof that the CSV
adapter parsed, signed and deduped correctly, run while an OFX export still
existed. No new OFX will ever arrive -- NFCU dropped the format in April 2026 --
so this is what catches a bad import from here on.

Why it is not circular
----------------------
A check that derives its expectation from the same transactions it is checking
cannot fail, and a check that cannot fail proves nothing. So the two figures
come from two places that cannot contaminate each other, and the code keeps
them apart on purpose:

    the stated figure    `purser.core.stated_balance.series` -- reads
                         `balances`, source_kind='stated'. Typed in by the
                         captain off a statement. Never touches transactions.

    the derived figure   `_ledger_movement` -- reads `transactions`. Sums what
                         was imported. Never touches balances.

    the check            this module's arithmetic, which does no I/O of its
                         own: it calls those two and subtracts.

Neither half can quietly become the other. If a future change makes
`_ledger_movement` consult a stated balance, or `series` fall back to summing
the ledger, the check is worthless from that moment and nothing will say so.

The arithmetic: roll forward, month by month
--------------------------------------------
The ledger holds movement, not position -- an export starting in March says
nothing about what the account held in February. So a stated figure is needed
as the baseline, and each later stated figure is checked against it:

    derived(month) = anchor.amount + net movement over (anchor.as_of, month_end]
    delta          = derived - stated(month)

where `anchor` is the most recent stated figure *before* the one being checked.
Rolling forward from the nearest prior stated balance rather than from the very
first one localises a break: the month whose delta is non-zero is the month the
bad import landed in, instead of every month after it.

A delta of zero means every transaction between the two statement dates was
imported exactly once, with the right sign. A non-zero delta is a real defect:
a dropped row, a duplicated row, an inverted sign, or a missing export.

Three outcomes are not failures and are not passes:

    not stated   no figure was entered for that month. Reported as such, so an
                 unchecked month is visibly unchecked rather than silently
                 counted as fine.
    anchor       the earliest stated figure. It *is* the baseline, so there is
                 nothing independent to check it against. Calling it a pass
                 would be the circularity this module exists to avoid.

Signs
-----
None here. Stated figures are normalized when they are recorded (see
`purser.core.stated_balance`), so a liability's balance is already negative
when value is owed, and the imported amounts are already negative when value
left. A credit card therefore reconciles with exactly the arithmetic above --
no per-type branch, which is the point of resolving the sign at the boundary.
The account's `balance_sign` is carried through only so a report can show the
figures back in the convention the statement uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import duckdb

from purser.core import stated_balance
from purser.db import database

CENTS = Decimal("0.01")
ZERO = Decimal("0.00")

OK = "ok"
MISMATCH = "mismatch"
NOT_STATED = "not stated"
ANCHOR = "anchor"


@dataclass(frozen=True)
class MonthCheck:
    """One account, one month, one verdict."""

    account_alias: str
    balance_sign: str
    month: str                        # 'YYYY-MM'
    status: str                       # OK | MISMATCH | NOT_STATED | ANCHOR
    stated: Decimal | None            # normalized; what the captain entered
    stated_as_of: date | None
    derived: Decimal | None           # normalized; anchor + imported movement
    delta: Decimal | None             # derived - stated; 0.00 means reconciled
    anchor_as_of: date | None
    anchor_amount: Decimal | None
    net_movement: Decimal | None      # imported movement over the rolled window
    note: str | None

    @property
    def checked(self) -> bool:
        """True only when an independent figure was actually compared."""
        return self.status in (OK, MISMATCH)

    @property
    def reconciles(self) -> bool:
        return self.status == OK

    def shown(self, amount: Decimal | None) -> Decimal | None:
        """A stored figure in the convention the captain's statement uses."""
        if amount is None:
            return None
        return stated_balance.as_entered(amount, self.balance_sign)


def _months(first: date, last: date) -> list[str]:
    """Every 'YYYY-MM' from first's month through last's month, inclusive."""
    months: list[str] = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def _ledger_extent(con, account_id: int) -> tuple[date, date] | None:
    """The posted-date span of what was imported. Reads `transactions` only."""
    row = con.execute(
        "SELECT MIN(posted_date), MAX(posted_date) FROM transactions "
        "WHERE account_id = ?",
        [account_id],
    ).fetchone()
    return (row[0], row[1]) if row and row[0] is not None else None


def _ledger_movement(con, account_id: int, *, after: date, through: date) -> Decimal:
    """Net imported movement over (after, through]. Reads `transactions` only.

    `after` is exclusive because the anchor's own as_of day is inclusive in the
    anchor: whatever posted that day is already inside the stated figure, and
    counting it again is a one-day double count in every later month.
    """
    row = con.execute(
        "SELECT COALESCE(SUM(amount), 0.00) FROM transactions "
        "WHERE account_id = ? AND posted_date > ? AND posted_date <= ?",
        [account_id, after, through],
    ).fetchone()
    return Decimal(str(row[0])).quantize(CENTS)


def check_account(
    con: duckdb.DuckDBPyConnection, *, account_alias: str
) -> list[MonthCheck]:
    """Every month of one account, oldest first."""
    account_id = database.account_id(con, account_alias)
    balance_sign = con.execute(
        "SELECT balance_sign FROM accounts WHERE account_id = ?", [account_id]
    ).fetchone()[0]

    stated = stated_balance.series(con, account_id)     # the independent input
    extent = _ledger_extent(con, account_id)            # what was imported
    if not stated and extent is None:
        return []

    # One stated figure per month decides that month: the latest one in it.
    # Earlier ones in the same month still serve as anchors for what follows.
    by_month: dict[str, tuple[date, Decimal, str | None]] = {}
    for day, amount, note in stated:
        by_month[f"{day.year:04d}-{day.month:02d}"] = (day, amount, note)

    bounds = [day for day, _, _ in stated]
    if extent is not None:
        bounds.extend(extent)
    months = _months(min(bounds), max(bounds))

    results: list[MonthCheck] = []
    for month in months:
        entry = by_month.get(month)
        if entry is None:
            results.append(
                MonthCheck(
                    account_alias=account_alias, balance_sign=balance_sign,
                    month=month, status=NOT_STATED, stated=None, stated_as_of=None,
                    derived=None, delta=None, anchor_as_of=None, anchor_amount=None,
                    net_movement=None, note=None,
                )
            )
            continue

        day, amount, note = entry
        anchor = next(
            ((d, a) for d, a, _ in reversed(stated) if d < day), None
        )
        if anchor is None:
            # The baseline. Checking it would mean checking it against itself.
            results.append(
                MonthCheck(
                    account_alias=account_alias, balance_sign=balance_sign,
                    month=month, status=ANCHOR, stated=amount, stated_as_of=day,
                    derived=None, delta=None, anchor_as_of=None, anchor_amount=None,
                    net_movement=None, note=note,
                )
            )
            continue

        anchor_day, anchor_amount = anchor
        movement = _ledger_movement(con, account_id, after=anchor_day, through=day)
        derived = (anchor_amount + movement).quantize(CENTS)
        delta = (derived - amount).quantize(CENTS)
        results.append(
            MonthCheck(
                account_alias=account_alias, balance_sign=balance_sign, month=month,
                status=OK if delta == ZERO else MISMATCH,
                stated=amount, stated_as_of=day, derived=derived, delta=delta,
                anchor_as_of=anchor_day, anchor_amount=anchor_amount,
                net_movement=movement, note=note,
            )
        )
    return results


def check_all(con: duckdb.DuckDBPyConnection) -> list[MonthCheck]:
    """Every declared account, in alias order."""
    aliases = [
        row[0] for row in con.execute("SELECT alias FROM accounts ORDER BY alias").fetchall()
    ]
    results: list[MonthCheck] = []
    for alias in aliases:
        results.extend(check_account(con, account_alias=alias))
    return results


__all__ = [
    "ANCHOR",
    "MISMATCH",
    "MonthCheck",
    "NOT_STATED",
    "OK",
    "check_account",
    "check_all",
]
