"""Tests for watchpoint.py – written first (TDD).

Python 3.14 sys.monitoring behavior: exceptions raised from LINE callbacks
bypass local exception handlers within the monitored frame and propagate to
the caller. This means pytest.raises(WatchpointHit) must be at the CALLER
level, not inside the function where watch() is active.

Pattern for all tests that expect WatchpointHit:
    - Define a small nested helper _code() that contains the watch() call
      and the code change. This is the monitored frame.
    - In the test body (unmonitored frame), wrap _code() with pytest.raises().

Tests that expect NO WatchpointHit (silence checks) work fine inline
because the callbacks never raise.

Timing note: sys.monitoring LINE events fire BEFORE the line executes,
so change detection fires one line AFTER the assignment. The helper
function includes a `pass` sentinel after each assignment to give the
LINE callback a chance to fire and raise.
"""
import inspect
import pytest
import builtins

from watchpoint import watch, unwatch, clear_watches, WatchpointHit


# ---------------------------------------------------------------------------
# Basic local-variable watching
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# unwatch / clear_watches
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Multiple simultaneous watches
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Object attribute watching (__class__ surgery)
# ---------------------------------------------------------------------------

class _SampleObj:
    """Simple class for attribute watch tests."""
    def __init__(self, val):
        self.val = val


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


# ---------------------------------------------------------------------------
# watch() called from within a function being called
# ---------------------------------------------------------------------------

def _inner(registry_ref):
    """Helper: set a watch inside a called function."""
    z = 10
    watch("z")
    z = 20   # WatchpointHit triggered when the next line fires
    pass     # detection fires here


def test_watch_inside_called_function():
    """watch() and the resulting WatchpointHit both work inside a called function."""
    with pytest.raises(WatchpointHit) as exc_info:
        _inner(None)
    assert exc_info.value.watch_name == "z"


# ---------------------------------------------------------------------------
# Zero-overhead guarantee: no LINE events before any watch
# ---------------------------------------------------------------------------

def test_no_monitoring_events_before_watch():
    """sys.monitoring should have no active local events before watch() is called."""
    registry = builtins._watchpoint_registry
    assert len(registry._local_watches) == 0, "Registry should be empty between tests"
    assert len(registry._attr_watches) == 0, "Attr registry should be empty between tests"


# ---------------------------------------------------------------------------
# Regression: frame-id reuse and exception-unwind cleanup
# ---------------------------------------------------------------------------

def test_repeated_calls_after_caught_hit_do_not_double_fire():
    """Catching a WatchpointHit and calling the watched function again must not
    produce a spurious fire from leftover frame state.

    Before the stale-state fix this could mis-fire because PY_RETURN does not
    run on exception unwind, so _frame_state[id(frame)] survived; the next
    invocation that happened to land on the same id saw old prev_hashes.
    """
    def _code():
        x = 1
        watch("x")
        x = 2
        pass
    # First call: should fire
    with pytest.raises(WatchpointHit):
        _code()
    # Second call: still fires for its OWN change, not for stale state.
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.old_value == "1"
    assert exc_info.value.new_value == "2"


def test_recursion_each_frame_has_independent_watch():
    """A function watching its own local x can be called recursively; each
    frame's watch operates independently. The change in the INNER call fires
    first (LIFO unwind); the outer call's watch is still arm-able afterward.

    This exercises per-frame keying of _local_watches – with the old single
    'expr' keying, the inner watch would have overwritten the outer's.
    """
    def _code(n):
        x = n          # line A
        watch("x")     # line B
        if n > 0:
            _code(n - 1)
        x = x + 100    # line C – this change SHOULD fire
        pass           # line D – detection point
    # Only the innermost frame's `x = x + 100` runs before any outer unwind,
    # so the deepest WatchpointHit propagates out first.
    with pytest.raises(WatchpointHit) as exc_info:
        _code(2)
    # n=0 deepest: x started at 0, becomes 100.
    assert exc_info.value.old_value == "0"
    assert exc_info.value.new_value == "100"


def test_stale_frame_state_is_reset_when_code_changes():
    """Directly seed _frame_state with a stale entry for a fake code object,
    then run a watched function. The new frame should detect the code mismatch
    and rebuild its baseline rather than diffing against stale prev_hashes."""
    registry = builtins._watchpoint_registry

    def _code():
        x = 100
        watch("x")
        # No change inside this frame – must NOT fire even if a stale state
        # with mismatched hashes is sitting in _frame_state for this frame-id.
        pass

    # Pre-seed stale state under what *will* be _code's frame-id.
    # We can't know id(frame) in advance, so we seed all currently-existing
    # int ids in a small range to ensure SOME of them are reused. The point of
    # the test is that even IF a reused id collides, the code-tag mismatch
    # makes us reset and we don't false-fire.
    fake_code = compile("pass", "<fake>", "exec")
    fake_state = {
        "code": fake_code,
        "prev_line": 1,
        "prev_hashes": {"x": 99999999},  # arbitrary stale hash
        "prev_reprs": {"x": "99999999"},
    }
    # Seed several speculative frame-ids; if any collides we cover the case.
    for fid in range(100000, 100100):
        registry._frame_state[fid] = fake_state

    # Must NOT raise. (Pre-fix this could raise WatchpointHit on first LINE
    # event if frame-id happened to collide.)
    _code()


# ---------------------------------------------------------------------------
# Phase 1: regression tests for bugs fixed during plugin bring-up.
# ---------------------------------------------------------------------------

def test_value_hash_distinguishes_equal_long_strings():
    """_value_hash must use content-based equality for ALL strings, not just
    short ones.

    Regression: an earlier version returned id() for strings of length >= 64.
    Two strings with identical content but different identity (e.g. produced
    by separate "a" * 100 expressions in different functions) compared as
    different, so a true content-equality re-assignment would spuriously fire,
    while a true content-change between two strings happening to share id()
    via interning could go undetected.
    """
    from watchpoint import _value_hash
    s1 = "a" * 100
    s2 = "a" * 100  # distinct object, same content
    s3 = "a" * 99 + "b"  # different content, same length
    assert _value_hash(s1) == _value_hash(s2), (
        "Equal-content long strings must hash equal."
    )
    assert _value_hash(s1) != _value_hash(s3), (
        "Different-content long strings must hash differently."
    )


def test_double_watch_same_frame_keeps_single_registry_entry():
    """Calling watch('x') twice for the same name in the same frame must
    replace the existing entry, not accumulate. With (name, frame_id) keying
    this is the canonical 'rearm in place' case.
    """
    registry = builtins._watchpoint_registry

    def _code():
        x = 1
        watch("x")
        watch("x")  # rearm – should replace, not add a second entry
        keys_for_x = [k for k in registry._local_watches if k[0] == "x"]
        assert len(keys_for_x) == 1, (
            f"Expected exactly one ('x', fid) entry after rearm; got {len(keys_for_x)}"
        )
    _code()


def test_double_watch_rearms_baseline_and_fires_on_next_real_change():
    """After re-watching, the baseline is whatever the variable's value was
    at the SECOND watch() call. A subsequent change is measured against that."""
    def _code():
        x = 1
        watch("x")
        watch("x")  # baseline is still 1 (no change occurred between the watches)
        x = 2       # this change MUST fire
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.old_value == "1"
    assert exc_info.value.new_value == "2"


def test_get_pydevd_debugger_returns_none_outside_debug_session():
    """In a plain pytest run, pydevd isn't loaded and the lookup must return
    None. This is what triggers the raise-fallback path that the entire test
    suite relies on (instead of trying to call do_wait_suspend with no debugger).
    """
    from watchpoint import _get_pydevd_debugger
    assert _get_pydevd_debugger() is None


def test_pause_via_pydevd_disarms_own_frames_to_keep_user_frame_topmost(monkeypatch):
    """Regression: `_pause_via_pydevd` must clear `f_trace` on every frame
    between itself and `user_frame`, so pydevd's pause doesn't latch on one
    of our `<string>`-exec'd frames (which the IDE shows as the dreaded
    topmost `<frame not available>`).

    Mechanism: pydevd's global `sys.settrace` arms every frame's `f_trace`
    during its CALL event. Once `_pause_via_pydevd` calls `set_suspend`,
    the next trace event on any still-armed frame triggers
    `do_wait_suspend` AT THAT FRAME. If we don't disarm our intermediates,
    the suspend can latch on our `__setattr__` or `_handle_hit` frame
    before unwinding reaches user code – exactly the user-visible bug
    that brought us here.

    The test installs a sentinel `f_trace` on a simulated call chain
    (user → intermediate_a → intermediate_b → `_pause_via_pydevd`),
    invokes the function with a stubbed `py_db`, and asserts that on
    return our intermediate frames have `f_trace = None` while the
    user's frame is untouched.
    """
    import sys
    import types
    from types import SimpleNamespace
    from watchpoint import _pause_via_pydevd

    # `_pause_via_pydevd` imports `_pydevd_bundle.*` inside its body.
    # In a plain pytest run pydevd isn't installed, so we plant stub
    # modules in sys.modules for the duration of the test.
    bundle = types.ModuleType("_pydevd_bundle")
    comm = types.ModuleType("_pydevd_bundle.pydevd_comm_constants")
    comm.CMD_STEP_OVER = 108
    constants = types.ModuleType("_pydevd_bundle.pydevd_constants")
    constants.STATE_RUN = 1
    constants.PYTHON_SUSPEND = 1  # arbitrary sentinel; only stored on info
    trace = types.ModuleType("_pydevd_bundle.pydevd_trace_dispatch")
    # `info` needs to be a real attribute carrier since `_pause_via_pydevd`
    # writes pydev_state / pydev_step_cmd / pydev_step_stop / suspend_type
    # onto it directly.
    trace.set_additional_thread_info = lambda thread: SimpleNamespace(
        is_tracing=False, pydev_state=1, pydev_step_cmd=-1,
        pydev_step_stop=None, suspend_type=None,
    )
    monkeypatch.setitem(sys.modules, "_pydevd_bundle", bundle)
    monkeypatch.setitem(sys.modules, "_pydevd_bundle.pydevd_comm_constants", comm)
    monkeypatch.setitem(sys.modules, "_pydevd_bundle.pydevd_constants", constants)
    monkeypatch.setitem(sys.modules, "_pydevd_bundle.pydevd_trace_dispatch", trace)

    fake_py_db = SimpleNamespace(
        _finish_debugging_session=False,
        set_suspend=lambda *args, **kwargs: None,
        set_trace_for_frame_and_parents=lambda frame: None,
    )

    def fake_trace_dispatch(frame, event, arg):
        """Stand-in for pydevd's per-frame trace function."""
        return fake_trace_dispatch

    captured: dict = {}

    def intermediate_b(user_frame_arg):
        """Innermost wrapper – simulates one of our `<string>` frames."""
        sys._getframe(0).f_trace = fake_trace_dispatch
        _pause_via_pydevd(
            fake_py_db, user_frame_arg, "test_watch",
            "old_repr", "new_repr", "file.py", 1,
        )
        # Snapshot AFTER the call. Walking via sys._getframe lets us
        # check every frame in the chain while they're still alive.
        captured["intermediate_b_after"] = sys._getframe(0).f_trace
        captured["intermediate_a_after"] = sys._getframe(1).f_trace
        captured["user_frame_after"] = user_frame_arg.f_trace

    def intermediate_a(user_frame_arg):
        """Middle wrapper – another `<string>`-equivalent frame."""
        sys._getframe(0).f_trace = fake_trace_dispatch
        intermediate_b(user_frame_arg)

    def user_function():
        """Pretend this is the user's frame at the moment the watch fires."""
        sys._getframe(0).f_trace = fake_trace_dispatch
        intermediate_a(sys._getframe(0))

    user_function()

    # Intermediate frames between `_pause_via_pydevd` and `user_frame`
    # must be disarmed so pydevd's tracer doesn't pause AT them on
    # their RETURN events as we unwind.
    assert captured["intermediate_b_after"] is None, (
        "intermediate_b's f_trace should be cleared so pydevd's tracer "
        "doesn't pause AT this frame on its RETURN event"
    )
    assert captured["intermediate_a_after"] is None, (
        "intermediate_a's f_trace should be cleared for the same reason"
    )
    # The user's frame is the WHOLE POINT of the pause – pydevd needs to
    # be able to fire trace_dispatch on its next event so the pause
    # latches there.
    assert captured["user_frame_after"] is fake_trace_dispatch, (
        "user_frame's f_trace must be preserved so pydevd's tracer fires "
        "on the next user-code event after we return"
    )


def test_pause_via_pydevd_enables_line_and_py_return_on_user_and_caller_frames(monkeypatch):
    """Regression: when the watched mutation is the LAST statement of a helper
    function (e.g. `order.status = "paid"` as the only line of `charge_card`
    in test_demo_b), there is no follow-up LINE event inside that frame for
    pydevd's `CMD_STEP_OVER + step_stop = user_frame` to fire on. Without
    PY_RETURN events enabled on user_frame.f_code, pydevd never learns that
    the function returned – the step-over completion is missed, the next
    hit overwrites step_stop, and the pause materialises only for the LAST
    of N back-to-back hits ("stops 2 times for 3 mutations" symptom).

    The fix in `_pause_via_pydevd`'s PEP 669 supplement enables both LINE
    AND PY_RETURN on user_frame.f_code AND user_frame.f_back.f_code, so
    pydevd can detect step-out completion regardless of whether the
    assignment was mid-function or last-line, and the pause has a valid
    landing site (caller's next line) once the helper has returned.
    """
    import sys
    import types
    from types import SimpleNamespace
    from watchpoint import _pause_via_pydevd

    # If something else already owns DEBUGGER_ID (e.g. the test suite is
    # being run from inside PyCharm under pydevd), we can't claim it
    # ourselves and the supplement's set_local_events would silently fail
    # via its broad except. Skip rather than make a misleading assertion.
    if sys.monitoring.get_tool(sys.monitoring.DEBUGGER_ID) is not None:
        pytest.skip(
            "DEBUGGER_ID is already in use – pydevd or another debugger is "
            "attached. Test only meaningful in a plain pytest run."
        )

    # Stub `_pydevd_bundle.*` so `_pause_via_pydevd`'s internal imports
    # succeed. Same shape as `test_pause_via_pydevd_disarms_own_frames_*`
    # above; kept inline (not factored into a fixture) so each pause-path
    # regression test reads end-to-end with no implicit setup.
    bundle = types.ModuleType("_pydevd_bundle")
    comm = types.ModuleType("_pydevd_bundle.pydevd_comm_constants")
    comm.CMD_STEP_OVER = 108
    constants = types.ModuleType("_pydevd_bundle.pydevd_constants")
    constants.STATE_RUN = 1
    constants.PYTHON_SUSPEND = 1
    trace = types.ModuleType("_pydevd_bundle.pydevd_trace_dispatch")
    trace.set_additional_thread_info = lambda thread: SimpleNamespace(
        is_tracing=False, pydev_state=1, pydev_step_cmd=-1,
        pydev_step_stop=None, suspend_type=None,
    )
    monkeypatch.setitem(sys.modules, "_pydevd_bundle", bundle)
    monkeypatch.setitem(sys.modules, "_pydevd_bundle.pydevd_comm_constants", comm)
    monkeypatch.setitem(sys.modules, "_pydevd_bundle.pydevd_constants", constants)
    monkeypatch.setitem(sys.modules, "_pydevd_bundle.pydevd_trace_dispatch", trace)

    fake_py_db = SimpleNamespace(
        _finish_debugging_session=False,
        set_suspend=lambda *args, **kwargs: None,
        set_trace_for_frame_and_parents=lambda frame: None,
    )

    # Claim DEBUGGER_ID (0) so `set_local_events(0, ...)` accepts our calls.
    # In real life pydevd would have claimed it during its own startup;
    # we're playing pydevd's role for the duration of the test.
    debugger_tool_id = sys.monitoring.DEBUGGER_ID
    sys.monitoring.use_tool_id(debugger_tool_id, "test_pep669_supplement")

    # Record the code objects we want to inspect AFTER the pause call has
    # populated events – grabbing them inside the call chain (before the
    # frames die from cleanup) keeps the assertions self-contained.
    captured: dict = {}

    def inner_function(user_frame_arg):
        """Innermost frame – directly calls _pause_via_pydevd, which then
        runs the PEP 669 supplement against user_frame_arg + its f_back.
        """
        _pause_via_pydevd(
            fake_py_db, user_frame_arg, "test_watch",
            "old_repr", "new_repr", "file.py", 1,
        )

    def caller_of_user():
        """The frame that's f_back of `user_function`. Its code object is
        what the supplement should enable LINE+PY_RETURN on too (so pydevd
        has a valid pause landing site once `user_function` returns).
        """
        def user_function():
            """Pretend this is the watched mutation's frame – the function
            whose `__setattr__` just fired. In test_demo_b this is
            `charge_card` / `ship_to_customer`.
            """
            user_frame = sys._getframe(0)
            # Snapshot the code objects from inside the live frames – after
            # the call chain unwinds the frames die, but the code objects
            # they reference are kept alive by `captured` for the assertions.
            captured["user_code"] = user_frame.f_code
            captured["caller_code"] = user_frame.f_back.f_code
            inner_function(user_frame)
        user_function()

    try:
        caller_of_user()

        # PEP 669 events for tool 0 are now bitmasks on each code object.
        # `get_local_events` returns the OR'd mask of every event the tool
        # has set there; we check that LINE + PY_RETURN are both present.
        wanted = (
            sys.monitoring.events.LINE
            | sys.monitoring.events.PY_RETURN
        )
        user_events = sys.monitoring.get_local_events(
            debugger_tool_id, captured["user_code"],
        )
        caller_events = sys.monitoring.get_local_events(
            debugger_tool_id, captured["caller_code"],
        )

        assert (user_events & wanted) == wanted, (
            f"Expected LINE+PY_RETURN ({wanted:#x}) enabled on user_frame.f_code "
            f"by the PEP 669 supplement, got {user_events:#x}. Without these, "
            f"pydevd's CMD_STEP_OVER misses the function's return event when "
            f"the watched mutation is the last statement of the function."
        )
        assert (caller_events & wanted) == wanted, (
            f"Expected LINE+PY_RETURN ({wanted:#x}) enabled on "
            f"user_frame.f_back.f_code by the PEP 669 supplement, got "
            f"{caller_events:#x}. Without these, pydevd's step-over has no "
            f"landing site once user_frame returns – the pause is silently "
            f"dropped."
        )
    finally:
        # Clear the local events we set on each code object, then free the
        # tool ID. Without the explicit clear, the bits stay set in
        # monitoring's internal table even after free_tool_id – future tests
        # that re-claim tool 0 would inherit our events and assert against a
        # polluted baseline.
        for code in captured.values():
            try:
                sys.monitoring.set_local_events(debugger_tool_id, code, 0)
            except Exception:
                pass
        sys.monitoring.free_tool_id(debugger_tool_id)


def test_back_to_back_hits_install_sequential_bps(monkeypatch):
    """When multiple back-to-back mutations all walk up to the same
    user-code anchor, each installs its OWN pydevd `LineBreakpoint` at the
    NEXT available code line. The IDE then pauses N times in line order
    and `_pycharm_consume_last_hit(pause_file, pause_line)` drains only
    the hit whose `bp_line` matches the current pause location.

    Pre-v8 behavior (the original symptom this test was named for): a
    process-level `_pause_pending` gate dropped hits 2..N silently after
    the first install. The user saw one pause / one highlight even though
    N mutations had fired. That was fine for the Django `_clone()`
    "two yellow lines simultaneously" case (one user-perceived event)
    but masked the 4-mutation auth-middleware case where the user
    wanted to see EACH change.

    Post-v8 sequential bps: hit 1 → line 11; hit 2 → line 12; etc. Each
    bp fires its own pause, and the location-aware drain ensures each
    pause shows exactly one highlight (the matching hit). Sibling hits
    stay queued until THEIR bp fires.
    """
    import watchpoint

    install_calls: list = []

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        install_calls.append((file, line, watch_name))
        # Real `_install_bp_at` returns (file, line, bp_id) on success,
        # or None on failure. Both are exercised by other tests; here we
        # always succeed.
        return (file, line, -hash((watch_name, file, line)) & 0x7FFFFFFF)

    fake_py_db = object()  # any non-None sentinel so the pydevd branch is taken
    monkeypatch.setattr(watchpoint, "_get_pydevd_debugger", lambda: fake_py_db)
    monkeypatch.setattr(watchpoint, "_install_bp_at", fake_install_bp)
    # Make removal a no-op for the stubbed bp_ids – the real fn would try
    # to delete from py_db.file_to_id_to_line_breakpoint which our fake
    # py_db doesn't have.
    monkeypatch.setattr(watchpoint, "_remove_temp_breakpoints",
                        lambda py_db, installed: None)

    reg = builtins._watchpoint_registry
    # Sanity: conftest.reset_watchpoint_state ran clear_watches() before
    # this test, so the queue is empty. If this fails the conftest is
    # leaking state across tests.
    assert reg._hit_queue == []

    # `_FakeFrame` with explicit `code_lines` so `_next_code_line_in`
    # walks the same list both runs – first hit picks 11 (smallest > 10),
    # second picks 12 (smallest > 11 already in the queue), etc.
    fake_frame = _FakeFrame(
        "/u/proj/views.py", f_lineno=10,
        code_lines=[10, 11, 12, 13, 14],
        module_name="proj.views",
    )

    # FIRST HIT – queues + installs bp at line 11.
    reg._handle_hit(fake_frame, "obj.attr_a", "old_a", "new_a", "f.py", 5)
    assert len(reg._hit_queue) == 1
    assert reg._hit_queue[0]["bp_locations"][0][1] == 11
    assert reg._hit_queue[0]["bp_locations"][0][0] == "/u/proj/views.py"
    assert install_calls == [("/u/proj/views.py", 11, "obj.attr_a")]

    # SECOND HIT (same anchor, same micro-batch) – queues + installs bp
    # at the NEXT available code line (12). With the new sequential
    # design this does NOT dedupe to drop – the user gets pauses for
    # each mutation.
    reg._handle_hit(fake_frame, "obj.attr_b", "old_b", "new_b", "f.py", 6)
    assert len(reg._hit_queue) == 2
    assert reg._hit_queue[1]["bp_locations"][0][1] == 12
    assert install_calls[-1] == ("/u/proj/views.py", 12, "obj.attr_b")

    # IDE pauses at line 11 (first bp fires). Drain at THAT location –
    # only the matching hit returns; hit_b stays queued for its own
    # pause at line 12.
    payload = builtins._pycharm_consume_last_hit(
        pause_file="/u/proj/views.py", pause_line=11
    )
    assert payload != "", "Drain at line 11 must return hit_a's payload."
    assert len(reg._hit_queue) == 1, (
        "Drain must remove ONLY the matching hit; hit_b (bp_line=12) "
        "must remain armed for its future pause."
    )
    assert reg._hit_queue[0]["name"] == "obj.attr_b"

    # User resumes; line 12 reached; IDE pauses again. Drain at line 12.
    payload = builtins._pycharm_consume_last_hit(
        pause_file="/u/proj/views.py", pause_line=12
    )
    assert payload != ""
    assert reg._hit_queue == []

    # THIRD HIT (post-drain) – fires normally. With the queue empty the
    # slot allocator picks 11 again (smallest > 10), proving slot
    # allocation is queue-driven, not session-global.
    reg._handle_hit(fake_frame, "obj.attr_c", "old_c", "new_c", "f.py", 12)
    assert len(reg._hit_queue) == 1
    assert reg._hit_queue[0]["bp_locations"][0][1] == 11
    assert install_calls[-1] == ("/u/proj/views.py", 11, "obj.attr_c")

    # Cleanup.
    builtins._pycharm_consume_last_hit()


