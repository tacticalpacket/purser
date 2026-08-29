"""Step 4's contract: importing the same file twice must add nothing the
second time, while still recording that the second import happened."""

from __future__ import annotations

from decimal import Decimal

from purser.core.importer import import_file


def _count(con) -> int:
    return con.execute("SELECT count(*) FROM transactions").fetchone()[0]


def test_second_import_of_the_same_file_inserts_zero_rows(con, checking_csv):
    before = _count(con)
    first = import_file(con, checking_csv, account_alias="nfcu-checking")
    after_first = _count(con)
    second = import_file(con, checking_csv, account_alias="nfcu-checking")
    after_second = _count(con)

    assert before == 0
    assert first.rows_inserted == 10 and after_first == 10
    assert second.rows_inserted == 0 and after_second == 10
    assert second.rows_duplicate == 10
    assert second.was_noop


def test_second_import_still_writes_its_own_import_log_entry(con, checking_csv):
    """An import that adds nothing is a different fact from an import that
    never happened, so it must leave a record."""
    first = import_file(con, checking_csv, account_alias="nfcu-checking")
    second = import_file(con, checking_csv, account_alias="nfcu-checking")

    assert first.import_id != second.import_id
    rows = con.execute(
        "SELECT import_id, rows_inserted, rows_duplicate, status FROM import_log "
        "WHERE source_sha256 = ? ORDER BY import_id",
        [first.source_sha256],
    ).fetchall()
    assert len(rows) == 2
    assert [r[1] for r in rows] == [10, 0]     # inserted
    assert [r[2] for r in rows] == [0, 10]     # duplicate
    assert all(r[3] == "ok" for r in rows)


def test_reimport_advances_last_seen_but_not_first_seen(con, checking_csv):
    first = import_file(con, checking_csv, account_alias="nfcu-checking")
    second = import_file(con, checking_csv, account_alias="nfcu-checking")
    rows = con.execute(
        "SELECT count(*) FROM transactions "
        "WHERE first_import_id = ? AND last_import_id = ?",
        [first.import_id, second.import_id],
    ).fetchone()[0]
    assert rows == 10


def test_repeated_identical_transactions_all_survive(con, checking_csv):
    """The three identical coffee rows must not be deduped into one."""
    import_file(con, checking_csv, account_alias="nfcu-checking")
    count = con.execute(
        "SELECT count(*) FROM transactions WHERE description = 'Bramblewood Coffee'"
    ).fetchone()[0]
    assert count == 3


def test_net_is_unchanged_by_reimporting(con, checking_csv):
    import_file(con, checking_csv, account_alias="nfcu-checking")
    net_once = con.execute("SELECT SUM(amount) FROM transactions").fetchone()[0]
    import_file(con, checking_csv, account_alias="nfcu-checking")
    net_twice = con.execute("SELECT SUM(amount) FROM transactions").fetchone()[0]
    assert Decimal(str(net_once)) == Decimal(str(net_twice)) == Decimal("855.03")


def test_identical_content_in_two_accounts_does_not_collide(con, checking_csv):
    """Dedupe is scoped per account.

    Importing the *same file* under two aliases is the worst case: byte-identical
    content on both sides, which is also what a transfer's two legs look like.
    Both accounts must end up with all ten rows. If identity ignored the account,
    the second import would recognize the first's rows and silently insert
    nothing, losing an entire account's ledger.
    """
    first = import_file(con, checking_csv, account_alias="nfcu-checking")
    second = import_file(con, checking_csv, account_alias="nfcu-cc")

    assert first.rows_inserted == 10
    assert second.rows_inserted == 10
    assert second.rows_duplicate == 0
    assert _count(con) == 20

    per_account = dict(
        con.execute(
            "SELECT a.alias, count(*) FROM transactions t "
            "JOIN accounts a USING (account_id) GROUP BY a.alias"
        ).fetchall()
    )
    assert per_account == {"nfcu-checking": 10, "nfcu-cc": 10}


def test_distinct_accounts_accumulate_independently(con, checking_csv, cc_csv):
    import_file(con, checking_csv, account_alias="nfcu-checking")
    import_file(con, cc_csv, account_alias="nfcu-cc")
    assert _count(con) == 15


def test_provenance_resolves_a_row_to_a_file_and_a_line(con, checking_csv):
    import_file(con, checking_csv, account_alias="nfcu-checking")
    row = con.execute(
        """
        SELECT l.source_filename, l.source_sha256, t.source_line_no
        FROM transactions t JOIN import_log l ON t.first_import_id = l.import_id
        WHERE t.description = 'Quillfeather Books'
        """
    ).fetchone()
    filename, sha256, line_no = row
    assert filename == "nfcu-checking-sample.csv"
    assert len(sha256) == 64
    assert line_no == 6  # header is line 1
