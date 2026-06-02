"""Python 3.12+ watchpoint implementation using sys.monitoring.

Performance design: zero overhead before any watch() call.
- No global monitoring events are registered at startup.
- watch("x") enables LINE + PY_RETURN events only for the specific code object
  that contains the watched frame (via set_local_events).
- watch("obj.attr") uses __class__ surgery – no LINE events at all.

A 1000-function codebase has exactly ONE function with LINE events active when
exactly one local-variable watch is set. Contrast with tracker_v312.py which
enables LINE events for every user-code function at startup.

Cross-version notes (3.12 / 3.13 / 3.14):
- PY_UNWIND cannot be enabled as a LOCAL event – only globally. To stay
  zero-overhead we never register PY_UNWIND. Instead we register PY_START
  locally on every watched code object: when a fresh frame enters, any
  watch / frame-state already keyed under id(new_frame) must belong to a
  dead frame (CPython aggressively reuses frame memory and frame ids), so
  we wipe it. Combined with lazy zombie-sweep in _on_line, this fully
  cleans up state from exception-unwound frames.
- frame.f_locals semantics differ (3.13 made it a fresh proxy each access);
  we always read once via `dict(frame.f_locals)` to snapshot consistently.
- Python 3.14 propagates exceptions raised from LINE callbacks past local
  try/except inside the monitored frame – they surface in the CALLER. Tests
  expecting WatchpointHit must therefore wrap the monitored code in a helper.
"""


import sys
import threading
import builtins

from . import constants
from .hit import WatchpointHit
from .registry import WatchpointRegistry, _setup_monitoring
from . import pydevd_pause
from .caller import _RUNTIME_FILENAMES, _is_runtime_filename

# WatchpointHit is defined in the leaf `hit` submodule, so its __module__
# would be '_pycharm_watchpoint.hit'. The IDE registers its exception
# breakpoint as '_pycharm_watchpoint.WatchpointHit', so rebrand it to the
# package name to keep that match working.
WatchpointHit.__module__ = __name__

# Re-export every submodule's package-internal surface onto the package
# namespace so `from _pycharm_watchpoint import <internal>` keeps working
# exactly as the old flat module did (the test-suite relies on this). The
# `constants` module is intentionally excluded: its mutable globals must be
# reached as `constants.X`, not copied here as stale snapshots.
from . import (hit, helpers, caller, pydevd_pause, watch_data,
               containers, classpatch, registry)
for _m in (hit, helpers, caller, pydevd_pause, watch_data,
           containers, classpatch, registry):
    for _k, _v in vars(_m).items():
        if not _k.startswith('__'):
            globals().setdefault(_k, _v)
del _m, _k, _v


# ---------------------------------------------------------------------------
# Module-level singleton and public API
# ---------------------------------------------------------------------------

_registry = WatchpointRegistry()
_setup_monitoring(_registry)

# Expose for plugin-side invocation and for conftest cleanup.
builtins._watchpoint_registry = _registry


def watch(expr: str, *, frame: Any = None) -> None:
    """Watch a variable or attribute for changes.

    When the value changes the debugger will pause via WatchpointHit exception.
    Supported forms:
    - "x"        – local variable in the caller's frame
    - "obj.attr" – attribute on an object in the caller's frame

    Args:
        expr:  Variable name or dotted attribute path to watch.
        frame: Explicit frame to resolve expr in. Defaults to caller's frame.
    """
    if frame is None:
        frame = sys._getframe(1)
    constants._installing_watch_thread = threading.get_ident()
    try:
        _registry.add_watch(expr, frame)
    finally:
        constants._installing_watch_thread = None


def unwatch(expr: str) -> None:
    """Remove the watchpoint for expr. Silently ignored if not watching expr."""
    _registry.remove_watch(expr)


def clear_watches() -> None:
    """Remove all active watchpoints."""
    _registry.clear_watches()