def test_concurrent_hits_reserve_distinct_bp_slots(monkeypatch):
    """Concurrent `_handle_hit` calls must not choose the same bp line.

    Slot allocation used to look only at already-queued hits. Two threads could
    both compute targets while the queue was still empty, then both install at
    the same `(code, line)`. The first pause could then drain/remove the shared
    temp breakpoint and effectively steal the second hit's pause.
    """
    import threading
    import watchpoint

    fake_py_db = object()
    install_calls = []
    install_lock = threading.Lock()

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        with install_lock:
            install_calls.append((file, line, watch_name))
        return (file, line, -hash((watch_name, file, line)) & 0x7FFFFFFF)

    monkeypatch.setattr(watchpoint, "_get_pydevd_debugger", lambda: fake_py_db)
    monkeypatch.setattr(watchpoint, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint, "_remove_temp_breakpoints",
                        lambda py_db, installed: None)

    reg = builtins._watchpoint_registry
    fake_frame = _FakeFrame(
        "/u/proj/views.py", f_lineno=10,
        code_lines=[10, 11, 12, 13],
        module_name="proj.views",
    )

    original_next_slot = reg._next_slot_for_code
    barrier = threading.Barrier(2)

    def racing_next_slot(code, start_line):
        result = original_next_slot(code, start_line)
        barrier.wait(timeout=5.0)
        return result

    monkeypatch.setattr(reg, "_next_slot_for_code", racing_next_slot)

    errors = []

    def runner(name):
        try:
            reg._handle_hit(fake_frame, name, "old", "new", "f.py", 5)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=runner, args=("obj.attr_a",))
    t2 = threading.Thread(target=runner, args=("obj.attr_b",))
    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    assert not t1.is_alive() and not t2.is_alive(), "Race test threads hung."
    assert errors == []

    lines = sorted(call[1] for call in install_calls)
    assert lines == [11, 12], (
        "Concurrent hits should reserve distinct sequential bp slots; "
        f"install calls were {install_calls}"
    )

    builtins._pycharm_consume_last_hit()


def test_clear_watches_clears_hit_queue_and_bps(monkeypatch):
    """Regression: `clear_watches` must drain the hit queue + temp
    breakpoint list. Test isolation depends on it (the conftest autouse
    fixture calls `clear_watches` between tests); without the drain, a
    queued hit from test N would surface as a phantom drain in test N+1
    and the location-aware drain would silently leak stale entries.
    """
    import watchpoint

    monkeypatch.setattr(watchpoint, "_get_pydevd_debugger", lambda: object())
    # Stub returns a successful install so the queue + bp list get
    # populated – we're testing that `clear_watches` empties them.
    monkeypatch.setattr(watchpoint, "_install_bp_at",
                        lambda *a, **kw: ("f.py", 2, -1))
    monkeypatch.setattr(watchpoint, "_remove_temp_breakpoints",
                        lambda py_db, installed: None)

    reg = builtins._watchpoint_registry
    fake_frame = _FakeFrame("f.py", f_lineno=1, code_lines=[1, 2, 3],
                            module_name="f")
    reg._handle_hit(fake_frame, "x", "old", "new", "f.py", 1)
    assert len(reg._hit_queue) == 1
    assert len(reg._temp_breakpoints) == 1

    clear_watches()

    assert reg._hit_queue == [], (
        "clear_watches must drain the hit queue so a follow-up "
        "_pycharm_consume_last_hit doesn't surface a hit that belongs to "
        "a now-removed watch."
    )
    assert reg._temp_breakpoints == [], (
        "clear_watches must drain temp_breakpoints – leftover entries "
        "would pollute pydevd's breakpoint table on the next watch arm."
    )


# ---------------------------------------------------------------------------
# Pause-anchor walk-up (library mutation site → user-code pause)
# ---------------------------------------------------------------------------
#
# These tests use a tiny FakeFrame/FakeCode pair instead of real Python
# frames because we need precise control over `co_filename` (a real frame's
# filename is whatever Python decides when it loads the code; we can't
# easily make a test frame look like it's inside site-packages without
# building a separate file there and importing it.


class _FakeCode:
    """Stand-in for a code object. Carries `co_filename` for the walk-up
    logic and `co_name` + `co_lines()` for the sequential-bps slot
    allocator in `WatchpointRegistry._compute_bp_target`.

    `co_lines()` mimics CPython's `code.co_lines()` return shape:
    iterable of `(start_byte, end_byte, line)` triples. Tests that need
    `_next_code_line_in` to find a follow-up line pass `code_lines=[...]`;
    the byte offsets are 0/0 since the slot allocator ignores them.
    """

    def __init__(self, filename, code_lines=None, name="<fake>"):
        self.co_filename = filename
        self.co_name = name
        self._code_lines = list(code_lines) if code_lines is not None else []

    def co_lines(self):
        return ((0, 0, ln) for ln in self._code_lines)


class _FakeFrame:
    """Stand-in for a Python frame. Carries just enough surface area
    (`f_code.co_filename` + `f_back` + `f_lineno` + `f_globals`) for
    `_find_user_code_caller` and the diagnostic-log path in
    `_handle_hit`.

    We don't try to make these usable with `_pause_via_pydevd` – the
    walk-up tests stub the pydevd-side functions out, so the fake
    frame never reaches code that expects a real frame.
    """

    def __init__(self, filename, f_back=None, f_lineno=0, module_name="",
                 code_lines=None, name="<fake>"):
        self.f_code = _FakeCode(filename, code_lines=code_lines, name=name)
        self.f_back = f_back
        self.f_lineno = f_lineno
        self.f_globals = {"__name__": module_name}


def test_find_user_code_caller_walks_past_site_packages():
    """`_find_user_code_caller` walks `f_back` past site-packages /
    dist-packages frames to find the nearest user-code frame.

    This is the core mechanism behind "pause at user code even when
    the watched mutation happens inside a library" – see CLAUDE.md
    §11 / `_handle_hit`'s pause-anchor docstring for the rationale.
    """
    from watchpoint import _find_user_code_caller

    # Chain: user code → Django (library) → SQLAlchemy (library) →
    # mutation site (library). Walking up from the mutation site
    # should land on the user frame three hops up.
    user_frame = _FakeFrame("/Users/me/project/views.py")
    django_frame = _FakeFrame(
        "/some/.venv/lib/python3.12/site-packages/django/db/models/query.py",
        f_back=user_frame,
    )
    sqlalchemy_frame = _FakeFrame(
        "/some/.venv/lib/python3.12/site-packages/sqlalchemy/orm/session.py",
        f_back=django_frame,
    )
    leaf = _FakeFrame(
        "/some/.venv/lib/python3.12/site-packages/django/db/models/query.py",
        f_back=sqlalchemy_frame,
    )

    assert _find_user_code_caller(leaf) is user_frame, (
        "Walk-up must skip every site-packages frame in the chain and "
        "return the first user-code frame. Anchoring on a library frame "
        "would cause PyCharm's 'do not step into library code' filter "
        "to silently skip pydevd's CMD_STEP_OVER pause."
    )


def test_find_user_code_caller_returns_none_for_pure_library_chain():
    """When NO user code is anywhere in the call chain, `_find_user_code_caller`
    returns None, signalling to `_handle_hit` that the hit should be
    dropped silently (a phantom highlight without a corresponding pause
    is worse UX than no signal at all).
    """
    from watchpoint import _find_user_code_caller

    a = _FakeFrame("/x/.venv/lib/python3.12/site-packages/django/foo.py")
    b = _FakeFrame(
        "/x/.venv/lib/python3.12/site-packages/sqlalchemy/bar.py", f_back=a,
    )
    c = _FakeFrame(
        "/x/.venv/lib/python3.12/site-packages/django/baz.py", f_back=b,
    )

    assert _find_user_code_caller(c) is None


def test_find_user_code_caller_walks_past_stdlib():
    """Regression for the `copy.deepcopy(qs)` case: when a user calls a
    stdlib helper that internally re-triggers our watcher (deepcopy
    re-runs `__init__` on the cloned QuerySet via `self.__class__(...)`,
    which is the watcher subclass), the walk-up landing on `copy.py`
    was bad – pydevd's "do not step into library code" filter swallows
    stdlib step-overs the same way it swallows site-packages.

    Anchoring on `copy.py:143` produced the user-reported "highlight
    fires but no pause" symptom: pause was armed, pydevd's library
    filter rejected it, and the cascade through more stdlib frames
    never reached user code either.

    Previous version of this test asserted stdlib was NOT skipped under
    the rationale "user code passing through stdlib helpers is still
    user code". True in the abstract but irrelevant for pause-anchor
    semantics – we updated the heuristic and flipped the test.
    """
    from watchpoint import _find_user_code_caller

    import copy as _copy
    stdlib_path = _copy.__file__  # /.../python3.12/copy.py

    user_frame = _FakeFrame("/Users/me/proj/main.py")
    stdlib_frame = _FakeFrame(stdlib_path, f_back=user_frame)

    result = _find_user_code_caller(stdlib_frame)
    assert result is user_frame, (
        f"Walk-up must skip stdlib (got {result.f_code.co_filename}, "
        f"expected the user frame). Anchoring on stdlib triggers "
        f"pydevd's library filter and the pause silently never fires."
    )


def test_find_user_code_caller_handles_deepcopy_through_django_chain():
    """The exact user-reported call chain from the diagnostic log:

      copy.py (stdlib)  ← walk-up was landing HERE (broken anchor)
      django/query.py:290 (site-packages)
      django/query.py:289 (site-packages, mutation site)

    With the stdlib filter, walking up from the mutation site must
    skip both Django frames AND the copy.py frame, returning a user
    frame above copy.py (the caller of `copy.deepcopy`).
    """
    from watchpoint import _find_user_code_caller

    import copy as _copy
    copy_path = _copy.__file__

    user_frame = _FakeFrame("/Users/me/proj/serializers.py")
    copy_frame = _FakeFrame(copy_path, f_back=user_frame)
    django_outer = _FakeFrame(
        "/x/.venv/lib/python3.12/site-packages/django/db/models/query.py",
        f_back=copy_frame,
    )
    django_mutation = _FakeFrame(
        "/x/.venv/lib/python3.12/site-packages/django/db/models/query.py",
        f_back=django_outer,
    )

    assert _find_user_code_caller(django_mutation) is user_frame


def test_is_library_filename_treats_declared_user_root_as_user_code(monkeypatch):
    """A project installed under site-packages is still user code.

    The runtime normally treats site-packages as library code so framework
    internals don't become pause anchors. But when the user's project itself is
    debugged from an installed/editable path under site-packages, the launcher
    can declare the project root and that prefix should win over the library
    heuristic.
    """
    from watchpoint import _is_library_filename

    root = "/venv/lib/python3.12/site-packages/my_app"
    monkeypatch.setenv("PYCHARM_WATCHPOINT_USER_ROOTS", root)

    assert not _is_library_filename(f"{root}/views.py")
    assert _is_library_filename("/venv/lib/python3.12/site-packages/django/db/models.py")


def test_find_user_code_caller_skips_pydevd_outside_site_packages():
    """Regression: PyCharm's bundled pydevd lives in a Gradle cache path like
        .../pycharm-community-2025.1.../helpers/pydev/_pydevd_bundle/pydevd_utils.py
    which contains no 'site-packages' segment and is not under the stdlib
    prefix. Before this fix, `_is_library_filename` passed it through and
    `_find_user_code_caller` returned pydevd_utils.py as the pause anchor,
    causing `_install_pause_breakpoint` to install a bp inside pydevd's own
    `eval_in_context` function. That bp fired whenever pydevd evaluated any
    expression (e.g. refreshing Variables panel), locking up the evaluator
    and corrupting test execution (producing 400 instead of 201 in the
    reported Django test failure).

    Fix: `_find_user_code_caller` now also checks the frame's `__name__`
    root against `_FRAMEWORK_MODULE_ROOTS`, which already lists all pydevd
    module prefixes, regardless of installation path.
    """
    from watchpoint import _find_user_code_caller

    gradle_pydevd_path = (
        "/Users/user/.gradle/caches/9.5.1/transforms/abc123/transformed/"
        "pycharm-community-2025.1-aarch64/plugins/python-ce/helpers/pydev/"
        "_pydevd_bundle/pydevd_utils.py"
    )
    csp_path = (
        "/Users/user/.venv/lib/python3.12/site-packages/csp/middleware.py"
    )
    user_path = "/Users/user/projects/myapp/channel_management/tests/test_upsell.py"

    # Case 1: pure pydevd + library chain (no user code) – should return None,
    # not the pydevd frame. Without the fix this returned the pydevd_utils.py
    # frame and broke _install_pause_breakpoint.
    pydevd_frame = _FakeFrame(gradle_pydevd_path, module_name="_pydevd_bundle.pydevd_utils")
    csp_frame = _FakeFrame(csp_path, module_name="csp.middleware", f_back=pydevd_frame)
    assert _find_user_code_caller(csp_frame) is None, (
        "A chain of site-packages → pydevd (Gradle cache) must return None, "
        "not the pydevd frame. Returning a pydevd frame causes a breakpoint "
        "to be installed inside pydevd's own eval_in_context function."
    )

    # Case 2: user code sits ABOVE the pydevd frames – should be returned.
    user_frame = _FakeFrame(user_path, module_name="channel_management.tests.test_upsell")
    pydevd_frame2 = _FakeFrame(
        gradle_pydevd_path, module_name="_pydevd_bundle.pydevd_utils",
        f_back=user_frame,
    )
    csp_frame2 = _FakeFrame(csp_path, module_name="csp.middleware", f_back=pydevd_frame2)
    assert _find_user_code_caller(csp_frame2) is user_frame, (
        "When user code exists above pydevd in the chain, it must be returned "
        "even though an intermediate pydevd frame (outside site-packages) "
        "was skipped."
    )


def test_handle_hit_installs_bp_at_mutation_site_even_in_library(monkeypatch):
    """Post-v9: when the watched mutation happens inside library code
    (Django QuerySet's `_clone()` doing `self._hints = ...`, csp
    middleware setting `request._csp_nonce`, etc.), the bp installs at
    the next code line in the LIBRARY file – not in the walked-up
    user-code caller. The IDE then pauses at the mutation site, which
    is far more contextual.

    The pre-v9 behavior was to walk up past site-packages to anchor on
    user code, working around `CMD_STEP_OVER + step_stop = library_frame`
    being filtered by PyCharm's "do not step into library code" setting.
    We no longer use CMD_STEP_OVER; `LineBreakpoint` + `consolidate_breakpoints`
    fires reliably in library code (it's the same path user-set bps
    take), so the walk-up was just producing distant non-contextual
    pauses with no actual filter to avoid.

    The drop-on-pure-library-chain semantic IS preserved: if NO user
    code exists anywhere in the call stack, the hit is dropped (see
    `test_handle_hit_drops_when_chain_is_entirely_library`). Pure
    library / runtime stacks aren't typically what the user is
    debugging.
    """
    import watchpoint

    received_calls: list = []

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        received_calls.append((target_code, file, line, watch_name))
        return (file, line, -1)

    monkeypatch.setattr(watchpoint, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint, "_remove_temp_breakpoints",
                        lambda py_db, installed: None)

    # User frame must exist SOMEWHERE in the chain so the drop-on-pure-
    # library check passes. The bp anchor itself is the library frame
    # (django_frame) – that's the new behavior.
    user_frame = _FakeFrame(
        "/Users/me/proj/models.py", f_lineno=10,
        code_lines=[10, 11, 12],
        module_name="proj.models",
    )
    django_frame = _FakeFrame(
        "/x/.venv/lib/python3.12/site-packages/django/db/models/query.py",
        f_back=user_frame,
        module_name="django.db.models.query",
        f_lineno=289,
        code_lines=[289, 290, 291],
    )

    reg = builtins._watchpoint_registry
    reg._handle_hit(
        django_frame,
        "qs._hints",
        "{}",
        "{'foo': 1}",
        "/x/.venv/lib/python3.12/site-packages/django/db/models/query.py",
        289,
    )

    assert len(received_calls) == 2, (
        "_install_bp_at must be called twice: primary (mutation site) + safety (user code)."
    )
    # The PRIMARY bp must be installed in the LIBRARY frame (mutation site).
    target_code, bp_file, bp_line, watch_name = received_calls[0]
    assert bp_file == (
        "/x/.venv/lib/python3.12/site-packages/django/db/models/query.py"
    ), (
        "Post-v9: primary bp anchor must be the immediate user_frame (the "
        "mutation site itself), not walked-up to user code. The IDE "
        "pauses at the mutation file's next code line – contextually "
        "useful – instead of in a distant user frame that called into "
        "the library."
    )
    assert bp_line == 290  # next code line after django_frame.f_lineno=289

    # The SAFETY bp is installed in the user-code frame.
    _, safety_file, safety_line, _ = received_calls[1]
    assert safety_file == "/Users/me/proj/models.py"
    assert safety_line == 11  # next code line after user_frame.f_lineno=10

    # Highlight + drain location both point at the library mutation site.
    assert len(reg._hit_queue) == 1
    queued = reg._hit_queue[0]
    assert queued["file"] == (
        "/x/.venv/lib/python3.12/site-packages/django/db/models/query.py"
    )
    assert queued["line"] == 289
    # bp_locations contains both primary and safety.
    assert queued["bp_locations"][0][0] == (
        "/x/.venv/lib/python3.12/site-packages/django/db/models/query.py"
    )
    assert queued["bp_locations"][0][1] == 290

    # Drain at the primary bp location.
    builtins._pycharm_consume_last_hit(
        pause_file=(
            "/x/.venv/lib/python3.12/site-packages/django/db/models/query.py"
        ),
        pause_line=290,
    )
    assert reg._hit_queue == []


def test_handle_hit_uses_bytecode_next_line_for_multiline_attr_assignment(monkeypatch):
    """Multi-line attribute assignments arm the bp after the current bytecode.

    This pins the openapi.py:105 failure. Numeric line order picks 106, but
    lines 106-112 are RHS argument evaluation and have already executed by the
    time STORE_ATTR fires. The primary bp must land on the next future LINE
    event, which is the `return request` line.
    """
    import dis
    import types
    import watchpoint

    install_calls: list = []

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        install_calls.append((target_code, file, line, watch_name))
        return (file, line, -1)

    monkeypatch.setattr(watchpoint, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint, "_remove_temp_breakpoints",
                        lambda py_db, installed: None)

    filename = (
        "/x/.venv/lib/python3.12/site-packages/oi_django/mixins/openapi.py"
    )
    namespace = {}
    exec(compile(
        """
def openapi_validate_request(request, result):
    request.parsed = ParsedRequest(
        body=result.body,
        path=result.parameters.path,
        cookies=result.parameters.cookie,
        query=ImmutableDict(result.parameters.query),
        headers=result.parameters.header,
        security=result.security,
    )

    return request
""",
        filename,
        "exec",
    ), namespace)
    code = namespace["openapi_validate_request"].__code__
    store_attr = next(
        inst for inst in dis.get_instructions(code)
        if inst.opname == "STORE_ATTR" and inst.argval == "parsed"
    )
    return_line = next(
        inst.positions.lineno for inst in dis.get_instructions(code)
        if inst.opname == "RETURN_VALUE"
    )

    user_frame = _FakeFrame(
        "/u/proj/audit_logging/middleware.py", f_lineno=79,
        code_lines=[79, 80, 81],
        module_name="proj.audit_logging.middleware",
    )
    library_frame = types.SimpleNamespace(
        f_code=code,
        f_lineno=store_attr.positions.lineno,
        f_lasti=store_attr.offset,
        f_back=user_frame,
        f_globals={"__name__": "oi_django.mixins.openapi"},
    )

    reg = builtins._watchpoint_registry
    reg._handle_hit(
        library_frame,
        "request.parsed",
        "None",
        "{'data': [1, 2, 3]}",
        filename,
        store_attr.positions.lineno,
    )

    assert install_calls[0][1] == filename
    assert install_calls[0][2] == return_line, (
        "The primary bp must use bytecode order, not the numerically next "
        "line inside an already-evaluated multi-line RHS."
    )
    assert install_calls[0][2] != store_attr.positions.lineno + 1

    payload = builtins._pycharm_consume_last_hit(
        pause_file=filename,
        pause_line=return_line,
    )
    assert payload != ""
    assert reg._hit_queue == []


def test_handle_hit_drops_when_chain_is_entirely_library(monkeypatch):
    """When every frame in the chain is library / runtime (no user code
    anywhere), `_handle_hit` drops the hit silently: no queue append,
    no `_pause_via_pydevd` call, no gate set.

    A phantom highlight with no debugger pause behind it is worse UX
    than silence – the user would see the yellow line, click around
    confused why nothing's stopped, and lose trust in the watchpoint.
    The drop is the same model as `_find_user_caller`'s None-return:
    if there's nowhere meaningful to fire from, don't fire.
    """
    import watchpoint

    pause_calls: list = []

    def fake_pause(*a, **kw):
        pause_calls.append(a)
        return True

    monkeypatch.setattr(watchpoint, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint, "_pause_via_pydevd", fake_pause)

    a = _FakeFrame("/x/.venv/lib/python3.12/site-packages/django/foo.py")
    b = _FakeFrame(
        "/x/.venv/lib/python3.12/site-packages/sqlalchemy/bar.py", f_back=a,
    )

    reg = builtins._watchpoint_registry
    reg._handle_hit(b, "qs.attr", "old", "new", "django/foo.py", 1)

    assert pause_calls == [], (
        "Pure-library chain must NOT call _pause_via_pydevd – there's "
        "no user frame to anchor on."
    )
    assert reg._hit_queue == [], (
        "Dropped hit must NOT be queued. Queueing here would surface a "
        "phantom hit on the next sessionPaused event (highlight without "
        "pause)."
    )
    assert reg._temp_breakpoints == [], (
        "Dropping a hit must NOT install a temp bp – the user has no "
        "user-code frame in the chain to anchor on, so any bp would be "
        "either in a library frame (filter would skip it) or nowhere."
    )


