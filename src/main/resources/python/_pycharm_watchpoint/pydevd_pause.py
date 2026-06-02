"""pydevd integration: debugger lookup, breakpoint install, and the pause
mechanisms (graceful no-op when pydevd is not loaded)."""


import sys
import builtins
from typing import Any, Optional


from . import constants
from .constants import Any, Optional, Tuple, builtins, sys, threading
from .helpers import _MAX_FRAME_WALK_HOPS, _log_warn


# ---------------------------------------------------------------------------
# pydevd integration (graceful no-op when pydevd isn't loaded)
# ---------------------------------------------------------------------------

# Last failure reason, surfaced via _pycharm_watchpoint_diag().
_pydevd_last_error: Optional[str] = None


def _get_pydevd_debugger() -> Any:
    """Return the live pydevd debugger instance, or None if not in a debug session.

    Canonical state lives in `_pydevd_bundle.pydevd_constants.GlobalDebuggerHolder.
    global_dbg`. The top-level `pydevd` module re-exports `get_global_debugger`
    which ultimately reads the same holder, so we prefer the direct read to
    avoid the re-export chain and to work even if `import pydevd` has any
    issues in the current Python startup phase.
    """
    global _pydevd_last_error

    try:
        from _pydevd_bundle.pydevd_constants import GlobalDebuggerHolder
        db = GlobalDebuggerHolder.global_dbg
        if db is not None:
            return db
        _pydevd_last_error = "GlobalDebuggerHolder.global_dbg is None"
    except Exception as e:  # noqa: BLE001
        _pydevd_last_error = f"GlobalDebuggerHolder import failed: {e!r}"

    # Fallback chain in case _pydevd_bundle isn't yet importable.
    try:
        import pydevd
        fn = getattr(pydevd, "get_global_debugger", None)
        if fn is not None:
            db = fn()
            if db is not None:
                return db
            _pydevd_last_error = "pydevd.get_global_debugger() returned None"
    except Exception as e:  # noqa: BLE001
        _pydevd_last_error = f"import pydevd failed: {e!r}"

    return None


def _pycharm_watchpoint_diag() -> str:
    """Return a short diagnostic the user can evaluate from the debugger."""
    import sys as _sys
    in_mod = "pydevd" in _sys.modules
    bundle_in_mod = "_pydevd_bundle.pydevd_constants" in _sys.modules
    tr = _sys.gettrace()
    tr_owner = type(getattr(tr, "__self__", None)).__name__ if tr is not None else "None"
    debugger = _get_pydevd_debugger()
    return (
        f"pydevd in sys.modules: {in_mod}; "
        f"_pydevd_bundle.pydevd_constants in sys.modules: {bundle_in_mod}; "
        f"sys.gettrace owner: {tr_owner}; "
        f"get_global_debugger -> {type(debugger).__name__ if debugger else 'None'}; "
        f"last_error: {_pydevd_last_error}"
    )


