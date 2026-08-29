"""Identity must be stable across re-reads and must not collapse real repeats."""

from __future__ import annotations

from decimal import Decimal

from purser.core.fingerprint import (
    OccurrenceCounter,
    content_key,
    dedupe_key,
    fingerprint,
)
from purser.ingest.nfcu_csv import parse_file


def _key(**over):
    base = dict(
        account_alias="nfcu-checking",
        posted_date="2025-03-12",
        transaction_date="2025-03-11",
        amount=Decimal("-42.17"),
        description="Bramblewood Coffee",
    )
    base.update(over)
    return content_key(**base)


def test_identical_rows_get_distinct_fingerprints(checking_csv):
    """Three identical coffee rows are three real transactions, not one."""
    rows = parse_file(checking_csv, account_alias="nfcu-checking").rows
    coffee = [r for r in rows if r.description == "Bramblewood Coffee"]
    assert len(coffee) == 3
    assert len({r.fingerprint for r in coffee}) == 3


def test_all_fingerprints_in_a_file_are_distinct(checking_csv):
    rows = parse_file(checking_csv, account_alias="nfcu-checking").rows
    assert len({r.fingerprint for r in rows}) == len(rows)


def test_fingerprints_are_stable_across_reparses(checking_csv):
    first = parse_file(checking_csv, account_alias="nfcu-checking").rows
    second = parse_file(checking_csv, account_alias="nfcu-checking").rows
    assert [r.fingerprint for r in first] == [r.fingerprint for r in second]


def test_sign_is_part_of_identity():
    """A debit and a credit of the same magnitude are not the same transaction."""
    debit = fingerprint(_key(amount=Decimal("-42.17")), 1)
    credit = fingerprint(_key(amount=Decimal("42.17")), 1)
    assert debit != credit


def test_account_is_part_of_identity():
    """The two legs of a transfer share content but are different transactions."""
    a = fingerprint(_key(account_alias="nfcu-checking"), 1)
    b = fingerprint(_key(account_alias="nfcu-cc"), 1)
    assert a != b


def test_description_whitespace_is_normalized():
    assert _key(description="Bramblewood   Coffee") == _key(description="Bramblewood Coffee")


def test_occurrence_counter_numbers_from_one():
    counter = OccurrenceCounter()
    key = _key()
    assert [counter.next(key) for _ in range(3)] == [1, 2, 3]


def test_dedupe_key_prefers_the_institution_id():
    """DESIGN.md's FITID-plus-fingerprint: use the bank's id when there is one."""
    assert dedupe_key("FIT-123", "abc") == "FIT-123"
    assert dedupe_key(None, "abc") == "abc"
    assert dedupe_key("", "abc") == "abc"


def test_the_frozen_normalizer_still_produces_its_pinned_hash():
    """The one guard on the frozen normalization contract.

    `normalize_description` feeds every stored `dedupe_key`, so its output is
    part of the on-disk data format. If someone "improves" it -- or points the
    merchant normalizer at it -- every existing fingerprint silently changes,
    the next import recognizes nothing, and the whole ledger is imported a
    second time without a single error.

    This pins a known input to its known digest. Failing here means either a
    deliberate FINGERPRINT_VERSION bump plus a rebuild, or a mistake.
    """
    key = content_key(
        account_alias="nfcu-checking",
        posted_date="2025-03-14",
        transaction_date="2025-03-12",
        amount=Decimal("-42.17"),
        description="Bramblewood   Coffee  #1184",  # irregular spacing, on purpose
        source_type="POS",
        check_number="",
        card_ending="4417",
    )
    assert key == (
        "1", "nfcu-checking", "2025-03-14", "2025-03-12", "-42.17",
        "Bramblewood Coffee #1184", "POS", "", "4417",
    )
    assert fingerprint(key, 1) == (
        "58ca3d1895c8ddc20138276dbe9ba893839d52a5cfcbdbfcf67ce21e8488c70c"
    )
