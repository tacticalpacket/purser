# purser

The ship's officer responsible for the money. A local-only personal finance
ledger: ingest statements, dedupe them, reconcile them, and report on them.

Nothing here talks to a network, and real financial data never leaves the
machine. **The code is public; the data is not, and they never share a
directory** -- real state lives outside every git worktree, in a private home
resolved from your environment rather than from the checkout. See
`docs/DESIGN.md`, "Public code, private data".

## Status

**M1 Ledger, in progress.** Landed so far: the DuckDB schema, the Navy Federal
CSV adapter, fingerprint/dedupe/`import_log` with provenance, and the balance
check. Next: the Schwab adapter, transfer matching, then the M2 report views.

## Install

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

## Set up your private home

Nothing real lives in this repository. Point purser at a home and give it a
registry, once:

```sh
purser paths          # where private state resolves on this machine

mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/purser"
cp config/accounts.example.yaml "${XDG_CONFIG_HOME:-$HOME/.config}/purser/accounts.yaml"
$EDITOR "${XDG_CONFIG_HOME:-$HOME/.config}/purser/accounts.yaml"
```

The data home is `$PURSER_HOME`, else `${XDG_DATA_HOME:-$HOME/.local/share}/purser`;
it holds `raw/`, `purser.duckdb`, `reports/` and `backups/`. The config home is
`${XDG_CONFIG_HOME:-$HOME/.config}/purser`; it holds the real `accounts.yaml`
and your own `rules/`, which override the generic rules shipped in `config/`.
There is no repo-relative fallback for either, on purpose.

## Use

Drop an export under `<data home>/raw/<alias>/`, where `<alias>` matches an
entry in your registry exactly. Files are never edited or renamed after landing.

```sh
purser sniff ~/.local/share/purser/raw/nfcu-checking/<file>.csv  # columns, BOM, row count
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

The suite runs entirely against `tests/fixtures/`, which are synthetic, and it
never resolves your real home: an isolated one is set before pytest collection
begins. Reaching real data is a deliberate act -- running the CLI -- not
something the tests do for you.

The guards are standalone commands too:

```sh
scripts/no-real-data-guard.sh          # what a commit is about to record
scripts/check_fixture_provenance.py    # every fixture is fabricated
scripts/check_workflow_policy.py       # no workflow can reach the private corpus
scripts/check_category_refs.py         # rule -> taxonomy references, both config layers
```

See `tests/fixtures/README.md` and `AGENTS.md` for the data-handling rules this
repo operates under.
