"""pytest configuration for watchpoint tests.

Imports watchpoint at session start (activates sys.monitoring tool ID),
then resets all active watches between every test to prevent cross-test
contamination.
"""
import builtins
import pytest


def pytest_sessionstart(session) -> None:
    """Boot the watchpoint monitoring at the start of the test session."""
    import watchpoint  # noqa: F401 – side-effect: registers sys.monitoring callbacks


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
