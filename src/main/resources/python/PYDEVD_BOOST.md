# pydevd_boost – Runtime Performance Patches for pydevd PEP 669 Tracing

> Location: `src/main/resources/python/pydevd_boost.py`
> Injected via: sitecustomize.py alongside `watchpoint.py`

## What it does

Injects performance patches into pydevd's PEP 669 tracing layer at runtime.
Eliminates 80-99% of debugger overhead for normal debugging sessions.
Works on Python 3.12+ with `PYDEVD_USE_CYTHON=NO` (forced by our sitecustomize).

## How it's loaded

1. sitecustomize.py sets `PYDEVD_USE_CYTHON=NO` (forces pure-Python pydevd)
2. sitecustomize execs `pydevd_boost.py` as module `_pycharm_watchpoint_boost`
3. `install()` hooks `builtins.__import__` to intercept the target module
4. When `_pydevd_bundle.pydevd_pep_669_tracing` finishes loading, patches are applied
5. pydevd later calls `enable_pep669_monitoring()` which registers our patched functions

## Active patches (3 of 6 from the guide)

| Patch | Target | Technique | Impact |
|-------|--------|-----------|--------|
| 3+4 | `_should_enable_line_events_for_code` | functools.wraps wrapper | Prevents 156x slowdown from module-level BPs |
| 5 | `py_raise_callback` | Source-level injection (inspect+exec) | 99% reduction in exception callback overhead |
| 6 | `_get_top_level_frame` | Direct replacement | Eliminates O(n) stack walk per exception |

## Why patches 1+2 are NOT applied

Patch 1 wraps `py_start_callback` (early cache check before `_getframe(1)`).
**Cannot work as a wrapper** because:

- `py_start_callback` uses `_getframe(1)` to get the user's frame
- `_get_thread_info(True, 1)` passes hardcoded depth to `_create_thread_info(depth+1)`
- Adding ANY Python wrapper frame shifts all depths by +1
- `_getframe(1)` then returns the wrapper's frame instead of user code
- Result: pydevd can't identify the correct file/line, breakpoints never hit

### What we tried (and failed) for patch 1:

1. **Wrapper + re-registration** – wrapped the function, re-registered with
   `sys.monitoring.register_callback()`. Broke breakpoints on ALL Python versions
   because re-registration invalidates armed local LINE events.

2. **Wrapper + deferred thread** – same wrapper, applied after pydevd loaded via
   a polling thread. Same _getframe depth issue.

3. **Wrapper + _getframe patching** – patched `module._getframe` with a thread-local
   flag that adds +1 when called from our wrapper. Failed because `_get_thread_info`
   also passes hardcoded depth values that don't go through `_getframe`.

4. **Import hook (current approach for patch 5)** – patches at import time so pydevd
   registers our version. Works for source-patched functions (no extra frame) but
   NOT for wrappers (extra frame still breaks depths).

**Conclusion**: Patch 1 requires modifying the function's source code (as the original
guide intended – it patches the .py file on disk). The `inspect.getsource()` + `exec()`
approach used for patch 5 COULD theoretically work for patch 1, but it's much more
complex because the injection point is deep inside a large function with many branches.

## Why patch 5 uses inspect+exec (source-level patching)

`py_raise_callback` also uses `_getframe(1)`. A wrapper would break it.
Instead we:

1. Get the function's source via `inspect.getsource()`
2. Find the marker `if py_db is None:\n        return\n`
3. Inject the early-exit check (5 lines) right after that marker
4. `exec()` the modified source in `module.__dict__`
5. The new function has identical frame depth – no wrapper frame

This is safe because:
- The injection point is stable across pydevd versions (the null check is always first)
- If the marker isn't found, the patch is skipped gracefully
- The injected code is purely an early-return optimization – can't break correctness

## The import hook mechanism

```python
builtins.__import__ = _patching_import  # installed in sitecustomize
```

Key details:
- Checks `sys.modules.get('_pydevd_bundle.pydevd_pep_669_tracing')` after every import
- **Must verify module is fully loaded** before patching – Python adds modules to
  `sys.modules` BEFORE executing their body (for circular import handling)
- We check for late-defined functions (`py_start_callback` at line ~494) as proof
  the module finished executing