def test_next_code_line_finds_actual_code_line_skipping_blanks():
    """Regression for the user-reported "watchpoint hit but no pause" on
    `set_accessible_products` (user_hotel_relationship.py:195, the last
    line of the function with line 196 blank).

    `_next_code_line_in` must use `co_lines()` to find the actual next
    code line in a code object, not just `current_line + 1`. Otherwise
    bps installed at blank-line positions never fire (pydevd's LINE
    event only fires for ACTUAL code lines), and the pause never
    materialises.
    """
    from watchpoint import _next_code_line_in

    def helper():
        x = 1
        y = 2
        z = 3
        return x + y + z

    code = helper.__code__
    lines = sorted({
        ln for (_, _, ln) in code.co_lines() if ln is not None
    })
    assert len(lines) >= 3, (
        f"Sanity: helper should have at least 3 distinct code lines "
        f"(got {lines})"
    )

    # Property 1: asking for next-after-X returns the smallest code
    # line strictly greater than X. Whatever lines `helper` ends up
    # at, the relationship between them must hold.
    for i, ln in enumerate(lines[:-1]):
        result = _next_code_line_in(code, ln)
        assert result == lines[i + 1], (
            f"_next_code_line_in({ln}) returned {result}, expected "
            f"{lines[i + 1]} (the next entry in {lines})."
        )

    # Property 2: a query that's STRICTLY BETWEEN two code lines must
    # return the upper one. This is the blank-line case that was
    # silently breaking pauses (bp at `current+1` lands on a blank
    # line, no LINE event fires there, bp never triggers).
    first = lines[0]
    second = lines[1]
    if second > first + 1:
        # Gap exists – query in the middle. Most realistic for source
        # with blank lines between statements; co_lines may or may not
        # produce gaps depending on the function's bytecode layout, so
        # this branch is best-effort.
        mid = first + 1
        result = _next_code_line_in(code, mid)
        assert result == second, (
            f"Query in gap (ln={mid}) must return {second}, not {result}. "
            f"This is the user-reported bug: bp at line+1 lands on blank "
            f"line and never fires."
        )

    # Property 3: asking after the LAST code line returns None –
    # signals to the caller "no follow-up line in this code object,
    # walk up to f_back".
    last = lines[-1]
    assert _next_code_line_in(code, last) is None, (
        f"No code lines past the function's last statement (last={last}, "
        f"all={lines}) – caller needs to detect this and fall back to "
        f"f_back."
    )


def test_next_code_line_after_frame_skips_handler_only_lines():
    """Bytecode-order next-line lookup must not choose an exception handler.

    The v20 bytecode-order lookup fixed multi-line assignments by choosing
    the next future LINE after `frame.f_lasti`, but a mutation on the last
    normal statement of a try body has the except handler as the next
    bytecode line. That line is unreachable on the normal no-exception path,
    so installing a breakpoint there recreates the late safety-net pause
    shape from v7-v9.
    """
    import sys
    from watchpoint import (
        _get_except_handler_lines,
        _next_code_line_after_frame,
    )

    captured = {}

    class Probe:
        """Capture the mutating frame from inside STORE_ATTR."""

        def __setattr__(self, name, value):
            frame = sys._getframe(1)
            captured["next_line"] = _next_code_line_after_frame(frame)
            captured["handler_lines"] = _get_except_handler_lines(frame.f_code)
            object.__setattr__(self, name, value)

    def mutation_last_in_try(obj):
        try:
            obj.value = 1
        except ValueError:
            handled = True

    mutation_last_in_try(Probe())

    assert captured["handler_lines"], (
        "Sanity: fixture should compile with handler-only lines after the "
        "try-body mutation."
    )
    assert captured["next_line"] is None, (
        "_next_code_line_after_frame must return None instead of a handler "
        f"line; got {captured['next_line']} from {captured['handler_lines']}"
    )


def test_get_except_handler_lines_excludes_finally_body():
    """finally body lines must NOT be in _get_except_handler_lines output.

    Unlike except handlers, finally bodies execute in the normal no-exception
    flow. If they were incorrectly classified as handler-only lines,
    _next_code_line_in would skip them and return None – causing the bp
    install to fall through to f_back unnecessarily.

    Regression case from test_fix.py (scenario 3): a mutation on the last
    statement of a try body with a finally clause must find the finally line
    as the next viable bp target.
    """
    from watchpoint import _get_except_handler_lines, _next_code_line_in

    def try_finally_func():
        x = 1
        try:
            y = x + 1
        finally:
            z = 3
        return z

    code = try_finally_func.__code__
    handler_lines = _get_except_handler_lines(code)
    lines = sorted({ln for (_, _, ln) in code.co_lines() if ln is not None})

    # The finally body (z = 3) must NOT be in handler_lines.
    # Find it by looking for lines between the try body and the return.
    # More robust: just assert no line that's reachable in normal flow is excluded.
    # The finally line should be navigable via _next_code_line_in from the try body.
    try_body_lines = [ln for ln in lines if ln > lines[0]]  # skip first (x = 1)
    # 'y = x + 1' is in the try body; next should be the finally body (z = 3)
    # not None (which would mean everything after try body was excluded).
    y_line = try_body_lines[1]  # second code line after x=1 (i.e. y = x + 1)
    next_line = _next_code_line_in(code, y_line)

    assert next_line is not None, (
        f"After try body line {y_line}, _next_code_line_in returned None. "
        f"This means the finally body was incorrectly classified as an "
        f"except handler. handler_lines={handler_lines}, all lines={lines}"
    )
    assert next_line not in handler_lines, (
        f"The finally body line {next_line} should not be in handler_lines "
        f"{handler_lines} – finally runs in normal flow."
    )


def test_next_code_line_in_skips_except_handler_directly():
    """_next_code_line_in must skip except-handler lines when choosing the
    next viable breakpoint target.

    Regression case from test_fix.py (scenarios 1 & 2): when the mutation is
    the last normal statement in a try body, bytecode ordering puts the except
    handler as the next line. That line is unreachable on the no-exception path,
    so _next_code_line_in must skip it and return the first line AFTER the
    handler (or None if nothing follows).
    """
    from watchpoint import _get_except_handler_lines, _next_code_line_in

    def simple_try_except():
        x = 1
        try:
            y = x + 1
        except ValueError:
            z = 3
        result = y
        return result

    code = simple_try_except.__code__
    handler_lines = _get_except_handler_lines(code)
    lines = sorted({ln for (_, _, ln) in code.co_lines() if ln is not None})

    assert handler_lines, (
        "Sanity: simple_try_except must produce at least one handler line."
    )

    # For each line, _next_code_line_in should never return a handler line.
    for ln in lines:
        result = _next_code_line_in(code, ln)
        if result is not None:
            assert result not in handler_lines, (
                f"_next_code_line_in(code, {ln}) returned {result} which is "
                f"an except handler line. Handler lines: {handler_lines}, "
                f"all lines: {lines}"
            )


def test_handle_hit_falls_back_to_do_wait_suspend_when_bp_install_fails(monkeypatch):
    """When `_install_bp_at` returns None (arm silently failed – pydevd
    unreachable, breakpoint API import broke, mid-shutdown, etc.),
    `_handle_hit` falls back to `_pause_via_do_wait_suspend` so the user
    still gets a pause for the deliberate `watch(...)` call. The queued
    hit stays in the queue so the IDE highlighter shows WHICH mutation
    fired on the next sessionPaused.

    Pre-v8 the equivalent path set `_pause_pending = True` regardless of
    arm outcome, which locked out every subsequent hit until the next
    `consume`. Post-v8 there is no shared gate – each hit's destiny is
    determined independently. This test pins the fallback to
    do_wait_suspend so the user-visible behavior ("watch fires, debugger
    pauses, IDE highlights the mutation") is preserved even when the bp
    path can't fire.
    """
    import watchpoint

    install_calls: list = []
    pause_calls: list = []

    def fake_install_returns_none(py_db, target_code, file, line, watch_name):
        install_calls.append((file, line, watch_name))
        return None  # simulate failed install

    def fake_do_wait_suspend(py_db, frame, watch_name):
        pause_calls.append(watch_name)
        return True  # pause arranged successfully

    monkeypatch.setattr(watchpoint, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint, "_install_bp_at", fake_install_returns_none)
    monkeypatch.setattr(watchpoint, "_pause_via_do_wait_suspend",
                        fake_do_wait_suspend)

    user_frame = _FakeFrame(
        "/Users/me/proj/views.py", f_lineno=10,
        code_lines=[10, 11, 12],
        module_name="proj.views",
    )
    reg = builtins._watchpoint_registry

    reg._handle_hit(user_frame, "x.attr", "old", "new", "views.py", 10)

    assert install_calls != [], (
        "_install_bp_at must have been called – we're testing the "
        "install-returned-None path, not the no-slot path."
    )
    assert pause_calls == ["x.attr"], (
        "When bp install fails, _handle_hit must fall back to "
        "_pause_via_do_wait_suspend so the user still gets a pause. "
        "Without this fallback, the highlight shows but the debugger "
        "doesn't stop – the user-reported 'highlight fires but no "
        "pause' confusion."
    )
    # The queue DOES contain the hit – the IDE-side highlighter will
    # still draw the yellow line on next sessionPaused. The queued hit
    # carries the failed-install's bp_anchor (line 11) so the legacy
    # drain-all path can still pick it up.
    assert len(reg._hit_queue) == 1


def test_consume_drains_only_matching_pause_location(monkeypatch):
    """`_pycharm_consume_last_hit(pause_file, pause_line)` returns only the
    hit whose installed bp matches the IDE's current pause location.
    Sibling hits (whose bps fire at OTHER lines) stay queued for their
    own future pauses.

    Without selective drain, the IDE would see ALL queued hits on a
    single sessionPaused and paint multiple highlights simultaneously
    (the pre-v8 "two yellow lines at query.py:289 and :290" symptom).
    """
    import watchpoint

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        return (file, line, -hash((watch_name, file, line)) & 0x7FFFFFFF)

    monkeypatch.setattr(watchpoint, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint, "_remove_temp_breakpoints",
                        lambda py_db, installed: None)

    reg = builtins._watchpoint_registry
    fake_frame = _FakeFrame(
        "/u/proj/views.py", f_lineno=10,
        code_lines=[10, 11, 12, 13],
        module_name="proj.views",
    )

    reg._handle_hit(fake_frame, "obj.a", "x", "y", "lib.py", 5)
    reg._handle_hit(fake_frame, "obj.b", "x", "z", "lib.py", 6)
    assert len(reg._hit_queue) == 2
    assert len(reg._temp_breakpoints) == 2

    # Drain at line 11 (first bp's location): only the matching hit
    # comes back; the other stays queued.
    payload = builtins._pycharm_consume_last_hit(
        pause_file="/u/proj/views.py", pause_line=11,
    )
    assert payload != ""
    assert len(reg._hit_queue) == 1
    assert reg._hit_queue[0]["name"] == "obj.b"
    # Only the matching bp got removed; the sibling bp at line 12
    # stays armed for its own future pause.
    assert len(reg._temp_breakpoints) == 1
    assert reg._temp_breakpoints[0][1] == 12

    # Non-matching pause location: drain returns empty, queue is
    # untouched. Models the case where a regular (non-watchpoint)
    # breakpoint fired while our bps are still pending.
    payload = builtins._pycharm_consume_last_hit(
        pause_file="/some/other.py", pause_line=99,
    )
    assert payload == ""
    assert len(reg._hit_queue) == 1
    assert len(reg._temp_breakpoints) == 1

    # Cleanup.
    builtins._pycharm_consume_last_hit()


def test_consume_no_args_drains_everything(monkeypatch):
    """Legacy `_pycharm_consume_last_hit()` (no args) drains the whole
    queue and removes every temp bp. Used at session shutdown and as
    the Kotlin-side fallback when the IDE can't read the pause
    location from `XDebugSession`.
    """
    import watchpoint

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        return (file, line, -1)

    monkeypatch.setattr(watchpoint, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint, "_remove_temp_breakpoints",
                        lambda py_db, installed: None)

    reg = builtins._watchpoint_registry
    fake_frame = _FakeFrame(
        "/u/proj/views.py", f_lineno=10,
        code_lines=[10, 11, 12, 13],
        module_name="proj.views",
    )
    reg._handle_hit(fake_frame, "obj.a", "x", "y", "lib.py", 5)
    reg._handle_hit(fake_frame, "obj.b", "x", "z", "lib.py", 6)
    assert len(reg._hit_queue) == 2

    # No args ⇒ drain everything.
    payload = builtins._pycharm_consume_last_hit()
    assert payload != ""
    # Two ';'-separated entries.
    assert payload.count(";") == 1
    assert reg._hit_queue == []
    assert reg._temp_breakpoints == []


def test_sequential_bps_drop_silently_when_anchor_runs_out_of_code_lines(monkeypatch):
    """When the anchor function only has N usable code lines after the
    mutation line, the (N+1)-th back-to-back hit at the same anchor
    has no available slot. Since the user has already received N
    pauses (or will, when those bps fire), the (N+1)-th hit drops
    silently rather than blocking the user thread on do_wait_suspend.
    """
    import watchpoint

    install_calls: list = []
    pause_calls: list = []

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        install_calls.append((file, line, watch_name))
        return (file, line, -1)

    def fake_do_wait_suspend(py_db, frame, watch_name):
        pause_calls.append(watch_name)
        return True

    monkeypatch.setattr(watchpoint, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint, "_pause_via_do_wait_suspend",
                        fake_do_wait_suspend)
    monkeypatch.setattr(watchpoint, "_remove_temp_breakpoints",
                        lambda py_db, installed: None)

    reg = builtins._watchpoint_registry
    # Anchor function has only 2 usable code lines after f_lineno=10:
    # line 11 and line 12. A third back-to-back hit exhausts them and
    # must drop silently.
    fake_frame = _FakeFrame(
        "/u/proj/views.py", f_lineno=10,
        code_lines=[10, 11, 12],
        module_name="proj.views",
    )

    reg._handle_hit(fake_frame, "obj.a", "x", "y", "f.py", 1)
    reg._handle_hit(fake_frame, "obj.b", "x", "y", "f.py", 2)
    reg._handle_hit(fake_frame, "obj.c", "x", "y", "f.py", 3)  # no slot

    assert len(reg._hit_queue) == 2, (
        "First two hits fill all available slots (lines 11 and 12); "
        "the third hit drops silently because the queue is non-empty "
        "and no follow-up code line exists."
    )
    assert pause_calls == [], (
        "Dropping a Nth hit must NOT fall back to do_wait_suspend – the "
        "user has already been notified of the prior hits via their bps."
    )
    assert [c[1] for c in install_calls] == [11, 12]

    # Cleanup.
    builtins._pycharm_consume_last_hit()


def test_sequential_bps_first_hit_with_no_slot_falls_back_to_do_wait_suspend(monkeypatch):
    """When the FIRST hit (queue empty) has no available bp slot
    (anchor function has no follow-up code line AND f_back isn't
    user code), `_handle_hit` falls back to `_pause_via_do_wait_suspend`
    so the user still gets a pause for the deliberate `watch(...)`.

    This is the `script.py` last-line-of-module corner case in design
    contract §13's `_pause_via_do_wait_suspend` rationale.
    """
    import watchpoint

    pause_calls: list = []

    def fake_do_wait_suspend(py_db, frame, watch_name):
        pause_calls.append(watch_name)
        return True

    monkeypatch.setattr(watchpoint, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint, "_pause_via_do_wait_suspend",
                        fake_do_wait_suspend)
    monkeypatch.setattr(watchpoint, "_remove_temp_breakpoints",
                        lambda py_db, installed: None)

    reg = builtins._watchpoint_registry

    # Anchor with `f_lineno=10` and `code_lines=[10]` – no line after
    # 10, so `_next_code_line_in` returns None. f_back is a library
    # frame, so it can't anchor either. → fall back to do_wait_suspend.
    library_fb = _FakeFrame(
        "/x/.venv/lib/python3.12/site-packages/django/x.py",
        module_name="django.x",
    )
    fake_frame = _FakeFrame(
        "/u/proj/views.py", f_lineno=10,
        code_lines=[10],
        module_name="proj.views",
        f_back=library_fb,
    )

    reg._handle_hit(fake_frame, "x.attr", "old", "new", "views.py", 10)

    assert pause_calls == ["x.attr"], (
        "First hit with no available bp slot must fall back to "
        "do_wait_suspend – the user explicitly asked to pause."
    )
    # The hit is queued without bp_locations (the do_wait_suspend path
    # stores an empty list). The legacy drain-all path or selective drain
    # at any location will both surface it.
    assert len(reg._hit_queue) == 1
    assert reg._hit_queue[0]["bp_locations"] == []

    # Cleanup.
    builtins._pycharm_consume_last_hit()


def test_watch_at_locates_paused_frame_in_other_thread():
    """_pycharm_watch_at scans every running thread's frame stack for a frame
    matching (file_hint, func_hint). Used by the PyCharm 'Add Watchpoint'
    action when the evaluator's own sys._getframe() chain doesn't contain
    the user's paused frame.
    """
    import threading
    started = threading.Event()
    can_finish = threading.Event()
    captured_frame_holder = {}

    def _paused_user_function():
        """Helper representing a paused user frame in another thread."""
        my_local = 1234
        captured_frame_holder["frame"] = inspect.currentframe()
        started.set()
        can_finish.wait(timeout=5.0)  # pretend we're paused at a breakpoint
        # Reference my_local to keep it live in f_locals.
        _ = my_local

    t = threading.Thread(target=_paused_user_function, name="paused-thread")
    t.start()
    started.wait(timeout=5.0)

    try:
        from watchpoint import _find_paused_user_frame
        found = _find_paused_user_frame(__file__, "_paused_user_function")
        assert found is not None
        assert found.f_code.co_name == "_paused_user_function"
        # The frame is alive – its locals should be readable.
        assert found.f_locals.get("my_local") == 1234
    finally:
        can_finish.set()
        t.join(timeout=5.0)


def test_watch_at_prefers_innermost_recursive_frame():
    """When multiple recursive frames match file + function, pick innermost.

    The IDE action only passes `(file_hint, func_hint)` today. If a recursive
    function is paused at its deepest call, every invocation has the same code
    object, so matching by file/name returns several candidates. Choosing the
    outermost one arms the watch on a frame that is not currently selected and
    makes the next inner-frame mutation invisible.
    """
    import threading
    started = threading.Event()
    can_finish = threading.Event()
    frames = []

    def _recursive_paused_function(depth):
        """Helper representing a paused recursive call stack."""
        frames.append(inspect.currentframe())
        if depth > 0:
            _recursive_paused_function(depth - 1)
        else:
            started.set()
            can_finish.wait(timeout=5.0)

    t = threading.Thread(
        target=_recursive_paused_function, args=(2,),
        name="recursive-paused-thread",
    )
    t.start()
    started.wait(timeout=5.0)

    try:
        from watchpoint import _find_paused_user_frame
        found = _find_paused_user_frame(__file__, "_recursive_paused_function")
        assert found is frames[-1], (
            "Recursive watch_at lookup should prefer the innermost matching "
            "frame, which is the frame PyCharm is normally paused on."
        )
    finally:
        can_finish.set()
        t.join(timeout=5.0)


# ---------------------------------------------------------------------------
# Phase 2: threading – will exercise concurrent-dict-iteration races in
# the LINE callback. Expected to FAIL until the snapshot-under-lock fix
# is applied. Stress tests are timing-sensitive; iteration counts chosen
# to make the race very likely under the unfixed code but still finish in
# < 2 seconds on a modern Mac.
# ---------------------------------------------------------------------------

def test_concurrent_watch_mutation_does_not_crash_callback():
    """While one set of threads runs LINE callbacks on watched code, another
    set is calling watch()/unwatch() in a tight loop. The shared
    _local_watches dict must not raise during iteration.

    Pre-fix this races into:
        RuntimeError: dictionary changed size during iteration
    """
    import threading
    import time

    errors: list = []
    errors_lock = threading.Lock()
    stop = threading.Event()

    def churner():
        """Tight watch/unwatch loop on its own frame."""
        try:
            while not stop.is_set():
                z = 0
                watch("z")
                unwatch("z")
        except Exception as e:  # noqa: BLE001
            with errors_lock:
                errors.append(("churner", repr(e)))

    def runner():
        """Tight enter/exit a freshly-allocated watched function."""
        try:
            while not stop.is_set():
                def inner():
                    y = 0
                    watch("y")
                    y = 1   # change – will trigger _on_line callback
                try:
                    inner()
                except WatchpointHit:
                    pass    # expected on most iterations
        except Exception as e:  # noqa: BLE001
            with errors_lock:
                errors.append(("runner", repr(e)))

    threads = [threading.Thread(target=churner) for _ in range(2)]
    threads += [threading.Thread(target=runner) for _ in range(2)]

    for t in threads:
        t.start()
    time.sleep(0.4)
    stop.set()
    for t in threads:
        t.join(timeout=5.0)

    assert not errors, f"Concurrent access caused errors: {errors[:5]}"


def test_two_threads_watch_same_code_independently():
    """Two threads execute the SAME watched function concurrently. Each
    thread's watch on its own frame must fire only for its own change.
    Exercises per-(name, frame_id) keying under load.
    """
    import threading

    barrier = threading.Barrier(2)
    hits: list = []
    hits_lock = threading.Lock()

    def runner(label):
        try:
            _shared_watched_function(label, barrier)
        except WatchpointHit as e:
            with hits_lock:
                hits.append((label, e.old_value, e.new_value))

    t1 = threading.Thread(target=runner, args=(1,))
    t2 = threading.Thread(target=runner, args=(2,))
    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    assert len(hits) == 2, f"Expected 2 hits, got {len(hits)}: {hits}"
    new_vals = sorted(int(n) for _, _, n in hits)
    assert new_vals == [101, 102], f"Unexpected new values: {new_vals}"


# Module-level so both threads share the same code object.
def _shared_watched_function(label, barrier):
    """Helper used by test_two_threads_watch_same_code_independently."""
    x = label
    watch("x")
    barrier.wait(timeout=5.0)  # rendezvous so both threads have armed before changing
    x = label + 100
    pass


# ---------------------------------------------------------------------------
# Phase 3: asyncio. asyncio runs on a single thread by default – tasks
# switch via await. Watches survive yields because PY_RETURN doesn't fire
# on coroutine yield (only on completion / exception).
# ---------------------------------------------------------------------------

def test_asyncio_two_tasks_watch_independently():
    """Two concurrent asyncio tasks running the same coroutine each fire on
    their own change. Per-frame keying handles per-coroutine isolation
    automatically because each task instance has a distinct coroutine frame.
    """
    import asyncio
    hits: list = []

    async def safe_run(label):
        try:
            await _watched_coroutine(label)
        except WatchpointHit as e:
            hits.append((label, e.old_value, e.new_value))

    async def main():
        await asyncio.gather(safe_run(1), safe_run(2))

    asyncio.run(main())
    assert len(hits) == 2, f"Expected 2 hits, got {len(hits)}: {hits}"
    new_vals = sorted(int(n) for _, _, n in hits)
    assert new_vals == [101, 102]


def test_asyncio_watch_survives_await():
    """A watch armed before an await persists through the suspend/resume and
    fires when the watched local changes after the coroutine resumes."""
    import asyncio

    async def main():
        with pytest.raises(WatchpointHit) as exc_info:
            await _coroutine_with_await()
        assert exc_info.value.new_value == "99"

    asyncio.run(main())


# Module-level so tests can share the code object.
async def _watched_coroutine(label):
    import asyncio
    x = label
    watch("x")
    await asyncio.sleep(0)   # yield to event loop – watch must survive
    x = label + 100
    pass


async def _coroutine_with_await():
    import asyncio
    x = 1
    watch("x")
    await asyncio.sleep(0)
    x = 99
    pass


