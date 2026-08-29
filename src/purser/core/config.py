"""Two configuration layers: the generic one that ships, the personal one that does not.

purser's repository is public. Its configuration is not all of one kind, and
the split is the point:

**Tracked and public** -- `config/` in the source tree. The category taxonomy,
deterministic rules that came from `docs/AUDIT-PLAN.md` rather than from anyone's
statement, and `config/accounts.example.yaml`. Nothing here describes a real
person's spending.

**Private overlay** -- `${XDG_CONFIG_HOME:-$HOME/.config}/purser`, resolved by
`purser.core.paths`. The real `accounts.yaml`, and `rules/merchants.yaml` /
`rules/categories.yaml` holding the mappings the captain confirmed against his
own statements. A merchant alias *is* disclosure: "this garbled descriptor is
that gym" says he goes to that gym. Those files are personal data and live
outside the repository, permanently.

This matters more than it looks. `config/rules/merchants.yaml` was designed to
accumulate exactly that material -- its own header used to instruct a human to
add confirmed real mappings straight into the tracked file -- so publishing the
repository without this split would have turned a documented workflow into a
slow leak. The file was still empty when the split landed; nothing had to be
cleaned up, and nothing can accumulate there now.

Merge rule: **the tracked layer loads first, the private overlay wins.**

- Merchant aliases are a mapping, so an overlay key replaces a tracked key.
- Category rules are a list evaluated first-match-wins, so the overlay's rules
  are placed *ahead* of the tracked ones. "Overlay wins" and "first match wins"
  only agree if the overlay goes first.
- The taxonomy (`config/categories.yaml`) has no overlay. It is generic by
  construction, and a private category id would be a rule reference the public
  integrity check could not resolve.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from purser.core import paths

#: Escape hatch for an installation whose source tree is not a checkout.
TRACKED_CONFIG_ENV = "PURSER_TRACKED_CONFIG"


class MissingRegistry(FileNotFoundError):
    """No private account registry, and there is deliberately no default."""


def tracked_config_dir() -> Path:
    """The public `config/` directory that ships with the source."""
    override = os.environ.get(TRACKED_CONFIG_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    # .../src/purser/core/config.py -> .../src/purser/core -> purser -> src -> root
    return Path(__file__).resolve().parents[3] / "config"


def read_yaml(path: Path) -> dict:
    """Parse a YAML mapping, treating a missing or empty file as `{}`."""
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_taxonomy(tracked_dir: Path | None = None) -> dict:
    """The category taxonomy. Tracked only -- see the module docstring."""
    directory = tracked_dir or tracked_config_dir()
    path = directory / "categories.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"category taxonomy not found at {path}")
    return read_yaml(path)


def category_ids(taxonomy: dict) -> set[str]:
    """Every category id in the taxonomy. Ids are unique across all groups."""
    ids: set[str] = set()
    for group_key, group in (taxonomy.get("groups") or {}).items():
        for category in (group or {}).get("categories") or []:
            cat_id = category.get("id")
            if not cat_id:
                raise ValueError(f"group {group_key!r} has a category with no id")
            if cat_id in ids:
                raise ValueError(f"category id {cat_id!r} is defined more than once")
            ids.add(cat_id)
    return ids


def load_merchant_aliases(
    tracked_dir: Path | None = None, private_dir: Path | None = None
) -> dict[str, str]:
    """Merchant normalization aliases, overlay keys replacing tracked keys."""
    tracked = tracked_dir or tracked_config_dir()
    private = private_dir if private_dir is not None else paths.private_rules_dir()

    aliases: dict[str, str] = {}
    for source in (tracked / "rules" / "merchants.yaml", private / "merchants.yaml"):
        aliases.update(read_yaml(source).get("aliases") or {})
    return aliases


def load_category_rules(
    tracked_dir: Path | None = None, private_dir: Path | None = None
) -> list[dict]:
    """Category rules, private first so the overlay wins under first-match-wins.

    Each rule is tagged with `_layer` ("private" or "tracked") so a caller can
    report which layer fired without having to re-read the files, and so a
    diagnostic can name a layer without quoting the rule that came from it.
    """
    tracked = tracked_dir or tracked_config_dir()
    private = private_dir if private_dir is not None else paths.private_rules_dir()

    rules: list[dict] = []
    for layer, source in (
        ("private", private / "categories.yaml"),
        ("tracked", tracked / "rules" / "categories.yaml"),
    ):
        for rule in read_yaml(source).get("rules") or []:
            rules.append({**rule, "_layer": layer})
    return rules


def load_account_registry(path: Path | None = None) -> list[dict]:
    """The real account registry, which lives only in the private overlay.

    There is no tracked fallback on purpose. `config/accounts.example.yaml` is
    a template to copy, not a default to inherit: silently ingesting against
    example aliases would put real exports under a fabricated account name.
    """
    registry = Path(path) if path is not None else paths.registry_path()
    if not registry.is_file():
        raise MissingRegistry(
            f"no account registry at {registry}. Copy the template and edit it:\n"
            f"  mkdir -p {registry.parent}\n"
            f"  cp {tracked_config_dir() / 'accounts.example.yaml'} {registry}\n"
            f"It is not tracked: account aliases and institutions are personal."
        )
    return validate_registry(read_yaml(registry), origin=registry)


def validate_registry(data: dict, origin: Path | str = "<registry>") -> list[dict]:
    """Shape-check a parsed registry and return its account list."""
    accounts = (data or {}).get("accounts") or []
    if not accounts:
        raise ValueError(f"{origin} declares no accounts")

    seen: set[str] = set()
    for acct in accounts:
        for required in ("alias", "institution", "type"):
            if not acct.get(required):
                raise ValueError(f"{origin}: account entry missing {required!r}: {acct!r}")
        alias = acct["alias"]
        if alias in seen:
            raise ValueError(f"{origin}: duplicate alias {alias!r}")
        seen.add(alias)
    return accounts


__all__ = [
    "TRACKED_CONFIG_ENV",
    "MissingRegistry",
    "category_ids",
    "load_account_registry",
    "load_category_rules",
    "load_merchant_aliases",
    "load_taxonomy",
    "read_yaml",
    "tracked_config_dir",
    "validate_registry",
]
