"""purser command line: ingest | balance-check | quality.

Everything runs against local files. Nothing here talks to a network.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from purser import __version__
from purser.core import balance_check
from purser.core.accounts import find_account, load_registry, raw_dir
from purser.core.importer import import_file
from purser.db.database import connect, sync_accounts
from purser.ingest import nfcu_csv

DEFAULT_DB = Path("data/purser.duckdb")
DEFAULT_REGISTRY = Path("config/accounts.yaml")


def _open(args):
    con = connect(args.db)
    sync_accounts(con, load_registry(args.registry))
    return con


def _exports(alias: str, data_root: Path, suffix: str) -> list[Path]:
    """Export files for an alias, oldest download-date first.

    The filename convention is <download-date>_<range-or-kind>, so a plain
    sort is chronological by download and overlapping re-exports apply in the
    order they were pulled.
    """
    directory = raw_dir(alias, data_root)
    if not directory.is_dir():
        raise SystemExit(f"no such directory: {directory}")
    return sorted(p for p in directory.glob(f"*{suffix}") if p.is_file())


def cmd_ingest(args) -> int:
    con = _open(args)
    accounts = load_registry(args.registry)
    aliases = [args.account] if args.account else [a["alias"] for a in accounts]

    exit_code = 0
    for alias in aliases:
        acct = find_account(accounts, alias)
        adapter = acct.get("adapter", nfcu_csv.ADAPTER_NAME)
        files = [Path(args.file)] if args.file else _exports(alias, args.data_root, ".csv")
        if not files:
            print(f"{alias}: no CSV exports found")
            continue
        for path in files:
            summary = import_file(
                con, path, account_alias=alias, adapter=adapter, strict=not args.lenient
            )
            print(
                f"{alias}: {path.name} "
                f"import_id={summary.import_id} parsed={summary.rows_parsed} "
                f"inserted={summary.rows_inserted} duplicate={summary.rows_duplicate} "
                f"rejected={summary.rows_rejected}"
            )
            if summary.rows_rejected:
                exit_code = 1
    return exit_code


def cmd_balance_check(args) -> int:
    con = _open(args)
    accounts = load_registry(args.registry)
    aliases = [args.account] if args.account else [a["alias"] for a in accounts]

    exit_code = 0
    for alias in aliases:
        ofx_files = _exports(alias, args.data_root, ".ofx")
        if not ofx_files:
            print(f"{alias}: no OFX export to reconcile against; skipped")
            continue
        result = balance_check.check_account(
            con, account_alias=alias, ofx_path=ofx_files[-1]
        )
        verdict = "RECONCILES" if result.reconciles else "DOES NOT RECONCILE"
        print(
            f"{alias}: {verdict} delta={result.delta} "
            f"(imported {result.transaction_count} of "
            f"{result.source_transaction_count} source transactions, "
            f"as of {result.ledger_as_of})"
        )
        if not result.reconciles:
            exit_code = 1
    return exit_code


def cmd_quality(args) -> int:
    """Structural report on the ledger. Prints aggregates, never rows."""
    con = _open(args)
    rows = con.execute(
        """
        SELECT a.alias, COUNT(t.transaction_id), MIN(t.posted_date),
               MAX(t.posted_date), COALESCE(SUM(t.amount), 0.00)
        FROM accounts a LEFT JOIN transactions t USING (account_id)
        GROUP BY a.alias ORDER BY a.alias
        """
    ).fetchall()
    for alias, count, lo, hi, net in rows:
        print(f"{alias}: {count} transactions  {lo}..{hi}  net={net}")
    imports = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT source_sha256), "
        "SUM(CASE WHEN status <> 'ok' THEN 1 ELSE 0 END) FROM import_log"
    ).fetchone()
    print(f"imports: {imports[0]} runs over {imports[1]} distinct files, "
          f"{imports[2] or 0} not ok")
    return 0


def cmd_sniff(args) -> int:
    """Structural inspection of an export: columns, BOM, row count."""
    shape = nfcu_csv.sniff(args.file)
    print(f"path: {shape['path']}")
    print(f"utf-8 BOM: {shape['has_utf8_bom']}")
    print(f"columns match NFCU export: {shape['columns_match']}")
    print(f"data rows: {nfcu_csv.count_data_rows(args.file)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="purser", description=__doc__)
    parser.add_argument("--version", action="version", version=f"purser {__version__}")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--data-root", type=Path, default=Path("data/raw"))
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="import exports into the ledger")
    p.add_argument("--account", help="alias; default is every declared account")
    p.add_argument("--file", help="one file; default is every export in the alias dir")
    p.add_argument("--lenient", action="store_true",
                   help="record bad rows as rejected instead of refusing the file")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("balance-check", help="reconcile against the known ending balance")
    p.add_argument("--account")
    p.set_defaults(func=cmd_balance_check)

    p = sub.add_parser("quality", help="ledger aggregates and import history")
    p.set_defaults(func=cmd_quality)

    p = sub.add_parser("sniff", help="structural inspection of one export file")
    p.add_argument("file")
    p.set_defaults(func=cmd_sniff)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.db.parent.mkdir(parents=True, exist_ok=True)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
