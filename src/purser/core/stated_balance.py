"""Recording a stated balance: the one figure purser is not allowed to compute.

Every other number in the ledger is derived from an export. This one is not,
and that is the entire point of it. NFCU's CSV carries no balance column, and
NFCU dropped OFX in April 2026, so there is no file left to read an
authoritative ending balance out of. The captain reads it off his monthly
statement or off the banking site on download day and types it in.

That makes it the **independent input** the monthly check needs. A figure
derived from the same transactions it is meant to validate cannot disagree with
them, so it proves nothing; see `purser.core.monthly_check`, which does the
comparing. This module only records. It never sums a transaction, never opens
an export, and never looks at the ledger -- keeping that separation visible is
what keeps the check honest.

Idempotency
-----------
`balances` is unique on (account_id, as_of, balance_type, source_kind), and
re-entering the same account and date **corrects** the figure rather than
adding a second row. Typing a balance twice is the normal case: it is how a
typo gets fixed. Two rows claiming different balances for one account on one
day is not a record of anything.

Signs, once, at the boundary
----------------------------
Stored amounts are normalized: a balance is negative when the captain owes
value, positive when he holds it, for every account type. A credit card
statement reports the amount owed as a positive number, so for an account
declared `balance_sign: liability` in the registry the entered figure is
negated on the way in. This is the same rule the CSV adapter follows for
`Credit Debit Indicator`, applied at the same kind of boundary, and for the
same reason: storing the source's convention and remembering to flip it
downstream is the classic silent defect in this domain. Because it is resolved
here, the monthly check reconciles a card with exactly the arithmetic it uses
for a checking account.

How `as_of` encodes a day
-------------------------
A hand-entered balance is a fact about a *day*: "this is what the account said
at the end of 2025-03-31". The column is a TIMESTAMPTZ, so the day is stored
canonically as its 00:00 UTC instant, and every comparison against it is made
on dates -- `posted_date <= as_of`, inclusive of the whole named day. The
timestamp is an encoding of the date, not a claim about a time of day.

Because it is an encoding, it must decode the same way everywhere. Reading the
day back is done in UTC **explicitly** (`as_of AT TIME ZONE 'UTC'` in `series`),
not by trusting whatever timezone the session happens to carry: a bare
`CAST(as_of AS DATE)` resolves in the session's zone, so west of UTC the
canonical midnight reads back as the day before and every reconciliation
boundary moves by one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

import duckdb

from purser.db import database

CENTS = Decimal("0.01")

#: A stated figure is always the posted (ledger) balance. `available` nets
#: pending holds, so it cannot agree with a sum of posted transactions, and on
#: a credit card it is not a balance at all but the remaining credit. There is
#: deliberately no way to state one.
BALANCE_TYPE = "ledger"

#: Distinguishes a figure the captain typed from one read out of an export.
SOURCE_KIND = "stated"


class StatedBalanceError(ValueError):
    """The figure or the date it is being recorded against is not usable."""


@dataclass(frozen=True)
class StatedBalance:
    """What was recorded, in both conventions, and whether it replaced anything."""

    account_alias: str
    as_of: date
    amount: Decimal              # normalized, as stored
    as_entered: Decimal          # as the captain typed it, off the statement
    balance_sign: str            # 'asset' | 'liability'
    note: str | None
    corrected: bool              # True when it replaced an earlier figure
    previous_amount: Decimal | None   # normalized; the figure it replaced


def parse_amount(value: str | Decimal | int) -> Decimal:
    """A balance as a human types it off a statement: `1,855.03`, `$1855.03`.

    Refuses more than two decimal places rather than rounding. Money is
    DECIMAL(18,2) everywhere, and silently rounding a third digit is how a
    reconciliation ends up off by a cent for a reason nobody can find.
    """
    if isinstance(value, Decimal):
        candidate = value
    else:
        text = str(value).strip().replace(",", "").replace("$", "").strip()
        if not text:
            raise StatedBalanceError("no amount given")
        try:
            candidate = Decimal(text)
        except InvalidOperation as exc:
            raise StatedBalanceError(f"{value!r} is not a number") from exc

    if not candidate.is_finite():
        raise StatedBalanceError(f"{value!r} is not a finite amount")
    if -candidate.as_tuple().exponent > 2:
        raise StatedBalanceError(
            f"{value!r} has more than two decimal places. A balance is a whole "
            f"number of cents; purser will not round one for you."
        )
    return candidate.quantize(CENTS)


def parse_as_of(value: str | date | datetime) -> date:
    """The day the balance is as of, `YYYY-MM-DD`. Inclusive of that whole day."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise StatedBalanceError(
            f"{value!r} is not a date. Use YYYY-MM-DD -- the day the balance is "
            f"as of, inclusive."
        ) from exc


