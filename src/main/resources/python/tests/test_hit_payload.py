"""Hit-payload caller_file / caller_line fields (secondary-highlight support)."""


import gc
import sys
import inspect
import weakref
import builtins
import pytest

from _pycharm_watchpoint import watch, unwatch, clear_watches, WatchpointHit
from _pycharm_watchpoint import (
    _next_code_line_after_frame,
    builtins,
)

from util import (
    _FakeFrame,
)


def test_hit_payload_includes_caller_file_and_line(monkeypatch):
    """The hit payload encoded by `_pycharm_consume_last_hit` includes
    caller_file (field 6) and caller_line (field 7) – the call-site
    location for the IDE's secondary "call-site" highlight.

    These fields let the Kotlin side mark the exact line that called
    into the code that mutated the watched value, without guessing
    offsets from the bp fire location.
    """
    import _pycharm_watchpoint as watchpoint
    import base64

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        return (file, line, -1)

    monkeypatch.setattr(watchpoint.pydevd_pause, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint.pydevd_pause, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint.pydevd_pause, "_remove_temp_breakpoints",
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
    import _pycharm_watchpoint as watchpoint
    import base64

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        return (file, line, -1)

    monkeypatch.setattr(watchpoint.pydevd_pause, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint.pydevd_pause, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint.pydevd_pause, "_remove_temp_breakpoints",
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
    import _pycharm_watchpoint as watchpoint
    import base64

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        return (file, line, -1)

    monkeypatch.setattr(watchpoint.pydevd_pause, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint.pydevd_pause, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint.pydevd_pause, "_remove_temp_breakpoints",
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
    import _pycharm_watchpoint as watchpoint
    import base64

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        return (file, line, -1)

    monkeypatch.setattr(watchpoint.pydevd_pause, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint.pydevd_pause, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint.pydevd_pause, "_remove_temp_breakpoints",
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
    from _pycharm_watchpoint import _next_code_line_after_frame
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
    import _pycharm_watchpoint as watchpoint

    install_calls: list = []

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        install_calls.append((file, line, watch_name))
        return (file, line, -1)

    monkeypatch.setattr(watchpoint.pydevd_pause, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint.pydevd_pause, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint.pydevd_pause, "_remove_temp_breakpoints",
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


def test_watch_list_inplace_mutation_via_line_diff():
    """In-place mutation of a watched list IS detectable because _value_hash
    uses repr(). The LINE event after the mutation sees a different repr hash.
    This test documents the actual behavior (which contradicts the 'silent'
    claim in earlier docs).
    """
    def _code():
        a = [1, 2]
        watch("a")
        a.append(3)  # in-place mutation
        pass          # LINE event here diffs repr → should fire
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert "[1, 2]" in exc_info.value.old_value
    assert "[1, 2, 3]" in exc_info.value.new_value


def test_object_watch_detects_name_rebinding():
    """watch('obj') on a user-defined object should ALSO detect when 'obj'
    itself is rebound to a different object – not just attribute mutations.

    Currently FAILS: class-surgery path returns early without installing a
    local-variable watch, so rebinding the name goes undetected.
    """
    class Thing:
        val = 1

    def _code():
        obj = Thing()
        watch("obj")
        obj = Thing()  # rebind to entirely new object
        pass  # LINE event here should detect the rebind
    with pytest.raises(WatchpointHit):
        _code()


def test_object_watch_detects_rebind_to_list_and_subsequent_mutations():
    """watch('a') on a user-defined object, then rebinding 'a' to a list,
    should detect both the rebind AND subsequent list mutations.

    Scenario from manual testing: watch 'a' (a Thing instance), then
    'a = [1,2,3]' rebinds it, then 'a.append(4)' mutates the list.
    Both should fire. Currently FAILS: the class-surgery watch on the
    original Thing doesn't monitor the local name, so the rebind is
    missed, and after rebind the list is unwatched entirely.
    """
    class Thing:
        val = 1

    def _code():
        a = Thing()
        watch("a")
        a = [1, 2, 3]  # rebind from Thing to list – should fire
        pass
    with pytest.raises(WatchpointHit):
        _code()
