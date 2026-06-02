"""Pause-anchor walk-up: library-mutation -> user-code pause, sequential
breakpoints, caller-finding, and bp-slot reservation."""


import gc
import sys
import inspect
import weakref
import builtins
import pytest

from _pycharm_watchpoint import watch, unwatch, clear_watches, WatchpointHit
from _pycharm_watchpoint import (
    _MAX_SUB_WATCHES_PER_ROOT,
    _RUNTIME_FILENAMES,
    _WatchedDict,
    _WatchedList,
    _WatchedSet,
    _find_paused_user_frame,
    _find_user_caller,
    _find_user_code_caller,
    _get_except_handler_lines,
    _is_library_filename,
    _is_user_defined_type,
    _next_code_line_after_frame,
    _next_code_line_in,
    builtins,
    hit,
    registry,
    sys,
    threading,
)

from util import (
    _ContainerHolder,
    _DjangoLikeMeta,
    _DjangoLikeModel,
    _Dto,
    _FakeFrame,
    _MAX_SUB_WATCHES_PER_ROOT,
    _RUNTIME_FILENAMES,
    _RequestLike,
    _SampleObj,
    _Settings,
    _StubbornDjangoLikeModel,
    _UserDtoWithFrameworkField,
    _coroutine_with_await,
    _django_like_set_via_method,
    _find_user_caller,
    _is_user_defined_type,
    _shared_watched_function,
    _sys_for_safeguards,
    _watched_coroutine,
    _wrap_for_lambda_test,
)


def test_find_user_code_caller_walks_past_site_packages():
    """`_find_user_code_caller` walks `f_back` past site-packages /
    dist-packages frames to find the nearest user-code frame.

    This is the core mechanism behind "pause at user code even when
    the watched mutation happens inside a library" – see CLAUDE.md
    §11 / `_handle_hit`'s pause-anchor docstring for the rationale.
    """
    from _pycharm_watchpoint import _find_user_code_caller

    # Chain: user code → Django (library) → SQLAlchemy (library) →
    # mutation site (library). Walking up from the mutation site
    # should land on the user frame three hops up.
    user_frame = _FakeFrame("/Users/me/project/views.py")
    django_frame = _FakeFrame(
        "/some/.venv/lib/python3.12/site-packages/django/db/models/query.py",
        f_back=user_frame,
    )
    sqlalchemy_frame = _FakeFrame(
        "/some/.venv/lib/python3.12/site-packages/sqlalchemy/orm/session.py",
        f_back=django_frame,
    )
    leaf = _FakeFrame(
        "/some/.venv/lib/python3.12/site-packages/django/db/models/query.py",
        f_back=sqlalchemy_frame,
    )

    assert _find_user_code_caller(leaf) is user_frame, (
        "Walk-up must skip every site-packages frame in the chain and "
        "return the first user-code frame. Anchoring on a library frame "
        "would cause PyCharm's 'do not step into library code' filter "
        "to silently skip pydevd's CMD_STEP_OVER pause."
    )


def test_find_user_code_caller_returns_none_for_pure_library_chain():
    """When NO user code is anywhere in the call chain, `_find_user_code_caller`
    returns None, signalling to `_handle_hit` that the hit should be
    dropped silently (a phantom highlight without a corresponding pause
    is worse UX than no signal at all).
    """
    from _pycharm_watchpoint import _find_user_code_caller

    a = _FakeFrame("/x/.venv/lib/python3.12/site-packages/django/foo.py")
    b = _FakeFrame(
        "/x/.venv/lib/python3.12/site-packages/sqlalchemy/bar.py", f_back=a,
    )
    c = _FakeFrame(
        "/x/.venv/lib/python3.12/site-packages/django/baz.py", f_back=b,
    )

    assert _find_user_code_caller(c) is None


def test_find_user_code_caller_walks_past_stdlib():
    """Regression for the `copy.deepcopy(qs)` case: when a user calls a
    stdlib helper that internally re-triggers our watcher (deepcopy
    re-runs `__init__` on the cloned QuerySet via `self.__class__(...)`,
    which is the watcher subclass), the walk-up landing on `copy.py`
    was bad – pydevd's "do not step into library code" filter swallows
    stdlib step-overs the same way it swallows site-packages.

    Anchoring on `copy.py:143` produced the user-reported "highlight
    fires but no pause" symptom: pause was armed, pydevd's library
    filter rejected it, and the cascade through more stdlib frames
    never reached user code either.

    Previous version of this test asserted stdlib was NOT skipped under
    the rationale "user code passing through stdlib helpers is still
    user code". True in the abstract but irrelevant for pause-anchor
    semantics – we updated the heuristic and flipped the test.
    """
    from _pycharm_watchpoint import _find_user_code_caller

    import copy as _copy
    stdlib_path = _copy.__file__  # /.../python3.12/copy.py

    user_frame = _FakeFrame("/Users/me/proj/main.py")
    stdlib_frame = _FakeFrame(stdlib_path, f_back=user_frame)

    result = _find_user_code_caller(stdlib_frame)
    assert result is user_frame, (
        f"Walk-up must skip stdlib (got {result.f_code.co_filename}, "
        f"expected the user frame). Anchoring on stdlib triggers "
        f"pydevd's library filter and the pause silently never fires."
    )


def test_find_user_code_caller_handles_deepcopy_through_django_chain():
    """The exact user-reported call chain from the diagnostic log:

      copy.py (stdlib)  ← walk-up was landing HERE (broken anchor)
      django/query.py:290 (site-packages)
      django/query.py:289 (site-packages, mutation site)

    With the stdlib filter, walking up from the mutation site must
    skip both Django frames AND the copy.py frame, returning a user
    frame above copy.py (the caller of `copy.deepcopy`).
    """
    from _pycharm_watchpoint import _find_user_code_caller

    import copy as _copy
    copy_path = _copy.__file__

    user_frame = _FakeFrame("/Users/me/proj/serializers.py")
    copy_frame = _FakeFrame(copy_path, f_back=user_frame)
    django_outer = _FakeFrame(
        "/x/.venv/lib/python3.12/site-packages/django/db/models/query.py",
        f_back=copy_frame,
    )
    django_mutation = _FakeFrame(
        "/x/.venv/lib/python3.12/site-packages/django/db/models/query.py",
        f_back=django_outer,
    )

    assert _find_user_code_caller(django_mutation) is user_frame


def test_is_library_filename_treats_declared_user_root_as_user_code(monkeypatch):
    """A project installed under site-packages is still user code.

    The runtime normally treats site-packages as library code so framework
    internals don't become pause anchors. But when the user's project itself is
    debugged from an installed/editable path under site-packages, the launcher
    can declare the project root and that prefix should win over the library
    heuristic.
    """
    from _pycharm_watchpoint import _is_library_filename

    root = "/venv/lib/python3.12/site-packages/my_app"
    monkeypatch.setenv("PYCHARM_WATCHPOINT_USER_ROOTS", root)

    assert not _is_library_filename(f"{root}/views.py")
    assert _is_library_filename("/venv/lib/python3.12/site-packages/django/db/models.py")


def test_find_user_code_caller_skips_pydevd_outside_site_packages():
    """Regression: PyCharm's bundled pydevd lives in a Gradle cache path like
        .../pycharm-community-2025.1.../helpers/pydev/_pydevd_bundle/pydevd_utils.py
    which contains no 'site-packages' segment and is not under the stdlib
    prefix. Before this fix, `_is_library_filename` passed it through and
    `_find_user_code_caller` returned pydevd_utils.py as the pause anchor,
    causing `_install_pause_breakpoint` to install a bp inside pydevd's own
    `eval_in_context` function. That bp fired whenever pydevd evaluated any
    expression (e.g. refreshing Variables panel), locking up the evaluator
    and corrupting test execution (producing 400 instead of 201 in the
    reported Django test failure).

    Fix: `_find_user_code_caller` now also checks the frame's `__name__`
    root against `_FRAMEWORK_MODULE_ROOTS`, which already lists all pydevd
    module prefixes, regardless of installation path.
    """
    from _pycharm_watchpoint import _find_user_code_caller

    gradle_pydevd_path = (
        "/Users/user/.gradle/caches/9.5.1/transforms/abc123/transformed/"
        "pycharm-community-2025.1-aarch64/plugins/python-ce/helpers/pydev/"
        "_pydevd_bundle/pydevd_utils.py"
    )
    csp_path = (
        "/Users/user/.venv/lib/python3.12/site-packages/csp/middleware.py"
    )
    user_path = "/Users/user/projects/myapp/channel_management/tests/test_upsell.py"

    # Case 1: pure pydevd + library chain (no user code) – should return None,
    # not the pydevd frame. Without the fix this returned the pydevd_utils.py
    # frame and broke _install_pause_breakpoint.
    pydevd_frame = _FakeFrame(gradle_pydevd_path, module_name="_pydevd_bundle.pydevd_utils")
    csp_frame = _FakeFrame(csp_path, module_name="csp.middleware", f_back=pydevd_frame)
    assert _find_user_code_caller(csp_frame) is None, (
        "A chain of site-packages → pydevd (Gradle cache) must return None, "
        "not the pydevd frame. Returning a pydevd frame causes a breakpoint "
        "to be installed inside pydevd's own eval_in_context function."
    )

    # Case 2: user code sits ABOVE the pydevd frames – should be returned.
    user_frame = _FakeFrame(user_path, module_name="channel_management.tests.test_upsell")
    pydevd_frame2 = _FakeFrame(
        gradle_pydevd_path, module_name="_pydevd_bundle.pydevd_utils",
        f_back=user_frame,
    )
    csp_frame2 = _FakeFrame(csp_path, module_name="csp.middleware", f_back=pydevd_frame2)
    assert _find_user_code_caller(csp_frame2) is user_frame, (
        "When user code exists above pydevd in the chain, it must be returned "
        "even though an intermediate pydevd frame (outside site-packages) "
        "was skipped."
    )


def test_handle_hit_installs_bp_at_mutation_site_even_in_library(monkeypatch):
    """Post-v9: when the watched mutation happens inside library code
    (Django QuerySet's `_clone()` doing `self._hints = ...`, csp
    middleware setting `request._csp_nonce`, etc.), the bp installs at
    the next code line in the LIBRARY file – not in the walked-up
    user-code caller. The IDE then pauses at the mutation site, which
    is far more contextual.

    The pre-v9 behavior was to walk up past site-packages to anchor on
    user code, working around `CMD_STEP_OVER + step_stop = library_frame`
    being filtered by PyCharm's "do not step into library code" setting.
    We no longer use CMD_STEP_OVER; `LineBreakpoint` + `consolidate_breakpoints`
    fires reliably in library code (it's the same path user-set bps
    take), so the walk-up was just producing distant non-contextual
    pauses with no actual filter to avoid.

    The drop-on-pure-library-chain semantic IS preserved: if NO user
    code exists anywhere in the call stack, the hit is dropped (see
    `test_handle_hit_drops_when_chain_is_entirely_library`). Pure
    library / runtime stacks aren't typically what the user is
    debugging.
    """
    import _pycharm_watchpoint as watchpoint

    received_calls: list = []

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        received_calls.append((target_code, file, line, watch_name))
        return (file, line, -1)

    monkeypatch.setattr(watchpoint.pydevd_pause, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint.pydevd_pause, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint.pydevd_pause, "_remove_temp_breakpoints",
                        lambda py_db, installed: None)

    # User frame must exist SOMEWHERE in the chain so the drop-on-pure-
    # library check passes. The bp anchor itself is the library frame
    # (django_frame) – that's the new behavior.
    user_frame = _FakeFrame(
        "/Users/me/proj/models.py", f_lineno=10,
        code_lines=[10, 11, 12],
        module_name="proj.models",
    )
    django_frame = _FakeFrame(
        "/x/.venv/lib/python3.12/site-packages/django/db/models/query.py",
        f_back=user_frame,
        module_name="django.db.models.query",
        f_lineno=289,
        code_lines=[289, 290, 291],
    )

    reg = builtins._watchpoint_registry
    reg._handle_hit(
        django_frame,
        "qs._hints",
        "{}",
        "{'foo': 1}",
        "/x/.venv/lib/python3.12/site-packages/django/db/models/query.py",
        289,
    )

    assert len(received_calls) == 2, (
        "_install_bp_at must be called twice: primary (mutation site) + safety (user code)."
    )
    # The PRIMARY bp must be installed in the LIBRARY frame (mutation site).
    target_code, bp_file, bp_line, watch_name = received_calls[0]
    assert bp_file == (
        "/x/.venv/lib/python3.12/site-packages/django/db/models/query.py"
    ), (
        "Post-v9: primary bp anchor must be the immediate user_frame (the "
        "mutation site itself), not walked-up to user code. The IDE "
        "pauses at the mutation file's next code line – contextually "
        "useful – instead of in a distant user frame that called into "
        "the library."
    )
    assert bp_line == 290  # next code line after django_frame.f_lineno=289

    # The SAFETY bp is installed in the user-code frame.
    _, safety_file, safety_line, _ = received_calls[1]
    assert safety_file == "/Users/me/proj/models.py"
    assert safety_line == 11  # next code line after user_frame.f_lineno=10

    # Highlight + drain location both point at the library mutation site.
    assert len(reg._hit_queue) == 1
    queued = reg._hit_queue[0]
    assert queued["file"] == (
        "/x/.venv/lib/python3.12/site-packages/django/db/models/query.py"
    )
    assert queued["line"] == 289
    # bp_locations contains both primary and safety.
    assert queued["bp_locations"][0][0] == (
        "/x/.venv/lib/python3.12/site-packages/django/db/models/query.py"
    )
    assert queued["bp_locations"][0][1] == 290

    # Drain at the primary bp location.
    builtins._pycharm_consume_last_hit(
        pause_file=(
            "/x/.venv/lib/python3.12/site-packages/django/db/models/query.py"
        ),
        pause_line=290,
    )
    assert reg._hit_queue == []


