"""Where purser's private state lives: outside every git worktree, always.

The defect this module exists to close was observed, not imagined. Every real
path used to be resolved relative to the process working directory --
``data/purser.duckdb``, ``data/raw``, ``config/accounts.yaml`` -- so "the
ledger" meant "the ledger of whatever checkout you happened to be standing
in". Firstmate hands each task a fresh disposable copy of the repository, and
the captain's real reconciled ledger was found sitting inside one of those
copies: a 7.6 MB file of real transactions in a directory built to be thrown
away, invisible to the check that refuses to discard unlanded work.

So resolution is anchored to the user, never to the tree:

    data home    $PURSER_HOME, else ${XDG_DATA_HOME:-$HOME/.local/share}/purser
    config home  ${XDG_CONFIG_HOME:-$HOME/.config}/purser

Those are the only two rules. There is deliberately **no repo-relative
fallback** -- not even a convenient one -- because a fallback is exactly the
behaviour that put real data in a disposable worktree, and a fallback that
fires silently is worse than an error that does not. There is likewise no
symlink from the repo into the private home: a symlink puts the corpus back
inside the tree for every tool that follows links, which is the thing being
prevented.

Layout under the data home::

    <data home>/raw/<account-alias>/   immutable landing zone for exports
    <data home>/purser.duckdb          the rebuildable ledger
    <data home>/reports/               generated output
    <data home>/backups/

The public repository holds generic configuration; the config home holds the
personalised overlay (see `purser.core.config`).
"""

from __future__ import annotations

import os
from pathlib import Path

#: Overrides the data home entirely. Must be an absolute path.
DATA_HOME_ENV = "PURSER_HOME"

#: The directory name appended to the XDG bases.
APP_DIR = "purser"


class InsecureDataHome(RuntimeError):
    """A configured private home that would defeat the point of having one.

    Raised for a relative ``$PURSER_HOME`` (which would resolve against the
    working directory, reintroducing the original defect) and for a home
    inside the purser checkout this code was loaded from.
    """


def _xdg_base(env_var: str, default: str) -> Path:
    """An XDG base directory, honouring the spec's "must be absolute" rule.

    The XDG spec says a relative value is invalid and must be ignored, which
    is also the safe reading here: a relative base would resolve against the
    working directory.
    """
    value = os.environ.get(env_var, "").strip()
    if value:
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            return candidate
    return Path.home() / default


def _source_checkout() -> Path | None:
    """The git checkout this purser was imported from, if it is one.

    Returns the repository root only when it genuinely holds *this* source
    tree -- a ``.git`` plus a ``src/purser/__init__.py`` that is the module
    now running. Anchoring on both facts keeps the guard from firing on an
    unrelated repository that happens to sit above the package (a dotfiles
    repo at ``$HOME`` being the obvious one). Returns ``None`` for an
    ordinary wheel install, where there is no checkout to be inside.
    """
    here = Path(__file__).resolve()
    marker = here.parents[1] / "__init__.py"  # src/purser/__init__.py
    for candidate in here.parents:
        if not (candidate / ".git").exists():
            continue
        installed = candidate / "src" / "purser" / "__init__.py"
        return candidate if installed == marker else None
    return None


def _reject_if_in_checkout(home: Path, origin: str) -> None:
    checkout = _source_checkout()
    if checkout is None:
        return
    if home == checkout or checkout in home.parents:
        raise InsecureDataHome(
            f"{origin} resolves to {home}, which is inside the purser checkout "
            f"at {checkout}. Private financial state must live outside every "
            f"git worktree -- a worktree is disposable and a copy of it is a "
            f"copy of the ledger. Point {DATA_HOME_ENV} somewhere else."
        )


def data_home() -> Path:
    """The private data home. Absolute, and never inside this checkout."""
    override = os.environ.get(DATA_HOME_ENV, "").strip()
    if override:
        home = Path(override).expanduser()
        if not home.is_absolute():
            raise InsecureDataHome(
                f"{DATA_HOME_ENV}={override!r} is relative. It would resolve "
                f"against the working directory, which is the exact failure "
                f"this setting exists to prevent. Use an absolute path."
            )
        _reject_if_in_checkout(home, DATA_HOME_ENV)
        return home

    home = _xdg_base("XDG_DATA_HOME", ".local/share") / APP_DIR
    _reject_if_in_checkout(home, "XDG_DATA_HOME")
    return home


def config_home() -> Path:
    """The private configuration overlay directory."""
    home = _xdg_base("XDG_CONFIG_HOME", ".config") / APP_DIR
    _reject_if_in_checkout(home, "XDG_CONFIG_HOME")
    return home


def raw_root() -> Path:
    """The immutable landing zone; one directory per account alias below it."""
    return data_home() / "raw"


def database_path() -> Path:
    return data_home() / "purser.duckdb"


def reports_dir() -> Path:
    return data_home() / "reports"


def backups_dir() -> Path:
    return data_home() / "backups"


def registry_path() -> Path:
    """The real account registry, which is personalised and never tracked."""
    return config_home() / "accounts.yaml"


def private_rules_dir() -> Path:
    """Where the captain's own merchant aliases and category rules live."""
    return config_home() / "rules"


def ensure_private_dir(path: Path) -> Path:
    """Create a directory and its parents, owner-only, and return it.

    Owner-only is hygiene, not the boundary. It stops another *user* on the
    machine; it does nothing about a process running as the owner, which is
    every process the captain starts -- including one started inside a checkout
    of an untrusted branch. Isolation is what protects the corpus; see
    AGENTS.md, "Trusted and untrusted code". This is still worth doing, and the
    umask is not to be trusted to do it: `mkdir`'s mode argument is masked by it.
    """
    missing: list[Path] = []
    probe = path
    while not probe.exists():
        missing.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent

    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        os.chmod(directory, 0o700)
    return path


def describe() -> str:
    """Human-readable resolution, for the CLI and for bug reports."""
    return (
        f"data home:   {data_home()}\n"
        f"config home: {config_home()}\n"
        f"raw exports: {raw_root()}\n"
        f"database:    {database_path()}\n"
        f"reports:     {reports_dir()}\n"
        f"backups:     {backups_dir()}\n"
        f"registry:    {registry_path()}\n"
        f"private rules: {private_rules_dir()}"
    )


__all__ = [
    "APP_DIR",
    "DATA_HOME_ENV",
    "InsecureDataHome",
    "backups_dir",
    "config_home",
    "data_home",
    "database_path",
    "describe",
    "ensure_private_dir",
    "private_rules_dir",
    "raw_root",
    "registry_path",
    "reports_dir",
]
