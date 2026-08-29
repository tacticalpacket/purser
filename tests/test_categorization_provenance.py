"""Precedence must be enforced, not remembered.

The defect this guards against: the captain corrects a category by hand, the
next deterministic rules re-run overwrites it, and nothing records that it
happened. These tests fail if a lower-authority pass can ever win.
"""

from __future__ import annotations

import duckdb
import pytest
import yaml

from purser.core import config
from purser.core.categorize import (
    AUTHORITY,
    UnknownSource,
    assign_category,
    assign_merchant,
    category_id_for_path,
    outranks,
)
from purser.core.importer import import_file


@pytest.fixture
def ledger(con, checking_csv):
    """A ledger with two categories declared and one transaction to label."""
    import_file(con, checking_csv, account_alias="nfcu-checking")
    con.execute(
        "INSERT INTO categories (name, path) VALUES ('coffee', 'food.coffee'), "
        "('subscriptions', 'discretionary.subscriptions')"
    )
    con.execute("INSERT INTO merchants (canonical_name) VALUES ('Bramblewood')")
    txn_id = con.execute(
        "SELECT min(transaction_id) FROM transactions "
        "WHERE description = 'Bramblewood Coffee'"
    ).fetchone()[0]
    return txn_id


def _category(con, txn_id):
    return con.execute(
        "SELECT category_id, category_source, category_rule_key FROM transactions "
        "WHERE transaction_id = ?",
        [txn_id],
    ).fetchone()


def _declined(con, txn_id):
    return con.execute(
        "SELECT category_declined_id, category_declined_source FROM transactions "
        "WHERE transaction_id = ?",
        [txn_id],
    ).fetchone()


def test_the_precedence_table_is_manual_over_rule_over_llm():
    assert outranks("manual", "rule") and outranks("manual", "llm")
    assert outranks("rule", "llm")
    assert not outranks("rule", "manual")
    assert not outranks("llm", "manual") and not outranks("llm", "rule")


def test_equal_authority_may_refresh_itself():
    """A rules re-run must be able to update the row it assigned last time."""
    assert outranks("rule", "rule")
    assert outranks("manual", "manual")
    assert outranks("llm", "llm")


def test_an_unassigned_row_may_be_claimed_by_anything():
    assert outranks("llm", None)


def test_an_unknown_source_is_refused_before_it_touches_a_row(con, ledger):
    with pytest.raises(UnknownSource):
        assign_category(con, ledger, category_id=1, source="vibes")
    assert _category(con, ledger)[1] is None


def test_a_rules_rerun_cannot_clobber_a_manual_correction(con, ledger):
    """The captain's own emphasis, and the whole point of the block."""
    coffee = category_id_for_path(con, "food.coffee")
    subs = category_id_for_path(con, "discretionary.subscriptions")

    assign_category(con, ledger, category_id=subs, source="rule", rule_key="r-1")
    manual = assign_category(con, ledger, category_id=coffee, source="manual")
    assert manual.applied

    rerun = assign_category(con, ledger, category_id=subs, source="rule", rule_key="r-1")
    assert rerun.declined
    assert rerun.previous_source == "manual"

    category_id, source, rule_key = _category(con, ledger)
    assert category_id == coffee
    assert source == "manual"
    assert rule_key is None  # the manual write cleared the stale rule key


def test_an_llm_suggestion_cannot_clobber_a_rule(con, ledger):
    coffee = category_id_for_path(con, "food.coffee")
    subs = category_id_for_path(con, "discretionary.subscriptions")
    assign_category(con, ledger, category_id=coffee, source="rule", rule_key="r-2")

    result = assign_category(con, ledger, category_id=subs, source="llm")
    assert result.declined
    assert _category(con, ledger)[0] == coffee


def test_a_declined_suggestion_is_visible_rather_than_dropped(con, ledger):
    """'The AI wanted to call this something else' must remain answerable."""
    coffee = category_id_for_path(con, "food.coffee")
    subs = category_id_for_path(con, "discretionary.subscriptions")
    assign_category(con, ledger, category_id=coffee, source="manual")
    assign_category(con, ledger, category_id=subs, source="llm")

    declined_id, declined_source = _declined(con, ledger)
    assert declined_id == subs
    assert declined_source == "llm"


def test_an_applied_assignment_records_the_rule_that_fired(con, ledger):
    coffee = category_id_for_path(con, "food.coffee")
    assign_category(con, ledger, category_id=coffee, source="rule", rule_key="coffee-shops")
    assert _category(con, ledger) == (coffee, "rule", "coffee-shops")


