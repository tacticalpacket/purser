#!/usr/bin/env bash
# Second-layer guard against committing real financial data or credentials.
#
# .gitignore is necessary but not sufficient: `git add -f` walks straight past it, and so
# does a tool or editor that stages by absolute path. This script inspects the STAGED set
# (what `git commit` is actually about to record) and refuses the commit if anything in it
# looks like it shouldn't be in git history at all.
#
# Installed as the pre-commit hook via githooks/pre-commit — see README.md / AGENTS.md for
# the one-time `git config core.hooksPath githooks` setup this depends on. Can also be run
# by hand at any time: scripts/no-real-data-guard.sh
#
# What it checks, and why:
#   1. Anything under data/ or reports/. These are no longer repository directories at
#      all — real state lives outside every worktree in the private data home, per
#      docs/DESIGN.md's "Public code, private data" — so nothing purser writes can put a
#      file there. The check stays because a human still can, by hand or by habit.
#   1b. config/accounts.yaml. The tracked registry is accounts.example.yaml; the real one
#      names where the captain actually banks and belongs in the private config overlay.
#      Copying it back into the repo is the obvious way to undo that split by accident.
#   2. Any .csv/.ofx/.qfx/.qif/.xls/.xlsx/.pdf outside tests/fixtures/ — these are the
#      file types real statement exports come in (docs/AUDIT-PLAN.md's NFCU/Schwab export
#      steps). tests/fixtures/ is exempt because it holds synthetic sample data on purpose
#      — and because that exemption is a blind spot, what is actually IN those files is
#      checked separately by scripts/check_fixture_provenance.py, run by the test suite.
#   3. Anything named like a credential — .env files, *.pem/*.key/*.pfx/*.p12, private-key
#      filenames, or a filename containing "secret"/"credential"/"password".
#
# This protects THIS repo on THIS machine, and only once enabled — see AGENTS.md for the
# residual gap (a file copied elsewhere, or something that already made it into history,
# are both outside what a pre-commit hook can do anything about).

set -euo pipefail

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "no-real-data-guard: not inside a git work tree" >&2
    exit 1
fi

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

# ACMR: added, copied, modified, renamed — anything about to land in the commit.
staged_files="$(git diff --cached --name-only --diff-filter=ACMR)"

if [ -z "$staged_files" ]; then
    exit 0
fi

violations=()

is_under_fixtures() {
    case "$1" in
        tests/fixtures/*) return 0 ;;
        *) return 1 ;;
    esac
}

while IFS= read -r file; do
    [ -z "$file" ] && continue

    case "$file" in
        data/*|reports/*)
            violations+=("$file :: under data/ or reports/, which are not repository directories — real state lives in the private data home (docs/DESIGN.md). This file bypassed .gitignore (git add -f, or an absolute-path stage)")
            continue
            ;;
        config/accounts.yaml)
            violations+=("$file :: the real account registry belongs in the private config overlay, not in git. The tracked template is config/accounts.example.yaml")
            continue
            ;;
    esac

    lower_file="$(printf '%s' "$file" | tr '[:upper:]' '[:lower:]')"

    case "$lower_file" in
        *.csv|*.ofx|*.qfx|*.qif|*.xls|*.xlsx|*.pdf)
            if ! is_under_fixtures "$file"; then
                violations+=("$file :: statement-export file type outside tests/fixtures/ — real financial data must never be committed")
            fi
            ;;
    esac

    base="$(basename "$lower_file")"
    case "$base" in
        .env|.env.*)
            violations+=("$file :: looks like a dotenv file")
            ;;
        *.pem|*.key|*.pfx|*.p12|id_rsa|id_ed25519|id_ecdsa|id_dsa)
            violations+=("$file :: looks like a private key / certificate file")
            ;;
        *secret*|*credential*|*password*)
            violations+=("$file :: filename contains secret/credential/password")
            ;;
    esac
done <<< "$staged_files"

if [ "${#violations[@]}" -eq 0 ]; then
    exit 0
fi

echo "no-real-data-guard: refusing this commit — files that must never enter git history are staged:" >&2
echo >&2
for v in "${violations[@]}"; do
    echo "  - $v" >&2
done
echo >&2
echo "If one of these is a deliberate, confirmed exception, override with:" >&2
echo "  git commit --no-verify" >&2
echo "(this skips ALL pre-commit hooks for that one commit — use it knowingly, not reflexively)" >&2

exit 1
