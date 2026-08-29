"""Fixtures are the one place a real export could pass unnoticed. So: check them.

Both of the repository's existing protections exempt `tests/fixtures/`, and both do it
deliberately -- `.gitignore` re-includes the directory at the bottom of the file, and
`scripts/no-real-data-guard.sh` skips it in the statement-extension rule -- because
sample data has to be committable. Everywhere else, a file that looks like a bank export
is refused for looking like one. Here it is admitted for the same reason.

That is the gap. A real export copied in to reproduce a parse bug clears both layers and
lands in a public repository's history. These tests are the third layer, and the only one
that reads what is actually in the file.

The check is an allowlist of the synthetic markers the fixtures already carry. There is
no denylist of real merchant names, and there will not be: a committed list of where the
captain actually shops is the disclosure this repository is being hardened against.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"

SYNTHETIC_OFX = (
    '<OFX><FI><ORG>SYNTHETIC</ORG><FID>0000</FID></FI>'
    '<BANKACCTFROM><BANKID>000000000</BANKID><ACCTID>0000000000</ACCTID></BANKACCTFROM>'
    '<STMTTRN><FITID>SYNTH0001</FITID><NAME>Saltmarsh Deli</NAME>'
    '<MEMO>SYNTHETIC</MEMO></STMTTRN></OFX>'
)

CSV_HEADER = (
    "Posting Date,Transaction Date,Amount,Credit Debit Indicator,type,Type Group,"
    "Reference,Instructed Currency,Currency Exchange Rate,Instructed Amount,"
    "Description,Category,Check Serial Number,Card Ending,Rewards Total,Rewards Type\n"
)


@pytest.fixture
def checker(script):
    return script("check_fixture_provenance")


@pytest.fixture
def elsewhere(tmp_path):
    """A fixture directory of this test's own, for proving the check bites."""
    directory = tmp_path / "fixtures"
    directory.mkdir()
    return directory


def _csv(directory: Path, name: str, *rows: str) -> Path:
    path = directory / name
    path.write_text(CSV_HEADER + "".join(rows), encoding="utf-8-sig")
    return path


# --------------------------------------------------------------------------
# The invariant, over the fixtures actually in the repository
# --------------------------------------------------------------------------


def test_every_committed_fixture_is_synthetic(checker):
    assert checker.check(FIXTURES) == []


def test_every_statement_shaped_fixture_was_actually_examined(checker):
    """Guard against a green result that checked nothing."""
    examined = [
        path for path in FIXTURES.rglob("*")
        if path.is_file() and path.suffix.lower() in checker.STATEMENT_SUFFIXES
    ]
    assert len(examined) >= 7, f"only {len(examined)} fixtures were in scope"
    assert any(path.suffix == ".ofx" for path in examined)
    assert any(path.suffix == ".csv" for path in examined)


def test_the_ofx_fixture_carries_every_marker(checker):
    text = (FIXTURES / "nfcu-checking-sample.ofx").read_text(encoding="utf-8")
    assert checker._tag_values(text, "ORG") == ["SYNTHETIC"]
    assert set(checker._tag_values(text, "MEMO")) == {"SYNTHETIC"}
    assert all(
        checker.FITID.match(value) for value in checker._tag_values(text, "FITID")
    )
    for tag in ("BANKID", "ACCTID"):
        assert all(
            checker.ZEROED.match(value) for value in checker._tag_values(text, tag)
        )


# --------------------------------------------------------------------------
# ...and proof that it bites, which is the only thing that makes the above mean anything
# --------------------------------------------------------------------------


def test_a_merchant_outside_the_allowlist_is_refused(checker, elsewhere):
    _csv(elsewhere, "leaked.csv",
         "03/12/2025,03/11/2025,42.17,Debit,POS,POS,,,,,"
         "Real Corner Store #4471,Dining,,,,\n")

    violations = checker.check(elsewhere)

    assert len(violations) == 1
    assert "Real Corner Store #4471" in violations[0]
    assert "SYNTHETIC_DESCRIPTIONS" in violations[0]


