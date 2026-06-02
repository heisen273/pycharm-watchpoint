"""The :class:`WatchpointRegistry` – all watch state + the sys.monitoring
callbacks – and ``_setup_monitoring`` which claims the tool id."""


import sys
import os
import threading
import weakref
import builtins
from typing import Any, Optional, Tuple


from . import constants
from . import pydevd_pause
from .constants import Any, Optional, _monitoring, sys, threading
from .hit import WatchpointHit
from .helpers import _LAZY_BODY_FLAGS, _MAX_FRAME_WALK_HOPS, _MAX_HIT_QUEUE_SIZE, _MAX_PROPAGATION_QUEUE_SIZE, _RECURSIVE_OBJECT_WATCH_DEPTH, _is_object_watchable, _is_user_defined_type, _log_warn, _python_code_for_call, _safe_iter_dict_attrs, _safe_repr, _try_add_sub_watch, _value_hash
from .caller import _find_user_caller, _find_user_code_caller, _is_pydevd_internal, _is_runtime_filename
from .pydevd_pause import _next_code_line_after_frame, _next_code_line_in, _pycharm_watchpoint_diag, _pydevd_last_error
from .watch_data import _AttributeWatch, _LocalWatch
from .containers import _CONTAINER_TYPES, _WATCHED_CONTAINER_TYPES, _unwrap_container, _wrap_container
from .classpatch import _install_classpatch_attr_watch, _remove_classpatch_attr_watch


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

        # Hit QUEUE – every hit since `_pycharm_consume_last_hit` last drained
        # entries matching the IDE's current pause location. Each hit carries
        # its own `bp_anchor_file` / `bp_line` tagging the pydevd
        # `LineBreakpoint` we installed for it; the drain filters by those
        # fields so only the hit whose bp triggered THIS pause gets returned.
        # Hits whose bps haven't fired yet stay queued for a future pause.
        #
        # Sequential pre-emptive bps (design contract §11, post-v8): when N
        # back-to-back mutations all walk up to the SAME user-code anchor
        # frame, each one is given its OWN bp at the NEXT available code
        # line after the previous hit's bp. Hit 1 → anchor's next line; hit
        # 2 → line after hit 1's bp_line; hit 3 → line after hit 2; etc.
        # This is what makes "4 attribute writes inside one library call"
        # surface as four separate pauses instead of one – previously a
        # process-level `_pause_pending` gate dropped hits 2-N silently.
        self._hit_queue: list = []

        # Track temp breakpoints installed by `_install_bp_at` so we can
        # remove them in `_pycharm_consume_last_hit` (when the matching
        # pause has been consumed) and in `clear_watches` (test isolation).
        # List of `(file, line, bp_id)` tuples. Each entry is one
        # `LineBreakpoint` we installed via `py_db.consolidate_breakpoints`.
        # Drain is selective: only bps at the IDE's current pause location
        # are removed, leaving sibling bps armed for their future pauses.
        # Cleaned up best-effort: a leaked entry just means a phantom bp
        # the user has to remove manually, not a correctness issue.
        self._temp_breakpoints: list = []

        # Direct-pause mechanism: maps (id(code_object), line) → True for
        # bp targets we want to pause at. When our own _on_line callback
        # (fired on _TOOL_ID, independent of pydevd's DEBUGGER_ID) sees a
        # matching (code, line), it calls do_wait_suspend directly on the
        # frame – completely bypassing pydevd's py_line_callback which may
        # fail to fire for library code mid-execution (the csp mystery).
        # Entries are added by `_install_bp_at` and removed by
        # `_pycharm_consume_last_hit` or when our _on_line fires.
        self._bp_pause_pending: dict = {}

        # Breakpoint slot reservations: {(id(code_object), line)} selected by
        # `_compute_bp_targets` but not yet reflected in `_hit_queue` as an
        # installed bp. This closes a narrow race where two threads computed
        # targets while the queue was still empty and both installed at the
        # same line. Reservations are released as soon as install succeeds
        # (the queued `bp_locations` becomes the durable owner) or fails.
        self._bp_slot_reservations: set = set()

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
                    # Also install a local-variable watch so that rebinding
                    # the name to a different object is detected. The object
                    # watch tracks attribute mutations; the local watch tracks
                    # identity changes (id-based hash differs on rebind).
                    fid = id(frame)
                    if (expr, fid) in self._local_watches:
                        self._remove_local_watch_locked((expr, fid))
                    self._add_local_watch(expr, frame)
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
            self._hit_queue.clear()
            # Snapshot + clear the temp breakpoint list under the lock;
            # actual pydevd remove happens after we release `_lock` so we
            # don't hold it across pydevd's own locks.
            bps_to_remove = list(self._temp_breakpoints)
            self._temp_breakpoints.clear()
            self._bp_pause_pending.clear()
            self._bp_slot_reservations.clear()
        # Best-effort remove of any temp pydevd breakpoints we'd installed.
        # If pydevd isn't loaded (the test path) this is a no-op.
        if bps_to_remove:
            py_db = pydevd_pause._get_pydevd_debugger()
            if py_db is not None:
                pydevd_pause._remove_temp_breakpoints(py_db, bps_to_remove)
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
            constants._TOOL_ID, code,
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
                    if type(value) in _CONTAINER_TYPES and not isinstance(value, _WATCHED_CONTAINER_TYPES):
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
                            user_caller = _find_user_caller(sys._getframe(1))
                            if user_caller is not None:
                                _registry_self._handle_hit(
                                    user_frame=user_caller,
                                    watch_name=_expr,
                                    old_repr=_safe_repr(old_val),
                                    new_repr=_safe_repr(value),
                                    source_file=user_caller.f_code.co_filename,
                                    source_line=user_caller.f_lineno,
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
        if type(initial_val) in _CONTAINER_TYPES and not isinstance(initial_val, _WATCHED_CONTAINER_TYPES):
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
        if (type(initial_val) in _CONTAINER_TYPES
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
        # Seed the persistent visited set with the root object's id so the
        # initial walk + every later __setattr__-triggered re-entry share a
        # single cycle-detection set. See `_AttributeWatch.visited_ids` for
        # why this lives on the watch (not as a per-call argument).
        root_watch.visited_ids.add(id(obj))
        self._instrument_object_tree(
            obj, root_expr=expr, current_path=expr,
            depth=1, root_watch=root_watch,
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
        # The `__class__` swap itself is a setattr on `obj`. If `obj` is
        # already wrapped by a previously-installed watcher (this method
        # is called recursively from `_instrument_object_tree` for nested
        # attrs, so the holder's watcher may be active), the assignment
        # would fire that watcher's `__setattr__` and queue a spurious hit.
        # Set the per-thread guard around the swap so the parent watcher
        # treats this as a silent rewrite.
        prev_guard = getattr(self._guard, "active", False)
        self._guard.active = True
        try:
            try:
                obj.__class__ = watcher_cls
            except (TypeError, AttributeError) as e:
                # Frozen dataclasses raise `FrozenInstanceError` (an
                # AttributeError subclass) from their custom __setattr__;
                # some exotic classes raise plain TypeError if their layout
                # forbids the swap. Convert either into a TypeError that
                # `add_watch` catches and falls back to local-variable
                # detection.
                raise TypeError(
                    f"Cannot watch object '{expr}': __class__ surgery failed on "
                    f"{original_cls.__name__} ({e})."
                ) from e
        finally:
            self._guard.active = prev_guard

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
                if type(value) in _CONTAINER_TYPES and not isinstance(value, _WATCHED_CONTAINER_TYPES):
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
                        if root_watch is not None and not root_watch.sub_watches_capped:
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
                                _try_add_sub_watch(root_watch, sub_w)
                            elif (_is_object_watchable(wrapped_value)
                                    and not isinstance(wrapped_value, type)
                                    and _is_user_defined_type(type(wrapped_value))
                                    and id(wrapped_value) not in root_watch.visited_ids):
                                # Same framework / class-object / cycle gates as
                                # `_instrument_object_tree` – this `__setattr__`
                                # path is the secondary entry point and used to
                                # reset depth + visited on every call, which is
                                # exactly how the Django-shaped explosion
                                # snowballed. Reuse `root_watch.visited_ids` so
                                # cycle detection survives across re-entries.
                                root_watch.visited_ids.add(id(wrapped_value))
                                try:
                                    sub_w = _registry_self._install_single_object_watch(
                                        sub_expr, wrapped_value, root_expr=_root_expr,
                                    )
                                    if _try_add_sub_watch(root_watch, sub_w):
                                        _registry_self._instrument_object_tree(
                                            wrapped_value, root_expr=_root_expr,
                                            current_path=sub_expr, depth=1,
                                            root_watch=root_watch,
                                        )
                                except TypeError:
                                    # Frozen / slotted / etc. – skip
                                    # auto-instrumentation but the rebind
                                    # already fired so the user knows.
                                    pass
                        # Walk past our own `<string>`-exec'd frames to find
                        # the user's mutation site. If the entire f_back
                        # chain is runtime (descriptor side-effect during
                        # IDE display, etc.), drop the hit silently – the
                        # mutation didn't originate from user code and
                        # firing a hit would just flood pydevd's queue.
                        user_caller = _find_user_caller(sys._getframe(1))
                        if user_caller is not None:
                            _registry_self._handle_hit(
                                user_frame=user_caller,
                                watch_name=sub_expr,
                                old_repr=_safe_repr(old_val),
                                new_repr=_safe_repr(wrapped_value),
                                source_file=user_caller.f_code.co_filename,
                                source_line=user_caller.f_lineno,
                            )
                    finally:
                        _registry_self._guard.active = False
                else:
                    super().__setattr__(name, wrapped_value)

        return _WatchedAnyAttrSubclass

    def _instrument_object_tree(self, obj: Any, root_expr: str, current_path: str,
                                depth: int,
                                root_watch: "_AttributeWatch") -> None:
        """Recursively instrument `obj`'s nested user-defined attributes.

        For every attribute on `obj.__dict__`:
        - `list` / `dict` / `set` value → wrap in `_WatchedList/Dict/Set`;
          record a container-wrap sub-watch in `root_watch.sub_watches`.
        - User-defined-object value (per `_is_object_watchable` AND
          `_is_user_defined_type`, AND not a class object) → install
          class surgery (`_install_single_object_watch`); record the
          resulting sub-watch; recurse one level deeper.
        - Framework objects (Django QuerySet, SQLAlchemy session,
          stdlib types, etc. – anything `_is_user_defined_type` rejects),
          class objects, primitives, already-watched values → skip.

        Depth-capped at `_RECURSIVE_OBJECT_WATCH_DEPTH` and cycle-guarded
        by `root_watch.visited_ids` (shared across this walk AND every
        later `__setattr__`-triggered re-entry, so a graph like
        `a.left = a.right` is instrumented exactly once for the lifetime
        of the watch – not once per re-entry).

        Bails out if `root_watch.sub_watches_capped` is set, which
        `_try_add_sub_watch` flips on the first time the per-root sub-
        watch count would exceed `_MAX_SUB_WATCHES_PER_ROOT`. Belt-and-
        suspenders against any cycle the type filter + visited set miss.
        """
        if depth > _RECURSIVE_OBJECT_WATCH_DEPTH:
            return
        if root_watch.sub_watches_capped:
            return
        for attr_name in _safe_iter_dict_attrs(obj):
            try:
                attr_value = getattr(obj, attr_name, None)
            except Exception:
                continue
            if attr_value is None:
                continue
            sub_expr = f"{current_path}.{attr_name}"

            if (type(attr_value) in _CONTAINER_TYPES
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
                    if not _try_add_sub_watch(root_watch, sub_w):
                        return
                continue

            if not _is_object_watchable(attr_value):
                continue
            # Don't recurse into class objects (their __dict__ is
            # descriptors, not data; walking it would resolve every
            # property/classmethod/descriptor and chain explosively –
            # especially under ORMs where class-level descriptors
            # fabricate proxy instances on access).
            if isinstance(attr_value, type):
                continue
            # Don't recurse into framework / stdlib / site-packages
            # types. The user wants coverage of THEIR code; framework
            # internals do their own setattrs as side effects of attr
            # reads (Django descriptors, SQLAlchemy lazy loaders, etc.)
            # which would re-trigger our watcher in an unbounded loop.
            # See `_is_user_defined_type` for the heuristic.
            if not _is_user_defined_type(type(attr_value)):
                continue
            if id(attr_value) in root_watch.visited_ids:
                continue
            root_watch.visited_ids.add(id(attr_value))
            try:
                sub_w = self._install_single_object_watch(
                    sub_expr, attr_value, root_expr=root_expr,
                )
            except TypeError:
                continue
            if not _try_add_sub_watch(root_watch, sub_w):
                return
            self._instrument_object_tree(
                attr_value, root_expr=root_expr, current_path=sub_expr,
                depth=depth + 1, root_watch=root_watch,
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
                _monitoring.set_local_events(constants._TOOL_ID, w.code, 0)
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
            # bound. See `_MAX_PROPAGATION_QUEUE_SIZE` for rationale.
            if len(queue) > _MAX_PROPAGATION_QUEUE_SIZE:
                del queue[0]

            # Enable the same set of events on the callee's code object so
            # the propagated watch behaves identically to a directly-armed
            # one, and so further calls FROM the callee can propagate again.
            try:
                _monitoring.set_local_events(
                    constants._TOOL_ID, callee_code,
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
            # ── Direct-pause mechanism ───────────────────────────────────
            # Check if this (code, line) is a pending bp pause target. If
            # so, trigger do_wait_suspend directly – bypassing pydevd's
            # py_line_callback which may silently fail for library code
            # whose PY_START returned DISABLE before our bp was installed
            # (the csp/_make_nonce mystery: py_line_callback fires for
            # pydevd's DEBUGGER_ID but our set_local_events on DEBUGGER_ID
            # doesn't reliably re-arm mid-execution instrumentation in all
            # CPython builds; our OWN tool's callback always fires because
            # we arm it fresh with no prior DISABLE history).
            bp_key = (id(code), line_number)
            if bp_key in self._bp_pause_pending:
                # Remove first so re-entrant calls don't double-fire.
                self._bp_pause_pending.pop(bp_key, None)
                # ── Hit bp cleanup ─────────────────────────────────────
                # When a hit has multiple bps (primary + safety-net), the
                # first one to fire must remove ALL pydevd bps for that hit
                # (both siblings AND the fired bp itself). Two reasons:
                #
                # 1. Sibling disarm: prevents the unfired safety-net from
                #    causing a spurious pause when execution reaches it later.
                # 2. Fired bp removal: prevents pydevd's own DEBUGGER_ID
                #    py_line_callback from seeing the LineBreakpoint and
                #    causing a SECOND pause at the same location. This is
                #    the dual-path architectural fix – our _TOOL_ID callback
                #    and pydevd's DEBUGGER_ID callback both fire for LINE
                #    events in user code, and only removing the bp itself
                #    prevents the double-pause.
                #
                # Our _trigger_direct_pause uses do_wait_suspend directly –
                # it does NOT need the pydevd LineBreakpoint to be present.
                # So removing it before calling _trigger_direct_pause is safe.
                fired_file = code.co_filename
                hit_bps_to_remove = []
                with self._lock:
                    for h in self._hit_queue:
                        locs = h.get("bp_locations", [])
                        if any(l[0] == fired_file and l[1] == line_number
                               for l in locs):
                            # Found the owning hit – remove ALL pydevd bps
                            # (fired + siblings). The fired bp must also be
                            # removed: our _trigger_direct_pause uses
                            # do_wait_suspend directly (doesn't need the
                            # pydevd LineBreakpoint), and leaving it installed
                            # causes pydevd's DEBUGGER_ID py_line_callback to
                            # fire independently and produce a second pause.
                            for loc in locs:
                                sib_code = loc[2] if len(loc) > 2 else None
                                # Remove siblings from _bp_pause_pending.
                                # (The fired one was already popped above.)
                                if not (loc[0] == fired_file
                                        and loc[1] == line_number):
                                    sib_key = (
                                        (id(sib_code), loc[1])
                                        if sib_code else None
                                    )
                                    if (sib_key
                                            and sib_key in self._bp_pause_pending):
                                        self._bp_pause_pending.pop(
                                            sib_key, None)
                                # Collect ALL pydevd bps for removal
                                # (including the fired one).
                                bp_loc = (loc[0], loc[1])
                                for t in self._temp_breakpoints:
                                    if (t[0], t[1]) == bp_loc:
                                        hit_bps_to_remove.append(t)
                            # Remove collected bps from the tracking list.
                            if hit_bps_to_remove:
                                self._temp_breakpoints = [
                                    t for t in self._temp_breakpoints
                                    if t not in hit_bps_to_remove
                                ]
                            break
                # Remove the actual pydevd LineBreakpoints OUTSIDE the
                # lock to avoid lock-order inversion with pydevd's locks.
                if hit_bps_to_remove:
                    _sib_py_db = pydevd_pause._get_pydevd_debugger()
                    if _sib_py_db is not None:
                        pydevd_pause._remove_temp_breakpoints(_sib_py_db, hit_bps_to_remove)
                    _log_warn(
                        f"_on_line: disarm removed {len(hit_bps_to_remove)} "
                        f"pydevd bp(s) for hit at {fired_file}:{line_number}"
                    )
                # ── End hit bp cleanup ─────────────────────────────────
                self._trigger_direct_pause(code, line_number)
                return
            # ── End direct-pause mechanism ────────────────────────────────

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

                # Resolve via eval(name, f_globals, f_locals) so global and
                # nonlocal variables are found correctly – plain
                # f_locals.get(name) misses them since they live in f_globals
                # or enclosing cell vars.
                def _resolve(name):
                    try:
                        return eval(name, frame.f_globals, frame.f_locals)
                    except Exception:
                        return None
                new_hashes = {w.name: _value_hash(_resolve(w.name))
                              for w in active_watches}
                new_reprs = {w.name: _safe_repr(_resolve(w.name))
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
                try:
                    self._fire_if_changed(
                        frame, active_watches, prev_line, prev_hashes, prev_reprs,
                        new_hashes, new_reprs, code.co_filename,
                    )
                except WatchpointHit:
                    # Intentional raise for no-pydevd/test mode – let it
                    # propagate. sys.monitoring treats this as a user-level
                    # exception, not a callback failure.
                    raise
                except Exception as e:
                    # Any other exception would cause sys.monitoring to
                    # DISABLE LINE events for this code object permanently.
                    # Swallow it and log so the user's watch doesn't
                    # silently die.
                    _log_warn(
                        f"_on_line: _fire_if_changed raised {e!r} for "
                        f"{code.co_filename}:{line_number}; swallowed to "
                        f"preserve monitoring.",
                        always=True,
                    )
        finally:
            self._guard.active = False

    def _trigger_direct_pause(self, code: Any, line_number: int) -> None:
        """Trigger a pydevd pause directly from our monitoring callback.

        Called when our _on_line (on _TOOL_ID) matches a pending bp target.
        This bypasses pydevd's py_line_callback entirely – solving the case
        where pydevd's DEBUGGER_ID LINE callback doesn't fire for a library
        code object whose PY_START was DISABLEd before our bp existed.

        Uses do_wait_suspend on the user's frame (not our runtime frame) so
        the IDE shows the correct file/line. The frame is obtained via
        sys._getframe(2): frame(0)=_trigger_direct_pause,
        frame(1)=_on_line, frame(2)=the user's code where LINE fired.
        """
        try:
            import threading
            from _pydevd_bundle.pydevd_comm_constants import CMD_SET_BREAK

            py_db = pydevd_pause._get_pydevd_debugger()
            if py_db is None or getattr(py_db, "_finish_debugging_session", False):
                return

            # frame(0) = this method
            # frame(1) = _on_line (our callback)
            # frame(2) = the actual user/library frame where LINE fired
            user_frame = sys._getframe(2)
            thread = threading.current_thread()

            # Validate that pydevd hasn't already suspended this thread
            # (e.g. from its own DEBUGGER_ID callback firing concurrently).
            # Double-suspending crashes the pydevd protocol layer.
            # pydevd stores thread state as `thread.additional_info.pydev_state`.
            info = getattr(thread, 'additional_info', None)
            if info is not None and getattr(info, 'pydev_state', 1) == 2:  # STATE_SUSPEND = 2
                _log_warn(
                    f"_trigger_direct_pause: thread already suspended, "
                    f"skipping direct pause at "
                    f"{user_frame.f_code.co_filename}:{line_number}"
                )
                return

            _log_warn(
                f"_trigger_direct_pause: firing for "
                f"{user_frame.f_code.co_filename}:{line_number} "
                f"(direct pause via _TOOL_ID callback)"
            )

            py_db.set_suspend(thread, CMD_SET_BREAK, suspend_other_threads=False)
            py_db.do_wait_suspend(thread, user_frame, 'line', None)
        except Exception as e:  # noqa: BLE001
            _log_warn(
                f"_trigger_direct_pause: failed ({e!r}); "
                f"pause may not materialise for this hit."
            )

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

                # Resolve via eval() same as _on_line – supports globals/nonlocals.
                def _resolve(name):
                    try:
                        return eval(name, frame.f_globals, frame.f_locals)
                    except Exception:
                        return None
                new_hashes = {w.name: _value_hash(_resolve(w.name))
                              for w in active_watches}
                new_reprs = {w.name: _safe_repr(_resolve(w.name))
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

    def _next_slot_for_code(self, code: Any,
                            start_line: int) -> Optional[int]:
        """Find next available bp slot in `code`, considering existing hits.

        Walks all queued hits' `bp_locations` and collects lines used in
        this code object, plus in-flight reservations from other threads.
        Returns the next code line strictly after the max used line (or
        after `start_line` if nothing used yet), and reserves it immediately.
        `_next_code_line_in` uses `co_lines()` so blank lines / lines past
        the last statement are skipped – pydevd's `py_line_callback`
        doesn't fire for those.
        """
        with self._lock:
            used_lines = []
            for h in self._hit_queue:
                for loc in h.get("bp_locations", []):
                    if loc[2] is code:
                        used_lines.append(loc[1])
            code_id = id(code)
            for reserved_code_id, reserved_line in self._bp_slot_reservations:
                if reserved_code_id == code_id:
                    used_lines.append(reserved_line)
            if used_lines:
                # Never search backward past the current mutation line –
                # a previous hit's bp_location can be before start_line
                # (e.g. feature_contexts bp at 287, then
                # external_feature_contexts mutates at 288). Without this
                # max(), we'd pick 288 (the mutation line itself) which has
                # already executed and a bp there will never fire.
                search_after = max(max(used_lines), start_line)
                candidate = _next_code_line_in(code, search_after)
            else:
                candidate = _next_code_line_in(code, start_line)
            if candidate is not None:
                self._bp_slot_reservations.add((id(code), candidate))
            return candidate

    def _bp_line_used_for_code(self, code: Any, line: int) -> bool:
        """Return True if a queued hit already owns `line` in `code`.

        The bytecode-order primary selector bypasses `_next_slot_for_code`,
        so it needs this small collision check to preserve the one-hit-per-line
        invariant that selective draining relies on.
        """
        with self._lock:
            return ((id(code), line) in self._bp_slot_reservations
                    or any(
                loc[2] is code and loc[1] == line
                for h in self._hit_queue
                for loc in h.get("bp_locations", [])
            ))

    def _release_bp_slot_reservation(self, code: Any, line: int) -> None:
        """Drop an in-flight reservation once install succeeds or fails."""
        with self._lock:
            self._bp_slot_reservations.discard((id(code), line))

    def _next_slot_after_frame(self, frame: Any) -> Optional[int]:
        """Find the next unused LINE event after `frame.f_lasti`.

        Numeric source order is wrong for multi-line RHS calls. In
        `request.parsed = ParsedRequest(...)`, the STORE_ATTR still belongs
        to line 105, but bytecode for lines 106-112 has already run. The next
        usable pause is the next LINE event after STORE_ATTR, line 114.
        """
        candidate = _next_code_line_after_frame(frame)
        while candidate is not None:
            with self._lock:
                if not self._bp_line_used_for_code(frame.f_code, candidate):
                    self._bp_slot_reservations.add((id(frame.f_code), candidate))
                    return candidate
            candidate = _next_code_line_in(frame.f_code, candidate)
        return candidate

    def _compute_bp_targets(self, user_frame: Any,
                            source_line: Optional[int] = None,
                            source_file: Optional[str] = None) -> list:
        """Compute bp slot(s) for a hit – primary + safety-net.

        Returns a list of `(file, line, code_object)` tuples that
        `_handle_hit` then installs as `LineBreakpoint`s. Each is a
        slot for a single bp. When one fires, `_trigger_direct_pause`
        removes sibling bps for the same hit synchronously – preventing
        the safety-net from causing a spurious second pause.

        Why two slots:
        - **Primary (mutation site)**: `user_frame.f_code` at the next
          code line after the mutation in bytecode order when possible,
          otherwise the next numeric code line. The bytecode-order path
          matters for multi-line calls like `request.parsed = ParsedRequest(...)`:
          by the time STORE_ATTR fires on line 105, argument lines 106-112
          have already executed, so a bp on line 106 can never fire.
        - **Safety (walked-up user code)**: the nearest user-code frame
          via `_find_user_code_caller`. Ensures a pause even when the
          primary's LINE event doesn't fire.

        Sequential allocation: when N hits target the same code object,
        `_next_slot_for_code` returns successive lines so each hit
        gets its own bp.

        Returns an empty list when NEITHER slot is available (the
        `script.py` last-line-of-module corner case). Caller falls
        back to `_pause_via_do_wait_suspend`.
        """
        targets: list = []

        # Primary: user_frame's code (mutation site). If the hit was raised
        # directly from the mutating line (attribute/classpatch watchers),
        # prefer the next LINE event after the current bytecode offset. This
        # skips already-executed continuation lines in multi-line statements.
        primary_line = None
        if source_line is not None and source_line == user_frame.f_lineno:
            primary_line = self._next_slot_after_frame(user_frame)
        if primary_line is None:
            # When source is in the SAME file and source_line < f_lineno,
            # the frame has advanced past the mutation line but f_lineno
            # hasn't executed yet – it's the correct pause target. Search
            # from f_lineno - 1 so _next_code_line_in (which finds strictly
            # >) can return f_lineno itself.
            # When source_line == f_lineno (fallback from _next_slot_after_frame
            # returning None), f_lineno already executed – search after it.
            search_after = user_frame.f_lineno
            if (source_line is not None
                    and source_line < user_frame.f_lineno
                    and source_file == user_frame.f_code.co_filename):
                search_after = user_frame.f_lineno - 1
            primary_line = self._next_slot_for_code(
                user_frame.f_code, search_after,
            )
        if primary_line is not None:
            targets.append((
                user_frame.f_code.co_filename, primary_line, user_frame.f_code,
            ))
        else:
            # Primary exhausted (mutation is on the function's last code
            # line – e.g. `_authorization` ends at line 288 with no follow-up).
            # Walk f_back to find the NEAREST caller frame with a valid next
            # code line. This gives a much more contextual pause location
            # than jumping straight to the distant user-code safety net.
            # Example: for `_authorization` called via an OpenTelemetry
            # decorator, the wrapper's caller or the view's dispatch line
            # is contextually closer than audit_logging middleware 30 frames up.
            f = user_frame.f_back
            hops = 0
            while f is not None and hops < _MAX_FRAME_WALK_HOPS:
                # Skip our own runtime frames AND pydevd-internal frames.
                # Crucially, pydevd's own files (helpers/pydev/pydevd.py etc.)
                # must be skipped: pydevd won't pause on breakpoints installed
                # inside its own infrastructure, so picking those as bp targets
                # silently swallows the pause (observed on PyCharm 2025.3 when
                # the mutation is on the last line of a function).
                # NOTE: site-packages frames are intentionally NOT skipped here –
                # pydevd CAN pause there via LineBreakpoint, and using a closer
                # library frame as the intermediate gives a more contextual pause
                # than jumping straight to the distant user-code safety net.
                if (_is_runtime_filename(f.f_code.co_filename)
                        or _is_pydevd_internal(f.f_code.co_filename)):
                    f = f.f_back
                    hops += 1
                    continue
                caller_line = self._next_slot_for_code(
                    f.f_code, f.f_lineno,
                )
                if caller_line is not None:
                    targets.append((
                        f.f_code.co_filename, caller_line, f.f_code,
                    ))
                    _log_warn(
                        f"_compute_bp_targets: primary exhausted, using "
                        f"f_back intermediate at "
                        f"{f.f_code.co_filename}:{caller_line} "
                        f"(hops={hops})"
                    )
                    break
                f = f.f_back
                hops += 1

        # Safety: nearest walked-up user-code frame. Ensures a pause even
        # when pydevd's monitoring fails to fire LINE events for the primary's
        # library code (openapi_validate_request is the known example: bps
        # install but never fire because PY_START was DISABLEd before our bp).
        # Spurious double-pauses are prevented by _trigger_direct_pause
        # removing sibling bps synchronously when the first bp fires.
        safety_frame = _find_user_code_caller(user_frame)
        if (safety_frame is not None
                and safety_frame.f_code is not user_frame.f_code):
            safety_line = self._next_slot_for_code(
                safety_frame.f_code, safety_frame.f_lineno,
            )
            if safety_line is not None:
                cand = (safety_frame.f_code.co_filename, safety_line)
                # Dedup: don't install twice at the same (file, line).
                if not any((t[0], t[1]) == cand for t in targets):
                    targets.append((
                        safety_frame.f_code.co_filename,
                        safety_line,
                        safety_frame.f_code,
                    ))
                else:
                    self._release_bp_slot_reservation(
                        safety_frame.f_code, safety_line,
                    )

        return targets

    def _handle_hit(self, user_frame: Any, watch_name: str,
                    old_repr: str, new_repr: str,
                    source_file: str, source_line: int) -> None:
        """Pause the debugger via pydevd; if pydevd isn't loaded, raise the exception.

        Each hit installs its OWN pydevd `LineBreakpoint` (design contract §13
        + the post-v8 sequential-bps refinement, see `_compute_bp_target`).
        N back-to-back mutations at the same user-code anchor install N bps
        at SUCCESSIVE code lines (80, 81, 82, ...). The IDE then pauses N
        times in line order. `_pycharm_consume_last_hit(pause_file, pause_line)`
        drains the one hit whose bp triggered THIS pause; the others stay
        queued for their future pauses. This replaces the previous
        process-level `_pause_pending` gate which dropped hits 2..N silently.

        Pause anchor: `user_frame` is the immediate mutation frame (e.g.
        `query.py:289` when Django's `_clone` does `self._hints = ...`).
        That frame is often inside `site-packages`. We still require a
        user-code frame somewhere in the call chain so pure-library
        side-effects stay silent, but the primary bp itself is anchored on
        the mutation frame.

        The raise fallback keeps the test suite working under plain pytest
        where pydevd is not present in the environment.
        """
        _log_warn(
            f"_handle_hit ENTRY: watch={watch_name!r} "
            f"user_frame={user_frame.f_code.co_filename}:{user_frame.f_lineno} "
            f"source={source_file}:{source_line} "
            f"queue_len={len(self._hit_queue)}"
        )

        # Suppress side-effect mutations fired during our own installation
        # flow (e.g. getattr triggering SimpleLazyObject lazy eval which
        # writes back to the watched object via __setattr__). Only suppress
        # on the same thread that's doing the installation – real mutations
        # on other user threads must still fire.
        if constants._installing_watch_thread == threading.get_ident():
            _log_warn(
                f"_handle_hit: SUPPRESSED (during installation) for {watch_name!r}"
            )
            return

        # A mutation whose ANCHOR frame is pydevd's own infrastructure is a
        # debugger-inspection side-effect, not a real program mutation. This
        # happens when the watched object lazily writes an attribute on read
        # (Django WSGIRequest, SimpleLazyObject, ...) while pydevd evaluates an
        # expression to render the Variables panel / answer
        # `_pycharm_locate_watches()`: the `__setattr__` fires from inside
        # `pydevd_utils.eval_expression`, and `_find_user_caller` (which skips
        # only OUR runtime frames) hands us that pydevd frame as `user_frame`.
        # Anchoring a `LineBreakpoint` there lands it inside pydevd_utils.py,
        # where it then fires on EVERY subsequent expression pydevd evaluates –
        # and `unwatch` (which doesn't sweep temp breakpoints) can't clear it.
        # The pure-library drop gate below doesn't catch this because real user
        # code usually sits higher up the suspended thread's stack. Drop it.
        if _is_pydevd_internal(user_frame.f_code.co_filename):
            _log_warn(
                f"_handle_hit: anchor frame is pydevd-internal "
                f"({user_frame.f_code.co_filename}:{user_frame.f_lineno}) for "
                f"{watch_name!r}; dropping hit (debugger-inspection side-effect, "
                f"not a real mutation)."
            )
            return

        # Declared up-front for both exception branches below (Python
        # rejects multiple `global` declarations in the same function).
        global _pydevd_last_error

        # Use `user_frame` itself as the bp anchor – the IDE pauses at
        # the next code line in the SAME file where the mutation
        # happened, which is much more contextual than the previous
        # behavior of walking up past site-packages and pausing in
        # whichever distant user-code frame called into the library.
        #
        # The design contract §12 walk-up was a workaround for
        # `CMD_STEP_OVER + step_stop = library_frame` being filtered by
        # PyCharm's "do not step into library code" setting. We no
        # longer use CMD_STEP_OVER; we use `LineBreakpoint`s installed
        # via `consolidate_breakpoints` – the same path user-set
        # breakpoints take, which fires reliably in library code. So
        # the walk-up is no longer needed for the filter-bypass reason
        # and just produces less contextual pauses.
        #
        # Drop-on-pure-library-chain (no user code anywhere in 32 hops)
        # is still preserved: when the entire stack is library / runtime
        # (descriptor side-effects, pydevd machinery, etc.) firing a
        # watchpoint hit is more confusing than silent. The detection
        # uses `_find_user_code_caller` purely as a "does user code
        # exist somewhere" check – we don't actually use the walked-up
        # frame as the anchor anymore.
        if _find_user_code_caller(user_frame) is None:
            _log_warn(
                f"_handle_hit: NO USER FRAME in chain for {watch_name!r}; "
                f"dropping hit (highlight + pause both suppressed)"
            )
            return
        pause_anchor = user_frame
        _log_warn(
            f"_handle_hit: pause_anchor="
            f"{pause_anchor.f_code.co_filename}:{pause_anchor.f_lineno} "
            f"(using user_frame directly as the mutation anchor)"
        )

        # Capture the direct parent frame's location – this is the frame
        # that called the function where the mutation happened. Its f_lineno
        # at this moment is the exact call-site line. The Kotlin side uses
        # this for the secondary "call-site" highlight so it can mark the
        # exact call expression rather than guessing offsets from the bp's
        # fire location.
        #
        # We use f_back (direct caller) rather than _find_user_code_caller
        # because the latter skips site-packages/stdlib and lands in distant
        # user code (e.g. audit_logging middleware) which is not contextually
        # useful for "this call triggered the watchpoint."
        parent = user_frame.f_back
        caller_file = parent.f_code.co_filename if parent else ""
        caller_line = parent.f_lineno if parent else 0

        py_db = pydevd_pause._get_pydevd_debugger()

        # No pydevd ⇒ standalone / pytest run. The hit queue is only
        # useful as a buffer between `_handle_hit` and the IDE-side
        # drain; without pydevd there IS no IDE-side drain, so skip
        # the queue + slot-allocator entirely and raise so the test
        # suite (or any non-IDE caller) can catch the event directly.
        if py_db is None:
            _log_warn(
                f"_handle_hit: pydevd not found for {watch_name!r}; "
                f"raising WatchpointHit. Diag: {_pycharm_watchpoint_diag()}"
            )
            raise WatchpointHit(
                watch_name, old_repr, new_repr, source_file, source_line,
            )

        # Compute bp slots for THIS hit. Each entry is a (file, line, code)
        # tuple. Up to TWO slots: primary at the mutation site, safety
        # at the walked-up user-code frame. Empty list ⇒ no slots
        # available anywhere (last-line-of-module corner case).
        targets = self._compute_bp_targets(pause_anchor, source_line, source_file)

        # Now that we know where the bp will fire (targets[0] file), refine
        # the caller info: walk up from user_frame to find the frame that's
        # in the SAME file as the bp target. That frame's f_lineno is the
        # call-site line the user expects to see highlighted (e.g.
        # "self._authorization(request)" in dispatch when the mutation is
        # deep inside _authorization's call chain via contextlib/etc.).
        #
        # Falls back to the direct parent (user_frame.f_back) when no
        # ancestor matches the bp file – still more useful than nothing.
        if targets:
            bp_file = targets[0][0]
            f = user_frame.f_back
            hops = 0
            while f is not None and hops < _MAX_FRAME_WALK_HOPS:
                if f.f_code.co_filename == bp_file:
                    caller_file = f.f_code.co_filename
                    caller_line = f.f_lineno
                    break
                f = f.f_back
                hops += 1

        with self._lock:
            queue_was_empty = len(self._hit_queue) == 0

        if not targets:
            # No slots available. If the queue is non-empty, drop –
            # the user has already been notified of prior hits and
            # a stack-blocking do_wait_suspend would be worse than
            # silence. If the queue IS empty, this is the very first
            # hit and we MUST honor the user's `watch(...)` request,
            # so fall back to do_wait_suspend with the urllib-on-stack
            # trade-off.
            if not queue_was_empty:
                _log_warn(
                    f"_handle_hit: no bp slots for {watch_name!r} "
                    f"(neither {pause_anchor.f_code.co_filename} nor walked-up "
                    f"user code have a next code line, and queue is "
                    f"non-empty); dropping hit"
                )
                return
            try:
                hit = {
                    "file": source_file,
                    "line": source_line,
                    "name": watch_name,
                    "old": old_repr,
                    "new": new_repr,
                    "caller_file": caller_file,
                    "caller_line": caller_line,
                    "bp_locations": [],  # untagged ⇒ next consume any-location matches
                }
                with self._lock:
                    self._last_hit = hit
                    self._hit_queue.append(hit)
                _log_warn(
                    f"_handle_hit: no bp slots for {watch_name!r}; "
                    f"falling back to do_wait_suspend at "
                    f"{pause_anchor.f_code.co_filename}:{pause_anchor.f_lineno}"
                )
                if not pydevd_pause._pause_via_do_wait_suspend(
                    py_db, pause_anchor, watch_name,
                ):
                    _log_warn(
                        f"_handle_hit: fallback do_wait_suspend also "
                        f"failed for {watch_name!r}; no pause possible "
                        f"for this mutation."
                    )
                return
            except Exception as e:  # noqa: BLE001
                _pydevd_last_error = f"do_wait_suspend failed: {e!r}"
                _log_warn(
                    f"pause-via-pydevd failed ({e!r}); falling back to raise. "
                    f"Diag: {_pycharm_watchpoint_diag()}"
                )
                raise WatchpointHit(
                    watch_name, old_repr, new_repr, source_file, source_line,
                )

        # Queue the hit FIRST (before install) so async bp firing can
        # find it. We populate `bp_locations` as each install succeeds.
        hit = {
            "file": source_file,
            "line": source_line,
            "name": watch_name,
            "old": old_repr,
            "new": new_repr,
            "caller_file": caller_file,
            "caller_line": caller_line,
            "bp_locations": [],
        }
        with self._lock:
            self._last_hit = hit
            self._hit_queue.append(hit)
            if len(self._hit_queue) > _MAX_HIT_QUEUE_SIZE:
                # Trim oldest – ancient queued hits past the cap are less
                # likely to fire (their bps may have been lost on session
                # restart). Keep the most recent N.
                del self._hit_queue[
                    : len(self._hit_queue) - _MAX_HIT_QUEUE_SIZE
                ]

        installed_any = False
        for (target_file, target_line, target_code) in targets:
            try:
                installed = pydevd_pause._install_bp_at(
                    py_db, target_code, target_file, target_line, watch_name,
                )
                if installed is not None:
                    with self._lock:
                        self._temp_breakpoints.append(installed)
                        hit["bp_locations"].append(
                            (installed[0], installed[1], target_code)
                        )
                        self._bp_slot_reservations.discard(
                            (id(target_code), target_line)
                        )
                    installed_any = True
                    _log_warn(
                        f"_handle_hit: pause ARMED for {watch_name!r} at "
                        f"{installed[0]}:{installed[1]} (one of "
                        f"{len(targets)} slots)"
                    )
                else:
                    self._release_bp_slot_reservation(target_code, target_line)
            except Exception as e:  # noqa: BLE001
                self._release_bp_slot_reservation(target_code, target_line)
                _pydevd_last_error = f"bp install failed: {e!r}"
                _log_warn(
                    f"_handle_hit: bp install at {target_file}:{target_line} "
                    f"raised ({e!r}); continuing to next target."
                )

        if installed_any:
            return

        # All installs failed – the queued hit has empty `bp_locations`,
        # so the next selective drain at ANY location will pick it up.
        _log_warn(
            f"_handle_hit: ALL bp installs failed for {watch_name!r}; "
            f"falling back to do_wait_suspend"
        )
        try:
            if not pydevd_pause._pause_via_do_wait_suspend(
                py_db, pause_anchor, watch_name,
            ):
                _log_warn(
                    f"_handle_hit: fallback do_wait_suspend also failed "
                    f"for {watch_name!r}; pause may not materialise."
                )
            return
        except Exception as e:  # noqa: BLE001
            _pydevd_last_error = f"pause failed: {e!r}"
            _log_warn(
                f"_handle_hit: fallback pause raised ({e!r}); raising "
                f"WatchpointHit. Diag: {_pycharm_watchpoint_diag()}"
            )
            raise WatchpointHit(
                watch_name, old_repr, new_repr, source_file, source_line,
            )




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
    if constants._TOOL_ID is not None:
        return  # already initialised – guard against double import / reload

    monitoring = sys.monitoring
    candidates = [5, 4, 3, monitoring.PROFILER_ID, monitoring.COVERAGE_ID]
    for tid in candidates:
        try:
            monitoring.use_tool_id(tid, "pycharm_watchpoints")
            constants._TOOL_ID = tid
            break
        except ValueError:
            continue

    if constants._TOOL_ID is None:
        raise RuntimeError("watchpoint.py: no free sys.monitoring tool ID available.")

    monitoring.register_callback(constants._TOOL_ID, monitoring.events.LINE, registry._on_line)
    monitoring.register_callback(
        constants._TOOL_ID, monitoring.events.PY_RETURN, registry._on_py_return
    )
    monitoring.register_callback(
        constants._TOOL_ID, monitoring.events.PY_START, registry._on_py_start
    )
    monitoring.register_callback(
        constants._TOOL_ID, monitoring.events.CALL, registry._on_call
    )
    # Deliberately NOT calling set_events() – zero global overhead until watch() fires.
