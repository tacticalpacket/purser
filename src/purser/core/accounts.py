"""The config/accounts.yaml registry loader.

DESIGN.md: the registry is the join point for everything downstream, and the
directory name under data/raw/ must match the alias exactly.
"""

from __future__ import annotations

from pathlib import Path

import yaml

DEFAULT_REGISTRY = Path("config/accounts.yaml")


def load_registry(path: str | Path = DEFAULT_REGISTRY) -> list[dict]:
    """Load and validate the account registry."""
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    accounts = data.get("accounts") or []
    if not accounts:
        raise ValueError(f"{path} declares no accounts")

    seen: set[str] = set()
    for acct in accounts:
        for required in ("alias", "institution", "type"):
            if not acct.get(required):
                raise ValueError(f"{path}: account entry missing {required!r}: {acct!r}")
        alias = acct["alias"]
        if alias in seen:
            raise ValueError(f"{path}: duplicate alias {alias!r}")
        seen.add(alias)
    return accounts


def find_account(accounts: list[dict], alias: str) -> dict:
    for acct in accounts:
        if acct["alias"] == alias:
            return acct
    raise KeyError(f"account alias {alias!r} is not declared in the registry")


def raw_dir(alias: str, data_root: str | Path = "data/raw") -> Path:
    """The immutable landing directory for an alias."""
    return Path(data_root) / alias