def _find_paused_user_frame(file_hint: str, func_hint: str) -> Any:
    """Locate the paused user frame matching `file_hint` + `func_hint`.

    Used by `watch_at` (PyCharm plugin entry point): when a watch is added
    via the IDE's "Add Python Watchpoint" action, the expression is evaluated
    by pydevd in a context that DOES NOT include the user's actual frame on
    its sys._getframe() stack – the eval frame's f_back leads back into
    pydevd, not into user code. We have to recover the user frame ourselves
    by scanning every running thread's top frame and walking up.

    Both endpath comparisons use `endswith` so the IDE-provided path can be
    absolute while the running interpreter sees a slightly different one
    (e.g. /private/var/... vs /var/... on macOS).
    """
    import threading
    target_basename = file_hint.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    candidates = []
    for tid, top_frame in sys._current_frames().items():
        f = top_frame
        while f is not None:
            cf = f.f_code.co_filename
            if f.f_code.co_name == func_hint and (
                cf == file_hint
                or cf.endswith(file_hint)
                or file_hint.endswith(cf)
                or cf.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] == target_basename
            ):
                candidates.append(f)
            f = f.f_back
    if not candidates:
        raise RuntimeError(
            f"watchpoint: could not locate paused frame for {func_hint}() in {file_hint}"
        )
    # If multiple matches (recursion), prefer the DEEPEST invocation. The IDE
    # evaluator gives us only file + function, and the frame where execution is
    # actually paused is normally the innermost matching recursive frame. Picking
    # the outer frame arms a watch that misses the selected frame's next change.
    candidates.sort(key=lambda fr: _frame_depth(fr), reverse=True)
    return candidates[0]


def _frame_depth(frame: Any) -> int:
    """Count frames from `frame` down to the bottom of its stack."""
    depth = 0
    f = frame
    while f is not None:
        depth += 1
        f = f.f_back
    return depth


def watch_at(expr: str, file_hint: str, func_hint: str) -> str:
    """Arm a watch on the user frame identified by (file_hint, func_hint).

    Returns a short diagnostic string so the IDE can show success vs failure
    in its evaluator-result UI without throwing into the user's debug session.
    """
    try:
        frame = _find_paused_user_frame(file_hint, func_hint)
    except Exception as e:
        return f"ERROR: {e}"
    try:
        constants._installing_watch_thread = threading.get_ident()
        try:
            _registry.add_watch(expr, frame)
        finally:
            constants._installing_watch_thread = None
    except Exception as e:
        return f"ERROR: add_watch failed: {e}"
    # Return the Python id() of the frame so the IDE can key the highlight to
    # this specific frame instance (not just the variable name). The Kotlin side
    # parses this as a Long and stores it alongside the expression in
    # WatchpointMarkerService, which the tree-cell renderer then uses to avoid
    # decorating same-named variables in unrelated frames.
    return str(id(frame))


