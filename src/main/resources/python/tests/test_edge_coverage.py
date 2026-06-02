"""Edge-case coverage: global/nonlocal, generators, shadowing, asyncio, and
issues #2 / #4."""


import gc
import sys
import inspect
import weakref
import builtins
import pytest

from _pycharm_watchpoint import watch, unwatch, clear_watches, WatchpointHit
from _pycharm_watchpoint import (
    _registry,
    sys,
    threading,
)


def test_watch_global_variable():
    """Watching a global variable via the `global` keyword should fire on
    reassignment – the LINE callback resolves names via eval(name, f_globals,
    f_locals) so globals are visible even though they aren't in f_locals.
    """
    global _module_var_for_global_test
    _module_var_for_global_test = 100

    def _code():
        global _module_var_for_global_test
        watch("_module_var_for_global_test")
        _module_var_for_global_test = 200
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.old_value == "100"
    assert exc_info.value.new_value == "200"


def test_watch_nonlocal_variable():
    """Watching a nonlocal (closure) variable should fire on reassignment.
    The variable lives in the enclosing frame's cell, not in f_locals of the
    inner function – eval() resolves it correctly through the closure.
    """
    def _code():
        counter = 0
        def inner():
            nonlocal counter
            watch("counter")
            counter = 1
            pass
        inner()
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.new_value == "1"


def test_watch_generator_send_fires():
    """Generator.send() rebinds a watched variable at the yield point.
    The LINE event after resumption detects the change because the frame
    resumes normally and LINE events keep firing.
    """
    def gen():
        x = 0
        watch("x")
        x = yield x   # send() provides new value here
        pass           # LINE fires here, detects x: 0 -> 42

    g = gen()
    next(g)
    with pytest.raises((WatchpointHit, StopIteration)) as exc_info:
        g.send(42)
    assert isinstance(exc_info.value, WatchpointHit), "Generator send did not fire WatchpointHit"
    assert exc_info.value.new_value == "42"


def test_watch_variable_shadowing_independent_scopes():
    """Watching 'x' in both outer and inner scopes – each watch is
    frame-scoped (keyed by (name, frame_id)) and fires independently.
    Inner fire is caught, then outer fire propagates up.
    """
    hits = []

    def _code():
        x = 1
        watch("x")
        def inner():
            x = 10
            watch("x")
            x = 20
            pass
        try:
            inner()
        except WatchpointHit as h:
            hits.append(("inner", h.old_value, h.new_value))
        x = 2
        pass

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    # Inner fires first (x: 10 -> 20), then outer fires (x: 1 -> 2)
    assert len(hits) == 1
    assert hits[0] == ("inner", "10", "20")
    assert exc_info.value.old_value == "1"
    assert exc_info.value.new_value == "2"


@pytest.mark.xfail(reason="Known limitation: asyncio coroutine frame not unwound via PY_RETURN on cancel")
def test_asyncio_task_cancellation_cleans_up_watch():
    """When a task is cancelled, the watched frame exits via CancelledError.
    PY_RETURN should fire and clean up the local watch.

    Currently xfail: asyncio task cancellation doesn't reliably trigger
    PY_RETURN for the coroutine frame in all CPython versions, leaving a
    stale entry in _local_watches. In practice this is cleaned by
    clear_watches() on the next debug session start.
    """
    import asyncio
    from _pycharm_watchpoint import _registry

    async def watched_coro():
        x = 1
        watch("x")
        await asyncio.sleep(10)
        x = 2  # never reached

    async def main():
        task = asyncio.create_task(watched_coro())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert len(_registry._local_watches) == 0, "Watch leaked after task cancellation"


def test_object_watch_does_not_prevent_gc():
    """Object watches use weakrefs for obj_ref so the watched object can be
    garbage-collected when user code drops all references. After unwatch(),
    nothing in the registry should keep the object alive.
    """
    import weakref as _weakref

    class Trackable:
        val = 1

    obj = Trackable()
    ref = _weakref.ref(obj)
    watch("obj")
    # The watch is armed. Now unwatch – this should release the registry's
    # reference to obj. Previously obj_ref was a strong reference and would
    # keep obj alive even after unwatch() dropped the _attr_watches entry.
    unwatch("obj")
    del obj
    gc.collect()
    assert ref() is None, "Watched object leaked – registry holds strong ref after unwatch"


def test_object_watch_gc_while_armed():
    """While a watch IS still armed, the registry should NOT prevent GC of the
    watched object (weakref for obj_ref). The _attr_watches entry may remain
    (stale), but it won't pin the object in memory.

    We test this by arming the watch inside a helper frame so the local-variable
    watch gets cleaned by PY_RETURN, leaving only the object-watch entry.
    """
    import weakref as _weakref
    from _pycharm_watchpoint import _registry

    class Trackable:
        val = 1

    holder = [Trackable()]
    ref = _weakref.ref(holder[0])

    def _arm():
        """Arm the watch inside a helper so the local `obj` dies with the frame."""
        obj = holder[0]
        watch("obj")

    try:
        _arm()
    except WatchpointHit:
        pass  # PY_RETURN diff might fire – ignore

    # The local watch was cleaned by PY_RETURN. The object watch entry in
    # _attr_watches holds a weakref to holder[0].
    holder.clear()
    gc.collect()
    assert ref() is None, (
        "Watched object stayed alive via registry strong ref – weakref not working"
    )
    # Cleanup
    unwatch("obj")


