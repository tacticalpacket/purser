"""Navy Federal Credit Union CSV export adapter.

Built against the captain's real checking and credit-card exports.

The export format
-----------------
One header shared by the checking and credit-card products, sixteen columns:

    Posting Date, Transaction Date, Amount, Credit Debit Indicator, type,
    Type Group, Reference, Instructed Currency, Currency Exchange Rate,
    Instructed Amount, Description, Category, Check Serial Number,
    Card Ending, Rewards Total, Rewards Type

Three things about it will bite a naive reader, all handled below.

1. The header carries a UTF-8 BOM (EF BB BF). Opened with a plain utf-8
   codec the first column is literally named "﻿Posting Date", so every
   lookup of "Posting Date" raises KeyError and the failure presents as a
   data problem rather than an encoding one. Opened with utf-8-sig it is
   correct. The BOM check in `sniff` exists so this stays diagnosable.

2. Dates are MM/DD/YYYY. Parsed explicitly with strptime rather than by any
   inference, because the first twelve days of each month are ambiguous
   against DD/MM/YYYY and a locale-dependent guess is a silent corruption.

3. The sign convention is not in the Amount column. See below.

The sign convention, and how it was established
-----------------------------------------------
There is both an `Amount` column and a separate `Credit Debit Indicator`, and
the obvious reading -- that Amount is signed -- is wrong.

Measured on the real exports: `Amount` is positive on 958 of 958 checking rows
and 765 of 765 card rows. It is an unsigned magnitude. All directional
information lives in `Credit Debit Indicator`, whose values are exactly
{Debit, Credit}.

So the rule is: Debit means value left the account (negative), Credit means
value arrived (positive).

Proof, against the OFX export of the same account and period, which the
institution generates independently and which carries its own signed TRNAMT:

  - Applying the rule to the CSV reproduces the OFX signed total exactly:
    checking -4.31 vs -4.31, card -714.52 vs -714.52.
  - Stronger than the total: the multiset of (posting date, signed amount)
    pairs is identical between the two files on both accounts. Every row
    agrees, so this is not two errors cancelling in a sum.
  - The Debit/Credit counts match the OFX TRNTYPE counts exactly: checking
    890 debit / 68 credit, card 741 debit / 24 credit.
  - The counterfactual is not close. Trusting Amount as written and ignoring
    the indicator puts checking off by 419,259.04 and the card off by
    119,585.66 -- the classic silent sign defect DESIGN.md calls out.

On the credit card the same rule reads correctly without a special case: a
Purchase is a Debit and lands negative because value left, and the card's
ledger balance is negative when money is owed, which is how NFCU itself
reports it.

Reference / FITID
-----------------
The `Reference` column looks like it should carry the OFX FITID. On the real
exports it is empty on every row of both accounts, so it cannot serve as the
dedupe identity; `fitid` is emitted as None and identity falls to the content
fingerprint. See src/purser/core/fingerprint.py.
"""

from __future__ import annotations

import csv
import hashlib
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from purser import __version__ as PURSER_VERSION
from purser.core.fingerprint import (
    OccurrenceCounter,
    content_key,
    dedupe_key,
    fingerprint,
    normalize_description,
)

ADAPTER_NAME = "nfcu_csv"
ADAPTER_VERSION = "1"

DATE_FORMAT = "%m/%d/%Y"
UTF8_BOM = b"\xef\xbb\xbf"

EXPECTED_COLUMNS = [
    "Posting Date",
    "Transaction Date",
    "Amount",
    "Credit Debit Indicator",
    "type",
    "Type Group",
    "Reference",
    "Instructed Currency",
    "Currency Exchange Rate",
    "Instructed Amount",
    "Description",
    "Category",
    "Check Serial Number",
    "Card Ending",
    "Rewards Total",
    "Rewards Type",
]

DEBIT = "Debit"
CREDIT = "Credit"


class NfcuParseError(ValueError):
    """The file is not a readable NFCU export."""


@dataclass(frozen=True)
class ParsedRow:
    """One transaction, normalized. `amount` is signed per the rule above."""

    posted_date: str          # ISO yyyy-mm-dd
    transaction_date: str     # ISO yyyy-mm-dd
    amount: Decimal
    currency: str
    description: str
    source_type: str
    source_category: str
    check_number: str
    card_ending: str
    fitid: str | None
    fingerprint: str
    dedupe_key: str
    source_line_no: int


@dataclass(frozen=True)
class RejectedRow:
    source_line_no: int
    reason: str


@dataclass(frozen=True)
class ParseResult:
    rows: list[ParsedRow]
    rejected: list[RejectedRow]

    @property
    def net_amount(self) -> Decimal:
        return sum((r.amount for r in self.rows), Decimal("0.00"))


def sniff(path: str | Path) -> dict:
    """Report the file's structural shape without reading transactions.

    Used by the CLI and tests to confirm an export is what it claims to be,
    and to keep the BOM diagnosable rather than mysterious.
    """
    path = Path(path)
    with open(path, "rb") as fh:
        head = fh.read(3)
    with open(path, encoding="utf-8-sig", newline="") as fh:
        header = next(csv.reader(fh))
    return {
        "path": str(path),
        "has_utf8_bom": head == UTF8_BOM,
        "columns": header,
        "columns_match": header == EXPECTED_COLUMNS,
    }


