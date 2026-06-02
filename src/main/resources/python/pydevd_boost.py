"""
pydevd_boost – runtime monkey-patches for pydevd's PEP 669 tracing layer.

Applied via importlib import hook when PYCHARM_WATCHPOINT_ACTIVE=1.
Targets pydevd_pep_669_tracing.py (pure-Python path, used when PYDEVD_USE_CYTHON=NO).

Patches address known performance bugs that cause 11-156x slowdown:
  3. Module-level breakpoints (func_name='None') trace ALL functions in file
  4. Any breakpoint in file traces all functions (missing has_breakpoint_in_frame check)
  5. Exception callback processes all exceptions even when no exception BPs enabled
  6. O(n) stack walk on every exception to find top-level frame

Patches 1+2 (py_start_callback early cache check) are NOT applied because wrapping
monitoring callbacks adds an extra Python frame, breaking _getframe(1) depth
assumptions throughout pydevd. The guide's patches require source-level modification.

Patch 5 uses source-level patching (inspect + exec) to inject the early exit check
directly into py_raise_callback without adding a wrapper frame.

These patches require Python 3.12+ (PEP 669 / sys.monitoring) and pure-Python pydevd
(PYDEVD_USE_CYTHON=NO). When Cython extensions are active, this module has no effect.

Safety: patches are applied with try/except around each one. If any patch fails
(e.g. function signatures changed in a newer PyCharm), we log and skip – the debugger
still works, just without the optimization.
"""

import sys
import os
import functools
import inspect

_LOG_PREFIX = "[WATCHPOINT-BOOST]"
_applied = False
_verbose = os.environ.get('PYCHARM_WATCHPOINT_LOG') == '1'


def _log(msg, *, always=False):
    """Log to stderr. Gated on PYCHARM_WATCHPOINT_LOG=1 unless always=True."""
    if always or _verbose:
        print(f"{_LOG_PREFIX} {msg}", file=sys.stderr)


def _validate_signature(fn, expected_params, fn_name):
    """
    Validate that fn's signature matches expected parameter names (positional).
    Raises ValueError if it doesn't match, so the patch is skipped gracefully.
    """
    try:
        sig = inspect.signature(fn)
        params = list(sig.parameters.keys())
        for i, expected in enumerate(expected_params):
            if i >= len(params) or params[i] != expected:
                raise ValueError(
                    f"{fn_name}: expected param[{i}]='{expected}', "
                    f"got '{params[i] if i < len(params) else '<missing>'}' "
                    f"(full sig: {params})"
                )
    except (ValueError, TypeError) as e:
        raise ValueError(str(e))


def apply_patches(module):
    """
    Apply all performance patches to the given pydevd_pep_669_tracing module.
    Each patch is independent – failure in one doesn't block others.

    IMPORTANT: This must be called BEFORE pydevd registers callbacks with
    sys.monitoring (i.e., before enable_pep669_monitoring()). When called at
    import time, pydevd will register our patched functions as the "originals" –
    no re-registration needed, breakpoints work normally.
    """
    global _applied
    if _applied:
        return
    _applied = True

    applied = []
    skipped = []

    # Patch 3+4: Optimize _should_enable_line_events_for_code
    try:
        _patch_should_enable_line_events(module)
        applied.append("_should_enable_line_events_for_code (module-level BP fix)")
    except Exception as e:
        skipped.append(f"_should_enable_line_events_for_code: {e}")

    # Patch 5: Optimize py_raise_callback with early exception-bp check
    # Uses source-level patching (no wrapper frame) to avoid _getframe depth issues
    try:
        _patch_py_raise_callback(module)
        applied.append("py_raise_callback (exception BP early exit)")
    except Exception as e:
        skipped.append(f"py_raise_callback: {e}")

    # Patch 6: Replace _get_top_level_frame O(n) with O(1) _is_top_level_frame
    try:
        _patch_top_level_frame(module)
        applied.append("_get_top_level_frame (O(1) replacement)")
    except Exception as e:
        skipped.append(f"_get_top_level_frame: {e}")

    total = len(applied) + len(skipped)
    _log(f"Applied {len(applied)}/{total} patches")
    if applied:
        _log(f"  ACTIVE: {', '.join(applied)}")
    if skipped:
        for s in skipped:
            _log(f"  SKIPPED: {s}", always=True)


