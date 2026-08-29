#!/usr/bin/env python3
"""Referential-integrity check for purser's category config.

Fails if any category rule names a category id that isn't in the taxonomy. That is the
one defect that matters in these files: everything else is YAML a human can read.

Configuration is two layers (see src/purser/core/config.py): the tracked generic rules in
`config/`, and the captain's private overlay under
`${XDG_CONFIG_HOME:-$HOME/.config}/purser`. Both are checked by default, because a
broken reference in the overlay breaks categorization just as thoroughly as one here --
and the overlay is the layer that actually gets edited.

Nothing is printed from the overlay's contents. A failure names the file, the rule index
and the unresolved category id; it never echoes a `match` string from the private layer,
because a match string is a real merchant descriptor.

Usage:
    scripts/check_category_refs.py                     # tracked + overlay if present
    scripts/check_category_refs.py --no-private        # tracked layer only
    scripts/check_category_refs.py --private-config DIR
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from purser.core import config, paths  # noqa: E402 - needs the path above


def _rules_with_origin(directory: Path, filename: str, label: str):
    """(index, rule, origin-label) for each rule in one layer's file."""
    path = directory / filename
    if not path.is_file():
        return []
    data = config.read_yaml(path)
    return [(i, rule, label) for i, rule in enumerate(data.get("rules") or [])]


def check(tracked_dir: Path, private_dir: Path | None) -> list[str]:
    """Every unresolved reference, as human-readable strings."""
    errors: list[str] = []

    known_ids = config.category_ids(config.load_taxonomy(tracked_dir))

    layers = [
        _rules_with_origin(
            tracked_dir / "rules", "categories.yaml", "config/rules/categories.yaml"
        )
    ]
    if private_dir is not None:
        layers.append(
            _rules_with_origin(private_dir, "categories.yaml", "private overlay")
        )

    for layer in layers:
        for index, rule, origin in layer:
            cat_id = rule.get("category")
            if cat_id not in known_ids:
                # `match` is quoted only for the tracked layer; it is a real merchant
                # descriptor in the overlay and must not be echoed to a terminal or log.
                where = (
                    f"(match={rule.get('match')!r})"
                    if origin != "private overlay"
                    else ""
                )
                errors.append(
                    f"{origin}: rule #{index} {where}".rstrip()
                    + f" references unknown category {cat_id!r}"
                )

    # Merchant aliases don't reference categories, but each file must at least parse and
    # have the expected shape.
    alias_files = [(tracked_dir / "rules" / "merchants.yaml", "config/rules/merchants.yaml")]
    if private_dir is not None:
        alias_files.append((private_dir / "merchants.yaml", "private overlay"))
    for path, origin in alias_files:
        if not path.is_file():
            continue
        if "aliases" not in config.read_yaml(path):
            errors.append(f"{origin}: missing top-level 'aliases' key")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--tracked-config", type=Path, default=None,
        help="the tracked config/ directory (default: this repository's)",
    )
    parser.add_argument(
        "--private-config", type=Path, default=None,
        help="the private rules directory (default: the overlay's rules/ dir)",
    )
    parser.add_argument(
        "--no-private", action="store_true",
        help="check only the tracked layer, as public CI would",
    )
    args = parser.parse_args(argv)

    tracked_dir = args.tracked_config or (REPO_ROOT / "config")
    if args.no_private:
        private_dir = None
    else:
        private_dir = args.private_config or paths.private_rules_dir()

    errors = check(tracked_dir, private_dir)
    if errors:
        print("category referential-integrity check FAILED:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    known = len(config.category_ids(config.load_taxonomy(tracked_dir)))
    layers = "tracked" if private_dir is None else "tracked + private overlay"
    print(f"OK: {known} categories, all rule references resolved ({layers}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