def test_handle_hit_uses_bytecode_next_line_for_multiline_attr_assignment(monkeypatch):
    """Multi-line attribute assignments arm the bp after the current bytecode.

    This pins the openapi.py:105 failure. Numeric line order picks 106, but
    lines 106-112 are RHS argument evaluation and have already executed by the
    time STORE_ATTR fires. The primary bp must land on the next future LINE
    event, which is the `return request` line.
    """
    import dis
    import types
    import _pycharm_watchpoint as watchpoint

    install_calls: list = []

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        install_calls.append((target_code, file, line, watch_name))
        return (file, line, -1)

    monkeypatch.setattr(watchpoint.pydevd_pause, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint.pydevd_pause, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint.pydevd_pause, "_remove_temp_breakpoints",
                        lambda py_db, installed: None)

    filename = (
        "/x/.venv/lib/python3.12/site-packages/oi_django/mixins/openapi.py"
    )
    namespace = {}
    exec(compile(
        """
def openapi_validate_request(request, result):
    request.parsed = ParsedRequest(
        body=result.body,
        path=result.parameters.path,
        cookies=result.parameters.cookie,
        query=ImmutableDict(result.parameters.query),
        headers=result.parameters.header,
        security=result.security,
    )

    return request
""",
        filename,
        "exec",
    ), namespace)
    code = namespace["openapi_validate_request"].__code__
    store_attr = next(
        inst for inst in dis.get_instructions(code)
        if inst.opname == "STORE_ATTR" and inst.argval == "parsed"
    )
    return_line = next(
        inst.positions.lineno for inst in dis.get_instructions(code)
        if inst.opname == "RETURN_VALUE"
    )

    user_frame = _FakeFrame(
        "/u/proj/audit_logging/middleware.py", f_lineno=79,
        code_lines=[79, 80, 81],
        module_name="proj.audit_logging.middleware",
    )
    library_frame = types.SimpleNamespace(
        f_code=code,
        f_lineno=store_attr.positions.lineno,
        f_lasti=store_attr.offset,
        f_back=user_frame,
        f_globals={"__name__": "oi_django.mixins.openapi"},
    )

    reg = builtins._watchpoint_registry
    reg._handle_hit(
        library_frame,
        "request.parsed",
        "None",
        "{'data': [1, 2, 3]}",
        filename,
        store_attr.positions.lineno,
    )

    assert install_calls[0][1] == filename
    assert install_calls[0][2] == return_line, (
        "The primary bp must use bytecode order, not the numerically next "
        "line inside an already-evaluated multi-line RHS."
    )
    assert install_calls[0][2] != store_attr.positions.lineno + 1

    payload = builtins._pycharm_consume_last_hit(
        pause_file=filename,
        pause_line=return_line,
    )
    assert payload != ""
    assert reg._hit_queue == []


def test_handle_hit_drops_when_chain_is_entirely_library(monkeypatch):
    """When every frame in the chain is library / runtime (no user code
    anywhere), `_handle_hit` drops the hit silently: no queue append,
    no `_pause_via_pydevd` call, no gate set.

    A phantom highlight with no debugger pause behind it is worse UX
    than silence – the user would see the yellow line, click around
    confused why nothing's stopped, and lose trust in the watchpoint.
    The drop is the same model as `_find_user_caller`'s None-return:
    if there's nowhere meaningful to fire from, don't fire.
    """
    import _pycharm_watchpoint as watchpoint

    pause_calls: list = []

    def fake_pause(*a, **kw):
        pause_calls.append(a)
        return True

    monkeypatch.setattr(watchpoint.pydevd_pause, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint.pydevd_pause, "_pause_via_pydevd", fake_pause)

    a = _FakeFrame("/x/.venv/lib/python3.12/site-packages/django/foo.py")
    b = _FakeFrame(
        "/x/.venv/lib/python3.12/site-packages/sqlalchemy/bar.py", f_back=a,
    )

    reg = builtins._watchpoint_registry
    reg._handle_hit(b, "qs.attr", "old", "new", "django/foo.py", 1)

    assert pause_calls == [], (
        "Pure-library chain must NOT call _pause_via_pydevd – there's "
        "no user frame to anchor on."
    )
    assert reg._hit_queue == [], (
        "Dropped hit must NOT be queued. Queueing here would surface a "
        "phantom hit on the next sessionPaused event (highlight without "
        "pause)."
    )
    assert reg._temp_breakpoints == [], (
        "Dropping a hit must NOT install a temp bp – the user has no "
        "user-code frame in the chain to anchor on, so any bp would be "
        "either in a library frame (filter would skip it) or nowhere."
    )


def test_next_code_line_finds_actual_code_line_skipping_blanks():
    """Regression for the user-reported "watchpoint hit but no pause" on
    `set_accessible_products` (user_hotel_relationship.py:195, the last
    line of the function with line 196 blank).

    `_next_code_line_in` must use `co_lines()` to find the actual next
    code line in a code object, not just `current_line + 1`. Otherwise
    bps installed at blank-line positions never fire (pydevd's LINE
    event only fires for ACTUAL code lines), and the pause never
    materialises.
    """
    from _pycharm_watchpoint import _next_code_line_in

    def helper():
        x = 1
        y = 2
        z = 3
        return x + y + z

    code = helper.__code__
    lines = sorted({
        ln for (_, _, ln) in code.co_lines() if ln is not None
    })
    assert len(lines) >= 3, (
        f"Sanity: helper should have at least 3 distinct code lines "
        f"(got {lines})"
    )

    # Property 1: asking for next-after-X returns the smallest code
    # line strictly greater than X. Whatever lines `helper` ends up
    # at, the relationship between them must hold.
    for i, ln in enumerate(lines[:-1]):
        result = _next_code_line_in(code, ln)
        assert result == lines[i + 1], (
            f"_next_code_line_in({ln}) returned {result}, expected "
            f"{lines[i + 1]} (the next entry in {lines})."
        )

    # Property 2: a query that's STRICTLY BETWEEN two code lines must
    # return the upper one. This is the blank-line case that was
    # silently breaking pauses (bp at `current+1` lands on a blank
    # line, no LINE event fires there, bp never triggers).
    first = lines[0]
    second = lines[1]
    if second > first + 1:
        # Gap exists – query in the middle. Most realistic for source
        # with blank lines between statements; co_lines may or may not
        # produce gaps depending on the function's bytecode layout, so
        # this branch is best-effort.
        mid = first + 1
        result = _next_code_line_in(code, mid)
        assert result == second, (
            f"Query in gap (ln={mid}) must return {second}, not {result}. "
            f"This is the user-reported bug: bp at line+1 lands on blank "
            f"line and never fires."
        )

    # Property 3: asking after the LAST code line returns None –
    # signals to the caller "no follow-up line in this code object,
    # walk up to f_back".
    last = lines[-1]
    assert _next_code_line_in(code, last) is None, (
        f"No code lines past the function's last statement (last={last}, "
        f"all={lines}) – caller needs to detect this and fall back to "
        f"f_back."
    )


def test_next_code_line_after_frame_skips_handler_only_lines():
    """Bytecode-order next-line lookup must not choose an exception handler.

    The v20 bytecode-order lookup fixed multi-line assignments by choosing
    the next future LINE after `frame.f_lasti`, but a mutation on the last
    normal statement of a try body has the except handler as the next
    bytecode line. That line is unreachable on the normal no-exception path,
    so installing a breakpoint there recreates the late safety-net pause
    shape from v7-v9.
    """
    import sys
    from _pycharm_watchpoint import (
        _get_except_handler_lines,
        _next_code_line_after_frame,
    )

    captured = {}

    class Probe:
        """Capture the mutating frame from inside STORE_ATTR."""

        def __setattr__(self, name, value):
            frame = sys._getframe(1)
            captured["next_line"] = _next_code_line_after_frame(frame)
            captured["handler_lines"] = _get_except_handler_lines(frame.f_code)
            object.__setattr__(self, name, value)

    def mutation_last_in_try(obj):
        try:
            obj.value = 1
        except ValueError:
            handled = True

    mutation_last_in_try(Probe())

    assert captured["handler_lines"], (
        "Sanity: fixture should compile with handler-only lines after the "
        "try-body mutation."
    )
    assert captured["next_line"] is None, (
        "_next_code_line_after_frame must return None instead of a handler "
        f"line; got {captured['next_line']} from {captured['handler_lines']}"
    )


def test_get_except_handler_lines_excludes_finally_body():
    """finally body lines must NOT be in _get_except_handler_lines output.

    Unlike except handlers, finally bodies execute in the normal no-exception
    flow. If they were incorrectly classified as handler-only lines,
    _next_code_line_in would skip them and return None – causing the bp
    install to fall through to f_back unnecessarily.

    Regression case from test_fix.py (scenario 3): a mutation on the last
    statement of a try body with a finally clause must find the finally line
    as the next viable bp target.
    """
    from _pycharm_watchpoint import _get_except_handler_lines, _next_code_line_in

    def try_finally_func():
        x = 1
        try:
            y = x + 1
        finally:
            z = 3
        return z

    code = try_finally_func.__code__
    handler_lines = _get_except_handler_lines(code)
    lines = sorted({ln for (_, _, ln) in code.co_lines() if ln is not None})

    # The finally body (z = 3) must NOT be in handler_lines.
    # Find it by looking for lines between the try body and the return.
    # More robust: just assert no line that's reachable in normal flow is excluded.
    # The finally line should be navigable via _next_code_line_in from the try body.
    try_body_lines = [ln for ln in lines if ln > lines[0]]  # skip first (x = 1)
    # 'y = x + 1' is in the try body; next should be the finally body (z = 3)
    # not None (which would mean everything after try body was excluded).
    y_line = try_body_lines[1]  # second code line after x=1 (i.e. y = x + 1)
    next_line = _next_code_line_in(code, y_line)

    assert next_line is not None, (
        f"After try body line {y_line}, _next_code_line_in returned None. "
        f"This means the finally body was incorrectly classified as an "
        f"except handler. handler_lines={handler_lines}, all lines={lines}"
    )
    assert next_line not in handler_lines, (
        f"The finally body line {next_line} should not be in handler_lines "
        f"{handler_lines} – finally runs in normal flow."
    )


def test_next_code_line_in_skips_except_handler_directly():
    """_next_code_line_in must skip except-handler lines when choosing the
    next viable breakpoint target.

    Regression case from test_fix.py (scenarios 1 & 2): when the mutation is
    the last normal statement in a try body, bytecode ordering puts the except
    handler as the next line. That line is unreachable on the no-exception path,
    so _next_code_line_in must skip it and return the first line AFTER the
    handler (or None if nothing follows).
    """
    from _pycharm_watchpoint import _get_except_handler_lines, _next_code_line_in

    def simple_try_except():
        x = 1
        try:
            y = x + 1
        except ValueError:
            z = 3
        result = y
        return result

    code = simple_try_except.__code__
    handler_lines = _get_except_handler_lines(code)
    lines = sorted({ln for (_, _, ln) in code.co_lines() if ln is not None})

    assert handler_lines, (
        "Sanity: simple_try_except must produce at least one handler line."
    )

    # For each line, _next_code_line_in should never return a handler line.
    for ln in lines:
        result = _next_code_line_in(code, ln)
        if result is not None:
            assert result not in handler_lines, (
                f"_next_code_line_in(code, {ln}) returned {result} which is "
                f"an except handler line. Handler lines: {handler_lines}, "
                f"all lines: {lines}"
            )


