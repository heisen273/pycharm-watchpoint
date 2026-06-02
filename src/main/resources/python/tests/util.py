"""Shared test helpers (fake frames/codes, sample objects, decoders) used
across the themed test modules. Extracted from the original monolithic
test_watchpoint.py so each themed file holds only its own test functions."""


import gc
import sys
import inspect
import weakref
import builtins
import pytest

from _pycharm_watchpoint import watch, unwatch, clear_watches, WatchpointHit


"""Tests for watchpoint.py – written first (TDD).

Python 3.14 sys.monitoring behavior: exceptions raised from LINE callbacks
bypass local exception handlers within the monitored frame and propagate to
the caller. This means pytest.raises(WatchpointHit) must be at the CALLER
level, not inside the function where watch() is active.

Pattern for all tests that expect WatchpointHit:
    - Define a small nested helper _code() that contains the watch() call
      and the code change. This is the monitored frame.
    - In the test body (unmonitored frame), wrap _code() with pytest.raises().

Tests that expect NO WatchpointHit (silence checks) work fine inline
because the callbacks never raise.

Timing note: sys.monitoring LINE events fire BEFORE the line executes,
so change detection fires one line AFTER the assignment. The helper
function includes a `pass` sentinel after each assignment to give the
LINE callback a chance to fire and raise.
"""


class _SampleObj:
    """Simple class for attribute watch tests."""
    def __init__(self, val):
        self.val = val


def _inner(registry_ref):
    """Helper: set a watch inside a called function."""
    z = 10
    watch("z")
    z = 20   # WatchpointHit triggered when the next line fires
    pass     # detection fires here


class _FakeCode:
    """Stand-in for a code object. Carries `co_filename` for the walk-up
    logic and `co_name` + `co_lines()` for the sequential-bps slot
    allocator in `WatchpointRegistry._compute_bp_target`.

    `co_lines()` mimics CPython's `code.co_lines()` return shape:
    iterable of `(start_byte, end_byte, line)` triples. Tests that need
    `_next_code_line_in` to find a follow-up line pass `code_lines=[...]`;
    the byte offsets are 0/0 since the slot allocator ignores them.
    """

    def __init__(self, filename, code_lines=None, name="<fake>"):
        self.co_filename = filename
        self.co_name = name
        self._code_lines = list(code_lines) if code_lines is not None else []

    def co_lines(self):
        return ((0, 0, ln) for ln in self._code_lines)


class _FakeFrame:
    """Stand-in for a Python frame. Carries just enough surface area
    (`f_code.co_filename` + `f_back` + `f_lineno` + `f_globals`) for
    `_find_user_code_caller` and the diagnostic-log path in
    `_handle_hit`.

    We don't try to make these usable with `_pause_via_pydevd` – the
    walk-up tests stub the pydevd-side functions out, so the fake
    frame never reaches code that expects a real frame.
    """

    def __init__(self, filename, f_back=None, f_lineno=0, module_name="",
                 code_lines=None, name="<fake>"):
        self.f_code = _FakeCode(filename, code_lines=code_lines, name=name)
        self.f_back = f_back
        self.f_lineno = f_lineno
        self.f_globals = {"__name__": module_name}


def _shared_watched_function(label, barrier):
    """Helper used by test_two_threads_watch_same_code_independently."""
    x = label
    watch("x")
    barrier.wait(timeout=5.0)  # rendezvous so both threads have armed before changing
    x = label + 100
    pass


async def _watched_coroutine(label):
    import asyncio
    x = label
    watch("x")
    await asyncio.sleep(0)   # yield to event loop – watch must survive
    x = label + 100
    pass


async def _coroutine_with_await():
    import asyncio
    x = 1
    watch("x")
    await asyncio.sleep(0)
    x = 99
    pass


class _RequestLike:
    """Mimics a Flask/Django request: a user-defined object whose attributes
    are mutated in-place during request handling."""
    def __init__(self):
        self.method = "GET"
        self.user = None
        self.external_user = None


def _wrap_for_lambda_test(val):
    """Helper: a Python function called from inside the test's lambda so
    the rebind has a line to fire from. Without this indirection the
    lambda body is just a single expression with no LINE event after a
    rebind. We're testing propagation INTO `_wrap_for_lambda_test` here –
    the lambda itself just relays the call.
    """
    val = "rebound-via-lambda-arg"
    pass
    return val


class _ContainerHolder:
    """Plain user-defined object with a list/dict/set attribute. Mirrors
    the shape of the user's `onboarding_dto.onboarding_settings` – the
    container is reached via a dotted path, watched specifically, and
    later mutated through aliasing + a method call inside a helper.
    """
    def __init__(self):
        self.items = []          # list to wrap
        self.bag = {}            # dict to wrap
        self.tags = set()        # set to wrap


