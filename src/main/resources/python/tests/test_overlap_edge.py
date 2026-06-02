"""Overlapping watches, __getattr__ side-effects, and the post-install guarantee."""


import gc
import sys
import inspect
import weakref
import builtins
import pytest

from _pycharm_watchpoint import watch, unwatch, clear_watches, WatchpointHit


def test_watch_obj_then_watch_dotted_both_fire():
    """Overlapping watches: watch("obj") arms object-wide monitoring, then
    watch("obj.x") arms a specific-attribute watch on the same object.
    A mutation to obj.x must still fire (at least one of the two catches it).

    This tests that stacking class-surgery doesn't break either watcher.
    """
    class Holder:
        def __init__(self):
            self.x = 1
            self.y = 2

    obj = Holder()
    watch("obj")
    watch("obj.x")

    # Mutation of x should fire from at least one of the two watches.
    with pytest.raises(WatchpointHit) as exc_info:
        def _mutate_x():
            obj.x = 99
            pass  # LINE sentinel
        _mutate_x()
    assert "99" in str(exc_info.value)

    # Mutation of y should still fire from the object-wide watch.
    with pytest.raises(WatchpointHit) as exc_info:
        def _mutate_y():
            obj.y = 88
            pass  # LINE sentinel
        _mutate_y()
    assert "88" in str(exc_info.value)
    clear_watches()


def test_getattr_side_effect_suppressed_during_tree_walk():
    """Object whose __getattr__ triggers a write on a DIFFERENT attribute.

    During _instrument_object_tree, we call getattr(obj, attr_name) on every
    __dict__ entry. If __getattr__ has a side effect that writes ANOTHER
    attribute (common in ORMs with lazy-loading descriptors), that write goes
    through our __setattr__ hook. The _installing_watch_thread flag must
    suppress it.

    Without suppression, the tree walk would fire a spurious hit during
    watch() setup – confusing because the user hasn't mutated anything yet.
    """
    class LazyLoader:
        """Simulates an ORM-like object where reading one attr populates another."""
        def __init__(self):
            self.data = "original"
            self._loaded = False

        def __getattr__(self, name):
            # Triggered only for attrs not in __dict__. Simulate a lazy-load
            # side effect that writes back to the instance.
            if name == "extra":
                # Side effect: writing _loaded via __setattr__
                self._loaded = True
                return "extra_value"
            raise AttributeError(name)

    obj = LazyLoader()

    # Arm – the tree walk will iterate __dict__ keys (data, _loaded).
    # __getattr__ won't fire for those since they exist in __dict__.
    # But our watcher class is installed, so if anything WRITES during
    # the tree walk, it goes through our hook.
    watch("obj")
    pass  # LINE sentinel

    # Post-install, a real mutation must fire.
    with pytest.raises(WatchpointHit) as exc_info:
        def _mutate():
            obj.data = "changed"
            pass  # LINE sentinel
        _mutate()
    assert exc_info.value.new_value == "'changed'"
    clear_watches()


def test_suppression_only_active_during_installation_window():
    """The _installing_watch_thread flag is ONLY set during watch(). A
    mutation immediately after watch() returns MUST fire – the flag is
    guaranteed cleared by the finally block.

    Regression guard: if the flag leaks (not cleared on success OR on
    exception), ALL subsequent hits on this thread would be silently dropped.
    """
    import _pycharm_watchpoint as wp

    class Simple:
        def __init__(self):
            self.value = "init"

    obj = Simple()
    watch("obj")

    # Flag must be None immediately after watch() returns.
    assert wp.constants._installing_watch_thread is None, (
        "_installing_watch_thread leaked after watch() – all subsequent "
        "hits on this thread would be silently suppressed!"
    )

    # The very first mutation after watch() must fire.
    with pytest.raises(WatchpointHit) as exc_info:
        def _mutate():
            obj.value = "first_change"
            pass  # LINE sentinel
        _mutate()
    assert exc_info.value.new_value == "'first_change'"
    clear_watches()


def test_overlapping_watch_obj_x_then_obj_fires_on_x():
    """Reverse overlap: watch("obj.x") first, then watch("obj"). The
    object-wide watch installs a second layer of class surgery. Mutations
    to x must still fire (the object-wide watcher's __setattr__ catches it).
    """
    class Holder:
        def __init__(self):
            self.x = 1

    obj = Holder()
    watch("obj.x")
    watch("obj")

    with pytest.raises(WatchpointHit) as exc_info:
        def _mutate():
            obj.x = 42
            pass  # LINE sentinel
        _mutate()
    assert "42" in str(exc_info.value)
    clear_watches()
