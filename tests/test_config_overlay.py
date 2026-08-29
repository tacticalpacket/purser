"""Generic configuration ships; personalised configuration does not.

`config/rules/merchants.yaml` was designed to accumulate the captain's real merchant
mappings -- its header told a human to add confirmed ones straight into the tracked
file, and "a human correction becomes a rule" is a documented workflow, not an
accident. In a public repository that is a slow leak by working as intended. An alias
is disclosure on its own: "this garbled descriptor is that clinic" says where someone
goes, with no amount or date required.

The file was still empty when the split landed, so nothing had to be cleaned up. These
tests keep it that way, and prove the overlay actually wins so that keeping a mapping
out of git costs nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from purser.core import config, paths
from purser.core.accounts import find_account, load_registry, raw_dir

REPO_ROOT = Path(__file__).resolve().parents[1]
TRACKED_CONFIG = REPO_ROOT / "config"


@pytest.fixture
def overlay(tmp_path):
    """An empty private overlay, and the rules directory inside it."""
    rules = tmp_path / "overlay" / "rules"
    rules.mkdir(parents=True)
    return rules


def _write(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


# --------------------------------------------------------------------------
# What the repository is allowed to contain
# --------------------------------------------------------------------------


def test_the_tracked_merchant_aliases_are_empty():
    """Nothing personal has ever landed here, and nothing may."""
    tracked = config.read_yaml(TRACKED_CONFIG / "rules" / "merchants.yaml")
    assert "aliases" in tracked, "the file must keep its shape even while empty"
    assert not (tracked["aliases"] or {}), (
        "a confirmed merchant mapping was added to the tracked, public rules file. "
        "Confirmed mappings belong in the private overlay -- see the file's header."
    )


def test_the_tracked_rules_point_a_human_at_the_private_overlay():
    """Defect 2's real fix: the loader is not enough if the docs still say 'here'.

    A human following the instructions in these headers must end up editing the
    overlay. If that sentence goes missing, the workflow silently reverts to
    accumulating real merchant names in a public file.
    """
    for name in ("merchants.yaml", "categories.yaml"):
        header = (TRACKED_CONFIG / "rules" / name).read_text(encoding="utf-8")
        assert "XDG_CONFIG_HOME" in header, f"{name} does not name the private overlay"
        assert "purser paths" in header, f"{name} does not say how to find it"


def test_the_real_account_registry_is_not_tracked():
    """Aliases and institutions are account-specific personal configuration."""
    assert not (TRACKED_CONFIG / "accounts.yaml").exists(), (
        "config/accounts.yaml is back in the repository. The real registry belongs in "
        "the private overlay; the tracked copy is accounts.example.yaml."
    )
    assert (TRACKED_CONFIG / "accounts.example.yaml").is_file()


def test_the_example_registry_is_obviously_not_a_real_one():
    example = config.read_yaml(TRACKED_CONFIG / "accounts.example.yaml")
    accounts = config.validate_registry(example, origin="accounts.example.yaml")
    for account in accounts:
        assert account["alias"].startswith("example-"), account["alias"]
        assert "Example" in account["institution"], account["institution"]


# --------------------------------------------------------------------------
# The loader: tracked first, overlay wins
# --------------------------------------------------------------------------


def test_merchant_aliases_merge_with_the_overlay_winning(overlay, tmp_path):
    tracked = tmp_path / "tracked"
    (tracked / "rules").mkdir(parents=True)
    _write(tracked / "rules" / "merchants.yaml",
           {"aliases": {"GENERIC PROCESSOR": "Generic Processor",
                        "SHARED KEY": "From The Tracked Layer"}})
    _write(overlay / "merchants.yaml",
           {"aliases": {"SHARED KEY": "From The Overlay",
                        "PRIVATE ONLY": "Private Merchant"}})

    merged = config.load_merchant_aliases(tracked_dir=tracked, private_dir=overlay)

    assert merged["GENERIC PROCESSOR"] == "Generic Processor"
    assert merged["PRIVATE ONLY"] == "Private Merchant"
    assert merged["SHARED KEY"] == "From The Overlay"


def test_a_missing_overlay_is_simply_the_tracked_layer(tmp_path):
    """A fresh clone with no private config must still load and work."""
    aliases = config.load_merchant_aliases(
        tracked_dir=TRACKED_CONFIG, private_dir=tmp_path / "does-not-exist"
    )
    assert aliases == {}

    rules = config.load_category_rules(
        tracked_dir=TRACKED_CONFIG, private_dir=tmp_path / "does-not-exist"
    )
    assert [rule["match"] for rule in rules] == ["ABC FITNESS", "PARAMOUNT ACCEPTANCE"]
    assert {rule["_layer"] for rule in rules} == {"tracked"}


def test_overlay_category_rules_are_evaluated_first(overlay):
    """'Overlay wins' and 'first match wins' only agree if the overlay leads."""
    _write(overlay / "categories.yaml", {"rules": [
        {"match": "ABC FITNESS", "category": "subscriptions_saas",
         "note": "a correction that must beat the generic rule"},
    ]})

    rules = config.load_category_rules(
        tracked_dir=TRACKED_CONFIG, private_dir=overlay
    )

    assert rules[0]["_layer"] == "private"
    first_match = next(r for r in rules if r["match"] == "ABC FITNESS")
    assert first_match["category"] == "subscriptions_saas"
    assert [r["_layer"] for r in rules[1:]] == ["tracked", "tracked"]


def test_the_taxonomy_has_no_private_overlay():
    """Categories are generic; a private id would break the public integrity check."""
    taxonomy = config.load_taxonomy(TRACKED_CONFIG)
    assert len(config.category_ids(taxonomy)) == 19


# --------------------------------------------------------------------------
# The registry: private only, with no repo-relative fallback
# --------------------------------------------------------------------------


def test_a_missing_registry_fails_loudly_rather_than_falling_back(tmp_path):
    """There must be no convenience path back to a copy inside a checkout."""
    with pytest.raises(config.MissingRegistry) as excinfo:
        load_registry(tmp_path / "nowhere" / "accounts.yaml")
    message = str(excinfo.value)
    assert "accounts.example.yaml" in message, "the error must say how to fix it"


def test_the_registry_defaults_to_the_private_overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    registry = paths.config_home() / "accounts.yaml"
    registry.parent.mkdir(parents=True)
    _write(registry, {"version": 1, "accounts": [
        {"alias": "private-checking", "institution": "Somewhere",
         "type": "checking", "currency": "USD"},
    ]})

    accounts = load_registry()

    assert [a["alias"] for a in accounts] == ["private-checking"]
    assert find_account(accounts, "private-checking")["institution"] == "Somewhere"


def test_a_registry_missing_a_required_field_is_refused(tmp_path):
    path = tmp_path / "accounts.yaml"
    _write(path, {"accounts": [{"alias": "no-institution", "type": "checking"}]})
    with pytest.raises(ValueError, match="institution"):
        load_registry(path)


def test_duplicate_aliases_are_refused(tmp_path):
    path = tmp_path / "accounts.yaml"
    _write(path, {"accounts": [
        {"alias": "twice", "institution": "A", "type": "checking"},
        {"alias": "twice", "institution": "B", "type": "checking"},
    ]})
    with pytest.raises(ValueError, match="duplicate alias"):
        load_registry(path)


def test_the_raw_landing_zone_defaults_to_the_private_home(private_home):
    assert raw_dir("nfcu-checking") == private_home / "raw" / "nfcu-checking"


# --------------------------------------------------------------------------
# The integrity check still works, over both layers
# --------------------------------------------------------------------------


def test_the_integrity_check_passes_over_the_tracked_layer(script):
    check = script("check_category_refs")
    assert check.check(TRACKED_CONFIG, None) == []


def test_the_integrity_check_reads_the_overlay(script, overlay):
    check = script("check_category_refs")
    _write(overlay / "categories.yaml", {"rules": [
        {"match": "SOMETHING", "category": "not_a_real_category"},
    ]})

    errors = check.check(TRACKED_CONFIG, overlay)

    assert len(errors) == 1
    assert "not_a_real_category" in errors[0]


def test_an_integrity_failure_never_echoes_a_private_match_string(script, overlay):
    """A `match` in the overlay is a real merchant descriptor. Naming it in an
    error message would put it in a terminal, a log, or a pasted bug report."""
    check = script("check_category_refs")
    _write(overlay / "categories.yaml", {"rules": [
        {"match": "SQ *REAL LOOKING DESCRIPTOR", "category": "not_a_real_category"},
    ]})

    errors = check.check(TRACKED_CONFIG, overlay)

    assert errors
    assert "REAL LOOKING DESCRIPTOR" not in " ".join(errors)
    assert "rule #0" in errors[0], "it must still say which rule is broken"