def test_a_rules_rerun_refreshes_its_own_assignment(con, ledger):
    coffee = category_id_for_path(con, "food.coffee")
    subs = category_id_for_path(con, "discretionary.subscriptions")
    assign_category(con, ledger, category_id=coffee, source="rule", rule_key="r-3")
    again = assign_category(con, ledger, category_id=subs, source="rule", rule_key="r-4")
    assert again.applied
    assert _category(con, ledger) == (subs, "rule", "r-4")


def test_merchant_assignment_obeys_the_same_precedence(con, ledger):
    merchant_id = con.execute("SELECT merchant_id FROM merchants").fetchone()[0]
    assert assign_merchant(con, ledger, merchant_id=merchant_id, source="manual").applied
    assert assign_merchant(con, ledger, merchant_id=merchant_id, source="rule").declined
    source = con.execute(
        "SELECT merchant_source FROM transactions WHERE transaction_id = ?", [ledger]
    ).fetchone()[0]
    assert source == "manual"


def test_the_schema_refuses_an_assignment_with_no_recorded_source(con, ledger):
    """A category with no source is the state this design exists to prevent."""
    with pytest.raises(duckdb.ConstraintException):
        con.execute(
            "UPDATE transactions SET category_id = 1 WHERE transaction_id = ?",
            [ledger],
        )


def test_a_rule_naming_a_category_outside_the_taxonomy_fails_loudly(con, ledger):
    with pytest.raises(KeyError):
        category_id_for_path(con, "food.not-a-real-category")


# --------------------------------------------------------------------------
# The private overlay must not become a way around the precedence
# --------------------------------------------------------------------------
#
# Corrections now land in `${XDG_CONFIG_HOME:-$HOME/.config}/purser/rules/categories.yaml`
# rather than in the tracked rules file. That moved WHERE a rule is written; it must not
# move what a rule is ALLOWED to do. An overlay rule is still a rule, carries `rule`
# authority and no more, and a manual correction still beats it. If the overlay were
# treated as more authoritative because it is the captain's own file, the failure would
# be exactly the one this module exists to prevent -- a hand correction silently
# overwritten by the next rules run.


def _first_matching_rule(rules, description):
    """The rule that would fire, under first-match-wins over the merged list."""
    for rule in rules:
        if rule["match"] in description.upper():
            return rule
    return None


def test_an_overlay_rule_still_carries_only_rule_authority(con, ledger, tmp_path):
    """The captain's own rules file is not a back door to manual authority."""
    overlay = tmp_path / "rules"
    overlay.mkdir()
    (overlay / "categories.yaml").write_text(
        yaml.safe_dump({"rules": [
            {"match": "BRAMBLEWOOD", "category": "subscriptions_saas",
             "note": "a correction the captain made in his private overlay"},
        ]}),
        encoding="utf-8",
    )
    rules = config.load_category_rules(private_dir=overlay)
    fired = _first_matching_rule(rules, "Bramblewood Coffee")
    assert fired is not None and fired["_layer"] == "private"

    coffee = category_id_for_path(con, "food.coffee")
    subs = category_id_for_path(con, "discretionary.subscriptions")

    assign_category(con, ledger, category_id=coffee, source="manual")
    attempt = assign_category(con, ledger, category_id=subs, source="rule",
                              rule_key=f"{fired['_layer']}:{fired['category']}")

    assert attempt.declined
    assert attempt.previous_source == "manual"
    assert _category(con, ledger)[0] == coffee


def test_an_overlay_rule_beats_a_tracked_rule_without_beating_a_human(con, ledger, tmp_path):
    """Overlay wins over the generic layer; a manual correction wins over both."""
    overlay = tmp_path / "rules"
    overlay.mkdir()
    (overlay / "categories.yaml").write_text(
        yaml.safe_dump({"rules": [
            {"match": "ABC FITNESS", "category": "subscriptions_saas",
             "note": "overrides the generic gym-biller rule"},
        ]}),
        encoding="utf-8",
    )
    rules = config.load_category_rules(private_dir=overlay)

    fired = _first_matching_rule(rules, "ABC FITNESS BILLING")
    assert fired["_layer"] == "private", "the overlay must be evaluated first"
    assert fired["category"] == "subscriptions_saas"

    # Both layers still assign with the same authority, so a human keeps the last word.
    assert outranks("rule", "rule")
    assert not outranks("rule", "manual")


def test_the_only_assignment_sources_are_the_three_the_schema_allows():
    """No 'overlay' or 'private' source crept in beside manual/rule/llm.

    A fourth source would need its own rank, and an unranked one would be a hole in
    the precedence rather than an addition to it.
    """
    assert set(AUTHORITY) == {"manual", "rule", "llm"}
