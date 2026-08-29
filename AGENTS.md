# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

This repo does not inherit any other repo's agent instructions. It is **public code with
private data**: the repository is world-readable, and the machine it is developed on holds
the captain's real bank and brokerage corpus. Those two facts together produce constraints
that are unusual for a personal project — read this whole file before touching anything.

## Spec and structure

`docs/DESIGN.md` is the spec: repo structure, data conventions, division of labor across
models, and the reasoning behind both. Follow its directory tree exactly — it is already
laid out on `main`; do not restructure it. `docs/AUDIT-PLAN.md` is the source the category
taxonomy (`config/categories.yaml`) was built from — if you're touching categories, that's
where the justification has to come from, not invention.

## The privacy boundary — read before touching state or config

**Real financial state lives outside every git worktree.** It is not in this repository,
under any path, ever. `src/purser/core/paths.py` is the only thing that resolves it:

    data home    $PURSER_HOME, else ${XDG_DATA_HOME:-$HOME/.local/share}/purser
    config home  ${XDG_CONFIG_HOME:-$HOME/.config}/purser

`purser paths` prints where that lands. Under the data home: `raw/<alias>/`,
`purser.duckdb`, `reports/`, `backups/`.

There is **no repo-relative fallback**, and adding one back — even as a convenience —
undoes the slice. There is **no symlink** from the repo into the private home either;
that decision is settled, because a symlink puts the corpus back inside the tree for
everything that follows links. `data/` and `reports/` are not repository directories any
more; their `.gitignore` entries are kept as redundant insurance against a human's habit,
not because anything writes there.

This fixes an observed failure, not a theoretical one: every real path used to resolve
against the working directory, so "the ledger" meant "the ledger of whichever checkout
you were standing in", and the captain's real 7.6 MB reconciled ledger was found inside a
*disposable* Firstmate worker copy. See `docs/DESIGN.md`, "Public code, private data".

Configuration is two layers (`src/purser/core/config.py`): tracked generic first, private
overlay second, **overlay wins**. Category rules are first-match-wins, so the overlay's
rules are evaluated *first* — that is what makes "overlay wins" true.

- **Tracked and public**: `config/categories.yaml`, generic rules justified by
  `docs/AUDIT-PLAN.md`, and `config/accounts.example.yaml`.
- **Private overlay**: the real `accounts.yaml`, `rules/merchants.yaml`,
  `rules/categories.yaml`, and any secrets.
- **A human correction becomes a rule in the OVERLAY**, never in the tracked file. A
  merchant alias is disclosure on its own — "this garbled descriptor is that clinic" says
  where someone goes — so the tracked rules files may hold only what is true for anyone.
  Their headers carry the exact mechanics.
- `scripts/check_category_refs.py` checks both layers by default; `--no-private` checks
  the tracked layer alone, as public CI would.

Protection is four layers, and each covers what the ones before it cannot:

1. **Out-of-tree state**, above. The only layer that removes the file from the blast
   radius rather than catching it on the way out.
2. **`.gitignore`.** `*.csv`/`*.ofx` and friends are ignored globally with
   `/tests/fixtures/**` re-included at the bottom — last match wins, so those
   re-inclusions must stay last.
3. **The commit guard.** `scripts/no-real-data-guard.sh`, installed as a pre-commit hook
   from `githooks/pre-commit`, inspects the staged set and refuses a commit touching
   `data/`/`reports/`, `config/accounts.yaml`, a statement-shaped file
   (`.csv`/`.ofx`/`.qfx`/`.qif`/`.xls`/`.xlsx`/`.pdf`) outside `tests/fixtures/`, or a
   credential-shaped filename. It exists because `.gitignore` alone doesn't stop
   `git add -f` or an absolute-path stage.
   - **Opt-in per clone** — git does not clone hooks. Run once after cloning:
     `git config core.hooksPath githooks`. A fresh clone that skips this line is
     unprotected; don't assume otherwise.
   - Override only deliberately, with `git commit --no-verify` (which skips all hooks for
     that commit, not just this one).
   - Residual gap, stated plainly: it is path-and-extension based, and protects only this
     repo, on a machine that has run the opt-in, going forward. It does nothing for a file
     already copied elsewhere, and nothing for git history if something ever landed there
     (nothing has — every blob on every ref has been audited).