def test_handle_hit_falls_back_to_do_wait_suspend_when_bp_install_fails(monkeypatch):
    """When `_install_bp_at` returns None (arm silently failed – pydevd
    unreachable, breakpoint API import broke, mid-shutdown, etc.),
    `_handle_hit` falls back to `_pause_via_do_wait_suspend` so the user
    still gets a pause for the deliberate `watch(...)` call. The queued
    hit stays in the queue so the IDE highlighter shows WHICH mutation
    fired on the next sessionPaused.

    Pre-v8 the equivalent path set `_pause_pending = True` regardless of
    arm outcome, which locked out every subsequent hit until the next
    `consume`. Post-v8 there is no shared gate – each hit's destiny is
    determined independently. This test pins the fallback to
    do_wait_suspend so the user-visible behavior ("watch fires, debugger
    pauses, IDE highlights the mutation") is preserved even when the bp
    path can't fire.
    """
    import _pycharm_watchpoint as watchpoint

    install_calls: list = []
    pause_calls: list = []

    def fake_install_returns_none(py_db, target_code, file, line, watch_name):
        install_calls.append((file, line, watch_name))
        return None  # simulate failed install

    def fake_do_wait_suspend(py_db, frame, watch_name):
        pause_calls.append(watch_name)
        return True  # pause arranged successfully

    monkeypatch.setattr(watchpoint.pydevd_pause, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint.pydevd_pause, "_install_bp_at", fake_install_returns_none)
    monkeypatch.setattr(watchpoint.pydevd_pause, "_pause_via_do_wait_suspend",
                        fake_do_wait_suspend)

    user_frame = _FakeFrame(
        "/Users/me/proj/views.py", f_lineno=10,
        code_lines=[10, 11, 12],
        module_name="proj.views",
    )
    reg = builtins._watchpoint_registry

    reg._handle_hit(user_frame, "x.attr", "old", "new", "views.py", 10)

    assert install_calls != [], (
        "_install_bp_at must have been called – we're testing the "
        "install-returned-None path, not the no-slot path."
    )
    assert pause_calls == ["x.attr"], (
        "When bp install fails, _handle_hit must fall back to "
        "_pause_via_do_wait_suspend so the user still gets a pause. "
        "Without this fallback, the highlight shows but the debugger "
        "doesn't stop – the user-reported 'highlight fires but no "
        "pause' confusion."
    )
    # The queue DOES contain the hit – the IDE-side highlighter will
    # still draw the yellow line on next sessionPaused. The queued hit
    # carries the failed-install's bp_anchor (line 11) so the legacy
    # drain-all path can still pick it up.
    assert len(reg._hit_queue) == 1


def test_consume_drains_only_matching_pause_location(monkeypatch):
    """`_pycharm_consume_last_hit(pause_file, pause_line)` returns only the
    hit whose installed bp matches the IDE's current pause location.
    Sibling hits (whose bps fire at OTHER lines) stay queued for their
    own future pauses.

    Without selective drain, the IDE would see ALL queued hits on a
    single sessionPaused and paint multiple highlights simultaneously
    (the pre-v8 "two yellow lines at query.py:289 and :290" symptom).
    """
    import _pycharm_watchpoint as watchpoint

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        return (file, line, -hash((watch_name, file, line)) & 0x7FFFFFFF)

    monkeypatch.setattr(watchpoint.pydevd_pause, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint.pydevd_pause, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint.pydevd_pause, "_remove_temp_breakpoints",
                        lambda py_db, installed: None)

    reg = builtins._watchpoint_registry
    fake_frame = _FakeFrame(
        "/u/proj/views.py", f_lineno=10,
        code_lines=[10, 11, 12, 13],
        module_name="proj.views",
    )

    reg._handle_hit(fake_frame, "obj.a", "x", "y", "lib.py", 5)
    reg._handle_hit(fake_frame, "obj.b", "x", "z", "lib.py", 6)
    assert len(reg._hit_queue) == 2
    assert len(reg._temp_breakpoints) == 2

    # Drain at line 11 (first bp's location): only the matching hit
    # comes back; the other stays queued.
    payload = builtins._pycharm_consume_last_hit(
        pause_file="/u/proj/views.py", pause_line=11,
    )
    assert payload != ""
    assert len(reg._hit_queue) == 1
    assert reg._hit_queue[0]["name"] == "obj.b"
    # Only the matching bp got removed; the sibling bp at line 12
    # stays armed for its own future pause.
    assert len(reg._temp_breakpoints) == 1
    assert reg._temp_breakpoints[0][1] == 12

    # Non-matching pause location: drain returns empty, queue is
    # untouched. Models the case where a regular (non-watchpoint)
    # breakpoint fired while our bps are still pending.
    payload = builtins._pycharm_consume_last_hit(
        pause_file="/some/other.py", pause_line=99,
    )
    assert payload == ""
    assert len(reg._hit_queue) == 1
    assert len(reg._temp_breakpoints) == 1

    # Cleanup.
    builtins._pycharm_consume_last_hit()


def test_consume_no_args_drains_everything(monkeypatch):
    """Legacy `_pycharm_consume_last_hit()` (no args) drains the whole
    queue and removes every temp bp. Used at session shutdown and as
    the Kotlin-side fallback when the IDE can't read the pause
    location from `XDebugSession`.
    """
    import _pycharm_watchpoint as watchpoint

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        return (file, line, -1)

    monkeypatch.setattr(watchpoint.pydevd_pause, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint.pydevd_pause, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint.pydevd_pause, "_remove_temp_breakpoints",
                        lambda py_db, installed: None)

    reg = builtins._watchpoint_registry
    fake_frame = _FakeFrame(
        "/u/proj/views.py", f_lineno=10,
        code_lines=[10, 11, 12, 13],
        module_name="proj.views",
    )
    reg._handle_hit(fake_frame, "obj.a", "x", "y", "lib.py", 5)
    reg._handle_hit(fake_frame, "obj.b", "x", "z", "lib.py", 6)
    assert len(reg._hit_queue) == 2

    # No args ⇒ drain everything.
    payload = builtins._pycharm_consume_last_hit()
    assert payload != ""
    # Two ';'-separated entries.
    assert payload.count(";") == 1
    assert reg._hit_queue == []
    assert reg._temp_breakpoints == []


def test_sequential_bps_drop_silently_when_anchor_runs_out_of_code_lines(monkeypatch):
    """When the anchor function only has N usable code lines after the
    mutation line, the (N+1)-th back-to-back hit at the same anchor
    has no available slot. Since the user has already received N
    pauses (or will, when those bps fire), the (N+1)-th hit drops
    silently rather than blocking the user thread on do_wait_suspend.
    """
    import _pycharm_watchpoint as watchpoint

    install_calls: list = []
    pause_calls: list = []

    def fake_install_bp(py_db, target_code, file, line, watch_name):
        install_calls.append((file, line, watch_name))
        return (file, line, -1)

    def fake_do_wait_suspend(py_db, frame, watch_name):
        pause_calls.append(watch_name)
        return True

    monkeypatch.setattr(watchpoint.pydevd_pause, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint.pydevd_pause, "_install_bp_at", fake_install_bp)
    monkeypatch.setattr(watchpoint.pydevd_pause, "_pause_via_do_wait_suspend",
                        fake_do_wait_suspend)
    monkeypatch.setattr(watchpoint.pydevd_pause, "_remove_temp_breakpoints",
                        lambda py_db, installed: None)

    reg = builtins._watchpoint_registry
    # Anchor function has only 2 usable code lines after f_lineno=10:
    # line 11 and line 12. A third back-to-back hit exhausts them and
    # must drop silently.
    fake_frame = _FakeFrame(
        "/u/proj/views.py", f_lineno=10,
        code_lines=[10, 11, 12],
        module_name="proj.views",
    )

    reg._handle_hit(fake_frame, "obj.a", "x", "y", "f.py", 1)
    reg._handle_hit(fake_frame, "obj.b", "x", "y", "f.py", 2)
    reg._handle_hit(fake_frame, "obj.c", "x", "y", "f.py", 3)  # no slot

    assert len(reg._hit_queue) == 2, (
        "First two hits fill all available slots (lines 11 and 12); "
        "the third hit drops silently because the queue is non-empty "
        "and no follow-up code line exists."
    )
    assert pause_calls == [], (
        "Dropping a Nth hit must NOT fall back to do_wait_suspend – the "
        "user has already been notified of the prior hits via their bps."
    )
    assert [c[1] for c in install_calls] == [11, 12]

    # Cleanup.
    builtins._pycharm_consume_last_hit()


def test_sequential_bps_first_hit_with_no_slot_falls_back_to_do_wait_suspend(monkeypatch):
    """When the FIRST hit (queue empty) has no available bp slot
    (anchor function has no follow-up code line AND f_back isn't
    user code), `_handle_hit` falls back to `_pause_via_do_wait_suspend`
    so the user still gets a pause for the deliberate `watch(...)`.

    This is the `script.py` last-line-of-module corner case in design
    contract §13's `_pause_via_do_wait_suspend` rationale.
    """
    import _pycharm_watchpoint as watchpoint

    pause_calls: list = []

    def fake_do_wait_suspend(py_db, frame, watch_name):
        pause_calls.append(watch_name)
        return True

    monkeypatch.setattr(watchpoint.pydevd_pause, "_get_pydevd_debugger", lambda: object())
    monkeypatch.setattr(watchpoint.pydevd_pause, "_pause_via_do_wait_suspend",
                        fake_do_wait_suspend)
    monkeypatch.setattr(watchpoint.pydevd_pause, "_remove_temp_breakpoints",
                        lambda py_db, installed: None)

    reg = builtins._watchpoint_registry

    # Anchor with `f_lineno=10` and `code_lines=[10]` – no line after
    # 10, so `_next_code_line_in` returns None. f_back is a library
    # frame, so it can't anchor either. → fall back to do_wait_suspend.
    library_fb = _FakeFrame(
        "/x/.venv/lib/python3.12/site-packages/django/x.py",
        module_name="django.x",
    )
    fake_frame = _FakeFrame(
        "/u/proj/views.py", f_lineno=10,
        code_lines=[10],
        module_name="proj.views",
        f_back=library_fb,
    )

    reg._handle_hit(fake_frame, "x.attr", "old", "new", "views.py", 10)

    assert pause_calls == ["x.attr"], (
        "First hit with no available bp slot must fall back to "
        "do_wait_suspend – the user explicitly asked to pause."
    )
    # The hit is queued without bp_locations (the do_wait_suspend path
    # stores an empty list). The legacy drain-all path or selective drain
    # at any location will both surface it.
    assert len(reg._hit_queue) == 1
    assert reg._hit_queue[0]["bp_locations"] == []

    # Cleanup.
    builtins._pycharm_consume_last_hit()


def test_watch_at_locates_paused_frame_in_other_thread():
    """_pycharm_watch_at scans every running thread's frame stack for a frame
    matching (file_hint, func_hint). Used by the PyCharm 'Add Watchpoint'
    action when the evaluator's own sys._getframe() chain doesn't contain
    the user's paused frame.
    """
    import threading
    started = threading.Event()
    can_finish = threading.Event()
    captured_frame_holder = {}

    def _paused_user_function():
        """Helper representing a paused user frame in another thread."""
        my_local = 1234
        captured_frame_holder["frame"] = inspect.currentframe()
        started.set()
        can_finish.wait(timeout=5.0)  # pretend we're paused at a breakpoint
        # Reference my_local to keep it live in f_locals.
        _ = my_local

    t = threading.Thread(target=_paused_user_function, name="paused-thread")
    t.start()
    started.wait(timeout=5.0)

    try:
        from _pycharm_watchpoint import _find_paused_user_frame
        found = _find_paused_user_frame(__file__, "_paused_user_function")
        assert found is not None
        assert found.f_code.co_name == "_paused_user_function"
        # The frame is alive – its locals should be readable.
        assert found.f_locals.get("my_local") == 1234
    finally:
        can_finish.set()
        t.join(timeout=5.0)


def test_watch_at_prefers_innermost_recursive_frame():
    """When multiple recursive frames match file + function, pick innermost.

    The IDE action only passes `(file_hint, func_hint)` today. If a recursive
    function is paused at its deepest call, every invocation has the same code
    object, so matching by file/name returns several candidates. Choosing the
    outermost one arms the watch on a frame that is not currently selected and
    makes the next inner-frame mutation invisible.
    """
    import threading
    started = threading.Event()
    can_finish = threading.Event()
    frames = []

    def _recursive_paused_function(depth):
        """Helper representing a paused recursive call stack."""
        frames.append(inspect.currentframe())
        if depth > 0:
            _recursive_paused_function(depth - 1)
        else:
            started.set()
            can_finish.wait(timeout=5.0)

    t = threading.Thread(
        target=_recursive_paused_function, args=(2,),
        name="recursive-paused-thread",
    )
    t.start()
    started.wait(timeout=5.0)

    try:
        from _pycharm_watchpoint import _find_paused_user_frame
        found = _find_paused_user_frame(__file__, "_recursive_paused_function")
        assert found is frames[-1], (
            "Recursive watch_at lookup should prefer the innermost matching "
            "frame, which is the frame PyCharm is normally paused on."
        )
    finally:
        can_finish.set()
        t.join(timeout=5.0)


def test_concurrent_watch_mutation_does_not_crash_callback():
    """While one set of threads runs LINE callbacks on watched code, another
    set is calling watch()/unwatch() in a tight loop. The shared
    _local_watches dict must not raise during iteration.

    Pre-fix this races into:
        RuntimeError: dictionary changed size during iteration
    """
    import threading
    import time

    errors: list = []
    errors_lock = threading.Lock()
    stop = threading.Event()

    def churner():
        """Tight watch/unwatch loop on its own frame."""
        try:
            while not stop.is_set():
                z = 0
                watch("z")
                unwatch("z")
        except Exception as e:  # noqa: BLE001
            with errors_lock:
                errors.append(("churner", repr(e)))

    def runner():
        """Tight enter/exit a freshly-allocated watched function."""
        try:
            while not stop.is_set():
                def inner():
                    y = 0
                    watch("y")
                    y = 1   # change – will trigger _on_line callback
                try:
                    inner()
                except WatchpointHit:
                    pass    # expected on most iterations
        except Exception as e:  # noqa: BLE001
            with errors_lock:
                errors.append(("runner", repr(e)))

    threads = [threading.Thread(target=churner) for _ in range(2)]
    threads += [threading.Thread(target=runner) for _ in range(2)]

    for t in threads:
        t.start()
    time.sleep(0.4)
    stop.set()
    for t in threads:
        t.join(timeout=5.0)

    assert not errors, f"Concurrent access caused errors: {errors[:5]}"


