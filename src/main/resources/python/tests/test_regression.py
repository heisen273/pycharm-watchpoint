"""Frame-id reuse / exception-unwind cleanup and the Phase-1 bring-up
regression suite."""


import gc
import sys
import inspect
import weakref
import builtins
import pytest

from _pycharm_watchpoint import watch, unwatch, clear_watches, WatchpointHit
from _pycharm_watchpoint import (
    _get_pydevd_debugger,
    _pause_via_pydevd,
    _value_hash,
    builtins,
    constants,
    registry,
    sys,
    threading,
)

from util import (
    _FakeFrame,
)


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
    from _pycharm_watchpoint import _value_hash
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
    from _pycharm_watchpoint import _get_pydevd_debugger
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
    from _pycharm_watchpoint import _pause_via_pydevd

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
    from _pycharm_watchpoint import _pause_via_pydevd

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
    import _pycharm_watchpoint as watchpoint

    install_calls: list = []

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        install_calls.append((file, line, watch_name))
        # Real `_install_bp_at` returns (file, line, bp_id) on success,
        # or None on failure. Both are exercised by other tests; here we
        # always succeed.
        return (file, line, -hash((watch_name, file, line)) & 0x7FFFFFFF)

    fake_py_db = object()  # any non-None sentinel so the pydevd branch is taken
    monkeypatch.setattr(watchpoint.pydevd_pause, "_get_pydevd_debugger", lambda: fake_py_db)
    monkeypatch.setattr(watchpoint.pydevd_pause, "_install_bp_at", fake_install_bp)
    # Make removal a no-op for the stubbed bp_ids – the real fn would try
    # to delete from py_db.file_to_id_to_line_breakpoint which our fake
    # py_db doesn't have.
    monkeypatch.setattr(watchpoint.pydevd_pause, "_remove_temp_breakpoints",
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


def test_bp_installed_at_current_line_not_next_when_source_differs(monkeypatch):
    """When source_line < user_frame.f_lineno, the bp must be placed AT f_lineno.

    Regression: local-variable watches detect mutations via LINE callbacks.
    The LINE callback fires BEFORE the next line executes, so by the time
    _handle_hit runs, f_lineno has advanced to the line ABOUT TO execute
    (not yet executed). The bp should pause there – not one line later.

    Concrete example from user report:
    - Line 61: `a.append(1)` – mutation detected by comparing f_locals
    - Line 62: next line (f_lineno when _handle_hit runs) – correct pause target
    - Line 63: one line too far – the old buggy behavior

    The root cause was `_next_slot_for_code(code, f_lineno)` searching for
    lines STRICTLY > f_lineno, skipping f_lineno itself which hadn't executed
    yet and was the correct pause point.
    """
    import _pycharm_watchpoint as watchpoint

    install_calls: list = []

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        """Record bp install calls and return success tuple."""
        install_calls.append((file, line, watch_name))
        return (file, line, -hash((watch_name, file, line)) & 0x7FFFFFFF)

    fake_py_db = object()
    monkeypatch.setattr(watchpoint.pydevd_pause, "_get_pydevd_debugger", lambda: fake_py_db)
    monkeypatch.setattr(watchpoint.pydevd_pause, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint.pydevd_pause, "_remove_temp_breakpoints",
                        lambda py_db, installed: None)

    reg = builtins._watchpoint_registry

    # Simulate: mutation on line 61, LINE callback fires for line 62.
    # code_lines includes 60-67 so there are valid lines in both directions.
    fake_frame = _FakeFrame(
        "/Users/me/project/demo.py", f_lineno=62,
        code_lines=[60, 61, 62, 63, 64, 65, 66, 67],
        module_name="demo",
    )

    # source_line=61, user_frame.f_lineno=62 – the common local-watch case.
    reg._handle_hit(fake_frame, "a", "'[1]'", "'[1, 2]'", "/Users/me/project/demo.py", 61)

    assert len(install_calls) >= 1, "At least one bp must be installed."
    primary_bp_line = install_calls[0][1]
    assert primary_bp_line == 62, (
        f"BP must be installed at f_lineno (62) – the line about to execute – "
        f"not at {primary_bp_line}. The mutation was on line 61; line 62 hasn't "
        f"run yet and is the correct pause target."
    )

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
    import _pycharm_watchpoint as watchpoint

    fake_py_db = object()
    install_calls = []
    install_lock = threading.Lock()

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        with install_lock:
            install_calls.append((file, line, watch_name))
        return (file, line, -hash((watch_name, file, line)) & 0x7FFFFFFF)

    monkeypatch.setattr(watchpoint.pydevd_pause, "_get_pydevd_debugger", lambda: fake_py_db)
    monkeypatch.setattr(watchpoint.pydevd_pause, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint.pydevd_pause, "_remove_temp_breakpoints",
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
    import _pycharm_watchpoint as watchpoint

    monkeypatch.setattr(watchpoint.pydevd_pause, "_get_pydevd_debugger", lambda: object())
    # Stub returns a successful install so the queue + bp list get
    # populated – we're testing that `clear_watches` empties them.
    monkeypatch.setattr(watchpoint.pydevd_pause, "_install_bp_at",
                        lambda *a, **kw: ("f.py", 2, -1))
    monkeypatch.setattr(watchpoint.pydevd_pause, "_remove_temp_breakpoints",
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
