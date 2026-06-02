"""Mutation-intercepting builtin container subclasses (list/dict/set)."""


from typing import Any


from .constants import Any, Optional, sys
from .caller import _find_user_caller


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

    Returns `"<unreprable>"` if the repr raises. This is load-bearing:
    container `__repr__` recursively reprs every contained value, and
    a half-constructed contained value (e.g., a Django Model being
    rebuilt mid-`copy.deepcopy`) can raise from its own `__repr__`
    /`__str__` (Django Model.__str__ accesses fields via descriptors
    that read `_state.fields_cache`, which doesn't exist yet during
    `_reconstruct`). Without this guard, our `_WatchedDict.__setitem__`
    on `deepcopy`'s memo dict propagates that AttributeError out of
    deepcopy and KILLS THE USER'S CODE.

    User-reported scenario: watching `self` on a Django TestCase. The
    recursive walker wraps `self._testdata_memo` (a plain dict) as
    `_WatchedDict`. Django's TestCase machinery passes that very dict
    as `copy.deepcopy(value, memo=cls._testdata_memo)`. Deepcopy does
    `memo[id(x)] = y` with `y` mid-reconstruction → our `__setitem__`
    snapshots the dict via `dict.__repr__` → iterates values → calls
    `repr` on the half-built Django Model → AttributeError → propagates.

    Result of the guard: when before- and after-snapshots both come
    back as `"<unreprable>"`, they compare equal, no hit fires, and
    the underlying `dict.__setitem__` mutation (already applied by
    the caller's mutating-method wrapper) is the only effect –
    exactly as if the value couldn't fire a change-detection event.
    """
    try:
        if isinstance(value, list):
            return list.__repr__(value)
        if isinstance(value, dict):
            return dict.__repr__(value)
        if isinstance(value, set):
            return set.__repr__(value)
        return repr(value)
    except Exception:  # noqa: BLE001
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
    # Walk past our own runtime frames so the hit's reported file/line is
    # the user's mutation call, not the container method's internal
    # `super().append(...)` etc. inside watchpoint.py. If every frame is
    # runtime (mutation triggered by a descriptor side-effect during IDE
    # display), drop the hit rather than flood pydevd's queue.
    user_caller = _find_user_caller(caller)
    if user_caller is None:
        return

    registry._guard.active = True
    try:
        # Truncate for display only; comparison above used full reprs so
        # we don't miss changes past the 200-char threshold.
        old_display = old_repr_full if len(old_repr_full) <= 200 else old_repr_full[:200] + "..."
        new_display = new_repr_full if len(new_repr_full) <= 200 else new_repr_full[:200] + "..."
        registry._handle_hit(
            user_frame=user_caller,
            watch_name=expr,
            old_repr=old_display,
            new_repr=new_display,
            source_file=user_caller.f_code.co_filename,
            source_line=user_caller.f_lineno,
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
        old = _wp_container_repr(self)
        list.append(self, item)
        _wp_fire_container_change(self, old)

    def extend(self, items: Any) -> None:
        old = _wp_container_repr(self)
        list.extend(self, items)
        _wp_fire_container_change(self, old)

    def insert(self, index: int, item: Any) -> None:
        old = _wp_container_repr(self)
        list.insert(self, index, item)
        _wp_fire_container_change(self, old)

    def remove(self, item: Any) -> None:
        old = _wp_container_repr(self)
        list.remove(self, item)
        _wp_fire_container_change(self, old)

    def pop(self, *args: Any) -> Any:
        old = _wp_container_repr(self)
        result = list.pop(self, *args)
        _wp_fire_container_change(self, old)
        return result

    def clear(self) -> None:
        old = _wp_container_repr(self)
        list.clear(self)
        _wp_fire_container_change(self, old)

    def sort(self, *args: Any, **kwargs: Any) -> None:
        old = _wp_container_repr(self)
        list.sort(self, *args, **kwargs)
        _wp_fire_container_change(self, old)

    def reverse(self) -> None:
        old = _wp_container_repr(self)
        list.reverse(self)
        _wp_fire_container_change(self, old)

    def __setitem__(self, key: Any, value: Any) -> None:
        old = _wp_container_repr(self)
        list.__setitem__(self, key, value)
        _wp_fire_container_change(self, old)

    def __delitem__(self, key: Any) -> None:
        old = _wp_container_repr(self)
        list.__delitem__(self, key)
        _wp_fire_container_change(self, old)

    def __iadd__(self, other: Any) -> "list":
        old = _wp_container_repr(self)
        # list.__iadd__ returns self – preserve that contract.
        result = list.__iadd__(self, other)
        _wp_fire_container_change(self, old)
        return result

    def __imul__(self, other: int) -> "list":
        old = _wp_container_repr(self)
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
        old = _wp_container_repr(self)
        dict.__setitem__(self, key, value)
        _wp_fire_container_change(self, old)

    def __delitem__(self, key: Any) -> None:
        old = _wp_container_repr(self)
        dict.__delitem__(self, key)
        _wp_fire_container_change(self, old)

    def pop(self, *args: Any, **kwargs: Any) -> Any:
        old = _wp_container_repr(self)
        result = dict.pop(self, *args, **kwargs)
        _wp_fire_container_change(self, old)
        return result

    def popitem(self) -> Any:
        old = _wp_container_repr(self)
        result = dict.popitem(self)
        _wp_fire_container_change(self, old)
        return result

    def clear(self) -> None:
        old = _wp_container_repr(self)
        dict.clear(self)
        _wp_fire_container_change(self, old)

    def update(self, *args: Any, **kwargs: Any) -> None:
        old = _wp_container_repr(self)
        dict.update(self, *args, **kwargs)
        _wp_fire_container_change(self, old)

    def setdefault(self, key: Any, default: Any = None) -> Any:
        old = _wp_container_repr(self)
        result = dict.setdefault(self, key, default)
        _wp_fire_container_change(self, old)
        return result

    def __ior__(self, other: Any) -> "dict":
        # 3.9+ adds `d |= other`. dict.__ior__ returns self.
        old = _wp_container_repr(self)
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
        old = _wp_container_repr(self)
        set.add(self, item)
        _wp_fire_container_change(self, old)

    def discard(self, item: Any) -> None:
        old = _wp_container_repr(self)
        set.discard(self, item)
        _wp_fire_container_change(self, old)

    def remove(self, item: Any) -> None:
        old = _wp_container_repr(self)
        set.remove(self, item)
        _wp_fire_container_change(self, old)

    def pop(self) -> Any:
        old = _wp_container_repr(self)
        result = set.pop(self)
        _wp_fire_container_change(self, old)
        return result

    def clear(self) -> None:
        old = _wp_container_repr(self)
        set.clear(self)
        _wp_fire_container_change(self, old)

    def update(self, *iterables: Any) -> None:
        old = _wp_container_repr(self)
        set.update(self, *iterables)
        _wp_fire_container_change(self, old)

    def intersection_update(self, *iterables: Any) -> None:
        old = _wp_container_repr(self)
        set.intersection_update(self, *iterables)
        _wp_fire_container_change(self, old)

    def difference_update(self, *iterables: Any) -> None:
        old = _wp_container_repr(self)
        set.difference_update(self, *iterables)
        _wp_fire_container_change(self, old)

    def symmetric_difference_update(self, *iterables: Any) -> None:
        old = _wp_container_repr(self)
        set.symmetric_difference_update(self, *iterables)
        _wp_fire_container_change(self, old)

    def __ior__(self, other: Any) -> "set":
        old = _wp_container_repr(self)
        result = set.__ior__(self, other)
        _wp_fire_container_change(self, old)
        return result

    def __iand__(self, other: Any) -> "set":
        old = _wp_container_repr(self)
        result = set.__iand__(self, other)
        _wp_fire_container_change(self, old)
        return result

    def __ixor__(self, other: Any) -> "set":
        old = _wp_container_repr(self)
        result = set.__ixor__(self, other)
        _wp_fire_container_change(self, old)
        return result

    def __isub__(self, other: Any) -> "set":
        old = _wp_container_repr(self)
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