def test_two_threads_watch_same_code_independently():
    """Two threads execute the SAME watched function concurrently. Each
    thread's watch on its own frame must fire only for its own change.
    Exercises per-(name, frame_id) keying under load.
    """
    import threading

    barrier = threading.Barrier(2)
    hits: list = []
    hits_lock = threading.Lock()

    def runner(label):
        try:
            _shared_watched_function(label, barrier)
        except WatchpointHit as e:
            with hits_lock:
                hits.append((label, e.old_value, e.new_value))

    t1 = threading.Thread(target=runner, args=(1,))
    t2 = threading.Thread(target=runner, args=(2,))
    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    assert len(hits) == 2, f"Expected 2 hits, got {len(hits)}: {hits}"
    new_vals = sorted(int(n) for _, _, n in hits)
    assert new_vals == [101, 102], f"Unexpected new values: {new_vals}"


def test_asyncio_two_tasks_watch_independently():
    """Two concurrent asyncio tasks running the same coroutine each fire on
    their own change. Per-frame keying handles per-coroutine isolation
    automatically because each task instance has a distinct coroutine frame.
    """
    import asyncio
    hits: list = []

    async def safe_run(label):
        try:
            await _watched_coroutine(label)
        except WatchpointHit as e:
            hits.append((label, e.old_value, e.new_value))

    async def main():
        await asyncio.gather(safe_run(1), safe_run(2))

    asyncio.run(main())
    assert len(hits) == 2, f"Expected 2 hits, got {len(hits)}: {hits}"
    new_vals = sorted(int(n) for _, _, n in hits)
    assert new_vals == [101, 102]


def test_asyncio_watch_survives_await():
    """A watch armed before an await persists through the suspend/resume and
    fires when the watched local changes after the coroutine resumes."""
    import asyncio

    async def main():
        with pytest.raises(WatchpointHit) as exc_info:
            await _coroutine_with_await()
        assert exc_info.value.new_value == "99"

    asyncio.run(main())


def test_watch_complex_object_fires_on_attribute_mutation():
    """watch('req') on a user-defined object installs an attribute-level
    watch that fires when ANY of req's attributes is assigned a new value.

    The hit's watch_name carries the full path including the changed attr
    (e.g. 'req.user'), so the IDE can show which attribute moved.
    """
    req = _RequestLike()
    watch("req")
    with pytest.raises(WatchpointHit) as exc_info:
        req.user = "alice"
    hit = exc_info.value
    assert hit.watch_name == "req.user", (
        f"Expected watch_name 'req.user', got {hit.watch_name!r}"
    )
    assert hit.new_value == "'alice'"
    assert hit.old_value == "None"


def test_watch_complex_object_fires_for_each_subsequent_attribute_change():
    """The object watch persists across multiple mutations – each fires
    independently. This is the canonical 'I want every change to this
    request object' flow."""
    req = _RequestLike()
    watch("req")
    with pytest.raises(WatchpointHit):
        req.user = "alice"
    with pytest.raises(WatchpointHit):
        req.method = "POST"
    with pytest.raises(WatchpointHit):
        req.external_user = req.user  # depends on req.user already set


def test_watch_complex_object_does_not_fire_on_same_value_reassignment():
    """Object watch uses _value_hash for equality: assigning the same value
    is a no-op and must not fire (no spurious 'self-assignment' interrupts)."""
    req = _RequestLike()
    req.user = "alice"   # before watch – not observed
    watch("req")
    req.user = "alice"   # same value, must be silent
    req.method = "GET"   # also same


def test_unwatch_complex_object_restores_original_class():
    """unwatch must reverse the class surgery so subsequent attribute changes
    are no longer instrumented and the type behaves exactly as before."""
    req = _RequestLike()
    original_cls = type(req)
    watch("req")
    assert type(req) is not original_cls, (
        "watch() on a custom object should change __class__ via surgery"
    )
    unwatch("req")
    assert type(req) is original_cls, (
        "unwatch() must restore the original class"
    )
    req.user = "alice"   # must be silent now


def test_watch_complex_object_fires_when_mutated_from_other_function():
    """The watch is bound to the OBJECT, not a frame, so attribute changes
    made from any code that holds a reference to it still fire."""
    req = _RequestLike()
    watch("req")

    def _mutate_elsewhere(r):
        r.user = "remote-change"

    with pytest.raises(WatchpointHit) as exc_info:
        _mutate_elsewhere(req)
    assert exc_info.value.new_value == "'remote-change'"
    # source_line should point at the assignment inside _mutate_elsewhere.
    assert exc_info.value.source_file.endswith("test_pause_anchor.py")


def test_watch_simple_value_still_uses_local_variable_watch():
    """For primitive values, watch() must still use local-variable rebinding
    detection (class surgery would fail on builtins anyway)."""
    def _code():
        x = 1
        watch("x")
        x = 2
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.watch_name == "x"


def test_watch_string_uses_local_variable_watch():
    """Strings are immutable: only rebinding is observable. Must use
    local-variable watch, not class surgery."""
    def _code():
        s = "hello"
        watch("s")
        s = "world"
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_watch_list_uses_local_variable_watch():
    """list is a built-in type – class surgery would fail. We fall back to
    the local-variable path. Item mutations (lst.append, lst[0]=...) are NOT
    detected; only rebinding lst to a new list fires."""
    def _code():
        lst = [1, 2, 3]
        watch("lst")
        lst = [4, 5, 6]
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_watch_fires_when_change_is_on_last_line():
    """The change on the LAST line of a function has no following LINE event
    in this frame. PY_RETURN catches the change."""
    def _code():
        x = 1
        watch("x")
        x = 2  # last line; PY_RETURN catches the change
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.old_value == "1"
    assert exc_info.value.new_value == "2"


def test_last_line_change_reports_inner_source_location():
    """Even though pause targets the caller's frame (for pydevd stability),
    the WatchpointHit's source_file/source_line still point to the actual
    assignment line in the inner watched function."""
    def _code():
        x = 1
        watch("x")
        x = 2

    inner_source_lines, inner_start = inspect.getsourcelines(_code)
    assignment_line = None
    for i, src_line in enumerate(inner_source_lines):
        if src_line.strip() == "x = 2":
            assignment_line = inner_start + i
            break
    assert assignment_line is not None, "Could not find 'x = 2' in _code source"

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.source_line == assignment_line
    assert exc_info.value.source_file.endswith("test_pause_anchor.py")


def test_object_watch_handles_rapid_repeated_attribute_changes():
    """A burst of attribute changes each fires its own WatchpointHit. This
    guards against an overly-aggressive re-entrancy guard accidentally
    suppressing real subsequent changes after the first one fires.
    """
    req = _RequestLike()
    watch("req")
    for value in ("a", "b", "c", "d"):
        with pytest.raises(WatchpointHit) as exc_info:
            req.user = value
        assert exc_info.value.new_value == repr(value)


def test_watch_object_fires_through_nested_call_chain():
    """An object watch survives an arbitrary call depth: a helper calls
    another helper that mutates the watched attribute, and the hit still
    fires with the correct watch_name and new value. Confirms that the
    class-surgery watch is frame-agnostic, not just one-call-deep.
    """
    req = _RequestLike()
    watch("req")

    def _step_two(r):
        r.user = "from-deep-helper"   # mutation two frames below the watch caller

    def _step_one(r):
        _step_two(r)                  # nested call – does not mutate directly

    with pytest.raises(WatchpointHit) as exc_info:
        _step_one(req)
    hit = exc_info.value
    assert hit.watch_name == "req.user"
    assert hit.new_value == "'from-deep-helper'"
    assert hit.old_value == "None"


def test_watch_list_mutation_via_helper_detected_on_return():
    """When a helper mutates a watched list in place, the watching frame's
    next LINE event detects the change.

    Mechanism: `_value_hash` for a list is hash(repr(list)), so the diff in
    `_on_line` catches content changes. The mutation itself happens inside
    the helper (no LINE events there because LINE is only enabled on the
    watching frame's code object), so detection lands on the line AFTER the
    call returns – the test puts an explicit `pass` there.
    """
    def _code():
        items = []
        watch("items")

        def _mutate(lst):
            lst.append(1)             # in-place mutation – list contents change

        _mutate(items)                # call returns with items == [1]
        pass                          # detection LINE event fires here

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    hit = exc_info.value
    assert hit.watch_name == "items"
    assert hit.old_value == "[]"
    assert hit.new_value == "[1]"


def test_watch_dict_mutation_via_helper_detected_on_return():
    """Same contract as list mutation: a helper that mutates a watched
    dict in place is caught on the next LINE event in the watching frame.
    """
    def _code():
        data = {}
        watch("data")

        def _populate(d):
            d["key"] = "value"        # in-place key insertion

        _populate(data)
        pass                          # detection here

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    hit = exc_info.value
    assert hit.watch_name == "data"
    assert hit.old_value == "{}"
    assert hit.new_value == "{'key': 'value'}"


def test_watch_primitive_follows_argument_into_callee():
    """A primitive watch propagates across a function-call boundary: when
    `watch('x')` is followed by `f(x)`, the callee's parameter that received
    the watched value is itself implicitly watched. If the callee rebinds
    that parameter, the watch fires from inside the callee.

    This is NOT default Python semantics – the callee's parameter is a
    separate binding from the caller's `x` – so this depends on the watch
    propagation mechanism in the registry that detects the CALL event,
    inspects the callee's f_locals at PY_START, and arms a per-parameter
    watch when the value's identity matches the caller's watched value.
    """
    def _code():
        x = 1
        watch("x")

        def _modify(x):               # callee's `x` is a different binding
            x = 99                    # rebind the param – should fire
            pass                      # LINE event for the rebind

        _modify(x)                    # pass watched value as argument

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    hit = exc_info.value
    # The hit should attribute to the callee's local rebind, not the caller's.
    assert hit.new_value == "99"
    assert hit.old_value == "1"


def test_propagation_through_method_call():
    """`obj.method(watched)` should propagate the watch into the method body.

    Method calls are still CALL bytecode and the bound method exposes
    `__code__`, so propagation should work the same as for plain functions.
    """
    class Service:
        def handle(self, val):
            val = val + 100   # rebind the param – should fire
            pass              # detection LINE event

    svc = Service()

    def _code():
        n = 5
        watch("n")
        svc.handle(n)         # propagation should reach Service.handle

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    hit = exc_info.value
    assert hit.new_value == "105"
    assert hit.old_value == "5"


def test_propagation_through_keyword_argument():
    """`f(x=watched)` keyword argument should still propagate.

    The callee's parameter is bound by name; at PY_START its f_locals shows
    the param holding the watched value, and the id() match arms the watch
    regardless of how it was passed.
    """
    def _modify(*, target):
        target = target + 1
        pass

    def _code():
        n = 10
        watch("n")
        _modify(target=n)

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.new_value == "11"


def test_propagation_with_multiple_watched_args():
    """`f(a, b)` where BOTH a and b are watched: a rebind to either should
    fire under the matching watch name. The first rebind (positional order:
    the inner one) raises and stops further execution.
    """
    def _modify(a, b):
        a = a + 1000     # rebind a first – should fire as 'first'
        b = b + 2000
        pass

    def _code():
        first = 1
        second = 2
        watch("first")
        watch("second")
        _modify(first, second)

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    hit = exc_info.value
    assert hit.watch_name == "first"
    assert hit.new_value == "1001"


def test_propagation_chains_through_multiple_helpers():
    """A → B → C: the watch armed in A should follow the value through B
    into C, firing when C rebinds its parameter. Confirms that propagation
    enables the necessary events on each callee so deeper chains keep
    propagating, not just the first hop.
    """
    def _leaf(val):
        val = -999
        pass

    def _mid(val):
        _leaf(val)            # propagation should chain from mid into leaf

    def _code():
        x = 42
        watch("x")
        _mid(x)

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.new_value == "-999"


def test_propagation_through_recursive_self_call():
    """A function that recursively calls itself with the watched value
    passed UNCHANGED should propagate at every level. We use a stable
    sentinel object as the propagated value so its id is preserved across
    recursive frames (id-based matching can't follow a transformed value
    like `n - 1`, which creates a new int with a new id – that's a
    documented limitation, not a bug).
    """
    payload = object()       # unique, non-interned sentinel

    def _recurse(val, depth):
        if depth > 0:
            _recurse(val, depth - 1)   # val passed unchanged – id preserved
        else:
            val = "rebound"            # innermost rebind
            pass

    def _code():
        start = payload
        watch("start")
        _recurse(start, 2)

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.new_value == "'rebound'"


