#!/usr/bin/env python3
"""Referential-integrity check for purser's category config.

Parses config/categories.yaml and config/rules/{categories,merchants}.yaml, and fails
if any rule names a category id that isn't in the taxonomy. This is the one defect that
matters in these files: everything else is just YAML a human can read directly.

Usage: scripts/check_category_refs.py
"""

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CATEGORIES_PATH = REPO_ROOT / "config" / "categories.yaml"
CATEGORY_RULES_PATH = REPO_ROOT / "config" / "rules" / "categories.yaml"
MERCHANT_RULES_PATH = REPO_ROOT / "config" / "rules" / "merchants.yaml"


def load_yaml(path: Path):
    with path.open() as f:
        return yaml.safe_load(f)


def known_category_ids(taxonomy: dict) -> set[str]:
    ids = set()
    for group_key, group in (taxonomy.get("groups") or {}).items():
        for category in group.get("categories") or []:
            cat_id = category.get("id")
            if not cat_id:
                raise ValueError(f"group {group_key!r} has a category with no id")
            if cat_id in ids:
                raise ValueError(f"category id {cat_id!r} is defined more than once")
            ids.add(cat_id)
    return ids


def main() -> int:
    errors = []

    taxonomy = load_yaml(CATEGORIES_PATH)
    known_ids = known_category_ids(taxonomy)

    category_rules = load_yaml(CATEGORY_RULES_PATH) or {}
    for i, rule in enumerate(category_rules.get("rules") or []):
        cat_id = rule.get("category")
        if cat_id not in known_ids:
            errors.append(
                f"{CATEGORY_RULES_PATH.relative_to(REPO_ROOT)}: rule #{i} "
                f"(match={rule.get('match')!r}) references unknown category {cat_id!r}"
            )

    # Merchant aliases don't reference categories, but their file must at least parse
    # and have the expected shape.
    merchant_aliases = load_yaml(MERCHANT_RULES_PATH) or {}
    if "aliases" not in merchant_aliases:
        errors.append(
            f"{MERCHANT_RULES_PATH.relative_to(REPO_ROOT)}: missing top-level 'aliases' key"
        )

    if errors:
        print("category referential-integrity check FAILED:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print(f"OK: {len(known_ids)} categories, all rule references resolved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