def _pause_via_pydevd(py_db: Any, user_frame: Any, watch_name: str,
                      old_repr: str, new_repr: str,
                      source_file: str, source_line: int) -> bool:
    """Hand off to pydevd's own tracer to pause cleanly at the next user line.

    Returns True if the pause was ACTUALLY armed (state set, f_trace
    assigned, PEP 669 events enabled), or False if a silent early-return
    path fired (`_finish_debugging_session`, `is_tracing`). The caller
    uses this signal to decide whether to engage the dedupe gate –
    setting the gate on a `False` return locks out all subsequent hits
    in the same micro-batch even though the IDE never paused for the
    first one, which manifests as "watchpoint hit fires + highlight
    renders, but the debugger never actually stops."

    We deliberately do NOT call `py_db.do_wait_suspend(...)` here. Going through
    do_wait_suspend would mean pydevd's send-suspend-message work (XML build +
    urllib.parse.quote URL encoding for the protocol stream) sits ON TOP of
    our `__setattr__` / `_handle_hit` chain in the user thread's call stack.
    The IDE then reports the thread as paused inside `urllib.parse.quote`,
    with our frames shown as `<frame not available>` because they live in
    a <string>-exec'd module.

    We ALSO deliberately do NOT use `set_suspend(... is_pause=True)` + state =
    STATE_SUSPEND. That sets up "pause on the next event in ANY frame", which
    means the suspend latches on the FIRST `trace_dispatch`-armed frame pydevd
    encounters as code resumes – including frames deep inside print()'s stdout-
    interception chain (a common landing site is `codecs.BufferedIncrementalDecoder.decode`,
    shown as topmost `<frame not available>` with the user's frame visible one
    level down, with `self = <encodings.utf_8.IncrementalDecoder object>`).

    Instead we mimic `pydevd.settrace(suspend=True, stop_at_frame=user_frame)`:
    1. Set `step_cmd = CMD_STEP_OVER` and `step_stop = user_frame` so pydevd's
       tracer treats this like a "step over" scoped to `user_frame`. With
       `state = STATE_RUN` (NOT SUSPEND), `trace_dispatch.can_skip` returns
       True on every frame that isn't `user_frame`, so codec frames in
       print's chain, IO interceptors, and our own `<string>` frames all
       flow through to `trace_exception` (no pause).
    2. When pydevd's tracer hits a LINE event on `user_frame` (frame matches
       step_stop), the CMD_STEP_OVER branch in PyDBFrame.trace_dispatch flips
       `stop = True`, which calls `set_suspend` from pydevd's own tracer
       context and then `do_wait_suspend` – clean pause with user_frame on top.
    3. Walk back from this frame via `f_back` and clear `f_trace = None` on
       every intermediate frame as belt-and-suspenders.
    4. `set_trace_for_frame_and_parents(user_frame)` + a direct `f_trace`
       assignment ensures user_frame's trace is the full `trace_dispatch`
       (pydevd's CALL handler may have parked it at `trace_exception`).

    Trade-off: pause happens on the line AFTER the assignment that triggered
    us (since our __setattr__ has to return before pydevd's tracer can fire).
    The actual assignment line + the watch identity are logged to stderr so
    the user can read them in the debug console.
    """
    import threading
    from _pydevd_bundle.pydevd_comm_constants import CMD_STEP_OVER
    from _pydevd_bundle.pydevd_constants import STATE_RUN, PYTHON_SUSPEND
    from _pydevd_bundle.pydevd_trace_dispatch import set_additional_thread_info

    if getattr(py_db, "_finish_debugging_session", False):
        # Debugger is shutting down – don't try to suspend.
        _log_warn(
            f"_pause_via_pydevd: EARLY RETURN (_finish_debugging_session=True) "
            f"for '{watch_name}'; pause NOT armed"
        )
        return False

    thread = threading.current_thread()
    info = set_additional_thread_info(thread)

    # Reentrancy guard – pydevd's own tracer may already be mid-callback on
    # this thread; let it finish before we try to overlay our suspend.
    if getattr(info, "is_tracing", False):
        _log_warn(
            f"_pause_via_pydevd: EARLY RETURN (info.is_tracing=True) "
            f"for '{watch_name}'; pause NOT armed. "
            f"This means pydevd's tracer is mid-callback on this thread – "
            f"our overlay would race with it."
        )
        return False

    # Emit a one-line hit notification on stderr (debug console) so the user
    # can see which watch fired and where, since we no longer set pydevd's
    # stop_message (which would re-introduce the urllib.quote pause).
    _log_warn(
        f"hit '{watch_name}': {old_repr} -> {new_repr} at {source_file}:{source_line}"
    )

    # Set up a "step over" scoped to user_frame. With state = STATE_RUN
    # (NOT SUSPEND), pydevd's trace_dispatch hits its `can_skip` short-
    # circuit for every frame that isn't `user_frame` – including codec
    # frames in print's stdout chain (`encodings.utf_8.IncrementalDecoder`-
    # adjacent), our own `<string>`-exec'd frames, and any pydevd
    # interceptor frames. Only when pydevd's tracer fires a LINE event on
    # `user_frame` does it flip into the pause path. This is the same
    # mechanism `pydevd.settrace(stop_at_frame=...)` uses for programmatic
    # suspend at a specific frame, and it's the only way to pause cleanly
    # without latching on intermediate stdout-encoding frames.
    info.pydev_state = STATE_RUN
    info.pydev_step_cmd = CMD_STEP_OVER
    info.pydev_step_stop = user_frame
    info.suspend_type = PYTHON_SUSPEND

    # Belt-and-suspenders: disarm `f_trace` on our own watchpoint-runtime
    # frames between here and `user_frame`. The CMD_STEP_OVER setup above
    # already prevents pauses on these via `can_skip`, but pydevd's global
    # sys.settrace may have armed them with `trace_dispatch` during their
    # CALL events – clearing makes their unwind events fire NO tracing at
    # all, which is strictly safer.
    own_frame = sys._getframe(0)
    safety_limit = _MAX_FRAME_WALK_HOPS  # bound against degenerate f_back chains
    while own_frame is not None and own_frame is not user_frame and safety_limit > 0:
        try:
            own_frame.f_trace = None
        except Exception:  # noqa: BLE001
            # f_trace assignment is documented as always-writable on
            # CPython, but tolerate a hypothetical refusal rather than
            # blowing up the pause path.
            pass
        own_frame = own_frame.f_back
        safety_limit -= 1

    # Arm pydevd's tracer on user_frame and its callers so the next line
    # event in any of them flips into pydevd's pause flow.
    #
    # CRITICAL: we set `user_frame.f_trace` DIRECTLY first, then call the
    # official API. Pydevd's CALL handler often parks user_frame's f_trace
    # at `trace_exception` (a fast-path that only handles exception
    # events, no suspend check) when the frame has no breakpoints and no
    # active step command at the moment of CALL. After `set_suspend`
    # flipped state to STATE_SUSPEND, we need user_frame's NEXT line
    # event to fire pydevd's FULL `trace_dispatch` (the one that pauses
    # on suspend). `set_trace_for_frame_and_parents` is supposed to do
    # this, but it has multiple early-return paths (PEP 669 monitoring
    # mode, filtered files, etc.) where it silently does nothing –
    # leaving user_frame.f_trace at `trace_exception`. Then the LINE
    # event fires trace_exception (no pause), the print on the next
    # user line runs, and the pause finally latches deep inside print's
    # internal codecs/IO chain (`<encodings.utf_8.IncrementalDecoder>`-
    # adjacent frames) – the IDE shows that frame as topmost
    # `<frame not available>` with the user's actual frame one level
    # down. Setting f_trace directly bypasses the API's early returns.
    trace_dispatch_fn = getattr(py_db, "trace_dispatch", None)
    if trace_dispatch_fn is not None:
        try:
            user_frame.f_trace = trace_dispatch_fn
        except Exception:  # noqa: BLE001
            pass

    set_trace_for_parents = getattr(py_db, "set_trace_for_frame_and_parents", None)
    if set_trace_for_parents is not None:
        try:
            set_trace_for_parents(user_frame)
        except Exception:  # noqa: BLE001
            pass

    # PEP 669 supplement (Python 3.12+). `set_trace_for_frame_and_parents`
    # was designed for the sys.settrace world; under sys.monitoring it is
    # mostly a no-op on frames pydevd hasn't already armed. Result: a hit
    # whose user_frame belongs to a function pydevd hasn't entered with an
    # active step command (e.g. a fast helper like
    # `charge_card`/`ship_to_customer` that we step INTO during run mode)
    # never gets LINE / PY_RETURN events delivered to pydevd's PEP 669
    # callbacks, so our `CMD_STEP_OVER + step_stop = user_frame` never fires
    # a pause. The subsequent hit overwrites step_stop and the pause
    # materialises only for the LAST hit – we observed this as "stops 2
    # times for 3 mutations" in test_demo_b.
    #
    # We force event delivery to pydevd's tool (DEBUGGER_ID = 0) on the
    # relevant code objects ourselves. Two code objects matter:
    #
    # 1. `user_frame.f_code` – needs both LINE (for "next line in this
    #    function" stepping) AND PY_RETURN (for "function returned without
    #    another LINE", which is the EXACT failure mode in test_demo_b:
    #    `order.status = "paid"` is the last statement of `charge_card`, so
    #    no further LINE event ever fires – pydevd has to learn about the
    #    return through PY_RETURN to recognize the step-over as completed).
    #
    # 2. `user_frame.f_back.f_code` – the caller's code. After PY_RETURN
    #    completes pydevd's step-over, the pause needs to land on the
    #    caller's NEXT line event. Without LINE enabled there for
    #    DEBUGGER_ID, pydevd's tracer never gets called when the caller
    #    resumes after `charge_card()` returns, and the pause is silently
    #    dropped. PY_RETURN on the caller is included for symmetry – if the
    #    caller is ALSO returning (e.g. our hit fires from the last line
    #    of a one-liner caller), pydevd still sees the unwind.
    #
    # `set_local_events` is per-code-object and overwrites the previous
    # event mask, so we OR with the current events to avoid stomping on
    # whatever pydevd already has registered there.
    supplement_status = "skipped"
    try:
        debugger_tool_id = sys.monitoring.DEBUGGER_ID  # 0 per PEP 669
        wanted = (
            sys.monitoring.events.LINE
            | sys.monitoring.events.PY_RETURN
        )
        existing = sys.monitoring.get_local_events(debugger_tool_id, user_frame.f_code)
        sys.monitoring.set_local_events(
            debugger_tool_id, user_frame.f_code, existing | wanted,
        )
        # Caller's f_code too – the natural pause landing site once
        # user_frame returns. Skip silently if there is no caller (only
        # happens for the program's top frame, which a watchpoint cannot
        # realistically fire from since there's no `__setattr__` chain
        # reaching root – but the guard costs nothing).
        f_back = user_frame.f_back
        if f_back is not None:
            existing_back = sys.monitoring.get_local_events(
                debugger_tool_id, f_back.f_code,
            )
            sys.monitoring.set_local_events(
                debugger_tool_id, f_back.f_code, existing_back | wanted,
            )
        supplement_status = "applied"
    except Exception as e:  # noqa: BLE001
        # Safe to ignore: if pydevd isn't using its DEBUGGER_ID tool slot
        # (older Python, sys.settrace mode, etc.), the regular trace_dispatch
        # mechanism above handles the pause and we don't need this supplement.
        supplement_status = f"failed ({e!r})"

    _log_warn(
        f"_pause_via_pydevd: pause ARMED for '{watch_name}' at "
        f"{user_frame.f_code.co_filename}:{user_frame.f_lineno} "
        f"(step_stop set, f_trace={'set' if trace_dispatch_fn else 'unavailable'}, "
        f"PEP669_supplement={supplement_status}). "
        f"Now waiting for pydevd's tracer to fire a LINE/PY_RETURN event "
        f"on this code object."
    )
    return True


