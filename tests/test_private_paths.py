"""The private home may never resolve into a git worktree. This is the invariant.

It closes an observed failure, not a hypothetical one. Every real path used to be
resolved relative to the process working directory -- `data/purser.duckdb`,
`data/raw`, `config/accounts.yaml` -- which meant "the ledger of whatever checkout
you are standing in". Firstmate gives each task a fresh disposable copy of the
repository, and the captain's real reconciled ledger was found inside one: a 7.6 MB
file of real transactions in a directory built to be deleted, invisible to the check
that refuses to discard unlanded work.

So these tests assert the property from the outside: resolve with a repository-rooted
working directory and prove the answer is somewhere else entirely.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from purser.core import paths

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Captured at MODULE IMPORT, before any fixture -- autouse or otherwise -- has run.
#: See `test_the_isolated_home_was_in_place_before_this_module_was_imported`.
HOME_AT_IMPORT = os.environ.get(paths.DATA_HOME_ENV)


def _enclosing_worktree(path: Path) -> Path | None:
    """The nearest ancestor of `path` that is a git worktree, if any."""
    for candidate in [path, *path.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def _every_private_path() -> dict[str, Path]:
    """Every path purser will put real financial state into."""
    return {
        "data_home": paths.data_home(),
        "config_home": paths.config_home(),
        "raw_root": paths.raw_root(),
        "database_path": paths.database_path(),
        "reports_dir": paths.reports_dir(),
        "backups_dir": paths.backups_dir(),
        "registry_path": paths.registry_path(),
        "private_rules_dir": paths.private_rules_dir(),
    }


# --------------------------------------------------------------------------
# The invariant itself
# --------------------------------------------------------------------------


def test_no_private_path_lands_inside_the_repository(monkeypatch, tmp_path):
    """Resolve from a repository-rooted cwd; every answer must be outside it."""
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.delenv(paths.DATA_HOME_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    for name, resolved in _every_private_path().items():
        assert resolved.is_absolute(), f"{name} is relative: {resolved}"
        assert REPO_ROOT not in resolved.parents, f"{name} is inside the repo: {resolved}"
        assert resolved != REPO_ROOT, f"{name} IS the repo root"


def test_no_private_path_lands_inside_any_git_worktree(monkeypatch, tmp_path):
    """Not just this repo: no worktree at all, wherever the process is standing."""
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.delenv(paths.DATA_HOME_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    for name, resolved in _every_private_path().items():
        worktree = _enclosing_worktree(resolved)
        assert worktree is None, f"{name} resolved inside the worktree at {worktree}"


def test_the_working_directory_cannot_change_the_answer(monkeypatch, tmp_path):
    """The original defect in one assertion: cwd must not be an input."""
    monkeypatch.delenv(paths.DATA_HOME_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    monkeypatch.chdir(REPO_ROOT)
    from_repo = _every_private_path()

    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    from_elsewhere = _every_private_path()

    assert from_repo == from_elsewhere


def test_the_data_home_follows_purser_home_then_xdg(monkeypatch, tmp_path):
    """The two rules, in order, and no third one."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    monkeypatch.setenv(paths.DATA_HOME_ENV, str(tmp_path / "explicit"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert paths.data_home() == tmp_path / "explicit"

    monkeypatch.delenv(paths.DATA_HOME_ENV)
    assert paths.data_home() == tmp_path / "xdg" / "purser"

    monkeypatch.delenv("XDG_DATA_HOME")
    assert paths.data_home() == tmp_path / "home" / ".local" / "share" / "purser"


