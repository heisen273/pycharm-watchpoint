"""Per-watch state objects: :class:`_LocalWatch` and :class:`_AttributeWatch`."""


from typing import Any, Optional


from .constants import Any, Optional, _make_weak_or_strong


# ---------------------------------------------------------------------------
# Watch data structures
# ---------------------------------------------------------------------------

class _LocalWatch:
    """State for a single local-variable watch.

    `frame_id` ties this watch to the specific frame instance where watch()
    was called. The watch is removed when that frame exits (PY_RETURN) or
    when PY_START detects the frame id was reused by a new frame.

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
    __slots__ = ("expr", "_obj_ref", "original_cls", "watcher_cls", "initial_repr",
                 "container_wrapper", "_container_holder_ref", "container_attr",
                 "sub_watches", "classpatch_key",
                 "visited_ids", "sub_watches_capped")

    def __init__(self, expr: str, obj_ref: Any, original_cls: Optional[type],
                 watcher_cls: Optional[type], initial_repr: str) -> None:
        self.expr = expr
        self._obj_ref = _make_weak_or_strong(obj_ref)
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
        self._container_holder_ref = None
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
        # Persistent cycle-detection set, shared across the initial
        # `_instrument_object_tree` walk AND every later re-entry triggered
        # by the watcher's `__setattr__` hook. Storing it on the root watch
        # (rather than passing a fresh set per __setattr__ call) is what
        # stops Django-shaped object graphs whose descriptors fabricate new
        # proxy instances on each access – the per-call sets never matched,
        # so each re-entry started a brand-new depth-4 walk and the
        # instrumentation snowballed past the depth limit. See
        # `_FRAMEWORK_MODULE_ROOTS` / `_is_user_defined_type` for the
        # complementary first-line defence; this set is what catches truly
        # cyclic user-defined graphs the type filter doesn't.
        self.visited_ids: set = set()
        # Belt-and-suspenders breadth cap. Set to True by
        # `_try_add_sub_watch` once `len(sub_watches) >=
        # _MAX_SUB_WATCHES_PER_ROOT`. Recursive walks check this and
        # bail out so a missed cycle can't grow the watch tree forever.
        # One warning logged per root the first time the cap engages.
        self.sub_watches_capped: bool = False

    @property
    def obj_ref(self):
        """Dereference the weak (or strong-fallback) reference to the watched object.
        Returns None if the referent has been garbage-collected.
        """
        return self._obj_ref()

    @obj_ref.setter
    def obj_ref(self, value):
        self._obj_ref = _make_weak_or_strong(value)

    @property
    def container_holder(self):
        """Dereference the weak (or strong-fallback) reference to the container's parent.
        Returns None if the referent has been garbage-collected.
        """
        ref = self._container_holder_ref
        if ref is None:
            return None
        return ref()

    @container_holder.setter
    def container_holder(self, value):
        self._container_holder_ref = _make_weak_or_strong(value) if value is not None else None
