# Fixtures

Every file here is **synthetic**. Same column shape and same encoding traps as
the real Navy Federal exports; fabricated merchants, amounts and dates. None of
it is a slice of the captain's data, and none of it may ever become one --
docs/DESIGN.md keeps real financial data outside every git worktree entirely.

**This directory is the one place a real export could pass unnoticed.** Both of
the repository's other protections exempt it on purpose: `.gitignore` re-includes
it at the bottom of the file, and `scripts/no-real-data-guard.sh` skips it in the
statement-extension rule, because sample data has to be committable. Everywhere
else a file is refused for looking like a bank export; here it is admitted for
looking like one.

`scripts/check_fixture_provenance.py` -- run by `tests/test_fixture_provenance.py`
-- is what closes that gap. It reads the contents and holds them to an
**allowlist** of the markers below: the `SYNTHETIC` org and memo, zeroed
`BANKID`/`ACCTID`, `SYNTH####` FITIDs, and the invented merchant names. Adding a
fixture means adding its invented names to `SYNTHETIC_DESCRIPTIONS` in that
script, and a statement-shaped file it has no reader for is a failure rather
than a pass. There is deliberately no denylist of real merchant names: a
committed list of where the captain actually shops would be the disclosure the
check exists to prevent.

The traps these fixtures deliberately reproduce, because they are what the
adapter exists to survive:

- **UTF-8 BOM on the header.** Written with the `utf-8-sig` codec, so a reader
  using a plain `utf-8` codec sees a first column named `"﻿Posting Date"`
  and fails on a lookup of `"Posting Date"`.
- **Unsigned `Amount` with the direction in `Credit Debit Indicator`.** Every
  amount is positive; `Debit` means value left the account.
- **`Reference` empty on every row**, as it is on both real exports, which is
  why identity falls to the content fingerprint.
- **Byte-identical repeated rows.** `nfcu-checking-sample.csv` has three
  identical Bramblewood Coffee rows and `nfcu-cc-sample.csv` two identical
  Saltmarsh Deli rows. These are distinct real transactions and must all
  survive dedupe; a naive content hash collapses them.
- **`Posting Date` != `Transaction Date`** on most rows, as in the real files.

`malformed-sample.csv` holds one good row followed by one row for each parse
trap the adapter must refuse rather than guess at: an unrecognized indicator,
an ISO date where MM/DD/YYYY is required, an empty amount, and a pre-signed
amount that would double-negate.

`nfcu-checking-sample.ofx` carries a `<LEDGERBAL>` of 1855.03, consistent with
an opening balance of 1000.00 plus the checking sample's net of +855.03.

## The overlapping-export set

Same-file-twice is the easy half of idempotency. Monthly use looks like two
*different* exports whose windows overlap, which is the case the occurrence
index in `src/purser/core/fingerprint.py` exists for.

- `nfcu-checking-window-a.csv` — 2025-04-01 .. 2025-04-10, 7 rows.
- `nfcu-checking-window-b.csv` — 2025-04-06 .. 2025-04-15, 7 rows.

The overlap is 2025-04-06 .. 2025-04-10, four rows that are byte-identical
between the two files, and inside it sits a pair of identical Marrowgate
Transit rows on 04-08. That pair is the whole point: it must survive as two
rows after both imports, not collapse to one and not become four.

The overlap is **whole days** on both sides, which is the condition DESIGN.md's
export convention guarantees.

- `nfcu-checking-split-early.csv` / `nfcu-checking-split-late.csv` — the case
  that convention exists to prevent. They model a 2025-04-20 that really held
  three identical transactions, with the export boundary falling *inside* that
  group: the early file caught two, the late file carries the third. The third
  is occurrence 1 in its own file, so it reads as a duplicate and is dropped.
  `tests/test_overlapping_exports.py` asserts that undercount deliberately, to
  pin the known limitation rather than leave it to be discovered later.
