"""Frame-chain walkers and library/runtime/pydevd filename classification."""


import os
import sys
from typing import Any, Optional


from . import constants
from .constants import Any, Optional, os
from .helpers import _FRAMEWORK_MODULE_ROOTS, _MAX_FRAME_WALK_HOPS


# Filename markers identifying our own runtime frames. When the runtime
# is exec'd by sitecustomize (production path) every frame here has
# co_filename = "<string>". When loaded via normal import (test path)
# the filename is this file's absolute path; captured at module load.
# `_find_user_caller` walks past frames matching either to find the
# real caller, so a hit's source line is the user's mutation, not our
# `super().__setattr__` line inside `_WatchedAnyAttrSubclass`.
_RUNTIME_FILENAMES = frozenset(
    f for f in ("<string>", globals().get("__file__")) if f
)


def _find_user_caller(start_frame: Any) -> Any:
    """Walk `f_back` from `start_frame` until co_filename leaves the
    runtime, or until we run out of frames / hit the safety limit.

    Returns the first user-code frame, or None if every frame in the
    chain is the watchpoint runtime (which would mean a watcher fired
    inside our own instrumentation – callers should drop the hit).
    """
    f = start_frame
    safety = _MAX_FRAME_WALK_HOPS
    while f is not None and safety > 0:
        if not _is_runtime_filename(f.f_code.co_filename):
            return f
        f = f.f_back
        safety -= 1
    return None


_STDLIB_DIR: Optional[str] = None
try:
    # `os.__file__` is `<base_prefix>/lib/pythonX.Y/os.py`; the directory
    # is the stdlib install location. Computed once at module load so
    # `_is_library_filename` stays O(1). Wrapped in try/except because
    # `__file__` can be missing in exotic embedded interpreters.
    import os as _os_for_stdlib_dir
    _STDLIB_DIR = _os_for_stdlib_dir.path.dirname(
        _os_for_stdlib_dir.path.abspath(_os_for_stdlib_dir.__file__)
    )
    _STDLIB_DIR_PREFIX = _STDLIB_DIR + _os_for_stdlib_dir.sep
    del _os_for_stdlib_dir
except Exception:
    _STDLIB_DIR_PREFIX = None  # type: ignore


# Module-load fingerprint: incremented every time we cut a new version
# of the runtime. Logged on import via `_log_warn` so when a user reports
# a bug we can confirm from /tmp/pythonwatchpoint.log which version of
# the runtime is actually loaded in their session – distinguishing
# "my fix didn't help" from "you're running an older bundled copy."
_RUNTIME_VERSION = "2026-06-03-drop-pydevd-anchored-hit-v28"


def _is_pydevd_internal(filename: str) -> bool:
    """True if *filename* belongs to PyCharm's pydevd / debugger-helper infrastructure.

    PyDevD will NOT pause on `LineBreakpoint`s installed inside its own code, so these
    frames must be filtered out when searching for a bp target in `_compute_bp_targets`.
    The substrings cover all known PyCharm versions regardless of install location or OS.

    Note: this is intentionally narrower than `_is_library_filename`. Third-party
    site-packages frames ARE valid bp targets (pydevd pauses there fine); only
    pydevd's own infrastructure silently drops the pause.
    """
    return (
        "helpers/pydev" in filename
        or "pydevd_bundle" in filename
        or "_pydev_bundle" in filename
    )


def _is_library_filename(path: str) -> bool:
    """True if `path` lives under a Python install (stdlib or
    site-packages / dist-packages).

    Cheap substring + prefix check. Targets the cases where pydevd's
    "do not step into library code" filter swallows a `CMD_STEP_OVER`:

    - **site-packages / dist-packages**: third-party libraries
      (Django, SQLAlchemy, pandas, ...). Substring match because the
      path varies wildly across virtualenvs / system installs / uv /
      poetry / etc.
    - **Stdlib**: under the directory of `os.__file__`. The
      user-reported `copy.deepcopy(qs)` case proved this: the mutation
      site was inside Django (`query.py:289`, site-packages), but the
      next user-code-looking frame walking up was `copy.py:143` –
      Python stdlib, NOT site-packages. Without the stdlib filter we
      anchored the pause on `copy.py:143`, which pydevd's library
      filter then swallowed, and the debugger silently never stopped.

    Why the previous "don't filter stdlib" stance was wrong: the
    argument was "user code passing through stdlib helpers is still
    user code". True in the abstract, but irrelevant for pause-anchor
    purposes: pydevd's filter treats stdlib the same as
    site-packages, so anchoring there is equally broken. The user's
    OWN code – outside any Python install root – is the only place
    pydevd will actually pause for us.
    """
    try:
        import os as _os_for_user_roots
        user_roots = _os_for_user_roots.environ.get(
            "PYCHARM_WATCHPOINT_USER_ROOTS", ""
        )
        for root in user_roots.split(_os_for_user_roots.pathsep):
            if not root:
                continue
            norm_root = _os_for_user_roots.path.abspath(root)
            norm_path = _os_for_user_roots.path.abspath(path)
            if norm_path == norm_root or norm_path.startswith(
                norm_root + _os_for_user_roots.sep
            ):
                return False
    except Exception:
        pass
    if "site-packages" in path or "dist-packages" in path:
        return True
    if _STDLIB_DIR_PREFIX is not None and path.startswith(_STDLIB_DIR_PREFIX):
        return True
    # IDE/debugger infrastructure – pydevd's own helpers live under the
    # PyCharm app bundle (e.g. helpers/pydev/pydevd.py). Including them here
    # ensures `_find_user_code_caller` (used for safety-net bp targets) skips
    # past pydevd frames and lands on actual user code.
    return _is_pydevd_internal(path)