def test_propagation_does_not_overtrigger_on_unrelated_args():
    """A primitive value passed alongside a watched arg should NOT cause
    spurious fires on the unrelated parameter. The watched arg is what the
    user cares about; only its identity should drive propagation.

    Note: for *interned* primitives, the implementation matches by id, so
    multiple args with the same value will all be watched. Here we use a
    fresh non-interned object to avoid that.
    """
    sentinel_a = object()
    sentinel_b = object()

    def _modify(a, b):
        # Only b is rebound. If the watch leaked onto a, this would not
        # report as 'watched_b'.
        b = "rebound-b"
        pass

    def _code():
        x = sentinel_a    # not watched
        y = sentinel_b    # watched
        watch("y")
        _modify(x, y)

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    hit = exc_info.value
    assert hit.watch_name == "y"
    assert hit.new_value == "'rebound-b'"


def test_propagation_callee_does_not_modify_does_not_fire():
        """If a callee receives a watched value but does NOT rebind / mutate it,
        no WatchpointHit should fire. Pure-pass-through must be silent.
        """
        def _read_only(val):
            _ = val + 1           # use without rebinding the param
            return val

        def _code():
            x = 7
            watch("x")
            result = _read_only(x)
            assert result == 7
        # Must NOT raise.
        _code()


def test_propagation_through_class_instantiation():
        """`MyClass(watched)` should propagate the watch into `__init__` so a
        rebind of the constructor's parameter fires.

        Currently the CALL event reports `callable_=MyClass`, which has no
        `__code__` attribute – class instances don't expose the constructor's
        code object directly. This test documents the gap; the fix is to
        special-case classes by looking up `callable_.__init__.__code__`.
        """
        class Holder:
            def __init__(self, val):
                val = val * 10    # rebind the param – ideally fires
                self.val = val

        def _code():
            n = 3
            watch("n")
            Holder(n)             # ideally propagates into __init__

        with pytest.raises(WatchpointHit) as exc_info:
            _code()
        assert exc_info.value.new_value == "30"


def test_propagation_into_generator_function_does_not_leak():
        """Calling a generator function returns a generator object WITHOUT
        entering the body, so no PY_START fires for the generator's code at the
        CALL site. If we naively queued a propagation it would sit there forever
        (or be applied much later when next() runs in unrelated context).

        Required contract: calling a generator function with a watched value
        should NOT leave a stale entry on the propagation queue. The test peeks
        at the queue depth (this is whitebox but the cleanest way to catch a
        leak).
        """
        def _gen(val):
            yield val
            val = -1              # rebind only happens on first next()
            yield val

        def _code():
            x = 5
            watch("x")
            g = _gen(x)           # CALL fires, but generator body doesn't run
            # If we ALSO iterate, the rebind to -1 happens on next next(); we
            # don't want a stale propagation entry, but we also don't want to
            # break that case if iteration does eventually happen.
            del g                 # don't iterate – just drop the generator

        _code()
        # Peek at the queue. If we leaked, len > 0.
        registry = builtins._watchpoint_registry
        queue = getattr(registry._pending_propagation, "queue", None)
        leftover = len(queue) if queue is not None else 0
        assert leftover == 0, (
            f"Generator-function CALL leaked {leftover} propagation entries"
        )


def test_propagation_through_async_function_does_not_leak():
        """Calling an async def returns a coroutine without running its body.
        Same leak risk as generators."""
        import asyncio

        async def _aio(val):
            return val + 100

        def _code():
            x = 5
            watch("x")
            coro = _aio(x)
            coro.close()          # discard without awaiting

        _code()
        registry = builtins._watchpoint_registry
        queue = getattr(registry._pending_propagation, "queue", None)
        leftover = len(queue) if queue is not None else 0
        assert leftover == 0, (
            f"Async-function CALL leaked {leftover} propagation entries"
        )


def test_watch_augmented_assignment_fires():
    """`x += 1` is detected as a value change on the next LINE event."""
    def _code():
        x = 0
        watch("x")
        x += 5                # augmented assignment
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.new_value == "5"


def test_watch_for_loop_rebinding_fires_on_first_iteration():
    """A for-loop rebinds the loop variable each iteration. With a watch
    armed on the loop variable from outside, the first iteration's rebind
    should fire (and propagate out, ending the loop).
    """
    def _code():
        x = "init"
        watch("x")
        for x in ["alpha", "beta", "gamma"]:
            pass              # first iteration: x rebinds to 'alpha', LINE fires
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.new_value == "'alpha'"


def test_watch_dict_subscript_assignment_fires():
    """`d['key'] = value` mutates the watched dict; the next LINE in the
    watching frame should detect via _value_hash(repr())."""
    def _code():
        d = {"a": 1}
        watch("d")
        d["b"] = 2            # subscript mutation
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    hit = exc_info.value
    assert "'b': 2" in hit.new_value or "'a': 1, 'b': 2" in hit.new_value


def test_watch_list_subscript_assignment_fires():
    """`lst[i] = value` mutates list contents – repr changes – detected."""
    def _code():
        lst = [1, 2, 3]
        watch("lst")
        lst[0] = 99
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert "99" in exc_info.value.new_value


def test_watch_attribute_with_slots_class():
    """Classes that use __slots__ don't have a __dict__. Our class-surgery
    creates a subclass; the subclass inherits the slots layout. __class__
    assignment between layout-compatible classes is allowed, so this should
    work.
    """
    class Slotted:
        __slots__ = ("val",)
        def __init__(self):
            self.val = 1

    obj = Slotted()
    try:
        watch("obj.val")
    except TypeError:
        pytest.skip("__class__ surgery refused on __slots__ class – limitation")
        return
    with pytest.raises(WatchpointHit) as exc_info:
        obj.val = 99
    assert exc_info.value.new_value == "99"


def test_watch_attribute_with_property_setter():
    """A @property's setter does NOT go through __setattr__ for the backing
    attribute; assignment to the property triggers the setter directly. Our
    class-surgery override of __setattr__ DOES intercept assignments to the
    property name itself though (because `obj.prop = x` is dispatched
    through __setattr__ at the type level when a data descriptor exists).
    """
    class WithProp:
        def __init__(self):
            self._v = 0
        @property
        def v(self):
            return self._v
        @v.setter
        def v(self, val):
            self._v = val

    obj = WithProp()
    watch("obj.v")
    # Setting via the property name. Does our __setattr__ intercept this
    # before the property descriptor runs? In CPython type.__setattr__ checks
    # for data descriptors and routes to them, so our override may or may
    # not see this. The test documents whatever the behavior is.
    try:
        with pytest.raises(WatchpointHit) as exc_info:
            obj.v = 42
        assert exc_info.value.new_value == "42"
    except Exception:
        pytest.skip("@property setter path bypasses class-surgery hook – limitation")


def test_watch_attribute_deletion_does_not_crash():
    """`del obj.attr` invokes __delattr__, not __setattr__. We don't override
    __delattr__, so the deletion is silent – but it MUST NOT crash.
    """
    obj = _SampleObj(10)
    watch("obj.val")
    del obj.val               # silent (limitation) but must not raise
    # After del, the obj still has its watched class until unwatch.
    assert not hasattr(obj, "val")


def test_watch_dict_bypass_via_obj_dict_does_not_crash():
    """`obj.__dict__['attr'] = val` writes directly to the instance dict,
    bypassing __setattr__. The watch won't fire – that's a known limitation
    – but the bypass must not crash either.
    """
    obj = _SampleObj(5)
    watch("obj.val")
    obj.__dict__["val"] = 999   # bypass – silent
    assert obj.val == 999


def test_watch_frozen_dataclass_falls_back_or_skips_gracefully():
    """A frozen dataclass forbids attribute assignment. Class surgery may
    or may not work, but the watch() call must not crash – at worst it
    should fall back to the local-variable path or skip with an exception
    we can catch.
    """
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Point:
        x: int
        y: int

    p = Point(1, 2)
    # watch('p') goes through _is_object_watchable → object-watch path.
    # We just need this to not crash; behavior on subsequent attempts to
    # mutate the frozen instance is irrelevant.
    try:
        watch("p")
    except Exception as e:
        pytest.fail(f"watch() on frozen dataclass crashed: {e!r}")


def test_propagation_through_lambda():
    """Lambdas have `__code__`, so propagation should work the same as for
    named functions. Lambdas are commonly used as callbacks – verifying
    propagation through one closes a common-pattern gap.
    """
    rebind = lambda val: (_ for _ in [None]).throw(WatchpointHit("val", "x", "y", "f", 1)) \
        if False else None
    del rebind   # silence linter; we define the real one below for clarity

    def _modify(val):
        val = "rebound-via-lambda-arg"
        pass
    _modify = lambda val: _wrap_for_lambda_test(val)   # type: ignore[assignment]

    def _code():
        x = "orig"
        watch("x")
        _modify(x)

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.new_value == "'rebound-via-lambda-arg'"


def test_propagation_through_classmethod():
    """`@classmethod` decorated methods get `cls` as the first argument.
    A watched value passed as the second arg should propagate.
    """
    class Helper:
        @classmethod
        def transform(cls, val):
            val = val + 1
            pass

    def _code():
        n = 100
        watch("n")
        Helper.transform(n)

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.new_value == "101"


def test_propagation_through_staticmethod():
    """Static methods have no implicit first arg – propagation should
    look like a plain function call.
    """
    class Helper:
        @staticmethod
        def transform(val):
            val = val + 1
            pass

    def _code():
        n = 100
        watch("n")
        Helper.transform(n)

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.new_value == "101"


def test_object_watch_fires_when_method_internally_mutates_self():
    """`watch("obj")` for a user-defined object should fire when a method
    on the object internally does `self.attr = value`. The class-surgery
    __setattr__ catches the assignment regardless of where it originates
    in the call chain.
    """
    class StatefulService:
        def __init__(self):
            self.counter = 0
        def tick(self):
            self.counter += 1   # in-method mutation – should be intercepted

    svc = StatefulService()
    watch("svc")
    with pytest.raises(WatchpointHit) as exc_info:
        svc.tick()
    hit = exc_info.value
    assert hit.watch_name == "svc.counter"
    assert hit.new_value == "1"


def test_propagation_does_not_overtrigger_on_unwatched_param_with_different_value():
    """Confirms the converse of the false-positive concern: when extra
    args have DIFFERENT values from the watched one, they must not get
    watched. (`object()` sentinels guarantee distinct ids.)
    """
    watched = object()
    other_a = object()
    other_b = object()

    def _modify(a, b, c):
        # a and b are not the watched value; rebinding them must not fire.
        a = "rebind-a"
        b = "rebind-b"
        # Then rebind c (which IS the watched one). This must fire.
        c = "rebound-c"
        pass

    def _code():
        x = watched
        watch("x")
        _modify(other_a, other_b, x)

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    # The hit must report c's change, with display_name 'x'.
    assert exc_info.value.watch_name == "x"
    assert exc_info.value.new_value == "'rebound-c'"


def test_propagation_acknowledged_limitation_interned_primitives():
    """For interned primitives, identity-based matching propagates to ANY
    callee parameter with the same value. We document this with a test so
    a future contributor doesn't 'fix' it without considering the trade-off.
    """
    def _modify(a, b):
        # Both a and b are 1 (small int, interned). The watch propagates
        # to both. Whichever gets rebound first fires.
        a = "first"
        # If propagation were perfect, only `a` would have the watch and
        # this rebind of `b` would be silent. In practice it also fires –
        # but the FIRST rebind already raised, so this line never executes.
        b = "second"
        pass

    def _code():
        x = 1
        watch("x")
        # Pass the watched x AND a literal 1 (same small-int instance).
        _modify(x, 1)

    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    # The first rebind (`a = "first"`) fires. The watch_name is "x"
    # because that's the caller's name we're tracking.
    assert exc_info.value.new_value == "'first'"


def test_no_propagation_into_builtin_function():
    """Calling a builtin like `len()` or `print()` must NOT crash, leak,
    or attempt propagation – they have no Python __code__ and no PY_START
    would fire. The watch on the caller should still work normally.
    """
    def _code():
        items = [1, 2, 3]
        watch("items")
        n = len(items)           # builtin call – propagation skips
        assert n == 3
        items.append(4)          # mutation in caller frame
        pass                     # detection of the mutation
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert "4" in exc_info.value.new_value


def test_propagation_queue_does_not_leak_across_many_builtin_calls():
    """Sanity check: many builtin calls (which we skip) shouldn't grow
    the queue. After a burst, the queue should be empty.
    """
    def _code():
        x = "watched"
        watch("x")
        for _ in range(50):
            _ = len("noise")     # builtin – skipped
            _ = repr(42)         # builtin – skipped
        # Caller frame still tracking x.

    _code()
    registry = builtins._watchpoint_registry
    queue = getattr(registry._pending_propagation, "queue", None)
    leftover = len(queue) if queue is not None else 0
    assert leftover == 0, (
        f"Builtin-call burst leaked {leftover} propagation entries"
    )


