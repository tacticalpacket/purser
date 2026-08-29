"""Root conftest: the test suite may never resolve the captain's real data home.

This file exists at the repository root, not under `tests/`, and does its work
at **module import time**, because that is the earliest moment available. The
ordering matters and is the whole point:

    root conftest.py imported   <- here
    pytest_configure
    collection
    tests/conftest.py imported
    test modules imported
    fixtures (autouse included) run

An autouse fixture is not sufficient on its own. It runs after collection and
after every test module has been imported, so anything that resolves a path at
import time -- a module-level constant, a plugin, a collection hook, a
subprocess spawned during collection -- would resolve the *real* home before
the fixture ever applied. Setting the environment here closes that window, and
`tests/test_private_paths.py` asserts the window is closed by checking, at its
own import time, that the isolated home is already in place.

Because these are real environment variables rather than patched module state,
any subprocess a test spawns inherits the isolated home instead of re-deriving
the default. `tests/test_private_paths.py` asserts that too.

`tests/conftest.py` additionally re-points the data home at each test's own
`tmp_path`, so tests do not share state. This file is the floor, not the whole
guarantee.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

# Kept so pytest_unconfigure can remove exactly what was created here.
_SESSION_HOME: str | None = None


def _isolate() -> str:
    """Point every private-state lookup at a throwaway directory."""
    global _SESSION_HOME
    if _SESSION_HOME is None:
        _SESSION_HOME = tempfile.mkdtemp(prefix="purser-test-home-")

    root = Path(_SESSION_HOME)
    # PURSER_HOME wins outright; the XDG bases and HOME are set as well so that
    # nothing reaches the real home through a fallback branch, and so a
    # subprocess started without PURSER_HOME still lands somewhere disposable.
    os.environ["PURSER_HOME"] = str(root / "data")
    os.environ["XDG_DATA_HOME"] = str(root / "xdg-data")
    os.environ["XDG_CONFIG_HOME"] = str(root / "xdg-config")
    os.environ["HOME"] = str(root / "home")
    (root / "home").mkdir(parents=True, exist_ok=True)
    return _SESSION_HOME


_isolate()


def pytest_configure(config):  # noqa: ARG001 - pytest hook signature
    """Reaffirm the isolation before collection, in case anything cleared it."""
    _isolate()


def pytest_unconfigure(config):  # noqa: ARG001 - pytest hook signature
    if _SESSION_HOME:
        shutil.rmtree(_SESSION_HOME, ignore_errors=True)
