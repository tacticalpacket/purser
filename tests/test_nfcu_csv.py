"""The adapter's job: survive the export's three traps and get the sign right."""

from __future__ import annotations

from decimal import Decimal

import pytest

from purser.ingest.nfcu_csv import (
    EXPECTED_COLUMNS,
    NfcuParseError,
    count_data_rows,
    parse_file,
    sniff,
)


def test_header_carries_a_bom_and_is_still_read_correctly(checking_csv):
    """The BOM is real; a plain utf-8 read names the first column '﻿Posting Date'."""
    shape = sniff(checking_csv)
    assert shape["has_utf8_bom"] is True
    assert shape["columns"] == EXPECTED_COLUMNS
    assert shape["columns"][0] == "Posting Date"  # not "﻿Posting Date"


def test_every_row_parses(checking_csv):
    result = parse_file(checking_csv, account_alias="nfcu-checking")
    assert len(result.rows) == count_data_rows(checking_csv) == 10
    assert result.rejected == []


def test_debit_is_negative_and_credit_is_positive(checking_csv):
    """The sign convention. Amount is an unsigned magnitude; direction is in
    Credit Debit Indicator."""
    rows = parse_file(checking_csv, account_alias="nfcu-checking").rows
    payroll = next(r for r in rows if r.description == "Payroll Deposit Vulcan")
    coffee = next(r for r in rows if r.description == "Bramblewood Coffee")
    assert payroll.amount == Decimal("2500.00")   # Credit -> value arrived
    assert coffee.amount == Decimal("-42.17")     # Debit  -> value left


def test_net_matches_hand_computed_total(checking_csv):
    """2500.00 credited, 1644.97 debited."""
    result = parse_file(checking_csv, account_alias="nfcu-checking")
    assert result.net_amount == Decimal("855.03")


def test_credit_card_purchase_is_negative_without_a_special_case(cc_csv):
    rows = parse_file(cc_csv, account_alias="nfcu-cc").rows
    purchase = next(r for r in rows if r.description == "Ironvale Hardware")
    payment = next(r for r in rows if r.description == "Payment Thank You")
    assert purchase.amount == Decimal("-76.30")
    assert payment.amount == Decimal("1000.00")


def test_dates_parse_as_month_day_year(checking_csv):
    rows = parse_file(checking_csv, account_alias="nfcu-checking").rows
    row = next(r for r in rows if r.description == "Quillfeather Books")
    # 03/14/2025 is 14 March, not 3 February of some DD/MM reading.
    assert row.posted_date == "2025-03-14"
    assert row.transaction_date == "2025-03-12"  # differs from posting date


def test_reference_column_is_empty_so_fitid_is_none(checking_csv):
    rows = parse_file(checking_csv, account_alias="nfcu-checking").rows
    assert all(r.fitid is None for r in rows)
    # identity therefore falls to the fingerprint
    assert all(r.dedupe_key == r.fingerprint for r in rows)


@pytest.mark.parametrize(
    "needle",
    [
        "Credit Debit Indicator 'Pending'",   # unrecognized indicator
        "is not %m/%d/%Y",                    # ISO date where MM/DD/YYYY required
        "Amount is empty",
        "is negative",                        # pre-signed amount would double-negate
    ],
)
def test_bad_rows_are_refused_rather_than_guessed(malformed_csv, needle):
    result = parse_file(malformed_csv, account_alias="nfcu-checking", strict=False)
    reasons = " | ".join(r.reason for r in result.rejected)
    assert needle in reasons
    # the one good row still parses
    assert len(result.rows) == 1


def test_strict_mode_refuses_the_whole_file(malformed_csv):
    with pytest.raises(NfcuParseError):
        parse_file(malformed_csv, account_alias="nfcu-checking", strict=True)
