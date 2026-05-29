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
import os
import sys
import builtins
import threading
from typing import Any, Optional

if sys.version_info < (3, 12):
    raise RuntimeError("watchpoint.py requires Python 3.12+.")

_monitoring = sys.monitoring


# ---------------------------------------------------------------------------
# Debug logging – gated, so it never spams pydevd users
# ---------------------------------------------------------------------------

# Active when the user sets PYCHARM_WATCHPOINT_DEBUG=1 OR debugpy is in sys.modules
# (debugpy support is in beta; users on that path get verbose tracing for free so
# we can diagnose failures from their stderr without asking them to re-run with a flag).
def _dbg_enabled() -> bool:
    """True when we should emit `[WATCHPOINT/dbg]` traces to stderr.

    Cheap to call from hot paths because each branch short-circuits. Re-evaluated
    every call rather than cached so toggling the env var between runs Just Works.
    """
    if os.environ.get("PYCHARM_WATCHPOINT_DEBUG") == "1":
        return True
    return "debugpy" in sys.modules


def _dbg_log(msg: str) -> None:
    """Emit a single-line debug trace. Silent unless _dbg_enabled()."""
    if _dbg_enabled():
        try:
            print(f"[WATCHPOINT/dbg] {msg}", file=sys.stderr, flush=True)
        except Exception:  # noqa: BLE001
            pass  # stderr could be closed during interpreter shutdown – never raise from a trace.


# ---------------------------------------------------------------------------
# WatchpointHit exception
# ---------------------------------------------------------------------------

