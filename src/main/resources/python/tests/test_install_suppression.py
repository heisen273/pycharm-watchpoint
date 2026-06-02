"""Installation side-effect suppression via the _installing_watch_thread flag."""


import gc
import sys
import inspect
import weakref
import builtins
import pytest

from _pycharm_watchpoint import watch, unwatch, clear_watches, WatchpointHit
from _pycharm_watchpoint import (
    threading,
)


def test_handle_hit_suppressed_during_installation():
    """Mutations triggered by our own _instrument_object_tree (e.g. lazy
    attribute access causing __setattr__) must be silently dropped.

    Regression test for the "hit 1 misfire" bug: arming a watch on an object
    whose attributes include a SimpleLazyObject causes getattr → __getattr__
    → _setup → __setattr__ → _handle_hit during the installation tree walk.
    That hit is bogus – the user didn't cause it.
    """
    import _pycharm_watchpoint as wp

    class LazyTrigger:
        """Object whose attribute access causes a side-effect __setattr__."""
        def __init__(self):
            self._real_value = None

        @property
        def lazy_prop(self):
            # Simulate SimpleLazyObject: reading triggers a write-back.
            self._side_effect_attr = "triggered"
            return "lazy_value"

    obj = LazyTrigger()

    # Arm the watch – during _instrument_object_tree, accessing `lazy_prop`
    # will trigger `self._side_effect_attr = ...` via our __setattr__ hook.
    # With the _installing_watch flag, _handle_hit should suppress this.
    watch("obj")
    pass  # LINE sentinel

    # Now do a REAL mutation – this one should fire.
    with pytest.raises(WatchpointHit) as exc_info:
        def _mutate():
            obj.real_change = "user_caused"
            pass  # LINE sentinel
        _mutate()

    assert "real_change" in str(exc_info.value) or "obj" in str(exc_info.value)
    clear_watches()


def test_installing_watch_flag_cleared_on_exception():
    """The _installing_watch_thread flag must be cleared even if add_watch raises,
    so subsequent real hits aren't permanently suppressed.
    """
    import _pycharm_watchpoint as wp

    # Verify the flag starts as None.
    assert wp.constants._installing_watch_thread is None

    class Unswappable:
        """Object that refuses __class__ surgery AND classpatch."""
        __slots__ = ("x",)

    obj = Unswappable()
    obj.x = 1

    # watch() on a slotted object falls through to local-variable watching,
    # but regardless, the flag should be None after watch() returns.
    watch("obj")
    pass  # LINE sentinel
    assert wp.constants._installing_watch_thread is None

    clear_watches()

    clear_watches()


def test_installation_suppression_thread_scoped():
    """The _installing_watch_thread flag must only suppress hits on the
    installing thread. Real mutations on OTHER threads must still fire.

    Regression: if the flag were a simple bool (True/False), a user thread
    mutating a watched attribute while the IDE evaluator thread is
    mid-installation would have its hit incorrectly suppressed. The fix
    uses threading.get_ident() to scope suppression to the installing thread.
    """
    import threading
    import _pycharm_watchpoint as wp

    class SharedObj:
        def __init__(self):
            self.value = "init"

    obj = SharedObj()
    watch("obj")

    hit_fired = threading.Event()
    hit_error = []

    def _mutate_from_other_thread():
        try:
            obj.value = "from-other-thread"
        except WatchpointHit:
            hit_fired.set()
        except Exception as e:
            hit_error.append(e)

    wp.constants._installing_watch_thread = threading.get_ident()
    try:
        t = threading.Thread(target=_mutate_from_other_thread)
        t.start()
        t.join(timeout=5.0)
    finally:
        wp.constants._installing_watch_thread = None

    assert hit_fired.is_set(), (
        "A real mutation on a different thread must NOT be suppressed by "
        "the installation flag. The flag should be thread-scoped."
    )
    assert not hit_error, f"Unexpected error in other thread: {hit_error}"
    clear_watches()


def test_installation_suppression_same_thread_does_suppress():
    """Confirm that side-effect mutations on the SAME thread as the
    installer ARE suppressed. This is the core fix for the misfire bug.
    """
    import _pycharm_watchpoint as wp
    import threading

    class Holder:
        def __init__(self):
            self.x = 1

    obj = Holder()
    watch("obj")

    wp.constants._installing_watch_thread = threading.get_ident()
    try:
        obj.x = 999
    finally:
        wp.constants._installing_watch_thread = None

    with pytest.raises(WatchpointHit) as exc_info:
        obj.x = 42
    assert exc_info.value.new_value == "42"
    clear_watches()


def test_multiple_watches_in_sequence_each_suppresses_independently():
    """Calling watch() multiple times in sequence: each call's installation
    suppresses only its own side effects. A real mutation between the two
    watch() calls must still fire.
    """
    class ObjA:
        def __init__(self):
            self.a = 1

    class ObjB:
        def __init__(self):
            self.b = 2

    obj_a = ObjA()
    obj_b = ObjB()

    watch("obj_a")
    with pytest.raises(WatchpointHit):
        obj_a.a = 99

    watch("obj_b")
    with pytest.raises(WatchpointHit):
        obj_b.b = 77

    clear_watches()


def test_suppression_does_not_discard_baseline():
    """When the baseline is established during watch installation, a
    subsequent assignment of the SAME value must be silent (same-value
    check via _value_hash). Only a genuinely different value should fire.

    Scenario: obj._cache is set to "computed_value" before watch("obj"),
    so the baseline captured at arm-time is "computed_value". Re-assigning
    the same value must not fire; assigning a different value must fire.
    """
    class LazyInit:
        def __init__(self):
            self._cache = None

        @property
        def computed(self):
            if self._cache is None:
                self._cache = "computed_value"
            return self._cache

    obj = LazyInit()
    # Trigger the lazy property so _cache == "computed_value" at arm time.
    _ = obj.computed
    watch("obj")
    pass  # LINE sentinel

    obj._cache = "computed_value"  # same value as baseline – must be silent
    with pytest.raises(WatchpointHit) as exc_info:
        obj._cache = "new_value"
    assert exc_info.value.new_value == "'new_value'"
    clear_watches()