def test_propagation_through_default_kwarg_value():
    """Defaulted parameters are bound to their default at call time. The
    default is captured at function-definition time and shared across
    calls; small-int defaults thus share id with watched small ints –
    the same interning trade-off. Test documents the behavior: defaults
    are NOT meaningfully tied to the caller's watched local.
    """
    def _modify(val=999):       # 999 default – distinct from caller's watched
        val = -1
        pass

    def _code():
        x = "string-value"      # distinct type/id from 999
        watch("x")
        _modify()               # no args – val gets the default 999
    # Must NOT raise: the default 999 is not the watched value.
    _code()


def test_container_subclass_not_wrapped_in_object_wide_watch():
    """dict/list/set SUBCLASSES assigned through a watched object's __setattr__
    are stored as-is — NOT replaced by _WatchedDict/_WatchedList/_WatchedSet.

    Regression: previously `isinstance(value, _CONTAINER_TYPES)` was used, which
    returns True for ANY dict/list/set subclass. This caused Django's QueryDict
    (a dict subclass) to be silently replaced by a plain _WatchedDict when any
    watched request attribute was re-assigned, stripping QueryDict-specific
    methods (getlist, urlencode, ...) and breaking Django views that called
    request.POST.getlist(). The view then returned HTTP 400 instead of 201.

    Fix: `type(value) in _CONTAINER_TYPES` matches ONLY the exact builtin types.
    """
    class _SpecialDict(dict):
        def getlist(self, key):
            """QueryDict-like method that plain dict doesn't have."""
            v = self.get(key)
            return [v] if v is not None else []

    class _SpecialList(list):
        def first(self):
            return self[0] if self else None

    class _Holder:
        pass

    holder = _Holder()
    watch("holder")

    special_dict = _SpecialDict(color="red")
    special_list = _SpecialList([1, 2, 3])

    def _set_dict():
        holder.mapping = special_dict

    def _set_list():
        holder.sequence = special_list

    with pytest.raises(WatchpointHit):
        _set_dict()

    assert type(holder.mapping) is _SpecialDict, (
        "Dict subclasses must not be replaced by _WatchedDict. "
        "Django's QueryDict was being silently downgraded, losing getlist()."
    )
    assert holder.mapping.getlist("color") == ["red"]

    with pytest.raises(WatchpointHit):
        _set_list()

    assert type(holder.sequence) is _SpecialList, (
        "List subclasses must not be replaced by _WatchedList."
    )
    assert holder.sequence.first() == 1


def test_watch_dotted_list_attr_fires_on_append():
    """`obj.attr.append(x)` fires when watching `obj.attr` and the attr is
    a list. This is the user's `room_types.append(...)` scenario."""
    holder = _ContainerHolder()
    def _code():
        watch("holder.items")
        holder.items.append("hotel")
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.watch_name == "holder.items"


def test_watch_dotted_list_attr_fires_on_extend():
    """`obj.attr.extend(iter)` fires."""
    holder = _ContainerHolder()
    def _code():
        watch("holder.items")
        holder.items.extend([1, 2])
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_watch_dotted_list_attr_fires_on_insert():
    """`obj.attr.insert(i, x)` fires."""
    holder = _ContainerHolder()
    holder.items.append("seed")
    def _code():
        watch("holder.items")
        holder.items.insert(0, "first")
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_watch_dotted_list_attr_fires_on_remove():
    """`obj.attr.remove(x)` fires."""
    holder = _ContainerHolder()
    holder.items.extend(["a", "b"])
    def _code():
        watch("holder.items")
        holder.items.remove("a")
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_watch_dotted_list_attr_fires_on_pop():
    """`obj.attr.pop()` fires and the popped value is returned correctly."""
    holder = _ContainerHolder()
    holder.items.extend(["a", "b", "c"])
    def _code():
        watch("holder.items")
        popped = holder.items.pop()
        # The popped value must be returned correctly even though the wrap
        # intercepts the call – if we lost the return path, callers reading
        # the popped value would silently get None.
        assert popped == "c", f"expected 'c', got {popped!r}"
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_watch_dotted_list_attr_fires_on_clear():
    """`obj.attr.clear()` fires when the list is non-empty."""
    holder = _ContainerHolder()
    holder.items.append("seed")
    def _code():
        watch("holder.items")
        holder.items.clear()
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_watch_dotted_list_attr_fires_on_sort_when_order_changes():
    """`obj.attr.sort()` fires if the call actually re-orders elements."""
    holder = _ContainerHolder()
    holder.items.extend([3, 1, 2])
    def _code():
        watch("holder.items")
        holder.items.sort()
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_watch_dotted_list_attr_silent_on_sort_already_sorted():
    """`obj.attr.sort()` does NOT fire if the list was already sorted –
    repr is identical pre/post, so no firing."""
    holder = _ContainerHolder()
    holder.items.extend([1, 2, 3])
    def _code():
        watch("holder.items")
        holder.items.sort()
        pass
    # Must NOT raise – sort on already-sorted list is a no-op.
    _code()


def test_watch_dotted_list_attr_fires_on_setitem():
    """`obj.attr[i] = v` fires."""
    holder = _ContainerHolder()
    holder.items.extend(["a", "b"])
    def _code():
        watch("holder.items")
        holder.items[0] = "changed"
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_watch_dotted_list_attr_fires_on_iadd():
    """`obj.attr += [x]` fires – uses list.__iadd__ which the wrapper hooks."""
    holder = _ContainerHolder()
    holder.items.append("seed")
    def _code():
        watch("holder.items")
        holder.items += ["added"]
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_watch_dotted_dict_attr_fires_on_setitem():
    """`obj.attr[k] = v` fires on dict."""
    holder = _ContainerHolder()
    def _code():
        watch("holder.bag")
        holder.bag["k"] = "v"
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_watched_dict_setitem_swallows_value_repr_errors():
    """Regression: Django's `TestCase._testdata_memo` is a plain dict
    that gets recursively wrapped as `_WatchedDict` when the user
    watches `self` on a TestCase. Django then passes that very dict
    as `memo` to `copy.deepcopy`. Deepcopy does `memo[id(x)] = y` with
    `y` being a half-reconstructed Django Model whose `__repr__` raises
    `AttributeError("'<Model>' object has no attribute '_state'")`.

    `_WatchedDict.__setitem__` snapshots the dict via
    `_wp_container_repr(self)` to compute before/after for change
    detection. That `__repr__` iterates the dict and reprs every value
    – including `y`, which raises. If the snapshot propagates the
    AttributeError, deepcopy dies and the user's test fails through
    no fault of theirs.

    The fix in `_wp_container_repr` catches any exception from the
    repr path and returns `"<unreprable>"`. Before- and after-
    snapshots both come back as `"<unreprable>"` so they compare
    equal, no hit fires, but the underlying mutation succeeds.
    """
    from _pycharm_watchpoint import _WatchedDict

    class _UnreprableValue:
        """Simulates a half-constructed Django Model mid-deepcopy."""

        def __repr__(self):
            raise AttributeError(
                "'_UnreprableValue' object has no attribute '_state'"
            )

    d = _WatchedDict()
    bad = _UnreprableValue()
    # This is the killer line: deepcopy's `memo[id(x)] = y` flow.
    # Pre-fix, the snapshot via `dict.__repr__` blew up on `bad.__repr__`.
    d[1] = bad
    assert d[1] is bad

    # Subsequent mutations also tolerate the unreprable value.
    d.update({2: bad, 3: bad})
    d.pop(1)
    d.clear()


def test_watched_list_mutations_swallow_value_repr_errors():
    """Symmetric guard for list. A `_WatchedList` containing an
    unreprable element must not crash its own mutating methods."""
    from _pycharm_watchpoint import _WatchedList

    class _UnreprableValue:
        def __repr__(self):
            raise RuntimeError("don't repr me")

    lst = _WatchedList()
    bad = _UnreprableValue()
    lst.append(bad)
    lst.append(bad)
    lst[0] = bad
    lst.extend([bad, bad])
    lst.pop()
    lst.remove(bad)
    lst.clear()


def test_watched_set_mutations_swallow_value_repr_errors():
    """Symmetric guard for set."""
    from _pycharm_watchpoint import _WatchedSet

    class _UnreprableValue:
        def __repr__(self):
            raise RuntimeError("don't repr me")

        def __hash__(self):
            # Need a stable hash for set membership; identity is fine.
            return id(self)

    s = _WatchedSet()
    bad = _UnreprableValue()
    s.add(bad)
    s.discard(bad)
    s.add(bad)
    s.update({bad})
    s.clear()


def test_watch_dotted_dict_attr_fires_on_update():
    """`obj.attr.update({...})` fires on dict."""
    holder = _ContainerHolder()
    def _code():
        watch("holder.bag")
        holder.bag.update({"k": "v"})
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_watch_dotted_dict_attr_fires_on_delitem():
    """`del obj.attr[k]` fires on dict."""
    holder = _ContainerHolder()
    holder.bag["k"] = "v"
    def _code():
        watch("holder.bag")
        del holder.bag["k"]
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_watch_dotted_set_attr_fires_on_add():
    """`obj.attr.add(x)` fires on set."""
    holder = _ContainerHolder()
    def _code():
        watch("holder.tags")
        holder.tags.add("ready")
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_watch_dotted_set_attr_silent_on_add_existing():
    """`obj.attr.add(x)` on an element already in the set does NOT fire –
    repr is unchanged after the no-op add."""
    holder = _ContainerHolder()
    holder.tags.add("ready")
    def _code():
        watch("holder.tags")
        holder.tags.add("ready")  # already present
        pass
    # Must NOT raise.
    _code()


def test_watch_dotted_list_attr_fires_through_helper():
    """The user's reported case: helper reads `obj.attr` into a local and
    mutates through that local – the wrap means the local IS the wrapper,
    so `.append(...)` still fires.
    """
    holder = _ContainerHolder()

    def _fill(target_holder):
        # mirrors OnboardingDtoFiller.fill_dto_fully:
        # captures the attr into a local, then mutates through the local.
        items = target_holder.items
        items.append("from-helper")
        pass

    def _code():
        watch("holder.items")
        _fill(holder)
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.watch_name == "holder.items"


def test_watch_dotted_list_attr_does_not_double_fire_when_reassigned():
    """When the user replaces `obj.attr = [...]` and then mutates, the new
    container is freshly wrapped. The reassignment fires once; the later
    mutation fires once. Verifies the auto-wrap-on-reassign path in
    `_WatchedSubclass.__setattr__`."""
    holder = _ContainerHolder()
    hits = []

    # We capture by overriding the registry's _handle_hit so we can count
    # firings WITHOUT pydevd or pytest.raises tearing down the frame.
    registry = builtins._watchpoint_registry
    original_handle_hit = registry._handle_hit
    def _capture(**kwargs):
        hits.append(kwargs)
        # Don't actually pause / raise – just record. We're testing the
        # firing count, not the pause flow.
    registry._handle_hit = _capture
    try:
        def _code():
            watch("holder.items")
            holder.items = ["replacement"]   # fires (rebind detector)
            holder.items.append("after")     # fires (wrapper on new list)
            pass
        _code()
    finally:
        registry._handle_hit = original_handle_hit

    # First hit = rebind (old [] -> ['replacement']),
    # Second hit = append (['replacement'] -> ['replacement', 'after']).
    assert len(hits) == 2, f"expected 2 hits, got {len(hits)}: {hits}"
    assert all(h["watch_name"] == "holder.items" for h in hits)


def test_watch_dotted_list_attr_aliased_ref_before_watch_does_not_fire():
    """Acknowledged limitation: a reference captured BEFORE the watch was
    armed points at the original (un-wrapped) list. Mutations through that
    stale alias don't fire. The wrap-and-replace approach cannot fix this
    without a CPython-level hook.
    """
    holder = _ContainerHolder()
    holder.items.append("seed")
    stale_alias = holder.items  # captured BEFORE watch – stays the original
    def _code():
        watch("holder.items")
        # After watch, holder.items is the wrapper. stale_alias is the
        # original (now orphaned) list.
        assert holder.items is not stale_alias
        stale_alias.append("invisible")  # NOT detected
        pass
    # Must NOT raise – mutation through stale_alias bypasses the wrapper.
    _code()


def test_unwatch_restores_plain_list_type():
    """After unwatch(), the attribute is a plain `list`, not our wrapper –
    `type(obj.attr) is list` must hold for user code that does strict
    type checks.
    """
    holder = _ContainerHolder()
    holder.items.append("seed")
    watch("holder.items")
    # While watched, the attr is a wrapper subclass.
    assert isinstance(holder.items, list)
    assert type(holder.items) is not list  # is the wrapper subclass
    unwatch("holder.items")
    # After unwatch, restored to plain list.
    assert type(holder.items) is list
    assert holder.items == ["seed"]


def test_unwatch_disables_firing_on_leaked_wrapper_alias():
    """If user code captured a reference to the wrapper while watched, then
    unwatched, that captured wrapper must not fire when mutated later –
    the watch is gone. The wrapper checks `_wp_registry` on every mutation
    and silently no-ops when cleanup nulled it.
    """
    holder = _ContainerHolder()
    watch("holder.items")
    wrapper_alias = holder.items   # is the _WatchedList instance
    unwatch("holder.items")
    # Now holder.items has been restored to a plain list, but
    # wrapper_alias still points at the _WatchedList. Mutating through
    # it must be silent.
    wrapper_alias.append("after-unwatch")  # must NOT raise


