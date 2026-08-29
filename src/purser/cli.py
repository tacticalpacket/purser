"""purser command line: ingest | record-balance | monthly-check | balance-check
| quality | dashboard-data | dashboard | sniff | paths.

Everything runs against local files. Nothing here talks to a network.

Every real path -- the ledger, the export landing zone, the account registry --
resolves from the private home in `purser.core.paths`, never from the working
directory. The defaults below are `None` on purpose: they resolve at call time,
so the answer cannot depend on which checkout the process was started in.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from purser import __version__
from purser import analytics, dashboard
from purser.core import balance_check, monthly_check, paths, stated_balance
from purser.core.accounts import find_account, load_registry, raw_dir
from purser.core.config import MissingRegistry
from purser.core.importer import import_file
from purser.db.database import UnknownAccount, connect, sync_accounts
from purser.ingest import nfcu_csv


def _db_path(args) -> Path:
    return args.db if args.db is not None else paths.database_path()


def _data_root(args) -> Path:
    return args.data_root if args.data_root is not None else paths.raw_root()


def _open(args):
    # The registry is read first on purpose: if it is missing, nothing should have
    # been created on disk before the user is told why.
    accounts = load_registry(args.registry)
    db = _db_path(args)
    paths.ensure_private_dir(db.parent)
    con = connect(db)
    sync_accounts(con, accounts)
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
    data_root = _data_root(args)

    exit_code = 0
    for alias in aliases:
        acct = find_account(accounts, alias)
        adapter = acct.get("adapter", nfcu_csv.ADAPTER_NAME)
        files = [Path(args.file)] if args.file else _exports(alias, data_root, ".csv")
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
    data_root = _data_root(args)

    exit_code = 0
    for alias in aliases:
        ofx_files = _exports(alias, data_root, ".ofx")
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


def cmd_record_balance(args) -> int:
    """Record the captain's own balance figure for one account, as of one day.

    This is the only number in purser that no file supplies. It is the
    independent input `monthly-check` compares the ledger against, so it is
    entered by hand, off a statement -- and re-entering the same account and
    day corrects the figure rather than adding a rival one.
    """
    con = _open(args)
    try:
        recorded = stated_balance.record(
            con,
            account_alias=args.account,
            as_of=args.as_of,
            amount=args.amount,
            note=args.note,
        )
    except stated_balance.StatedBalanceError as exc:
        print(f"purser: {exc}", file=sys.stderr)
        return 2

    shown = f"{recorded.as_entered}"
    if recorded.balance_sign == "liability":
        # Say the flip out loud. A statement shows what is owed as a positive
        # number; the ledger stores it negative. An unexplained minus sign in
        # the next report is how a captain stops trusting the tool.
        shown += f" owed (stored as {recorded.amount})"
    verb = "corrected" if recorded.corrected else "recorded"
    print(f"{args.account}: stated ledger balance as of {recorded.as_of} {verb}: {shown}")
    if recorded.corrected:
        was = stated_balance.as_entered(recorded.previous_amount, recorded.balance_sign)
        print(f"  was {was}")
    if recorded.note:
        print(f"  note: {recorded.note}")
    return 0


def cmd_monthly_check(args) -> int:
    """Derived-versus-stated, one row per stated interval. The ongoing reconciliation.

    Every consecutive pair of stated figures is reconciled, so a month holding
    two figures prints two rows -- the interval, not the month, is what gets
    checked. A zero delta means the interval's *net* movement reconciles; it is
    not proof that every transaction was imported exactly once. See
    `purser.core.monthly_check`.
    """
    con = _open(args)
    if args.account:
        results = monthly_check.check_account(con, account_alias=args.account)
    else:
        results = monthly_check.check_all(con)

    exit_code = 0
    for row in results:
        label = f"{row.account_alias} {row.month}"
        if row.status == monthly_check.NOT_STATED:
            # Neither a pass nor a failure. An unchecked month must look
            # unchecked, or a silent gap reads as a clean run.
            print(f"{label}  not stated")
            continue
        if row.status == monthly_check.ANCHOR:
            print(f"{label}  anchor    {row.stated_as_of}  "
                  f"stated {row.shown(row.stated)} "
                  f"(baseline; nothing earlier to check it against)")
            continue
        verdict = "OK      " if row.reconciles else "MISMATCH"
        # The interval is printed, not just the month: a month can hold two
        # stated figures and so produce two rows, and each row has to say which
        # window it reconciled.
        print(
            f"{label}  {verdict}  {row.anchor_as_of} -> {row.stated_as_of}  "
            f"stated {row.shown(row.stated)}  derived {row.shown(row.derived)}  "
            f"delta {row.shown(row.delta)}  "
            f"(movement {row.shown(row.net_movement)})"
        )
        if not row.reconciles:
            exit_code = 1
    if not results:
        print("nothing to check: no transactions imported and no stated balances")
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


def cmd_dashboard_data(args) -> int:
    """Print the dashboard document as JSON on stdout. Reads; never writes.

    The ledger is opened READ-ONLY, so a reporting run cannot alter the data it
    is reporting on. That also means this command does not create the database
    or sync the account registry the way the writing subcommands do: if the
    ledger is not there yet, that is a fact to report, not one to fix by
    creating an empty one.

    The document is local-only, like everything else here. It carries real
    descriptions and real amounts; nothing sends it anywhere.
    """
    db = _db_path(args)
    if not db.is_file():
        print(f"purser: no ledger at {db}; run `purser ingest` first", file=sys.stderr)
        return 2
    con = analytics.open_read_only(db)
    try:
        document = analytics.build_document(con)
    finally:
        con.close()
    json.dump(document, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")
    return 0


def cmd_dashboard(args) -> int:
    """Build the dashboard document and serve the page that renders it.

    Reads the ledger read-only through the same `analytics.build_document` that
    `dashboard-data` prints, then hands the result to
    `purser.dashboard.server`. Nothing is written and the page has no remote
    subresources at all. `purser.dashboard.server`'s module docstring is where
    those rules are written down.

    `--host` is the one way to publish the page beyond this machine, and it is
    opt-in: the default is unchanged loopback, so anyone who does not ask for a
    routable address still gets exactly the old behaviour. Asking for one loads
    the password from the private config home and hands it to `build_server`,
    which refuses to bind without it. The password is read here and passed on;
    it is never printed, and no failure message quotes it.

    `--document` renders a prepared JSON document instead of building one from
    the ledger. That is how the page is developed against the synthetic fixture
    in `tests/fixtures/dashboard_sample.json`, and it is the only way to look
    at the page without opening real state.
    """
    password = None if dashboard.is_loopback(args.host) else dashboard.load_password()
    if args.document is not None:
        path = Path(args.document)
        if not path.is_file():
            print(f"purser: no such document: {path}", file=sys.stderr)
            return 2
        document = json.loads(path.read_text(encoding="utf-8"))
        print(f"rendering {path}")
    else:
        db = _db_path(args)
        if not db.is_file():
            print(f"purser: no ledger at {db}; run `purser ingest` first", file=sys.stderr)
            return 2
        con = analytics.open_read_only(db)
        try:
            document = analytics.build_document(con)
        finally:
            con.close()
    try:
        return dashboard.serve(
            document, port=args.port, host=args.host, password=password
        )
    except dashboard.AuthenticationRequired as exc:
        print(f"purser: {exc}", file=sys.stderr)
        return 2


def cmd_sniff(args) -> int:
    """Structural inspection of an export: columns, BOM, row count."""
    shape = nfcu_csv.sniff(args.file)
    print(f"path: {shape['path']}")
    print(f"utf-8 BOM: {shape['has_utf8_bom']}")
    print(f"columns match NFCU export: {shape['columns_match']}")
    print(f"data rows: {nfcu_csv.count_data_rows(args.file)}")
    return 0


def cmd_paths(args) -> int:
    """Where private state resolves to. Creates nothing; prints locations only."""
    print(paths.describe())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="purser", description=__doc__)
    parser.add_argument("--version", action="version", version=f"purser {__version__}")
    parser.add_argument("--db", type=Path, default=None,
                        help="ledger file; default is purser.duckdb in $PURSER_HOME")
    parser.add_argument("--registry", type=Path, default=None,
                        help="account registry; default is accounts.yaml in the "
                             "private config overlay")
    parser.add_argument("--data-root", type=Path, default=None,
                        help="export landing zone; default is raw/ in $PURSER_HOME")
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

    p = sub.add_parser(
        "record-balance",
        help="record the balance you read off a statement, for one account and day",
    )
    p.add_argument("--account", required=True, help="alias, as declared in the registry")
    p.add_argument("--as-of", required=True, metavar="YYYY-MM-DD",
                   help="the day the balance is as of, inclusive of that whole day")
    p.add_argument("--amount", required=True,
                   help="the figure as the statement shows it; on a credit card "
                        "that is the amount OWED, entered positive")
    p.add_argument("--note", help="where it came from, e.g. 'August statement p1'")
    p.set_defaults(func=cmd_record_balance)

    p = sub.add_parser(
        "monthly-check",
        help="reconcile the derived balance against the stated one, interval by interval",
        description=(
            "Reconciles every consecutive pair of stated figures: derived = the "
            "earlier figure plus imported movement since it, against the later "
            "figure. A zero delta means that interval's NET movement reconciles "
            "-- it does not prove every transaction was imported exactly once, "
            "since offsetting errors cancel in a sum. A non-zero delta is a real "
            "defect. Months with no stated figure are reported 'not stated'."
        ),
    )
    p.add_argument("--account", help="alias; default is every declared account")
    p.set_defaults(func=cmd_monthly_check)

    p = sub.add_parser("quality", help="ledger aggregates and import history")
    p.set_defaults(func=cmd_quality)

    p = sub.add_parser(
        "dashboard-data",
        help="print the dashboard JSON document to stdout",
        description=(
            "Reads the ledger read-only and prints one JSON document: coverage, "
            "balances, cash flow, spending, merchants, card, recurrence and the "
            "flow-classification counts that make the totals auditable. Card "
            "payments and internal transfers are excluded from spending on both "
            "legs; rows that cannot be placed are reported as ambiguous rather "
            "than folded into a total."
        ),
    )
    p.set_defaults(func=cmd_dashboard_data)

    p = sub.add_parser(
        "dashboard",
        help="serve the dashboard page; 127.0.0.1 unless --host says otherwise",
        description=(
            "Builds the same document `dashboard-data` prints and serves one "
            "self-contained HTML page that renders it. The listener binds "
            "127.0.0.1 unless --host asks for something else, because this page "
            "shows your whole financial position. A non-loopback --host turns on "
            "HTTP Basic authentication and will not start without a password in "
            "`dashboard-password` under the private config home (see `purser "
            "paths`); Basic auth over plain HTTP is readable "
            "on the wire, so it belongs on a network you trust. The page has no "
            "remote subresources: CSS, JavaScript and every chart are inlined, "
            "so it renders with the network cable pulled. Press Ctrl-C to stop."
        ),
    )
    p.add_argument("--port", type=int, default=8787,
                   help="port; 0 picks a free one. Default 8787, and "
                        "if it is taken a free one is picked anyway")
    p.add_argument("--host", default=dashboard.LOOPBACK,
                   help="address to bind. Default 127.0.0.1. Any non-loopback "
                        "address requires the configured password and refuses "
                        "to start without one")
    p.add_argument("--document", metavar="PATH",
                   help="render a prepared JSON document instead of reading the "
                        "ledger; used to develop the page against the synthetic "
                        "fixture")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("sniff", help="structural inspection of one export file")
    p.add_argument("file")
    p.set_defaults(func=cmd_sniff)

    p = sub.add_parser("paths", help="show where private state resolves to")
    p.set_defaults(func=cmd_paths)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (MissingRegistry, UnknownAccount, paths.InsecureDataHome) as exc:
        # All three mean "the private home is not set up the way it has to
        # be". A traceback buries the one sentence that says how to fix it.
        print(f"purser: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