class _Settings:
    """User-defined leaf object for the recursive-watch fixtures."""
    def __init__(self):
        self.current_step = "init"
        self.room_types = []
        self.config = {}


class _Dto:
    """User-defined parent object holding `_Settings` and other state.
    Mirrors the user's `OnboardingDto.onboarding_settings` shape – the
    interesting watch target is the root DTO; changes happen on `.settings.*`
    or further nested.
    """
    def __init__(self):
        self.settings = _Settings()
        self.tags = set()
        self.name = "anonymous"


class _DjangoLikeMeta(type):
    """Stand-in for Django's `ModelBase`: refuses to build a subclass of
    any class already created with this metaclass (mimics ModelBase
    requiring `Meta.app_label` + INSTALLED_APPS membership). Allows
    class-level `__setattr__` assignment, which mirrors real Django:
    `ModelBase` doesn't override the metaclass's `__setattr__`."""
    def __new__(mcs, name, bases, namespace):
        if bases and any(
            isinstance(b, _DjangoLikeMeta) and getattr(b, "_django_like_real", False)
            for b in bases
        ):
            raise RuntimeError(
                f"Model class {namespace.get('__qualname__', name)} doesn't "
                f"declare an explicit app_label and isn't in an application "
                f"in INSTALLED_APPS."
            )
        return super().__new__(mcs, name, bases, namespace)


class _DjangoLikeModel(metaclass=_DjangoLikeMeta):
    """Mock of a Django Model instance. The metaclass refuses our
    `_WatchedAnyAttrSubclass(...)` / `_WatchedSubclass(...)` dynamic
    subclassing, so the watch falls back to classpatch."""
    _django_like_real = True
    def __init__(self):
        self.name = "django-thing"
        self.tag = "default"


class _StubbornDjangoLikeMeta(_DjangoLikeMeta):
    """Refuses dynamic subclassing AND refuses class-level `__setattr__`
    assignment. Exercises the rare 'even classpatch failed' path so we
    can confirm the dotted watch surfaces a clean TypeError and the
    bare-name watch falls through to local-variable rebind detection."""
    def __setattr__(cls, name, value):
        if name == "__setattr__":
            raise TypeError(
                "_StubbornDjangoLikeMeta refuses __setattr__ install on the class."
            )
        super().__setattr__(name, value)


class _StubbornDjangoLikeModel(metaclass=_StubbornDjangoLikeMeta):
    """Instance of a class whose metaclass refuses BOTH dynamic subclassing
    AND class-level `__setattr__` assignment – neither class-surgery nor
    classpatch can instrument it."""
    _django_like_real = True
    def __init__(self):
        self.name = "stubborn-thing"


def _django_like_set_via_method(obj, new_name):
    """Method-style helper that internally does `self.field = value`.
    Mirrors the user-reported `relation.set_accessible_products(...)`
    pattern, where the method body rebinds an attribute on `self` and
    we want the classpatch fallback to intercept it."""
    obj.name = new_name


from _pycharm_watchpoint import (
    _is_user_defined_type, _find_user_caller, _RUNTIME_FILENAMES,
    _MAX_SUB_WATCHES_PER_ROOT,
)


import sys as _sys_for_safeguards


class _FakeDjangoFieldDescriptor:
    """Stand-in for a Django ORM internal object. We don't want to recurse
    into this when watching a DTO that references it, because real Django
    descriptors have cyclic relationships (`field.remote_field.field…`)
    that would explode the watch tree."""
    def __init__(self):
        self.cached_state = None


_FakeDjangoFieldDescriptor.__module__ = "django.db.models.fields.related_descriptors"


class _UserDtoWithFrameworkField:
    """Mimics a user DTO that holds a reference to a framework object.
    We expect: watcher fires on `dto.*` mutations; framework object's
    internals are left alone."""
    def __init__(self):
        self.label = "alpha"
        self.framework_obj = _FakeDjangoFieldDescriptor()


_module_var_for_global_test = 100


def _decode_locate_payload(payload):
    """Decode a `_pycharm_locate_watches` result into a set of (name, frame_id).

    Mirrors the Kotlin-side decoder: base64 of UTF-8, records separated by
    \\x01, each record `name\\x00frameid`.
    """
    import base64
    if not payload:
        return set()
    raw = base64.b64decode(payload).decode("utf-8")
    out = set()
    for record in raw.split("\x01"):
        name, frame_id = record.split("\x00")
        out.add((name, int(frame_id)))
    return out