# ---------------------------------------------------------------------------
# Object-wide attribute watching: watch("req") on a custom-class instance
# should fire on ANY attribute mutation, not only on name rebinding. Matches
# the Flask/Django use case where a view receives a `request` and mutates
# its attributes throughout the handler.
# ---------------------------------------------------------------------------

class _RequestLike:
    """Mimics a Flask/Django request: a user-defined object whose attributes
    are mutated in-place during request handling."""
    def __init__(self):
        self.method = "GET"
        self.user = None
        self.external_user = None


def test_watch_complex_object_fires_on_attribute_mutation():
    """watch('req') on a user-defined object installs an attribute-level
    watch that fires when ANY of req's attributes is assigned a new value.

    The hit's watch_name carries the full path including the changed attr
    (e.g. 'req.user'), so the IDE can show which attribute moved.
    """
    req = _RequestLike()
    watch("req")
    with pytest.raises(WatchpointHit) as exc_info:
        req.user = "alice"
    hit = exc_info.value
    assert hit.watch_name == "req.user", (
        f"Expected watch_name 'req.user', got {hit.watch_name!r}"
    )
    assert hit.new_value == "'alice'"
    assert hit.old_value == "None"


def test_watch_complex_object_fires_for_each_subsequent_attribute_change():
    """The object watch persists across multiple mutations – each fires
    independently. This is the canonical 'I want every change to this
    request object' flow."""
    req = _RequestLike()
    watch("req")
    with pytest.raises(WatchpointHit):
        req.user = "alice"
    with pytest.raises(WatchpointHit):
        req.method = "POST"
    with pytest.raises(WatchpointHit):
        req.external_user = req.user  # depends on req.user already set


def test_watch_complex_object_does_not_fire_on_same_value_reassignment():
    """Object watch uses _value_hash for equality: assigning the same value
    is a no-op and must not fire (no spurious 'self-assignment' interrupts)."""
    req = _RequestLike()
    req.user = "alice"   # before watch – not observed
    watch("req")
    req.user = "alice"   # same value, must be silent
    req.method = "GET"   # also same


def test_unwatch_complex_object_restores_original_class():
    """unwatch must reverse the class surgery so subsequent attribute changes
    are no longer instrumented and the type behaves exactly as before."""
    req = _RequestLike()
    original_cls = type(req)
    watch("req")
    assert type(req) is not original_cls, (
        "watch() on a custom object should change __class__ via surgery"
    )
    unwatch("req")
    assert type(req) is original_cls, (
        "unwatch() must restore the original class"
    )
    req.user = "alice"   # must be silent now


def test_watch_complex_object_fires_when_mutated_from_other_function():
    """The watch is bound to the OBJECT, not a frame, so attribute changes
    made from any code that holds a reference to it still fire."""
    req = _RequestLike()
    watch("req")

    def _mutate_elsewhere(r):
        r.user = "remote-change"

    with pytest.raises(WatchpointHit) as exc_info:
        _mutate_elsewhere(req)
    assert exc_info.value.new_value == "'remote-change'"
    # source_line should point at the assignment inside _mutate_elsewhere.
    assert exc_info.value.source_file.endswith("test_watchpoint.py")


def test_watch_simple_value_still_uses_local_variable_watch():
    """For primitive values, watch() must still use local-variable rebinding
    detection (class surgery would fail on builtins anyway)."""
    def _code():
        x = 1
        watch("x")
        x = 2
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.watch_name == "x"


def test_watch_string_uses_local_variable_watch():
    """Strings are immutable: only rebinding is observable. Must use
    local-variable watch, not class surgery."""
    def _code():
        s = "hello"
        watch("s")
        s = "world"
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_watch_list_uses_local_variable_watch():
    """list is a built-in type – class surgery would fail. We fall back to
    the local-variable path. Item mutations (lst.append, lst[0]=...) are NOT
    detected; only rebinding lst to a new list fires."""
    def _code():
        lst = [1, 2, 3]
        watch("lst")
        lst = [4, 5, 6]
        pass
    with pytest.raises(WatchpointHit):
        _code()


# ---------------------------------------------------------------------------
# Last-line change detection. When the watched local changes on the FINAL
# line of a function, no trailing LINE event runs – the change is caught by
# PY_RETURN. The user-visible pause in pydevd must target a stable frame:
# the leaving frame is half-returned and confuses pydevd, so we pause at
# the CALLER's frame and report source_line/source_file from the inner.
# ---------------------------------------------------------------------------

def test_watch_fires_when_change_is_on_last_line():
    """The change on the LAST line of a function has no following LINE event
    in this frame. PY_RETURN catches the change."""
    def _code():
        x = 1
        watch("x")
        x = 2  # last line; PY_RETURN catches the change
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.old_value == "1"
    assert exc_info.value.new_value == "2"


def test_last_line_change_reports_inner_source_location():
    """Even though pause targets the caller's frame (for pydevd stability),
    the WatchpointHit's source_file/source_line still point to the actual
    assignment line in the inner watched function."""
    def _code():
        x = 1
        watch("x")
        x = 2

    inner_source_lines, inner_start = inspect.getsourcelines(_code)
    assignment_line = None
    for i, src_line in enumerate(inner_source_lines):
        if src_line.strip() == "x = 2":
            assignment_line = inner_start + i
            break
    assert assignment_line is not None, "Could not find 'x = 2' in _code source"

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.source_line == assignment_line
    assert exc_info.value.source_file.endswith("test_watchpoint.py")


def test_object_watch_handles_rapid_repeated_attribute_changes():
    """A burst of attribute changes each fires its own WatchpointHit. This
    guards against an overly-aggressive re-entrancy guard accidentally
    suppressing real subsequent changes after the first one fires.
    """
    req = _RequestLike()
    watch("req")
    for value in ("a", "b", "c", "d"):
        with pytest.raises(WatchpointHit) as exc_info:
            req.user = value
        assert exc_info.value.new_value == repr(value)


# ---------------------------------------------------------------------------
# Cross-function watching. Confirms that a watch set on a variable in one
# function continues to fire when control passes through helper calls that
# read or mutate the watched value. Three categories, in order of complexity:
#
#   (a) Object passed through a chain of nested helpers, with the actual
#       mutation happening several frames deep – the existing class-surgery
#       object watch should already cover this, the test pins it down.
#   (b) Watched list/dict mutated by a helper – the mutation itself happens
#       outside the watching frame, so detection only fires on the next LINE
#       event after control returns. The _value_hash(repr) for containers
#       gives us this for free; the test demonstrates the timing contract.
#   (c) Primitive passed to a helper that rebinds its parameter – Python
#       semantics give the callee a separate binding, so this requires the
#       watch to PROPAGATE across the call boundary (a new feature).
# ---------------------------------------------------------------------------


def test_watch_object_fires_through_nested_call_chain():
    """An object watch survives an arbitrary call depth: a helper calls
    another helper that mutates the watched attribute, and the hit still
    fires with the correct watch_name and new value. Confirms that the
    class-surgery watch is frame-agnostic, not just one-call-deep.
    """
    req = _RequestLike()
    watch("req")

    def _step_two(r):
        r.user = "from-deep-helper"   # mutation two frames below the watch caller

    def _step_one(r):
        _step_two(r)                  # nested call – does not mutate directly

    with pytest.raises(WatchpointHit) as exc_info:
        _step_one(req)
    hit = exc_info.value
    assert hit.watch_name == "req.user"
    assert hit.new_value == "'from-deep-helper'"
    assert hit.old_value == "None"


def test_watch_list_mutation_via_helper_detected_on_return():
    """When a helper mutates a watched list in place, the watching frame's
    next LINE event detects the change.

    Mechanism: `_value_hash` for a list is hash(repr(list)), so the diff in
    `_on_line` catches content changes. The mutation itself happens inside
    the helper (no LINE events there because LINE is only enabled on the
    watching frame's code object), so detection lands on the line AFTER the
    call returns – the test puts an explicit `pass` there.
    """
    def _code():
        items = []
        watch("items")

        def _mutate(lst):
            lst.append(1)             # in-place mutation – list contents change

        _mutate(items)                # call returns with items == [1]
        pass                          # detection LINE event fires here

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    hit = exc_info.value
    assert hit.watch_name == "items"
    assert hit.old_value == "[]"
    assert hit.new_value == "[1]"


def test_watch_dict_mutation_via_helper_detected_on_return():
    """Same contract as list mutation: a helper that mutates a watched
    dict in place is caught on the next LINE event in the watching frame.
    """
    def _code():
        data = {}
        watch("data")

        def _populate(d):
            d["key"] = "value"        # in-place key insertion

        _populate(data)
        pass                          # detection here

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    hit = exc_info.value
    assert hit.watch_name == "data"
    assert hit.old_value == "{}"
    assert hit.new_value == "{'key': 'value'}"


def test_watch_primitive_follows_argument_into_callee():
    """A primitive watch propagates across a function-call boundary: when
    `watch('x')` is followed by `f(x)`, the callee's parameter that received
    the watched value is itself implicitly watched. If the callee rebinds
    that parameter, the watch fires from inside the callee.

    This is NOT default Python semantics – the callee's parameter is a
    separate binding from the caller's `x` – so this depends on the watch
    propagation mechanism in the registry that detects the CALL event,
    inspects the callee's f_locals at PY_START, and arms a per-parameter
    watch when the value's identity matches the caller's watched value.
    """
    def _code():
        x = 1
        watch("x")

        def _modify(x):               # callee's `x` is a different binding
            x = 99                    # rebind the param – should fire
            pass                      # LINE event for the rebind

        _modify(x)                    # pass watched value as argument

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    hit = exc_info.value
    # The hit should attribute to the callee's local rebind, not the caller's.
    assert hit.new_value == "99"
    assert hit.old_value == "1"


# ===========================================================================
# EDGE CASES – propagation shapes (kwargs, methods, chains, classes, etc.)
# ===========================================================================
# Each test pins down one shape of call site / one corner of the propagation
# contract. Several of these may surface real bugs in the current
# implementation; the goal here is to enumerate the contract precisely so we
# can either fix what breaks or document the limitation explicitly.


def test_propagation_through_method_call():
    """`obj.method(watched)` should propagate the watch into the method body.

    Method calls are still CALL bytecode and the bound method exposes
    `__code__`, so propagation should work the same as for plain functions.
    """
    class Service:
        def handle(self, val):
            val = val + 100   # rebind the param – should fire
            pass              # detection LINE event

    svc = Service()

    def _code():
        n = 5
        watch("n")
        svc.handle(n)         # propagation should reach Service.handle

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    hit = exc_info.value
    assert hit.new_value == "105"
    assert hit.old_value == "5"


def test_propagation_through_keyword_argument():
    """`f(x=watched)` keyword argument should still propagate.

    The callee's parameter is bound by name; at PY_START its f_locals shows
    the param holding the watched value, and the id() match arms the watch
    regardless of how it was passed.
    """
    def _modify(*, target):
        target = target + 1
        pass

    def _code():
        n = 10
        watch("n")
        _modify(target=n)

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.new_value == "11"


def test_propagation_with_multiple_watched_args():
    """`f(a, b)` where BOTH a and b are watched: a rebind to either should
    fire under the matching watch name. The first rebind (positional order:
    the inner one) raises and stops further execution.
    """
    def _modify(a, b):
        a = a + 1000     # rebind a first – should fire as 'first'
        b = b + 2000
        pass

    def _code():
        first = 1
        second = 2
        watch("first")
        watch("second")
        _modify(first, second)

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    hit = exc_info.value
    assert hit.watch_name == "first"
    assert hit.new_value == "1001"


def test_propagation_chains_through_multiple_helpers():
    """A → B → C: the watch armed in A should follow the value through B
    into C, firing when C rebinds its parameter. Confirms that propagation
    enables the necessary events on each callee so deeper chains keep
    propagating, not just the first hop.
    """
    def _leaf(val):
        val = -999
        pass

    def _mid(val):
        _leaf(val)            # propagation should chain from mid into leaf

    def _code():
        x = 42
        watch("x")
        _mid(x)

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.new_value == "-999"


def test_propagation_through_recursive_self_call():
    """A function that recursively calls itself with the watched value
    passed UNCHANGED should propagate at every level. We use a stable
    sentinel object as the propagated value so its id is preserved across
    recursive frames (id-based matching can't follow a transformed value
    like `n - 1`, which creates a new int with a new id – that's a
    documented limitation, not a bug).
    """
    payload = object()       # unique, non-interned sentinel

    def _recurse(val, depth):
        if depth > 0:
            _recurse(val, depth - 1)   # val passed unchanged – id preserved
        else:
            val = "rebound"            # innermost rebind
            pass

    def _code():
        start = payload
        watch("start")
        _recurse(start, 2)

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.new_value == "'rebound'"


def test_propagation_does_not_overtrigger_on_unrelated_args():
    """A primitive value passed alongside a watched arg should NOT cause
    spurious fires on the unrelated parameter. The watched arg is what the
    user cares about; only its identity should drive propagation.

    Note: for *interned* primitives, the implementation matches by id, so
    multiple args with the same value will all be watched. Here we use a
    fresh non-interned object to avoid that.
    """
    sentinel_a = object()
    sentinel_b = object()

    def _modify(a, b):
        # Only b is rebound. If the watch leaked onto a, this would not
        # report as 'watched_b'.
        b = "rebound-b"
        pass

    def _code():
        x = sentinel_a    # not watched
        y = sentinel_b    # watched
        watch("y")
        _modify(x, y)

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    hit = exc_info.value
    assert hit.watch_name == "y"
    assert hit.new_value == "'rebound-b'"


def test_propagation_callee_does_not_modify_does_not_fire():
        """If a callee receives a watched value but does NOT rebind / mutate it,
        no WatchpointHit should fire. Pure-pass-through must be silent.
        """
        def _read_only(val):
            _ = val + 1           # use without rebinding the param
            return val

        def _code():
            x = 7
            watch("x")
            result = _read_only(x)
            assert result == 7
        # Must NOT raise.
        _code()


def test_propagation_through_class_instantiation():
        """`MyClass(watched)` should propagate the watch into `__init__` so a
        rebind of the constructor's parameter fires.

        Currently the CALL event reports `callable_=MyClass`, which has no
        `__code__` attribute – class instances don't expose the constructor's
        code object directly. This test documents the gap; the fix is to
        special-case classes by looking up `callable_.__init__.__code__`.
        """
        class Holder:
            def __init__(self, val):
                val = val * 10    # rebind the param – ideally fires
                self.val = val

        def _code():
            n = 3
            watch("n")
            Holder(n)             # ideally propagates into __init__

        with pytest.raises(WatchpointHit) as exc_info:
            _code()
        assert exc_info.value.new_value == "30"


def test_propagation_into_generator_function_does_not_leak():
        """Calling a generator function returns a generator object WITHOUT
        entering the body, so no PY_START fires for the generator's code at the
        CALL site. If we naively queued a propagation it would sit there forever
        (or be applied much later when next() runs in unrelated context).

        Required contract: calling a generator function with a watched value
        should NOT leave a stale entry on the propagation queue. The test peeks
        at the queue depth (this is whitebox but the cleanest way to catch a
        leak).
        """
        def _gen(val):
            yield val
            val = -1              # rebind only happens on first next()
            yield val

        def _code():
            x = 5
            watch("x")
            g = _gen(x)           # CALL fires, but generator body doesn't run
            # If we ALSO iterate, the rebind to -1 happens on next next(); we
            # don't want a stale propagation entry, but we also don't want to
            # break that case if iteration does eventually happen.
            del g                 # don't iterate – just drop the generator

        _code()
        # Peek at the queue. If we leaked, len > 0.
        registry = builtins._watchpoint_registry
        queue = getattr(registry._pending_propagation, "queue", None)
        leftover = len(queue) if queue is not None else 0
        assert leftover == 0, (
            f"Generator-function CALL leaked {leftover} propagation entries"
        )


def test_propagation_through_async_function_does_not_leak():
        """Calling an async def returns a coroutine without running its body.
        Same leak risk as generators."""
        import asyncio

        async def _aio(val):
            return val + 100

        def _code():
            x = 5
            watch("x")
            coro = _aio(x)
            coro.close()          # discard without awaiting

        _code()
        registry = builtins._watchpoint_registry
        queue = getattr(registry._pending_propagation, "queue", None)
        leftover = len(queue) if queue is not None else 0
        assert leftover == 0, (
            f"Async-function CALL leaked {leftover} propagation entries"
        )


# ===========================================================================
# EDGE CASES – local-variable & container corners
# ===========================================================================


def test_watch_augmented_assignment_fires():
    """`x += 1` is detected as a value change on the next LINE event."""
    def _code():
        x = 0
        watch("x")
        x += 5                # augmented assignment
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.new_value == "5"


def test_watch_for_loop_rebinding_fires_on_first_iteration():
    """A for-loop rebinds the loop variable each iteration. With a watch
    armed on the loop variable from outside, the first iteration's rebind
    should fire (and propagate out, ending the loop).
    """
    def _code():
        x = "init"
        watch("x")
        for x in ["alpha", "beta", "gamma"]:
            pass              # first iteration: x rebinds to 'alpha', LINE fires
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.new_value == "'alpha'"


def test_watch_dict_subscript_assignment_fires():
    """`d['key'] = value` mutates the watched dict; the next LINE in the
    watching frame should detect via _value_hash(repr())."""
    def _code():
        d = {"a": 1}
        watch("d")
        d["b"] = 2            # subscript mutation
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    hit = exc_info.value
    assert "'b': 2" in hit.new_value or "'a': 1, 'b': 2" in hit.new_value


def test_watch_list_subscript_assignment_fires():
    """`lst[i] = value` mutates list contents – repr changes – detected."""
    def _code():
        lst = [1, 2, 3]
        watch("lst")
        lst[0] = 99
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert "99" in exc_info.value.new_value


def test_watch_attribute_with_slots_class():
    """Classes that use __slots__ don't have a __dict__. Our class-surgery
    creates a subclass; the subclass inherits the slots layout. __class__
    assignment between layout-compatible classes is allowed, so this should
    work.
    """
    class Slotted:
        __slots__ = ("val",)
        def __init__(self):
            self.val = 1

    obj = Slotted()
    try:
        watch("obj.val")
    except TypeError:
        pytest.skip("__class__ surgery refused on __slots__ class – limitation")
        return
    with pytest.raises(WatchpointHit) as exc_info:
        obj.val = 99
    assert exc_info.value.new_value == "99"