- Restores original `__import__` after successful patch (one-shot)

### The half-loaded module trap

During import of `pydevd_pep_669_tracing.py`, nested imports (e.g., `from _pydevd_bundle.pydevd_constants import ...`) trigger our hook. At that point the module is in
`sys.modules` but only partially executed – early functions exist but late ones don't.
If we patch prematurely, we get "py_start_callback not found" while
`_get_top_level_frame` (defined earlier in the file) works fine.

Fix: require ALL key functions to exist before patching:
```python
_required_attrs = ('py_start_callback', 'py_raise_callback',
                   '_should_enable_line_events_for_code')
if all(hasattr(mod, attr) for attr in _required_attrs):
    apply_patches(mod)
```

## The callback re-registration trap

**NEVER call `sys.monitoring.register_callback()` after pydevd has armed breakpoints.**

Re-registration invalidates local LINE events that pydevd set via
`sys.monitoring.set_local_events()`. This causes all breakpoints to stop hitting.
Tested on Python 3.12, 3.13, 3.14 – broken on ALL versions with PyCharm 2026.1.

The only safe way to get patched callbacks registered: patch the module-level
function BEFORE `enable_pep669_monitoring()` is called (i.e., during import).

## The _getframe depth contract

pydevd's monitoring callbacks assume a specific call stack layout:

```
[C code: sys.monitoring dispatch]
  → py_start_callback(code, instruction_offset)     # _getframe(0) = this
      → _getframe(1) = user code frame              # the frame that triggered PY_START
```

If you add a Python wrapper:
```
[C code: sys.monitoring dispatch]
  → our_wrapper(code, instruction_offset)           # _getframe(0) = wrapper
      → original_py_start_callback(...)             # _getframe(0) = original
          → _getframe(1) = our_wrapper's frame ← WRONG! Should be user frame
```

This is unfixable without source-level patching because:
- `_getframe` depth is hardcoded in multiple places
- `_get_thread_info(True, 1)` passes depth as a parameter (not via module._getframe)
- `_create_thread_info(depth + 1)` propagates it further

## PyCharm version compatibility

Tested function signatures:

| Function | 2024.3 | 2025.1 | 2025.2 | 2026.1 |
|----------|--------|--------|--------|--------|
| `py_start_callback` | `(code, instruction_offset)` | same | same | same |
| `py_raise_callback` | `(code, instruction_offset, exception)` | same | same | same |
| `_should_enable_line_events_for_code` | `(frame, code, filename, info)` | same | +`will_be_stopped=False` | same |
| `_get_top_level_frame` | `()` | same | same | same |

The `_validate_signature()` check ensures graceful skip if signatures change.

## The PYDEVD_USE_CYTHON=NO decision

We force pure-Python pydevd for ALL watchpoint debug sessions because:

1. Cython .so files don't exist for Python 3.13+ in PyCharm ≤2026.1
2. The pure-Python version WITH our patches is faster than buggy Cython
3. Eliminates `ImportError: No module named '_pydevd_bundle.pydevd_cython_darwin_314_64'`

**Note**: Normal debug (non-watchpoint) still uses whatever PyCharm defaults to.
Users on 3.13/3.14 with older PyCharm will get Cython errors on normal debug – that's
a PyCharm bug, not ours. They need `PYDEVD_USE_CYTHON=NO` in their run config env.

## Performance expectations

Based on the pydevd_boost_guide.md benchmarks (10M function calls):

| Scenario | Without patches | With patches | Speedup |
|----------|----------------|--------------|---------|
| No breakpoints (debug mode) | 4.34s | ~0.28s | ~15x |
| Module-level breakpoint | 43.06s | ~0.28s | ~156x |
| Breakpoint in unused function | 3.16s | ~0.27s | ~11x |

Real-world Django monolith: previously ~10s slower than Cython, now comparable.

## File layout

```
src/main/resources/python/
├── pydevd_boost.py          # This module – runtime patches
├── watchpoint.py            # Watchpoint runtime (independent)
└── CLAUDE.md                # Watchpoint runtime docs
```

Both are base64-encoded into sitecustomize.py by `DebugWithWatchpointAction.kt`.