def test_the_config_home_follows_xdg_config(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    assert paths.config_home() == tmp_path / "xdg-config" / "purser"

    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert paths.config_home() == tmp_path / "home" / ".config" / "purser"


def test_the_layout_under_the_data_home_is_the_agreed_one(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.DATA_HOME_ENV, str(tmp_path / "home"))
    home = tmp_path / "home"
    assert paths.raw_root() == home / "raw"
    assert paths.database_path() == home / "purser.duckdb"
    assert paths.reports_dir() == home / "reports"
    assert paths.backups_dir() == home / "backups"


def test_a_relative_purser_home_is_refused(monkeypatch):
    """A relative override would resolve against cwd -- the original bug, opted into."""
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setenv(paths.DATA_HOME_ENV, "data")
    with pytest.raises(paths.InsecureDataHome):
        paths.data_home()


def test_a_data_home_inside_this_checkout_is_refused(monkeypatch):
    """Even spelled absolutely, the corpus may not be pointed back into the tree."""
    monkeypatch.setenv(paths.DATA_HOME_ENV, str(REPO_ROOT / "data"))
    with pytest.raises(paths.InsecureDataHome) as excinfo:
        paths.data_home()
    assert "inside the purser checkout" in str(excinfo.value)


def test_a_config_home_inside_this_checkout_is_refused(monkeypatch):
    """The overlay is private state too: the rule is not the data home's alone."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(REPO_ROOT))
    with pytest.raises(paths.InsecureDataHome) as excinfo:
        paths.config_home()
    assert "inside the purser checkout" in str(excinfo.value)


@pytest.mark.parametrize(
    ("env_var", "value", "resolve"),
    [
        (paths.DATA_HOME_ENV, str(REPO_ROOT / "data"), lambda: paths.data_home()),
        ("XDG_DATA_HOME", str(REPO_ROOT), lambda: paths.data_home()),
        ("XDG_CONFIG_HOME", str(REPO_ROOT), lambda: paths.config_home()),
    ],
)
def test_the_refusal_names_the_setting_that_actually_caused_it(
    monkeypatch, env_var, value, resolve
):
    """The remedy must name the offending setting, not a fixed one.

    Each home is reached through its own setting, so a message that always says
    "point $PURSER_HOME somewhere else" is right by accident for the data home
    and wrong for the config home.
    """
    for name in (paths.DATA_HOME_ENV, "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(env_var, value)

    with pytest.raises(paths.InsecureDataHome) as excinfo:
        resolve()
    assert f"Point {env_var} somewhere else" in str(excinfo.value)


def test_purser_home_cannot_clear_a_config_home_refusal(monkeypatch, tmp_path):
    """The regression: $PURSER_HOME overrides the data home and nothing else.

    Following an instruction to point it elsewhere leaves a config home inside
    the checkout exactly as refused, which is why the message may not give it.
    Setting the config home's own variable is what clears the error.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(REPO_ROOT))
    monkeypatch.setenv(paths.DATA_HOME_ENV, str(tmp_path / "elsewhere"))
    with pytest.raises(paths.InsecureDataHome):
        paths.config_home()

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    assert paths.config_home() == tmp_path / "xdg-config" / paths.APP_DIR


def test_a_relative_xdg_base_is_ignored_rather_than_resolved(monkeypatch, tmp_path):
    """The XDG spec says a relative base is invalid; resolving it would use cwd."""
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.delenv(paths.DATA_HOME_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", "relative/share")

    assert paths.data_home() == tmp_path / "home" / ".local" / "share" / "purser"


# --------------------------------------------------------------------------
# Resolution must be lazy, and isolated before anything is imported
# --------------------------------------------------------------------------


def test_the_isolated_home_was_in_place_before_this_module_was_imported():
    """An autouse fixture would be too late; the root conftest.py is not.

    `HOME_AT_IMPORT` is read at module scope, which pytest evaluates during
    collection -- before any fixture runs. If isolation were left to the autouse
    fixture alone, this would be whatever the developer's machine has.
    """
    assert HOME_AT_IMPORT, (
        "PURSER_HOME was unset when this test module was imported: private state "
        "could have been resolved at import or collection time. The repository-root "
        "conftest.py must set it before collection begins."
    )
    assert Path(HOME_AT_IMPORT).is_absolute()
    assert REPO_ROOT not in Path(HOME_AT_IMPORT).parents


def test_no_module_captured_a_resolved_private_path_at_import(monkeypatch, tmp_path):
    """No module-level constant may freeze a path; resolution happens per call.

    A constant captured at import is invisible to every environment change that
    follows -- including the one this suite relies on -- so it would quietly go on
    pointing wherever the process started.
    """
    import importlib
    import pkgutil

    import purser

    monkeypatch.setenv(paths.DATA_HOME_ENV, str(tmp_path / "second-home"))
    stale = str(HOME_AT_IMPORT)

    offenders = []
    for info in pkgutil.walk_packages(purser.__path__, prefix="purser."):
        module = importlib.import_module(info.name)
        for attribute, value in vars(module).items():
            if attribute.startswith("__") or not isinstance(value, (str, Path)):
                continue
            if stale and stale in str(value):
                offenders.append(f"{info.name}.{attribute} = {value}")

    assert not offenders, (
        "these module attributes froze a private path at import time: "
        + "; ".join(offenders)
    )


def test_a_subprocess_inherits_the_isolated_home(private_home):
    """Isolation is environment, not patched state, so a child process keeps it."""
    result = subprocess.run(
        [sys.executable, "-c",
         "from purser.core import paths; print(paths.database_path())"],
        capture_output=True, text=True, check=True,
        cwd=REPO_ROOT, env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
    )
    assert result.stdout.strip() == str(private_home / "purser.duckdb")


def test_importing_purser_touches_nothing_in_the_real_home(tmp_path):
    """Import must not create, or need, anything under the user's home.

    Run in a child process with a sentinel home and no PURSER_HOME at all, which
    is the shape a developer's machine has before this suite's isolation applies.
    Import the whole package, then assert the sentinel is untouched.
    """
    sentinel = tmp_path / "sentinel-home"
    sentinel.mkdir()

    program = (
        "import importlib, pkgutil, purser\n"
        "for info in pkgutil.walk_packages(purser.__path__, prefix='purser.'):\n"
        "    importlib.import_module(info.name)\n"
        "print('imported')\n"
    )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(sentinel),
        "PYTHONPATH": str(REPO_ROOT / "src"),
    }
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True, text=True, check=True, cwd=REPO_ROOT, env=env,
    )

    assert result.stdout.strip() == "imported"
    assert list(sentinel.iterdir()) == [], (
        f"importing purser created {[p.name for p in sentinel.iterdir()]} under the "
        f"user's home. Import must resolve nothing and create nothing."
    )


def test_ensure_private_dir_makes_every_level_owner_only(tmp_path):
    """Including parents: `mkdir(parents=True, mode=...)` only sets the leaf."""
    import stat

    nested = tmp_path / "outer" / "inner" / "leaf"
    paths.ensure_private_dir(nested)

    for directory in (tmp_path / "outer", tmp_path / "outer" / "inner", nested):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory


def test_ensure_private_dir_leaves_an_existing_directory_alone(tmp_path):
    """It must not widen or narrow a directory the captain already set up."""
    import stat

    existing = tmp_path / "already-here"
    existing.mkdir(mode=0o750)
    paths.ensure_private_dir(existing)

    assert stat.S_IMODE(existing.stat().st_mode) == 0o750