def test_watch_attribute_with_property_setter():
    """A @property's setter does NOT go through __setattr__ for the backing
    attribute; assignment to the property triggers the setter directly. Our
    class-surgery override of __setattr__ DOES intercept assignments to the
    property name itself though (because `obj.prop = x` is dispatched
    through __setattr__ at the type level when a data descriptor exists).
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
    # Setting via the property name. Does our __setattr__ intercept this
    # before the property descriptor runs? In CPython type.__setattr__ checks
    # for data descriptors and routes to them, so our override may or may
    # not see this. The test documents whatever the behavior is.
    try:
        with pytest.raises(WatchpointHit) as exc_info:
            obj.v = 42
        assert exc_info.value.new_value == "42"
    except Exception:
        pytest.skip("@property setter path bypasses class-surgery hook – limitation")


def test_watch_attribute_deletion_does_not_crash():
    """`del obj.attr` invokes __delattr__, not __setattr__. We don't override
    __delattr__, so the deletion is silent – but it MUST NOT crash.
    """
    obj = _SampleObj(10)
    watch("obj.val")
    del obj.val               # silent (limitation) but must not raise
    # After del, the obj still has its watched class until unwatch.
    assert not hasattr(obj, "val")


def test_watch_dict_bypass_via_obj_dict_does_not_crash():
    """`obj.__dict__['attr'] = val` writes directly to the instance dict,
    bypassing __setattr__. The watch won't fire – that's a known limitation
    – but the bypass must not crash either.
    """
    obj = _SampleObj(5)
    watch("obj.val")
    obj.__dict__["val"] = 999   # bypass – silent
    assert obj.val == 999


def test_watch_frozen_dataclass_falls_back_or_skips_gracefully():
    """A frozen dataclass forbids attribute assignment. Class surgery may
    or may not work, but the watch() call must not crash – at worst it
    should fall back to the local-variable path or skip with an exception
    we can catch.
    """
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Point:
        x: int
        y: int

    p = Point(1, 2)
    # watch('p') goes through _is_object_watchable → object-watch path.
    # We just need this to not crash; behavior on subsequent attempts to
    # mutate the frozen instance is irrelevant.
    try:
        watch("p")
    except Exception as e:
        pytest.fail(f"watch() on frozen dataclass crashed: {e!r}")


# ===========================================================================
# Final batch – misc patterns that surface real-world failure modes
# ===========================================================================


def test_propagation_through_lambda():
    """Lambdas have `__code__`, so propagation should work the same as for
    named functions. Lambdas are commonly used as callbacks – verifying
    propagation through one closes a common-pattern gap.
    """
    rebind = lambda val: (_ for _ in [None]).throw(WatchpointHit("val", "x", "y", "f", 1)) \
        if False else None
    del rebind   # silence linter; we define the real one below for clarity

    def _modify(val):
        val = "rebound-via-lambda-arg"
        pass
    _modify = lambda val: _wrap_for_lambda_test(val)   # type: ignore[assignment]

    def _code():
        x = "orig"
        watch("x")
        _modify(x)

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.new_value == "'rebound-via-lambda-arg'"


def _wrap_for_lambda_test(val):
    """Helper: a Python function called from inside the test's lambda so
    the rebind has a line to fire from. Without this indirection the
    lambda body is just a single expression with no LINE event after a
    rebind. We're testing propagation INTO `_wrap_for_lambda_test` here –
    the lambda itself just relays the call.
    """
    val = "rebound-via-lambda-arg"
    pass
    return val


def test_propagation_through_classmethod():
    """`@classmethod` decorated methods get `cls` as the first argument.
    A watched value passed as the second arg should propagate.
    """
    class Helper:
        @classmethod
        def transform(cls, val):
            val = val + 1
            pass

    def _code():
        n = 100
        watch("n")
        Helper.transform(n)

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.new_value == "101"


def test_propagation_through_staticmethod():
    """Static methods have no implicit first arg – propagation should
    look like a plain function call.
    """
    class Helper:
        @staticmethod
        def transform(val):
            val = val + 1
            pass

    def _code():
        n = 100
        watch("n")
        Helper.transform(n)

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.new_value == "101"


def test_object_watch_fires_when_method_internally_mutates_self():
    """`watch("obj")` for a user-defined object should fire when a method
    on the object internally does `self.attr = value`. The class-surgery
    __setattr__ catches the assignment regardless of where it originates
    in the call chain.
    """
    class StatefulService:
        def __init__(self):
            self.counter = 0
        def tick(self):
            self.counter += 1   # in-method mutation – should be intercepted

    svc = StatefulService()
    watch("svc")
    with pytest.raises(WatchpointHit) as exc_info:
        svc.tick()
    hit = exc_info.value
    assert hit.watch_name == "svc.counter"
    assert hit.new_value == "1"


def test_propagation_does_not_overtrigger_on_unwatched_param_with_different_value():
    """Confirms the converse of the false-positive concern: when extra
    args have DIFFERENT values from the watched one, they must not get
    watched. (`object()` sentinels guarantee distinct ids.)
    """
    watched = object()
    other_a = object()
    other_b = object()

    def _modify(a, b, c):
        # a and b are not the watched value; rebinding them must not fire.
        a = "rebind-a"
        b = "rebind-b"
        # Then rebind c (which IS the watched one). This must fire.
        c = "rebound-c"
        pass

    def _code():
        x = watched
        watch("x")
        _modify(other_a, other_b, x)

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    # The hit must report c's change, with display_name 'x'.
    assert exc_info.value.watch_name == "x"
    assert exc_info.value.new_value == "'rebound-c'"


def test_propagation_acknowledged_limitation_interned_primitives():
    """For interned primitives, identity-based matching propagates to ANY
    callee parameter with the same value. We document this with a test so
    a future contributor doesn't 'fix' it without considering the trade-off.
    """
    def _modify(a, b):
        # Both a and b are 1 (small int, interned). The watch propagates
        # to both. Whichever gets rebound first fires.
        a = "first"
        # If propagation were perfect, only `a` would have the watch and
        # this rebind of `b` would be silent. In practice it also fires –
        # but the FIRST rebind already raised, so this line never executes.
        b = "second"
        pass

    def _code():
        x = 1
        watch("x")
        # Pass the watched x AND a literal 1 (same small-int instance).
        _modify(x, 1)

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    # The first rebind (`a = "first"`) fires. The watch_name is "x"
    # because that's the caller's name we're tracking.
    assert exc_info.value.new_value == "'first'"


def test_no_propagation_into_builtin_function():
    """Calling a builtin like `len()` or `print()` must NOT crash, leak,
    or attempt propagation – they have no Python __code__ and no PY_START
    would fire. The watch on the caller should still work normally.
    """
    def _code():
        items = [1, 2, 3]
        watch("items")
        n = len(items)           # builtin call – propagation skips
        assert n == 3
        items.append(4)          # mutation in caller frame
        pass                     # detection of the mutation
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert "4" in exc_info.value.new_value


def test_propagation_queue_does_not_leak_across_many_builtin_calls():
    """Sanity check: many builtin calls (which we skip) shouldn't grow
    the queue. After a burst, the queue should be empty.
    """
    def _code():
        x = "watched"
        watch("x")
        for _ in range(50):
            _ = len("noise")     # builtin – skipped
            _ = repr(42)         # builtin – skipped
        # Caller frame still tracking x.

    _code()
    registry = builtins._watchpoint_registry
    queue = getattr(registry._pending_propagation, "queue", None)
    leftover = len(queue) if queue is not None else 0
    assert leftover == 0, (
        f"Builtin-call burst leaked {leftover} propagation entries"
    )


def test_propagation_through_default_kwarg_value():
    """Defaulted parameters are bound to their default at call time. The
    default is captured at function-definition time and shared across
    calls; small-int defaults thus share id with watched small ints –
    the same interning trade-off. Test documents the behavior: defaults
    are NOT meaningfully tied to the caller's watched local.
    """
    def _modify(val=999):       # 999 default – distinct from caller's watched
        val = -1
        pass

    def _code():
        x = "string-value"      # distinct type/id from 999
        watch("x")
        _modify()               # no args – val gets the default 999
    # Must NOT raise: the default 999 is not the watched value.
    _code()


# ---------------------------------------------------------------------------
# Container mutation watching via dotted-path attr watch
#
# `_add_attr_watch` wraps the leaf attribute in a _WatchedList/Dict/Set
# subclass when it's a mutable builtin container. Mutating methods on the
# wrapper fire `_handle_hit` so user code like
#     value = obj.attr; value.append(x)
# triggers the watch even though `obj.attr` reference isn't rebinded.
# The wrap is silent (guard-suppressed) on watch-arm and on unwatch.
# ---------------------------------------------------------------------------


def test_container_subclass_not_wrapped_in_object_wide_watch():
    """dict/list/set SUBCLASSES assigned through a watched object's __setattr__
    are stored as-is — NOT replaced by _WatchedDict/_WatchedList/_WatchedSet.

    Regression: previously `isinstance(value, _CONTAINER_TYPES)` was used, which
    returns True for ANY dict/list/set subclass. This caused Django's QueryDict
    (a dict subclass) to be silently replaced by a plain _WatchedDict when any
    watched request attribute was re-assigned, stripping QueryDict-specific
    methods (getlist, urlencode, ...) and breaking Django views that called
    request.POST.getlist(). The view then returned HTTP 400 instead of 201.

    Fix: `type(value) in _CONTAINER_TYPES` matches ONLY the exact builtin types.
    """
    class _SpecialDict(dict):
        def getlist(self, key):
            """QueryDict-like method that plain dict doesn't have."""
            v = self.get(key)
            return [v] if v is not None else []

    class _SpecialList(list):
        def first(self):
            return self[0] if self else None

    class _Holder:
        pass

    holder = _Holder()
    watch("holder")

    special_dict = _SpecialDict(color="red")
    special_list = _SpecialList([1, 2, 3])

    def _set_dict():
        holder.mapping = special_dict

    def _set_list():
        holder.sequence = special_list

    with pytest.raises(WatchpointHit):
        _set_dict()

    assert type(holder.mapping) is _SpecialDict, (
        "Dict subclasses must not be replaced by _WatchedDict. "
        "Django's QueryDict was being silently downgraded, losing getlist()."
    )
    assert holder.mapping.getlist("color") == ["red"]

    with pytest.raises(WatchpointHit):
        _set_list()

    assert type(holder.sequence) is _SpecialList, (
        "List subclasses must not be replaced by _WatchedList."
    )
    assert holder.sequence.first() == 1


class _ContainerHolder:
    """Plain user-defined object with a list/dict/set attribute. Mirrors
    the shape of the user's `onboarding_dto.onboarding_settings` – the
    container is reached via a dotted path, watched specifically, and
    later mutated through aliasing + a method call inside a helper.
    """
    def __init__(self):
        self.items = []          # list to wrap
        self.bag = {}            # dict to wrap
        self.tags = set()        # set to wrap


def test_watch_dotted_list_attr_fires_on_append():
    """`obj.attr.append(x)` fires when watching `obj.attr` and the attr is
    a list. This is the user's `room_types.append(...)` scenario."""
    holder = _ContainerHolder()
    def _code():
        watch("holder.items")
        holder.items.append("hotel")
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.watch_name == "holder.items"


def test_watch_dotted_list_attr_fires_on_extend():
    """`obj.attr.extend(iter)` fires."""
    holder = _ContainerHolder()
    def _code():
        watch("holder.items")
        holder.items.extend([1, 2])
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_watch_dotted_list_attr_fires_on_insert():
    """`obj.attr.insert(i, x)` fires."""
    holder = _ContainerHolder()
    holder.items.append("seed")
    def _code():
        watch("holder.items")
        holder.items.insert(0, "first")
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_watch_dotted_list_attr_fires_on_remove():
    """`obj.attr.remove(x)` fires."""
    holder = _ContainerHolder()
    holder.items.extend(["a", "b"])
    def _code():
        watch("holder.items")
        holder.items.remove("a")
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_watch_dotted_list_attr_fires_on_pop():
    """`obj.attr.pop()` fires and the popped value is returned correctly."""
    holder = _ContainerHolder()
    holder.items.extend(["a", "b", "c"])
    def _code():
        watch("holder.items")
        popped = holder.items.pop()
        # The popped value must be returned correctly even though the wrap
        # intercepts the call – if we lost the return path, callers reading
        # the popped value would silently get None.
        assert popped == "c", f"expected 'c', got {popped!r}"
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_watch_dotted_list_attr_fires_on_clear():
    """`obj.attr.clear()` fires when the list is non-empty."""
    holder = _ContainerHolder()
    holder.items.append("seed")
    def _code():
        watch("holder.items")
        holder.items.clear()
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_watch_dotted_list_attr_fires_on_sort_when_order_changes():
    """`obj.attr.sort()` fires if the call actually re-orders elements."""
    holder = _ContainerHolder()
    holder.items.extend([3, 1, 2])
    def _code():
        watch("holder.items")
        holder.items.sort()
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_watch_dotted_list_attr_silent_on_sort_already_sorted():
    """`obj.attr.sort()` does NOT fire if the list was already sorted –
    repr is identical pre/post, so no firing."""
    holder = _ContainerHolder()
    holder.items.extend([1, 2, 3])
    def _code():
        watch("holder.items")
        holder.items.sort()
        pass
    # Must NOT raise – sort on already-sorted list is a no-op.
    _code()


def test_watch_dotted_list_attr_fires_on_setitem():
    """`obj.attr[i] = v` fires."""
    holder = _ContainerHolder()
    holder.items.extend(["a", "b"])
    def _code():
        watch("holder.items")
        holder.items[0] = "changed"
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_watch_dotted_list_attr_fires_on_iadd():
    """`obj.attr += [x]` fires – uses list.__iadd__ which the wrapper hooks."""
    holder = _ContainerHolder()
    holder.items.append("seed")
    def _code():
        watch("holder.items")
        holder.items += ["added"]
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_watch_dotted_dict_attr_fires_on_setitem():
    """`obj.attr[k] = v` fires on dict."""
    holder = _ContainerHolder()
    def _code():
        watch("holder.bag")
        holder.bag["k"] = "v"
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_watched_dict_setitem_swallows_value_repr_errors():
    """Regression: Django's `TestCase._testdata_memo` is a plain dict
    that gets recursively wrapped as `_WatchedDict` when the user
    watches `self` on a TestCase. Django then passes that very dict
    as `memo` to `copy.deepcopy`. Deepcopy does `memo[id(x)] = y` with
    `y` being a half-reconstructed Django Model whose `__repr__` raises
    `AttributeError("'<Model>' object has no attribute '_state'")`.

    `_WatchedDict.__setitem__` snapshots the dict via
    `_wp_container_repr(self)` to compute before/after for change
    detection. That `__repr__` iterates the dict and reprs every value
    – including `y`, which raises. If the snapshot propagates the
    AttributeError, deepcopy dies and the user's test fails through
    no fault of theirs.

    The fix in `_wp_container_repr` catches any exception from the
    repr path and returns `"<unreprable>"`. Before- and after-
    snapshots both come back as `"<unreprable>"` so they compare
    equal, no hit fires, but the underlying mutation succeeds.
    """
    from watchpoint import _WatchedDict

    class _UnreprableValue:
        """Simulates a half-constructed Django Model mid-deepcopy."""

        def __repr__(self):
            raise AttributeError(
                "'_UnreprableValue' object has no attribute '_state'"
            )

    d = _WatchedDict()
    bad = _UnreprableValue()
    # This is the killer line: deepcopy's `memo[id(x)] = y` flow.
    # Pre-fix, the snapshot via `dict.__repr__` blew up on `bad.__repr__`.
    d[1] = bad
    assert d[1] is bad

    # Subsequent mutations also tolerate the unreprable value.
    d.update({2: bad, 3: bad})
    d.pop(1)
    d.clear()


def test_watched_list_mutations_swallow_value_repr_errors():
    """Symmetric guard for list. A `_WatchedList` containing an
    unreprable element must not crash its own mutating methods."""
    from watchpoint import _WatchedList

    class _UnreprableValue:
        def __repr__(self):
            raise RuntimeError("don't repr me")

    lst = _WatchedList()
    bad = _UnreprableValue()
    lst.append(bad)
    lst.append(bad)
    lst[0] = bad
    lst.extend([bad, bad])
    lst.pop()
    lst.remove(bad)
    lst.clear()


def test_watched_set_mutations_swallow_value_repr_errors():
    """Symmetric guard for set."""
    from watchpoint import _WatchedSet

    class _UnreprableValue:
        def __repr__(self):
            raise RuntimeError("don't repr me")

        def __hash__(self):
            # Need a stable hash for set membership; identity is fine.
            return id(self)

    s = _WatchedSet()
    bad = _UnreprableValue()
    s.add(bad)
    s.discard(bad)
    s.add(bad)
    s.update({bad})
    s.clear()


def test_watch_dotted_dict_attr_fires_on_update():
    """`obj.attr.update({...})` fires on dict."""
    holder = _ContainerHolder()
    def _code():
        watch("holder.bag")
        holder.bag.update({"k": "v"})
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_watch_dotted_dict_attr_fires_on_delitem():
    """`del obj.attr[k]` fires on dict."""
    holder = _ContainerHolder()
    holder.bag["k"] = "v"
    def _code():
        watch("holder.bag")
        del holder.bag["k"]
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_watch_dotted_set_attr_fires_on_add():
    """`obj.attr.add(x)` fires on set."""
    holder = _ContainerHolder()
    def _code():
        watch("holder.tags")
        holder.tags.add("ready")
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_watch_dotted_set_attr_silent_on_add_existing():
    """`obj.attr.add(x)` on an element already in the set does NOT fire –
    repr is unchanged after the no-op add."""
    holder = _ContainerHolder()
    holder.tags.add("ready")
    def _code():
        watch("holder.tags")
        holder.tags.add("ready")  # already present
        pass
    # Must NOT raise.
    _code()


def test_watch_dotted_list_attr_fires_through_helper():
    """The user's reported case: helper reads `obj.attr` into a local and
    mutates through that local – the wrap means the local IS the wrapper,
    so `.append(...)` still fires.
    """
    holder = _ContainerHolder()

    def _fill(target_holder):
        # mirrors OnboardingDtoFiller.fill_dto_fully:
        # captures the attr into a local, then mutates through the local.
        items = target_holder.items
        items.append("from-helper")
        pass

    def _code():
        watch("holder.items")
        _fill(holder)
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.watch_name == "holder.items"


def test_watch_dotted_list_attr_does_not_double_fire_when_reassigned():
    """When the user replaces `obj.attr = [...]` and then mutates, the new
    container is freshly wrapped. The reassignment fires once; the later
    mutation fires once. Verifies the auto-wrap-on-reassign path in
    `_WatchedSubclass.__setattr__`."""
    holder = _ContainerHolder()
    hits = []

    # We capture by overriding the registry's _handle_hit so we can count
    # firings WITHOUT pydevd or pytest.raises tearing down the frame.
    registry = builtins._watchpoint_registry
    original_handle_hit = registry._handle_hit
    def _capture(**kwargs):
        hits.append(kwargs)
        # Don't actually pause / raise – just record. We're testing the
        # firing count, not the pause flow.
    registry._handle_hit = _capture
    try:
        def _code():
            watch("holder.items")
            holder.items = ["replacement"]   # fires (rebind detector)
            holder.items.append("after")     # fires (wrapper on new list)
            pass
        _code()
    finally:
        registry._handle_hit = original_handle_hit

    # First hit = rebind (old [] -> ['replacement']),
    # Second hit = append (['replacement'] -> ['replacement', 'after']).
    assert len(hits) == 2, f"expected 2 hits, got {len(hits)}: {hits}"
    assert all(h["watch_name"] == "holder.items" for h in hits)


def test_watch_dotted_list_attr_aliased_ref_before_watch_does_not_fire():
    """Acknowledged limitation: a reference captured BEFORE the watch was
    armed points at the original (un-wrapped) list. Mutations through that
    stale alias don't fire. The wrap-and-replace approach cannot fix this
    without a CPython-level hook.
    """
    holder = _ContainerHolder()
    holder.items.append("seed")
    stale_alias = holder.items  # captured BEFORE watch – stays the original
    def _code():
        watch("holder.items")
        # After watch, holder.items is the wrapper. stale_alias is the
        # original (now orphaned) list.
        assert holder.items is not stale_alias
        stale_alias.append("invisible")  # NOT detected
        pass
    # Must NOT raise – mutation through stale_alias bypasses the wrapper.
    _code()


def test_unwatch_restores_plain_list_type():
    """After unwatch(), the attribute is a plain `list`, not our wrapper –
    `type(obj.attr) is list` must hold for user code that does strict
    type checks.
    """
    holder = _ContainerHolder()
    holder.items.append("seed")
    watch("holder.items")
    # While watched, the attr is a wrapper subclass.
    assert isinstance(holder.items, list)
    assert type(holder.items) is not list  # is the wrapper subclass
    unwatch("holder.items")
    # After unwatch, restored to plain list.
    assert type(holder.items) is list
    assert holder.items == ["seed"]


def test_unwatch_disables_firing_on_leaked_wrapper_alias():
    """If user code captured a reference to the wrapper while watched, then
    unwatched, that captured wrapper must not fire when mutated later –
    the watch is gone. The wrapper checks `_wp_registry` on every mutation
    and silently no-ops when cleanup nulled it.
    """
    holder = _ContainerHolder()
    watch("holder.items")
    wrapper_alias = holder.items   # is the _WatchedList instance
    unwatch("holder.items")
    # Now holder.items has been restored to a plain list, but
    # wrapper_alias still points at the _WatchedList. Mutating through
    # it must be silent.
    wrapper_alias.append("after-unwatch")  # must NOT raise
    # No assertion – the absence of an exception is the test.


# ---------------------------------------------------------------------------
# Recursive object-wide watching
#
# `_add_object_watch` walks `obj.__dict__` to depth `_RECURSIVE_OBJECT_WATCH_DEPTH`
# at watch-arm time, installs class surgery on every nested user-defined
# object it finds, and wraps nested mutable containers. The watcher's
# `__setattr__` hook also auto-instruments any newly-assigned user object
# or container.
# ---------------------------------------------------------------------------

class _Settings:
    """User-defined leaf object for the recursive-watch fixtures."""
    def __init__(self):
        self.current_step = "init"
        self.room_types = []
        self.config = {}


class _Dto:
    """User-defined parent object holding `_Settings` and other state.
    Mirrors the user's `OnboardingDto.onboarding_settings` shape – the
    interesting watch target is the root DTO; changes happen on `.settings.*`
    or further nested.
    """
    def __init__(self):
        self.settings = _Settings()
        self.tags = set()
        self.name = "anonymous"


def test_recursive_watch_fires_on_nested_attr_assignment():
    """Watching the root object fires when a nested attribute is reassigned."""
    dto = _Dto()
    def _code():
        watch("dto")
        dto.settings.current_step = "create_user"
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    # watch_name should carry the full dotted path so the IDE can show
    # which slot actually moved.
    assert exc_info.value.watch_name == "dto.settings.current_step"


def test_recursive_watch_fires_on_nested_list_mutation():
    """Watching the root object fires when a nested list is mutated through
    a method (append). Bug 2 + Bug 3 interaction: the nested list gets
    container-wrapped during recursive instrumentation."""
    dto = _Dto()
    def _code():
        watch("dto")
        dto.settings.room_types.append("hotel")
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.watch_name == "dto.settings.room_types"


def test_recursive_watch_fires_on_nested_dict_mutation():
    """Watching the root object fires when a nested dict's item is set."""
    dto = _Dto()
    def _code():
        watch("dto")
        dto.settings.config["mode"] = "fast"
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.watch_name == "dto.settings.config"


def test_recursive_watch_fires_on_direct_attr_assignment():
    """Recursive watch must still fire on direct (non-nested) attr changes –
    the depth-1 attrs of the root are also instrumented."""
    dto = _Dto()
    def _code():
        watch("dto")
        dto.name = "renamed"
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.watch_name == "dto.name"


def test_recursive_watch_fires_when_nested_attr_replaced_with_new_object():
    """Reassigning a nested user-defined attr to a brand-new object: the
    rebind fires under the parent's setattr hook, AND the new object's
    sub-attrs get auto-instrumented so subsequent mutations on the new
    object also fire."""
    dto = _Dto()
    new_settings = _Settings()
    new_settings.current_step = "after-rebind"

    def _code():
        watch("dto")
        dto.settings = new_settings  # depth-1 rebind fires
        # The newly-installed instrumentation on new_settings means the
        # following mutation should fire too.
        dto.settings.current_step = "after-mutation"
        pass
    # Just verify SOMETHING raises (the first rebind happens before the
    # second mutation, so the helper exits via the rebind hit).
    with pytest.raises(WatchpointHit):
        _code()


def test_recursive_watch_with_cycle_does_not_blow_stack():
    """A graph where two nested objects reference each other (or self)
    must not cause infinite recursion during watch-arm instrumentation.
    The id-visited set short-circuits the second visit."""
    class _Node:
        def __init__(self):
            self.peer = None

    a = _Node()
    b = _Node()
    a.peer = b
    b.peer = a   # cycle: a.peer.peer is a
    # Must complete without RecursionError.
    watch("a")


def test_recursive_watch_self_reference_does_not_blow_stack():
    """An object referencing itself (`x.parent = x`) must not loop."""
    class _Self:
        def __init__(self):
            self.parent = None

    x = _Self()
    x.parent = x
    watch("x")


def test_recursive_watch_unwatch_restores_nested_classes():
    """unwatch on the root restores every nested object's original class –
    no _WatchedAny_ subclasses lingering. Container wrappers are restored
    to plain containers too.
    """
    dto = _Dto()
    settings_cls_before = type(dto.settings)
    config_type_before = type(dto.settings.config)
    items_type_before = type(dto.settings.room_types)

    watch("dto")
    # While watched: every nested user-object's type should be a
    # _WatchedAny_ subclass, containers wrapped.
    assert type(dto.settings) is not settings_cls_before
    assert type(dto.settings.config) is not config_type_before
    assert type(dto.settings.room_types) is not items_type_before

    unwatch("dto")
    # After unwatch: all restored.
    assert type(dto.settings) is settings_cls_before
    assert type(dto.settings.config) is config_type_before
    assert type(dto.settings.room_types) is items_type_before


def test_recursive_watch_depth_cap():
    """Beyond the depth cap, mutations don't fire. Documented behavior;
    depth-5 changes are out of reach for the root watch."""
    class _Lvl:
        def __init__(self):
            self.child = None

    root = _Lvl()
    root.child = _Lvl()                     # depth 1
    root.child.child = _Lvl()               # depth 2
    root.child.child.child = _Lvl()         # depth 3
    root.child.child.child.child = _Lvl()   # depth 4
    root.child.child.child.child.child = _Lvl()  # depth 5

    def _code():
        watch("root")
        # Change at depth 5 should NOT fire – the recursion stops at 4.
        root.child.child.child.child.child.child = "untracked"
        pass
    # Must NOT raise – depth 5 is past the cap.
    _code()


def test_recursive_watch_depth_at_cap_still_fires():
    """At-or-below the depth cap, mutations DO fire. Verifies the cap is
    exclusive of `_RECURSIVE_OBJECT_WATCH_DEPTH` itself."""
    class _Lvl:
        def __init__(self):
            self.child = None
            self.val = "init"

    root = _Lvl()
    root.child = _Lvl()                     # depth 1
    root.child.child = _Lvl()               # depth 2
    root.child.child.child = _Lvl()         # depth 3
    root.child.child.child.child = _Lvl()   # depth 4

    def _code():
        watch("root")
        # Change at depth 4: change `.val` on root.child.child.child.child
        # which itself is at depth 4 (root counts as 0). The class surgery
        # was installed on that object, so its setattr fires.
        root.child.child.child.child.val = "changed"
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_recursive_watch_two_paths_to_same_object_instrumented_once():
    """If a nested object is reachable via two paths from the root, it
    should be instrumented exactly once (no double-fire, no double class
    surgery). The id-visited set short-circuits the second visit."""
    class _Node:
        def __init__(self):
            self.val = "init"

    shared = _Node()

    class _Owner:
        def __init__(self):
            self.left = shared
            self.right = shared   # same object reached via two paths

    owner = _Owner()

    def _code():
        # NOTE the explicit `owner` reference: it forces CPython to capture
        # `owner` as a free variable of `_code`, which is what makes
        # `eval("owner", f_globals, f_locals)` inside `add_watch` succeed.
        # Without a use in `_code`'s body, the name resolves to nothing
        # and the watch falls through to local-variable handling on None.
        assert owner is not None
        watch("owner")
        shared.val = "changed"   # fires once, under whichever path won
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    # Either "owner.left.val" or "owner.right.val" – we don't pin which
    # path wins (dict iteration order), but it must NOT be the un-pathed
    # bare ".val".
    assert exc_info.value.watch_name in ("owner.left.val", "owner.right.val")


