from __future__ import annotations

from pathlib import Path

import pytest

from purser.db.database import connect, sync_accounts

FIXTURES = Path(__file__).parent / "fixtures"

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