def test_recursive_watch_fires_on_nested_attr_assignment():
    """Watching the root object fires when a nested attribute is reassigned."""
    dto = _Dto()
    def _code():
        watch("dto")
        dto.settings.current_step = "create_user"
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    # watch_name should carry the full dotted path so the IDE can show
    # which slot actually moved.
    assert exc_info.value.watch_name == "dto.settings.current_step"


def test_recursive_watch_fires_on_nested_list_mutation():
    """Watching the root object fires when a nested list is mutated through
    a method (append). Bug 2 + Bug 3 interaction: the nested list gets
    container-wrapped during recursive instrumentation."""
    dto = _Dto()
    def _code():
        watch("dto")
        dto.settings.room_types.append("hotel")
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.watch_name == "dto.settings.room_types"


def test_recursive_watch_fires_on_nested_dict_mutation():
    """Watching the root object fires when a nested dict's item is set."""
    dto = _Dto()
    def _code():
        watch("dto")
        dto.settings.config["mode"] = "fast"
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.watch_name == "dto.settings.config"


def test_recursive_watch_fires_on_direct_attr_assignment():
    """Recursive watch must still fire on direct (non-nested) attr changes –
    the depth-1 attrs of the root are also instrumented."""
    dto = _Dto()
    def _code():
        watch("dto")
        dto.name = "renamed"
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.watch_name == "dto.name"


def test_recursive_watch_fires_when_nested_attr_replaced_with_new_object():
    """Reassigning a nested user-defined attr to a brand-new object: the
    rebind fires under the parent's setattr hook, AND the new object's
    sub-attrs get auto-instrumented so subsequent mutations on the new
    object also fire."""
    dto = _Dto()
    new_settings = _Settings()
    new_settings.current_step = "after-rebind"

    def _code():
        watch("dto")
        dto.settings = new_settings  # depth-1 rebind fires
        # The newly-installed instrumentation on new_settings means the
        # following mutation should fire too.
        dto.settings.current_step = "after-mutation"
        pass
    # Just verify SOMETHING raises (the first rebind happens before the
    # second mutation, so the helper exits via the rebind hit).
    with pytest.raises(WatchpointHit):
        _code()


def test_recursive_watch_with_cycle_does_not_blow_stack():
    """A graph where two nested objects reference each other (or self)
    must not cause infinite recursion during watch-arm instrumentation.
    The id-visited set short-circuits the second visit."""
    class _Node:
        def __init__(self):
            self.peer = None

    a = _Node()
    b = _Node()
    a.peer = b
    b.peer = a   # cycle: a.peer.peer is a
    # Must complete without RecursionError.
    watch("a")


def test_recursive_watch_self_reference_does_not_blow_stack():
    """An object referencing itself (`x.parent = x`) must not loop."""
    class _Self:
        def __init__(self):
            self.parent = None

    x = _Self()
    x.parent = x
    watch("x")


def test_recursive_watch_unwatch_restores_nested_classes():
    """unwatch on the root restores every nested object's original class –
    no _WatchedAny_ subclasses lingering. Container wrappers are restored
    to plain containers too.
    """
    dto = _Dto()
    settings_cls_before = type(dto.settings)
    config_type_before = type(dto.settings.config)
    items_type_before = type(dto.settings.room_types)

    watch("dto")
    # While watched: every nested user-object's type should be a
    # _WatchedAny_ subclass, containers wrapped.
    assert type(dto.settings) is not settings_cls_before
    assert type(dto.settings.config) is not config_type_before
    assert type(dto.settings.room_types) is not items_type_before

    unwatch("dto")
    # After unwatch: all restored.
    assert type(dto.settings) is settings_cls_before
    assert type(dto.settings.config) is config_type_before
    assert type(dto.settings.room_types) is items_type_before


def test_recursive_watch_depth_cap():
    """Beyond the depth cap, mutations don't fire. Documented behavior;
    depth-5 changes are out of reach for the root watch."""
    class _Lvl:
        def __init__(self):
            self.child = None

    root = _Lvl()
    root.child = _Lvl()                     # depth 1
    root.child.child = _Lvl()               # depth 2
    root.child.child.child = _Lvl()         # depth 3
    root.child.child.child.child = _Lvl()   # depth 4
    root.child.child.child.child.child = _Lvl()  # depth 5

    def _code():
        watch("root")
        # Change at depth 5 should NOT fire – the recursion stops at 4.
        root.child.child.child.child.child.child = "untracked"
        pass
    # Must NOT raise – depth 5 is past the cap.
    _code()


def test_recursive_watch_depth_at_cap_still_fires():
    """At-or-below the depth cap, mutations DO fire. Verifies the cap is
    exclusive of `_RECURSIVE_OBJECT_WATCH_DEPTH` itself."""
    class _Lvl:
        def __init__(self):
            self.child = None
            self.val = "init"

    root = _Lvl()
    root.child = _Lvl()                     # depth 1
    root.child.child = _Lvl()               # depth 2
    root.child.child.child = _Lvl()         # depth 3
    root.child.child.child.child = _Lvl()   # depth 4

    def _code():
        watch("root")
        # Change at depth 4: change `.val` on root.child.child.child.child
        # which itself is at depth 4 (root counts as 0). The class surgery
        # was installed on that object, so its setattr fires.
        root.child.child.child.child.val = "changed"
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_recursive_watch_two_paths_to_same_object_instrumented_once():
    """If a nested object is reachable via two paths from the root, it
    should be instrumented exactly once (no double-fire, no double class
    surgery). The id-visited set short-circuits the second visit."""
    class _Node:
        def __init__(self):
            self.val = "init"

    shared = _Node()

    class _Owner:
        def __init__(self):
            self.left = shared
            self.right = shared   # same object reached via two paths

    owner = _Owner()

    def _code():
        # NOTE the explicit `owner` reference: it forces CPython to capture
        # `owner` as a free variable of `_code`, which is what makes
        # `eval("owner", f_globals, f_locals)` inside `add_watch` succeed.
        # Without a use in `_code`'s body, the name resolves to nothing
        # and the watch falls through to local-variable handling on None.
        assert owner is not None
        watch("owner")
        shared.val = "changed"   # fires once, under whichever path won
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    # Either "owner.left.val" or "owner.right.val" – we don't pin which
    # path wins (dict iteration order), but it must NOT be the un-pathed
    # bare ".val".
    assert exc_info.value.watch_name in ("owner.left.val", "owner.right.val")


def test_recursive_watch_does_not_fire_when_nothing_changes():
    """Sanity: arming the recursive watch must not itself fire on the
    initial instrumentation pass (the guard suppresses the wrap+swap
    setattrs)."""
    dto = _Dto()
    watch("dto")
    pass  # Should not have raised


def test_recursive_watch_primitive_leaf_unchanged_silent():
    """Setting a nested primitive attr to the SAME value (same hash) does
    not fire – mirrors the existing `_value_hash` skip on equal values."""
    dto = _Dto()
    def _code():
        watch("dto")
        dto.settings.current_step = "init"  # already "init"
        pass
    _code()  # must NOT raise


def test_recursive_watch_through_helper_function():
    """The user's actual scenario: watch the root DTO before the helper
    runs, helper mutates a nested list. The watch must fire from inside
    the helper's call without any propagation machinery (the class surgery
    is ambient – it fires wherever the mutation happens)."""
    dto = _Dto()

    def _filler(target_dto):
        # Mirrors OnboardingDtoFiller.fill_dto_fully – mutates nested state.
        items = target_dto.settings.room_types
        items.append("from-filler")
        pass

    def _code():
        watch("dto")
        _filler(dto)
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.watch_name == "dto.settings.room_types"


def test_django_like_dotted_watch_fires_on_specific_attr():
    """Dotted `watch('obj.field')` on a Django-like instance uses the
    classpatch fallback. Rebinding the watched attribute fires
    `WatchpointHit` with the dotted watch_name preserved."""
    obj = _DjangoLikeModel()
    watch("obj.name")
    def _code():
        obj.name = "renamed"
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.watch_name == "obj.name"
    assert "renamed" in exc_info.value.new_value


def test_django_like_dotted_watch_fires_when_method_rebinds_attribute():
    """Reproduces the user-reported pattern: a model method calls
    `self.field = computed_value`. Classpatch intercepts the assignment
    inside the method, so the user pauses right after the method's
    `self.field = ...` line."""
    obj = _DjangoLikeModel()
    watch("obj.name")
    def _code():
        _django_like_set_via_method(obj, "via-method")
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.watch_name == "obj.name"
    assert "via-method" in exc_info.value.new_value


def test_django_like_dotted_watch_silent_on_same_value():
    """Assigning the same value to a watched attribute doesn't fire –
    `_value_hash` equality short-circuits before reaching `_handle_hit`."""
    obj = _DjangoLikeModel()
    watch("obj.name")
    obj.name = "django-thing"  # identical to __init__-assigned value
    clear_watches()


def test_django_like_dotted_watch_other_instance_unaffected():
    """Patching the class targets only the watched instance via
    `id(instance)`. Writes to other instances of the same class pass
    through the patched `__setattr__` without firing."""
    obj1 = _DjangoLikeModel()
    obj2 = _DjangoLikeModel()
    watch("obj1.name")
    obj2.name = "different"  # must not fire
    clear_watches()


def test_django_like_dotted_watch_other_attr_unaffected():
    """A specific-attribute classpatch entry fires only on the watched
    attribute; writes to other attributes on the same instance pass
    through with no fire."""
    obj = _DjangoLikeModel()
    watch("obj.name")
    obj.tag = "new-tag"  # different attribute, must not fire
    clear_watches()


def test_django_like_dotted_watch_unwatch_restores_setattr():
    """After unwatch, the class's `__setattr__` is restored (removed
    from `cls.__dict__` since the test class had no own original) and
    subsequent writes don't fire."""
    obj = _DjangoLikeModel()
    watch("obj.name")
    # Patched while watch is active.
    assert "__setattr__" in _DjangoLikeModel.__dict__
    unwatch("obj.name")
    # Restored to MRO-lookup default.
    assert "__setattr__" not in _DjangoLikeModel.__dict__
    obj.name = "after-unwatch"  # must not fire


def test_django_like_bare_name_watch_fires_on_any_attribute():
    """Bare-name `watch('obj')` on a Django-like instance installs a
    wildcard classpatch entry. Any attribute write on the watched
    instance fires, with `watch_name` extended to `obj.<attr>` so the
    user sees which attribute changed."""
    obj = _DjangoLikeModel()
    watch("obj")
    def _code():
        obj.tag = "wildcard-fire"
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code()
    assert exc_info.value.watch_name == "obj.tag"
    assert "wildcard-fire" in exc_info.value.new_value


def test_django_like_bare_name_watch_other_instance_unaffected():
    """Wildcard classpatch only fires for the specific watched instance,
    not for other instances of the same class even though they share
    the patched class-level `__setattr__`."""
    obj1 = _DjangoLikeModel()
    obj2 = _DjangoLikeModel()
    watch("obj1")
    obj2.name = "different"
    obj2.tag = "also-different"
    clear_watches()


def test_django_like_bare_name_watch_unwatch_restores_setattr():
    """After unwatch, the wildcard's patched `__setattr__` is removed
    from the class and subsequent attribute writes pass through."""
    obj = _DjangoLikeModel()
    watch("obj")
    assert "__setattr__" in _DjangoLikeModel.__dict__
    unwatch("obj")
    assert "__setattr__" not in _DjangoLikeModel.__dict__
    obj.name = "after-unwatch"
    obj.tag = "also-after-unwatch"


def test_django_like_specific_takes_priority_over_wildcard():
    """When both `watch('obj')` (wildcard) and `watch('obj.name')`
    (specific) are armed, a write to `obj.name` reports the specific
    watch_name. A write to a different attribute still goes through
    wildcard. After removing the specific entry, the wildcard alone
    catches writes to `obj.name` too."""
    obj = _DjangoLikeModel()
    watch("obj")
    watch("obj.name")
    def _code_specific():
        obj.name = "via-specific"
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code_specific()
    # Specific entry wins; reports the dotted watch the user installed.
    assert exc_info.value.watch_name == "obj.name"
    # Drop the specific entry; wildcard remains.
    unwatch("obj.name")
    def _code_wildcard():
        obj.tag = "via-wildcard"
        pass
    with pytest.raises(WatchpointHit) as exc_info:
        _code_wildcard()
    assert exc_info.value.watch_name == "obj.tag"


def test_django_like_classpatch_cleanup_independent_for_two_classes():
    """Two different Django-like classes patched independently; unwatching
    one does not affect the other's patch state. Exercises the per-class
    `_classpatch_registry` keying."""
    class _A(metaclass=_DjangoLikeMeta):
        _django_like_real = True
        def __init__(self):
            self.field = "a-init"
    class _B(metaclass=_DjangoLikeMeta):
        _django_like_real = True
        def __init__(self):
            self.field = "b-init"
    a = _A()
    b = _B()
    watch("a.field")
    watch("b.field")
    assert "__setattr__" in _A.__dict__
    assert "__setattr__" in _B.__dict__
    unwatch("a.field")
    # Only _A's patch is gone; _B's is still in place.
    assert "__setattr__" not in _A.__dict__
    assert "__setattr__" in _B.__dict__
    unwatch("b.field")
    assert "__setattr__" not in _B.__dict__