def _get_except_handler_lines(code: Any) -> set:
    """Get line numbers that are ONLY reachable via exception handlers.

    These lines are unreachable by normal sequential flow – installing a bp
    there means it will never fire in the normal (no-exception) path. This
    is the root cause of the out-of-order watchpoint hits bug: a mutation
    on the last line of a try body installs a bp at the next code line
    (the except handler), which never fires; the safety-net bp at a distant
    caller fires much later giving confusing ordering.

    Important: lines that appear in BOTH handler regions AND normal-flow
    regions (e.g. inlined finally bodies) are NOT excluded – they ARE
    reachable in normal flow.

    Uses `dis._parse_exception_table` (private but stable since 3.12).
    Returns an empty set on failure (graceful degradation to old behavior).
    """
    try:
        import dis

        # Collect all (start_offset, end_offset, line) triples from co_lines.
        line_entries = [
            (start, end, line)
            for (start, end, line) in code.co_lines()
            if line is not None
        ]
        if not line_entries:
            return set()

        # Build handler regions: the bytecode range [target, ...) for each
        # depth=0 handler. We approximate the handler's end as the next
        # handler's start or the code's end.
        handler_targets = sorted(set(
            entry.target
            for entry in dis._parse_exception_table(code)
            if entry.depth == 0
        ))
        if not handler_targets:
            return set()

        # For each line, determine if ALL its bytecode is within handler
        # regions. A line with bytecode both inside and outside a handler
        # region (e.g. inlined finally body) should NOT be excluded.
        # Strategy: a line entry at offset X is "in a handler" if X >= some
        # handler target AND X < the next non-handler code after that target.
        # Simplified: just check if the line entry's start offset is >= any
        # handler target. Since handlers are contiguous blocks at the end of
        # the code object (CPython's layout), any offset >= a handler_target
        # that's before the next try body is in a handler.

        # Simpler approach: find handler entry lines (first line at each
        # handler target) and lines exclusively within handler ranges.
        # For correctness with finally inlining, only mark lines where
        # EVERY occurrence in co_lines is within a handler region.
        handler_only_lines: set = set()
        normal_flow_lines: set = set()

        # Determine which offsets are in handler regions.
        # Handler region starts at the target; for simplicity we consider
        # any offset >= target that's before the next non-handler entry
        # point to be "in a handler". CPython lays out handlers after the
        # try body's jump, so an offset is in a handler if it's >= any
        # handler_target and there's no lower handler_target between it.
        handler_target_set = set(handler_targets)

        # Build the set of "handler region" offsets. An instruction at
        # offset X is in a handler if X >= some handler target. The try
        # body is always at lower offsets. This heuristic works because
        # CPython always emits try bodies before their handlers.
        min_handler_offset = min(handler_targets)

        for (start, end, line) in line_entries:
            if start >= min_handler_offset:
                handler_only_lines.add(line)
            else:
                normal_flow_lines.add(line)

        # Lines in both sets are reachable in normal flow – exclude them.
        return handler_only_lines - normal_flow_lines
    except Exception:  # noqa: BLE001
        return set()


