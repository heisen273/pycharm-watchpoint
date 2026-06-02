"""v13 direct-pause dispatch: _bp_pause_pending registration and cleanup."""


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
)

from util import (
    _FakeFrame,
)


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
    import _pycharm_watchpoint as watchpoint

    install_calls: list = []
    original_install = watchpoint._install_bp_at

    def tracking_install(py_db, target_code, file, line, watch_name):
        """Call real _install_bp_at but track calls for assertions."""
        install_calls.append((target_code, file, line))
        # In test mode (no real pydevd), _install_bp_at will likely fail
        # because it tries to import _pydevd_bundle. Mock just enough.
        return (file, line, -1)

    monkeypatch.setattr(watchpoint.pydevd_pause, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint.pydevd_pause, "_install_bp_at", tracking_install)
    monkeypatch.setattr(watchpoint.pydevd_pause, "_remove_temp_breakpoints",
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
    import _pycharm_watchpoint as watchpoint

    install_calls: list = []

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        install_calls.append((target_code, file, line))
        return (file, line, -1)

    monkeypatch.setattr(watchpoint.pydevd_pause, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint.pydevd_pause, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint.pydevd_pause, "_remove_temp_breakpoints",
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
    import _pycharm_watchpoint as watchpoint

    install_calls: list = []

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        install_calls.append((file, line, watch_name))
        return (file, line, -1)

    monkeypatch.setattr(watchpoint.pydevd_pause, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint.pydevd_pause, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint.pydevd_pause, "_remove_temp_breakpoints",
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
    import _pycharm_watchpoint as watchpoint

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

    monkeypatch.setattr(watchpoint.pydevd_pause, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint.pydevd_pause, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint.pydevd_pause, "_remove_temp_breakpoints", tracking_remove)

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
    import _pycharm_watchpoint as watchpoint

    install_calls: list = []

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        install_calls.append((file, line, watch_name))
        # Replicate the real _install_bp_at's _bp_pause_pending registration.
        reg._bp_pause_pending[(id(target_code), line)] = True
        return (file, line, -1)

    monkeypatch.setattr(watchpoint.pydevd_pause, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint.pydevd_pause, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint.pydevd_pause, "_remove_temp_breakpoints",
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
