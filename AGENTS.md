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

- `data/` (raw exports + the local DuckDB file) and `reports/` (generated output) are
  gitignored **entirely** and must never be committed. `tests/fixtures/` holds synthetic
  data only — real statements never belong there either.
- Hosted models (this includes Claude Code sessions without local-only tooling) must never
  be shown full transaction rows. If a hosted-model pass needs merchant context, extract a
  **distinct unique-merchant-string list only** — no amounts, no dates, no account numbers,
  no row context. `docs/DESIGN.md`'s "Division of labor" section is the source of this rule.
- Second-layer commit guard: `scripts/no-real-data-guard.sh` is a pre-commit hook (installed
  from `githooks/pre-commit`) that inspects the staged set and refuses a commit touching
  `data/`/`reports/`, a statement-shaped file (`.csv`/`.ofx`/`.qfx`/`.qif`/`.xls`/`.xlsx`/`.pdf`)
  outside `tests/fixtures/`, or a credential-shaped filename. It exists because `.gitignore`
  alone doesn't stop `git add -f` or an absolute-path stage.
  - **Opt-in per clone** — git does not clone hooks. Run once after cloning:
    `git config core.hooksPath githooks`. A fresh clone that skips this line is unprotected;
    don't assume otherwise.
  - Override only deliberately, with `git commit --no-verify` (skips all hooks for that
    commit, not just this one).
  - Residual gap, stated plainly: this protects only this repo, on a machine that has run
    the opt-in, going forward. It does nothing for a file already copied elsewhere, and
    nothing for git history if something ever landed there (nothing has, as of the config
    work in PR that introduced this guard — verified by auditing every commit on every ref).

## Categorization

- Deterministic rules run before any AI categorization pass, and a human correction becomes
  a rule rather than a one-off fix — see the header comments in `config/rules/categories.yaml`
  and `config/rules/merchants.yaml` for the exact mechanics (how a rule is added, first-match-
  wins ordering, alias-then-match pipeline order).
- `scripts/check_category_refs.py` is the referential-integrity check: every `category` a
  rule names must exist in `config/categories.yaml`. Run it after editing either rules file
  or the taxonomy.
- AI never does arithmetic (`docs/DESIGN.md`): SQL/Python (`queries/`, `src/purser/core/`)
  compute; a model describes or labels, it does not calculate totals, medians, or verdicts.

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