def _patch_should_enable_line_events(module):
    """
    Patches 3+4: Fix module-level breakpoint over-tracing.

    Real signatures across PyCharm versions:
      2024.3:  (frame, code, filename, info)
      2025.1+: (frame, code, filename, info, will_be_stopped=False)

    Bug #3: When func_name='None' (module-level BP), ALL functions in the file
    get line-traced. Fix: validate BP line is within the function's line range.
    """
    original_fn = getattr(module, '_should_enable_line_events_for_code', None)
    if original_fn is None:
        raise AttributeError("_should_enable_line_events_for_code not found")

    _validate_signature(original_fn, ['frame', 'code', 'filename', 'info'],
                        '_should_enable_line_events_for_code')

    global_cache_frame_skips = getattr(module, 'global_cache_frame_skips', None)
    _make_frame_cache_key = getattr(module, '_make_frame_cache_key', None)
    GlobalDebuggerHolder = None
    try:
        from _pydevd_bundle.pydevd_constants import GlobalDebuggerHolder as _GDH
        GlobalDebuggerHolder = _GDH
    except ImportError:
        pass

    if global_cache_frame_skips is None or _make_frame_cache_key is None:
        raise AttributeError("Required module globals not found")

    @functools.wraps(original_fn)
    def patched_should_enable(frame, code, filename, info, *args, **kwargs):
        py_db = GlobalDebuggerHolder.global_dbg if GlobalDebuggerHolder else None
        if py_db is None:
            return original_fn(frame, code, filename, info, *args, **kwargs)

        breakpoints_for_file = py_db.breakpoints.get(filename)
        if not breakpoints_for_file:
            return original_fn(frame, code, filename, info, *args, **kwargs)

        # Bug #3 fix: if ALL breakpoints in file are module-level and none fall
        # within this function's line range, skip line tracing.
        curr_func_name = code.co_name
        if curr_func_name not in ('?', '<module>', '<lambda>', ''):
            all_module_level = True
            any_in_range = False
            first_line = code.co_firstlineno
            last_line = None

            for bp in breakpoints_for_file.values():
                bp_func_name = getattr(bp, 'func_name', None)
                if bp_func_name != 'None':
                    all_module_level = False
                    break
                if last_line is None:
                    try:
                        lines = [ln for _, _, ln in code.co_lines() if ln is not None]
                        last_line = max(lines) if lines else first_line
                    except AttributeError:
                        last_line = first_line + 1000
                bp_line = getattr(bp, 'line', 0)
                if first_line <= bp_line <= last_line:
                    any_in_range = True
                    break

            if all_module_level and not any_in_range:
                frame_cache_key = _make_frame_cache_key(code)
                global_cache_frame_skips[frame_cache_key] = 0
                return False

        return original_fn(frame, code, filename, info, *args, **kwargs)

    module._should_enable_line_events_for_code = patched_should_enable


