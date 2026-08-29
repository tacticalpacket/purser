"""The dedupe case the occurrence index actually exists for.

Importing the same bytes twice is the easy half of idempotency. The real shape
of monthly use is two DIFFERENT exports whose windows overlap: the second file
re-reports days the first already covered, and inside that overlap sit rows
that are byte-identical to each other but are distinct real transactions.

A naive content hash gets this wrong in both directions at once -- it re-adds
the overlap because the rows came from a new file, or it collapses the
identical pair because they hash the same. The occurrence-index fingerprint
has to do neither.

Fixtures:
  window-a  2025-04-01 .. 2025-04-10   7 rows
  window-b  2025-04-06 .. 2025-04-15   7 rows
  overlap   2025-04-06 .. 2025-04-10   4 rows, including an identical pair
            of Marrowgate Transit rows on 04-08
"""

from __future__ import annotations

from decimal import Decimal

from purser.core.importer import import_file


def _count(con) -> int:
    return con.execute("SELECT count(*) FROM transactions").fetchone()[0]


def _import_both(con, a, b):
    first = import_file(con, a, account_alias="nfcu-checking")
    second = import_file(con, b, account_alias="nfcu-checking")
    return first, second


def test_the_second_export_adds_only_its_genuinely_new_rows(
    con, window_a_csv, window_b_csv
):
    first, second = _import_both(con, window_a_csv, window_b_csv)

    assert first.rows_inserted == 7          # the whole first window
    assert second.rows_parsed == 7
    assert second.rows_inserted == 3         # 04-12, 04-14, 04-15 only
    assert _count(con) == 10                 # 7 + 3, not 14


def test_rows_duplicate_accounts_for_the_whole_overlap(
    con, window_a_csv, window_b_csv
):
    """The overlap must be measured, not inferred from what did not insert."""
    _, second = _import_both(con, window_a_csv, window_b_csv)
    assert second.rows_duplicate == 4
    assert second.rows_inserted + second.rows_duplicate == second.rows_parsed


def test_the_identical_pair_in_the_overlap_survives_as_two_rows(
    con, window_a_csv, window_b_csv
):
    """Both directions of the failure at once: not collapsed, not duplicated.

    Two byte-identical Marrowgate Transit rows on 04-08 appear in BOTH exports.
    A content-only hash stores one. A file-scoped identity stores four. Two is
    the only right answer.
    """
    _import_both(con, window_a_csv, window_b_csv)
    count = con.execute(
        "SELECT count(*) FROM transactions WHERE description = 'Marrowgate Transit'"
    ).fetchone()[0]
    assert count == 2


def test_the_overlap_rows_record_the_second_export_as_having_seen_them(
    con, window_a_csv, window_b_csv
):
    """Coverage by the second file must be recorded, not hidden.

    Otherwise the import log says a file was imported while every row it
    covered still points only at the first import, and "which exports cover
    2025-04-08?" becomes unanswerable.
    """
    first, second = _import_both(con, window_a_csv, window_b_csv)

    advanced = con.execute(
        "SELECT count(*) FROM transactions "
        "WHERE first_import_id = ? AND last_import_id = ?",
        [first.import_id, second.import_id],
    ).fetchone()[0]
    assert advanced == 4                     # exactly the overlap

    untouched = con.execute(
        "SELECT count(*) FROM transactions "
        "WHERE first_import_id = ? AND last_import_id = ?",
        [first.import_id, first.import_id],
    ).fetchone()[0]
    assert untouched == 3                    # A's rows outside the overlap


def test_the_net_is_the_union_not_the_sum_of_the_two_files(
    con, window_a_csv, window_b_csv
):
    """The number a double-counted overlap would visibly wreck."""
    _import_both(con, window_a_csv, window_b_csv)
    net = con.execute("SELECT SUM(amount) FROM transactions").fetchone()[0]
    # 1500.00 + 250.00 credited; 54.09 + 9.99 + 88.40 + 6.75 + 6.75 + 31.20
    # + 120.00 + 17.45 debited.
    assert Decimal(str(net)) == Decimal("1415.37")


def test_importing_the_overlapping_pair_in_either_order_agrees(
    con, window_a_csv, window_b_csv
):
    """Order of arrival must not change the ledger, only who inserted what."""
    import_file(con, window_b_csv, account_alias="nfcu-checking")
    second = import_file(con, window_a_csv, account_alias="nfcu-checking")
    assert second.rows_inserted == 3         # 04-01, 04-02, 04-04
    assert second.rows_duplicate == 4
    assert _count(con) == 10
    net = con.execute("SELECT SUM(amount) FROM transactions").fetchone()[0]
    assert Decimal(str(net)) == Decimal("1415.37")


def test_a_boundary_that_splits_identical_rows_undercounts_known_limitation(
    con, split_early_csv, split_late_csv
):
    """The one case the occurrence index cannot survive. Pinned, not fixed.

    Modelled truth: 2025-04-20 really held THREE identical Marrowgate Transit
    transactions. The early export was cut mid-day and caught the first two;
    the late export starts mid-day and carries the third.

    The third row is the first Marrowgate row in its own file, so it is
    numbered occurrence 1, which is already stored. It is therefore read as a
    duplicate and dropped: the ledger ends with two, and the real third
    transaction is lost. An UNDERCOUNT, and a silent one -- no error, and the
    import log reports an ordinary duplicate.

    This is asserted rather than fixed because the fix is a data convention,
    not code: DESIGN.md requires each export to start several full days before
    the previous one ended, so an overlap is always whole days and this
    boundary never falls inside a same-day group. The test exists so the
    limitation is visible here rather than discovered in a wrong report.
    """
    import_file(con, split_early_csv, account_alias="nfcu-checking")
    late = import_file(con, split_late_csv, account_alias="nfcu-checking")

    assert late.rows_inserted == 2           # 04-22 and 04-24 only
    assert late.rows_duplicate == 1          # the genuinely-new 04-20 row

    marrowgate = con.execute(
        "SELECT count(*) FROM transactions WHERE description = 'Marrowgate Transit'"
    ).fetchone()[0]
    assert marrowgate == 2                   # the modelled truth is 3