def _next_code_line_in(code: Any, after_line: int) -> Optional[int]:
    """Find the smallest line number in `code` that's strictly > `after_line`
    AND reachable by normal sequential flow (not an exception handler entry).

    Uses `code.co_lines()` (Python 3.10+) which iterates the line table.
    Only ACTUAL code lines are returned – blank lines, lines between
    statements, and lines past the function's last statement are not in
    the table and won't be returned.

    Exception handler lines (the `except ...:` clause, `finally:` handler
    entry, etc.) are excluded because they're unreachable in normal flow.
    When a mutation happens on the last line of a try body, the numerically
    next code line is the except handler – installing a bp there means it
    never fires in the normal path. Skipping those lines lets us find the
    first line AFTER the try/except block, which IS reachable.

    Falls back to including handler lines if no non-handler candidate
    exists (better than returning None and forcing a do_wait_suspend
    fallback).

    Why this matters: pydevd `LineBreakpoint` fires only when a LINE event
    is emitted for the breakpoint's line. LINE events only fire for actual
    code lines. If we install a bp at `f_lineno + 1` and that line is
    blank (e.g., the function ends right after the watched mutation),
    the bp sits there inert and the pause never materialises – the
    user-reported `set_accessible_products` case exactly: line 195 is the
    function's last statement, line 196 is blank, bp at 196 never fires.

    Returns the smallest valid code line > `after_line`, or None if no
    such line exists in this code object (in which case the caller should
    walk up to f_back and try there).
    """
    try:
        # co_lines yields (start_offset, end_offset, lineno) tuples;
        # `lineno` can be None for synthetic instructions, which we skip.
        all_lines = [
            ln for (_, _, ln) in code.co_lines()
            if ln is not None and ln > after_line
        ]
        if not all_lines:
            return None

        # Exclude exception handler entry lines – they're unreachable in
        # normal (no-exception) flow and installing a bp there means it
        # silently never fires.
        handler_lines = _get_except_handler_lines(code)
        if handler_lines:
            non_handler = [ln for ln in all_lines if ln not in handler_lines]
            if non_handler:
                return min(non_handler)
            # All candidates are handler lines – fall back to the closest
            # one (better than None which triggers do_wait_suspend).

        return min(all_lines)
    except Exception:  # noqa: BLE001
        # `co_lines` exists on all 3.12/3.13/3.14, but some exotic
        # code objects (e.g., compiled by alternative frontends) might
        # not implement it. Returning None falls back gracefully.
        return None


