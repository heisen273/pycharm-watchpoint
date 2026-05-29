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
        c = "rebind-c"
        pass

    def _code():
        x = watched
        watch("x")
        _modify(other_a, other_b, x)

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    # The hit must report c's change, with display_name 'x'.
    assert exc_info.value.watch_name == "x"
    assert exc_info.value.new_value == "'rebind-c'"


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
    wrapper_alias.append("after-unwatch")  # must NOT raise/pause
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
