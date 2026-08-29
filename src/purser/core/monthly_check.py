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

The arithmetic: roll forward, one stated figure at a time
---------------------------------------------------------
The ledger holds movement, not position -- an export starting in March says
nothing about what the account held in February. So a stated figure is needed
as the baseline, and each later stated figure is checked against the one
immediately before it:

    derived(figure) = anchor.amount + net movement over (anchor.as_of, figure.as_of]
    delta           = derived - figure.amount

where `anchor` is the stated figure immediately preceding it *by date*. Rolling
forward from the nearest prior stated balance rather than from the very first
one localises a break: the interval whose delta is non-zero is the interval the
bad import landed in, instead of every one after it.

The unit of the check is the interval, not the calendar month
-------------------------------------------------------------
**Every consecutive pair of stated figures is reconciled -- one row each.** A
month is only how the report is grouped and sorted, so a month holding two
stated figures produces two rows, and both intervals are checked.

This is load-bearing, not cosmetic. Deciding a month by the last figure in it
and anchoring on the most recent figure strictly earlier by date silently drops
whole intervals: with figures on the 31st of January, the 15th of March and the
31st of March, March is checked as 15th -> 31st, February reports `not stated`,
and nothing at all reconciles the January-to-March interval. An import error
landing in that window passes while the report looks healthy -- which is the one
failure this module exists to prevent. **No interval between two consecutive
stated figures may go unreconciled, and no unchecked interval may be invisible
in the output.**

What a zero delta does and does not prove
-----------------------------------------
It proves the **net** movement over the interval reconciles: the sum of what was
imported between the two statement dates equals the difference between the two
stated figures. That is what catches an ordinary bad import -- a dropped row, a
duplicated row, an inverted sign, a missing export -- because any one of those
moves the net.

It is **not** proof that every transaction was imported exactly once. The check
compares two sums, and offsetting errors cancel inside a sum: a dropped debit
and an equal duplicated debit net to zero, as does any other pair of errors of
equal size and opposite effect. Transaction-level completeness would need a
different mechanism, comparing rows rather than totals; see `docs/DESIGN.md`.
A non-zero delta, by contrast, is unambiguous -- it is a real defect.

Three outcomes are not failures and are not passes:

    not stated   no figure was entered for any day in that month, so the month
                 has no independent figure of its own. Reported as such, so an
                 unchecked month is visibly unchecked rather than silently
                 counted as fine. (Its movement may still sit inside a
                 reconciled interval spanning it; that is not the same as the
                 month having been checked.)
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
    """One verdict: either one reconciled interval, or one month with no figure.

    `month` groups the report; it does not decide the check. A month holding two
    stated figures yields two rows, one per interval, because the interval is
    the unit that gets reconciled. `anchor_as_of` -> `stated_as_of` is the
    interval a checked row covers.
    """

    account_alias: str
    balance_sign: str
    month: str                        # 'YYYY-MM'; how the report groups, not the unit
    status: str                       # OK | MISMATCH | NOT_STATED | ANCHOR
    stated: Decimal | None            # normalized; what the captain entered
    stated_as_of: date | None
    derived: Decimal | None           # normalized; anchor + imported movement
    delta: Decimal | None             # derived - stated; 0.00 means the net reconciles
    anchor_as_of: date | None
    anchor_amount: Decimal | None
    net_movement: Decimal | None      # imported movement over (anchor_as_of, stated_as_of]
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
    """One account: every stated interval, plus every month that has no figure.

    Oldest first. Rows come from two sources that cannot overlap -- one per
    stated figure, and one per calendar month holding none -- so every interval
    between consecutive stated figures is reconciled and every month without an
    independent figure still says so.
    """
    account_id = database.account_id(con, account_alias)
    balance_sign = con.execute(
        "SELECT balance_sign FROM accounts WHERE account_id = ?", [account_id]
    ).fetchone()[0]

    stated = stated_balance.series(con, account_id)     # the independent input
    extent = _ledger_extent(con, account_id)            # what was imported
    if not stated and extent is None:
        return []

    bounds = [day for day, _, _ in stated]
    if extent is not None:
        bounds.extend(extent)

    results: list[MonthCheck] = []

    # One row per stated figure. The first is the baseline; every later one is
    # checked against its immediate predecessor, so no consecutive pair is
    # skipped -- not even two figures landing in the same calendar month.
    for index, (day, amount, note) in enumerate(stated):
        month = f"{day.year:04d}-{day.month:02d}"
        if index == 0:
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

        anchor_day, anchor_amount, _ = stated[index - 1]
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

    # ...and one row per month of the span that holds no stated figure at all,
    # so a month with nothing independent behind it is visibly unchecked.
    with_a_figure = {f"{day.year:04d}-{day.month:02d}" for day, _, _ in stated}
    for month in _months(min(bounds), max(bounds)):
        if month in with_a_figure:
            continue
        results.append(
            MonthCheck(
                account_alias=account_alias, balance_sign=balance_sign,
                month=month, status=NOT_STATED, stated=None, stated_as_of=None,
                derived=None, delta=None, anchor_as_of=None, anchor_amount=None,
                net_movement=None, note=None,
            )
        )

    # Month first, then the interval's end date, so a month holding two figures
    # reads in the order the intervals actually run.
    results.sort(key=lambda row: (row.month, row.stated_as_of or date.min))
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
