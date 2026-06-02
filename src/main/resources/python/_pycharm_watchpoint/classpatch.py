"""Class-level ``__setattr__`` monkey-patch fallback for hostile metaclasses."""


import threading
from typing import Any


from .constants import Any, sys, threading
from .helpers import _log_warn, _safe_repr, _value_hash
from .caller import _find_user_caller


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
                    user_caller = _find_user_caller(sys._getframe(1))
                    if user_caller is not None:
                        _reg._handle_hit(
                            user_frame=user_caller,
                            watch_name=watch_name,
                            old_repr=_safe_repr(old_val),
                            new_repr=_safe_repr(value),
                            source_file=user_caller.f_code.co_filename,
                            source_line=user_caller.f_lineno,
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
