"""Assignment authority: the only supported writer of the enrichment columns.

Three different things assign a category to a transaction -- a deterministic
rule from `config/rules/categories.yaml`, an LLM pass over what the rules did
not match, and the captain correcting one by hand -- and they run at different
times and re-run independently.

Left to convention, the failure is silent and total: the captain corrects a
category, the next rules re-run overwrites it, and nothing anywhere records
that it happened. Months later a report is wrong and there is no trace of why.

So the precedence is a rule the code enforces, stated once here and once in
`src/purser/db/schema.sql`:

    manual (3)   >   rule (2)   >   llm (1)

A pass may write only when its own authority is greater than or equal to the
authority already on the row. Equal is deliberate -- a rules re-run must be
able to refresh its own assignment, and a later manual correction must be able
to replace an earlier one -- and strictly lower is refused.

A refused category assignment is recorded rather than dropped: the losing
proposal goes into the `category_declined_*` columns, one slot, overwritten
each time. That is enough to answer "did the AI want to call this something
else?" without a suggestion queue or a review workflow, which DESIGN.md's
scope does not include.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import duckdb

#: Higher wins. The numbers are ordering only; nothing persists them.
AUTHORITY = {"llm": 1, "rule": 2, "manual": 3}


class UnknownSource(ValueError):
    """A source outside the three the schema's CHECK constraint allows."""


def _authority(source: str) -> int:
    try:
        return AUTHORITY[source]
    except KeyError:
        raise UnknownSource(
            f"{source!r} is not an assignment source; known: {sorted(AUTHORITY)}"
        ) from None


def outranks(new_source: str, current_source: str | None) -> bool:
    """May `new_source` write over an assignment made by `current_source`?

    ``None`` means the row is unassigned, which anything may claim.
    """
    new = _authority(new_source)
    if current_source is None:
        return True
    return new >= _authority(current_source)


@dataclass(frozen=True)
class Assignment:
    """What one assignment attempt did, and why."""

    transaction_id: int
    field: str                    # 'category' | 'merchant'
    source: str                   # the source that attempted the write
    previous_source: str | None   # what already held the row
    applied: bool

    @property
    def declined(self) -> bool:
        return not self.applied

    def __str__(self) -> str:  # pragma: no cover - diagnostics only
        verb = "assigned" if self.applied else "declined"
        held = self.previous_source or "unassigned"
        return f"txn {self.transaction_id}: {self.field} {verb} by {self.source} (held by {held})"


def category_id_for_path(con: duckdb.DuckDBPyConnection, path: str) -> int:
    """Resolve a dotted category path to its id, or fail loudly.

    Rules address categories by path because ids are generated; a rule naming
    a path that is not in `config/categories.yaml` is a broken rule, not a
    row to skip. `scripts/check_category_refs.py` catches that before import.
    """
    row = con.execute(
        "SELECT category_id FROM categories WHERE path = ?", [path]
    ).fetchone()
    if row is None:
        raise KeyError(
            f"category path {path!r} is not in the taxonomy; "
            f"declare it in config/categories.yaml"
        )
    return int(row[0])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def assign_category(
    con: duckdb.DuckDBPyConnection,
    transaction_id: int,
    *,
    category_id: int,
    source: str,
    rule_key: str | None = None,
    at: datetime | None = None,
) -> Assignment:
    """Assign a category, honouring the precedence. Never silently overwrites.

    Returns an `Assignment` describing what happened. A declined attempt is
    recorded in the `category_declined_*` columns so it stays visible.
    """
    _authority(source)  # reject an unknown source before touching the row
    at = at or _now()

    row = con.execute(
        "SELECT category_source FROM transactions WHERE transaction_id = ?",
        [transaction_id],
    ).fetchone()
    if row is None:
        raise KeyError(f"no transaction {transaction_id}")
    current = row[0]

    if outranks(source, current):
        con.execute(
            "UPDATE transactions SET category_id = ?, category_source = ?, "
            "category_rule_key = ?, category_assigned_at = ? "
            "WHERE transaction_id = ?",
            [category_id, source, rule_key, at, transaction_id],
        )
        applied = True
    else:
        # The proposal lost. Keep it: an override the captain never sees is
        # indistinguishable from a pass that never ran.
        con.execute(
            "UPDATE transactions SET category_declined_id = ?, "
            "category_declined_source = ?, category_declined_at = ? "
            "WHERE transaction_id = ?",
            [category_id, source, at, transaction_id],
        )
        applied = False

    return Assignment(
        transaction_id=transaction_id,
        field="category",
        source=source,
        previous_source=current,
        applied=applied,
    )


def assign_merchant(
    con: duckdb.DuckDBPyConnection,
    transaction_id: int,
    *,
    merchant_id: int,
    source: str,
    rule_key: str | None = None,
    at: datetime | None = None,
) -> Assignment:
    """Assign a merchant under the same precedence.

    No declined slot here, unlike category. A merchant mapping is recoverable:
    it is produced by `config/rules/merchants.yaml`, which is committed and
    re-runnable, so a losing proposal can always be reproduced. A category the
    captain corrected by hand is the one an overwrite genuinely destroys.
    """
    _authority(source)
    at = at or _now()

    row = con.execute(
        "SELECT merchant_source FROM transactions WHERE transaction_id = ?",
        [transaction_id],
    ).fetchone()
    if row is None:
        raise KeyError(f"no transaction {transaction_id}")
    current = row[0]

    applied = outranks(source, current)
    if applied:
        con.execute(
            "UPDATE transactions SET merchant_id = ?, merchant_source = ?, "
            "merchant_rule_key = ?, merchant_assigned_at = ? "
            "WHERE transaction_id = ?",
            [merchant_id, source, rule_key, at, transaction_id],
        )

    return Assignment(
        transaction_id=transaction_id,
        field="merchant",
        source=source,
        previous_source=current,
        applied=applied,
    )


__all__ = [
    "AUTHORITY",
    "Assignment",
    "UnknownSource",
    "assign_category",
    "assign_merchant",
    "category_id_for_path",
    "outranks",
]