def normalize(amount: Decimal, balance_sign: str) -> Decimal:
    """Entered convention -> stored convention. See "Signs" in the module docstring."""
    if balance_sign == "liability":
        return (-amount).quantize(CENTS)
    return amount.quantize(CENTS)


def as_entered(amount: Decimal, balance_sign: str) -> Decimal:
    """Stored convention -> entered convention, for showing a figure back."""
    return normalize(amount, balance_sign)  # the mapping is its own inverse


def as_of_timestamp(day: date) -> datetime:
    """The canonical instant for a day. Built in UTC so it does not vary by host."""
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def record(
    con: duckdb.DuckDBPyConnection,
    *,
    account_alias: str,
    as_of: str | date | datetime,
    amount: str | Decimal | int,
    note: str | None = None,
) -> StatedBalance:
    """Record (or correct) the captain's own balance figure for one account and day."""
    account_id = database.account_id(con, account_alias)
    balance_sign, currency = con.execute(
        "SELECT balance_sign, currency FROM accounts WHERE account_id = ?",
        [account_id],
    ).fetchone()

    day = parse_as_of(as_of)
    entered = parse_amount(amount)
    stored = normalize(entered, balance_sign)
    stamp = as_of_timestamp(day)
    note = note.strip() if isinstance(note, str) and note.strip() else None

    existing = con.execute(
        "SELECT balance_id, amount FROM balances "
        "WHERE account_id = ? AND as_of = ? AND balance_type = ? AND source_kind = ?",
        [account_id, stamp, BALANCE_TYPE, SOURCE_KIND],
    ).fetchone()

    if existing is None:
        con.execute(
            "INSERT INTO balances (account_id, as_of, balance_type, source_kind, "
            "amount, currency, note) VALUES (?,?,?,?,?,?,?)",
            [account_id, stamp, BALANCE_TYPE, SOURCE_KIND, stored, currency, note],
        )
        previous = None
    else:
        # The correcting path. One account, one day, one stated figure -- the
        # newest one wins outright rather than accumulating alongside the old.
        previous = Decimal(str(existing[1])).quantize(CENTS)
        con.execute(
            "UPDATE balances SET amount = ?, note = ?, currency = ? WHERE balance_id = ?",
            [stored, note, currency, existing[0]],
        )

    return StatedBalance(
        account_alias=account_alias,
        as_of=day,
        amount=stored,
        as_entered=entered,
        balance_sign=balance_sign,
        note=note,
        corrected=existing is not None,
        previous_amount=previous,
    )


def series(con: duckdb.DuckDBPyConnection, account_id: int) -> list[tuple[date, Decimal, str | None]]:
    """Every stated figure for an account, oldest first.

    Reads `balances` and nothing else. `purser.core.monthly_check` depends on
    that: its independent input must not be able to pick up anything derived
    from the transactions it is checking.
    """
    # `AT TIME ZONE 'UTC'` before the cast, never a bare CAST: `as_of` is a
    # TIMESTAMPTZ and a bare cast resolves it in the *session's* timezone, so
    # the 00:00 UTC instant a day is written as reads back as the previous day
    # on any host behind UTC. That would shift every reconciliation boundary by
    # one, masking real edge-day errors and inventing others. `database.connect`
    # pins the session to UTC, but this must not depend on that: the day has to
    # round-trip identically whoever opened the connection.
    rows = con.execute(
        "SELECT CAST(as_of AT TIME ZONE 'UTC' AS DATE), amount, note FROM balances "
        "WHERE account_id = ? AND balance_type = ? AND source_kind = ? "
        "ORDER BY as_of",
        [account_id, BALANCE_TYPE, SOURCE_KIND],
    ).fetchall()
    return [(row[0], Decimal(str(row[1])).quantize(CENTS), row[2]) for row in rows]


__all__ = [
    "BALANCE_TYPE",
    "SOURCE_KIND",
    "StatedBalance",
    "StatedBalanceError",
    "as_entered",
    "as_of_timestamp",
    "normalize",
    "parse_amount",
    "parse_as_of",
    "record",
    "series",
]