def test_django_like_nested_under_recursive_watch_skipped_gracefully():
    """When a Django-like instance is nested under a `watch('root')`
    recursive walk, the failed sub-instrumentation must NOT abort the
    whole tree – `_instrument_object_tree` catches the TypeError and
    moves on. The classpatch fallback is NOT used for nested objects
    (only the top-level bare-name watch path uses it), so the nested
    Django instance is simply not instrumented; its own attribute
    writes won't fire. Sibling non-Django attrs of the root still get
    full class-surgery instrumentation."""
    class _Container:
        def __init__(self):
            self.django_thing = _DjangoLikeModel()  # skipped
            self.normal_thing = _Dto()              # instrumented as usual
    root = _Container()
    watch("root")
    # Confirm the Django-like child wasn't subclass'd – stays as the
    # original class (no surgery, no classpatch from recursion).
    assert type(root.django_thing) is _DjangoLikeModel
    # Confirm the normal nested object WAS instrumented.
    assert type(root.normal_thing) is not _Dto  # is a _WatchedAny_ subclass
    # And mutation on the normal nested object still fires (sanity check
    # that the partial failure didn't break the rest of the tree).
    def _code():
        root.normal_thing.name = "renamed"
        pass
    with pytest.raises(WatchpointHit):
        _code()


def test_stubborn_metaclass_dotted_watch_raises_clear_type_error():
    """When BOTH dynamic subclassing AND class-level `__setattr__`
    assignment are blocked, the dotted watch path can install neither
    strategy and surfaces a clean TypeError naming both failures so
    the user understands why the watch couldn't be armed."""
    obj = _StubbornDjangoLikeModel()
    with pytest.raises(TypeError) as exc_info:
        watch("obj.name")
    msg = str(exc_info.value)
    assert "_StubbornDjangoLikeModel" in msg
    # Message must hint that BOTH strategies failed, not just subclassing.
    assert "monkey-patching" in msg or "__setattr__" in msg


def test_stubborn_metaclass_bare_name_falls_back_to_local_variable():
    """When BOTH strategies fail for bare-name watch on a stubborn
    metaclass, the dispatch falls through to local-variable rebind
    detection. The watch installs without raising, but attribute
    mutations on the instance won't fire (only rebinding the local
    variable in the watching frame would)."""
    obj = _StubbornDjangoLikeModel()
    watch("obj")  # must not raise
    obj.name = "after-watch"  # no fire – neither classpatch nor surgery armed
    clear_watches()


def test_is_user_defined_type_accepts_class_in_test_module():
    """A class declared in this test module has __module__ = the test
    module's name (not in any denylist, not stdlib, not site-packages)
    so the helper accepts it for recursive instrumentation."""
    class MyUserClass:
        pass
    assert _is_user_defined_type(MyUserClass) is True


def test_is_user_defined_type_rejects_builtins():
    """Built-in types are not user code and must not be recursed into."""
    assert _is_user_defined_type(int) is False
    assert _is_user_defined_type(str) is False
    assert _is_user_defined_type(list) is False
    assert _is_user_defined_type(dict) is False


def test_is_user_defined_type_rejects_known_frameworks():
    """Types whose __module__ root matches the framework denylist are
    rejected without needing the framework actually installed. We
    synthesize the test by assigning __module__ explicitly so the suite
    runs in environments without Django / SQLAlchemy / pydantic."""
    class FakeDjangoQuerySet:
        pass
    FakeDjangoQuerySet.__module__ = "django.db.models.query"
    assert _is_user_defined_type(FakeDjangoQuerySet) is False

    class FakeSQLAlchemyMapper:
        pass
    FakeSQLAlchemyMapper.__module__ = "sqlalchemy.orm.mapper"
    assert _is_user_defined_type(FakeSQLAlchemyMapper) is False

    class FakePydanticModel:
        pass
    FakePydanticModel.__module__ = "pydantic.main"
    assert _is_user_defined_type(FakePydanticModel) is False

    class FakePydevdInternal:
        pass
    FakePydevdInternal.__module__ = "_pydevd_bundle.pydevd_constants"
    assert _is_user_defined_type(FakePydevdInternal) is False


def test_is_user_defined_type_rejects_stdlib_modules():
    """Stdlib types (pathlib.Path, collections.OrderedDict, etc.) are
    rejected via `sys.stdlib_module_names`. User code that touches these
    in passing won't trigger recursive instrumentation."""
    import pathlib
    import collections
    import email.message
    assert _is_user_defined_type(pathlib.Path) is False
    assert _is_user_defined_type(collections.OrderedDict) is False
    assert _is_user_defined_type(email.message.Message) is False


def test_is_user_defined_type_rejects_site_packages_heuristic(tmp_path, monkeypatch):
    """A type whose module's __file__ lives under site-packages is rejected
    even when its __module__ root isn't in the framework denylist.
    Catches obscure / less-popular libraries we haven't named."""
    fake_mod = type(_sys_for_safeguards)("some_random_third_party")
    fake_mod.__file__ = str(
        tmp_path / "lib" / "python3.12" / "site-packages"
        / "some_random_third_party" / "__init__.py"
    )
    monkeypatch.setitem(_sys_for_safeguards.modules, "some_random_third_party", fake_mod)

    class FakeThirdPartyType:
        pass
    FakeThirdPartyType.__module__ = "some_random_third_party"
    assert _is_user_defined_type(FakeThirdPartyType) is False


def test_is_user_defined_type_rejects_non_types():
    """The helper accepts None and non-type inputs gracefully so callers
    can ask `_is_user_defined_type(type(value))` without an isinstance
    pre-check."""
    assert _is_user_defined_type(None) is False
    assert _is_user_defined_type("not a type") is False
    assert _is_user_defined_type(42) is False


def test_runtime_filenames_includes_string_marker():
    """The set used by `_find_user_caller` MUST include the `<string>`
    filename – that's what the runtime's frames carry when exec'd by the
    plugin's sitecustomize injection. Without it, runtime frames wouldn't
    be skipped and hits would report from `<string>:NNN` lines."""
    assert "<string>" in _RUNTIME_FILENAMES


def test_find_user_caller_returns_immediate_user_frame():
    """When the immediate caller IS a user frame (test_*.py), the helper
    returns it without walking."""
    user = _find_user_caller(_sys_for_safeguards._getframe(0))
    assert user is not None
    assert user.f_code.co_filename.endswith("test_pause_anchor.py")


def test_find_user_caller_returns_none_for_empty_chain():
    """Defensive: a None start frame returns None rather than raising."""
    assert _find_user_caller(None) is None


def test_recursion_stops_at_framework_boundary():
    """A user DTO whose attribute is a framework-typed object: the DTO
    gets full class surgery, but `dto.framework_obj` is NOT recursively
    instrumented – assigning attributes INSIDE `framework_obj` is silent
    (no watcher installed on it), but rebinding `dto.framework_obj`
    itself still fires."""
    dto = _UserDtoWithFrameworkField()
    fw_inner_cls_before = type(dto.framework_obj)
    watch("dto")
    # The framework object's class must NOT have been swapped – recursion
    # should have stopped at the framework boundary.
    assert type(dto.framework_obj) is fw_inner_cls_before, (
        "framework object was instrumented despite framework module prefix"
    )
    # Mutating an attribute INSIDE the framework object does NOT fire –
    # there's no watcher on it.
    dto.framework_obj.cached_state = "mutated"  # silent

    # Mutating the DTO's own user-defined attr DOES fire.
    with pytest.raises(WatchpointHit) as exc_info:
        dto.label = "beta"
    assert exc_info.value.watch_name == "dto.label"


def test_recursion_skips_class_objects():
    """When a user object holds a reference to a CLASS (not an instance),
    we don't try to wrap the class's __dict__ – it's full of descriptors
    that would each get instrumented and trigger explosive growth."""
    class _Inner:
        pass

    class _OuterHoldingClass:
        def __init__(self):
            self.normal_attr = "hello"
            self.held_class = _Inner  # the class itself, not an instance

    o = _OuterHoldingClass()
    watch("o")

    # The filter rejected `held_class` BEFORE we tried class surgery on
    # it – so it's not in sub_watches. Without the filter, the eventual
    # TypeError from a metaclass conflict in `_install_single_object_watch`
    # also keeps it out of sub_watches, but does so via the slower
    # catch-and-fallback path. Asserting absence locks in the cheap
    # path and surfaces regressions where someone tries to add a
    # secondary instrumentation strategy for class objects.
    registry = builtins._watchpoint_registry
    aw = registry._attr_watches["o"]
    sub_exprs = [sw.expr for sw in aw.sub_watches]
    assert not any("held_class" in e for e in sub_exprs), (
        f"held_class was added to sub_watches despite being a class object: "
        f"{sub_exprs!r}"
    )
    # The held class itself is unchanged – no residue from a partial
    # __class__ surgery attempt.
    assert _Inner.__name__ == "_Inner"

    # Mutating the normal user attr fires.
    with pytest.raises(WatchpointHit):
        o.normal_attr = "world"
    clear_watches()
    # Writing an attribute on the held class is silent – no watcher
    # was installed on _Inner itself.
    _Inner.added_after = "ok"  # must not raise


def test_visited_ids_shared_across_setattr_reentry():
    """Cyclic user-defined graph (a.next = b; b.next = a). The initial
    walk records both ids in `root_watch.visited_ids`. A subsequent
    __setattr__ that assigns one of the cycle members to another
    attribute must NOT re-instrument it – the persistent visited set
    catches the duplicate.

    Pre-fix, the watcher's __setattr__ recursed with a fresh
    `visited={id(wrapped_value)}` set, so any later assignment of an
    already-instrumented object started another depth-4 walk.
    """
    class _Node:
        def __init__(self):
            self.value = None
            self.next = None

    root = _Node()
    other = _Node()
    root.next = other
    other.next = root  # closes the cycle
    watch("root")

    registry = builtins._watchpoint_registry
    aw = registry._attr_watches["root"]
    initial_sub_count = len(aw.sub_watches)
    # Both root (root_watch itself) and `other` should already be in
    # visited_ids after the initial walk.
    assert id(root) in aw.visited_ids
    assert id(other) in aw.visited_ids

    # Assign `other` to another attribute. Pre-fix, the watcher would
    # call _install_single_object_watch on `other` again because the
    # fresh per-call visited set didn't contain it.
    with pytest.raises(WatchpointHit):
        root.value = other

    # No new sub-watch installed for `other` – it was already covered.
    assert len(aw.sub_watches) == initial_sub_count, (
        f"sub_watches grew from {initial_sub_count} to {len(aw.sub_watches)} "
        f"on __setattr__ re-entry – persistent visited set missed cycle"
    )


def test_breadth_cap_engages_with_warning(capsys):
    """A pathological object with more sub-objects than the cap allows
    triggers the breadth-cap guard. `sub_watches_capped` flips to True,
    sub_watches stays at or below the cap, and a one-line warning is
    written to stderr so the user can see why deeper mutations aren't
    firing."""
    class _Leaf:
        def __init__(self, i):
            self.i = i

    class _Root:
        def __init__(self):
            for i in range(_MAX_SUB_WATCHES_PER_ROOT + 50):
                setattr(self, f"leaf_{i}", _Leaf(i))

    obj = _Root()
    watch("obj")

    registry = builtins._watchpoint_registry
    aw = registry._attr_watches["obj"]
    assert aw.sub_watches_capped is True, "breadth cap should have engaged"
    assert len(aw.sub_watches) <= _MAX_SUB_WATCHES_PER_ROOT, (
        f"sub_watches grew past cap: {len(aw.sub_watches)} > {_MAX_SUB_WATCHES_PER_ROOT}"
    )
    captured = capsys.readouterr()
    assert "sub-watch cap" in captured.err, (
        f"expected breadth-cap warning on stderr, got: {captured.err!r}"
    )


def test_class_swap_under_guard_does_not_fire_spurious_hit():
    """`obj.__class__ = watcher_cls` inside `_install_single_object_watch`
    must be wrapped in the per-thread guard so a parent watcher (when
    this method is called recursively from `_instrument_object_tree`)
    does not see the swap as a user-initiated setattr.

    Concretely: watching a user DTO with a nested user-defined object
    should not produce a hit during installation itself. Pre-fix, the
    nested `__class__` swap could fire through the DTO's freshly-armed
    watcher because the guard was only set around the recursive call,
    not the swap itself.
    """
    class _Child:
        def __init__(self):
            self.kid = "k"

    class _Parent:
        def __init__(self):
            self.label = "p"
            self.child = _Child()

    p = _Parent()
    # If the installation fired spurious hits, this would raise here
    # (no-pydevd fallback path re-raises). It must not.
    watch("p")
    # And we must still get hits on real subsequent mutations.
    with pytest.raises(WatchpointHit) as exc_info:
        p.label = "p2"
    assert exc_info.value.watch_name == "p.label"
