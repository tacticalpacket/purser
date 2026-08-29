#!/usr/bin/env python3
"""Prove every statement-shaped fixture is fabricated, not a slice of a real export.

`tests/fixtures/` is the one blind spot in purser's other two protections, and both are
blind to it *by design*: `.gitignore` re-includes the directory at the bottom of the file
so sample data can be committed, and `scripts/no-real-data-guard.sh` exempts it from the
statement-extension rule for the same reason. Everything else in the repository is
refused for looking like an export. Fixtures are admitted for looking like one.

That exemption is what makes this check necessary. A real export dropped into
`tests/fixtures/` -- to reproduce a parse bug, say, with every intention of taking it
back out -- passes both existing layers and lands in a public repository's history.

The check is an **allowlist of synthetic markers**, and it is only an allowlist:

- OFX: `<ORG>` and every `<MEMO>` say `SYNTHETIC`; `<BANKID>`/`<ACCTID>` are zeroed;
  every `<FITID>` matches `SYNTH####`.
- CSV: every description-shaped cell holds one of the invented merchant names below,
  and every card/account-number-shaped cell holds a known placeholder.
- Any statement-shaped file this script has no reader for is a violation, not a pass.
  Adding a `.pdf` or `.xlsx` fixture must fail loudly rather than sail through
  unexamined.

There is deliberately **no denylist of real merchant names**. A list of the places the
captain actually shops would be exactly the disclosure this repository is being hardened
against, committed to the public repository in order to prevent disclosure.

Adding a fixture therefore means adding its invented names to `SYNTHETIC_DESCRIPTIONS`
below. That is the point: a name enters the allowlist because a human wrote it, and a
name that arrived from a bank does not.

    scripts/check_fixture_provenance.py [FIXTURE_DIR]
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"

#: The extensions real statement exports arrive in -- the same list the commit guard
#: refuses outside this directory.
STATEMENT_SUFFIXES = {".csv", ".ofx", ".qfx", ".qif", ".xls", ".xlsx", ".pdf"}

#: Every merchant/description string any fixture is allowed to contain. Invented, one
#: and all; see tests/fixtures/README.md for what each fixture is for.
SYNTHETIC_DESCRIPTIONS = {
    # tests/fixtures/nfcu-checking-sample.csv + .ofx
    "Bramblewood Coffee",
    "Payroll Deposit Vulcan",
    "Quillfeather Books",
    "ATM Withdrawal Marloe",
    "Check 1042",
    "Thornfield Utilities",
    "Transfer To Card",
    "Larkspur Streaming",
    # tests/fixtures/nfcu-cc-sample.csv
    "Ironvale Hardware",
    "Saltmarsh Deli",
    "Payment Thank You",
    "Annual Membership",
    # the overlapping-window and split-day sets
    "Emberlyn Grocery",
    "Marrowgate Transit",
    "Cinderhale Refund",
    # tests/fixtures/malformed-sample.csv, where the name states the trap
    "Good Row Merchant",
    "Unknown Indicator",
    "Iso Date Merchant",
    "Empty Amount",
    "Presigned Amount",
}

#: Columns holding free text a bank wrote. These are the ones that carry merchant names.
DESCRIPTION_COLUMNS = {"Description", "Memo", "Payee", "Name", "Merchant"}

#: Columns holding a fragment of an account or card number.
IDENTIFIER_COLUMNS = {"Card Ending", "Account Number", "Account", "Check Serial Number"}

#: Placeholders those identifier columns may hold. Empty is always fine.
SYNTHETIC_IDENTIFIERS = {"", "4417", "1042"}

#: The marker every OFX institution and memo field must carry.
SYNTHETIC_MARKER = "SYNTHETIC"

FITID = re.compile(r"^SYNTH\d{4}$")
ZEROED = re.compile(r"^0+$")

_TAG = r"<{tag}>([^<]*)"


def _tag_values(text: str, tag: str) -> list[str]:
    """Every value of an SGML/XML-style OFX tag. OFX exports are one long line."""
    return [m.strip() for m in re.findall(_TAG.format(tag=tag), text)]


def check_ofx(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    name = path.name
    violations: list[str] = []

    orgs = _tag_values(text, "ORG")
    if not orgs:
        violations.append(f"{name}: no <ORG>; a synthetic fixture must declare one")
    for org in orgs:
        if org != SYNTHETIC_MARKER:
            violations.append(f"{name}: <ORG> is {org!r}, expected {SYNTHETIC_MARKER!r}")

    for tag in ("BANKID", "ACCTID", "CCACCTID", "FID"):
        for value in _tag_values(text, tag):
            if not ZEROED.match(value):
                violations.append(
                    f"{name}: <{tag}> is not zeroed. A fixture carries no real "
                    f"institution or account number."
                )

    fitids = _tag_values(text, "FITID")
    if not fitids:
        violations.append(f"{name}: no <FITID> values found; is this really an export?")
    for fitid in fitids:
        if not FITID.match(fitid):
            violations.append(
                f"{name}: FITID {fitid!r} is not of the synthetic form SYNTH####. "
                f"A real FITID is the institution's own transaction identifier."
            )

    memos = _tag_values(text, "MEMO")
    if not memos:
        violations.append(f"{name}: no <MEMO> values; every fixture row must be marked")
    for memo in memos:
        if memo != SYNTHETIC_MARKER:
            violations.append(f"{name}: <MEMO> is {memo!r}, expected {SYNTHETIC_MARKER!r}")

    for value in _tag_values(text, "NAME"):
        if value not in SYNTHETIC_DESCRIPTIONS:
            violations.append(_unknown_description(name, "<NAME>", value))

    return violations


def _unknown_description(name: str, where: str, value: str) -> str:
    return (
        f"{name}: {where} holds {value!r}, which is not in SYNTHETIC_DESCRIPTIONS. "
        f"Either this fixture is not synthetic, or its invented names were never added "
        f"to the allowlist in scripts/check_fixture_provenance.py -- decide which before "
        f"adding it."
    )


def check_csv(path: Path) -> list[str]:
    name = path.name
    violations: list[str] = []

    # utf-8-sig: the real NFCU export carries a BOM and the fixtures reproduce it.
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = set(reader.fieldnames or [])
        described = columns & DESCRIPTION_COLUMNS
        if not described:
            return [
                f"{name}: no recognised description column "
                f"(any of {sorted(DESCRIPTION_COLUMNS)}), so its contents cannot be "
                f"checked against the synthetic allowlist. Teach this script the format "
                f"before committing the fixture."
            ]

        for row_number, row in enumerate(reader, start=2):
            for column in sorted(described):
                value = (row.get(column) or "").strip()
                if value and value not in SYNTHETIC_DESCRIPTIONS:
                    violations.append(
                        _unknown_description(name, f"row {row_number} {column!r}", value)
                    )
            for column in sorted(columns & IDENTIFIER_COLUMNS):
                value = (row.get(column) or "").strip()
                if value not in SYNTHETIC_IDENTIFIERS:
                    violations.append(
                        f"{name}: row {row_number} {column!r} holds {value!r}, which is "
                        f"not a known placeholder. A fixture carries no real card or "
                        f"account number."
                    )

    return violations


READERS = {".ofx": check_ofx, ".qfx": check_ofx, ".csv": check_csv}


def check(fixture_dir: Path = FIXTURE_DIR) -> list[str]:
    """Every provenance violation under a fixture directory, recursively."""
    if not fixture_dir.is_dir():
        return [f"{fixture_dir}: no such fixture directory"]

    violations: list[str] = []
    statement_files = sorted(
        path
        for path in fixture_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in STATEMENT_SUFFIXES
    )
    if not statement_files:
        return [f"{fixture_dir}: holds no statement-shaped fixtures at all"]

    for path in statement_files:
        reader = READERS.get(path.suffix.lower())
        if reader is None:
            violations.append(
                f"{path.relative_to(fixture_dir)}: statement-shaped, but this script has "
                f"no way to read it, so nothing proves it is synthetic. An unverifiable "
                f"fixture is a failure, not a pass."
            )
            continue
        violations.extend(reader(path))
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("fixture_dir", nargs="?", type=Path, default=FIXTURE_DIR)
    args = parser.parse_args(argv)

    violations = check(args.fixture_dir)
    if violations:
        print("fixture provenance check FAILED:", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        return 1

    print(f"OK: every statement-shaped fixture under {args.fixture_dir} is synthetic.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
