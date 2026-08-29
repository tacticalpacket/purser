"""The CLI is where the defect actually lived, so the CLI is where it is proven fixed.

`cli.py` used to declare `DEFAULT_DB = Path("data/purser.duckdb")` and
`--data-root` defaulting to `Path("data/raw")`. Running `purser ingest` from inside a
checkout therefore wrote the ledger into that checkout. In a Firstmate worker copy --
disposable, deleted when the task ends -- that is how the captain's real reconciled
ledger came to be sitting in a directory built to be thrown away.

These tests run the real command line, from a repository-rooted working directory, and
assert that everything landed in the private home and nothing landed in the repository.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from purser import cli
from purser.core import paths

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def configured(private_home, tmp_path, monkeypatch, checking_csv):
    """A private home with a registry and one synthetic export ready to ingest."""
    registry = paths.config_home() / "accounts.yaml"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        yaml.safe_dump({"version": 1, "accounts": [
            {"alias": "nfcu-checking", "institution": "Example Credit Union",
             "type": "checking", "currency": "USD", "balance_sign": "asset",
             "adapter": "nfcu_csv"},
        ]}),
        encoding="utf-8",
    )

    landing = paths.raw_root() / "nfcu-checking"
    landing.mkdir(parents=True)
    (landing / "2025-03-25_sample.csv").write_bytes(checking_csv.read_bytes())

    # Stand in the repository, which is exactly where the old defaults resolved from.
    monkeypatch.chdir(REPO_ROOT)
    return private_home


def test_ingest_writes_the_ledger_into_the_private_home(configured, capsys):
    assert cli.main(["ingest"]) == 0

    out = capsys.readouterr().out
    assert "inserted=10" in out
    assert (configured / "purser.duckdb").is_file()


def test_ingest_from_inside_the_repository_writes_nothing_into_it(configured):
    before = {p.name for p in REPO_ROOT.iterdir()}

    cli.main(["ingest"])

    assert {p.name for p in REPO_ROOT.iterdir()} == before
    assert not (REPO_ROOT / "data").exists()
    assert not (REPO_ROOT / "reports").exists()


def test_the_landing_zone_default_is_the_private_raw_root(configured, capsys):
    """`--data-root` used to default to `data/raw`, relative to wherever you stood."""
    cli.main(["ingest"])
    assert "2025-03-25_sample.csv" in capsys.readouterr().out


def test_quality_reports_against_the_private_ledger(configured, capsys):
    cli.main(["ingest"])
    assert cli.main(["quality"]) == 0
    assert "nfcu-checking: 10 transactions" in capsys.readouterr().out


def test_paths_prints_the_private_home_and_creates_nothing(configured, capsys):
    assert cli.main(["paths"]) == 0

    out = capsys.readouterr().out
    assert str(configured) in out
    assert str(paths.config_home()) in out
    assert not (REPO_ROOT / "data").exists()


def test_a_missing_registry_is_a_clean_error_not_a_traceback(monkeypatch, tmp_path, capsys):
    """Removing the fallback must produce advice, not a stack trace."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config"))
    monkeypatch.chdir(REPO_ROOT)

    assert cli.main(["quality"]) == 2

    err = capsys.readouterr().err
    assert err.startswith("purser: no account registry at ")
    assert "accounts.example.yaml" in err


def test_nothing_is_created_before_the_registry_error(monkeypatch, tmp_path, private_home):
    """The advice is useless if a half-built ledger was left behind first."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config"))
    monkeypatch.chdir(REPO_ROOT)

    assert cli.main(["quality"]) == 2
    assert not private_home.exists()


def test_a_data_home_pointed_into_the_repository_is_a_clean_error(monkeypatch, capsys):
    """The settled decision -- state never inside the tree -- enforced at the CLI."""
    monkeypatch.setenv(paths.DATA_HOME_ENV, str(REPO_ROOT / "data"))
    monkeypatch.chdir(REPO_ROOT)

    assert cli.main(["paths"]) == 2

    assert "inside the purser checkout" in capsys.readouterr().err
    assert not (REPO_ROOT / "data").exists()


def test_purser_creates_the_private_home_owner_only(private_home, tmp_path,
                                                    monkeypatch, checking_csv, capsys):
    """Hygiene, not the boundary -- but the boundary docs claim it, so it is true.

    Mode 700 stops another *user*; it does nothing about a process running as the
    owner, which is the case AGENTS.md's "Trusted and untrusted code" section is
    actually about. Asserted here so the claim does not quietly become aspirational.

    The home must not exist beforehand: this is about what purser creates, and
    `mkdir`'s mode argument is masked by the umask, so it cannot be trusted alone.
    """
    import stat

    registry = paths.config_home() / "accounts.yaml"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        yaml.safe_dump({"version": 1, "accounts": [
            {"alias": "nfcu-checking", "institution": "Example Credit Union",
             "type": "checking", "currency": "USD", "adapter": "nfcu_csv"},
        ]}),
        encoding="utf-8",
    )
    monkeypatch.chdir(REPO_ROOT)
    assert not private_home.exists()

    assert cli.main(["ingest", "--account", "nfcu-checking",
                     "--file", str(checking_csv)]) == 0

    assert (private_home / "purser.duckdb").is_file()
    mode = stat.S_IMODE(private_home.stat().st_mode)
    assert mode == 0o700, f"{private_home} is {oct(mode)}, not 0o700"