def _offset_to_line(code: Any, offset: int) -> Optional[int]:
    """Map a bytecode offset to its source line via co_lines()."""
    try:
        for start, end, line in code.co_lines():
            if line is not None and start <= offset < end:
                return line
    except Exception:  # noqa: BLE001
        pass
    return None


def _next_code_line_after_frame(frame: Any) -> Optional[int]:
    """Find the next LINE event after the frame's current bytecode offset.

    Source-line order and execution order differ for multi-line statements.
    The openapi.py failure is exactly this shape: STORE_ATTR reports line 105,
    but the RHS continuation lines 106-112 have already executed. Looking at
    `f_lasti` lets us pick the next executable line in the future.

    Exception-handler-only lines are intentionally skipped. If a mutation is
    the last normal statement in a try body, bytecode order points at the
    except handler next, but that line is unreachable on the no-exception path
    and a breakpoint there would sit inert until a distant safety net fires.

    Backward-pointing lines are rejected UNLESS they are loop back-edge
    targets (JUMP_BACKWARD). In a tight loop like
    ``for i in range(N): setattr(...)``, the next bytecode after setattr is
    JUMP_BACKWARD to the for-header. That line WILL execute again on the
    next iteration, making it a valid bp target. Without this, tight loops
    exhaust the primary slot and spill bps into caller frames (often library
    code). The target line is resolved via co_lines() because
    JUMP_BACKWARD itself has no starts_line attribute.
    """
    lasti = getattr(frame, "f_lasti", None)
    if lasti is None:
        return None
    try:
        import dis
        current_line = getattr(frame, "f_lineno", None)
        handler_lines = _get_except_handler_lines(frame.f_code)
        loop_back_line = None
        for inst in dis.get_instructions(frame.f_code):
            if inst.offset <= lasti:
                continue
            if (loop_back_line is None
                    and current_line is not None
                    and inst.opname in ("JUMP_BACKWARD",
                                        "JUMP_BACKWARD_NO_INTERRUPT")):
                target_line = _offset_to_line(frame.f_code, inst.argval)
                if (target_line is not None
                        and target_line != current_line
                        and target_line not in handler_lines):
                    loop_back_line = target_line
            line = _instruction_starts_line(inst)
            if (line is not None
                    and line != current_line
                    and (current_line is None or line > current_line)
                    and line not in handler_lines):
                return line
        return loop_back_line
    except Exception:  # noqa: BLE001
        return None
    return None


def _instruction_starts_line(inst: Any) -> Optional[int]:
    """Return the source line if `inst` starts a new line event.

    Python 3.14 changed `dis.Instruction.starts_line` to a bool and moved
    the actual line number to `line_number`; 3.12/3.13 store the integer
    directly in `starts_line`.
    """
    starts_line = getattr(inst, "starts_line", None)
    if isinstance(starts_line, int) and not isinstance(starts_line, bool):
        return starts_line
    if starts_line:
        line_number = getattr(inst, "line_number", None)
        if isinstance(line_number, int):
            return line_number
    return None