class WatchpointHit(Exception):
    """Raised when a watched variable or attribute changes value.

    Attributes:
        watch_name:  The expression string passed to watch() (e.g. 'x' or 'obj.val').
        old_value:   repr() of the value before the change.
        new_value:   repr() of the value after the change.
        source_file: Absolute path of the file where the change occurred.
        source_line: Line number of the statement that performed the change.
    """

    def __init__(self, watch_name: str, old_value: str, new_value: str,
                 source_file: str, source_line: int) -> None:
        self.watch_name = watch_name
        self.old_value = old_value
        self.new_value = new_value
        self.source_file = source_file
        self.source_line = source_line
        super().__init__(
            f"Watchpoint: '{watch_name}' changed from {old_value} to {new_value} "
            f"at {source_file}:{source_line}"
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class WatchpointRegistry:
    """Tracks active local-variable and attribute watchpoints.

    Lifetime model for LOCAL watches:
    A `watch("x")` call binds the watch to the SPECIFIC frame it was called
    from (via id(frame)). The watch dies when that frame exits, by either:
    - PY_RETURN: explicit cleanup in _on_py_return; OR
    - Exception unwind: lazy cleanup the next time a LINE event fires for
      the same code object under a DIFFERENT frame id (the old fid's frame
      is dead because you can only be in one frame of a given code at a
      time per thread – recursion produces distinct fids).

    This frame-scoping prevents leftover state from a returned invocation
    from spuriously firing on a future call to the same function.

    Thread safety: a simple RLock guards mutation of both watch tables.
    The monitoring callbacks themselves hold no locks (they run under the
    GIL during instrumentation dispatch).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()

        # Local-variable watches: {(name, frame_id): LocalWatch}. Keying by
        # (name, frame_id) supports recursion – the same name watched from
        # two simultaneously-live frames of the same code is allowed.
        self._local_watches: dict[tuple, "_LocalWatch"] = {}

        # Attribute watches: {expr: AttributeWatch}. Single watch per expr;
        # __class__ surgery is global to the object so re-watching is replace.
        self._attr_watches: dict[str, "_AttributeWatch"] = {}

        # Per-frame diff state for the LINE callback:
        #   {frame_id: {"code": code, "prev_line": int|None,
        #               "prev_hashes": {name: hash}, "prev_reprs": {name: repr}}}
        self._frame_state: dict[int, dict] = {}

        # Per-thread re-entrancy guard; LINE callbacks can otherwise recurse
        # if `eval()` (used during watch setup) itself triggers instrumentation.
        self._guard = threading.local()

        # Per-thread queue of pending watch propagations into callees. A CALL
        # event from a watching frame pushes (callee_code, {id(value): name})
        # entries; the matching PY_START in the callee pops and arms a watch
        # on every parameter whose value matches a watched id. See _on_call /
        # _on_py_start for the full contract. Used as a stack so re-entrant
        # calls nest correctly; mismatched entries (e.g. CALL whose PY_START
        # never fires because the callable was a C function that raised) are
        # popped lazily when a later PY_START finds the matching code object
        # further down the stack.
        self._pending_propagation = threading.local()

        # Most recent watchpoint hit, set by `_handle_hit` before the pause is
        # arranged. The IDE plugin reads (and clears) this via
        # `_pycharm_consume_last_hit` on each `sessionPaused` event to power
        # the "this line is why you paused" line highlight. None when there is
        # no pending hit – also reset by `clear_watches` so a fresh session
        # doesn't inherit a stale entry.
        self._last_hit: Optional[dict] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_watch(self, expr: str, frame: Any) -> None:
        """Register a watchpoint for expr, captured from the given frame.

        For plain names ('x'): bind to frame.f_code and enable LINE+PY_RETURN
        local events on that code object.
        For dotted paths ('obj.attr'): install __class__ surgery on obj.
        """
        with self._lock:
            if "." in expr:
                if expr in self._attr_watches:
                    self._remove_attr_watch_locked(expr)
                self._add_attr_watch(expr, frame)
                return

            # Plain name. Decide: is the resolved value a user-defined
            # object that supports __class__ surgery? If so, install an
            # object-wide attribute watch so ANY attribute mutation fires.
            # Otherwise fall back to local-variable rebinding detection.
            try:
                value = eval(expr, frame.f_globals, frame.f_locals)
            except Exception:
                value = None

            if _is_object_watchable(value):
                if expr in self._attr_watches:
                    self._remove_attr_watch_locked(expr)
                try:
                    self._add_object_watch(expr, value)
                    return
                except TypeError:
                    # Class-surgery refused (Django Model metaclass,
                    # exotic builtin instance, frozen dataclass, ...).
                    # Try classpatch wildcard: every attribute write on
                    # THIS instance fires under the bare-name watch. If
                    # even classpatch is blocked, fall through to
                    # local-variable rebind detection.
                    if self._try_classpatch_object_watch(expr, value):
                        return

            fid = id(frame)
            # Same name + same frame instance ⇒ replace baseline.
            if (expr, fid) in self._local_watches:
                self._remove_local_watch_locked((expr, fid))
            self._add_local_watch(expr, frame)

    def remove_watch(self, expr: str) -> None:
        """Remove all watches for expr (across any frames or as an attr watch)."""
        with self._lock:
            if expr in self._attr_watches:
                self._remove_attr_watch_locked(expr)
            stale_keys = [k for k in self._local_watches if k[0] == expr]
            for k in stale_keys:
                self._remove_local_watch_locked(k)

    def clear_watches(self) -> None:
        """Remove all active watchpoints."""
        with self._lock:
            for k in list(self._local_watches):
                self._remove_local_watch_locked(k)
            for expr in list(self._attr_watches):
                self._remove_attr_watch_locked(expr)
            self._frame_state.clear()
            # Drop any pending hit so a follow-up `consume_last_hit` doesn't
            # surface a hit that belongs to a now-removed watch.
            self._last_hit = None
        # Drop the current thread's propagation queue too – between test
        # cases (conftest calls clear_watches) any leftover CALL entries
        # would otherwise survive and arm spurious watches in later tests.
        # threading.local attributes are per-thread; we can only clear ours.
        queue = getattr(self._pending_propagation, "queue", None)
        if queue is not None:
            queue.clear()

    # ------------------------------------------------------------------
    # Local-variable watch internals
    # ------------------------------------------------------------------

    def _add_local_watch(self, name: str, frame: Any,
                         display_name: Optional[str] = None) -> None:
        """Install a local-variable watch for `name` in `frame`.

        `display_name` overrides the watch's user-visible name for the
        emitted `WatchpointHit`. Used by cross-function propagation so a
        propagated watch on the callee's parameter `d` still reports as the
        caller's original `data` watch (see `_apply_propagation`).
        """
        code = frame.f_code
        fid = id(frame)
        try:
            initial_val = eval(name, frame.f_globals, frame.f_locals)
        except Exception:
            initial_val = None

        initial_hash = _value_hash(initial_val)
        initial_repr = _safe_repr(initial_val)
        watch = _LocalWatch(
            name=name,
            code=code,
            frame_id=fid,
            initial_hash=initial_hash,
            initial_repr=initial_repr,
            display_name=display_name,
        )
        self._local_watches[(name, fid)] = watch

        # Initialise / merge frame state so the next LINE event has a baseline.
        # Leaving prev_line=None ensures the very next LINE event won't fire
        # (it would have no real previous-line attribution).
        state = self._frame_state.get(fid)
        if state is None or state.get("code") is not code:
            state = {
                "code": code,
                "prev_line": None,
                "prev_hashes": {},
                "prev_reprs": {},
            }
            self._frame_state[fid] = state
        state["prev_hashes"][name] = initial_hash
        state["prev_reprs"][name] = initial_repr

        # Enable LINE + PY_RETURN + PY_START + CALL local events for this
        # code object. PY_START is needed so a future re-entry of the same
        # code wipes any stale watch/state that happens to share id() with
        # the new frame. CALL is needed so the watch propagates into any
        # function called from this frame that receives the watched value
        # as an argument (see _on_call / cross-function watching).
        _monitoring.set_local_events(
            _TOOL_ID, code,
            _monitoring.events.LINE
            | _monitoring.events.PY_RETURN
            | _monitoring.events.PY_START
            | _monitoring.events.CALL
        )

    # ------------------------------------------------------------------
    # Attribute watch internals (__class__ surgery)
    # ------------------------------------------------------------------

    def _add_attr_watch(self, expr: str, frame: Any) -> None:
        """Install an attribute watch for 'obj.attr' expression.

        Splits expr into obj_path + attr_name, evaluates obj in the frame,
        then swaps obj's class for a dynamically-created subclass whose
        __setattr__ raises WatchpointHit when attr_name is assigned.
        """
        dot_idx = expr.rfind(".")
        obj_path = expr[:dot_idx]
        attr_name = expr[dot_idx + 1:]

        try:
            obj = eval(obj_path, frame.f_globals, frame.f_locals)
        except Exception as e:
            raise ValueError(f"Cannot resolve '{obj_path}' in current frame: {e}") from e

        original_cls = type(obj)
        try:
            initial_val = getattr(obj, attr_name)
        except AttributeError:
            initial_val = None

        # Build a subclass with a __setattr__ that intercepts the watched attr.
        # Closure captures expr, attr_name, original class, and registry so the
        # custom setattr can route the change through the same _handle_hit
        # pipeline used by LINE-callback detections (pause-via-pydevd or raise).
        _expr = expr
        _attr = attr_name
        _orig_cls = original_cls
        _registry_self = self

        # The `class _WatchedSubclass(_orig_cls):` statement triggers
        # `_orig_cls`'s metaclass — and some metaclasses (Django's ModelBase,
        # SQLAlchemy's DeclarativeMeta, etc.) refuse to build a subclass
        # outside of their own registration flow. That refusal is raised
        # *here*, not at the later `obj.__class__ = ...` swap, so wrap the
        # class statement itself in try/except.
        try:
            class _WatchedSubclass(_orig_cls):
                # __slots__ = () keeps the instance layout identical to the
                # original class: if the parent uses __slots__ to forbid a
                # __dict__, our subclass must do the same or __class__ swap
                # raises "layout differs" TypeError. For classes that already
                # have a __dict__ (the common case) this is a no-op.
                __slots__ = ()
                __qualname__ = f"_Watched_{_orig_cls.__name__}"

                def __setattr__(self, name: str, value: Any) -> None:  # noqa: N805
                    if name != _attr:
                        super().__setattr__(name, value)
                        return
                    # Re-entrancy guard – see object-watch __setattr__ for rationale.
                    if getattr(_registry_self._guard, "active", False):
                        super().__setattr__(name, value)
                        return
                    try:
                        old_val = getattr(self, _attr)
                    except AttributeError:
                        old_val = None
                    # If the incoming value is a mutable builtin container, wrap
                    # it BEFORE storing so subsequent `.append` / `[k]=v` / etc.
                    # fire too. Without this, `obj.attr = []; obj.attr.append(x)`
                    # would lose the watch on the first reassignment. The wrap
                    # is idempotent for already-wrapped values.
                    if isinstance(value, _CONTAINER_TYPES) and not isinstance(value, _WATCHED_CONTAINER_TYPES):
                        value = _wrap_container(value, _registry_self, _expr)
                        # Keep the registry's _AttributeWatch in sync so unwatch
                        # restores the *current* (newly wrapped) attribute and
                        # not a stale wrapper from an earlier assignment.
                        aw = _registry_self._attr_watches.get(_expr)
                        if aw is not None:
                            aw.container_wrapper = value
                    if _value_hash(old_val) != _value_hash(value):
                        super().__setattr__(name, value)
                        _registry_self._guard.active = True
                        try:
                            caller = sys._getframe(1)
                            _registry_self._handle_hit(
                                user_frame=caller,
                                watch_name=_expr,
                                old_repr=_safe_repr(old_val),
                                new_repr=_safe_repr(value),
                                source_file=caller.f_code.co_filename,
                                source_line=caller.f_lineno,
                            )
                        finally:
                            _registry_self._guard.active = False
                    else:
                        super().__setattr__(name, value)
        except Exception as e:  # noqa: BLE001
            _log_warn(
                f"Cannot build watcher subclass for {original_cls.__name__} "
                f"(watching '{expr}'): metaclass refused the subclass ({e!r}). "
                f"Common causes: Django Model, SQLAlchemy declarative base, "
                f"any framework whose metaclass demands app/registry "
                f"membership. Falling back to class-level __setattr__ "
                f"monkey-patching scoped to this instance."
            )
            if self._try_classpatch_attr_watch(expr, obj, attr_name, initial_val):
                return
            raise TypeError(
                f"Cannot watch '{expr}': its type ({original_cls.__name__}) "
                f"has a metaclass that refuses dynamic subclassing, AND "
                f"class-level monkey-patching of __setattr__ was also "
                f"blocked. ({e})"
            ) from e

        # Class surgery: swap the instance's class to our watcher subclass.
        try:
            obj.__class__ = _WatchedSubclass
        except TypeError as e:
            raise TypeError(
                f"Cannot watch attribute '{expr}': class surgery failed for "
                f"{original_cls.__name__}. Make sure it's a user-defined class. ({e})"
            ) from e

        watch = _AttributeWatch(
            expr=expr,
            obj_ref=obj,
            original_cls=original_cls,
            watcher_cls=_WatchedSubclass,
            initial_repr=_safe_repr(initial_val),
        )
        self._attr_watches[expr] = watch

        # If the leaf attribute holds a mutable builtin container, wrap it
        # so the user's `obj.attr.append(...)` / `obj.attr[k] = v` etc.
        # also fire the watch. We do this AFTER registering the watch so
        # the wrapper's `_wp_fire` finds `_attr_watches[expr]` and doesn't
        # treat itself as an orphaned wrapper. The replacement goes through
        # the rebind-detector with the guard up so it doesn't fire on our
        # own assignment.
        if isinstance(initial_val, _CONTAINER_TYPES) and not isinstance(initial_val, _WATCHED_CONTAINER_TYPES):
            wrapped = _wrap_container(initial_val, self, expr)
            self._guard.active = True
            try:
                setattr(obj, attr_name, wrapped)
            except Exception:
                # If the parent object rejected the assignment (read-only
                # property, slotted class without this attr, etc.), back
                # out cleanly – the rebind-detector still works.
                wrapped = None
            finally:
                self._guard.active = False
            if wrapped is not None:
                watch.container_wrapper = wrapped
                watch.container_holder = obj
                watch.container_attr = attr_name

    def _try_classpatch_attr_watch(self, expr: str, obj: Any, attr_name: str,
                                   initial_val: Any) -> bool:
        """Install a classpatch watch for `obj.attr_name` (registered under `expr`).

        Fallback used by `_add_attr_watch` when the parent class's metaclass
        refuses dynamic subclassing. The watch fires when the SPECIFIC
        attribute is rebound on THIS instance (other instances of the same
        class pass through). If the leaf is a mutable container we
        additionally wrap-and-replace so in-place mutations also fire
        (same trade-off as the class-surgery path).

        Returns True on success; False if even patching the class's
        `__setattr__` was blocked – the caller should raise a clean
        TypeError so the user knows neither strategy worked.
        """
        watch = _AttributeWatch(
            expr=expr, obj_ref=obj, original_cls=None, watcher_cls=None,
            initial_repr=_safe_repr(initial_val),
        )
        watch.classpatch_key = attr_name
        if not _install_classpatch_attr_watch(self, obj, attr_name, watch):
            return False
        self._attr_watches[expr] = watch

        # Container wrap at the leaf. The wrap setattr goes through our
        # newly-installed patched __setattr__; the guard is up so it doesn't
        # fire as a spurious initial hit.
        if (isinstance(initial_val, _CONTAINER_TYPES)
                and not isinstance(initial_val, _WATCHED_CONTAINER_TYPES)):
            wrapped = _wrap_container(initial_val, self, expr)
            self._guard.active = True
            try:
                setattr(obj, attr_name, wrapped)
            except Exception:
                wrapped = None
            finally:
                self._guard.active = False
            if wrapped is not None:
                watch.container_wrapper = wrapped
                watch.container_holder = obj
                watch.container_attr = attr_name
        return True

    def _add_object_watch(self, expr: str, obj: Any) -> None:
        """Install an object-wide watch on `obj`, recursing into nested
        user-defined attributes + wrapping nested mutable containers.

        Used when `watch('name')` resolves to a user-defined object: rather
        than tracking the name's rebinding (which usually doesn't happen
        for objects like a Flask request or a DTO), we instrument the
        object so any attribute mutation – including nested ones reached
        via `obj.a.b.c = ...` or `obj.a.bs.append(...)` – fires.

        Recursion walks `__dict__` to depth `_RECURSIVE_OBJECT_WATCH_DEPTH`,
        cycle-guarded by an id-visited set. Containers (list/dict/set) are
        wrapped in place (see `_wrap_container`). New attributes assigned
        at runtime are recursively instrumented by the watcher's
        `__setattr__` hook.

        Lives until `unwatch(expr)` is called or the registry is cleared;
        the object (and the whole sub-tree we walked) is kept alive by the
        registry's hard references via the root `_AttributeWatch.obj_ref`
        and `_AttributeWatch.sub_watches`.
        """
        root_watch = self._install_single_object_watch(expr, obj, root_expr=expr)
        self._attr_watches[expr] = root_watch
        # Snapshot of ids we've already instrumented. Shared across the
        # recursion so a graph like `a.left = a.right` (same object reached
        # via two paths) gets instrumented exactly once.
        visited = {id(obj)}
        self._instrument_object_tree(
            obj, root_expr=expr, current_path=expr,
            depth=1, visited=visited, root_watch=root_watch,
        )

    def _install_single_object_watch(self, expr: str, obj: Any,
                                     root_expr: str) -> "_AttributeWatch":
        """Apply class surgery on `obj` so any attribute assignment fires.

        `expr` is the dotted path from root to THIS object (used in the
        `watch_name` field of the hit). `root_expr` is the top-level user
        expression (used when this object's `__setattr__` later needs to
        recursively instrument a freshly-assigned nested value – it has to
        know which root's `sub_watches` to record into).

        Returns an `_AttributeWatch` describing the installed surgery, NOT
        registered in `_attr_watches`. The caller is responsible for either
        registering (for the top-level call from `_add_object_watch`) or
        appending to `root_watch.sub_watches` (for recursive sub-watches).

        Raises `TypeError` if `__class__` swap isn't allowed (frozen
        dataclasses, exotic layouts) – `add_watch` catches and falls back.
        """
        original_cls = type(obj)
        # `_make_any_attr_watcher_class` runs `class _WatchedAnyAttrSubclass(original_cls):`
        # which invokes the original class's metaclass. Django's ModelBase
        # and SQLAlchemy's DeclarativeMeta refuse the subclass (they expect
        # app_label / registry membership) and raise RuntimeError. Catch
        # that here and convert to TypeError so `add_watch`'s
        # `_is_object_watchable` branch can fall back to local-variable.
        try:
            watcher_cls = self._make_any_attr_watcher_class(
                original_cls, expr=expr, root_expr=root_expr,
            )
        except Exception as e:  # noqa: BLE001
            _log_warn(
                f"Cannot build watcher subclass for {original_cls.__name__} "
                f"(watching '{expr}'): metaclass refused the subclass ({e!r}). "
                f"Common causes: Django Model, SQLAlchemy declarative base, "
                f"or any framework whose metaclass demands app/registry "
                f"membership. Falling back to local-variable rebind detection."
            )
            raise TypeError(
                f"Cannot watch '{expr}': type {original_cls.__name__} has a "
                f"metaclass that refuses dynamic subclassing. ({e})"
            ) from e
        try:
            obj.__class__ = watcher_cls
        except (TypeError, AttributeError) as e:
            # Frozen dataclasses raise `FrozenInstanceError` (an
            # AttributeError subclass) from their custom __setattr__; some
            # exotic classes raise plain TypeError if their layout forbids
            # the swap. Convert either into a TypeError that `add_watch`
            # catches and falls back to local-variable detection.
            raise TypeError(
                f"Cannot watch object '{expr}': __class__ surgery failed on "
                f"{original_cls.__name__} ({e})."
            ) from e

        return _AttributeWatch(
            expr=expr,
            obj_ref=obj,
            original_cls=original_cls,
            watcher_cls=watcher_cls,
            initial_repr=_safe_repr(obj),
        )

    def _try_classpatch_object_watch(self, expr: str, obj: Any) -> bool:
        """Install a wildcard classpatch watch on `obj` for bare-name `watch(expr)`.

        Fallback used by `add_watch` when `_add_object_watch` raises because
        the class's metaclass refuses dynamic subclassing (e.g. Django
        Model). Registers a `'__any__'` key in the classpatch table so any
        attribute write on THIS instance fires under `expr`, with the hit's
        `watch_name` extended to `f"{expr}.{attr_name}"` to surface which
        attribute changed.

        Recursion into nested attrs is NOT performed by this fallback. The
        class-surgery path's `_instrument_object_tree` walks `__dict__` to
        wrap containers and instrument nested user-defined objects; the
        classpatch fallback only intercepts top-level assignments. Bare-name
        watch on a Django model therefore catches `obj.field = ...` rebinds
        of every field but NOT `obj.somelist.append(x)` in-place mutations
        (which never trigger `__setattr__`). Users who need that should
        watch the specific dotted path so the leaf gets a container wrap.

        Returns True on success; False if even patching the class's
        `__setattr__` was blocked – the caller falls through to local-
        variable rebind detection.
        """
        watch = _AttributeWatch(
            expr=expr, obj_ref=obj, original_cls=None, watcher_cls=None,
            initial_repr=_safe_repr(obj),
        )
        watch.classpatch_key = "__any__"
        if not _install_classpatch_attr_watch(self, obj, "__any__", watch):
            return False
        self._attr_watches[expr] = watch
        return True

    def _make_any_attr_watcher_class(self, original_cls: type, expr: str,
                                     root_expr: str) -> type:
        """Construct a `_WatchedAnyAttrSubclass(original_cls)` whose
        `__setattr__` fires `_handle_hit`, wraps newly-assigned containers,
        and recursively instruments newly-assigned user-defined objects.

        Closure captures:
        - `_expr`: this object's path (the watch_name template; firing
          emits `f"{_expr}.{name}"`).
        - `_root_expr`: the user's top-level watched expression (used to
          look up the root `_AttributeWatch` in `_attr_watches` when this
          watcher needs to record a sub-watch from `__setattr__`).
        - `_registry_self`: the registry singleton (for the guard,
          `_handle_hit`, and `_instrument_object_tree`).
        """
        _expr = expr
        _root_expr = root_expr
        _orig_cls = original_cls
        _registry_self = self

        class _WatchedAnyAttrSubclass(_orig_cls):
            # See `_WatchedSubclass` for why `__slots__ = ()` – preserves
            # the parent's layout so `__class__` swap is allowed on
            # slotted classes too.
            #
            # Note: the `class` statement here invokes `_orig_cls`'s
            # metaclass; some metaclasses (Django's ModelBase, SQLAlchemy's
            # DeclarativeMeta) refuse a dynamic subclass and raise from
            # *this line*. The caller `_install_single_object_watch`
            # wraps the call to `_make_any_attr_watcher_class` in
            # try/except for that reason.
            __slots__ = ()
            __qualname__ = f"_WatchedAny_{_orig_cls.__name__}"

            def __setattr__(self, name: str, value: Any) -> None:  # noqa: N805
                # Re-entrancy guard. While suspended in pydevd, the debug
                # protocol code may end up touching attributes of THIS
                # object (e.g. to read its repr for the IDE's Variables
                # panel). If that touch is a write, recursing into
                # `_handle_hit` would deadlock or compound-pause. The
                # guard is per-thread because pydevd's protocol work
                # happens on the same thread we suspended; we lift the
                # suppression as soon as we leave the watch path.
                if getattr(_registry_self._guard, "active", False):
                    super().__setattr__(name, value)
                    return
                try:
                    old_val = getattr(self, name)
                except AttributeError:
                    old_val = None
                sub_expr = f"{_expr}.{name}"
                # If the incoming value is a mutable container, wrap it
                # so subsequent `.append` / `[k]=v` / etc. on the new
                # attribute fire under the same root watch.
                if isinstance(value, _CONTAINER_TYPES) and not isinstance(value, _WATCHED_CONTAINER_TYPES):
                    wrapped_value = _wrap_container(value, _registry_self, sub_expr)
                else:
                    wrapped_value = value
                if _value_hash(old_val) != _value_hash(wrapped_value):
                    # Apply the assignment first so the IDE shows the
                    # post-change state when it pauses (and so the user
                    # observes the value they just set if pydevd isn't
                    # available).
                    super().__setattr__(name, wrapped_value)
                    _registry_self._guard.active = True
                    try:
                        # Track the wrap for cleanup (if we wrapped) AND
                        # recursively instrument the new value's subtree
                        # if it's a user-defined object. Both happen under
                        # the guard so any setattrs we trigger are silent.
                        root_watch = _registry_self._attr_watches.get(_root_expr)
                        if root_watch is not None:
                            if wrapped_value is not value:
                                # We wrapped a container into wrapped_value.
                                sub_w = _AttributeWatch(
                                    expr=sub_expr,
                                    obj_ref=None,
                                    original_cls=None,
                                    watcher_cls=None,
                                    initial_repr=_safe_repr(value),
                                )
                                sub_w.container_wrapper = wrapped_value
                                sub_w.container_holder = self
                                sub_w.container_attr = name
                                root_watch.sub_watches.append(sub_w)
                            elif _is_object_watchable(wrapped_value):
                                try:
                                    sub_w = _registry_self._install_single_object_watch(
                                        sub_expr, wrapped_value, root_expr=_root_expr,
                                    )
                                    root_watch.sub_watches.append(sub_w)
                                    _registry_self._instrument_object_tree(
                                        wrapped_value, root_expr=_root_expr,
                                        current_path=sub_expr, depth=1,
                                        visited={id(wrapped_value)},
                                        root_watch=root_watch,
                                    )
                                except TypeError:
                                    # Frozen / slotted / etc. – skip
                                    # auto-instrumentation but the rebind
                                    # already fired so the user knows.
                                    pass
                        caller = sys._getframe(1)
                        _registry_self._handle_hit(
                            user_frame=caller,
                            watch_name=sub_expr,
                            old_repr=_safe_repr(old_val),
                            new_repr=_safe_repr(wrapped_value),
                            source_file=caller.f_code.co_filename,
                            source_line=caller.f_lineno,
                        )
                    finally:
                        _registry_self._guard.active = False
                else:
                    super().__setattr__(name, wrapped_value)

        return _WatchedAnyAttrSubclass

    def _instrument_object_tree(self, obj: Any, root_expr: str, current_path: str,
                                depth: int, visited: set,
                                root_watch: "_AttributeWatch") -> None:
        """Recursively instrument `obj`'s nested user-defined attributes.

        For every attribute on `obj.__dict__`:
        - `list` / `dict` / `set` value → wrap in `_WatchedList/Dict/Set`;
          record a container-wrap sub-watch in `root_watch.sub_watches`.
        - User-defined-object value (per `_is_object_watchable`) → install
          class surgery (`_install_single_object_watch`); record the
          resulting sub-watch; recurse one level deeper.
        - Anything else (primitives, builtins, already-watched values) → skip.

        Depth-capped at `_RECURSIVE_OBJECT_WATCH_DEPTH` and cycle-guarded by
        `visited` (set of `id()`s) so a graph like `a.left = a; a.right = a`
        doesn't spin forever.
        """
        if depth > _RECURSIVE_OBJECT_WATCH_DEPTH:
            return
        for attr_name in _safe_iter_dict_attrs(obj):
            try:
                attr_value = getattr(obj, attr_name, None)
            except Exception:
                continue
            if attr_value is None:
                continue
            sub_expr = f"{current_path}.{attr_name}"

            if (isinstance(attr_value, _CONTAINER_TYPES)
                    and not isinstance(attr_value, _WATCHED_CONTAINER_TYPES)):
                wrapped = _wrap_container(attr_value, self, sub_expr)
                self._guard.active = True
                installed = False
                try:
                    setattr(obj, attr_name, wrapped)
                    installed = True
                except Exception:
                    pass
                finally:
                    self._guard.active = False
                if installed:
                    sub_w = _AttributeWatch(
                        expr=sub_expr,
                        obj_ref=None,
                        original_cls=None,
                        watcher_cls=None,
                        initial_repr=_safe_repr(attr_value),
                    )
                    sub_w.container_wrapper = wrapped
                    sub_w.container_holder = obj
                    sub_w.container_attr = attr_name
                    root_watch.sub_watches.append(sub_w)
                continue

            if _is_object_watchable(attr_value):
                if id(attr_value) in visited:
                    continue
                visited.add(id(attr_value))
                try:
                    sub_w = self._install_single_object_watch(
                        sub_expr, attr_value, root_expr=root_expr,
                    )
                except TypeError:
                    continue
                root_watch.sub_watches.append(sub_w)
                self._instrument_object_tree(
                    attr_value, root_expr=root_expr, current_path=sub_expr,
                    depth=depth + 1, visited=visited, root_watch=root_watch,
                )

    # ------------------------------------------------------------------
    # Removal internals
    # ------------------------------------------------------------------

    def _remove_local_watch_locked(self, key: tuple) -> None:
        """Remove a single local watch identified by (name, frame_id)."""
        w = self._local_watches.pop(key, None)
        if w is None:
            return
        # If no other watch still cares about this code object, disable LINE events.
        if not any(lw.code is w.code for lw in self._local_watches.values()):
            try:
                _monitoring.set_local_events(_TOOL_ID, w.code, 0)
            except Exception:
                pass

    def _remove_attr_watch_locked(self, expr: str) -> None:
        """Remove an attribute watch and undo all instrumentation it owns.

        For a `_add_attr_watch`-installed watch, that's the rebind-detector
        class surgery plus (if any) the container wrap at the leaf.

        For a `_add_object_watch`-installed root, that's also all the
        nested sub-watches recorded in `sub_watches` – we undo them depth-
        last (so containers get un-wrapped while their holder's class
        surgery is still in place to suppress firing on the restore).
        """
        w = self._attr_watches.pop(expr, None)
        if w is None:
            return
        # Walk sub_watches in REVERSE-installation order. Sub-watches were
        # appended depth-first during instrumentation, so reversing means
        # we undo deep children before their parents – containers come off
        # while the parent class surgery is still active to swallow the
        # un-wrap setattr, and nested object class swaps come off bottom
        # up so a parent that overrides `__setattr__` to validate doesn't
        # see partially-restored child types.
        for sub in reversed(w.sub_watches):
            self._undo_attr_watch_payload(sub)
        # Now undo the root itself.
        self._undo_attr_watch_payload(w)

    def _undo_attr_watch_payload(self, w: "_AttributeWatch") -> None:
        """Reverse a single _AttributeWatch's instrumentation.

        Handles three shapes a watch can carry – any combination of them
        may be set on the same watch (e.g. a dotted classpatch watch on a
        list-valued leaf carries both a container wrap and a classpatch):

        - container-wrap watch: `container_wrapper`/`container_holder`/
          `container_attr` are set; we restore the plain container at the
          holder and null the wrapper's registry/expr so any user-held
          alias stops firing on mutation.
        - class-surgery watch: `obj_ref`/`original_cls`/`watcher_cls` are
          set; we restore `obj.__class__` to the original.
        - classpatch watch: `obj_ref` + `classpatch_key` are set; we
          remove the (id(obj), key) entry from the per-class classpatch
          registry. When the last entry on a class is gone, the patched
          `__setattr__` itself is removed and the class is restored.

        Container unwrap goes first for the same ordering reason
        `_remove_attr_watch_locked` reverses the `sub_watches` list:
        un-wraps want their parent's interception (class-surgery or
        classpatch) still active so the un-wrap `setattr` doesn't fire
        as a spurious change.
        """
        try:
            wrapper = w.container_wrapper
            holder = w.container_holder
            attr_name = w.container_attr
            if wrapper is not None and holder is not None and attr_name is not None:
                # Only un-wrap if the holder still has *our* wrapper at
                # that attribute – the user may have reassigned it, which
                # would have gone through `__setattr__` and is their
                # responsibility now.
                current = getattr(holder, attr_name, None)
                if current is wrapper:
                    self._guard.active = True
                    try:
                        setattr(holder, attr_name, _unwrap_container(wrapper))
                    finally:
                        self._guard.active = False
                # Drop registry/expr on the wrapper so any leftover alias
                # user code holds doesn't keep firing against a removed
                # watch.
                wrapper.__dict__["_wp_registry"] = None
                wrapper.__dict__["_wp_expr"] = None
        except Exception:
            pass
        try:
            obj = w.obj_ref
            if obj is not None and w.watcher_cls is not None and type(obj) is w.watcher_cls:
                obj.__class__ = w.original_cls
        except Exception:
            pass
        try:
            if w.classpatch_key is not None and w.obj_ref is not None:
                _remove_classpatch_attr_watch(w.obj_ref, w.classpatch_key)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # sys.monitoring callbacks
    # ------------------------------------------------------------------
    #
    # Concurrency contract:
    # - All dict reads/mutations in callbacks happen under self._lock.
    # - The lock is RELEASED before any potentially-blocking operation
    #   (in particular before _fire_if_changed → _handle_hit → do_wait_suspend).
    # - Lookups of frame_state and active_watches build immutable snapshots
    #   so concurrent watch()/unwatch() from other threads cannot race the
    #   callback into "dict changed size during iteration".
    # - We do NOT sweep "stale" watches in _on_line. A naive sweep that
    #   removed any watch with code==this AND frame_id!=mine would wrongly
    #   remove watches from CONCURRENT live frames of the same code
    #   (multiple threads / asyncio tasks running the same function).
    #   PY_START handles the id-reuse case that the sweep used to cover.

    def _on_py_start(self, code: Any, instruction_offset: int) -> None:
        """PY_START callback. Fires when a watched code object is entered.

        Two responsibilities:
        1. Defeat CPython's frame-id reuse. When a new frame F2 is entered
           with id(F2) == id(F1) for a long-dead frame F1 (whose watch was
           never cleaned up because exception unwinding bypassed PY_RETURN),
           the LINE callbacks would otherwise diff against F1's stale
           baseline and spuriously fire. Here, immediately on entry, we wipe
           any prior watch/state keyed under (id(F2), this_code) – they
           cannot belong to F2 because F2 hasn't called watch() yet.
           Crucially we MATCH ON EXACT fid (not "any other fid"), so
           concurrent frames of the same code on other threads / tasks are
           not touched.
        2. Pick up a pending cross-function watch propagation, if the most
           recent CALL from a watching frame scheduled one for this code.
           See `_on_call` for how the propagation is registered.
        """
        if getattr(self._guard, "active", False):
            return
        try:
            frame = sys._getframe(1)
            fid = id(frame)
        except Exception:
            return
        self._guard.active = True
        try:
            with self._lock:
                stale_keys = [k for k, w in self._local_watches.items()
                              if w.code is code and w.frame_id == fid]
                for k in stale_keys:
                    self._remove_local_watch_locked(k)
                stale_state = self._frame_state.get(fid)
                if stale_state is not None and stale_state.get("code") is code:
                    del self._frame_state[fid]

            # Apply any pending propagation scheduled by a CALL event from a
            # watching frame. The queue is a per-thread stack; we search from
            # the top so the most recent CALL matches first. Mismatched
            # entries (e.g. CALL whose PY_START never fires) are left in
            # place and are popped later when their code object eventually
            # matches a PY_START, or are cleared by `clear_watches`.
            queue = getattr(self._pending_propagation, "queue", None)
            if queue:
                for i in range(len(queue) - 1, -1, -1):
                    if queue[i][0] is code:
                        _, watched_by_id = queue.pop(i)
                        self._apply_propagation(frame, watched_by_id)
                        break
        finally:
            self._guard.active = False

    def _on_call(self, code: Any, instruction_offset: int,
                 callable_: Any, arg0: Any) -> None:
        """CALL callback. Fires from a watching frame before each function call.

        Cross-function watch propagation: if any local in the watching frame
        is being passed to the callee, schedule the callee's matching
        parameter to be watched too. Matching is by object identity – when
        the watched value is a unique object the match is precise; for
        interned primitives (small ints, short strings) several params with
        the same value can match, which is the documented trade-off for
        following primitives across call boundaries.

        We schedule when the callable's body will actually run as a Python
        frame next, so PY_START fires and the propagation gets picked up:
        - plain Python functions: `callable_.__code__`
        - bound methods: `callable_.__code__` works on the bound method too
        - class instantiation: `callable_.__init__.__code__` (Python __init__)
        Generators / coroutines / async generators are skipped: calling them
        doesn't enter their body (it returns a generator/coroutine object),
        so no PY_START would follow and a queued propagation would leak.
        C functions, classes whose __init__ is C-implemented, partials, and
        anything else without a Python __code__ are simply skipped.
        """
        if getattr(self._guard, "active", False):
            return

        callee_code = _python_code_for_call(callable_)
        if callee_code is None:
            return  # not a Python frame – no PY_START would follow
        if callee_code.co_flags & _LAZY_BODY_FLAGS:
            # Generator / coroutine / async-generator: call returns the
            # iterable without running the body, so PY_START is decoupled
            # from CALL. Skip to avoid a stale queue entry.
            return

        self._guard.active = True
        try:
            try:
                caller_frame = sys._getframe(1)
            except Exception:
                return
            caller_fid = id(caller_frame)

            # Build {id(value): caller_name} for every watch active in this
            # caller frame. We snapshot inside the lock so concurrent
            # watch()/unwatch() on other threads can't race us into iterating
            # a mutated dict. The id() snapshot itself is safe to use after
            # the lock is dropped because we no longer touch the dict.
            with self._lock:
                watched_by_id: dict[int, str] = {}
                for (name, fid) in list(self._local_watches.keys()):
                    if fid != caller_fid:
                        continue
                    try:
                        value = caller_frame.f_locals.get(name)
                    except Exception:
                        continue
                    watched_by_id[id(value)] = name

            if not watched_by_id:
                return

            queue = getattr(self._pending_propagation, "queue", None)
            if queue is None:
                queue = []
                self._pending_propagation.queue = queue
            queue.append((callee_code, watched_by_id))
            # Safety cap: pathological cases (rare arg-binding failures that
            # raise before the callee body runs, or a stalled callable that
            # never reaches PY_START) would otherwise grow the queue without
            # bound. 128 is well above any plausible legitimate nesting.
            if len(queue) > 128:
                del queue[0]

            # Enable the same set of events on the callee's code object so
            # the propagated watch behaves identically to a directly-armed
            # one, and so further calls FROM the callee can propagate again.
            try:
                _monitoring.set_local_events(
                    _TOOL_ID, callee_code,
                    _monitoring.events.LINE
                    | _monitoring.events.PY_RETURN
                    | _monitoring.events.PY_START
                    | _monitoring.events.CALL
                )
            except Exception:
                pass
        finally:
            self._guard.active = False

    def _apply_propagation(self, frame: Any, watched_by_id: dict) -> None:
        """Arm a local watch on every parameter of `frame` whose value
        matches (by `id()`) one of the watched values passed in.

        Called from `_on_py_start` when a pending propagation was scheduled
        by the matching CALL. Only positional and keyword-only parameters
        are scanned today; *args / **kwargs contents are not unpacked.
        """
        code = frame.f_code
        n_args = code.co_argcount + code.co_kwonlyargcount
        arg_names = code.co_varnames[:n_args]
        if not arg_names:
            return

        try:
            f_locals = dict(frame.f_locals)
        except Exception:
            return

        for param_name in arg_names:
            if param_name not in f_locals:
                continue
            value_id = id(f_locals[param_name])
            caller_name = watched_by_id.get(value_id)
            if caller_name is None:
                continue
            try:
                with self._lock:
                    fid = id(frame)
                    if (param_name, fid) in self._local_watches:
                        self._remove_local_watch_locked((param_name, fid))
                    # display_name=caller_name so the hit surfaces as the
                    # user's original watched name ("data") rather than the
                    # callee's parameter name ("d").
                    self._add_local_watch(
                        param_name, frame, display_name=caller_name
                    )
            except Exception:
                # Best-effort propagation: a failure on one param should not
                # prevent the rest of the callee from running normally.
                pass

    def _on_line(self, code: Any, line_number: int) -> None:
        """LINE callback. Fires BEFORE `line_number` executes.

        Because the event fires before the line runs, frame.f_locals reflects
        the state produced by the PREVIOUS line. We diff that against the
        stored prev_hashes to detect what that previous line changed.

        Concurrency: the dict reads/writes happen inside `self._lock`. We
        release the lock BEFORE calling _fire_if_changed because that path
        can call do_wait_suspend, which blocks until the user clicks Resume
        in the IDE; holding the lock there would freeze every other thread.

        Re-entrancy: state is advanced inside the lock; the snapshot we hand
        off to _fire_if_changed is from BEFORE that advance. If a caught
        WatchpointHit causes execution to resume here, the next callback
        sees the up-to-date hashes and does not re-fire on the same change.
        """
        if getattr(self._guard, "active", False):
            return
        self._guard.active = True
        try:
            frame = sys._getframe(1)
            fid = id(frame)

            with self._lock:
                # Iterate (and possibly mutate) under lock so concurrent
                # watch()/unwatch() on other threads cannot race us.
                active_watches = [
                    w for w in self._local_watches.values()
                    if w.code is code and w.frame_id == fid
                ]
                if not active_watches:
                    return

                current_locals = dict(frame.f_locals)
                new_hashes = {w.name: _value_hash(current_locals.get(w.name))
                              for w in active_watches}
                new_reprs = {w.name: _safe_repr(current_locals.get(w.name))
                             for w in active_watches}

                state = self._frame_state.get(fid)
                if state is None or state.get("code") is not code:
                    self._frame_state[fid] = {
                        "code": code,
                        "prev_line": line_number,
                        "prev_hashes": new_hashes,
                        "prev_reprs": new_reprs,
                    }
                    return

                prev_line = state["prev_line"]
                prev_hashes = state["prev_hashes"]
                prev_reprs = state["prev_reprs"]

                # Advance state inside the lock so a follow-up event sees
                # the post-fire baseline regardless of when our pause returns.
                state["prev_line"] = line_number
                state["prev_hashes"] = new_hashes
                state["prev_reprs"] = new_reprs

            # Lock released – safe to enter pydevd which may block.
            if prev_line is not None:
                self._fire_if_changed(
                    frame, active_watches, prev_line, prev_hashes, prev_reprs,
                    new_hashes, new_reprs, code.co_filename,
                )
        finally:
            self._guard.active = False

    def _on_py_return(self, code: Any, instruction_offset: int, retval: Any) -> None:
        """PY_RETURN callback – fires on NORMAL return only (not exception unwind).

        Two responsibilities:
        1. Diff once more to catch a change made by the LAST line, which has
           no following LINE event in this frame.
        2. Clean up: drop watches and frame state belonging to this frame.
        """
        if getattr(self._guard, "active", False):
            return
        try:
            frame = sys._getframe(1)
            fid = id(frame)
        except Exception:
            return

        self._guard.active = True
        try:
            with self._lock:
                state = self._frame_state.pop(fid, None)
                active_keys = [k for k, w in self._local_watches.items()
                               if w.code is code and w.frame_id == fid]
                active_watches = [self._local_watches[k] for k in active_keys]

                # Drop this frame's watches; the frame is leaving regardless of
                # whether a final-line change-fire is about to happen.
                for k in active_keys:
                    self._remove_local_watch_locked(k)

                if not active_watches:
                    return
                if not state or state.get("code") is not code or state["prev_line"] is None:
                    return

                current_locals = dict(frame.f_locals)
                new_hashes = {w.name: _value_hash(current_locals.get(w.name))
                              for w in active_watches}
                new_reprs = {w.name: _safe_repr(current_locals.get(w.name))
                             for w in active_watches}

            # Lock released – safe to call _fire_if_changed which may block in pydevd.
            # Pause at the CALLER's frame, not the leaving one: pydevd's
            # do_wait_suspend on a frame that's mid-return causes the IDE to
            # show "Frames are not available in non-suspended state". The
            # caller is alive and the natural place for the user to inspect
            # the state of the returning function's locals (via the up-stack
            # navigator – source_file/source_line still point at the assignment).
            pause_frame = frame.f_back or frame
            self._fire_if_changed(
                pause_frame, active_watches, state["prev_line"], state["prev_hashes"],
                state["prev_reprs"], new_hashes, new_reprs, code.co_filename,
            )
        finally:
            self._guard.active = False

    def _fire_if_changed(self, user_frame: Any, active_watches: list, prev_line: int,
                         prev_hashes: dict, prev_reprs: dict,
                         new_hashes: dict, new_reprs: dict,
                         filename: str) -> None:
        """Trigger the watchpoint for the first active watch whose hash changed.

        Pause behaviour is delegated to _handle_hit which either pauses the
        debugger (if pydevd is loaded) or raises WatchpointHit (standalone /
        test runs). Caller advances frame state BEFORE calling this, so a
        caught exception and resumed execution do not refire on the same change.
        """
        for w in active_watches:
            name = w.name
            old_hash = prev_hashes.get(name)
            new_hash = new_hashes.get(name)
            if old_hash is None or new_hash is None or old_hash == new_hash:
                continue
            self._handle_hit(
                user_frame=user_frame,
                # `display_name` equals `name` for direct watches and carries
                # the caller's original watched name for propagated ones.
                watch_name=w.display_name,
                old_repr=prev_reprs.get(name, w.initial_repr),
                new_repr=new_reprs.get(name, "<unknown>"),
                source_file=filename,
                source_line=prev_line,
            )
            return  # one pause per LINE event – any other watches will fire on their next change

    def _handle_hit(self, user_frame: Any, watch_name: str,
                    old_repr: str, new_repr: str,
                    source_file: str, source_line: int) -> None:
        """Pause the debugger via pydevd; if pydevd isn't loaded, raise the exception.

        Using pydevd's do_wait_suspend gives a clean stack view – the IDE shows
        the user's frame as the stopped frame, not our LINE-callback frames –
        and clicking Resume continues execution silently rather than re-throwing
        WatchpointHit out of the user's code.

        The raise fallback keeps the test suite working under plain pytest where
        pydevd is not present in the environment.
        """
        _dbg_log(
            f"_handle_hit: name={watch_name!r}, file={source_file}:{source_line}, "
            f"old={old_repr!r}, new={new_repr!r}, backend={_detect_debugger()}"
        )
        # Publish the hit BEFORE we hand off to pydevd. The IDE plugin's
        # session-paused listener calls `_pycharm_consume_last_hit` once the
        # pause has materialised; whichever pause path we take below, this
        # field is the single source of truth for "what just fired".
        with self._lock:
            self._last_hit = {
                "file": source_file,
                "line": source_line,
                "name": watch_name,
                "old": old_repr,
                "new": new_repr,
            }

        # Also emit a structured stderr marker the IDE can parse without an eval.
        # The PyDebugProcess.evaluate(..., doTrunc=false) path that `_pycharm_consume_last_hit`
        # was designed for is pydevd-only; under debugpy the generic XDebuggerEvaluator
        # truncates base64 payloads so the IDE can't reconstruct the hit. Routing the
        # same payload through the process's stderr stream sidesteps both problems –
        # the plugin attaches a ProcessListener and consumes the marker on every hit,
        # working identically under pydevd, debugpy, and any future backend.
        _emit_hit_marker(self._last_hit)

        py_db = _get_pydevd_debugger()
        if py_db is not None:
            try:
                _pause_via_pydevd(py_db, user_frame, watch_name, old_repr, new_repr,
                                  source_file, source_line)
                return
            except Exception as e:  # noqa: BLE001
                global _pydevd_last_error
                _pydevd_last_error = f"do_wait_suspend failed: {e!r}"
                _log_warn(
                    f"pause-via-pydevd failed ({e!r}); diag: {_pycharm_watchpoint_diag()}"
                )

        # Pause failed. Choose between raising and silently logging based on the
        # detected backend:
        #   pydevd → raise. The plugin's `WatchpointDebugListener` registered a
        #            `PyExceptionBreakpointType` for `watchpoint.WatchpointHit`,
        #            so pydevd catches the raise and pauses cleanly. This is the
        #            no-pydevd-fallback-but-pydevd-is-loaded edge case (rare).
        #   debugpy → don't raise. debugpy doesn't honor `PyExceptionBreakpointType`,
        #             so the exception would propagate uncaught and KILL the user's
        #             program. Better UX: log the hit to stderr (Debug Console)
        #             and let execution continue. The user sees `[WATCHPOINT] hit
        #             '...': old -> new at file:line` and knows what happened.
        #   none → raise. Same as pydevd, except now `WatchpointHit` propagates as
        #          a real Python exception, which is what the test suite asserts on.
        backend = _detect_debugger()
        if backend == "debugpy":
            _log_warn(
                f"hit '{watch_name}': {old_repr} -> {new_repr} at {source_file}:{source_line} "
                f"(debugpy: pause not yet implemented – execution continues)"
            )
            return
        _log_warn(
            f"pydevd debugger not found / pause failed; raising WatchpointHit. "
            f"Diag: {_pycharm_watchpoint_diag()}"
        )
        raise WatchpointHit(watch_name, old_repr, new_repr, source_file, source_line)


# ---------------------------------------------------------------------------
# Watch data structures
# ---------------------------------------------------------------------------

class _LocalWatch:
    """State for a single local-variable watch.

    `frame_id` ties this watch to the specific frame instance where watch()
    was called. The watch is removed when that frame exits (PY_RETURN) or
    is detected dead via the lazy zombie sweep in _on_line.

    `display_name` is the name surfaced in `WatchpointHit.watch_name`. It
    matches `name` for directly-armed watches; for watches armed by
    cross-function propagation (`_apply_propagation`) it carries the
    caller's original watched name so the user-visible identity of the
    hit stays consistent across the call boundary, even though `name`
    has to be the callee's parameter name (since that's what we eval/diff).
    """
    __slots__ = ("name", "code", "frame_id", "initial_hash", "initial_repr",
                 "display_name")

    def __init__(self, name: str, code: Any, frame_id: int,
                 initial_hash: int, initial_repr: str,
                 display_name: Optional[str] = None) -> None:
        self.name = name
        self.code = code
        self.frame_id = frame_id
        self.initial_hash = initial_hash
        self.initial_repr = initial_repr
        self.display_name = display_name or name


class _AttributeWatch:
    """State for a single object-attribute watch.

    `container_wrapper` is the _WatchedList / _WatchedDict / _WatchedSet
    instance we installed in place of the leaf attribute when its value was
    a mutable container (see `_add_attr_watch` + `_wrap_container`). It's
    None when the leaf wasn't a container (the rebind-detector alone
    suffices), or after `_remove_attr_watch_locked` restored the plain
    container in place of the wrapper.
    """
    __slots__ = ("expr", "obj_ref", "original_cls", "watcher_cls", "initial_repr",
                 "container_wrapper", "container_holder", "container_attr",
                 "sub_watches", "classpatch_key")

    def __init__(self, expr: str, obj_ref: Any, original_cls: Optional[type],
                 watcher_cls: Optional[type], initial_repr: str) -> None:
        self.expr = expr
        self.obj_ref = obj_ref
        self.original_cls = original_cls
        self.watcher_cls = watcher_cls
        self.initial_repr = initial_repr
        # Container wrap bookkeeping – populated by `_add_attr_watch` if the
        # leaf value was a mutable builtin container. `container_holder` is
        # the object whose attribute we replaced (i.e. `obj_ref` for the
        # specific-attribute path); `container_attr` is the attribute name.
        # Holding the holder + attr separately means restoring on remove
        # doesn't need to re-parse `expr`.
        self.container_wrapper = None
        self.container_holder = None
        self.container_attr = None
        # Sub-watches: child _AttributeWatch instances installed by recursive
        # object-tree instrumentation (see `_instrument_object_tree`). NOT
        # registered in `_attr_watches` – they live solely here so cleanup
        # of the root watch automatically unwinds the entire subtree. A
        # sub-watch can be either:
        #   - a class-surgery watch (obj_ref + original_cls + watcher_cls set)
        #   - a container-wrap watch (container_holder + container_attr +
        #     container_wrapper set, others None)
        #   - a classpatch watch (obj_ref + classpatch_key set)
        self.sub_watches: list = []
        # Classpatch bookkeeping – populated by `_try_classpatch_attr_watch`
        # / `_try_classpatch_object_watch` when the subclassing strategy is
        # blocked by a hostile metaclass. Holds the attribute name (for
        # dotted watches) or `'__any__'` (for bare-name wildcard watches).
        # `obj_ref` carries the target instance. `_undo_attr_watch_payload`
        # calls `_remove_classpatch_attr_watch(obj_ref, classpatch_key)`
        # when this is set.
        self.classpatch_key = None


# ---------------------------------------------------------------------------
# Container-mutation watchers
# ---------------------------------------------------------------------------
#
# When `_add_attr_watch` resolves the leaf attribute to a mutable builtin
# container (list / dict / set), we cannot use `__class__` surgery to add
# mutation interception – CPython forbids `__class__` assignment between
# heap types and builtin instances (`TypeError: __class__ assignment: only
# for heap types`). Instead, we construct a NEW instance of a watcher
# subclass populated with the original's contents and replace the leaf
# attribute. From watch-arm time on, every read of `obj.attr` returns the
# wrapper; mutations through the wrapper fire `_handle_hit`.
#
# Documented trade-off: any reference to the original container that user
# code captured BEFORE the watch was armed still points at the original
# (un-wrapped) instance. Mutations through that stale reference are
# invisible to the watchpoint. The wrap-and-replace approach is the most
# we can do without a CPython-level hook, and it covers the natural
# pattern `value = obj.attr; value.mutate()` because the read happens
# after watch-arm and therefore yields the wrapper.

# Types we know how to wrap. Tuple matches `_LOCAL_WATCH_BUILTIN_TYPES`
# intentionally narrowly: bytearray, frozenset are excluded because the
# first is rarely watched and the second is immutable.
_CONTAINER_TYPES = (list, dict, set)


def _wp_container_repr(value: Any) -> str:
    """Return the *base type's* repr for a wrapped container.

    We delegate to `list.__repr__` / `dict.__repr__` / `set.__repr__`
    explicitly so a future subclass-of-subclass that overrides `__repr__`
    can't recurse into our firing path.
    """
    if isinstance(value, list):
        return list.__repr__(value)
    if isinstance(value, dict):
        return dict.__repr__(value)
    if isinstance(value, set):
        return set.__repr__(value)
    try:
        return repr(value)
    except Exception:
        return "<unreprable>"


def _wp_fire_container_change(self: Any, old_repr_full: str,
                              frame_skip: int = 2) -> None:
    """Fire `_handle_hit` if the wrapped container's repr changed.

    Called by every mutating method on _WatchedList/_WatchedDict/_WatchedSet
    *after* the underlying mutation has been applied. `frame_skip=2` skips
    this function + the mutating method, so `sys._getframe(2)` lands on the
    user-code frame that called `.append(...)` / `[k]=v` / etc.

    Silently no-ops in three cases that aren't real changes:
    - The mutation didn't actually change repr (e.g. `set.add` of an
      already-present element, or `dict.update({})`).
    - The watch has been removed (`_wp_registry` was nulled by
      `_remove_attr_watch_locked`). User code may still hold the wrapper
      via a captured alias; mutations on it after unwatch should be silent.
    - We're already inside the registry's re-entrancy guard (pydevd's own
      protocol code may end up touching the wrapper while we're paused).
    """
    new_repr_full = _wp_container_repr(self)
    if old_repr_full == new_repr_full:
        return

    registry = self.__dict__.get("_wp_registry")
    expr = self.__dict__.get("_wp_expr")
    if registry is None or expr is None:
        return
    if getattr(registry._guard, "active", False):
        return

    try:
        caller = sys._getframe(frame_skip)
    except Exception:
        return

    registry._guard.active = True
    try:
        # Truncate for display only; comparison above used full reprs so
        # we don't miss changes past the 200-char threshold.
        old_display = old_repr_full if len(old_repr_full) <= 200 else old_repr_full[:200] + "..."
        new_display = new_repr_full if len(new_repr_full) <= 200 else new_repr_full[:200] + "..."
        registry._handle_hit(
            user_frame=caller,
            watch_name=expr,
            old_repr=old_display,
            new_repr=new_display,
            source_file=caller.f_code.co_filename,
            source_line=caller.f_lineno,
        )
    finally:
        registry._guard.active = False


class _WatchedList(list):
    """List subclass that fires the watch on every mutating call.

    Wrapping mechanism (see module comment above the watcher classes for
    why we wrap-and-replace instead of using `__class__` surgery):
    - constructor copies the original list's items so the wrapper has the
      same logical contents
    - bookkeeping (`_wp_registry`, `_wp_expr`) lives on the instance's
      `__dict__`, NOT via the parent's `__setattr__`, so list subclass
      sanity invariants aren't disturbed

    Trade-offs that user code may notice:
    - `type(obj.attr) is list` → False (`isinstance(obj.attr, list)` is True)
    - pickling will succeed but registry/expr are dropped – the
      unpickled instance won't fire (registry is None on rebuild)
    """

    def __init__(self, iterable: Any = None, *, registry: Any = None,
                 expr: Optional[str] = None) -> None:
        if iterable is None:
            super().__init__()
        else:
            super().__init__(iterable)
        # __dict__ direct write avoids triggering any unexpected
        # __setattr__ behavior in pathological subclass-of-subclass cases.
        self.__dict__["_wp_registry"] = registry
        self.__dict__["_wp_expr"] = expr

    def append(self, item: Any) -> None:  # noqa: D401
        old = list.__repr__(self)
        list.append(self, item)
        _wp_fire_container_change(self, old)

    def extend(self, items: Any) -> None:
        old = list.__repr__(self)
        list.extend(self, items)
        _wp_fire_container_change(self, old)

    def insert(self, index: int, item: Any) -> None:
        old = list.__repr__(self)
        list.insert(self, index, item)
        _wp_fire_container_change(self, old)

    def remove(self, item: Any) -> None:
        old = list.__repr__(self)
        list.remove(self, item)
        _wp_fire_container_change(self, old)

    def pop(self, *args: Any) -> Any:
        old = list.__repr__(self)
        result = list.pop(self, *args)
        _wp_fire_container_change(self, old)
        return result

    def clear(self) -> None:
        old = list.__repr__(self)
        list.clear(self)
        _wp_fire_container_change(self, old)

    def sort(self, *args: Any, **kwargs: Any) -> None:
        old = list.__repr__(self)
        list.sort(self, *args, **kwargs)
        _wp_fire_container_change(self, old)

    def reverse(self) -> None:
        old = list.__repr__(self)
        list.reverse(self)
        _wp_fire_container_change(self, old)

    def __setitem__(self, key: Any, value: Any) -> None:
        old = list.__repr__(self)
        list.__setitem__(self, key, value)
        _wp_fire_container_change(self, old)

    def __delitem__(self, key: Any) -> None:
        old = list.__repr__(self)
        list.__delitem__(self, key)
        _wp_fire_container_change(self, old)

    def __iadd__(self, other: Any) -> "list":
        old = list.__repr__(self)
        # list.__iadd__ returns self – preserve that contract.
        result = list.__iadd__(self, other)
        _wp_fire_container_change(self, old)
        return result

    def __imul__(self, other: int) -> "list":
        old = list.__repr__(self)
        result = list.__imul__(self, other)
        _wp_fire_container_change(self, old)
        return result


class _WatchedDict(dict):
    """Dict subclass that fires on every mutating call. See `_WatchedList`."""

    def __init__(self, mapping: Any = None, *, registry: Any = None,
                 expr: Optional[str] = None) -> None:
        if mapping is None:
            super().__init__()
        else:
            super().__init__(mapping)
        self.__dict__["_wp_registry"] = registry
        self.__dict__["_wp_expr"] = expr

    def __setitem__(self, key: Any, value: Any) -> None:
        old = dict.__repr__(self)
        dict.__setitem__(self, key, value)
        _wp_fire_container_change(self, old)

    def __delitem__(self, key: Any) -> None:
        old = dict.__repr__(self)
        dict.__delitem__(self, key)
        _wp_fire_container_change(self, old)

    def pop(self, *args: Any, **kwargs: Any) -> Any:
        old = dict.__repr__(self)
        result = dict.pop(self, *args, **kwargs)
        _wp_fire_container_change(self, old)
        return result

    def popitem(self) -> Any:
        old = dict.__repr__(self)
        result = dict.popitem(self)
        _wp_fire_container_change(self, old)
        return result

    def clear(self) -> None:
        old = dict.__repr__(self)
        dict.clear(self)
        _wp_fire_container_change(self, old)

    def update(self, *args: Any, **kwargs: Any) -> None:
        old = dict.__repr__(self)
        dict.update(self, *args, **kwargs)
        _wp_fire_container_change(self, old)

    def setdefault(self, key: Any, default: Any = None) -> Any:
        old = dict.__repr__(self)
        result = dict.setdefault(self, key, default)
        _wp_fire_container_change(self, old)
        return result

    def __ior__(self, other: Any) -> "dict":
        # 3.9+ adds `d |= other`. dict.__ior__ returns self.
        old = dict.__repr__(self)
        result = dict.__ior__(self, other)
        _wp_fire_container_change(self, old)
        return result


class _WatchedSet(set):
    """Set subclass that fires on every mutating call. See `_WatchedList`."""

    def __init__(self, iterable: Any = None, *, registry: Any = None,
                 expr: Optional[str] = None) -> None:
        if iterable is None:
            super().__init__()
        else:
            super().__init__(iterable)
        self.__dict__["_wp_registry"] = registry
        self.__dict__["_wp_expr"] = expr

    def add(self, item: Any) -> None:
        old = set.__repr__(self)
        set.add(self, item)
        _wp_fire_container_change(self, old)

    def discard(self, item: Any) -> None:
        old = set.__repr__(self)
        set.discard(self, item)
        _wp_fire_container_change(self, old)

    def remove(self, item: Any) -> None:
        old = set.__repr__(self)
        set.remove(self, item)
        _wp_fire_container_change(self, old)

    def pop(self) -> Any:
        old = set.__repr__(self)
        result = set.pop(self)
        _wp_fire_container_change(self, old)
        return result

    def clear(self) -> None:
        old = set.__repr__(self)
        set.clear(self)
        _wp_fire_container_change(self, old)

    def update(self, *iterables: Any) -> None:
        old = set.__repr__(self)
        set.update(self, *iterables)
        _wp_fire_container_change(self, old)

    def intersection_update(self, *iterables: Any) -> None:
        old = set.__repr__(self)
        set.intersection_update(self, *iterables)
        _wp_fire_container_change(self, old)

    def difference_update(self, *iterables: Any) -> None:
        old = set.__repr__(self)
        set.difference_update(self, *iterables)
        _wp_fire_container_change(self, old)

    def symmetric_difference_update(self, *iterables: Any) -> None:
        old = set.__repr__(self)
        set.symmetric_difference_update(self, *iterables)
        _wp_fire_container_change(self, old)

    def __ior__(self, other: Any) -> "set":
        old = set.__repr__(self)
        result = set.__ior__(self, other)
        _wp_fire_container_change(self, old)
        return result

    def __iand__(self, other: Any) -> "set":
        old = set.__repr__(self)
        result = set.__iand__(self, other)
        _wp_fire_container_change(self, old)
        return result

    def __ixor__(self, other: Any) -> "set":
        old = set.__repr__(self)
        result = set.__ixor__(self, other)
        _wp_fire_container_change(self, old)
        return result

    def __isub__(self, other: Any) -> "set":
        old = set.__repr__(self)
        result = set.__isub__(self, other)
        _wp_fire_container_change(self, old)
        return result


_WATCHED_CONTAINER_TYPES = (_WatchedList, _WatchedDict, _WatchedSet)


def _wrap_container(value: Any, registry: Any, expr: str) -> Any:
    """Construct the appropriate `_Watched*` wrapper for `value`.

    Returns `value` unchanged when it's already a watcher (idempotent –
    `_add_attr_watch` reuse / `__setattr__` reassignment won't double-wrap)
    or not a wrappable container.
    """
    if isinstance(value, _WATCHED_CONTAINER_TYPES):
        return value
    if isinstance(value, list):
        return _WatchedList(value, registry=registry, expr=expr)
    if isinstance(value, dict):
        return _WatchedDict(value, registry=registry, expr=expr)
    if isinstance(value, set):
        return _WatchedSet(value, registry=registry, expr=expr)
    return value


def _unwrap_container(value: Any) -> Any:
    """Return a plain (un-watched) copy of a wrapped container.

    Used by `_remove_attr_watch_locked` to restore the holder's attribute
    to a non-wrapper instance once the watch is gone. We copy rather than
    just clear the registry because keeping a `_WatchedList` instance
    visible to user code after unwatching would surprise anyone doing
    `type(x) is list` checks.
    """
    if isinstance(value, _WatchedList):
        return list(value)
    if isinstance(value, _WatchedDict):
        return dict(value)
    if isinstance(value, _WatchedSet):
        return set(value)
    return value


# ---------------------------------------------------------------------------
# Class-level __setattr__ monkey-patching (fallback for hostile metaclasses)
# ---------------------------------------------------------------------------
#
# When `_add_attr_watch` / `_add_object_watch` would otherwise fail because
# the parent class's metaclass refuses dynamic subclassing – Django's
# `ModelBase` raises `RuntimeError: Model class … doesn't declare an
# explicit app_label …`, SQLAlchemy's `DeclarativeMeta` raises about
# missing `__table__`/`__tablename__`, etc. – we fall back to patching the
# *existing* class's `__setattr__` directly. The patch intercepts every
# attribute write to instances of that class; we filter down to writes on
# the SPECIFIC watched instance(s) using a per-class `instance_watches`
# table keyed by `id(instance)`.
#
# Trade-offs vs. the class-surgery approach:
# - Pro: works on classes with hostile metaclasses (Django Model,
#   SQLAlchemy declarative base, etc.) where `class _Sub(orig):` itself
#   raises before we ever get a chance to `__class__`-swap.
# - Con: every setattr on ANY instance of the patched class pays a dict
#   lookup, including instances that are not being watched. For Django
#   apps this is usually negligible (single O(1) dict miss per ORM op).
# - Con: cleanup is per-instance, not per-class. `watch('obj.field')`
#   patches the class but registers only THAT instance; other instances
#   of the same class fall through to the original `__setattr__`. When
#   the last instance is unwatched, the patch is removed from the class.
#
# The patched `__setattr__` runs the same `_handle_hit` pipeline as the
# class-surgery path, so the pause behavior and the hit notification
# format are identical between the two strategies.

class _ClassPatch:
    """Per-class state for one monkey-patched `__setattr__`.

    `cls` – the class we patched.
    `had_own_setattr` / `original_setattr_in_cls_dict` – whether the class
        had its own `__setattr__` in `cls.__dict__` before we patched it,
        plus a reference to it if so. On cleanup, presence of an own one
        means we restore it; absence means we `del cls.__setattr__` so MRO
        lookup resumes finding the parent's `__setattr__`.
    `instance_watches` – `{id(target_instance): {attr_name | '__any__':
        _AttributeWatch}}`. The patched `__setattr__` looks up `id(self)`
        here on every call; absence means pass-through to the original. The
        `'__any__'` wildcard is used by bare-name `watch('obj')` on a
        hostile-metaclass instance and fires for ANY attribute write;
        specific attribute names fire only for that single attribute.
    """
    __slots__ = ("cls", "had_own_setattr", "original_setattr_in_cls_dict",
                 "instance_watches")

    def __init__(self, cls: type, had_own_setattr: bool,
                 original_setattr_in_cls_dict: Any) -> None:
        self.cls = cls
        self.had_own_setattr = had_own_setattr
        self.original_setattr_in_cls_dict = original_setattr_in_cls_dict
        self.instance_watches: dict = {}


# Global registry of patched classes. Locked by `_classpatch_lock` for
# install / remove. Reads from inside the patched `__setattr__` use the
# closure-bound `_ClassPatch` instance directly and never touch this dict,
# so the patched-setattr hot path doesn't contend on this lock.
_classpatch_registry: dict = {}
_classpatch_lock = threading.RLock()


def _find_inherited_setattr(cls: type) -> Any:
    """Walk `cls`'s MRO past `cls` to find the closest inherited `__setattr__`.

    Returns a callable matching the `__setattr__(obj, name, value)` shape.
    We pre-bind this lookup at install time so the patched `__setattr__`'s
    hot path is a single direct call rather than a per-setattr MRO walk.
    """
    for base in cls.__mro__[1:]:
        sa = base.__dict__.get("__setattr__")
        if sa is not None:
            return sa
    return object.__setattr__


def _install_classpatch_attr_watch(registry: "WatchpointRegistry",
                                   target_obj: Any, key: str,
                                   attr_watch: "_AttributeWatch") -> bool:
    """Install a classpatch watch on `target_obj` for attribute `key`.

    `key` is either a specific attribute name (dotted-path watch on a
    hostile-metaclass instance) or `'__any__'` for the wildcard form used
    by bare-name `watch('obj')` on the same.

    Returns True on success, False if patching the class's `__setattr__`
    itself is blocked (a meta-metaclass that refuses class-level attribute
    writes, or an analogous restriction). On False the caller should
    either raise a clean TypeError (dotted watch) or fall through to the
    next available strategy (bare-name → local-variable rebind detection).
    """
    cls = type(target_obj)
    target_id = id(target_obj)

    with _classpatch_lock:
        patch = _classpatch_registry.get(cls)
        if patch is None:
            had_own = "__setattr__" in cls.__dict__
            original_in_dict = cls.__dict__.get("__setattr__") if had_own else None
            patch = _ClassPatch(cls, had_own, original_in_dict)

            # Pre-bind the original/inherited `__setattr__` so the hot path
            # avoids an MRO walk per call.
            _orig_callable = original_in_dict if had_own else _find_inherited_setattr(cls)

            def patched_setattr(self_obj: Any, name: str, value: Any,
                                _patch: _ClassPatch = patch,
                                _orig: Any = _orig_callable,
                                _reg: "WatchpointRegistry" = registry) -> None:
                """Patched `__setattr__`: route writes on watched instances
                through `_handle_hit`, otherwise behave like the original.

                Hot path is two dict-gets when this instance isn't watched.
                Re-entrancy guard mirrors the in-class `__setattr__` overrides
                in `_WatchedSubclass` / `_WatchedAnyAttrSubclass` so pydevd's
                protocol code can't recursively pause while the user is
                already paused.
                """
                entries = _patch.instance_watches.get(id(self_obj))
                if not entries:
                    _orig(self_obj, name, value)
                    return
                if getattr(_reg._guard, "active", False):
                    _orig(self_obj, name, value)
                    return
                # Specific name takes priority over wildcard so a user with
                # both `watch('obj')` and `watch('obj.field')` sees the
                # specific watch_name when `field` is set.
                aw = entries.get(name)
                wildcard = False
                if aw is None:
                    aw = entries.get("__any__")
                    wildcard = aw is not None
                if aw is None:
                    _orig(self_obj, name, value)
                    return
                try:
                    old_val = getattr(self_obj, name)
                except AttributeError:
                    old_val = None
                # Apply the assignment first so a failure (read-only attr,
                # validation in the original __setattr__, etc.) doesn't
                # leave the watch firing on an aborted change.
                _orig(self_obj, name, value)
                if _value_hash(old_val) == _value_hash(value):
                    return
                # Build the user-visible watch_name. Specific entries report
                # `aw.expr` verbatim; wildcard appends the actual attribute
                # so the user sees `obj.foo` rather than just `obj`.
                watch_name = aw.expr if not wildcard else f"{aw.expr}.{name}"
                _reg._guard.active = True
                try:
                    caller = sys._getframe(1)
                    _reg._handle_hit(
                        user_frame=caller,
                        watch_name=watch_name,
                        old_repr=_safe_repr(old_val),
                        new_repr=_safe_repr(value),
                        source_file=caller.f_code.co_filename,
                        source_line=caller.f_lineno,
                    )
                finally:
                    _reg._guard.active = False

            try:
                cls.__setattr__ = patched_setattr
            except (TypeError, AttributeError) as e:
                _log_warn(
                    f"classpatch install failed: cannot set __setattr__ on "
                    f"{cls.__name__} ({e!r}). Watch will fall back further."
                )
                return False
            _classpatch_registry[cls] = patch

        patch.instance_watches.setdefault(target_id, {})[key] = attr_watch
        return True


def _remove_classpatch_attr_watch(target_obj: Any, key: str) -> None:
    """Reverse one classpatch install. Restores the original `__setattr__`
    on the class when the last watched instance is removed.

    Safe to call on a target that was never patched – silently no-ops.
    """
    cls = type(target_obj)
    with _classpatch_lock:
        patch = _classpatch_registry.get(cls)
        if patch is None:
            return
        target_id = id(target_obj)
        entries = patch.instance_watches.get(target_id)
        if entries:
            entries.pop(key, None)
            if not entries:
                patch.instance_watches.pop(target_id, None)
        if patch.instance_watches:
            return
        # Last watch on this class — restore the original `__setattr__`.
        try:
            if patch.had_own_setattr:
                cls.__setattr__ = patch.original_setattr_in_cls_dict
            else:
                # No original on the class; delete our patched one so MRO
                # lookup resumes finding the parent's `__setattr__`.
                del cls.__setattr__
        except (TypeError, AttributeError):
            # If we can't restore, the patched `__setattr__` becomes a
            # functional no-op since `instance_watches` is now empty – the
            # next call falls into the `if not entries: _orig(...)` branch
            # and behaves identically to the unpatched original. We tolerate
            # this rather than fail the unwatch.
            pass
        _classpatch_registry.pop(cls, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_repr(value: Any) -> str:
    """Return a short repr of value, safe against exceptions."""
    try:
        r = repr(value)
        return r if len(r) <= 200 else r[:200] + "..."
    except Exception:
        return "<unprintable>"


def _log_warn(msg: str) -> None:
    """Write a one-line `[WATCHPOINT]` warning to stderr, never raise."""
    try:
        print(f"[WATCHPOINT] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


# Code-object flags that mean "calling this doesn't enter the body" – the
# call returns a generator / coroutine / async-generator instead. We use
# these in `_on_call` to skip propagation, since PY_START would not fire as
# a continuation of CALL for these callees (it fires later, on iteration /
# awaiting, in a different stack context). Defined as a module-level
# constant so the per-call cost is a single AND, not a getattr lookup.
import inspect as _inspect
_LAZY_BODY_FLAGS = (
    _inspect.CO_GENERATOR
    | _inspect.CO_COROUTINE
    | _inspect.CO_ASYNC_GENERATOR
)


def _python_code_for_call(callable_: Any) -> Any:
    """Return the Python code object whose PY_START will fire as the next
    event after a CALL on `callable_`, or None if there's no such code
    (C function, partial without a Python wrapper, etc.).

    Handles three common shapes:
    - plain function / bound method / lambda: `callable_.__code__`
    - class instantiation: the new instance is built and the Python
      `__init__` runs first; its code is `callable_.__init__.__code__`
    - everything else: skipped (no propagation)
    """
    code = getattr(callable_, "__code__", None)
    if code is not None:
        return code
    if isinstance(callable_, type):
        # Class call: propagate into the (possibly-inherited) Python __init__.
        # If __init__ is a C-implemented slot wrapper (e.g. object.__init__),
        # it has no __code__ and we silently skip.
        init = getattr(callable_, "__init__", None)
        return getattr(init, "__code__", None) if init is not None else None
    return None


# Built-in / primitive types that should always use local-variable rebinding
# detection rather than object-wide __class__ surgery. Surgery would fail on
# these anyway, but we filter eagerly so watch() doesn't incur the eval + try
# overhead for the common case of `watch("x")` where x is an int.
_LOCAL_WATCH_BUILTIN_TYPES = (
    type(None), bool, int, float, complex, str, bytes, bytearray,
    list, tuple, dict, set, frozenset,
)


# How many levels of nested user-defined attributes `_instrument_object_tree`
# walks beneath the root watch. Depth 0 is the root itself (installed by
# the caller); depth 1 is its direct attrs, depth 2 its grandchildren, etc.
# 4 levels deep matches `pythonvartracker`'s reference value – deep enough
# for typical DTO graphs without runaway instrumentation cost on huge object
# trees. Cycle guard is independent (per-call `visited` set).
_RECURSIVE_OBJECT_WATCH_DEPTH = 4


def _safe_iter_dict_attrs(obj: Any):
    """Yield non-dunder attribute names from `obj.__dict__`.

    Used by `_instrument_object_tree` to discover the candidate child
    attributes of a user-defined object. Silently no-ops for objects with
    no `__dict__` (slotted classes, builtin instances) – they don't expose
    a uniform attribute iterator we can trust, and slot descriptors are
    out of scope for recursive object instrumentation today.
    """
    try:
        d = obj.__dict__
    except AttributeError:
        return
    # Snapshot to a list to avoid "dict changed size during iteration" if
    # a side-effect path mutates the dict mid-iteration.
    for name in list(d.keys()):
        # Skip dunder attrs – walking `__class__` would loop, and most
        # other dunders are framework internals the user isn't watching.
        if name.startswith("__") and name.endswith("__"):
            continue
        yield name


def _is_object_watchable(value: Any) -> bool:
    """Return True if `value` is a user-defined object worth instrumenting
    via __class__ surgery (i.e. watch('name') should mean 'fire on any
    attribute mutation', not 'fire on name rebinding').

    Heuristic: not None, not a known primitive / container, AND its type is
    not declared in the builtins module. This catches the typical case of
    a Flask Request, a Django QuerySet, a domain object, etc., while leaving
    ints / strings / lists / dicts on the local-variable code path.
    """
    if value is None:
        return False
    if isinstance(value, _LOCAL_WATCH_BUILTIN_TYPES):
        return False
    return type(value).__module__ != "builtins"


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


def _detect_debugger() -> str:
    """Identify the live debugger backend driving this interpreter.

    Returns one of:
        'pydevd'  – PyCharm's native debugger; `_pause_via_pydevd` is usable.
        'debugpy' – Microsoft's debugger (PyCharm 2025+ beta mode, VS Code).
                    `_pause_via_pydevd` is NOT usable; use the debugpy backend.
        'none'    – no debugger detected (plain `python script.py`, pytest, etc.).
                    `_handle_hit` falls back to raising WatchpointHit.

    Order matters: debugpy bundles its own copy of pydevd internally, so we
    check for the top-level `debugpy` module first. Only if it's absent do we
    treat a present `pydevd` as the canonical pydevd backend.
    """
    if "debugpy" in sys.modules:
        return "debugpy"
    if _get_pydevd_debugger() is not None:
        return "pydevd"
    return "none"


def _pycharm_watchpoint_diag() -> str:
    """Return a short diagnostic the user can evaluate from the debugger."""
    import sys as _sys
    in_mod = "pydevd" in _sys.modules
    bundle_in_mod = "_pydevd_bundle.pydevd_constants" in _sys.modules
    debugpy_in_mod = "debugpy" in _sys.modules
    tr = _sys.gettrace()
    tr_owner = type(getattr(tr, "__self__", None)).__name__ if tr is not None else "None"
    debugger = _get_pydevd_debugger()
    return (
        f"backend: {_detect_debugger()}; "
        f"pydevd in sys.modules: {in_mod}; "
        f"_pydevd_bundle.pydevd_constants in sys.modules: {bundle_in_mod}; "
        f"debugpy in sys.modules: {debugpy_in_mod}; "
        f"sys.gettrace owner: {tr_owner}; "
        f"get_global_debugger -> {type(debugger).__name__ if debugger else 'None'}; "
        f"last_error: {_pydevd_last_error}"
    )


def _pycharm_log_state() -> str:
    """Print the runtime state to stderr (visible in the Debug Console) and
    return a short confirmation.

    Why this exists: under debugpy, `XValue.toString()` on the IDE side returns
    empty for many DAP-evaluated expressions, so a caller that just evaluates
    `_pycharm_watchpoint_state()` and tries to read the result string gets
    nothing back. Printing to stderr instead routes the diagnostic into the
    Debug Console where the user can actually see it, and works identically
    across pydevd / debugpy / no-debugger.
    """
    msg = _pycharm_watchpoint_state()
    try:
        print(f"[WATCHPOINT/probe] {msg}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass
    return "OK"


def _pycharm_watchpoint_state() -> str:
    """Full state dump for diagnosing why a watch did/didn't fire.

    Returns everything we'd want to know on a "nothing happens" bug report:
    detected debugger, claimed sys.monitoring tool ID, which tools own the
    other IDs, current watch registry contents, and the standard pydevd diag.
    Designed to be safe to call from the IDE evaluator while paused under
    any debugger backend; never raises.
    """
    try:
        tools = []
        for i in range(6):
            try:
                tools.append((i, sys.monitoring.get_tool(i)))
            except Exception as e:  # noqa: BLE001
                tools.append((i, f"<err:{e!r}>"))
        local_keys = list(_registry._local_watches.keys())
        attr_keys = list(_registry._attr_watches.keys())
        return (
            f"backend: {_detect_debugger()}; "
            f"tool_id: {_TOOL_ID}; "
            f"tools: {tools}; "
            f"local_watches: {local_keys}; "
            f"attr_watches: {attr_keys}; "
            f"diag: {_pycharm_watchpoint_diag()}"
        )
    except Exception as e:  # noqa: BLE001
        return f"_pycharm_watchpoint_state ERROR: {e!r}"


def _pause_via_pydevd(py_db: Any, user_frame: Any, watch_name: str,
                      old_repr: str, new_repr: str,
                      source_file: str, source_line: int) -> None:
    """Hand off to pydevd's own tracer to pause cleanly at the next user line.

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
    # `set_additional_thread_info` is re-exported by PyCharm's pydevd through
    # `pydevd_trace_dispatch`, but debugpy's vendored pydevd does NOT re-export
    # it there – we hit `ImportError: cannot import name 'set_additional_thread_info'`
    # on the very first watchpoint hit under debugpy. The function itself exists
    # in both flavors at this canonical location, so import it directly.
    from _pydevd_bundle.pydevd_additional_thread_info import set_additional_thread_info

    if getattr(py_db, "_finish_debugging_session", False):
        # Debugger is shutting down – don't try to suspend.
        return

    thread = threading.current_thread()
    info = set_additional_thread_info(thread)

    # Reentrancy guard – pydevd's own tracer may already be mid-callback on
    # this thread; let it finish before we try to overlay our suspend.
    if getattr(info, "is_tracing", False):
        return

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
    safety_limit = 32  # bound against degenerate f_back chains
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

    # Arm pydevd's full `trace_dispatch` on user_frame. Pydevd's CALL handler
    # often parks user_frame's `f_trace` at `trace_exception` (a fast-path
    # that only handles exception events – no step_cmd check) when the frame
    # has no in-line breakpoints. For our CMD_STEP_OVER hint above to fire,
    # user_frame's `f_trace` must be the FULL `trace_dispatch`. We set it
    # directly first (the most reliable mechanism), then call the official
    # API which handles the parent chain. `set_trace_for_frame_and_parents`
    # has several silent early-return paths (PEP 669 mode, filtered files,
    # etc.) so we can't rely on it alone to update user_frame's f_trace.
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


def _value_hash(value: Any) -> int:
    """Stable hash for change-detection.

    Strategy:
    - Hashable immutable types (None / bool / int / float / str / bytes /
      complex / tuple / frozenset): hash((type, value)). The type tag prevents
      `1 == True` and `1 == 1.0` collisions from masking a real type change.
    - Mutable containers (list / dict / set / bytearray): hash(repr(value)) so
      content changes are detected. O(n) in the container size, but the cost
      is paid per line of ONE watched function – not globally.
    - Custom objects: id(value) XOR hash(type qualname). Rebinding to a new
      object of the same type yields a different hash; in-place mutation is
      NOT detected (that requires an attribute watch).
    """
    tp = type(value)
    if tp in (type(None), bool, int, float, str, bytes, complex, tuple, frozenset):
        try:
            return hash((tp, value))
        except TypeError:
            return id(value)
    if tp in (list, dict, set, bytearray):
        try:
            return hash(repr(value))
        except Exception:
            return id(value)
    return id(value) ^ hash(tp.__qualname__)


# ---------------------------------------------------------------------------
# sys.monitoring setup
# ---------------------------------------------------------------------------

_TOOL_ID: Optional[int] = None


def _setup_monitoring(registry: "WatchpointRegistry") -> None:
    """Claim a sys.monitoring tool ID and register callbacks.

    No global events are set here – LINE and PY_RETURN are only enabled
    per-code-object when watch() is called.

    Tool-ID priority (lowest conflict first):
    - 5: unnamed application slot, almost always free
    - 4: OPTIMIZER_ID on 3.14, unnamed application slot on 3.12/3.13
    - 3: BRANCH_COVERAGE_ID on 3.13+, unnamed on 3.12
    - PROFILER_ID (2): only if nothing else is free
    - COVERAGE_ID (1): last resort – claiming this silently disables coverage.py
    - DEBUGGER_ID (0) is intentionally never tried; pydevd / PyCharm own it.
    """
    global _TOOL_ID
    if _TOOL_ID is not None:
        return  # already initialised – guard against double import / reload

    monitoring = sys.monitoring
    candidates = [5, 4, 3, monitoring.PROFILER_ID, monitoring.COVERAGE_ID]
    for tid in candidates:
        try:
            monitoring.use_tool_id(tid, "pycharm_watchpoints")
            _TOOL_ID = tid
            break
        except ValueError:
            continue

    if _TOOL_ID is None:
        raise RuntimeError("watchpoint.py: no free sys.monitoring tool ID available.")

    monitoring.register_callback(_TOOL_ID, monitoring.events.LINE, registry._on_line)
    monitoring.register_callback(
        _TOOL_ID, monitoring.events.PY_RETURN, registry._on_py_return
    )
    monitoring.register_callback(
        _TOOL_ID, monitoring.events.PY_START, registry._on_py_start
    )
    monitoring.register_callback(
        _TOOL_ID, monitoring.events.CALL, registry._on_call
    )
    # Deliberately NOT calling set_events() – zero global overhead until watch() fires.


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
    _registry.add_watch(expr, frame)


def unwatch(expr: str) -> None:
    """Remove the watchpoint for expr. Silently ignored if not watching expr."""
    _registry.remove_watch(expr)


def clear_watches() -> None:
    """Remove all active watchpoints."""
    _registry.clear_watches()


def _find_paused_user_frame(file_hint: str, func_hint: str) -> Any:
    """Locate the paused user frame matching `file_hint` (and, if available,
    `func_hint`).

    Used by `watch_at` (PyCharm plugin entry point): when a watch is added
    via the IDE's "Add Python Watchpoint" action, the expression is evaluated
    by pydevd in a context that DOES NOT include the user's actual frame on
    its sys._getframe() stack – the eval frame's f_back leads back into
    pydevd, not into user code. We have to recover the user frame ourselves
    by scanning every running thread's top frame and walking up.

    `func_hint` may be empty: under debugpy the IDE-side `AddWatchpointAction`
    sometimes cannot extract the function name from a DAP stack frame, so it
    passes "" and asks us to match by file alone. When that happens we accept
    any frame in the matching file – `_frame_depth` then prefers the shallowest
    candidate (the outer invocation), which is virtually always the one the
    user is paused in unless they were inside a recursion. Recursion with an
    empty func_hint will pick the outermost call, not the current one – an
    acknowledged limitation of running without a func hint.

    Both endpath comparisons use `endswith` so the IDE-provided path can be
    absolute while the running interpreter sees a slightly different one
    (e.g. /private/var/... vs /var/... on macOS).
    """
    import threading
    target_basename = file_hint.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    candidates = []
    match_func = func_hint != ""  # empty string = file-only match (debugpy fallback)
    for tid, top_frame in sys._current_frames().items():
        f = top_frame
        while f is not None:
            cf = f.f_code.co_filename
            file_matches = (
                cf == file_hint
                or cf.endswith(file_hint)
                or file_hint.endswith(cf)
                or cf.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] == target_basename
            )
            func_matches = (not match_func) or f.f_code.co_name == func_hint
            if file_matches and func_matches:
                candidates.append(f)
            f = f.f_back
    if not candidates:
        raise RuntimeError(
            f"watchpoint: could not locate paused frame for "
            f"{func_hint or '<any func>'}() in {file_hint}"
        )
    # If multiple matches (recursion), prefer the SHALLOWEST – it's the outer
    # invocation and most likely the one the user is paused in.
    candidates.sort(key=lambda fr: _frame_depth(fr))
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
    _dbg_log(f"watch_at entry: expr={expr!r}, file_hint={file_hint!r}, func_hint={func_hint!r}")
    try:
        frame = _find_paused_user_frame(file_hint, func_hint)
        _dbg_log(
            f"watch_at: located frame id={id(frame)} "
            f"({frame.f_code.co_filename}:{frame.f_code.co_name})"
        )
    except Exception as e:
        # On lookup failure dump every thread's top-of-stack so we can see WHY
        # the heuristics in _find_paused_user_frame missed – under debugpy the
        # filename PyCharm hands us may not match what sys._current_frames sees.
        if _dbg_enabled():
            import threading as _th
            current = sys._current_frames()
            for tid, top in current.items():
                tname = next((t.name for t in _th.enumerate() if t.ident == tid), "?")
                _dbg_log(
                    f"watch_at: thread {tid} ({tname}) top frame "
                    f"{top.f_code.co_filename}:{top.f_code.co_name}"
                )
        _dbg_log(f"watch_at: frame lookup FAILED: {e}")
        return f"ERROR: {e}"
    try:
        _registry.add_watch(expr, frame)
        _dbg_log(
            f"watch_at: add_watch returned. "
            f"local_watches now: {list(_registry._local_watches.keys())} "
            f"attr_watches now: {list(_registry._attr_watches.keys())}"
        )
    except Exception as e:
        _dbg_log(f"watch_at: add_watch FAILED: {e!r}")
        return f"ERROR: add_watch failed: {e}"
    return f"OK: watching {expr!r} in {func_hint}()"


def _hit_file_path() -> str:
    """Canonical path for the last-hit JSON file.

    When the IDE plugin sets `PYCHARM_WATCHPOINT_HIT_DIR`, write into that
    directory – it's the per-session temp dir the plugin already created (and
    knows the path of), so the highlighter can read back without any
    out-of-band PID extraction. Falls back to a PID-derived path in the system
    temp dir when the env var isn't set (e.g. running under plain pytest with
    no debugger), which makes the function safe to call from the test suite.
    """
    import os, tempfile
    plugin_dir = os.environ.get("PYCHARM_WATCHPOINT_HIT_DIR")
    if plugin_dir:
        return os.path.join(plugin_dir, "lasthit.json")
    return os.path.join(tempfile.gettempdir(), f"pycharm_watchpoint_lasthit_{os.getpid()}.json")


def _emit_hit_marker(hit: dict) -> None:
    """Publish the hit info to the IDE through two channels.

    1. **Append-only JSON-lines file** (primary channel). Each hit is appended
       as a single JSON object on its own line. The IDE plugin drains the file
       (reads all lines, deletes) on every sessionPaused, so a pause that
       coalesces multiple in-flight hits renders all of them.

       Why append rather than overwrite: under pydevd's `CMD_STEP_OVER +
       step_stop = user_frame` mechanism, pause materializes at the next LINE
       event in the user frame – which doesn't fire if subsequent mutations
       happen on consecutive lines with no intermediate non-mutation lines.
       Under debugpy with PEP 669, multiple `step_stop` resets across hits
       coalesce into a single pause. Both cases caused the single-slot file to
       lose all hits except the most recent. Append mode preserves every hit.

       Files are still per-session (PYCHARM_WATCHPOINT_HIT_DIR/lasthit.json),
       so multiple concurrent watchpoint sessions don't share state.

    2. `[WATCHPOINT/event]<base64>` line to stderr (secondary, Debug Console
       visibility + fallback for backends where the file path isn't reachable).
       Same NUL-separated encoding as the legacy `_pycharm_consume_last_hit`.

    Both channels survive fd-level output capture (pytest's default), since
    the file write is a direct filesystem operation untouched by stdio
    redirection.
    """
    import base64 as _b64
    import json as _json

    # File channel (primary). Append one JSON object per line. No atomic-rename
    # gymnastics needed because append + line-buffered writes are themselves
    # atomic at the line level on local filesystems; the IDE-side reader splits
    # on newlines and ignores any partial trailing line.
    try:
        target = _hit_file_path()
        with open(target, "a") as fh:
            fh.write(_json.dumps(hit) + "\n")
    except Exception:  # noqa: BLE001
        pass

    # Stderr marker (secondary).
    try:
        parts = [
            hit["file"],
            str(hit["line"]),
            hit["name"],
            hit["old"],
            hit["new"],
        ]
        raw = "\x00".join(parts).encode("utf-8")
        encoded = _b64.b64encode(raw).decode("ascii")
        print(f"[WATCHPOINT/event]{encoded}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass


def _pycharm_consume_last_hit() -> str:
    """Return the most recent watchpoint hit, base64-encoded, then clear it.

    Empty string ⇒ no pending hit (typical when the pause was triggered by a
    regular breakpoint instead of a watchpoint). Otherwise the decoded payload
    is a UTF-8 string of five `\\x00`-separated fields: file, line, name, old,
    new. Reading consumes the hit so the same change is never highlighted twice.

    Why base64 + NUL-separated and not JSON: pydevd renders the evaluator
    result via repr(), which means any string value would arrive on the Kotlin
    side wrapped in Python-style quoting with backslash-escaped specials.
    Base64'ing the payload reduces decoding on the IDE side to "strip the
    outer quotes pydevd added, then base64-decode" – no JSON parser, no
    escape-sequence handling, no Gson dependency.
    """
    reg = _registry
    with reg._lock:
        hit = reg._last_hit
        reg._last_hit = None
    if hit is None:
        return ""
    import base64 as _b64
    parts = [
        hit["file"],
        str(hit["line"]),
        hit["name"],
        hit["old"],
        hit["new"],
    ]
    raw = "\x00".join(parts).encode("utf-8")
    return _b64.b64encode(raw).decode("ascii")


# Expose via builtins so PyCharm plugin can call them without importing.
builtins._pycharm_watch = watch
builtins._pycharm_watch_at = watch_at
builtins._pycharm_unwatch = unwatch
builtins._pycharm_clear_watches = clear_watches
builtins._pycharm_watchpoint_diag = _pycharm_watchpoint_diag
builtins._pycharm_watchpoint_state = _pycharm_watchpoint_state
builtins._pycharm_log_state = _pycharm_log_state
builtins._pycharm_consume_last_hit = _pycharm_consume_last_hit

# Announce backend at load time. Visible in the Debug Console alongside the
# existing `[WATCHPOINT] Loaded in process N` line emitted by sitecustomize.
# Crucial under debugpy: gives us proof the runtime actually loaded in the
# user-code interpreter (vs. some debugpy sidecar process).
#
# Heads-up about the detected value at this point: sitecustomize runs during
# Python startup, BEFORE the debug bootstrap (pydevd or debugpy) imports its
# debugger module. So under debugpy especially, this almost always prints
# `boot-backend: none` – it's not lying, debugpy literally isn't loaded yet.
# `_detect_debugger()` is a live lookup, so every later call (incl. inside
# `_handle_hit`) sees the actual backend.
try:
    print(
        f"[WATCHPOINT] Runtime ready – boot-backend: {_detect_debugger()} "
        f"(re-detected lazily on each event); "
        f"hit-dir: {os.environ.get('PYCHARM_WATCHPOINT_HIT_DIR') or '<unset>'}",
        file=sys.stderr,
        flush=True,
    )
except Exception:  # noqa: BLE001
    pass
