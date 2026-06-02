"""f_back intermediate bp targets and the loop-back (tight-loop) bp slot."""


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
    import _pycharm_watchpoint as watchpoint

    install_calls: list = []

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        install_calls.append((file, line, watch_name))
        return (file, line, -1)

    monkeypatch.setattr(watchpoint.pydevd_pause, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint.pydevd_pause, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint.pydevd_pause, "_remove_temp_breakpoints",
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


def test_next_code_line_after_frame_returns_loop_header_for_tight_loop():
    """When the frame is at the last instruction of a tight for-loop body
    with no forward code line, _next_code_line_after_frame should return
    the loop header line via JUMP_BACKWARD target detection. Without this,
    tight loops exhaust the primary bp slot and force the f_back walk,
    which spills bps into library frames.
    """
    import dis
    from _pycharm_watchpoint import _next_code_line_after_frame

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
    from _pycharm_watchpoint import _next_code_line_after_frame

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
    import dis
    import _pycharm_watchpoint as watchpoint

    install_calls: list = []

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        install_calls.append((file, line, watch_name))
        return (file, line, -1)

    monkeypatch.setattr(watchpoint.pydevd_pause, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint.pydevd_pause, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint.pydevd_pause, "_remove_temp_breakpoints",
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