def _install_bp_at(py_db: Any, target_code: Any, file: str, line: int,
                   watch_name: str) -> Optional[Tuple[str, int, int]]:
    """Install a single temporary pydevd `LineBreakpoint` at (file, line).

    `target_code` is the code object that owns `line`. We need it to call
    `sys.monitoring.set_local_events(DEBUGGER_ID, target_code, ...)` so
    pydevd's `py_line_callback` fires for the bp regardless of pydevd's
    per-function "should-trace" decision made at the function's first
    entry (design contract §13).

    Returns `(file, line, bp_id)` on success – caller appends to
    `WatchpointRegistry._temp_breakpoints` for later cleanup. Returns None
    if install failed (LineBreakpoint import error, debugger shutting down,
    `consolidate_breakpoints` raised, etc.) – caller decides whether to
    fall back to `_pause_via_do_wait_suspend` or drop the hit silently.

    Why `LineBreakpoint` + `consolidate_breakpoints`: it is the same path
    that user-set breakpoints go through – the most heavily exercised
    code path in pydevd. Pydevd's `consolidate_breakpoints` calls
    `clear_skip_caches` which under PEP 669 calls `restart_events()`,
    re-firing PY_START for currently-executing code so pydevd's
    per-function setup logic re-runs and correctly enables LINE tracing
    now that the file has a breakpoint.

    Caller is responsible for selecting (file, line, target_code) – the
    sequential-bps logic in `WatchpointRegistry._compute_bp_target` walks
    the anchor frame's code lines to find the next unused slot, so this
    helper does not do candidate selection itself anymore.
    """
    try:
        from _pydevd_bundle.pydevd_breakpoints import LineBreakpoint
    except Exception as e:  # noqa: BLE001
        _log_warn(
            f"_install_bp_at: LineBreakpoint import failed ({e!r}); "
            f"pause cannot be armed via breakpoint."
        )
        return None

    if getattr(py_db, "_finish_debugging_session", False):
        _log_warn(
            f"_install_bp_at: debugger is shutting down; "
            f"skipping bp install for '{watch_name}'."
        )
        return None

    try:
        # Pydevd internally uses the "real path" (post-`os.path.realpath` +
        # case-normalization on Windows) as the key for both
        # `py_db.breakpoints` AND `_FILENAME_TO_IN_SCOPE_CACHE`. Its
        # `py_line_callback` looks up `py_db.breakpoints[real_path]`. If
        # we registered our bp under `co_filename` (which can differ –
        # e.g. macOS `/var` vs `/private/var`, symlink-collapsed paths,
        # case differences on Windows), the lookup misses and the bp
        # silently never fires. Always normalize via pydevd's own
        # function so our keys match what pydevd computes from the
        # live frame.
        try:
            from pydevd_file_utils import (
                get_abs_path_real_path_and_base_from_file,
            )
            abs_path, real_path, _base = get_abs_path_real_path_and_base_from_file(file)
        except Exception:  # noqa: BLE001
            abs_path = file
            real_path = file

        # Negative bp_id so we never collide with pydevd's positive,
        # IDE-assigned IDs. Hash the (watch_name, file, line) tuple
        # for some semblance of uniqueness across concurrent watches.
        bp_id = -((hash((watch_name, real_path, line)) & 0x7FFFFFFF) + 1)

        # `func_name` matters when the bp is in a library file. Pydevd's
        # `_should_enable_line_events_for_code` (in
        # `_pydevd_bundle/pydevd_pep_669_tracing.py`) iterates the file's
        # bps and decides whether to enable LINE tracing for the CURRENT
        # frame's code object by matching `bp.func_name` against
        # `curr_func_name`. A bp with `func_name=""` never matches – so
        # for library files where pydevd's PY_START callback would
        # otherwise short-circuit, LINE tracing stays disabled and our
        # bp never fires. Setting `func_name` to the target function's
        # actual name (`target_code.co_name`) makes the match succeed.
        bp = LineBreakpoint(
            line,
            condition=None,
            func_name=target_code.co_name,
            expression=None,
            suspend_policy="NONE",
        )

        # Pre-mark the file as "in project scope" so pydevd's PY_START
        # callback doesn't return DISABLE on first entry (it does so for
        # LIB_FILE files whose `in_project_scope()` returns False – which
        # includes everything under site-packages by default). Without
        # this, pydevd never reaches `_should_enable_line_events_for_code`
        # for library files, and LINE tracing for our target_code stays
        # disabled. We mark BOTH `abs_path` and `real_path` because the
        # cache is keyed by whichever pydevd happens to look up.
        try:
            from _pydevd_bundle.pydevd_utils import _FILENAME_TO_IN_SCOPE_CACHE
            _FILENAME_TO_IN_SCOPE_CACHE[real_path] = True
            _FILENAME_TO_IN_SCOPE_CACHE[abs_path] = True
            _FILENAME_TO_IN_SCOPE_CACHE[file] = True
        except Exception as e:  # noqa: BLE001
            _log_warn(
                f"_install_bp_at: could not mark {file} as in-project ({e!r}); "
                f"bp may not fire if pydevd's library filter rejects it."
            )

        id_to_bp = py_db.file_to_id_to_line_breakpoint.setdefault(real_path, {})
        id_to_bp[bp_id] = bp

        # `consolidate_breakpoints` does the standard pydevd setup:
        # - Builds line→bp map in py_db.breakpoints[real_path]
        # - Clears global_cache_skips / global_cache_frame_skips
        # - In PEP 669 mode, calls `restart_events()`.
        py_db.consolidate_breakpoints(real_path, id_to_bp, py_db.breakpoints)

        # CRITICAL: also force LINE+PY_RETURN events armed for the
        # target code object directly. Without this, the bp WON'T
        # fire for the function that's currently mid-execution.
        # Why: pydevd's PY_START callback decides LINE tracing per
        # code object on first entry. For files with no breakpoints
        # at that moment, it decides "no LINE tracing needed" and
        # never arms LINE for the code object. `restart_events`
        # re-fires already-armed events but doesn't fabricate a
        # fresh PY_START to re-evaluate – so the current invocation
        # of e.g. `_get_relation` retains its "no LINE" decision
        # from BEFORE we added the breakpoint, and our bp at
        # features_calculation.py:594 never gets checked.
        #
        # `set_local_events` overrides that decision at the C level,
        # making LINE+PY_RETURN fire for `target_code` regardless of
        # what pydevd's per-function setup decided. Pydevd's
        # py_line_callback then sees the bp via the breakpoints dict
        # and pauses. PY_RETURN is included for symmetry – covers
        # cases where target_code's bp-line is unreachable in this
        # invocation and the function returns first.
        try:
            debugger_tool_id = sys.monitoring.DEBUGGER_ID
            wanted = (
                sys.monitoring.events.LINE
                | sys.monitoring.events.PY_RETURN
            )
            existing = sys.monitoring.get_local_events(
                debugger_tool_id, target_code,
            )
            sys.monitoring.set_local_events(
                debugger_tool_id, target_code, existing | wanted,
            )
            # CRITICAL: restart_events() MUST be called AFTER
            # set_local_events. Per CPython docs, restart_events()
            # "triggers events to be generated immediately for the
            # currently running code." Without this, set_local_events
            # on a code object that's MID-EXECUTION may not take
            # effect for the current frame – the instrumentation
            # points in the bytecode aren't updated until the next
            # function entry. This is the root cause of the csp
            # mystery: _make_nonce's bp at line 28 installed and
            # local_events was set, but LINE never fired because
            # CPython's eval loop hadn't re-instrumented the active
            # frame's bytecode. restart_events() forces that
            # re-instrumentation immediately.
            sys.monitoring.restart_events()
            arm_status = "armed"
        except Exception as e:  # noqa: BLE001
            # Tool ID not in use (sys.settrace mode, older pydevd).
            # The regular bp install above still works for future
            # invocations; only mid-execution is the concern.
            arm_status = f"failed ({e!r})"

        _log_warn(
            f"_install_bp_at: bp installed at "
            f"{real_path}:{line} (bp_id={bp_id}, target_code={target_code.co_name}, "
            f"local_events={arm_status}, original_path={file}) for '{watch_name}'"
        )

        # DIRECT-PAUSE MECHANISM: arm LINE on OUR tool (_TOOL_ID) so our
        # _on_line callback fires independently of pydevd's DEBUGGER_ID.
        # This is the belt-and-suspenders fix for the csp mystery: even if
        # pydevd's py_line_callback never fires (because its PY_START
        # DISABLEd the code before our bp existed and set_local_events on
        # DEBUGGER_ID doesn't reliably re-arm mid-execution), OUR callback
        # WILL fire because _TOOL_ID has no prior DISABLE history for this
        # code object. When our _on_line sees (code, line) in
        # _bp_pause_pending, it triggers do_wait_suspend directly.
        try:
            if constants._TOOL_ID is not None:
                tool_existing = sys.monitoring.get_local_events(
                    constants._TOOL_ID, target_code,
                )
                sys.monitoring.set_local_events(
                    constants._TOOL_ID, target_code,
                    tool_existing | sys.monitoring.events.LINE,
                )
                # Register in the registry's pending dict so _on_line
                # knows to trigger a pause for this (code, line).
                builtins._watchpoint_registry._bp_pause_pending[(id(target_code), line)] = True
        except Exception as e:  # noqa: BLE001
            _log_warn(
                f"_install_bp_at: direct-pause arm on _TOOL_ID failed "
                f"({e!r}); relying on pydevd's callback alone."
            )

        # Return `real_path` as the bp file key – `_remove_temp_breakpoints`
        # uses it to look up `py_db.file_to_id_to_line_breakpoint[real_path]`.
        # The caller's selective-drain match (hit's `bp_anchor_file`) also
        # uses this, so the Kotlin side's pause location should match.
        return (real_path, line, bp_id)
    except Exception as e:  # noqa: BLE001
        _log_warn(
            f"_install_bp_at: failed to install at "
            f"{file}:{line} ({e!r})"
        )
        return None


