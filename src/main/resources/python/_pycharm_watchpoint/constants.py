"""Foundational constants, the runtime version guard, and the three
cross-module mutable globals (accessed elsewhere as ``constants.X``)."""


import sys
import os
import builtins
import threading
import weakref
from typing import Any, Optional, Tuple

if sys.version_info < (3, 12):
    raise RuntimeError("watchpoint.py requires Python 3.12+.")

# When truthy, [WATCHPOINT] lines are also printed to stderr (Debug Console).
# Set PYCHARM_WATCHPOINT_LOG=1 to enable. The Kotlin plugin always injects
# this when launching via "Debug with Watchpoint", so the Debug Console shows
# output automatically during IDE sessions. Unset (default) keeps stderr clean
# for scripts/tests that don't need the noise. All output still goes to the
# file sink at /tmp/pythonwatchpoint.log regardless of this flag.
_WATCHPOINT_LOG: bool = os.environ.get('PYCHARM_WATCHPOINT_LOG') == '1'

_monitoring = sys.monitoring


# Sentinel for weak-ref dereference when referent is dead.
_DEAD = object()


def _make_weak_or_strong(obj):
    """Return a callable that yields `obj` when called – weakref.ref if the
    object supports it, otherwise a strong-ref lambda. Returns None-returning
    callable if obj is None.
    """
    if obj is None:
        return lambda: None
    try:
        return weakref.ref(obj)
    except TypeError:
        # Objects that don't support weakref (e.g. some C-extension instances,
        # or objects with __slots__ but no __weakref__ slot). Fall back to a
        # strong reference wrapped in a callable for uniform access.
        return lambda: obj



# Flag: when set to a thread ident, _handle_hit silently drops mutations
# on THAT thread only. Set around the installation flow (watch_at →
# add_watch → _instrument_object_tree) to suppress side-effect hits caused
# by our own tree walk triggering lazy evaluation (e.g.
# SimpleLazyObject.__getattr__ → __setattr__). Thread-scoped so that real
# mutations on OTHER user threads are not accidentally suppressed while the
# IDE evaluator thread is mid-installation.
_installing_watch_thread: Optional[int] = None

# Cross-module mutable globals. Accessed by sibling modules as `constants.X`
# (never `from .constants import X`, which would snapshot the value). Writers
# assign `constants.X = ...` directly.

# sys.monitoring tool id, claimed lazily by registry._setup_monitoring.
_TOOL_ID: "Optional[int]" = None
