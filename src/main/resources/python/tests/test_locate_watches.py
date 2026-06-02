"""Band 17: _pycharm_locate_watches cross-frame watch location for the IDE."""


import gc
import sys
import inspect
import weakref
import builtins
import pytest

from _pycharm_watchpoint import watch, unwatch, clear_watches, WatchpointHit
from _pycharm_watchpoint import (
    builtins,
    sys,
)

from util import (
    _decode_locate_payload,
)


def test_locate_watches_reports_local_watch():
    """A local-variable watch is reported as (name, id(frame)) for the exact
    frame it was armed in – the renderer's frame-scoped match relies on this.
    """
    import sys

    secret = [1, 2, 3]
    assert secret  # silence "unused" – the watch resolves it by name
    watch("secret")
    pairs = _decode_locate_payload(builtins._pycharm_locate_watches())
    this_frame_id = id(sys._getframe())

    assert ("secret", this_frame_id) in pairs, (
        f"local watch should be located in its own frame, got {pairs}"
    )


def test_locate_watches_finds_object_by_identity_across_frames():
    """An object watch is located in EVERY live frame that holds the same
    object – under each frame's own local name – so the icon shows in caller
    and callee frames, not just the one the watch was armed in.
    """
    import sys

    class _Req:
        """Minimal user-defined object eligible for object-wide watching."""
        def __init__(self):
            self.n = 0

    located = {}

    def _callee(passed):
        """Inspect the located pairs while both frames are live on the stack."""
        located["pairs"] = _decode_locate_payload(builtins._pycharm_locate_watches())
        located["callee_id"] = id(sys._getframe())
        located["caller_id"] = id(sys._getframe().f_back)

    def _caller():
        req = _Req()
        watch("req")            # object-wide watch (class surgery + rebind local)
        _callee(req)            # same object visible here as `passed`

    _caller()
    pairs = located["pairs"]
    assert ("req", located["caller_id"]) in pairs, (
        f"watched object should be located in the caller frame as 'req', got {pairs}"
    )
    assert ("passed", located["callee_id"]) in pairs, (
        f"watched object should be located in the callee frame as 'passed', got {pairs}"
    )


def test_locate_watches_empty_when_nothing_watched():
    """With no armed watches the builtin returns the empty string – the IDE
    treats this as authoritative ("clear the cross-frame set").
    """
    clear_watches()
    assert builtins._pycharm_locate_watches() == ""
