# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

This repo does not inherit any other repo's agent instructions. It holds the captain's real
bank and brokerage data and has constraints that are unusual for a personal project — read
this whole file before touching anything.

## Spec and structure

`docs/DESIGN.md` is the spec: repo structure, data conventions, division of labor across
models, and the reasoning behind both. Follow its directory tree exactly — it is already
laid out on `main`; do not restructure it. `docs/AUDIT-PLAN.md` is the source the category
taxonomy (`config/categories.yaml`) was built from — if you're touching categories, that's
where the justification has to come from, not invention.

## Real financial data — read before touching `data/` or `reports/`

Protection is three layers, and each covers what the one before it cannot:

1. **`.gitignore`.** `data/` (raw exports plus the local DuckDB file) and `reports/`
   (generated output) are ignored **entirely** and must stay that way. `*.csv`/`*.ofx` are
   ignored globally with `/tests/fixtures/**` re-included at the bottom of the file — last
   match wins, so those re-inclusions must stay last.
2. **The commit guard.** `scripts/no-real-data-guard.sh`, installed as a pre-commit hook
   from `githooks/pre-commit`, inspects the staged set and refuses a commit touching
   `data/`/`reports/`, a statement-shaped file (`.csv`/`.ofx`/`.qfx`/`.qif`/`.xls`/`.xlsx`/`.pdf`)
   outside `tests/fixtures/`, or a credential-shaped filename. It exists because
   `.gitignore` alone doesn't stop `git add -f` or an absolute-path stage.
   - **Opt-in per clone** — git does not clone hooks. Run once after cloning:
     `git config core.hooksPath githooks`. A fresh clone that skips this line is
     unprotected; don't assume otherwise.
   - Override only deliberately, with `git commit --no-verify` (which skips all hooks for
     that commit, not just this one).
   - Residual gap, stated plainly: it is path-and-extension based, and protects only this
     repo, on a machine that has run the opt-in, going forward. It does nothing for a file
     already copied elsewhere, and nothing for git history if something ever landed there
     (nothing has — every blob on every ref has been audited).
3. **Agent discipline**, which is the only layer covering what you put in your own context.
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
   - `tests/fixtures/` is synthetic and must stay synthetic: same column shape and the same
     traps, fabricated merchants, amounts and dates. Never a slice of a real file. See
     `tests/fixtures/README.md`.

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
  ending-balance figure has to be one the captain enters himself; the OFX
  `<LEDGERBAL><BALAMT>` parity check in `src/purser/core/balance_check.py` was a one-time
  proof that the adapter was right, not the ongoing mechanism. The `balances` table already
  carries `source_kind='stated'` and `note` for that figure; **the CLI path that records one
  is not built yet.** Never reconcile against `<AVAILBAL>`: it nets pending holds, and on a
  credit card it reports available *credit*.

## Categorization

- Deterministic rules run before any AI categorization pass, and a human correction becomes
  a rule rather than a one-off fix — see the header comments in `config/rules/categories.yaml`
  and `config/rules/merchants.yaml` for the exact mechanics (how a rule is added, first-match-
  wins ordering, alias-then-match pipeline order).
- `scripts/check_category_refs.py` is the referential-integrity check: every `category` a
  rule names must exist in `config/categories.yaml`. Run it after editing either rules file
  or the taxonomy.
- **Authority precedence is enforced, not conventional**: `manual` > `rule` > `llm`, written
  down and implemented in `src/purser/db/schema.sql`'s `category_source` block. A
  lower-authority pass must not silently overwrite a higher-authority assignment.
- AI never does arithmetic (`docs/DESIGN.md`): SQL/Python (`queries/`, `src/purser/core/`)
  compute; a model describes or labels, it does not calculate totals, medians, or verdicts.

## Conventions

- Money is `DECIMAL(18,2)` everywhere, never `DOUBLE`.
- Account aliases are declared once in `config/accounts.yaml` and the directory under
  `data/raw/` must match the alias exactly. Renaming means changing both in one commit.
- The immutable raw layer is the **file set** under `data/raw/`, not a database table. The
  DuckDB is rebuildable from `data/raw/` plus `config/`. So anything a *human* decides —
  manual categorizations, aliases, stated balances — must live in `config/` or be
  re-enterable, never only in the database.

## Build and test

```
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest        # pyproject sets pythonpath=src, testpaths=tests
```

## CI

No GitHub Actions workflow with an automatic trigger (push/PR/schedule) before 2026-09-01 —
private-repo Actions minutes bill against the captain's account and that budget is
constrained right now. A manual-dispatch-only workflow is the most that's allowed before
that date; check the date before adding any workflow at all.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