def file_digest(path: str | Path) -> tuple[str, int, datetime]:
    """sha256, size and mtime: the file identity recorded in import_log."""
    path = Path(path)
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = path.stat()
    return (
        digest.hexdigest(),
        stat.st_size,
        datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
    )


def _parse_date(value: str, field: str, line_no: int) -> str:
    value = value.strip()
    if not value:
        raise NfcuParseError(f"line {line_no}: {field} is empty")
    try:
        return datetime.strptime(value, DATE_FORMAT).date().isoformat()
    except ValueError as exc:
        raise NfcuParseError(
            f"line {line_no}: {field} {value!r} is not {DATE_FORMAT}"
        ) from exc


def _signed_amount(row: dict, line_no: int) -> Decimal:
    """Apply the sign convention. The whole adapter turns on this function."""
    raw = (row.get("Amount") or "").strip().replace(",", "").replace("$", "")
    if not raw:
        raise NfcuParseError(f"line {line_no}: Amount is empty")
    try:
        magnitude = Decimal(raw)
    except InvalidOperation as exc:
        raise NfcuParseError(f"line {line_no}: Amount {raw!r} is not a number") from exc

    indicator = (row.get("Credit Debit Indicator") or "").strip()
    if indicator == DEBIT:
        signed = -abs(magnitude)
    elif indicator == CREDIT:
        signed = abs(magnitude)
    else:
        # Refused rather than guessed. An unrecognized indicator means the
        # export changed shape, and defaulting either way would silently
        # invert real money.
        raise NfcuParseError(
            f"line {line_no}: Credit Debit Indicator {indicator!r} is not "
            f"{DEBIT!r} or {CREDIT!r}; refusing to guess the sign"
        )

    if magnitude < 0:
        # Never observed: Amount is an unsigned magnitude on every real row.
        # If NFCU ever starts signing it, abs() above would double-negate a
        # debit into a credit, so this fails loudly instead.
        raise NfcuParseError(
            f"line {line_no}: Amount {raw!r} is negative, but this export "
            f"carries sign in Credit Debit Indicator; the format has changed"
        )
    return signed


def parse_file(
    path: str | Path,
    *,
    account_alias: str,
    strict: bool = True,
) -> ParseResult:
    """Parse an NFCU CSV export into normalized, fingerprinted rows.

    `strict` refuses the whole file on the first bad row. With strict=False a
    bad row is recorded in `rejected` and the rest of the file is imported --
    which is only ever appropriate under a human's eye, because a rejected row
    is missing money.
    """
    path = Path(path)
    shape = sniff(path)
    if not shape["columns_match"]:
        missing = [c for c in EXPECTED_COLUMNS if c not in shape["columns"]]
        raise NfcuParseError(
            f"{path}: unexpected columns. missing={missing} got={shape['columns']}"
        )

    rows: list[ParsedRow] = []
    rejected: list[RejectedRow] = []
    occurrences = OccurrenceCounter()

    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for line_no, raw in enumerate(reader, start=2):  # line 1 is the header
            try:
                posted = _parse_date(raw["Posting Date"], "Posting Date", line_no)
                # Transaction Date is the one that is occasionally blank in
                # other institutions' exports; fall back to posting date.
                txn_raw = (raw.get("Transaction Date") or "").strip()
                txn = (
                    _parse_date(txn_raw, "Transaction Date", line_no)
                    if txn_raw
                    else posted
                )
                amount = _signed_amount(raw, line_no)
                description = normalize_description(raw.get("Description") or "")
                if not description:
                    raise NfcuParseError(f"line {line_no}: Description is empty")

                source_type = (raw.get("type") or "").strip()
                check_number = (raw.get("Check Serial Number") or "").strip()
                card_ending = (raw.get("Card Ending") or "").strip()

                # Empty on every row of the real exports; kept as the hook for
                # a source that does populate it.
                reference = (raw.get("Reference") or "").strip()
                fitid = reference or None

                key = content_key(
                    account_alias=account_alias,
                    posted_date=posted,
                    transaction_date=txn,
                    amount=amount,
                    description=description,
                    source_type=source_type,
                    check_number=check_number,
                    card_ending=card_ending,
                )
                fp = fingerprint(key, occurrences.next(key))

                rows.append(
                    ParsedRow(
                        posted_date=posted,
                        transaction_date=txn,
                        amount=amount,
                        currency=(raw.get("Instructed Currency") or "").strip() or "USD",
                        description=description,
                        source_type=source_type,
                        source_category=(raw.get("Category") or "").strip(),
                        check_number=check_number,
                        card_ending=card_ending,
                        fitid=fitid,
                        fingerprint=fp,
                        dedupe_key=dedupe_key(fitid, fp),
                        source_line_no=line_no,
                    )
                )
            except NfcuParseError as exc:
                if strict:
                    raise
                rejected.append(RejectedRow(source_line_no=line_no, reason=str(exc)))

    return ParseResult(rows=rows, rejected=rejected)


def count_data_rows(path: str | Path) -> int:
    """Rows in the file excluding the header, for the import_log tally."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return max(sum(1 for _ in csv.reader(fh)) - 1, 0)


def hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:  # pragma: no cover - defensive
        return "unknown"


__all__ = [
    "ADAPTER_NAME",
    "ADAPTER_VERSION",
    "EXPECTED_COLUMNS",
    "NfcuParseError",
    "ParseResult",
    "ParsedRow",
    "PURSER_VERSION",
    "count_data_rows",
    "file_digest",
    "hostname",
    "parse_file",
    "sniff",
]
