"""Deterministic transaction identity.

DESIGN.md specifies "FITID-plus-fingerprint dedupe". The two halves are not
interchangeable and the choice between them is made per source, from what the
source actually provides:

  fitid       An institution-assigned transaction id. Authoritative when it
              exists, because the institution guarantees its stability.
  fingerprint A hash of the transaction's own content. Always computable.

The NFCU CSV export has a `Reference` column that looks like it should carry
the OFX FITID. Measured against the captain's real exports it is empty on
every row of both accounts -- 0 of 958 for checking and 0 of 765 for the card.
So for that adapter the fingerprint is the identity, and `fitid` stays NULL
until a source turns up that populates one.

The subtlety that makes a naive content hash wrong
--------------------------------------------------
A pure hash of the row's fields is lossy, because genuinely distinct
transactions can be byte-identical: the same coffee bought twice on the same
day is two real rows with identical content. In the captain's checking export
23 of 958 rows are exact duplicates of another row, and on the card 7 pairs
are. Hashing content alone would silently collapse those into one, quietly
deleting real spending -- a dedupe bug that looks like clean data.

So the fingerprint includes an occurrence index: the 1-based ordinal of this
row among rows sharing identical content within the same account. The second
identical coffee hashes differently from the first, and both survive.

That index is stable across re-imports of the same file, and across
overlapping re-exports, because it is scoped to the content key rather than to
a line number -- the Nth identical transaction on a given day is the Nth in
any export that contains that day in full. The one case it cannot survive is
an export whose range boundary splits a group of identical same-day
transactions; such a row would be seen as new. The download-date filename
convention in DESIGN.md means overlaps are whole-day, so this does not arise
in practice, but it is the known edge and is recorded here rather than
discovered later.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from decimal import Decimal

# Bump when the hashed field set changes: fingerprints computed under
# different versions are not comparable, and a bump makes that explicit
# instead of showing up as a wave of phantom "new" transactions.
FINGERPRINT_VERSION = "1"

_SEP = "\x1f"  # ASCII unit separator: cannot occur in the source fields


def normalize_description(raw: str) -> str:
    """Collapse whitespace so trivial spacing changes are not new transactions.

    FROZEN. This function is an input to every stored `dedupe_key`, so its
    output is part of the on-disk data format, not an implementation detail.

    Change what it returns and every fingerprint already in the database
    becomes uncomputable from its source row. The next import then recognizes
    nothing, treats the entire ledger as new, and double-counts it -- with no
    error, because every insert succeeds. If the hashed inputs ever genuinely
    must change, bump FINGERPRINT_VERSION and rebuild, never edit in place.

    Merchant normalization must NOT reuse this function. That pipeline is
    config-driven (`config/rules/merchants.yaml`) and is meant to keep getting
    better at stripping store numbers and city noise; this one is meant never
    to move. They look like the same job and are opposite requirements.

    `test_fingerprint.py::test_the_frozen_normalizer_still_produces_its_pinned_hash`
    pins this: any edit here fails that test loudly.
    """
    return " ".join(raw.split())


def content_key(
    *,
    account_alias: str,
    posted_date: str,
    transaction_date: str,
    amount: Decimal,
    description: str,
    source_type: str = "",
    check_number: str = "",
    card_ending: str = "",
) -> tuple[str, ...]:
    """The identity-bearing fields of a transaction, before occurrence indexing.

    `amount` is the NORMALIZED signed amount, so a debit and a credit of the
    same magnitude never share a key.
    """
    return (
        FINGERPRINT_VERSION,
        account_alias,
        posted_date,
        transaction_date,
        f"{amount:.2f}",
        normalize_description(description),
        source_type.strip(),
        check_number.strip(),
        card_ending.strip(),
    )


def fingerprint(key: tuple[str, ...], occurrence: int) -> str:
    """Hash a content key plus its occurrence index into a stable hex digest."""
    payload = _SEP.join((*key, str(occurrence)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class OccurrenceCounter:
    """Assigns each content key its 1-based occurrence index within a file."""

    def __init__(self) -> None:
        self._seen: Counter[tuple[str, ...]] = Counter()

    def next(self, key: tuple[str, ...]) -> int:
        self._seen[key] += 1
        return self._seen[key]


def dedupe_key(fitid: str | None, fingerprint_hex: str) -> str:
    """DESIGN.md's FITID-plus-fingerprint: prefer the institution's id."""
    return fitid if fitid else fingerprint_hex