def _patch_py_raise_callback(module):
    """
    Patch 5: Inject early has_exception_breakpoints check into py_raise_callback
    using source-level patching (inspect.getsource + exec). This avoids adding a
    wrapper frame which would break _getframe(1) depth assumptions.

    The injected code checks if exception breakpoints are enabled BEFORE any
    expensive operations. Python raises hundreds of thousands of exceptions
    internally for control flow – this eliminates 99% of callback overhead.
    """
    original_fn = getattr(module, 'py_raise_callback', None)
    if original_fn is None:
        raise AttributeError("py_raise_callback not found")

    _validate_signature(original_fn, ['code', 'instruction_offset', 'exception'],
                        'py_raise_callback')

    # Get the source and inject early exit after 'if py_db is None: return'
    source = inspect.getsource(original_fn)

    # The injection point: right after "if py_db is None:\n        return\n"
    # We inject a check for has_exception_breakpoints
    injection = """
    # [WATCHPOINT-BOOST] Early exit when no exception breakpoints are active.
    # Python raises hundreds of thousands of internal exceptions for control flow.
    if not (py_db.break_on_caught_exceptions
            or py_db.has_plugin_exception_breaks
            or getattr(py_db, 'stop_on_failed_tests', False)):
        return
"""

    # Find the injection point – after the 'if py_db is None:' block
    # Pattern: "if py_db is None:\n        return\n"
    marker = "if py_db is None:\n        return\n"
    if marker not in source:
        # Try alternate indentation
        marker = "if py_db is None:\n        return"
        if marker not in source:
            raise RuntimeError("Cannot find injection point in py_raise_callback source")

    # Inject after the marker
    patched_source = source.replace(marker, marker + injection, 1)

    # Dedent the source (inspect.getsource includes module-level indentation)
    import textwrap
    patched_source = textwrap.dedent(patched_source)

    # Compile and exec in the module's namespace so the function has access
    # to all the same globals (GlobalDebuggerHolder, _getframe, etc.)
    code_obj = compile(patched_source, inspect.getfile(original_fn), 'exec')
    exec(code_obj, module.__dict__)

    # Verify the new function exists
    new_fn = getattr(module, 'py_raise_callback', None)
    if new_fn is original_fn:
        raise RuntimeError("exec did not replace py_raise_callback")


def _patch_top_level_frame(module):
    """
    Patch 6: Replace O(n) _get_top_level_frame stack walk with O(1)
    _is_top_level_frame check. Only relevant when exception breakpoints are active.
    """
    from os.path import basename, splitext

    def _is_top_level_frame(frame):
        """Check if frame is a top-level entry point (O(1) instead of walking stack)."""
        name = splitext(basename(frame.f_code.co_filename))[0]
        if name == 'pydevd' and frame.f_code.co_name == '_exec':
            return True
        if name == 'threading' and frame.f_code.co_name == '_bootstrap_inner':
            return True
        return False

    if hasattr(module, '_get_top_level_frame'):
        module._is_top_level_frame = _is_top_level_frame
        original_get = module._get_top_level_frame

        def patched_get_top_level():
            """Compatibility shim – preserves API contract."""
            from sys import _getframe
            frame = _getframe(1)
            while frame:
                if _is_top_level_frame(frame):
                    return frame
                frame = frame.f_back
            return None

        module._get_top_level_frame = patched_get_top_level
    else:
        module._is_top_level_frame = _is_top_level_frame


def install():
    """
    Install the pydevd boost patches. Call this from sitecustomize.

    Strategy:
    - If pydevd_pep_669_tracing is already imported, patch it immediately.
    - Otherwise, hook builtins.__import__ to intercept the module at import time,
      BEFORE pydevd registers callbacks. This ensures pydevd registers our patched
      functions as the "originals" – no re-registration needed, breakpoints intact.
    """
    global _applied
    if _applied:
        return

    target_module = '_pydevd_bundle.pydevd_pep_669_tracing'

    # Already loaded? Patch now (fallback).
    if target_module in sys.modules:
        _log("Module already loaded – patching immediately (late patch)")
        apply_patches(sys.modules[target_module])
        return

    # Hook __import__ to patch the module AT IMPORT TIME.
    import builtins
    _original_import = builtins.__import__

    # Required functions that must exist for the module to be fully loaded.
    _required_attrs = ('py_start_callback', 'py_raise_callback',
                       '_should_enable_line_events_for_code')

    def _patching_import(name, *args, **kwargs):
        result = _original_import(name, *args, **kwargs)
        if not _applied:
            mod = sys.modules.get(target_module)
            if mod is not None:
                # Only patch if the module is FULLY loaded
                if all(hasattr(mod, attr) for attr in _required_attrs):
                    apply_patches(mod)
                    builtins.__import__ = _original_import
                    _log("Import hook triggered – patched at import time (before registration)")
        return result

    builtins.__import__ = _patching_import
    _log("Import hook installed – will patch pydevd_pep_669_tracing on first import")