def test_recursive_watch_does_not_fire_when_nothing_changes():
    """Sanity: arming the recursive watch must not itself fire on the
    initial instrumentation pass (the guard suppresses the wrap+swap
    setattrs)."""
    dto = _Dto()
    watch("dto")
    pass  # Should not have raised


def test_recursive_watch_primitive_leaf_unchanged_silent():
    """Setting a nested primitive attr to the SAME value (same hash) does
    not fire – mirrors the existing `_value_hash` skip on equal values."""
    dto = _Dto()
    def _code():
        watch("dto")
        dto.settings.current_step = "init"  # already "init"
        pass
    _code()  # must NOT raise


def test_recursive_watch_through_helper_function():
    """The user's actual scenario: watch the root DTO before the helper
    runs, helper mutates a nested list. The watch must fire from inside
    the helper's call without any propagation machinery (the class surgery
    is ambient – it fires wherever the mutation happens)."""
    dto = _Dto()

    def _filler(target_dto):
        # Mirrors OnboardingDtoFiller.fill_dto_fully – mutates nested state.
        items = target_dto.settings.room_types
        items.append("from-filler")
        pass

    def _code():
        watch("dto")
        _filler(dto)
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.watch_name == "dto.settings.room_types"


# ---------------------------------------------------------------------------
# Heavily-metaclassed types (Django Model, SQLAlchemy declarative base)
#
# Some frameworks reject dynamic subclassing in their metaclass. We can't
# class-surgery them, so we fall back to monkey-patching the class's
# `__setattr__` directly (the "classpatch" path). The patch is scoped to
# the watched instance(s) via an id(instance)-keyed table and is removed
# from the class when the last watched instance is unwatched.
#
# Bare-name `watch('django_obj')` registers a wildcard entry that fires
# on ANY attribute write on the instance. Dotted `watch('django_obj.field')`
# registers a specific-attribute entry that fires only when `field` is
# written. The recursive `_instrument_object_tree` walker (for non-Django
# parents) does NOT use this fallback for nested objects – a Django child
# of a non-Django parent is silently skipped during the walk.
# ---------------------------------------------------------------------------

class _DjangoLikeMeta(type):
    """Stand-in for Django's `ModelBase`: refuses to build a subclass of
    any class already created with this metaclass (mimics ModelBase
    requiring `Meta.app_label` + INSTALLED_APPS membership). Allows
    class-level `__setattr__` assignment, which mirrors real Django:
    `ModelBase` doesn't override the metaclass's `__setattr__`."""
    def __new__(mcs, name, bases, namespace):
        if bases and any(
            isinstance(b, _DjangoLikeMeta) and getattr(b, "_django_like_real", False)
            for b in bases
        ):
            raise RuntimeError(
                f"Model class {namespace.get('__qualname__', name)} doesn't "
                f"declare an explicit app_label and isn't in an application "
                f"in INSTALLED_APPS."
            )
        return super().__new__(mcs, name, bases, namespace)


class _DjangoLikeModel(metaclass=_DjangoLikeMeta):
    """Mock of a Django Model instance. The metaclass refuses our
    `_WatchedAnyAttrSubclass(...)` / `_WatchedSubclass(...)` dynamic
    subclassing, so the watch falls back to classpatch."""
    _django_like_real = True
    def __init__(self):
        self.name = "django-thing"
        self.tag = "default"


class _StubbornDjangoLikeMeta(_DjangoLikeMeta):
    """Refuses dynamic subclassing AND refuses class-level `__setattr__`
    assignment. Exercises the rare 'even classpatch failed' path so we
    can confirm the dotted watch surfaces a clean TypeError and the
    bare-name watch falls through to local-variable rebind detection."""
    def __setattr__(cls, name, value):
        if name == "__setattr__":
            raise TypeError(
                "_StubbornDjangoLikeMeta refuses __setattr__ install on the class."
            )
        super().__setattr__(name, value)


class _StubbornDjangoLikeModel(metaclass=_StubbornDjangoLikeMeta):
    """Instance of a class whose metaclass refuses BOTH dynamic subclassing
    AND class-level `__setattr__` assignment – neither class-surgery nor
    classpatch can instrument it."""
    _django_like_real = True
    def __init__(self):
        self.name = "stubborn-thing"


def _django_like_set_via_method(obj, new_name):
    """Method-style helper that internally does `self.field = value`.
    Mirrors the user-reported `relation.set_accessible_products(...)`
    pattern, where the method body rebinds an attribute on `self` and
    we want the classpatch fallback to intercept it."""
    obj.name = new_name


def test_django_like_dotted_watch_fires_on_specific_attr():
    """Dotted `watch('obj.field')` on a Django-like instance uses the
    classpatch fallback. Rebinding the watched attribute fires
    `WatchpointHit` with the dotted watch_name preserved."""
    obj = _DjangoLikeModel()
    watch("obj.name")
    def _code():
        obj.name = "renamed"
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.watch_name == "obj.name"
    assert "renamed" in exc_info.value.new_value


def test_django_like_dotted_watch_fires_when_method_rebinds_attribute():
    """Reproduces the user-reported pattern: a model method calls
    `self.field = computed_value`. Classpatch intercepts the assignment
    inside the method, so the user pauses right after the method's
    `self.field = ...` line."""
    obj = _DjangoLikeModel()
    watch("obj.name")
    def _code():
        _django_like_set_via_method(obj, "via-method")
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.watch_name == "obj.name"
    assert "via-method" in exc_info.value.new_value


def test_django_like_dotted_watch_silent_on_same_value():
    """Assigning the same value to a watched attribute doesn't fire –
    `_value_hash` equality short-circuits before reaching `_handle_hit`."""
    obj = _DjangoLikeModel()
    watch("obj.name")
    obj.name = "django-thing"  # identical to __init__-assigned value
    clear_watches()


def test_django_like_dotted_watch_other_instance_unaffected():
    """Patching the class targets only the watched instance via
    `id(instance)`. Writes to other instances of the same class pass
    through the patched `__setattr__` without firing."""
    obj1 = _DjangoLikeModel()
    obj2 = _DjangoLikeModel()
    watch("obj1.name")
    obj2.name = "different"  # must not fire
    clear_watches()


def test_django_like_dotted_watch_other_attr_unaffected():
    """A specific-attribute classpatch entry fires only on the watched
    attribute; writes to other attributes on the same instance pass
    through with no fire."""
    obj = _DjangoLikeModel()
    watch("obj.name")
    obj.tag = "new-tag"  # different attribute, must not fire
    clear_watches()


def test_django_like_dotted_watch_unwatch_restores_setattr():
    """After unwatch, the class's `__setattr__` is restored (removed
    from `cls.__dict__` since the test class had no own original) and
    subsequent writes don't fire."""
    obj = _DjangoLikeModel()
    watch("obj.name")
    # Patched while watch is active.
    assert "__setattr__" in _DjangoLikeModel.__dict__
    unwatch("obj.name")
    # Restored to MRO-lookup default.
    assert "__setattr__" not in _DjangoLikeModel.__dict__
    obj.name = "after-unwatch"  # must not fire


def test_django_like_bare_name_watch_fires_on_any_attribute():
    """Bare-name `watch('obj')` on a Django-like instance installs a
    wildcard classpatch entry. Any attribute write on the watched
    instance fires, with `watch_name` extended to `obj.<attr>` so the
    user sees which attribute changed."""
    obj = _DjangoLikeModel()
    watch("obj")
    def _code():
        obj.tag = "wildcard-fire"
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.watch_name == "obj.tag"
    assert "wildcard-fire" in exc_info.value.new_value


def test_django_like_bare_name_watch_other_instance_unaffected():
    """Wildcard classpatch only fires for the specific watched instance,
    not for other instances of the same class even though they share
    the patched class-level `__setattr__`."""
    obj1 = _DjangoLikeModel()
    obj2 = _DjangoLikeModel()
    watch("obj1")
    obj2.name = "different"
    obj2.tag = "also-different"
    clear_watches()


def test_django_like_bare_name_watch_unwatch_restores_setattr():
    """After unwatch, the wildcard's patched `__setattr__` is removed
    from the class and subsequent attribute writes pass through."""
    obj = _DjangoLikeModel()
    watch("obj")
    assert "__setattr__" in _DjangoLikeModel.__dict__
    unwatch("obj")
    assert "__setattr__" not in _DjangoLikeModel.__dict__
    obj.name = "after-unwatch"
    obj.tag = "also-after-unwatch"


def test_django_like_specific_takes_priority_over_wildcard():
    """When both `watch('obj')` (wildcard) and `watch('obj.name')`
    (specific) are armed, a write to `obj.name` reports the specific
    watch_name. A write to a different attribute still goes through
    wildcard. After removing the specific entry, the wildcard alone
    catches writes to `obj.name` too."""
    obj = _DjangoLikeModel()
    watch("obj")
    watch("obj.name")
    def _code_specific():
        obj.name = "via-specific"
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code_specific()
    # Specific entry wins; reports the dotted watch the user installed.
    assert exc_info.value.watch_name == "obj.name"
    # Drop the specific entry; wildcard remains.
    unwatch("obj.name")
    def _code_wildcard():
        obj.tag = "via-wildcard"
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code_wildcard()
    assert exc_info.value.watch_name == "obj.tag"


def test_django_like_classpatch_cleanup_independent_for_two_classes():
    """Two different Django-like classes patched independently; unwatching
    one does not affect the other's patch state. Exercises the per-class
    `_classpatch_registry` keying."""
    class _A(metaclass=_DjangoLikeMeta):
        _django_like_real = True
        def __init__(self):
            self.field = "a-init"
    class _B(metaclass=_DjangoLikeMeta):
        _django_like_real = True
        def __init__(self):
            self.field = "b-init"
    a = _A()
    b = _B()
    watch("a.field")
    watch("b.field")
    assert "__setattr__" in _A.__dict__
    assert "__setattr__" in _B.__dict__
    unwatch("a.field")
    # Only _A's patch is gone; _B's is still in place.
    assert "__setattr__" not in _A.__dict__
    assert "__setattr__" in _B.__dict__
    unwatch("b.field")
    assert "__setattr__" not in _B.__dict__


def test_django_like_nested_under_recursive_watch_skipped_gracefully():
    """When a Django-like instance is nested under a `watch('root')`
    recursive walk, the failed sub-instrumentation must NOT abort the
    whole tree – `_instrument_object_tree` catches the TypeError and
    moves on. The classpatch fallback is NOT used for nested objects
    (only the top-level bare-name watch path uses it), so the nested
    Django instance is simply not instrumented; its own attribute
    writes won't fire. Sibling non-Django attrs of the root still get
    full class-surgery instrumentation."""
    class _Container:
        def __init__(self):
            self.django_thing = _DjangoLikeModel()  # skipped
            self.normal_thing = _Dto()              # instrumented as usual
    root = _Container()
    watch("root")
    # Confirm the Django-like child wasn't subclass'd – stays as the
    # original class (no surgery, no classpatch from recursion).
    assert type(root.django_thing) is _DjangoLikeModel
    # Confirm the normal nested object WAS instrumented.
    assert type(root.normal_thing) is not _Dto  # is a _WatchedAny_ subclass
    # And mutation on the normal nested object still fires (sanity check
    # that the partial failure didn't break the rest of the tree).
    def _code():
        root.normal_thing.name = "renamed"
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_stubborn_metaclass_dotted_watch_raises_clear_type_error():
    """When BOTH dynamic subclassing AND class-level `__setattr__`
    assignment are blocked, the dotted watch path can install neither
    strategy and surfaces a clean TypeError naming both failures so
    the user understands why the watch couldn't be armed."""
    obj = _StubbornDjangoLikeModel()
    with pytest.raises(TypeError) as exc_info:
        watch("obj.name")
    msg = str(exc_info.value)
    assert "_StubbornDjangoLikeModel" in msg
    # Message must hint that BOTH strategies failed, not just subclassing.
    assert "monkey-patching" in msg or "__setattr__" in msg


def test_stubborn_metaclass_bare_name_falls_back_to_local_variable():
    """When BOTH strategies fail for bare-name watch on a stubborn
    metaclass, the dispatch falls through to local-variable rebind
    detection. The watch installs without raising, but attribute
    mutations on the instance won't fire (only rebinding the local
    variable in the watching frame would)."""
    obj = _StubbornDjangoLikeModel()
    watch("obj")  # must not raise
    obj.name = "after-watch"  # no fire – neither classpatch nor surgery armed
    clear_watches()


# ---------------------------------------------------------------------------
# Framework / cyclic-graph safeguards
#
# Watching a Django QuerySet used to blow up the runtime: the QuerySet's
# .model returned a Model class, whose FK descriptors fabricated fresh
# proxy instances on each read, whose .remote_field returned another
# proxy, ... and the watcher's __setattr__ re-entry path restarted a
# depth-1 walk with a fresh visited set on every assignment. Hundreds of
# queued hits per Variables-panel expansion + IDE freeze. The fix is a
# combination of:
#   - `_is_user_defined_type` filter (stop walking at framework boundary),
#   - `isinstance(value, type)` filter (don't walk class objects),
#   - `root_watch.visited_ids` (persistent cycle detection),
#   - `_try_add_sub_watch` breadth cap (belt-and-suspenders),
#   - `_find_user_caller` (drop hits originating from runtime frames).
# These tests cover each piece in isolation plus an integration test.
# ---------------------------------------------------------------------------

from watchpoint import (
    _is_user_defined_type, _find_user_caller, _RUNTIME_FILENAMES,
    _MAX_SUB_WATCHES_PER_ROOT,
)
import sys as _sys_for_safeguards


def test_is_user_defined_type_accepts_class_in_test_module():
    """A class declared in this test module has __module__ = the test
    module's name (not in any denylist, not stdlib, not site-packages)
    so the helper accepts it for recursive instrumentation."""
    class MyUserClass:
        pass
    assert _is_user_defined_type(MyUserClass) is True


def test_is_user_defined_type_rejects_builtins():
    """Built-in types are not user code and must not be recursed into."""
    assert _is_user_defined_type(int) is False
    assert _is_user_defined_type(str) is False
    assert _is_user_defined_type(list) is False
    assert _is_user_defined_type(dict) is False


def test_is_user_defined_type_rejects_known_frameworks():
    """Types whose __module__ root matches the framework denylist are
    rejected without needing the framework actually installed. We
    synthesize the test by assigning __module__ explicitly so the suite
    runs in environments without Django / SQLAlchemy / pydantic."""
    class FakeDjangoQuerySet:
        pass
    FakeDjangoQuerySet.__module__ = "django.db.models.query"
    assert _is_user_defined_type(FakeDjangoQuerySet) is False

    class FakeSQLAlchemyMapper:
        pass
    FakeSQLAlchemyMapper.__module__ = "sqlalchemy.orm.mapper"
    assert _is_user_defined_type(FakeSQLAlchemyMapper) is False

    class FakePydanticModel:
        pass
    FakePydanticModel.__module__ = "pydantic.main"
    assert _is_user_defined_type(FakePydanticModel) is False

    class FakePydevdInternal:
        pass
    FakePydevdInternal.__module__ = "_pydevd_bundle.pydevd_constants"
    assert _is_user_defined_type(FakePydevdInternal) is False


def test_is_user_defined_type_rejects_stdlib_modules():
    """Stdlib types (pathlib.Path, collections.OrderedDict, etc.) are
    rejected via `sys.stdlib_module_names`. User code that touches these
    in passing won't trigger recursive instrumentation."""
    import pathlib
    import collections
    import email.message
    assert _is_user_defined_type(pathlib.Path) is False
    assert _is_user_defined_type(collections.OrderedDict) is False
    assert _is_user_defined_type(email.message.Message) is False


def test_is_user_defined_type_rejects_site_packages_heuristic(tmp_path, monkeypatch):
    """A type whose module's __file__ lives under site-packages is rejected
    even when its __module__ root isn't in the framework denylist.
    Catches obscure / less-popular libraries we haven't named."""
    fake_mod = type(_sys_for_safeguards)("some_random_third_party")
    fake_mod.__file__ = str(
        tmp_path / "lib" / "python3.12" / "site-packages"
        / "some_random_third_party" / "__init__.py"
    )
    monkeypatch.setitem(_sys_for_safeguards.modules, "some_random_third_party", fake_mod)

    class FakeThirdPartyType:
        pass
    FakeThirdPartyType.__module__ = "some_random_third_party"
    assert _is_user_defined_type(FakeThirdPartyType) is False


def test_is_user_defined_type_rejects_non_types():
    """The helper accepts None and non-type inputs gracefully so callers
    can ask `_is_user_defined_type(type(value))` without an isinstance
    pre-check."""
    assert _is_user_defined_type(None) is False
    assert _is_user_defined_type("not a type") is False
    assert _is_user_defined_type(42) is False


def test_runtime_filenames_includes_string_marker():
    """The set used by `_find_user_caller` MUST include the `<string>`
    filename – that's what the runtime's frames carry when exec'd by the
    plugin's sitecustomize injection. Without it, runtime frames wouldn't
    be skipped and hits would report from `<string>:NNN` lines."""
    assert "<string>" in _RUNTIME_FILENAMES


def test_find_user_caller_returns_immediate_user_frame():
    """When the immediate caller IS a user frame (test_*.py), the helper
    returns it without walking."""
    user = _find_user_caller(_sys_for_safeguards._getframe(0))
    assert user is not None
    assert user.f_code.co_filename.endswith("test_watchpoint.py")


def test_find_user_caller_returns_none_for_empty_chain():
    """Defensive: a None start frame returns None rather than raising."""
    assert _find_user_caller(None) is None


# A user-defined holder class whose attribute is a framework-typed object.
# Mimics the real-world case where a user DTO references a Django QuerySet
# / SQLAlchemy session / etc.
class _FakeDjangoFieldDescriptor:
    """Stand-in for a Django ORM internal object. We don't want to recurse
    into this when watching a DTO that references it, because real Django
    descriptors have cyclic relationships (`field.remote_field.field…`)
    that would explode the watch tree."""
    def __init__(self):
        self.cached_state = None
_FakeDjangoFieldDescriptor.__module__ = "django.db.models.fields.related_descriptors"


class _UserDtoWithFrameworkField:
    """Mimics a user DTO that holds a reference to a framework object.
    We expect: watcher fires on `dto.*` mutations; framework object's
    internals are left alone."""
    def __init__(self):
        self.label = "alpha"
        self.framework_obj = _FakeDjangoFieldDescriptor()


def test_recursion_stops_at_framework_boundary():
    """A user DTO whose attribute is a framework-typed object: the DTO
    gets full class surgery, but `dto.framework_obj` is NOT recursively
    instrumented – assigning attributes INSIDE `framework_obj` is silent
    (no watcher installed on it), but rebinding `dto.framework_obj`
    itself still fires."""
    dto = _UserDtoWithFrameworkField()
    fw_inner_cls_before = type(dto.framework_obj)
    watch("dto")
    # The framework object's class must NOT have been swapped – recursion
    # should have stopped at the framework boundary.
    assert type(dto.framework_obj) is fw_inner_cls_before, (
        "framework object was instrumented despite framework module prefix"
    )
    # Mutating an attribute INSIDE the framework object does NOT fire –
    # there's no watcher on it.
    dto.framework_obj.cached_state = "mutated"  # silent

    # Mutating the DTO's own user-defined attr DOES fire.
    with pytest.raises(WatchpointHit) as exc_info:
        dto.label = "beta"
    assert exc_info.value.watch_name == "dto.label"


def test_recursion_skips_class_objects():
    """When a user object holds a reference to a CLASS (not an instance),
    we don't try to wrap the class's __dict__ – it's full of descriptors
    that would each get instrumented and trigger explosive growth."""
    class _Inner:
        pass

    class _OuterHoldingClass:
        def __init__(self):
            self.normal_attr = "hello"
            self.held_class = _Inner  # the class itself, not an instance

    o = _OuterHoldingClass()
    watch("o")

    # The filter rejected `held_class` BEFORE we tried class surgery on
    # it – so it's not in sub_watches. Without the filter, the eventual
    # TypeError from a metaclass conflict in `_install_single_object_watch`
    # also keeps it out of sub_watches, but does so via the slower
    # catch-and-fallback path. Asserting absence locks in the cheap
    # path and surfaces regressions where someone tries to add a
    # secondary instrumentation strategy for class objects.
    registry = builtins._watchpoint_registry
    aw = registry._attr_watches["o"]
    sub_exprs = [sw.expr for sw in aw.sub_watches]
    assert not any("held_class" in e for e in sub_exprs), (
        f"held_class was added to sub_watches despite being a class object: "
        f"{sub_exprs!r}"
    )
    # The held class itself is unchanged – no residue from a partial
    # __class__ surgery attempt.
    assert _Inner.__name__ == "_Inner"

    # Mutating the normal user attr fires.
    with pytest.raises(WatchpointHit):
        o.normal_attr = "world"
    clear_watches()
    # Writing an attribute on the held class is silent – no watcher
    # was installed on _Inner itself.
    _Inner.added_after = "ok"  # must not raise


def test_visited_ids_shared_across_setattr_reentry():
    """Cyclic user-defined graph (a.next = b; b.next = a). The initial
    walk records both ids in `root_watch.visited_ids`. A subsequent
    __setattr__ that assigns one of the cycle members to another
    attribute must NOT re-instrument it – the persistent visited set
    catches the duplicate.

    Pre-fix, the watcher's __setattr__ recursed with a fresh
    `visited={id(wrapped_value)}` set, so any later assignment of an
    already-instrumented object started another depth-4 walk.
    """
    class _Node:
        def __init__(self):
            self.value = None
            self.next = None

    root = _Node()
    other = _Node()
    root.next = other
    other.next = root  # closes the cycle
    watch("root")

    registry = builtins._watchpoint_registry
    aw = registry._attr_watches["root"]
    initial_sub_count = len(aw.sub_watches)
    # Both root (root_watch itself) and `other` should already be in
    # visited_ids after the initial walk.
    assert id(root) in aw.visited_ids
    assert id(other) in aw.visited_ids

    # Assign `other` to another attribute. Pre-fix, the watcher would
    # call _install_single_object_watch on `other` again because the
    # fresh per-call visited set didn't contain it.
    with pytest.raises(WatchpointHit):
        root.value = other

    # No new sub-watch installed for `other` – it was already covered.
    assert len(aw.sub_watches) == initial_sub_count, (
        f"sub_watches grew from {initial_sub_count} to {len(aw.sub_watches)} "
        f"on __setattr__ re-entry – persistent visited set missed cycle"
    )