def _pycharm_consume_last_hit(pause_file: Optional[str] = None,
                              pause_line: Optional[int] = None) -> str:
    """Drain watchpoint hits matching the IDE's current pause location.

    `pause_file` / `pause_line` (the user thread's topmost stack frame at
    the moment of pause): if both provided, only hits whose installed bp
    matches `(pause_file, pause_line)` are drained and returned – the
    rest stay queued for their own future pauses. If either is None
    (or both omitted, the legacy signature), every queued hit is drained
    – used at session end / `clear_watches` and as the fallback when the
    plugin cannot read the pause location from `XDebugSession`.

    Empty string ⇒ no matching hits (typical when the pause was triggered
    by a regular breakpoint or by a non-matching watchpoint bp). Otherwise
    the result is one OR MORE base64-encoded hit payloads separated by `;`.
    Each payload decodes to UTF-8 of five `\\x00`-separated fields:
    file, line, name, old, new.

    Why selective drain: with sequential pre-emptive bps, N back-to-back
    mutations install N bps at N distinct lines and queue N hits. The IDE
    will pause N times, once per bp. If the drain returned ALL queued
    hits on the first pause, the user would see N highlights at once
    (the original "two yellow lines at query.py:289 and :290" symptom)
    and resume with an empty queue, missing the remaining N-1 pauses.
    Filtering by `(pause_file, pause_line)` gives each pause its own
    single matching hit and leaves the rest for next time.

    Why `;` as the separator: it is NOT in the base64 alphabet
    (A–Z, a–z, 0–9, +, /, =), so a simple split is unambiguous on the
    Kotlin side.

    Why base64 + NUL-separated payload (per hit) instead of JSON: pydevd
    renders the evaluator result via repr(), which means any string value
    would arrive on the Kotlin side wrapped in Python-style quoting with
    backslash-escaped specials. Base64'ing each payload reduces decoding to
    "strip outer quotes, split on `;`, base64-decode each" – no JSON parser,
    no escape-sequence handling.
    """
    reg = _registry
    selective = pause_file is not None and pause_line is not None
    if selective:
        try:
            pause_line_int = int(pause_line)
        except (TypeError, ValueError):
            pause_line_int = pause_line
    else:
        pause_line_int = None

    with reg._lock:
        if selective:
            # A hit matches when ANY of its installed bps is at the
            # IDE's current pause location. Each hit has up to two
            # bps (primary at mutation site + safety at walked-up
            # user code – see `_compute_bp_targets`); whichever
            # fires first triggers the pause, and we drain the
            # whole hit on the first match. Hits with empty
            # `bp_locations` (do_wait_suspend fallback path) match
            # ANY pause so they don't leak forever – their pause
            # was triggered directly by `do_wait_suspend`, and the
            # next consume drains them.
            def matches(h: dict) -> bool:
                locs = h.get("bp_locations", [])
                if not locs:
                    return True
                return any(
                    loc[0] == pause_file and loc[1] == pause_line_int
                    for loc in locs
                )

            hits = [h for h in reg._hit_queue if matches(h)]
            reg._hit_queue = [h for h in reg._hit_queue if not matches(h)]
            # Collect every (file, line) belonging to a drained hit and
            # remove its bps – this includes the SAFETY bp at the walked-
            # up user code even when the PRIMARY bp at the mutation site
            # is what just fired (and vice versa). Otherwise the unfired
            # sibling bp would surface as a phantom pause later when
            # execution naturally passes its line.
            drained_locs: set = set()
            for h in hits:
                for loc in h.get("bp_locations", []):
                    drained_locs.add((loc[0], loc[1]))
                    # Clean _bp_pause_pending for drained hits so our
                    # _on_line callback doesn't double-fire after pydevd
                    # already handled the pause. loc[2] is the code object.
                    if len(loc) > 2 and loc[2] is not None:
                        reg._bp_pause_pending.pop(
                            (id(loc[2]), loc[1]), None
                        )
            bps_to_remove = [
                t for t in reg._temp_breakpoints
                if (t[0], t[1]) in drained_locs
            ]
            reg._temp_breakpoints = [
                t for t in reg._temp_breakpoints
                if (t[0], t[1]) not in drained_locs
            ]
        else:
            # Legacy drain-everything path. Used at session end and
            # as the fallback when the IDE-side caller can't supply
            # a pause location.
            hits = list(reg._hit_queue)
            reg._hit_queue.clear()
            bps_to_remove = list(reg._temp_breakpoints)
            reg._temp_breakpoints.clear()
            reg._bp_pause_pending.clear()
        if hits:
            reg._last_hit = None

    # Remove any temp breakpoints we're cleaning up. The pydevd-side
    # `consolidate_breakpoints` may take its own lock, so we make this
    # call OUTSIDE `reg._lock` to avoid lock-order inversion.
    if bps_to_remove:
        py_db = pydevd_pause._get_pydevd_debugger()
        if py_db is not None:
            pydevd_pause._remove_temp_breakpoints(py_db, bps_to_remove)

    if not hits:
        return ""
    import base64 as _b64
    encoded_hits = []
    for hit in hits:
        parts = [
            hit["file"],
            str(hit["line"]),
            hit["name"],
            hit["old"],
            hit["new"],
            hit.get("caller_file", ""),
            str(hit.get("caller_line", 0)),
        ]
        raw = "\x00".join(parts).encode("utf-8")
        encoded_hits.append(_b64.b64encode(raw).decode("ascii"))
    return ";".join(encoded_hits)


# Soft cap on the number of (name, frame_id) pairs `_pycharm_locate_watches`
# returns, so a pathological stack (deep recursion holding a watched object in
# every frame) can't produce an unbounded payload. Far above any realistic
# count of frames that legitimately reference a watched object.
_MAX_LOCATE_PAIRS = 5000


