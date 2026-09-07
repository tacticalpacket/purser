"""Agent scaffolding must not be able to reach a commit here.

`.claude/` and `.codex/` are written into a working copy by the agent runtime, not
by anyone working on purser. They carry tool configuration, hook definitions and
local machine paths. This repository is public and GitHub keeps pull-request head
refs permanently, so a commit that picks one up is not something a later rewrite
takes back -- the rule has to be prevention.

The failure this closes was measured on a sibling project: both directories shipped
to a production host, and the exclusion everyone believed in turned out to be a
machine-local `.git/info/exclude` line. From inside the one clone that had it, that
is indistinguishable from a repository rule. Every other clone was bare. So the
tests below are careful about *what* does the ignoring, not just whether something
does: the scratch repository they run in has no machine-local exclude and no global
git configuration, which leaves the tracked `.gitignore` as the only thing that can
account for a match.

Each half is tested against something that must NOT match, for the same reason
`test_workflow_policy.py` writes a violating workflow: a check that has only ever
been shown passing cannot distinguish a protected repository from a broken check.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD = REPO_ROOT / "scripts" / "no-real-data-guard.sh"

# The protection has to hold on any machine, so no machine's git configuration gets
# a say in the results below -- a developer's core.excludesFile could otherwise
# ignore these paths on one machine and account for a pass the repository did not
# earn. (`tests/conftest.py` already re-points HOME; this closes the rest.)
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

SCAFFOLDING = [
    ".claude/settings.local.json",
    ".claude/hooks/on-session-start.sh",
    ".codex/config.toml",
    ".codex/history.jsonl",
]

ORDINARY = ["README.md", "src/purser/cli.py", "config/categories.yaml"]


def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd, capture_output=True, text=True, check=check,
        env={**os.environ, **GIT_ENV},
    )


@pytest.fixture
def scratch_repo(tmp_path) -> Path:
    """A fresh repository carrying this repository's tracked `.gitignore` and nothing else.

    Nothing machine-local comes with it. `git init` writes a `.git/info/exclude`
    holding only commented examples -- asserted here rather than assumed, because
    the whole point is that no exclude line is doing the work -- and `GIT_ENV`
    removes global and system configuration. Whatever this repository ignores, the
    tracked file is what ignores it.
    """
    repo = tmp_path / "scratch"
    repo.mkdir()
    _git("init", "-q", ".", cwd=repo)

    exclude = repo / ".git" / "info" / "exclude"
    active = [
        line for line in exclude.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert active == [], (
        f"the scratch repository's .git/info/exclude carries patterns {active}, so a "
        f"match below would no longer prove the tracked .gitignore is what matched"
    )

    shutil.copyfile(REPO_ROOT / ".gitignore", repo / ".gitignore")
    return repo


# --------------------------------------------------------------------------
# Layer one: the tracked .gitignore, and only the tracked .gitignore
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", SCAFFOLDING)
def test_the_tracked_gitignore_is_what_ignores_agent_scaffolding(scratch_repo, path):
    result = _git("check-ignore", "-v", "--no-index", path, cwd=scratch_repo, check=False)

    assert result.returncode == 0, (
        f"{path} is ignored by nothing that travels with this repository. A clone that "
        f"has not been hand-configured would offer it to `git add .`"
    )
    source = result.stdout.split(":", 1)[0]
    assert source == ".gitignore", (
        f"{path} is ignored by {source}, not by the tracked .gitignore. An exclusion "
        f"only one clone can see is not a repository rule"
    )


@pytest.mark.parametrize("path", ORDINARY)
def test_the_ignore_rule_still_admits_ordinary_project_files(scratch_repo, path):
    """The control. A `.gitignore` that swallowed everything would pass the test above."""
    result = _git("check-ignore", "--no-index", path, cwd=scratch_repo, check=False)

    assert result.returncode == 1, (
        f"{path} is ignored, and it is ordinary project code -- the ignore rules have "
        f"grown past agent scaffolding"
    )


def test_no_agent_scaffolding_is_tracked_in_this_repository():
    """Nothing has already slipped in -- with a positive control on the query itself.

    An empty result proves nothing on its own; it is exactly what a misspelled
    pathspec returns. So the same query runs against a directory known to be
    tracked, and has to come back non-empty.
    """
    tracked = _git("ls-files", "--", ".claude", ".codex", cwd=REPO_ROOT).stdout.split()
    control = _git("ls-files", "--", "scripts", cwd=REPO_ROOT).stdout.split()

    assert control, (
        "the control query found no tracked files under scripts/, so this is a broken "
        "query rather than a clean repository -- the assertion below proves nothing"
    )
    assert tracked == [], f"agent scaffolding is tracked here: {tracked}"


# --------------------------------------------------------------------------
# Layer two: the commit guard, for when .gitignore is walked past
# --------------------------------------------------------------------------


@pytest.fixture
def staging_repo(tmp_path) -> Path:
    """A repository holding the tracked guard and the tracked `.gitignore`.

    Both, because the case the guard exists for is `git add -f` -- staging a path
    the ignore rules already cover. Without the `.gitignore` here, `-f` would have
    nothing to force past and the test would be softer than the real thing.
    """
    repo = tmp_path / "staging"
    (repo / "scripts").mkdir(parents=True)
    _git("init", "-q", ".", cwd=repo)

    shutil.copyfile(REPO_ROOT / ".gitignore", repo / ".gitignore")
    installed = repo / "scripts" / GUARD.name
    shutil.copyfile(GUARD, installed)
    installed.chmod(0o755)
    return repo


def _stage(repo: Path, path: str, body: str = "placeholder\n") -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _git("add", "-f", "--", path, cwd=repo)


def _run_guard(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(repo / "scripts" / GUARD.name)],
        cwd=repo, capture_output=True, text=True,
        env={**os.environ, **GIT_ENV},
    )


@pytest.mark.parametrize("path", [*SCAFFOLDING, "worker-copy/.claude/settings.json"])
def test_the_guard_refuses_staged_agent_scaffolding(staging_repo, path):
    _stage(staging_repo, path)

    result = _run_guard(staging_repo)

    assert result.returncode == 1, (
        f"the guard admitted {path}. `git add -f` is precisely the case it exists for"
    )
    assert path in result.stderr, (
        f"the guard refused the commit without naming {path}, which is what tells "
        f"whoever hit it which file to unstage"
    )


def test_the_guard_fails_with_scaffolding_staged_and_passes_without_it(staging_repo):
    """One repository, one variable: whether `.claude/` is in the staged set."""
    _stage(staging_repo, "src/purser/cli.py", "def main() -> None: ...\n")
    assert _run_guard(staging_repo).returncode == 0, "an ordinary change was refused"

    _stage(staging_repo, ".claude/settings.local.json", "{}\n")
    refused = _run_guard(staging_repo)
    assert refused.returncode == 1
    assert ".claude/settings.local.json" in refused.stderr

    _git("rm", "--cached", "-q", "--", ".claude/settings.local.json", cwd=staging_repo)
    assert _run_guard(staging_repo).returncode == 0, (
        "the guard kept refusing after the scaffolding was unstaged, so it is not the "
        "staged scaffolding it was reacting to"
    )


# --------------------------------------------------------------------------
# The rest of the guard, which this change must leave exactly as it found it
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "data/purser.duckdb.backup",
    "reports/2025-03.md",
    "config/accounts.yaml",
    "notes/january-export.csv",
    "deploy/.env",
    "keys/statement-api.pem",
])
def test_the_guard_still_refuses_what_it_refused_before(staging_repo, path):
    """The scaffolding rule was added to an existing guard; it must displace nothing."""
    _stage(staging_repo, path)

    assert _run_guard(staging_repo).returncode == 1, f"the guard stopped refusing {path}"


@pytest.mark.parametrize("path", [
    "src/purser/cli.py",
    "docs/DESIGN.md",
    "tests/fixtures/nfcu-checking-sample.csv",
])
def test_the_guard_still_admits_what_it_admitted_before(staging_repo, path):
    """Including the fixtures exemption -- a guard that refuses everything is unusable."""
    _stage(staging_repo, path)

    result = _run_guard(staging_repo)
    assert result.returncode == 0, f"the guard started refusing {path}:\n{result.stderr}"
