from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from purser.db.database import connect, sync_accounts

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    """Every test resolves private state inside its own `tmp_path`, always.

    Ordinary work must default to synthetic data. Reaching the real corpus is a
    deliberate act -- running the CLI, on the captain's machine, against the
    real home -- and must never be something a worker gets for free by running
    the test suite on a machine that happens to have one.

    The repository-root `conftest.py` already set an isolated home before
    collection, which is the part an autouse fixture cannot do; this narrows it
    further to one directory per test so nothing leaks between them.

    These are real environment variables, not patched module state, so a
    subprocess a test spawns inherits the isolation rather than re-deriving the
    default. `tests/test_private_paths.py` asserts both halves of that.
    """
    data_home = tmp_path / "private-data"
    fake_user_home = tmp_path / "user-home"
    fake_user_home.mkdir()

    monkeypatch.setenv("PURSER_HOME", str(data_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("HOME", str(fake_user_home))
    return data_home


def load_script(name: str):
    """Import one of `scripts/`'s standalone checkers as a module.

    They are deliberately runnable by hand (`scripts/check_*.py`) rather than
    library code, so there is no package to import them from.
    """
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"purser_scripts_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

REGISTRY = [
    {
        "alias": "nfcu-checking",
        "institution": "Navy Federal Credit Union",
        "type": "checking",
        "currency": "USD",
        "balance_sign": "asset",
        "adapter": "nfcu_csv",
    },
    {
        "alias": "nfcu-cc",
        "institution": "Navy Federal Credit Union",
        "type": "credit_card",
        "currency": "USD",
        "balance_sign": "liability",
        "adapter": "nfcu_csv",
    },
]


@pytest.fixture
def con():
    connection = connect()  # in-memory
    sync_accounts(connection, REGISTRY)
    yield connection
    connection.close()


@pytest.fixture
def checking_csv() -> Path:
    return FIXTURES / "nfcu-checking-sample.csv"


@pytest.fixture
def cc_csv() -> Path:
    return FIXTURES / "nfcu-cc-sample.csv"


@pytest.fixture
def malformed_csv() -> Path:
    return FIXTURES / "malformed-sample.csv"


@pytest.fixture
def checking_ofx() -> Path:
    return FIXTURES / "nfcu-checking-sample.ofx"


@pytest.fixture
def window_a_csv() -> Path:
    """First export: 2025-04-01 .. 2025-04-10."""
    return FIXTURES / "nfcu-checking-window-a.csv"


@pytest.fixture
def window_b_csv() -> Path:
    """Second export: 2025-04-06 .. 2025-04-15. Overlaps A by whole days."""
    return FIXTURES / "nfcu-checking-window-b.csv"


@pytest.fixture
def split_early_csv() -> Path:
    """An export cut mid-day inside a group of identical rows."""
    return FIXTURES / "nfcu-checking-split-early.csv"


@pytest.fixture
def split_late_csv() -> Path:
    """The next export, starting mid-day inside that same group."""
    return FIXTURES / "nfcu-checking-split-late.csv"


@pytest.fixture(scope="session")
def script():
    """The `load_script` helper, as a fixture, for tests that check `scripts/`."""
    return load_script