4. **Agent discipline**, which is the only layer covering what you put in your own
   context.
   - Hosted models (this includes Claude Code sessions without local-only tooling) must
     never be shown full transaction rows. If a hosted-model pass needs merchant context,
     extract a **distinct unique-merchant-string list only** — no amounts, no dates, no
     account numbers, no row context. `docs/DESIGN.md`'s "Division of labor" section is the
     source of this rule.
   - Work against the real files **programmatically**: write code, run it, and read
     aggregates and shapes — row counts, column names, parse failures, min/max dates,
     checksum totals. Never `cat` a statement. The `.ofx` exports are a **single line**, so
     a casual `head`/`sed` on one dumps hundreds of real transactions at once. Use
     `purser sniff <file>` for structure.
   - `tests/fixtures/` is synthetic and must stay synthetic. It is also the one place a
     real export could pass unnoticed, because layers 2 and 3 both exempt it on purpose;
     `scripts/check_fixture_provenance.py` (run by the suite) is what reads the contents
     and holds them to an allowlist of synthetic markers. Adding a fixture means adding
     its invented names to that allowlist. There is deliberately **no denylist of real
     merchant names** — that would be the disclosure it was meant to prevent.
   - The test suite never resolves the real home. The repository-root `conftest.py` sets an
     isolated one **before collection**, and an autouse fixture narrows it to each test's
     `tmp_path`; both set real environment variables, so subprocesses inherit the isolation.
     Reaching the real corpus is a deliberate act — running the CLI against the real home —
     never something running the tests gets for free.

**What reaches a pull request is permanent.** GitHub retains pull-request head refs
forever; they cannot be deleted, and a history rewrite does not remove them. So anything
that has appeared in a commit message or a pull-request description on this repository
stays reachable regardless of what happens to the branch afterwards. The rule is
prevention, not cleanup: sensitive material must never enter a commit message or a PR
body in the first place. Note what that means for coverage — the commit guard inspects
staged *files*, and the privacy boundary keeps real state out of the tree; neither reads
your prose.

## Trusted and untrusted code — what may execute locally

This repository is public, so strangers can open pull requests against it. The machine
that reviews them holds the private corpus. That is the whole problem, and it is sharper
than it looks: `githooks/pre-commit` runs the *tracked* `scripts/no-real-data-guard.sh`,
so in a checkout of a hostile fork's branch, the first `git commit` anyone makes executes
that fork's script.

The rule is not a list of dangerous paths. **Every file in an untrusted pull request is
potentially executable.** Enumeration fails: a diff also runs through test files,
`conftest.py` and pytest plugins, an ordinary module import, the build backend,
`pyproject.toml`'s declared dependencies, editor and task configuration, and Makefiles.
`githooks/`, `scripts/`, `.github/` and `pyproject.toml` are worth naming as examples of
*why* enumeration fails, not as the boundary.

So:

- **Untrusted fork pull requests are read as a diff, not checked out and not executed.**
  Review the patch in the browser or with `gh pr diff`.
- If a change genuinely must be run to be evaluated, it runs **only in an environment that
  cannot reach** the private data home, the private config home, credentials, agent
  sockets, or the host session. Not "a clean directory on the same machine".
- Changes to `githooks/`, `scripts/`, `.github/` and `pyproject.toml` always get human
  review — but so does the rest of the diff, for the reason above.
- **Filesystem permissions are not the boundary here.** The private home is mode 700,
  which stops another *user*; every process running as the captain reads it, including
  one started by his own shell in a checkout of a fork's branch. Isolation is what
  protects the corpus. Permissions are hygiene.
- Public CI, when it exists, runs on **GitHub-hosted runners against synthetic fixtures
  only**: never a self-hosted runner, never real data, never financial secrets.
  `scripts/check_workflow_policy.py` enforces the shapes that would break this and is run
  by the suite; it passes vacuously while there are no workflows.

**Ordinary trusted development is unaffected.** A Firstmate crewmate or a local Claude
Code session working on this repository is trusted code doing its job: branch, edit, run
the suite, commit, open a pull request, all locally, as now. The target of this section is
*unreviewed external code* running in the trusted environment — nothing else.

## Sharp edges in the NFCU export

Documented in full in `src/purser/ingest/nfcu_csv.py`; the short version:

- The header carries a **UTF-8 BOM**. Read with `utf-8-sig`, or the first column is
  literally named `"﻿Posting Date"` and the failure looks like a data problem rather
  than an encoding one.
- **`Amount` is an unsigned magnitude.** Direction lives in the separate
  `Credit Debit Indicator` (`Debit` = value left, `Credit` = value arrived). Sign is
  normalized at the adapter boundary, never stored raw and flipped downstream.
- **`Reference` is empty on every row**, so it cannot serve as the OFX FITID. Identity for
  a CSV source is the content fingerprint — and per `docs/DESIGN.md` it is one identity
  scheme per account, ever.