def _find_user_code_caller(start_frame: Any) -> Any:
    """Walk `f_back` past runtime AND third-party library frames to
    find the nearest user-code frame.

    Different from `_find_user_caller`, which only skips OUR runtime
    (`<string>` / this file). This one ALSO skips site-packages /
    dist-packages frames, so the result is the user's own code – the
    place the user can actually navigate to and reason about.

    Why this exists: pydevd's `CMD_STEP_OVER + step_stop = library_frame`
    is silently filtered out by PyCharm's "do not step into library
    code" setting (and most users have it on). When the watched
    mutation happens inside Django's `QuerySet._clone()` or
    SQLAlchemy's session flush, anchoring the pause on that library
    frame means pydevd's tracer skips the step-over entirely and the
    cascade via PY_RETURN dies in further library frames before
    reaching user code. Result: the watchpoint hit fires, the IDE
    highlights the mutation site, but the debugger never actually
    pauses.

    Anchoring on user code in the first place avoids the filter-vs-
    step-over interaction entirely: the pause lands at the user's
    code that called into the library, which is what the user
    actually wants ("I called .all() on line 956 of my code; that
    triggered Django to do an internal mutation; show me the line in
    MY code where I did that").

    Returns the first non-runtime, non-library frame, or None if the
    entire chain is runtime / library. None means "no user code to
    anchor on – drop the hit": there's nowhere meaningful to pause,
    and showing a highlight without a corresponding pause is worse
    UX than silence.
    """
    f = start_frame
    safety = _MAX_FRAME_WALK_HOPS
    while f is not None and safety > 0:
        path = f.f_code.co_filename
        if not _is_runtime_filename(path) and not _is_library_filename(path):
            # Path checks catch site-packages / stdlib. Also reject frames
            # from known debugger/framework module roots installed OUTSIDE
            # site-packages – e.g. PyCharm's bundled pydevd lives in a
            # Gradle cache path like
            #   .../pycharm-community-2025.1.../helpers/pydev/
            # which contains no "site-packages" segment and is not under
            # the stdlib prefix, so `_is_library_filename` passes it
            # through. Checking the frame's __name__ root against
            # _FRAMEWORK_MODULE_ROOTS catches these regardless of where
            # the debugger is physically installed.
            mod_root = (f.f_globals.get("__name__", "") or "").partition(".")[0]
            if mod_root not in _FRAMEWORK_MODULE_ROOTS:
                return f
        f = f.f_back
        safety -= 1
    return None



# Directory of the package on disk – every runtime submodule lives here. With
# the package shipped as real files (not exec'd as "<string>"), a frame belongs
# to our runtime when its co_filename sits under this directory. `<string>` is
# kept for the legacy exec path and because a test pins it in _RUNTIME_FILENAMES.
_RUNTIME_DIR = os.path.dirname(os.path.abspath(__file__))


def _is_runtime_filename(path: str) -> bool:
    """True if *path* is one of the watchpoint runtime's own source frames.

    Replaces the old single-file `co_filename in _RUNTIME_FILENAMES` check: the
    runtime now spans several files under `_RUNTIME_DIR`, so we match by
    directory prefix (plus the `<string>` exec marker) instead of exact equality.
    """
    if not path:
        return False
    if path == "<string>":
        return True
    try:
        return os.path.abspath(path).startswith(_RUNTIME_DIR + os.sep)
    except Exception:
        return False
