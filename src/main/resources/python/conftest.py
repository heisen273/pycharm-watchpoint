"""pytest configuration for watchpoint tests.

Imports watchpoint at session start (activates sys.monitoring tool ID),
then resets all active watches between every test to prevent cross-test
contamination.
"""
import os
import sys

import builtins
import pytest

# Ensure this directory (which holds the `_pycharm_watchpoint` package) is on
# sys.path so the themed test modules under tests/ can import it even though
# pytest prepends the test files' own directory, not this one.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def pytest_sessionstart(session) -> None:
    """Boot the watchpoint monitoring at the start of the test session."""
    import _pycharm_watchpoint as watchpoint  # noqa: F401 – side-effect: registers sys.monitoring callbacks
    # Enable stderr output for all tests so capsys-based assertions can catch
    # [WATCHPOINT] lines. In production, this is controlled by PYCHARM_WATCHPOINT_LOG.
    watchpoint.constants._WATCHPOINT_LOG = True


@pytest.fixture(autouse=True)
def reset_watchpoint_state():
    """Clear all watches and per-frame state before each test.

    Without this, a watch set in test A would still be active in test B,
    causing false WatchpointHit exceptions on unrelated variable changes.
    """
    registry = builtins._watchpoint_registry
    registry.clear_watches()
    registry._frame_state.clear()
    yield
    # Post-test cleanup – ensures a failing test doesn't corrupt later ones.
    registry.clear_watches()
    registry._frame_state.clear()
