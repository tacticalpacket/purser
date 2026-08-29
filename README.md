# purser

The ship's officer responsible for the money. A local-only personal finance
ledger: ingest statements, dedupe them, reconcile them, and report on them.

Private repository. Nothing here talks to a network, and real financial data
never leaves the machine -- see `docs/DESIGN.md`.

## Status

**M1 Ledger, in progress.** Landed so far: the DuckDB schema, the Navy Federal
CSV adapter, fingerprint/dedupe/`import_log` with provenance, and the balance
check. Next: the Schwab adapter, transfer matching, then the M2 report views.

## Install

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

## Use

Drop an export under `data/raw/<alias>/`, where `<alias>` matches an entry in
`config/accounts.yaml` exactly. Files are never edited or renamed after landing.

```sh
purser sniff data/raw/nfcu-checking/<file>.csv   # structure only: columns, BOM, row count
purser ingest                                     # every declared account
purser ingest --account nfcu-checking             # just one
purser balance-check                              # reconcile against the known ending balance
purser quality                                    # ledger aggregates and import history
```

Imports are idempotent: re-running over the same file inserts nothing the
second time, while still recording that the import ran.

## Tests

```sh
.venv/bin/python -m pytest
```

All fixtures are synthetic. See `tests/fixtures/README.md` and `AGENTS.md` for
the data-handling rules this repo operates under.