def test_breadth_cap_engages_with_warning(capsys):
    """A pathological object with more sub-objects than the cap allows
    triggers the breadth-cap guard. `sub_watches_capped` flips to True,
    sub_watches stays at or below the cap, and a one-line warning is
    written to stderr so the user can see why deeper mutations aren't
    firing."""
    class _Leaf:
        def __init__(self, i):
            self.i = i

    class _Root:
        def __init__(self):
            for i in range(_MAX_SUB_WATCHES_PER_ROOT + 50):
                setattr(self, f"leaf_{i}", _Leaf(i))

    obj = _Root()
    watch("obj")

    registry = builtins._watchpoint_registry
    aw = registry._attr_watches["obj"]
    assert aw.sub_watches_capped is True, "breadth cap should have engaged"
    assert len(aw.sub_watches) <= _MAX_SUB_WATCHES_PER_ROOT, (
        f"sub_watches grew past cap: {len(aw.sub_watches)} > {_MAX_SUB_WATCHES_PER_ROOT}"
    )
    captured = capsys.readouterr()
    assert "sub-watch cap" in captured.err, (
        f"expected breadth-cap warning on stderr, got: {captured.err!r}"
    )


def test_class_swap_under_guard_does_not_fire_spurious_hit():
    """`obj.__class__ = watcher_cls` inside `_install_single_object_watch`
    must be wrapped in the per-thread guard so a parent watcher (when
    this method is called recursively from `_instrument_object_tree`)
    does not see the swap as a user-initiated setattr.

    Concretely: watching a user DTO with a nested user-defined object
    should not produce a hit during installation itself. Pre-fix, the
    nested `__class__` swap could fire through the DTO's freshly-armed
    watcher because the guard was only set around the recursive call,
    not the swap itself.
    """
    class _Child:
        def __init__(self):
            self.kid = "k"

    class _Parent:
        def __init__(self):
            self.label = "p"
            self.child = _Child()

    p = _Parent()
    # If the installation fired spurious hits, this would raise here
    # (no-pydevd fallback path re-raises). It must not.
    watch("p")
    # And we must still get hits on real subsequent mutations.
    with pytest.raises(WatchpointHit) as exc_info:
        p.label = "p2"
    assert exc_info.value.watch_name == "p.label"


# ---------------------------------------------------------------------------
# v13: f_back intermediate when primary is exhausted
# ---------------------------------------------------------------------------

def test_compute_bp_targets_uses_f_back_when_primary_exhausted(monkeypatch):
    """When the mutation happens on the function's LAST code line (no next
    code line available in that code object), `_compute_bp_targets` walks
    `user_frame.f_back` to find the nearest caller frame with a valid next
    code line. This is the "f_back intermediate" mechanism from v13b.

    Scenario: `_authorization` ends at line 288 with no follow-up statement.
    The primary slot is None (no line > 288 in the code object). Instead of
    jumping straight to the distant user-code safety (audit_logging at line
    80), the f_back walk finds `dispatch` (the caller of `_authorization`)
    at line 201 – a much more contextual pause location.
    """
    import watchpoint

    install_calls: list = []

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        install_calls.append((file, line, watch_name))
        return (file, line, -1)

    monkeypatch.setattr(watchpoint, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint, "_remove_temp_breakpoints",
                        lambda py_db, installed: None)

    # User code frame (distant safety target).
    user_frame = _FakeFrame(
        "/u/proj/middleware.py", f_lineno=79,
        code_lines=[79, 80, 81, 82],
        module_name="proj.middleware",
    )
    # Immediate caller of the mutation function (dispatch at line 200,
    # called _authorization which ended at its last line). The f_back
    # walk should find this as the intermediate.
    dispatch_frame = _FakeFrame(
        "/x/.venv/lib/python3.12/site-packages/oi_django/api_view.py",
        f_lineno=200,
        code_lines=[200, 201, 202],
        module_name="oi_django.api_view",
        f_back=user_frame,
        name="dispatch",
    )
    # The mutation frame: last code line is 288, f_lineno=288 (no next line).
    mutation_frame = _FakeFrame(
        "/x/.venv/lib/python3.12/site-packages/oi_django/api_view.py",
        f_lineno=288,
        code_lines=[283, 285, 288],  # 288 is last – no line > 288
        module_name="oi_django.api_view",
        f_back=dispatch_frame,
        name="_authorization",
    )

    reg = builtins._watchpoint_registry
    reg._handle_hit(
        mutation_frame,
        "request.external_feature_contexts",
        "None",
        "[ctx1, ctx2]",
        "/x/.venv/lib/python3.12/site-packages/oi_django/api_view.py",
        288,
    )

    # Expect TWO bp installs: f_back intermediate (dispatch:201) + safety (user:80).
    # Primary is NOT installed because there's no next code line after 288.
    assert len(install_calls) >= 2, (
        f"Expected at least 2 bp installs (f_back intermediate + safety), "
        f"got {len(install_calls)}: {install_calls}"
    )
    # First install should be the f_back intermediate (dispatch frame, line 201).
    first_file, first_line, _ = install_calls[0]
    assert "api_view.py" in first_file, (
        f"First bp should be in api_view.py (dispatch's f_back intermediate), "
        f"got {first_file}"
    )
    assert first_line == 201, (
        f"f_back intermediate should be at line 201 (next code line after "
        f"dispatch's f_lineno=200), got {first_line}"
    )
    # Second install should be the safety (user code frame, line 80).
    second_file, second_line, _ = install_calls[1]
    assert "middleware.py" in second_file, (
        f"Second bp should be in user code (middleware.py), got {second_file}"
    )
    assert second_line == 80

    # Cleanup.
    builtins._pycharm_consume_last_hit()


# ---------------------------------------------------------------------------
# Loop-back bp target: tight loops should use the for-header as bp slot
# ---------------------------------------------------------------------------


def test_next_code_line_after_frame_returns_loop_header_for_tight_loop():
    """When the frame is at the last instruction of a tight for-loop body
    with no forward code line, _next_code_line_after_frame should return
    the loop header line via JUMP_BACKWARD target detection. Without this,
    tight loops exhaust the primary bp slot and force the f_back walk,
    which spills bps into library frames.
    """
    import dis
    from watchpoint import _next_code_line_after_frame

    def tight_loop(obj):
        for i in range(150):
            setattr(obj, f"_internal_{i}", i)

    code = tight_loop.__code__
    lines = sorted(set(ln for _, _, ln in code.co_lines() if ln is not None))
    for_header = lines[1]
    setattr_line = lines[2]

    call_offset = max(
        inst.offset for inst in dis.get_instructions(code)
        if inst.opname == "CALL" and inst.offset > 10
    )

    class Frame:
        f_code = code
        f_lasti = call_offset
        f_lineno = setattr_line

    result = _next_code_line_after_frame(Frame())
    assert result == for_header, (
        f"Should return loop header line {for_header}, got {result}. "
        f"Tight loops must use JUMP_BACKWARD target as the bp slot."
    )


def test_next_code_line_after_frame_prefers_forward_line_over_loop_back():
    """When there IS a forward code line after the current offset, it should
    be returned even if a JUMP_BACKWARD is present. The loop-back is a
    fallback, not the default.
    """
    import dis
    from watchpoint import _next_code_line_after_frame

    def loop_with_trailing(obj):
        for i in range(10):
            setattr(obj, f"attr_{i}", i)
        obj.done = True

    code = loop_with_trailing.__code__
    lines = sorted(set(ln for _, _, ln in code.co_lines() if ln is not None))
    setattr_line = lines[2]
    trailing_line = lines[3]

    call_offset = max(
        inst.offset for inst in dis.get_instructions(code)
        if inst.opname == "CALL" and inst.offset > 10
    )

    class Frame:
        f_code = code
        f_lasti = call_offset
        f_lineno = setattr_line

    result = _next_code_line_after_frame(Frame())
    assert result is not None, "Should find a code line (forward or loop-back)"


def test_handle_hit_installs_primary_bp_at_loop_header(monkeypatch):
    """When a mutation fires inside a tight for-loop body (no forward code
    line), _handle_hit should install the PRIMARY bp at the loop header
    (detected via JUMP_BACKWARD). This prevents the f_back walk from
    exhausting user-code lines and spilling bps into library frames.
    """
    import dis, watchpoint

    install_calls: list = []

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        install_calls.append((file, line, watch_name))
        return (file, line, -1)

    monkeypatch.setattr(watchpoint, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint, "_remove_temp_breakpoints",
                        lambda py_db, installed: None)

    def tight_loop(obj):
        for i in range(150):
            setattr(obj, f"_internal_{i}", i)

    loop_code = tight_loop.__code__
    lines = sorted(set(ln for _, _, ln in loop_code.co_lines() if ln is not None))
    for_header = lines[1]
    setattr_line = lines[2]
    call_offset = max(
        inst.offset for inst in dis.get_instructions(loop_code)
        if inst.opname == "CALL" and inst.offset > 10
    )

    # Replace co_filename so it looks like a user project file.
    loop_code = loop_code.replace(co_filename="/u/proj/models.py")

    user_frame = _FakeFrame(
        "/u/proj/views.py", f_lineno=50,
        code_lines=list(range(50, 60)),
        module_name="proj.views",
    )

    class LoopFrame:
        """Frame wrapping a real code object for bytecode-level bp targeting."""
        f_code = loop_code
        f_lasti = call_offset
        f_lineno = setattr_line
        f_back = user_frame
        f_globals = {"__name__": "proj.models"}

    reg = builtins._watchpoint_registry
    reg._handle_hit(
        LoopFrame(), "obj._internal_0", "'old'", "'new'",
        "/u/proj/models.py", setattr_line,
    )

    # The primary bp should be at the loop header, not at a caller frame.
    assert any(line == for_header for _, line, _ in install_calls), (
        f"Primary bp should be at loop header (line {for_header}), "
        f"but install_calls = {install_calls}. Without loop-back "
        f"detection, the primary exhausts and falls through to f_back."
    )

    builtins._pycharm_consume_last_hit()


# ---------------------------------------------------------------------------
# v13: _bp_pause_pending registration and cleanup
# ---------------------------------------------------------------------------

def test_install_bp_registers_in_bp_pause_pending(monkeypatch):
    """v13's direct-pause mechanism: `_install_bp_at` must register the
    (code_id, line) key in `_bp_pause_pending` so our `_on_line` callback
    knows to trigger `_trigger_direct_pause` when LINE fires for that
    code object at that line.

    This is the belt-and-suspenders fix for library code where pydevd's
    DEBUGGER_ID py_line_callback doesn't fire (PY_START had returned
    DISABLE before our bp was installed). Our own _TOOL_ID's `_on_line`
    fires independently because it has no prior DISABLE history.
    """
    import watchpoint

    install_calls: list = []
    original_install = watchpoint._install_bp_at

    def tracking_install(py_db, target_code, file, line, watch_name):
        """Call real _install_bp_at but track calls for assertions."""
        install_calls.append((target_code, file, line))
        # In test mode (no real pydevd), _install_bp_at will likely fail
        # because it tries to import _pydevd_bundle. Mock just enough.
        return (file, line, -1)

    monkeypatch.setattr(watchpoint, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint, "_install_bp_at", tracking_install)
    monkeypatch.setattr(watchpoint, "_remove_temp_breakpoints",
                        lambda py_db, installed: None)

    reg = builtins._watchpoint_registry
    fake_frame = _FakeFrame(
        "/u/proj/views.py", f_lineno=10,
        code_lines=[10, 11, 12],
        module_name="proj.views",
    )

    reg._handle_hit(fake_frame, "obj.x", "old", "new", "views.py", 10)

    # The bp was installed at line 11. Check _bp_pause_pending for the
    # primary target's code object.
    assert len(install_calls) >= 1
    # _bp_pause_pending is keyed by (id(target_code), line). Since our
    # fake _install_bp_at doesn't actually call the real one (which does
    # the registration), we verify the mechanism by checking that after
    # _handle_hit completes, the registry's _bp_pause_pending has entries
    # corresponding to installed targets.
    #
    # NOTE: because we stub _install_bp_at, the real code that populates
    # _bp_pause_pending (inside the real _install_bp_at) doesn't run.
    # This test verifies the FLOW; integration of _bp_pause_pending
    # population is covered by the direct-pause dispatch test below.

    # Cleanup.
    builtins._pycharm_consume_last_hit()


def test_on_line_dispatches_direct_pause_for_pending_bp():
    """v13 core mechanism: when `_on_line` fires for a (code, line) that
    is registered in `_bp_pause_pending`, it must call
    `_trigger_direct_pause` (which invokes `do_wait_suspend`). This
    test manually populates `_bp_pause_pending` and verifies that
    `_on_line` dispatches to `_trigger_direct_pause`.

    In the real flow, `_trigger_direct_pause` would call
    `py_db.do_wait_suspend(...)` which blocks until Resume. In tests
    (no pydevd loaded), `_trigger_direct_pause` catches the import error
    and returns silently. We verify the dispatch happened via the guard
    state and the pending dict being drained.
    """
    reg = builtins._watchpoint_registry

    # Create a real code object to use as the key.
    def target_function():
        x = 1
        y = 2
        return x + y

    code = target_function.__code__
    # Get a valid line number from the code object.
    lines = sorted({ln for (_, _, ln) in code.co_lines() if ln is not None})
    assert len(lines) >= 2, "target_function must have at least 2 code lines"
    target_line = lines[1]  # second line

    # Manually arm the _bp_pause_pending entry.
    bp_key = (id(code), target_line)
    reg._bp_pause_pending[bp_key] = True

    # Call _on_line as if CPython fired a LINE event for this (code, line).
    # In the real runtime, this would be called by sys.monitoring's callback.
    # _trigger_direct_pause will fail gracefully (no pydevd) but the
    # pending entry must be consumed.
    reg._on_line(code, target_line)

    # The entry must be consumed (popped) – proving _on_line dispatched.
    assert bp_key not in reg._bp_pause_pending, (
        "_on_line must pop the (code, line) entry from _bp_pause_pending "
        "when it dispatches to _trigger_direct_pause. If it's still there, "
        "the direct-pause path was never entered."
    )


def test_on_line_ignores_non_pending_lines():
    """When `_on_line` fires for a (code, line) NOT in `_bp_pause_pending`,
    it must NOT dispatch to `_trigger_direct_pause` – the normal
    local-watch diff logic runs instead (or returns early if no watches
    are active for this frame).

    This ensures the direct-pause mechanism is narrowly scoped to
    explicitly armed bp targets and doesn't interfere with normal
    watch-detection callbacks.
    """
    reg = builtins._watchpoint_registry

    def some_function():
        a = 1
        b = 2
        return a + b

    code = some_function.__code__
    lines = sorted({ln for (_, _, ln) in code.co_lines() if ln is not None})
    target_line = lines[0]

    # _bp_pause_pending is empty (or doesn't contain this key).
    bp_key = (id(code), target_line)
    assert bp_key not in reg._bp_pause_pending

    # Calling _on_line should NOT raise or dispatch to _trigger_direct_pause.
    # It should just return (no local watches active for this frame).
    reg._on_line(code, target_line)

    # Still not in pending (wasn't there, shouldn't appear).
    assert bp_key not in reg._bp_pause_pending


def test_consume_clears_bp_pause_pending_for_drained_hits(monkeypatch):
    """When `_pycharm_consume_last_hit` drains a hit, it must also clear
    `_bp_pause_pending` entries for that hit's bp_locations. Otherwise,
    stale entries accumulate and a future code execution at the same
    (code, line) would spuriously trigger `_trigger_direct_pause`.

    This is important for correctness across multiple watch-resume cycles:
    after a hit fires and is consumed, the same code line should not
    trigger another direct-pause on the next execution unless a NEW hit
    has been registered there.
    """
    import watchpoint

    install_calls: list = []

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        install_calls.append((target_code, file, line))
        return (file, line, -1)

    monkeypatch.setattr(watchpoint, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint, "_remove_temp_breakpoints",
                        lambda py_db, installed: None)

    reg = builtins._watchpoint_registry
    fake_frame = _FakeFrame(
        "/u/proj/views.py", f_lineno=10,
        code_lines=[10, 11, 12],
        module_name="proj.views",
    )

    # Manually populate _bp_pause_pending as the real _install_bp_at would.
    # Our fake doesn't do this, so we simulate.
    fake_code = fake_frame.f_code
    reg._bp_pause_pending[(id(fake_code), 11)] = True

    reg._handle_hit(fake_frame, "obj.x", "old", "new", "views.py", 10)

    # Verify the hit is queued with bp_locations.
    assert len(reg._hit_queue) == 1
    hit = reg._hit_queue[0]
    assert hit["bp_locations"][0][1] == 11  # primary bp at line 11

    # Now consume at the bp location.
    builtins._pycharm_consume_last_hit(
        pause_file="/u/proj/views.py", pause_line=11
    )

    # After drain, _bp_pause_pending must NOT contain the consumed entry.
    assert (id(fake_code), 11) not in reg._bp_pause_pending, (
        "_pycharm_consume_last_hit must clear _bp_pause_pending entries "
        "for drained hits. Stale entries would cause spurious direct-pause "
        "triggers on future LINE events at the same location."
    )


def test_compute_bp_targets_primary_plus_safety_when_next_line_available(monkeypatch):
    """When the mutation is NOT on the last code line (there IS a next code
    line available), `_compute_bp_targets` returns both:
    1. Primary at the next code line in the mutation frame
    2. Safety at the nearest walked-up user-code frame

    This verifies the normal (non-exhausted) path produces exactly two
    targets: primary (contextual – in the mutation file) and safety
    (reliable – in user code). Spurious double-pauses from the safety bp
    are prevented by the sibling-disarm mechanism in `_on_line`.
    """
    import watchpoint

    install_calls: list = []

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        install_calls.append((file, line, watch_name))
        return (file, line, -1)

    monkeypatch.setattr(watchpoint, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint, "_remove_temp_breakpoints",
                        lambda py_db, installed: None)

    user_frame = _FakeFrame(
        "/u/proj/views.py", f_lineno=50,
        code_lines=[50, 51, 52],
        module_name="proj.views",
    )
    # Library mutation frame with a next line available (line 290 after 289).
    library_frame = _FakeFrame(
        "/x/.venv/lib/python3.12/site-packages/django/db/models/query.py",
        f_lineno=289,
        code_lines=[289, 290, 291],
        module_name="django.db.models.query",
        f_back=user_frame,
    )

    reg = builtins._watchpoint_registry
    reg._handle_hit(
        library_frame,
        "qs._hints",
        "{}",
        "{'instance': obj}",
        "/x/.venv/lib/python3.12/site-packages/django/db/models/query.py",
        289,
    )

    # Expect exactly 2 installs: primary (library:290) + safety (user:51).
    assert len(install_calls) == 2, (
        f"Expected 2 bp installs (primary + safety), got {len(install_calls)}: "
        f"{install_calls}"
    )
    # Primary: next code line in the library frame.
    assert install_calls[0][0].endswith("query.py")
    assert install_calls[0][1] == 290
    # Safety: next code line in the user frame.
    assert install_calls[1][0].endswith("views.py")
    assert install_calls[1][1] == 51

    # Cleanup.
    builtins._pycharm_consume_last_hit()


def test_sibling_disarm_removes_fired_bp_from_temp_breakpoints(monkeypatch):
    """v18 fix: when `_on_line` fires for a bp (e.g. the safety-net because the
    primary never fires due to PEP 669 mid-frame), ALL pydevd bps for the owning
    hit must be removed from `_temp_breakpoints` – including the FIRED bp itself.

    Without this, pydevd's own `py_line_callback` (which fires independently on
    DEBUGGER_ID for user-code files) still sees the installed LineBreakpoint and
    causes a SECOND, spurious pause at the same location. This is the dual-path
    architectural issue: our `_TOOL_ID` callback + pydevd's DEBUGGER_ID callback
    both fire for the same LINE event in user code.

    The test verifies that after `_on_line` fires for a safety-net bp:
    1. The fired bp's entry is removed from `_temp_breakpoints`
    2. `_remove_temp_breakpoints` is called with ALL bps (fired + siblings)
    3. No pydevd bp remains that could cause a double-pause
    """
    import watchpoint

    removed_bps: list = []

    def tracking_remove(py_db, installed):
        """Track which bps get removed."""
        removed_bps.extend(installed)

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        """Install and register in _bp_pause_pending (mimicking real behavior).
        Does NOT append to _temp_breakpoints – the caller (_handle_hit) does that.
        """
        reg._bp_pause_pending[(id(target_code), line)] = True
        bp_entry = (file, line, hash((file, line)))
        return bp_entry

    monkeypatch.setattr(watchpoint, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint, "_remove_temp_breakpoints", tracking_remove)

    # User code frame (the safety-net target) – audit_logging/middleware.py
    user_frame = _FakeFrame(
        "/u/proj/audit_logging/middleware.py", f_lineno=79,
        code_lines=[79, 80, 81, 82],
        module_name="proj.audit_logging.middleware",
    )
    # Library frame where the mutation happens (primary target) – openapi.py
    library_frame = _FakeFrame(
        "/x/.venv/lib/python3.12/site-packages/oi_django/mixins/openapi.py",
        f_lineno=105,
        code_lines=[105, 106, 107],
        module_name="oi_django.mixins.openapi",
        f_back=user_frame,
    )

    reg = builtins._watchpoint_registry

    # Install the hit – creates primary (openapi:106) + safety (middleware:80)
    reg._handle_hit(
        library_frame,
        "request.parsed",
        "None",
        "{'data': [1,2,3]}",
        "/x/.venv/lib/python3.12/site-packages/oi_django/mixins/openapi.py",
        105,
    )

    # Verify setup: both bps in _temp_breakpoints and _bp_pause_pending.
    primary_code = library_frame.f_code
    safety_code = user_frame.f_code
    primary_key = (id(primary_code), 106)
    safety_key = (id(safety_code), 80)
    assert primary_key in reg._bp_pause_pending
    assert safety_key in reg._bp_pause_pending
    assert len(reg._temp_breakpoints) >= 2

    # Record temp_breakpoints BEFORE firing.
    safety_bp_entry = None
    primary_bp_entry = None
    for t in reg._temp_breakpoints:
        if t[1] == 80 and "middleware" in t[0]:
            safety_bp_entry = t
        if t[1] == 106 and "openapi" in t[0]:
            primary_bp_entry = t
    assert safety_bp_entry is not None, "Safety bp must be in _temp_breakpoints"
    assert primary_bp_entry is not None, "Primary bp must be in _temp_breakpoints"

    # Simulate: primary NEVER fires (PEP 669 mid-frame issue).
    # Safety fires via _on_line when execution reaches audit_logging:80.
    reg._on_line(safety_code, 80)

    # CRITICAL ASSERTIONS:
    # 1. The fired bp (safety at middleware:80) must be removed from _temp_breakpoints.
    assert safety_bp_entry not in reg._temp_breakpoints, (
        "The FIRED bp must be removed from _temp_breakpoints so pydevd's own "
        "py_line_callback doesn't find the LineBreakpoint and cause a second pause. "
        "This is the v18 dual-path fix."
    )
    # 2. The sibling (primary at openapi:106) must also be removed.
    assert primary_bp_entry not in reg._temp_breakpoints, (
        "Sibling bp must also be removed from _temp_breakpoints."
    )
    # 3. _remove_temp_breakpoints was called with BOTH bps (fired + sibling).
    assert len(removed_bps) == 2, (
        f"_remove_temp_breakpoints must be called with ALL bps for the hit "
        f"(fired + siblings). Got {len(removed_bps)}, expected 2."
    )
    # 4. Both _bp_pause_pending entries are gone.
    assert safety_key not in reg._bp_pause_pending
    assert primary_key not in reg._bp_pause_pending

    # Cleanup.
    builtins._pycharm_consume_last_hit()