def test_watch_property_setter_fires():
    """Watching 'obj.v' where v is a @property must detect assignment via
    the property setter.
    """
    class WithProp:
        def __init__(self):
            self._v = 0
        @property
        def v(self):
            return self._v
        @v.setter
        def v(self, val):
            self._v = val

    obj = WithProp()
    watch("obj.v")
    with pytest.raises(WatchpointHit) as exc_info:
        obj.v = 42
    assert exc_info.value.new_value == "42"


def test_on_line_swallows_non_watchpointhit_exceptions():
    """If _fire_if_changed raises a non-WatchpointHit exception (e.g. from a
    broken pydevd or corrupt state), _on_line must swallow it so sys.monitoring
    doesn't permanently DISABLE LINE events for that code object.

    After the swallowed exception, subsequent changes to the watched variable
    should still fire – proving LINE events weren't disabled.
    """
    from unittest.mock import patch
    from _pycharm_watchpoint import _registry

    call_count = [0]

    original_handle_hit = _registry._handle_hit

    def _exploding_handle_hit(*args, **kwargs):
        """First call raises RuntimeError (simulates pydevd crash), second
        call raises WatchpointHit normally."""
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("Simulated pydevd internal error")
        return original_handle_hit(*args, **kwargs)

    def _code():
        x = 1
        watch("x")
        x = 2    # first change – _handle_hit raises RuntimeError (swallowed)
        pass
        x = 3    # second change – should still fire (LINE not disabled)
        pass

    with patch.object(_registry, '_handle_hit', _exploding_handle_hit):
        with pytest.raises(WatchpointHit) as exc_info:
            _code()

    # The second change DID fire, proving monitoring wasn't disabled
    assert exc_info.value.new_value == "3"
    assert call_count[0] == 2, (
        f"Expected _handle_hit called twice, got {call_count[0]} – "
        f"LINE events may have been disabled after first exception"
    )


def test_watchpointhit_still_propagates_from_on_line():
    """WatchpointHit (the intentional no-pydevd raise) must NOT be swallowed –
    it should propagate out of _on_line so tests and standalone usage still work.
    """
    def _code():
        x = 1
        watch("x")
        x = 2
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_trigger_direct_pause_skips_already_suspended_thread():
    """If the thread is already in STATE_SUSPEND (pydev_state == 2),
    _trigger_direct_pause must skip the do_wait_suspend call to avoid
    crashing pydevd's protocol layer with a double-suspend.

    We mock pydevd to simulate the already-suspended condition and verify
    that do_wait_suspend is NOT called.
    """
    import sys
    import types
    import threading
    from unittest.mock import MagicMock, patch
    from _pycharm_watchpoint import _registry

    # Provide a fake _pydevd_bundle.pydevd_comm_constants module so the
    # import inside _trigger_direct_pause succeeds.
    fake_constants = types.ModuleType("_pydevd_bundle.pydevd_comm_constants")
    fake_constants.CMD_SET_BREAK = 111
    fake_bundle = types.ModuleType("_pydevd_bundle")

    mock_py_db = MagicMock()
    mock_py_db._finish_debugging_session = False

    # Simulate already-suspended: pydevd stores state on thread.additional_info
    current_thread = threading.current_thread()
    mock_info = MagicMock()
    mock_info.pydev_state = 2  # STATE_SUSPEND
    original_info = getattr(current_thread, 'additional_info', None)
    current_thread.additional_info = mock_info

    try:
        with patch.dict(sys.modules, {
            "_pydevd_bundle": fake_bundle,
            "_pydevd_bundle.pydevd_comm_constants": fake_constants,
        }):
            with patch('_pycharm_watchpoint.pydevd_pause._get_pydevd_debugger', return_value=mock_py_db):
                _registry._trigger_direct_pause(None, 1)
    finally:
        # Restore original state
        if original_info is None:
            if hasattr(current_thread, 'additional_info'):
                del current_thread.additional_info
        else:
            current_thread.additional_info = original_info

    # do_wait_suspend must NOT have been called
    mock_py_db.do_wait_suspend.assert_not_called()


def test_trigger_direct_pause_proceeds_when_not_suspended():
    """When thread is NOT already suspended (pydev_state != 2),
    _trigger_direct_pause should call set_suspend + do_wait_suspend.
    """
    import sys
    import types
    import threading
    from unittest.mock import MagicMock, patch
    from _pycharm_watchpoint import _registry

    fake_constants = types.ModuleType("_pydevd_bundle.pydevd_comm_constants")
    fake_constants.CMD_SET_BREAK = 111
    fake_bundle = types.ModuleType("_pydevd_bundle")

    mock_py_db = MagicMock()
    mock_py_db._finish_debugging_session = False

    # Simulate NOT suspended: pydev_state = 1 (STATE_RUN)
    current_thread = threading.current_thread()
    mock_info = MagicMock()
    mock_info.pydev_state = 1  # STATE_RUN
    original_info = getattr(current_thread, 'additional_info', None)
    current_thread.additional_info = mock_info

    try:
        with patch.dict(sys.modules, {
            "_pydevd_bundle": fake_bundle,
            "_pydevd_bundle.pydevd_comm_constants": fake_constants,
        }):
            with patch('_pycharm_watchpoint.pydevd_pause._get_pydevd_debugger', return_value=mock_py_db):
                _registry._trigger_direct_pause(None, 1)
    finally:
        if original_info is None:
            if hasattr(current_thread, 'additional_info'):
                del current_thread.additional_info
        else:
            current_thread.additional_info = original_info

    # Both set_suspend and do_wait_suspend should have been called
    mock_py_db.set_suspend.assert_called_once()
    mock_py_db.do_wait_suspend.assert_called_once()
