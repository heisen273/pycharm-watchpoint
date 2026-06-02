"""Basic local watching, unwatch/clear, multiple watches, object-attribute
surgery, watch-from-callee, and the zero-overhead guarantee."""


import gc
import sys
import inspect
import weakref
import builtins
import pytest

from _pycharm_watchpoint import watch, unwatch, clear_watches, WatchpointHit
from _pycharm_watchpoint import (
    builtins,
    hit,
    registry,
)

from util import (
    _SampleObj,
    _inner,
)


def test_watch_fires_on_local_change():
    """Changing a watched local variable raises WatchpointHit."""
    def _code():
        x = 1
        watch("x")
        x = 2
        pass  # LINE event here detects change from previous line
    with pytest.raises(WatchpointHit):
        _code()


def test_old_and_new_values_captured():
    """WatchpointHit carries accurate old_value and new_value reprs."""
    def _code():
        x = 42
        watch("x")
        x = 99
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    hit = exc_info.value
    assert hit.old_value == "42", f"expected old_value='42', got {hit.old_value!r}"
    assert hit.new_value == "99", f"expected new_value='99', got {hit.new_value!r}"
    assert hit.watch_name == "x"


def test_source_location_reported():
    """WatchpointHit.source_line points to the assignment line, not the detection line."""
    def _code():
        x = 1
        watch("x")
        x = 2   # assignment line
        pass    # detection triggers here

    # Determine the absolute line number of 'x = 2' inside _code.
    func_source_lines, func_start = inspect.getsourcelines(_code)
    assignment_line = None
    for i, src_line in enumerate(func_source_lines):
        if src_line.strip() == "x = 2   # assignment line":
            assignment_line = func_start + i
            break
    assert assignment_line is not None, "Could not locate 'x = 2' in _code source"

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    hit = exc_info.value
    assert hit.source_line == assignment_line, (
        f"Expected source_line={assignment_line}, got {hit.source_line}"
    )


def test_no_fire_before_watch_is_set():
    """Assigning a variable before watch() is called must not raise."""
    x = 1
    x = 2
    x = 3
    watch("x")   # set AFTER assignments – those should be silent


def test_no_spurious_fire_on_same_value():
    """Re-assigning the same value must not raise WatchpointHit."""
    x = 5
    watch("x")
    x = 5   # same value – must be silent
    x = 5   # same value – must be silent


def test_unwatch_stops_firing():
    """After unwatch(), changes to the variable no longer raise."""
    x = 1
    watch("x")
    unwatch("x")
    x = 2   # should NOT raise
    x = 3   # should NOT raise


def test_clear_watches_stops_all():
    """clear_watches() disarms every active watch."""
    x = 1
    y = 100
    watch("x")
    watch("y")
    clear_watches()
    x = 2   # should NOT raise
    y = 200  # should NOT raise


def test_rewatch_after_unwatch():
    """watch() can be called again after unwatch() and fires correctly."""
    def _code():
        x = 1
        watch("x")
        unwatch("x")
        x = 2   # silent – LINE events disabled by unwatch
        watch("x")  # re-watch: x=2 is the new baseline
        x = 3   # should trigger WatchpointHit
        pass    # detection fires here
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.old_value == "2"
    assert exc_info.value.new_value == "3"


def test_multiple_watches_independent():
    """Two watches: only the changed variable fires, with the right name."""
    def _code():
        x = 1
        y = 100
        watch("x")
        watch("y")
        x = 2   # fires WatchpointHit for x
        pass    # detection here
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.watch_name == "x"


def test_second_watch_fires_independently():
    """Two watches each fire independently when their variable changes."""
    def _code_x():
        x = 1
        watch("x")
        x = 2
        pass
    def _code_y():
        y = 100
        watch("y")
        y = 200
        pass

    with pytest.raises(WatchpointHit) as exc_info:
        _code_x()
    assert exc_info.value.watch_name == "x"
    assert exc_info.value.new_value == "2"

    with pytest.raises(WatchpointHit) as exc_info:
        _code_y()
    assert exc_info.value.watch_name == "y"
    assert exc_info.value.new_value == "200"


def test_watch_object_attribute_fires():
    """watch('obj.val') raises WatchpointHit when obj.val changes."""
    obj = _SampleObj(0)
    watch("obj.val")
    with pytest.raises(WatchpointHit) as exc_info:
        obj.val = 99
    hit = exc_info.value
    assert hit.watch_name == "obj.val"
    assert hit.new_value == "99"


def test_watch_attribute_old_value():
    """WatchpointHit for attribute change carries the correct old value."""
    obj = _SampleObj(10)
    watch("obj.val")
    with pytest.raises(WatchpointHit) as exc_info:
        obj.val = 20
    assert exc_info.value.old_value == "10"


def test_watch_attribute_no_fire_on_same_value():
    """Setting an attribute to the same value must not raise."""
    obj = _SampleObj(7)
    watch("obj.val")
    obj.val = 7   # same value – must be silent


def test_unwatch_attribute_stops_firing():
    """After unwatch('obj.val'), attribute changes are silent."""
    obj = _SampleObj(1)
    watch("obj.val")
    unwatch("obj.val")
    obj.val = 99   # should NOT raise


def test_unwatch_attribute_restores_class():
    """After unwatch, obj's class should be restored to the original."""
    obj = _SampleObj(1)
    original_cls = type(obj)
    watch("obj.val")
    unwatch("obj.val")
    assert type(obj) is original_cls, (
        f"Expected class {original_cls}, got {type(obj)}"
    )


def test_watch_inside_called_function():
    """watch() and the resulting WatchpointHit both work inside a called function."""
    with pytest.raises(WatchpointHit) as exc_info:
        _inner(None)
    assert exc_info.value.watch_name == "z"


def test_no_monitoring_events_before_watch():
    """sys.monitoring should have no active local events before watch() is called."""
    registry = builtins._watchpoint_registry
    assert len(registry._local_watches) == 0, "Registry should be empty between tests"
    assert len(registry._attr_watches) == 0, "Attr registry should be empty between tests"
