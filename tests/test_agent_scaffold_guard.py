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
from pathlib import Path, PurePosixPath

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


def _stage(repo: Path, path: str, body: str = "placeholder\n") -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _git("add", "-f", "--", path, cwd=repo)


def _tracked_scaffolding(repo: Path) -> list[str]:
    """Every tracked path holding a `.claude` or `.codex` path SEGMENT, at any depth.

    Segment equality, not a pathspec: `git ls-files -- .claude` is a leading-path
    match, so it reports root-level scaffolding and silently misses the same
    directory one level down.
    """
    tracked = _git("ls-files", "-z", cwd=repo).stdout.split("\0")
    return [p for p in tracked if p and {".claude", ".codex"} & set(PurePosixPath(p).parts)]


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
# The ignore rule: the tracked .gitignore, and only the tracked .gitignore
# --------------------------------------------------------------------------


def _ignored(repo: Path, path: str) -> bool:
    """Whether git actually ignores `path` -- the plain call's exit status, never `-v`'s.

    `check-ignore -v` exits 0 for ANY pattern that decided the path, a negation
    included, so it answers "what matched", not "is it ignored". Reading a verdict
    off it is how a re-inclusion comes back looking like an exclusion.
    """
    return _git("check-ignore", "--no-index", path, cwd=repo, check=False).returncode == 0


def _deciding_pattern(repo: Path, path: str) -> tuple[str, str]:
    """The `(source file, pattern)` git reports as deciding `path`."""
    verbose = _git("check-ignore", "-v", "--no-index", path, cwd=repo, check=False)
    source, _line, pattern = verbose.stdout.split("\t", 1)[0].split(":", 2)
    return source, pattern


@pytest.mark.parametrize("path", SCAFFOLDING)
def test_the_tracked_gitignore_is_what_ignores_agent_scaffolding(scratch_repo, path):
    assert _ignored(scratch_repo, path), (
        f"{path} is ignored by nothing that travels with this repository. A clone that "
        f"has not been hand-configured would offer it to `git add .`"
    )

    source, pattern = _deciding_pattern(scratch_repo, path)

    assert source == ".gitignore", (
        f"{path} is ignored by {source}, not by the tracked .gitignore. An exclusion "
        f"only one clone can see is not a repository rule"
    )
    assert not pattern.startswith("!"), (
        f"{path} was decided by the re-inclusion {pattern}, which un-ignores it. A "
        f"negation must never be what satisfies this test"
    )


@pytest.mark.parametrize("path", ORDINARY)
def test_the_ignore_rule_still_admits_ordinary_project_files(scratch_repo, path):
    """The control. A `.gitignore` that swallowed everything would pass the test above."""
    assert not _ignored(scratch_repo, path), (
        f"{path} is ignored, and it is ordinary project code -- the ignore rules have "
        f"grown past agent scaffolding"
    )


def test_no_agent_scaffolding_is_tracked_anywhere_in_this_repository(scratch_repo):
    """Nothing has already slipped in -- with a control that can fail for that reason.

    An empty result proves nothing on its own; it is equally what a sweep looking in
    the wrong place returns. So the same sweep runs first against a repository where
    scaffolding IS tracked, at the root and nested under `tests/fixtures/`, and has to
    find both. The nested path is the one that matters: it is the path this repository
    documents as un-ignored by the trailing re-inclusion, so it is the likeliest to
    actually land, and a root-anchored query would report the repository clean while
    it sat there.
    """
    _stage(scratch_repo, ".claude/settings.local.json", "{}\n")
    _stage(scratch_repo, "tests/fixtures/.claude/settings.json", "{}\n")

    control = _tracked_scaffolding(scratch_repo)

    assert control == [
        ".claude/settings.local.json",
        "tests/fixtures/.claude/settings.json",
    ], (
        f"the sweep found {control} in a repository tracking scaffolding at both "
        f"depths, so it cannot see everything it claims to -- an empty result from it "
        f"below would mean nothing"
    )

    tracked = _tracked_scaffolding(REPO_ROOT)
    assert tracked == [], f"agent scaffolding is tracked here: {tracked}"


# --------------------------------------------------------------------------
# The commit guard, for when .gitignore is walked past
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


def test_scaffolding_under_the_fixtures_re_inclusion_is_covered_by_the_guard_alone(
    scratch_repo, staging_repo
):
    """The one corner where the ignore rules do not reach, pinned rather than papered over.

    `!/tests/fixtures/**` sits at the bottom of `.gitignore` and gitignore is
    last-match-wins, so it un-ignores `tests/fixtures/.claude/`. Moving the
    `.claude/` rule below it is not available -- AGENTS.md pins those re-inclusions
    last. The guard is what covers this path, which is the layering working as
    designed, and this test is what will notice if either half changes.
    """
    path = "tests/fixtures/.claude/settings.json"

    assert not _ignored(scratch_repo, path), (
        f"{path} is now ignored by the tracked .gitignore. That is an improvement, but "
        f"the re-inclusion block moved or narrowed to get there -- check AGENTS.md "
        f"layer 2 and the guard's refusal message, which both describe the old state"
    )

    _stage(staging_repo, path)
    result = _run_guard(staging_repo)

    assert result.returncode == 1, (
        f"{path} is ignored by nothing and refused by nothing, so `git add .` in a "
        f"worker copy would stage it into a permanent public ref"
    )
    assert path in result.stderr


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