def test_a_real_looking_card_number_is_refused(checker, elsewhere):
    _csv(elsewhere, "carded.csv",
         "03/12/2025,03/11/2025,42.17,Debit,POS,POS,,,,,"
         "Saltmarsh Deli,Dining,,9182,,\n")

    violations = checker.check(elsewhere)

    assert len(violations) == 1
    assert "not a known placeholder" in violations[0]


def test_an_ofx_naming_a_real_institution_is_refused(checker, elsewhere):
    (elsewhere / "real.ofx").write_text(
        SYNTHETIC_OFX.replace("<ORG>SYNTHETIC</ORG>", "<ORG>A Real Bank</ORG>"),
        encoding="utf-8",
    )

    violations = checker.check(elsewhere)

    assert any("<ORG>" in v for v in violations)


def test_an_ofx_with_a_real_account_number_is_refused(checker, elsewhere):
    (elsewhere / "real.ofx").write_text(
        SYNTHETIC_OFX.replace("<ACCTID>0000000000</ACCTID>",
                              "<ACCTID>3141592653</ACCTID>"),
        encoding="utf-8",
    )

    violations = checker.check(elsewhere)

    assert any("ACCTID" in v and "not zeroed" in v for v in violations)


def test_an_ofx_with_an_institution_issued_fitid_is_refused(checker, elsewhere):
    """A real FITID is the bank's own transaction id, and looks nothing like SYNTH####."""
    (elsewhere / "real.ofx").write_text(
        SYNTHETIC_OFX.replace("<FITID>SYNTH0001</FITID>",
                              "<FITID>20250312001234567890</FITID>"),
        encoding="utf-8",
    )

    violations = checker.check(elsewhere)

    assert any("SYNTH####" in v for v in violations)


def test_an_ofx_row_with_no_synthetic_memo_is_refused(checker, elsewhere):
    (elsewhere / "real.ofx").write_text(
        SYNTHETIC_OFX.replace("<MEMO>SYNTHETIC</MEMO>",
                              "<MEMO>CARD PURCHASE 03/12</MEMO>"),
        encoding="utf-8",
    )

    violations = checker.check(elsewhere)

    assert any("<MEMO>" in v for v in violations)


def test_a_statement_type_with_no_reader_is_a_failure_not_a_pass(checker, elsewhere):
    """An unverifiable fixture must fail loudly rather than sail through."""
    (elsewhere / "statement.pdf").write_bytes(b"%PDF-1.4\n")
    _csv(elsewhere, "fine.csv",
         "03/12/2025,03/11/2025,42.17,Debit,POS,POS,,,,,Saltmarsh Deli,Dining,,,,\n")

    violations = checker.check(elsewhere)

    assert len(violations) == 1
    assert "no way to read it" in violations[0]


def test_a_csv_in_an_unknown_format_is_a_failure_not_a_pass(checker, elsewhere):
    """A future Schwab fixture must teach this script its columns first."""
    (elsewhere / "schwab.csv").write_text(
        "Date,Action,Symbol,Quantity\n03/12/2025,Buy,ZZZZ,10\n", encoding="utf-8"
    )

    violations = checker.check(elsewhere)

    assert len(violations) == 1
    assert "no recognised description column" in violations[0]


def test_an_empty_fixture_directory_is_a_failure(checker, elsewhere):
    """A check that silently examines nothing is worse than no check."""
    assert checker.check(elsewhere) == [
        f"{elsewhere}: holds no statement-shaped fixtures at all"
    ]


def test_there_is_no_denylist_of_real_merchant_names(checker):
    """A settled decision, recorded as a test so it is not quietly revisited.

    A committed list of the places the captain actually shops would itself be the
    disclosure this repository is being hardened against.
    """
    names = [name for name in vars(checker) if "DENY" in name or "BLOCK" in name]
    assert names == [], f"a denylist appeared: {names}"