- A content hash alone is lossy: byte-identical rows can be distinct real transactions.
  `src/purser/core/fingerprint.py` adds an occurrence index. Its `normalize_description` is
  **frozen** under `FINGERPRINT_VERSION`; changing it invalidates every stored
  `dedupe_key`, and merchant normalization must never reuse it.
- Dates are `MM/DD/YYYY`. Parse explicitly; a locale guess silently corrupts the first
  twelve days of each month.
- NFCU's CSV carries **no balance**, and NFCU dropped OFX in April 2026. So the ongoing
  ending-balance figure is one the captain enters himself: `purser record-balance`
  (`src/purser/core/stated_balance.py`) writes it as `source_kind='stated'`, and
  `purser monthly-check` (`src/purser/core/monthly_check.py`) is the ongoing
  reconciliation. The OFX `<LEDGERBAL><BALAMT>` parity check in
  `src/purser/core/balance_check.py` was a one-time proof that the adapter was right; it
  needs an `.ofx` that will never arrive again. Never reconcile against `<AVAILBAL>`: it
  nets pending holds, and on a credit card it reports available *credit*.

## Reconciliation

- **The monthly check must never derive its expectation from the ledger it is checking.**
  The stated figure is the independent input and comes from `balances`
  (`source_kind='stated'`); the derived figure comes from `transactions`. The two are read
  by separate functions that touch separate tables, and the module headers say why. A
  check that computes both sides from the same rows cannot fail, so it proves nothing and
  nothing will report that it has stopped working.
- Balance signs are normalized **once, at entry**, exactly as the CSV adapter normalizes
  transaction direction: a credit-card figure is typed as the statement shows it (amount
  owed, positive) and stored negative. That is what lets a card reconcile with the same
  arithmetic as a checking account, with no per-type branch downstream.
- `as_of` is inclusive of its whole day, and `database.connect` pins the DuckDB session
  timezone to UTC so a `DATE` vs `TIMESTAMPTZ` comparison cannot shift by a day on a
  machine west of UTC.
- A month with no stated figure is reported `not stated`, and the earliest stated figure is
  reported `anchor`. Neither is a pass; an unchecked month has to look unchecked.

## Categorization

- Deterministic rules run before any AI categorization pass, and a human correction becomes
  a rule rather than a one-off fix — **written into the private overlay**, not the tracked
  file. See the header comments in `config/rules/categories.yaml` and
  `config/rules/merchants.yaml` for the exact mechanics (where a rule goes and why,
  first-match-wins ordering across both layers, alias-then-match pipeline order).
- `scripts/check_category_refs.py` is the referential-integrity check: every `category` a
  rule names must exist in `config/categories.yaml`. Run it after editing either layer or
  the taxonomy. It never echoes a `match` string from the private layer — that string is a
  real merchant descriptor.
- **Authority precedence is enforced, not conventional**: `manual` > `rule` > `llm`, written
  down and implemented in `src/purser/db/schema.sql`'s `category_source` block. A
  lower-authority pass must not silently overwrite a higher-authority assignment.
- AI never does arithmetic (`docs/DESIGN.md`): SQL/Python (`queries/`, `src/purser/core/`)
  compute; a model describes or labels, it does not calculate totals, medians, or verdicts.

## Conventions

- Money is `DECIMAL(18,2)` everywhere, never `DOUBLE`.
- Account aliases are declared once in the private `accounts.yaml`, and the directory
  under `<data home>/raw/` must match the alias exactly. Renaming means changing both
  together.
- The immutable raw layer is the **file set** under `<data home>/raw/`, not a database
  table. The DuckDB is rebuildable from that file set plus configuration. So anything a
  *human* decides — manual categorizations, aliases, stated balances — must live in
  configuration (tracked generic, or the private overlay) or be re-enterable, never only
  in the database. The private home is what porter backs up; the repository is not.

## Build and test

```
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest        # pyproject sets pythonpath=src, testpaths=tests
```

The suite needs no private state and must never touch any: it runs entirely against
`tests/fixtures/`. Two of its checks are also standalone commands —
`scripts/check_fixture_provenance.py` and `scripts/check_workflow_policy.py`.

## CI

**While the repository is private**, no GitHub Actions workflow may have an automatic
trigger (push/PR/schedule): private-repo Actions minutes bill against the captain's
account and that budget is constrained. Manual-dispatch-only is the most that's allowed.
Check the repository's visibility, not a date, before adding any workflow at all.

That restriction retires when **both** are true: the repository is public (public-repo
Actions minutes are free), and the public-CI security gate has passed. It has not been
built yet — do not assume public CI exists. When it is built, the rules in "Trusted and
untrusted code" apply to it, and `scripts/check_workflow_policy.py` enforces the shapes
that would break them.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