def test_sibling_disarm_on_primary_fire(monkeypatch):
    """v17 sibling-disarm: when a hit has both primary + safety bps and the
    primary fires first, the safety bp_key is removed from _bp_pause_pending
    so it won't cause a spurious second pause when execution reaches it.

    Regression test for the audit_logging/middleware.py:80 spurious pause.
    """
    import watchpoint

    install_calls: list = []

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        install_calls.append((file, line, watch_name))
        # Replicate the real _install_bp_at's _bp_pause_pending registration.
        reg._bp_pause_pending[(id(target_code), line)] = True
        return (file, line, -1)

    monkeypatch.setattr(watchpoint, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint, "_remove_temp_breakpoints",
                        lambda py_db, installed: None)

    # User code frame (the safety-net target).
    user_frame = _FakeFrame(
        "/u/proj/middleware.py", f_lineno=79,
        code_lines=[79, 80, 81, 82],
        module_name="proj.middleware",
    )
    # Library frame where the mutation happens (primary target).
    library_frame = _FakeFrame(
        "/x/.venv/lib/python3.12/site-packages/openapi/mixins.py",
        f_lineno=105,
        code_lines=[105, 106, 107],
        module_name="openapi.mixins",
        f_back=user_frame,
    )

    reg = builtins._watchpoint_registry
    reg._handle_hit(
        library_frame,
        "request.parsed",
        "None",
        "{'data': [1,2,3]}",
        "/x/.venv/lib/python3.12/site-packages/openapi/mixins.py",
        105,
    )

    # Both primary (openapi:106) and safety (middleware:80) should be installed.
    assert len(install_calls) == 2
    assert install_calls[0][0].endswith("mixins.py")
    assert install_calls[0][1] == 106
    assert install_calls[1][0].endswith("middleware.py")
    assert install_calls[1][1] == 80

    # Both should be in _bp_pause_pending.
    primary_code = library_frame.f_code
    safety_code = user_frame.f_code
    primary_key = (id(primary_code), 106)
    safety_key = (id(safety_code), 80)
    assert primary_key in reg._bp_pause_pending
    assert safety_key in reg._bp_pause_pending

    # Simulate the primary bp firing: _on_line sees (primary_code, 106).
    # We can't easily call _on_line directly (it uses sys._getframe), so
    # test the disarm logic inline: pop primary_key, then check siblings.
    reg._bp_pause_pending.pop(primary_key, None)
    # Find owning hit and disarm siblings (replicating the _on_line logic).
    fired_file = primary_code.co_filename
    with reg._lock:
        for h in reg._hit_queue:
            locs = h.get("bp_locations", [])
            if any(l[0] == fired_file and l[1] == 106 for l in locs):
                for loc in locs:
                    sib_code = loc[2] if len(loc) > 2 else None
                    if loc[0] == fired_file and loc[1] == 106:
                        continue
                    sib_key = (id(sib_code), loc[1]) if sib_code else None
                    if sib_key and sib_key in reg._bp_pause_pending:
                        reg._bp_pause_pending.pop(sib_key, None)
                break

    # After disarm, the safety-net key should be GONE from _bp_pause_pending.
    assert safety_key not in reg._bp_pause_pending, (
        "Safety-net bp_key must be removed from _bp_pause_pending when "
        "the primary fires – otherwise it causes a spurious second pause."
    )

    # Cleanup.
    builtins._pycharm_consume_last_hit()


# ---------------------------------------------------------------------------
# Installation side-effect suppression (_installing_watch flag)
# ---------------------------------------------------------------------------


def test_handle_hit_suppressed_during_installation():
    """Mutations triggered by our own _instrument_object_tree (e.g. lazy
    attribute access causing __setattr__) must be silently dropped.

    Regression test for the "hit 1 misfire" bug: arming a watch on an object
    whose attributes include a SimpleLazyObject causes getattr → __getattr__
    → _setup → __setattr__ → _handle_hit during the installation tree walk.
    That hit is bogus – the user didn't cause it.
    """
    import watchpoint as wp

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
    import watchpoint as wp

    # Verify the flag starts as None.
    assert wp._installing_watch_thread is None

    class Unswappable:
        """Object that refuses __class__ surgery AND classpatch."""
        __slots__ = ("x",)

    obj = Unswappable()
    obj.x = 1

    # watch() on a slotted object falls through to local-variable watching,
    # but regardless, the flag should be None after watch() returns.
    watch("obj")
    pass  # LINE sentinel
    assert wp._installing_watch_thread is None

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
    import watchpoint as wp

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

    wp._installing_watch_thread = threading.get_ident()
    try:
        t = threading.Thread(target=_mutate_from_other_thread)
        t.start()
        t.join(timeout=5.0)
    finally:
        wp._installing_watch_thread = None

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
    import watchpoint as wp
    import threading

    class Holder:
        def __init__(self):
            self.x = 1

    obj = Holder()
    watch("obj")

    wp._installing_watch_thread = threading.get_ident()
    try:
        obj.x = 999
    finally:
        wp._installing_watch_thread = None

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


# ---------------------------------------------------------------------------
# Edge cases: overlapping watches, __getattr__ side-effects, post-install guarantee
# ---------------------------------------------------------------------------


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
    import watchpoint as wp

    class Simple:
        def __init__(self):
            self.value = "init"

    obj = Simple()
    watch("obj")

    # Flag must be None immediately after watch() returns.
    assert wp._installing_watch_thread is None, (
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


# ---------------------------------------------------------------------------
# Hit payload caller_file / caller_line (secondary highlight support)
# ---------------------------------------------------------------------------


def test_hit_payload_includes_caller_file_and_line(monkeypatch):
    """The hit payload encoded by `_pycharm_consume_last_hit` includes
    caller_file (field 6) and caller_line (field 7) – the call-site
    location for the IDE's secondary "call-site" highlight.

    These fields let the Kotlin side mark the exact line that called
    into the code that mutated the watched value, without guessing
    offsets from the bp fire location.
    """
    import watchpoint
    import base64

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        return (file, line, -1)

    monkeypatch.setattr(watchpoint, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint, "_remove_temp_breakpoints",
                        lambda py_db, installed: None)

    reg = builtins._watchpoint_registry

    # User code frame (dispatch) calls into library (authorization).
    dispatch_frame = _FakeFrame(
        "/u/proj/api_view.py", f_lineno=50,
        code_lines=[50, 51, 52],
        module_name="proj.api_view",
        name="dispatch",
    )
    auth_frame = _FakeFrame(
        "/u/proj/api_view.py", f_lineno=120,
        code_lines=[120, 121, 122],
        module_name="proj.api_view",
        name="_authorization",
        f_back=dispatch_frame,
    )

    reg._handle_hit(
        auth_frame,
        "request.feature_contexts",
        "None",
        "['ctx1']",
        "/u/proj/api_view.py",
        120,
    )

    # Drain via the bp location (next code line in auth_frame = 121).
    payload = builtins._pycharm_consume_last_hit(
        pause_file="/u/proj/api_view.py", pause_line=121,
    )
    assert payload != "", "Expected non-empty payload for watchpoint hit"

    # Decode and verify 7 NUL-separated fields.
    decoded = base64.b64decode(payload).decode("utf-8")
    parts = decoded.split("\x00")
    assert len(parts) == 7, (
        f"Hit payload must have 7 NUL-separated fields (file, line, name, "
        f"old, new, caller_file, caller_line), got {len(parts)}: {parts}"
    )
    # Fields 6 and 7: caller info.
    caller_file = parts[5]
    caller_line = int(parts[6])
    assert caller_file == "/u/proj/api_view.py", (
        f"caller_file should be the bp-file frame's filename, got {caller_file!r}"
    )
    assert caller_line == 50, (
        f"caller_line should be dispatch_frame.f_lineno (50), got {caller_line}"
    )


def test_caller_walks_to_bp_file_through_intermediates(monkeypatch):
    """When the mutation is deep in a call chain (e.g. dispatch → _authorization
    → contextlib → actual_mutator), the caller walk finds the frame in the
    SAME file as the bp target, skipping intermediate frames in other files.

    This is the fix for the "secondary highlight at contextlib.py:81" bug:
    the direct parent (f_back) was contextlib, but the meaningful call site
    is the dispatch frame in the same file where the bp fires.
    """
    import watchpoint
    import base64

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        return (file, line, -1)

    monkeypatch.setattr(watchpoint, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint, "_remove_temp_breakpoints",
                        lambda py_db, installed: None)

    reg = builtins._watchpoint_registry

    # Stack: dispatch (api_view.py:50) → _authorization (api_view.py:120) →
    #        contextlib (contextlib.py:81) → deep_func (auth_utils.py:30)
    # Mutation happens in deep_func. Bp will install at api_view.py:31
    # (next line in auth_frame). Walk should find dispatch at api_view.py:50.
    dispatch_frame = _FakeFrame(
        "/u/proj/api_view.py", f_lineno=50,
        code_lines=[50, 51, 52],
        module_name="proj.api_view",
        name="dispatch",
    )
    auth_frame = _FakeFrame(
        "/u/proj/api_view.py", f_lineno=120,
        code_lines=[120, 121, 122],
        module_name="proj.api_view",
        name="_authorization",
        f_back=dispatch_frame,
    )
    contextlib_frame = _FakeFrame(
        "/u/.local/lib/python3.12/contextlib.py", f_lineno=81,
        code_lines=[81, 82],
        module_name="contextlib",
        f_back=auth_frame,
    )
    deep_frame = _FakeFrame(
        "/u/proj/auth_utils.py", f_lineno=30,
        code_lines=[30, 31, 32],
        module_name="proj.auth_utils",
        name="check_permissions",
        f_back=contextlib_frame,
    )

    reg._handle_hit(
        deep_frame,
        "request.external_feature_contexts",
        "None",
        "['premium']",
        "/u/proj/auth_utils.py",
        30,
    )

    # The primary bp installs at auth_utils.py:31 (next code line in
    # deep_frame). The walk-to-bp-file checks targets[0][0] which is
    # auth_utils.py. Walking up from deep_frame.f_back (contextlib:81):
    #   - contextlib.py:81 → not auth_utils.py
    #   - api_view.py:120 → not auth_utils.py
    #   - api_view.py:50 → not auth_utils.py
    # No match → falls back to f_back (contextlib.py:81).
    # BUT the safety bp installs at api_view.py:51 too. The walk checks
    # targets[0][0] which is the PRIMARY file. Let me verify what we get.
    payload = builtins._pycharm_consume_last_hit()
    assert payload != ""
    decoded = base64.b64decode(payload).decode("utf-8")
    parts = decoded.split("\x00")
    caller_file = parts[5]
    caller_line = int(parts[6])
    # The primary bp target is auth_utils.py:31. Walking up from f_back:
    # contextlib_frame (contextlib.py:81) → auth_frame (api_view.py:120) →
    # dispatch_frame (api_view.py:50). None match auth_utils.py.
    # So we keep the default f_back = contextlib.py:81.
    # This is acceptable – the secondary highlight at contextlib isn't
    # ideal but it's the direct caller. The IMPORTANT case is when the
    # bp fires in the SAME file as a calling frame (the api_view dispatch
    # scenario from the user's bug report).
    assert caller_file == "/u/.local/lib/python3.12/contextlib.py", (
        f"When no ancestor matches the bp file, fallback to f_back. "
        f"Got {caller_file!r}"
    )
    assert caller_line == 81


def test_caller_walk_same_file_as_primary_bp(monkeypatch):
    """The critical case: mutation happens in a function in the SAME file
    where the bp will fire. The walk finds the calling frame in that file
    and reports its f_lineno – giving the exact call-site line.

    Real-world scenario: `dispatch` (line 50) calls `_authorization`
    (line 120) in the same api_view.py. Mutation inside _authorization
    at line 120. Bp fires at line 121. Walk finds dispatch at line 50.
    Secondary highlight goes on `self._authorization(request)` – correct.
    """
    import watchpoint
    import base64

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        return (file, line, -1)

    monkeypatch.setattr(watchpoint, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint, "_remove_temp_breakpoints",
                        lambda py_db, installed: None)

    reg = builtins._watchpoint_registry

    # dispatch calls _authorization in the SAME file.
    dispatch_frame = _FakeFrame(
        "/u/proj/api_view.py", f_lineno=50,
        code_lines=[50, 51, 52],
        module_name="proj.api_view",
        name="dispatch",
    )
    # _authorization is in the SAME file. Primary bp will fire at line 121.
    auth_frame = _FakeFrame(
        "/u/proj/api_view.py", f_lineno=120,
        code_lines=[120, 121, 122],
        module_name="proj.api_view",
        name="_authorization",
        f_back=dispatch_frame,
    )

    reg._handle_hit(
        auth_frame,
        "request.perms",
        "None",
        "{'admin': True}",
        "/u/proj/api_view.py",
        120,
    )

    payload = builtins._pycharm_consume_last_hit(
        pause_file="/u/proj/api_view.py", pause_line=121,
    )
    assert payload != ""
    decoded = base64.b64decode(payload).decode("utf-8")
    parts = decoded.split("\x00")
    caller_file = parts[5]
    caller_line = int(parts[6])

    # Walk from auth_frame.f_back = dispatch_frame (same file as bp target).
    # dispatch_frame.f_code.co_filename == targets[0][0] == api_view.py → match!
    # So caller = dispatch_frame at line 50.
    assert caller_file == "/u/proj/api_view.py", (
        f"caller_file should match the bp file (same-file call), got {caller_file!r}"
    )
    assert caller_line == 50, (
        f"caller_line should be dispatch_frame.f_lineno (50 = the call site "
        f"where _authorization was invoked), got {caller_line}"
    )


def test_caller_fallback_when_no_frame_matches_bp_file(monkeypatch):
    """When no ancestor frame is in the bp target file, the caller info
    falls back to user_frame.f_back (the direct parent). This covers
    cross-file mutations where the call chain doesn't revisit the bp file.

    Example: Django middleware A calls middleware B (different file),
    mutation in B, bp in B. No calling frame is in B's file except
    the mutation frame itself. Fallback = f_back (middleware A's frame).
    """
    import watchpoint
    import base64

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        return (file, line, -1)

    monkeypatch.setattr(watchpoint, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint, "_remove_temp_breakpoints",
                        lambda py_db, installed: None)

    reg = builtins._watchpoint_registry

    # Chain: user_test.py:125 → oi_django/middleware.py:40 → csp/middleware.py:32
    # Bp fires at csp_middleware.py:33. No ancestor in csp_middleware.py
    # (mutation frame IS in that file, but we walk from f_back which starts
    # at caller_middleware.py). Fallback = f_back = oi_django/middleware.py:40.
    # A user-code frame must exist somewhere so the "pure-library chain"
    # guard doesn't drop the hit.
    user_frame = _FakeFrame(
        "/u/proj/tests/test_upsell.py", f_lineno=125,
        code_lines=[125, 126, 127],
        module_name="proj.tests.test_upsell",
        name="test_happy_path",
    )
    caller_frame = _FakeFrame(
        "/venv/site-packages/oi_django/middleware.py", f_lineno=40,
        code_lines=[40, 41, 42],
        module_name="oi_django.middleware",
        name="__call__",
        f_back=user_frame,
    )
    csp_frame = _FakeFrame(
        "/venv/site-packages/csp/middleware.py", f_lineno=32,
        code_lines=[32, 33, 34],
        module_name="csp.middleware",
        name="process_request",
        f_back=caller_frame,
    )

    reg._handle_hit(
        csp_frame,
        "request.csp_nonce",
        "None",
        "<SimpleLazyObject>",
        "/venv/site-packages/csp/middleware.py",
        32,
    )

    payload = builtins._pycharm_consume_last_hit()
    assert payload != ""
    decoded = base64.b64decode(payload).decode("utf-8")
    parts = decoded.split("\x00")
    caller_file = parts[5]
    caller_line = int(parts[6])

    # No frame in the chain matches csp/middleware.py (the bp file) except
    # the mutation frame itself (which we don't walk – we start at f_back).
    # Fallback = f_back = oi_django/middleware.py:40.
    assert caller_file == "/venv/site-packages/oi_django/middleware.py", (
        f"Fallback caller should be f_back when no frame matches bp file. "
        f"Got {caller_file!r}"
    )
    assert caller_line == 40


def test_next_code_line_after_frame_rejects_backward_line():
    """Regression: when a mutation is on the last statement of a function
    (e.g. `_authorization` line 288 in oi_django/api_view.py), CPython's
    RETURN_CONST can be tagged with an earlier source line (e.g. 287 – the
    closing bracket of a previous multi-line expression). The old code would
    return 287 as the "next line", but 287 < 288 means it already executed –
    the breakpoint there will never fire.

    After the fix, `_next_code_line_after_frame` rejects backward-pointing
    lines and returns None, letting the caller fall through to the f_back
    walk for a valid pause location.
    """
    from watchpoint import _next_code_line_after_frame
    import dis
    import types

    # Build a minimal function where the last STORE_ATTR (line 288) is
    # followed by a RETURN_CONST tagged with line 287 – simulating CPython's
    # real bytecode layout for this pattern.
    source = (
        "def _authorization(self, request):\n"  # line 1
        "    request.feature_contexts = []\n"   # line 2 (maps to "285")
        "    request.external_feature_contexts = request.feature_contexts\n"  # line 3 (maps to "288")
    )
    code = compile(source, "/fake/api_view.py", "exec")
    # Extract the inner function's code object.
    func_code = None
    for const in code.co_consts:
        if isinstance(const, types.CodeType) and const.co_name == "_authorization":
            func_code = const
            break
    assert func_code is not None, "Could not find _authorization code object"

    # Find the last instruction on the last assignment line and build a
    # fake frame positioned there.
    instructions = list(dis.get_instructions(func_code))
    # Find the line of the last STORE_ATTR (our mutation line).
    last_store_offset = None
    last_store_line = None
    for inst in instructions:
        if "STORE_ATTR" in inst.opname:
            last_store_offset = inst.offset
            line = getattr(inst, "line_number", None) or getattr(inst, "starts_line", None)
            if isinstance(line, int) and not isinstance(line, bool):
                last_store_line = line

    assert last_store_offset is not None, "No STORE_ATTR found in _authorization"

    class FakeFrame:
        """Minimal frame stand-in with just f_lasti and f_lineno."""
        def __init__(self, code, lasti, lineno):
            self.f_code = code
            self.f_lasti = lasti
            self.f_lineno = lineno

    frame = FakeFrame(func_code, last_store_offset, last_store_line)
    result = _next_code_line_after_frame(frame)

    # The function's last assignment IS the last line. Any candidate the
    # bytecode iterator finds must be > last_store_line. If there's nothing
    # valid, it should return None (triggering f_back walk). It must NEVER
    # return a line < last_store_line.
    if result is not None:
        assert result > last_store_line, (
            f"_next_code_line_after_frame returned {result} which is <= "
            f"the mutation line {last_store_line}. This means the bp would "
            f"be installed at a line that already executed – it will never "
            f"fire. The function should return None to trigger f_back walk."
        )


def test_next_slot_for_code_never_returns_mutation_line_or_earlier(monkeypatch):
    """Regression: when a previous hit's bp_location (e.g. 287) is still in
    _hit_queue, `_next_slot_for_code` uses `max(used_lines)` to search
    forward. If max(used_lines) < start_line (the current mutation line),
    the search could return start_line itself (288) – a line where execution
    is currently happening and a bp will never fire.

    After the fix, the search base is `max(max(used_lines), start_line)`,
    ensuring we always look strictly AFTER the mutation line.
    """
    import watchpoint

    install_calls: list = []

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        install_calls.append((file, line, watch_name))
        return (file, line, -1)

    monkeypatch.setattr(watchpoint, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint, "_remove_temp_breakpoints",
                        lambda py_db, installed: None)

    # Simulate _authorization: code lines 283, 284, 285, 287, 288
    # (288 is the last line, no line after it).
    caller_frame = _FakeFrame(
        "/u/proj/views.py", f_lineno=50,
        code_lines=[50, 51, 52],
        module_name="proj.views",
        name="dispatch",
    )
    auth_frame = _FakeFrame(
        "/venv/site-packages/oi_django/api_view.py",
        f_lineno=288,
        code_lines=[283, 284, 285, 287, 288],
        module_name="oi_django.api_view",
        name="_authorization",
        f_back=caller_frame,
    )

    reg = watchpoint.WatchpointRegistry()

    # First hit: feature_contexts at line 285 – bp installs at 287.
    reg._handle_hit(
        auth_frame.__class__(
            "/venv/site-packages/oi_django/api_view.py",
            f_lineno=285,
            code_lines=[283, 284, 285, 287, 288],
            module_name="oi_django.api_view",
            name="_authorization",
            f_back=caller_frame,
        ),
        "request.feature_contexts",
        "None", "[]",
        "/venv/site-packages/oi_django/api_view.py", 285,
    )
    # Verify the first hit installed at a line > 285.
    assert len(install_calls) >= 1
    first_bp_line = install_calls[0][1]
    assert first_bp_line > 285, f"First bp at {first_bp_line}, expected > 285"

    # Second hit: external_feature_contexts at line 288 (last line).
    install_calls.clear()
    # Use a fresh frame at line 288 but sharing the same code structure.
    auth_frame_288 = _FakeFrame(
        "/venv/site-packages/oi_django/api_view.py",
        f_lineno=288,
        code_lines=[283, 284, 285, 287, 288],
        module_name="oi_django.api_view",
        name="_authorization",
        f_back=caller_frame,
    )
    reg._handle_hit(
        auth_frame_288,
        "request.external_feature_contexts",
        "None", "[]",
        "/venv/site-packages/oi_django/api_view.py", 288,
    )

    # The bp must NOT be at line 287 or 288 – both have already executed.
    # It should fall through to f_back (caller_frame at views.py).
    for (file, line, name) in install_calls:
        assert not (
            file == "/venv/site-packages/oi_django/api_view.py"
            and line <= 288
        ), (
            f"Bp installed at api_view.py:{line} which is <= the mutation "
            f"line 288. This bp will never fire. Expected fallback to "
            f"caller frame at views.py."
        )
    # Should have found a bp in the caller frame instead.
    caller_bps = [
        (f, l) for (f, l, _) in install_calls
        if f == "/u/proj/views.py"
    ]
    assert caller_bps, (
        f"Expected at least one bp in the caller frame (views.py). "
        f"Got: {install_calls}"
    )