def _pause_via_do_wait_suspend(py_db: Any, frame: Any,
                               watch_name: str) -> bool:
    """Last-resort pause: call `do_wait_suspend` directly on `frame`.

    Used as a fallback when no valid next-code-line bp slot is available
    via `WatchpointRegistry._compute_bp_target`. The user-reported scenario is the
    `script.py` last-line case: watched local mutates on line 16, the
    LINE-event for line 18 (the last code line in the module's body)
    fires our callback to detect the change, and there's no next line
    in the module to install a bp on. f_back is Python's runner /
    pydevd's machinery (filtered as library), so the bp approach has
    nowhere to land.

    Trade-off (a.k.a. "the urllib trap" – see `_pause_via_pydevd`'s
    rule-1 docstring): `do_wait_suspend` sends the IDE a "stopped"
    message via pydevd's protocol layer, which encodes the protocol
    XML using `urllib.parse.quote`. That puts `urllib/parse.py` on top
    of the user thread's call stack at pause time. The IDE shows
    `urllib/parse.py` as the topmost frame, our `<string>` runtime
    below it, then the actual user frame at the bottom. The user has
    to click their frame in the Debugger's Frames panel to see their
    code.

    Why we use this anyway: silent no-pause is worse than ugly-stack
    pause for a deliberate `watch(...)` call. The user *asked* the
    debugger to pause when the variable changes; honoring that with
    a less-than-ideal stack is better than ignoring it.

    Returns True if pause armed (do_wait_suspend invoked – it blocks
    until user resumes, so on return either the pause completed
    cleanly or the call failed before blocking). False on any setup
    exception.
    """
    try:
        import threading
        from _pydevd_bundle.pydevd_comm_constants import CMD_SET_BREAK

        if getattr(py_db, "_finish_debugging_session", False):
            _log_warn(
                f"_pause_via_do_wait_suspend: debugger shutting down, "
                f"skipping fallback for '{watch_name}'"
            )
            return False

        thread = threading.current_thread()
        # CMD_SET_BREAK is what real breakpoints use for set_suspend –
        # matches the watchpoint's "this is a break-like event" semantics
        # better than CMD_THREAD_SUSPEND (which is the manual-pause
        # button's command).
        py_db.set_suspend(thread, CMD_SET_BREAK, suspend_other_threads=False)
        # Blocks here until the user clicks Resume / Step. The IDE-side
        # sessionPaused fires during this call (before the block), so
        # the highlighter has a chance to drain the queue, install
        # decorations, and clear the dedupe gate while we're paused.
        py_db.do_wait_suspend(thread, frame, 'line', None)
        return True
    except Exception as e:  # noqa: BLE001
        _log_warn(
            f"_pause_via_do_wait_suspend: failed for '{watch_name}': {e!r}"
        )
        return False


def _remove_temp_breakpoints(py_db: Any, installed: list) -> None:
    """Remove the breakpoints installed by `_install_bp_at`.

    Safe to call when py_db is None, when the breakpoints were never
    installed, when pydevd is mid-shutdown, etc. Errors are logged but
    never raised – a leaked bp is a minor annoyance; an exception out of
    `_pycharm_consume_last_hit` could destabilize the IDE-side drain
    flow.
    """
    if py_db is None or not installed:
        return
    for file, line, bp_id in installed:
        try:
            id_to_bp = py_db.file_to_id_to_line_breakpoint.get(file)
            if id_to_bp is None:
                continue
            id_to_bp.pop(bp_id, None)
            # Rebuild line→bp map so the user's bp (if any) at the same
            # line gets restored. `consolidate_breakpoints` is what does
            # this rebuilding.
            py_db.consolidate_breakpoints(file, id_to_bp, py_db.breakpoints)
        except Exception as e:  # noqa: BLE001
            _log_warn(
                f"_remove_temp_breakpoints: failed to remove bp at "
                f"{file}:{line} (bp_id={bp_id}): {e!r}"
            )
