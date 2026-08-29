"""Idempotent import: parse, dedupe, and record provenance.

The contract, which the tests pin:

  Importing the same file twice inserts its transactions once. The second run
  inserts zero transaction rows, but still writes its own import_log entry
  recording that it ran and what it saw. Silence is not an acceptable record
  of an import -- "did that file get imported?" must be answerable, and an
  import that adds nothing is a different fact from an import that never
  happened.

Rows the second run recognizes have their last_import_id advanced, so the log
shows both when a transaction first appeared and when it was last confirmed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import duckdb

from purser import __version__ as PURSER_VERSION
from purser.db import database
from purser.ingest import nfcu_csv

ADAPTERS = {
    nfcu_csv.ADAPTER_NAME: nfcu_csv,
}


@dataclass(frozen=True)
class ImportSummary:
    import_id: int
    account_alias: str
    source_path: str
    source_sha256: str
    rows_in_file: int
    rows_parsed: int
    rows_inserted: int
    rows_duplicate: int
    rows_rejected: int
    net_amount: Decimal
    min_posted_date: str | None
    max_posted_date: str | None

    @property
    def was_noop(self) -> bool:
        """True when this run added nothing -- the idempotency signal."""
        return self.rows_inserted == 0


def import_file(
    con: duckdb.DuckDBPyConnection,
    path: str | Path,
    *,
    account_alias: str,
    adapter: str = nfcu_csv.ADAPTER_NAME,
    strict: bool = True,
) -> ImportSummary:
    """Import one export file into the ledger. Safe to re-run on the same file."""
    path = Path(path)
    if adapter not in ADAPTERS:
        raise KeyError(f"unknown adapter {adapter!r}; known: {sorted(ADAPTERS)}")
    module = ADAPTERS[adapter]

    account_id = database.account_id(con, account_alias)
    sha256, size, mtime = module.file_digest(path)
    rows_in_file = module.count_data_rows(path)

    import_id = con.execute(
        """
        INSERT INTO import_log (
            account_id, source_path, source_filename, source_sha256,
            source_bytes, source_mtime, adapter, adapter_version,
            purser_version, hostname, status, rows_in_file
        ) VALUES (?,?,?,?,?,?,?,?,?,?, 'running', ?)
        RETURNING import_id
        """,
        [
            account_id,
            str(path),
            path.name,
            sha256,
            size,
            mtime,
            module.ADAPTER_NAME,
            module.ADAPTER_VERSION,
            PURSER_VERSION,
            module.hostname(),
            rows_in_file,
        ],
    ).fetchone()[0]

    try:
        result = module.parse_file(path, account_alias=account_alias, strict=strict)
    except Exception as exc:
        con.execute(
            "UPDATE import_log SET status = 'failed', finished_at = ?, notes = ? "
            "WHERE import_id = ?",
            [datetime.now(timezone.utc), f"{type(exc).__name__}: {exc}"[:2000], import_id],
        )
        raise

    # Partition against what the ledger already holds. Done explicitly rather
    # than by swallowing constraint violations, so the duplicate count is a
    # measured number and not an inferred one.
    existing = {
        row[0]
        for row in con.execute(
            "SELECT dedupe_key FROM transactions WHERE account_id = ?", [account_id]
        ).fetchall()
    }
    new_rows = [r for r in result.rows if r.dedupe_key not in existing]
    dup_keys = [r.dedupe_key for r in result.rows if r.dedupe_key in existing]

    if new_rows:
        con.executemany(
            """
            INSERT INTO transactions (
                account_id, posted_date, transaction_date, amount, currency,
                description, source_type, source_category, check_number,
                card_ending, fitid, fingerprint, dedupe_key,
                first_import_id, last_import_id, source_line_no
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                [
                    account_id,
                    r.posted_date,
                    r.transaction_date,
                    r.amount,
                    r.currency,
                    r.description,
                    r.source_type,
                    r.source_category,
                    r.check_number or None,
                    r.card_ending or None,
                    r.fitid,
                    r.fingerprint,
                    r.dedupe_key,
                    import_id,
                    import_id,
                    r.source_line_no,
                ]
                for r in new_rows
            ],
        )

    # Re-confirm the rows this run saw again: same transaction, later sighting.
    if dup_keys:
        con.executemany(
            "UPDATE transactions SET last_import_id = ? "
            "WHERE account_id = ? AND dedupe_key = ?",
            [[import_id, account_id, k] for k in dup_keys],
        )

    posted = [r.posted_date for r in result.rows]
    min_posted = min(posted) if posted else None
    max_posted = max(posted) if posted else None

    con.execute(
        """
        UPDATE import_log SET
            status = 'ok', finished_at = ?, rows_parsed = ?, rows_inserted = ?,
            rows_duplicate = ?, rows_rejected = ?, min_posted_date = ?,
            max_posted_date = ?, net_amount = ?
        WHERE import_id = ?
        """,
        [
            datetime.now(timezone.utc),
            len(result.rows),
            len(new_rows),
            len(dup_keys),
            len(result.rejected),
            min_posted,
            max_posted,
            result.net_amount,
            import_id,
        ],
    )

    return ImportSummary(
        import_id=import_id,
        account_alias=account_alias,
        source_path=str(path),
        source_sha256=sha256,
        rows_in_file=rows_in_file,
        rows_parsed=len(result.rows),
        rows_inserted=len(new_rows),
        rows_duplicate=len(dup_keys),
        rows_rejected=len(result.rejected),
        net_amount=result.net_amount,
        min_posted_date=min_posted,
        max_posted_date=max_posted,
    )
