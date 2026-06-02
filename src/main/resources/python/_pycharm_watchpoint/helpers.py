"""Stateless helpers: safe repr, the file/stderr log sink, value hashing,
object-watchability heuristics, and the recursion caps."""


import sys
from typing import Any, Optional


from . import constants
from .constants import Any, Optional, sys


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


def _log_warn(msg: str, *, always: bool = False) -> None:
    """Write a one-line `[WATCHPOINT]` warning to stderr AND tee to a
    diagnostic file, never raise.

    Stderr output is gated on `_WATCHPOINT_LOG` (i.e. `PYCHARM_WATCHPOINT_LOG=1`),
    UNLESS `always=True` is passed – use that for critical startup lines that
    should be visible regardless of the env flag (e.g. "runtime loaded").
    The file sink at `/tmp/pythonwatchpoint.log` is always written regardless
    of either flag – it is the durable fallback the user can `tail -f` under
    pytest (which hides stderr by default) or when pydevd's stdout interception
    drops lines.

    File path is fixed (not env-driven) on purpose: when the user is
    reporting a bug, "just look at /tmp/pythonwatchpoint.log" is one
    less moving part than "set this env var before launching the IDE."
    Append mode + truncation guard keeps growth bounded over long
    sessions (we trim to the last ~1 MB when we cross 2 MB).
    """
    line = f"[WATCHPOINT] {msg}"
    if always or constants._WATCHPOINT_LOG:
        try:
            print(line, file=sys.stderr, flush=True)
        except Exception:
            pass
    try:
        import os as _os
        path = "/tmp/pythonwatchpoint.log"
        try:
            if _os.path.exists(path) and _os.path.getsize(path) > 2_000_000:
                with open(path, "rb") as fh:
                    fh.seek(-1_000_000, 2)
                    tail = fh.read()
                with open(path, "wb") as fh:
                    fh.write(tail)
        except Exception:
            pass
        with open(path, "a", encoding="utf-8", errors="replace") as fh:
            import datetime as _dt
            ts = _dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            fh.write(f"{ts} {line}\n")
    except Exception:
        pass


def _try_add_sub_watch(root_watch: "_AttributeWatch",
                       sub_w: "_AttributeWatch") -> bool:
    """Append `sub_w` to `root_watch.sub_watches` unless we've hit the
    per-root cap. Returns True on success, False once the cap engages.

    The first time the cap engages on a given root, a one-line warning is
    logged naming the root expression so the user can see why their watch
    isn't picking up deeper mutations. Subsequent caps on the same root
    are silent.
    """
    if root_watch.sub_watches_capped:
        return False
    if len(root_watch.sub_watches) >= _MAX_SUB_WATCHES_PER_ROOT:
        root_watch.sub_watches_capped = True
        _log_warn(
            f"watch '{root_watch.expr}': reached sub-watch cap "
            f"({_MAX_SUB_WATCHES_PER_ROOT}); stopping deeper instrumentation. "
            f"Likely cause: cyclic / framework-shaped object graph."
        )
        return False
    root_watch.sub_watches.append(sub_w)
    return True


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
# 4 levels deep is a pragmatic default – deep enough
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


# Module name roots whose types are framework / third-party code, not user
# code. We refuse to RECURSE into objects of these types during
# `_instrument_object_tree` (the root watch itself can still use whatever
# strategy `add_watch` picks – class surgery, classpatch fallback, or
# rebind detection). Without this filter, watching a Django QuerySet
# explodes through `obj.model.user_id.field.remote_field.field.remote_field…`
# because Django's ORM descriptors fabricate fresh proxy objects on each
# access – id()-based cycle detection never matches – and reading those
# descriptors does internal setattrs as a side effect, which OUR
# __setattr__ then catches as "mutations" and re-recurses with a fresh
# depth-1 walk. End result: hundreds of queued hits per Variables-panel
# expansion and a frozen IDE.
#
# We *want* full coverage of user code; we *want* a clean stop at the
# framework boundary. The Variables-panel display path then runs through
# normal Django attribute access (no watcher in the way), so no queue
# flood. A user who watches `my_dto` where `my_dto.queryset` is a Django
# QuerySet still gets full instrumentation on `my_dto.*` and on any
# user-defined sub-objects; we just stop walking when we reach
# `my_dto.queryset` and leave it untouched.
_FRAMEWORK_MODULE_ROOTS = frozenset({
    "django", "sqlalchemy", "alembic",
    "pydevd", "_pydevd", "_pydev_bundle", "_pydevd_bundle",
    "_pydev_runfiles", "pydev_ipython", "pydevd_plugins",
    "numpy", "pandas", "polars", "scipy", "sklearn",
    "pydantic", "pydantic_core", "attr", "attrs", "marshmallow",
    "werkzeug", "flask", "django_extensions",
    "requests", "urllib3", "tornado", "aiohttp", "httpx", "starlette", "fastapi",
    "tortoise", "peewee", "sqlmodel", "pony", "mongoengine",
    "celery", "kombu", "redis", "pymongo",
    "boto3", "botocore",
    "grpc", "_grpc",
    "sentry_sdk", "loguru",
    "uvicorn", "gunicorn",
    "pytest", "_pytest", "pluggy",
    "setuptools", "pkg_resources", "_distutils_hack",
    # Our own runtime module is registered as "_pycharm_watchpoint" in sys.modules
    # by the IDE's sitecustomize injection (see plugin's
    # DebugWithWatchpointAction.injectViaSiteCustomize); make sure our
    # dynamically-created watcher subclasses don't get re-instrumented.
    # "watchpoint" is kept for test-mode where the file is imported by filename.
    "_pycharm_watchpoint",
    "watchpoint",
})


