"""The account registry loader.

The registry is the join point for everything downstream, and the directory
name under the raw root must match the alias exactly.

It is **not** tracked. Aliases and institution names describe where the captain
actually banks, so the real file lives in the private config overlay
(`purser.core.paths.registry_path`) and the repository ships only
`config/accounts.example.yaml`. `purser.core.config` explains the split.

Every default here resolves lazily, at call time, from the private home --
never from the working directory and never at import time. A module-level
constant holding a resolved path is the exact bug this file used to have:
`Path("config/accounts.yaml")` meant "whatever checkout you are standing in".
"""

from __future__ import annotations

from pathlib import Path

from purser.core import paths
from purser.core.config import load_account_registry


def load_registry(path: str | Path | None = None) -> list[dict]:
    """Load and validate the account registry.

    `path` defaults to the private overlay's `accounts.yaml`, resolved now
    rather than at import.
    """
    return load_account_registry(Path(path) if path is not None else None)


def find_account(accounts: list[dict], alias: str) -> dict:
    for acct in accounts:
        if acct["alias"] == alias:
            return acct
    raise KeyError(f"account alias {alias!r} is not declared in the registry")


def raw_dir(alias: str, data_root: str | Path | None = None) -> Path:
    """The immutable landing directory for an alias."""
    root = Path(data_root) if data_root is not None else paths.raw_root()
    return root / alias


__all__ = ["find_account", "load_registry", "raw_dir"]