def _pycharm_locate_watches() -> str:
    """Report every (variable name, frame id) pair where an armed watch is
    live RIGHT NOW, across all threads' stack frames.

    The IDE plugin calls this on each `sessionPaused` to drive the
    Variables-panel watch icon across the whole call stack. The result is
    authoritative and frame-scoped, so the icon follows the watched OBJECT
    (by identity) into caller and callee frames without ever lighting up an
    unrelated, same-named variable – the failure mode of the old name-only
    fallback (the "ghost icon" bug).

    Two sources of (name, frame_id):
      1. Every live `_local_watches` key – the armed frame plus any callee
         frames a watch propagated INTO (design contract §8). These are
         auto-pruned on frame exit, so the set is always live.
      2. Identity scan: for each watched OBJECT (the `_AttributeWatch`
         referents – bare-object and dotted-attribute watches), walk every
         live frame and emit `(local_name, id(frame))` for any local bound to
         that exact object by `id()`. This is what carries the icon into
         caller frames the watch never propagated into – e.g. a `request`
         held by a middleware frame above the one the user armed in.

    Side-effect free: it only reads `id()` and iterates `f_locals` – it never
    touches a watched object's attributes, so no watcher `__setattr__` /
    `__getattribute__` override can fire during the scan.

    Encoding mirrors `_pycharm_consume_last_hit`: base64 of UTF-8, records
    separated by `\\x01`, each record `name\\x00frameid`. Empty string ⇒ no
    live watches (authoritative – the plugin may safely clear its cross-frame
    set). An `ERROR:`-prefixed string signals an internal failure so the
    plugin can leave its existing state untouched rather than wipe it on a
    transient hiccup.
    """
    try:
        reg = _registry
        pairs: set = set()          # (name, frame_id) to emit
        watched_ids: dict = {}      # id(obj) -> obj, the watched objects

        # Snapshot registry state under the lock; release before the frame
        # scan so we don't hold `_lock` across a potentially deep stack walk
        # (the scan touches no registry state, so it needs no lock).
        with reg._lock:
            for (name, fid) in reg._local_watches.keys():
                pairs.add((name, int(fid)))
            for aw in list(reg._attr_watches.values()):
                ref = getattr(aw, "_obj_ref", None)
                if ref is None:
                    continue
                try:
                    obj = ref()
                except Exception:
                    obj = None
                if obj is not None:
                    watched_ids[id(obj)] = obj

        # Identity scan: find every live local bound to a watched object.
        if watched_ids:
            try:
                frames = list(sys._current_frames().values())
            except Exception:
                frames = []
            for top in frames:
                if len(pairs) >= _MAX_LOCATE_PAIRS:
                    break
                f = top
                hops = _MAX_FRAME_WALK_HOPS
                while f is not None and hops > 0:
                    hops -= 1
                    try:
                        co_filename = f.f_code.co_filename
                    except Exception:
                        co_filename = ""
                    # Skip our own runtime frames (the eval shim, <string>).
                    if not _is_runtime_filename(co_filename):
                        try:
                            local_items = list(f.f_locals.items())
                        except Exception:
                            local_items = []
                        fid = id(f)
                        for nm, val in local_items:
                            try:
                                if id(val) in watched_ids:
                                    pairs.add((nm, fid))
                            except Exception:
                                continue
                        if len(pairs) >= _MAX_LOCATE_PAIRS:
                            break
                    f = f.f_back

        if not pairs:
            return ""
        import base64 as _b64
        records = ["%s\x00%d" % (nm, fid) for (nm, fid) in pairs]
        raw = "\x01".join(records).encode("utf-8")
        return _b64.b64encode(raw).decode("ascii")
    except Exception as e:
        return "ERROR: locate_watches failed: %s" % (e,)


# Expose via builtins so PyCharm plugin can call them without importing.
builtins._pycharm_watch = watch
builtins._pycharm_watch_at = watch_at
builtins._pycharm_unwatch = unwatch
builtins._pycharm_clear_watches = clear_watches
builtins._pycharm_watchpoint_diag = _pycharm_watchpoint_diag
builtins._pycharm_consume_last_hit = _pycharm_consume_last_hit
builtins._pycharm_locate_watches = _pycharm_locate_watches


# Fingerprint line: when the user reports "I rebuilt and it still doesn't
# work", this is the first thing we check in /tmp/pythonwatchpoint.log –
# if the version stamp matches the latest fix, the rebuild took. If it's
# missing or stale, gradle didn't pick up the resource change.
# Also surfaces `_STDLIB_DIR_PREFIX` so we can confirm the stdlib filter
# is using the right path for THIS Python install (uv-managed Pythons
# sit under custom roots that we want to confirm are detected).
_log_warn("runtime loaded", always=True)