def _is_user_defined_type(t: Optional[type]) -> bool:
    """Return True if `t` is a type from the user's own code (not stdlib,
    not a known framework, not under site-packages / dist-packages).

    Used by `_instrument_object_tree` (and the watcher's `__setattr__`
    re-entry path) to decide whether to RECURSE into an attribute value.
    The ROOT object the user passes is always considered watchable by
    `add_watch`'s own logic; this helper only gates the recursive walk.

    The check is intentionally cheap (called once per candidate attribute
    during the depth-4 walk):

    - reject non-types and `builtins`-module types,
    - reject if the top-level module name is in `sys.stdlib_module_names`
      (Python 3.10+) or `_FRAMEWORK_MODULE_ROOTS`,
    - reject if `sys.modules[mod].__file__` lives under `site-packages`
      or `dist-packages` (catches frameworks our denylist hasn't named).

    Result is NOT cached: types are O(1) hashable but the cache leaks
    references to one-off subclasses (a common pattern in metaclass-heavy
    libraries), and the check itself is sub-microsecond.
    """
    if t is None or not isinstance(t, type):
        return False
    mod_name = getattr(t, "__module__", None)
    if not mod_name or mod_name == "builtins":
        return False
    root = mod_name.partition(".")[0]
    if root in _FRAMEWORK_MODULE_ROOTS:
        return False
    stdlib_names = getattr(sys, "stdlib_module_names", None)
    if stdlib_names is not None and root in stdlib_names:
        return False
    mod = sys.modules.get(mod_name)
    if mod is not None:
        mod_file = getattr(mod, "__file__", None)
        if mod_file is not None and (
            "site-packages" in mod_file or "dist-packages" in mod_file
        ):
            return False
    return True


# How many sub-watches a single root can accumulate before we stop adding
# more. Sub-watches come from `_instrument_object_tree` recursion + the
# watcher's `__setattr__` re-entry path; the type-filter + persistent
# visited set already prevent the runaway, but this is belt-and-suspenders
# against any cyclic user-defined graph we missed. 100 is generous enough
# for normal DTO trees (typically < 20 sub-watches) and small enough to
# catch a runaway before pydevd's hit queue overflows.
_MAX_SUB_WATCHES_PER_ROOT = 100



# ---------------------------------------------------------------------------
# Belt-and-suspenders caps for runaway scenarios.
#
# All three are "should never hit in practice; if you do, something is
# genuinely pathological" limits. The dedupe gate keeps the hit queue at
# 0-or-1 in normal use; the per-thread propagation queue self-drains on
# every PY_START; the frame-walk safety only triggers on a degenerate
# `f_back` chain that loops back on itself (which shouldn't happen but
# has been observed under exotic frame manipulation libraries). 1024
# matches Python's default recursion limit – above that, you'd be
# hitting `RecursionError` from the interpreter before we'd hit it.
#
# Pulled out as named constants instead of inline literals so they
# can be bumped from one place if a future use case exceeds them.
# ---------------------------------------------------------------------------

# Max entries the IDE-side hit queue (`WatchpointRegistry._hit_queue`)
# retains. With the dedupe gate this is normally 0-or-1; the cap only
# matters for test paths or bug scenarios where the gate isn't engaged.
# When exceeded, the oldest entries are dropped (most recent are kept,
# since they're the ones the IDE is most likely to want to show next).
_MAX_HIT_QUEUE_SIZE = 1024

# Max entries in the per-thread cross-function propagation queue
# (`WatchpointRegistry._pending_propagation.queue`). Each CALL event
# queues one entry that's consumed by the matching PY_START; the cap
# protects against pathological CALL-without-PY_START sequences (e.g.,
# rare arg-binding failures, C extensions intercepting Python calls).
_MAX_PROPAGATION_QUEUE_SIZE = 1024

# Max hops a frame-chain walker (`_find_user_caller`, `_find_user_code_caller`,
# the `_pause_via_pydevd` disarm loop) will traverse before giving up.
# A degenerate `f_back` chain that loops or grows beyond Python's
# recursion limit is a sign of something gone wrong, not a normal stack.
_MAX_FRAME_WALK_HOPS = 1024




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
    # Identity-only hash for user-defined objects. Rebinding to a different
    # object yields a different id(); in-place mutation is NOT detected (that
    # requires an attribute watch). We intentionally omit type().__qualname__
    # because our own class-surgery (__class__ swap) changes the qualname of a
    # watched object, which would cause spurious fires on the local-variable
    # watch that tracks rebinding alongside an object watch.
    return id(value)
