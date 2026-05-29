# Python watchpoint runtime – handoff notes

This directory contains the Python side of the **pythonwatchpoint** PyCharm plugin.
It implements *watchpoints* for Python 3.12+ using `sys.monitoring` (PEP 669):
when a watched variable or attribute changes, the debugger pauses at the change
site. The Kotlin plugin is upstream of this directory and is not covered here.

## TL;DR

- `watchpoint.py` – runtime: registry, sys.monitoring callbacks, pydevd integration.
- `test_watchpoint.py` – 121 tests, pure-pytest (no pydevd).
- `conftest.py` – per-test cleanup of registry + frame state.
- Targets **Python 3.12, 3.13, 3.14** – all three are run in CI-style local checks.
- Performance design: **zero global sys.monitoring overhead** until the first `watch()` call.

```bash
# Run the suite on every supported interpreter
python3.12 -m pytest test_watchpoint.py
python3.13 -m pytest test_watchpoint.py
python3.14 -m pytest test_watchpoint.py
```

## Public API

```python
watch(expr, *, frame=None)   # arm a watchpoint
unwatch(expr)                # remove
clear_watches()              # remove all

# Plugin-side entry points (exposed on builtins so the IDE evaluator can call them):
builtins._pycharm_watch          # alias of watch
builtins._pycharm_watch_at       # name, file_hint, func_hint – locates the paused user frame
builtins._pycharm_unwatch
builtins._pycharm_clear_watches
builtins._pycharm_watchpoint_diag  # returns a stderr-style diagnostic about pydevd lookup
builtins._watchpoint_registry    # the singleton, for conftest cleanup
```

`watch("name")` auto-picks one of three flavors based on what `name` resolves to:

| Resolved value             | Watch installed                                                                                  |
| -------------------------- | ------------------------------------------------------------------------------------------------ |
| primitive / list / dict /… | **local-variable** (LINE-event diff per frame)                                                   |
| user-defined object        | **object-wide attribute** + **recursive instrumentation** to depth 4 (see §9)                    |
| `"a.b.c"` (dotted form)    | **specific attribute** at the leaf; if the leaf is a list/dict/set, also **container-wrap** (§9) |

The user-defined-object detection is `type(value).__module__ != "builtins"`
plus a fast-path filter for primitives. See `_is_object_watchable`.

## Design contract – read before changing anything below

### 1. Per-frame keying

`_local_watches` is a `dict[(name, frame_id), _LocalWatch]`. The frame_id is
`id(frame)` at `watch()` time. This is what makes recursive watches, concurrent
threads, and `asyncio.gather`'d coroutines work correctly: each frame instance
gets its own watch row keyed by its own `id()`.

### 2. Watches are frame-scoped, NOT function-scoped

A watch dies with its frame:

- **PY_RETURN** removes its row from `_local_watches` and pops its `_frame_state`.
- **PY_START** removes any leftover row whose `frame_id` equals the new frame's
  `id()` – this catches the case where CPython reused a freed frame's memory
  address for the new frame after an exception unwound the old one (since
  PY_UNWIND can't be a local event, see §4).
- **DO NOT** add a "zombie sweep" in `_on_line` that removes watches whose
  `frame_id != id(current_frame)`. That logic existed earlier and was *wrong*
  for concurrency – any LINE event in thread A would nuke thread B's watches
  on the same code object. See the `test_asyncio_two_tasks_watch_independently`
  failure history.

### 3. Frame state is tagged with `code`

`_frame_state[fid]` is a dict carrying `code`, `prev_line`, `prev_hashes`,
`prev_reprs`. The `code` tag is checked on every LINE event – if it doesn't
match, the state is from a dead frame whose id got reused, and we reinitialize.

### 4. PY_UNWIND can't be a local event

It can only be enabled globally, which would defeat the zero-overhead goal.
We don't register it. The combination of PY_RETURN (normal return) + PY_START
(catches id reuse) + the `code`-tag check is sufficient. State for an
exception-unwound frame leaks until its `fid` is reused; that's bounded and
acceptable.

### 5. Tool ID priority (sys.monitoring)

`_setup_monitoring` tries `[5, 4, 3, PROFILER_ID, COVERAGE_ID]` in order.
**DEBUGGER_ID (0) is never tried** – pydevd / PyCharm own it. Grabbing
COVERAGE_ID would silently break `pytest --cov`; that's why it's last.

### 6. Concurrency: lock-snapshot pattern in callbacks

All callbacks (`_on_line`, `_on_py_return`, `_on_py_start`) acquire
`self._lock` to:
- iterate `_local_watches.values()`
- mutate `_local_watches` / `_frame_state`
- compute hash/repr snapshots

The lock is **released before** any potentially-blocking call (i.e. before
`_fire_if_changed → _handle_hit → _pause_via_pydevd`). Holding it through a
pause would freeze every other thread that wants to call `watch()` /
`unwatch()`. Tests `test_concurrent_watch_mutation_does_not_crash_callback`
and `test_two_threads_watch_same_code_independently` exist specifically to
catch regressions here.

### 7. Per-thread reentrancy guard

`self._guard = threading.local()`. Each callback sets `_guard.active = True`
before doing work and clears it in `finally`. This:
- Prevents `eval()` inside `_add_local_watch` from triggering a recursive
  LINE event on the watched code object.
- Prevents pydevd's protocol code (which may touch attributes of the watched
  object while suspended) from recursively triggering a second pause through
  our `__setattr__` overrides.

Both `_WatchedSubclass.__setattr__` (specific attr) and
`_WatchedAnyAttrSubclass.__setattr__` (object-wide) check this guard.

### 8. Cross-function watch propagation

When a watching frame calls a Python function with a watched value as one
of its arguments, the watch follows the value into the callee. Mechanism:

1. `_add_local_watch` enables LINE + PY_RETURN + PY_START **+ CALL** on
   the watched code object.
2. `_on_call` (CALL callback) snapshots `{id(value): caller_name}` for every
   local watched in the calling frame and pushes
   `(callee_code, snapshot)` onto a thread-local stack
   (`_pending_propagation.queue`).
3. `_on_py_start` searches the stack top-down for an entry whose code
   matches and pops it. `_apply_propagation` then scans the callee's
   positional + keyword-only parameters and arms a local watch on every
   parameter whose value's `id()` matches a watched id. The new watch's
   `display_name` is the **caller's** original watched name so the hit
   surfaces under the user-visible identity, not the callee's parameter
   name.
4. `_python_code_for_call` resolves `callable_` to the code object whose
   PY_START will fire next: it returns `callable_.__code__` for functions
   and bound methods, and `callable_.__init__.__code__` for class
   instantiation so `MyClass(watched)` propagates into `__init__`.

Three guarantees you must preserve when touching this:

- **Identity-based matching** (`id(value)`). Precise for unique objects;
  for interned primitives (small ints, short strings) several params with
  the same value will all match. This is the documented trade-off – do
  NOT try to "fix" it by switching to value equality, which would break
  the user-defined-object case.
- **Lazy callees are skipped.** `_on_call` checks `co_flags &
  _LAZY_BODY_FLAGS` (generator / coroutine / async-generator) and returns
  without queueing. Their PY_START fires later, in a different stack
  context, so a queued entry would leak. The queue also has a hard cap
  of 128 entries as a belt-and-suspenders against any other rare
  CALL-without-PY_START scenarios (e.g. arg-binding failures).
- **The callee gets the same event set.** `_on_call` calls
  `set_local_events(..., LINE|PY_RETURN|PY_START|CALL)` on the callee's
  code so the propagated watch behaves identically to a directly-armed
  one and so deeper calls FROM the callee can propagate again.

### 9. Container-mutation + recursive object instrumentation

Two related extensions to the basic class-surgery mechanism, both anchored
in the same `_AttributeWatch` so cleanup unwinds the whole thing atomically.

**Container wrap (for `watch("obj.attr")` where attr is list/dict/set).**

`__class__` surgery is impossible on builtin instances (CPython refuses
class swap between heap and builtin types). Instead, `_add_attr_watch`
constructs a new `_WatchedList` / `_WatchedDict` / `_WatchedSet` instance
populated from the original's contents and replaces the leaf attribute.
Every mutating method (`append`/`extend`/`insert`/`remove`/`pop`/`clear`/
`sort`/`reverse`/`__setitem__`/`__delitem__`/`__iadd__`/`__imul__` for
list; the dict and set equivalents) computes a before/after `__repr__`,
calls `_wp_fire_container_change` if they differ. `_AttributeWatch.container_wrapper`
+ `container_holder` + `container_attr` remember the install so
`_remove_attr_watch_locked` can `setattr(holder, attr, _unwrap_container(...))`
to put a plain list/dict/set back in place.

The rebind-detector `_WatchedSubclass.__setattr__` *also* wraps any newly
assigned container value, so `obj.attr = []; obj.attr.append(x)` keeps
firing after the rebind. The wrap is guarded so the install setattr
doesn't spuriously fire.

Documented trade-offs (each pinned by a regression test):
- `type(obj.attr) is list` becomes False (the wrapper is a subclass).
  `isinstance` still returns True.
- References user code captured BEFORE the watch was armed point at the
  original (un-wrapped) container and mutations through them are
  invisible. `test_watch_dotted_list_attr_aliased_ref_before_watch_does_not_fire`
  pins this down.

**Recursive object-wide instrumentation (for `watch("obj")` of a user-defined object).**

`_add_object_watch` first calls `_install_single_object_watch` (which is
the old class-surgery path factored out), then `_instrument_object_tree`
walks `obj.__dict__` (cycle-guarded by a visited-id set, depth-capped
by `_RECURSIVE_OBJECT_WATCH_DEPTH = 4`):

- User-defined-object attrs → recursive `_install_single_object_watch`
  on the nested value, then `_instrument_object_tree` one level deeper.
- list/dict/set attrs → `_wrap_container` + guarded `setattr` to install
  the wrapper at the nested attribute.
- Anything else → skip.

Every sub-instrumentation creates an `_AttributeWatch` appended to the
root's `sub_watches` list. The sub-watches are NOT registered in
`_attr_watches` (avoids collision with user-installed dotted watches).
`_remove_attr_watch_locked` reverses `sub_watches` and undoes each via
`_undo_attr_watch_payload` so containers come off while their parent's
class surgery is still active to swallow the un-wrap setattr.

The watcher's `__setattr__` also auto-instruments newly assigned values:
if user code does `obj.x = new_settings_dataclass`, the new value gets
class surgery + a fresh recursive walk under the same root. New container
assignments get wrapped too. Both happen under the guard so the
auto-instrumentation never fires itself.

Two guarantees to preserve:
- **Class-surgery is per-object, not per-class.** Each call to
  `_make_any_attr_watcher_class` produces a fresh class with closure-
  captured `_expr` (this object's path) + `_root_expr` (the user's
  top-level expression). DO NOT try to share a single watcher class
  across instances of the same original class – the `watch_name` would
  collide.
- **Recursion stops at the depth cap *exclusively*.** A depth-4 object's
  direct attributes ARE instrumented; depth-5 ones aren't. Two regression
  tests (`test_recursive_watch_depth_cap` and
  `test_recursive_watch_depth_at_cap_still_fires`) pin both sides.

Limitations the design intentionally accepts:
- Containers' CONTENTS (e.g., a list-of-user-objects) are not recursed
  into. Mutating `obj.items[0].name` doesn't fire even if `items` is the
  watched container's contents.
- Slotted classes (no `__dict__`) aren't iterated by `_safe_iter_dict_attrs`.
  Direct attr changes on them still fire (the class surgery catches
  `__setattr__`), but nested user objects under slotted attrs aren't
  auto-discovered.
- `@property` and descriptor-backed attrs aren't iterated.

### 10. Classpatch fallback for hostile metaclasses

When `class _WatchedSubclass(orig_cls):` itself raises (Django's
`ModelBase`, SQLAlchemy's `DeclarativeMeta`, any framework whose
metaclass demands app/registry membership at class-creation time), the
class-surgery strategy is unavailable BEFORE we even reach the
`__class__` swap. Falling back to local-variable rebind detection makes
watch effectively useless for these instances – which are exactly the
ones a Django/SQLAlchemy user is most likely to want to watch.

The fallback is `_install_classpatch_attr_watch`: it monkey-patches the
*existing* class's `__setattr__` (which is allowed – ModelBase /
DeclarativeMeta don't override the metaclass's `__setattr__`, so
`cls.__setattr__ = patched` just goes through `type.__setattr__`). The
patched method consults `_ClassPatch.instance_watches`, a
`{id(instance): {attr_name | '__any__': _AttributeWatch}}` map, so only
SPECIFIC instances trigger – other instances of the same patched class
pass through to the original `__setattr__`. The patch is removed from
the class (and the `_classpatch_registry` entry deleted) when the last
watched instance is unwatched.

Two entry points:
- **Bare-name** `watch('django_obj')` → `_try_classpatch_object_watch`
  installs a `'__any__'` wildcard entry. Any attribute write on the
  watched instance fires under `f"{expr}.{attr_name}"` so the user sees
  which attribute changed (e.g. `'obj.tag'` rather than just `'obj'`).
  Recursion into nested attrs is NOT performed by this path – classpatch
  catches top-level rebinds only, NOT in-place container mutations.
  Users who need in-place mutation tracking should watch the dotted
  path so the leaf gets a container wrap.
- **Dotted** `watch('django_obj.field')` → `_try_classpatch_attr_watch`
  installs a specific-attribute entry. Fires only when `field` is
  rebound. If the leaf is a container, also wraps-and-replaces it so
  in-place mutations fire too (same as the class-surgery dotted path).

The patched `__setattr__` runs the same `_handle_hit` pipeline as the
class-surgery and container-mutation paths – pause behavior and hit
notification format are identical across all three.

Guarantees to preserve:
- **Per-instance, not per-class.** `instance_watches.get(id(self_obj))`
  filters every call; unrelated instances of the patched class pay one
  O(1) dict miss and pass through unchanged. DO NOT switch to a class-
  level "is this class watched?" gate – it would fire on every instance,
  including unrelated ORM operations on the user's other models.
- **Re-entrancy guard mirrors the class-surgery overrides.** The same
  `_registry._guard` that protects `_WatchedSubclass.__setattr__` and
  `_WatchedAnyAttrSubclass.__setattr__` protects the classpatch
  `__setattr__`. Without it, pydevd's protocol code touching the watched
  object's attributes while paused would recursively trigger a second
  pause. The container wrap inside `_try_classpatch_attr_watch` also
  runs under the guard so the install `setattr` doesn't fire as a
  spurious initial hit.
- **Specific takes priority over wildcard.** When a user has both
  `watch('obj')` and `watch('obj.name')` armed, a write to `obj.name`
  reports the specific watch_name. Writes to other attributes go through
  the wildcard. This ordering keeps the user-visible hit identity stable
  with what the user explicitly typed.
- **`_instrument_object_tree` does NOT use this fallback.** Recursive
  class-surgery skips Django children silently (test:
  `test_django_like_nested_under_recursive_watch_skipped_gracefully`).
  Auto-classpatching every nested ORM model under a parent watch would
  install patches on potentially dozens of classes from a single
  bare-name watch, with surprising side effects. The classpatch
  fallback is reserved for the top-level dispatch where the user
  explicitly named the instance.
- **Cleanup is symmetric.** `_undo_attr_watch_payload` calls
  `_remove_classpatch_attr_watch(obj_ref, classpatch_key)`. When the
  last `instance_watches` entry on a class is gone, the patched
  `__setattr__` is removed from `cls.__dict__` (if we installed it
  fresh) or restored to the original (if the class already had its
  own `__setattr__` before we patched). Cleanup is best-effort: if
  the class refuses removal (`_StubbornDjangoLikeMeta` test fixture),
  the patched method becomes a functional no-op since
  `instance_watches` is now empty.

When even classpatch can't be installed (a metaclass that overrides
`__setattr__` to refuse class-level attribute writes – not seen in
real frameworks but pinned by `_StubbornDjangoLikeMeta` tests):
bare-name watch falls through to local-variable rebind detection;
dotted watch raises a clean `TypeError` naming both failure modes so
the user understands neither subclassing nor classpatching worked.

## The pydevd pause – tread carefully

### The two rules

**Rule 1: never call `py_db.do_wait_suspend(...)` directly from inside our
callback chain.** Doing so puts the user thread into pydevd's protocol-
encoding code (which uses `urllib.parse.quote` for XML escaping) at the
moment the IDE polls for the stack. The user then sees `urllib/parse.py` as
the topmost "stopped at" frame, with our `<string>`-exec'd module frames
showing as `<frame not available>`.

**Rule 2: never use `set_suspend(... is_pause=True)` + `state = STATE_SUSPEND`
for our pause either.** That sets up "pause on the next event in ANY frame",
which means the suspend latches on the FIRST `trace_dispatch`-armed frame
pydevd's tracer encounters as code resumes. If the user's next line happens
to call `print(...)` (or any I/O), the suspend gets caught by a stdlib codec
frame deep in pydevd's stdout-interception chain – a common landing site is
`codecs.BufferedIncrementalDecoder.decode`, which the IDE shows as topmost
`<frame not available>` with `self = <encodings.utf_8.IncrementalDecoder>`
and the user's frame visible one level down. The container-mutation path
(`_wp_fire_container_change`) "happened to work" with this approach because
the next event after a `.append(...)` was usually a LINE event in the same
user frame (loop continuation) – no intervening stdlib code to latch onto.
But for any setattr followed by an I/O-doing line, the bug bites.

Both of these took multiple iterations to discover. Don't unwind either rule.

### What we do instead: scoped step-over

`_pause_via_pydevd` mimics `pydevd.settrace(suspend=True, stop_at_frame=user_frame)`:

```python
# state = STATE_RUN (NOT SUSPEND) + scoped step-over on user_frame
info.pydev_state = STATE_RUN
info.pydev_step_cmd = CMD_STEP_OVER
info.pydev_step_stop = user_frame
info.suspend_type = PYTHON_SUSPEND

# Belt-and-suspenders: disarm f_trace on our own `<string>` frames between
# here and user_frame so their unwind events go through NO pydevd tracing.
own_frame = sys._getframe(0)
while own_frame is not None and own_frame is not user_frame:
    own_frame.f_trace = None
    own_frame = own_frame.f_back

# Arm user_frame's f_trace with the FULL trace_dispatch. Pydevd's CALL
# handler often parks it at `trace_exception` (no step_cmd check) when
# the frame has no in-line breakpoints – which would silently swallow
# our CMD_STEP_OVER. Direct assignment is the most reliable mechanism;
# the official API has multiple silent early-return paths.
user_frame.f_trace = py_db.trace_dispatch
py_db.set_trace_for_frame_and_parents(user_frame)
# return – pydevd's tracer fires the actual pause from its own context
```

Why this works: with `CMD_STEP_OVER` + `step_stop = user_frame` + `state =
RUN`, pydevd's `trace_dispatch.can_skip` short-circuits on EVERY frame that
isn't user_frame. Codec frames in print's chain, IO interceptors, our own
`<string>` frames, pydevd's own pre-suspend bookkeeping – all flow through
`trace_exception` (no pause check) without latching. Only when pydevd's
tracer fires a LINE (or RETURN) event on user_frame itself does the
CMD_STEP_OVER branch flip `stop = True`, which calls `set_suspend(CMD_SET_BREAK)`
from pydevd's own tracer context and then `do_wait_suspend` – clean pause
with user_frame on top.

Result: pause lands on the **next line** in user_frame, from pydevd's own
tracer context. The IDE shows the user's frame on top, no `urllib` or
`<frame not available>` clutter, regardless of whether the next line does
I/O.

Trade-offs:
- Pause is one line **after** the assignment (because `__setattr__` has to
  return before pydevd's tracer can fire). We mitigate by emitting a
  `[WATCHPOINT] hit '...': old -> new at file:line` line to stderr (visible
  in the Debug Console) at the moment of the hit.
- The IDE's stop reason becomes `CMD_SET_BREAK` ("set break") instead of
  `CMD_THREAD_SUSPEND` ("thread suspend"). Cosmetic, and arguably more
  accurate for a watchpoint anyway.
- `_threads_suspended_single_notification.on_pause()` isn't called (we no
  longer pass `is_pause=True`). The IDE doesn't get the "user-requested
  pause" signal; it sees a breakpoint hit instead. Cosmetic.
- If pydevd has an in-progress step at the moment our watchpoint fires, we
  clobber `step_cmd` / `step_stop`. In practice this can't happen because
  stepping implies the thread is paused (so user code isn't executing).

### Looking up the pydevd debugger

`_get_pydevd_debugger()` prefers `_pydevd_bundle.pydevd_constants.GlobalDebuggerHolder.global_dbg`
(the canonical state). It falls back to `import pydevd; pydevd.get_global_debugger()`
which goes through pydevd's re-export chain. Both reach the same instance –
the direct read avoids surprises when `pydevd.py` was launched as `__main__`
versus when it was imported as a module.

### Don't set `info.pydev_message`

It looks like a friendly knob ("show the user a custom stop message") but
pydevd then URL-encodes it via `urllib.parse.quote` into the protocol XML –
which is one of the call paths that put the user thread visually inside
`urllib/parse.py`. The stop message we log to stderr instead is enough.

### No-pydevd fallback

When `_get_pydevd_debugger()` returns `None` (i.e. running under plain
pytest), `_handle_hit` **raises** `WatchpointHit`. The whole test suite relies
on this fallback – do not remove it.

## Cross-version notes (3.12 / 3.13 / 3.14)

| Concern                                  | Behavior across versions                                     |
| ---------------------------------------- | ------------------------------------------------------------ |
| `sys.monitoring` API                     | Stable since 3.12. Same callback signatures.                 |
| `frame.f_locals`                         | 3.13 made it a fresh `FrameLocalsProxy` each access. We always `dict(frame.f_locals)` once. |
| LINE-callback exception propagation      | **3.14 bypasses local `try/except` in the monitored frame.** Tests expecting `WatchpointHit` must wrap the monitored code in an inner helper – see the comment block at the top of `test_watchpoint.py`. |
| `PY_UNWIND` as a local event             | Rejected on all 3.12+ versions (`ValueError: invalid local event set`). Confirmed empirically. |
| `PY_START` as a local event              | Works on 3.12+ (confirmed). We rely on this for id-reuse cleanup. |

## `_value_hash` semantics

- Immutable primitives (`None`/`bool`/`int`/`float`/`str`/`bytes`/`tuple`/...):
  `hash((type, value))` – includes the type so `1 == True` and `1 == 1.0`
  don't mask a real type change.
- Mutable containers (`list`/`dict`/`set`/`bytearray`): `hash(repr(value))` –
  O(n), but only paid per line of ONE watched function.
- Custom objects: `id(value) ^ hash(type.__qualname__)` – rebinding to a new
  instance of the same type yields a different hash; **in-place mutation
  through methods is NOT detected** (that's what object-wide attribute
  watching is for).

There is a `test_value_hash_distinguishes_equal_long_strings` regression test
locking in correct behavior for strings of any length – an old version of
this function fell through to `id()` for strings ≥ 64 chars, which broke
content-equality.

## Frame discovery for the PyCharm action

When the IDE evaluator runs `_pycharm_watch_at(name, file_hint, func_hint)`,
the evaluator's `sys._getframe()` stack does **not** contain the user's
paused frame (the eval happens in a separate context). `_find_paused_user_frame`
walks every thread's stack via `sys._current_frames()` and matches by
`co_name == func_hint` + multiple file-suffix comparisons (absolute /
`/private/var/...` prefixes / basename) so it works across macOS path
quirks. Test: `test_watch_at_locates_paused_frame_in_other_thread`.

## What lives where in `watchpoint.py`

| Section                           | Purpose                                       |
| --------------------------------- | --------------------------------------------- |
| `WatchpointHit`                   | The exception class. Raised in the no-pydevd fallback path. |
| `WatchpointRegistry`              | The singleton holding all state.              |
| `WatchpointRegistry.add_watch`    | Dispatch: local / object-wide / specific-attr. |
| `WatchpointRegistry._add_local_watch` | Installs a per-frame local watch + enables LINE/PY_RETURN/PY_START/CALL. Accepts a `display_name` override used by propagation. |
| `WatchpointRegistry._add_attr_watch`  | Class surgery for `obj.attr` rebind detection; if the leaf is a container, also calls `_wrap_container` and replaces the attribute. Bookkeeping for the wrap goes into the `_AttributeWatch`. Falls back to `_try_classpatch_attr_watch` if the class-statement raises (Django metaclass etc.). |
| `WatchpointRegistry._try_classpatch_attr_watch` | Classpatch fallback for dotted watches when subclassing fails. Installs an instance-specific entry under `attr_name`; if the leaf is a container, also wraps it. Returns False if even `cls.__setattr__ = patched` is blocked (caller raises clean TypeError). |
| `WatchpointRegistry._add_object_watch`| Top-level object watch: installs surgery on the root via `_install_single_object_watch`, then `_instrument_object_tree` walks nested user-defined attrs + containers to depth 4. |
| `WatchpointRegistry._try_classpatch_object_watch` | Classpatch wildcard fallback for bare-name watches when subclassing fails. Installs a `'__any__'` entry so any attribute write on this instance fires under `f"{expr}.{attr}"`. Does NOT recurse – the recursive walker is unused on the classpatch path. |
| `WatchpointRegistry._install_single_object_watch` | Per-object class-surgery installer. Used by `_add_object_watch` for the root AND by `_instrument_object_tree` for every nested user-defined object. Returns an `_AttributeWatch` the caller either registers in `_attr_watches` (root) or appends to `root.sub_watches` (nested). Does NOT use classpatch fallback – only the top-level dispatch in `add_watch` does. |
| `WatchpointRegistry._make_any_attr_watcher_class` | Constructs the `_WatchedAnyAttrSubclass(original_cls)` with closure-captured `_expr` + `_root_expr`. Its `__setattr__` fires `_handle_hit`, wraps container values, recursively instruments newly-assigned user objects (under the same `_root_expr`). |
| `WatchpointRegistry._instrument_object_tree` | Recurses `obj.__dict__` to depth `_RECURSIVE_OBJECT_WATCH_DEPTH = 4`. Cycle-guarded by a per-call visited-id set. Records each nested sub-instrumentation in `root_watch.sub_watches`. |
| `WatchpointRegistry._undo_attr_watch_payload` | Reverses one `_AttributeWatch`'s instrumentation (un-wraps its container if any, restores `obj.__class__` if any). Shared by root + sub-watch cleanup. |
| `WatchpointRegistry._on_line` / `_on_py_return` / `_on_py_start` | sys.monitoring callbacks. PY_START also picks up cross-function propagations. |
| `WatchpointRegistry._on_call`     | CALL callback. Snapshots watched locals and queues a propagation for the callee. Skips lazy-body callees (gen/coro/asyncgen) and non-Python callables. |
| `WatchpointRegistry._apply_propagation` | Inside callee's PY_START, scans positional + keyword-only params and arms watches on those whose value's id matches a watched id. |
| `_python_code_for_call`           | Resolves a callable to the code object whose PY_START will fire next: `__code__` for functions/methods, `__init__.__code__` for classes. |
| `_LAZY_BODY_FLAGS`                | Bitmask of CO_GENERATOR | CO_COROUTINE | CO_ASYNC_GENERATOR – used by `_on_call` to skip lazy-body callees. |
| `_LocalWatch.display_name`        | User-visible name for `WatchpointHit.watch_name`. Equals `name` for direct watches; carries the caller's original watched name for propagated ones. |
| `_AttributeWatch.sub_watches`     | List of nested `_AttributeWatch` instances installed by `_instrument_object_tree`. Walked in reverse on cleanup so deepest sub-watches undo first. |
| `_AttributeWatch.container_wrapper` / `_holder` / `_attr` | Bookkeeping for a wrap-and-replace: enough to find the wrapper at its host attribute and restore the plain container on cleanup. |
| `_AttributeWatch.classpatch_key`  | Marks this watch as a classpatch install. Either an attribute name (dotted) or `'__any__'` (wildcard from bare-name). `_undo_attr_watch_payload` calls `_remove_classpatch_attr_watch(obj_ref, classpatch_key)` when set. |
| `_ClassPatch`                     | Per-class state for one monkey-patched `__setattr__`. Carries the original setattr (for restore), `had_own_setattr` flag (decides between `cls.__setattr__ = orig` vs `del cls.__setattr__` on cleanup), and `instance_watches: {id(obj): {attr | '__any__': _AttributeWatch}}`. |
| `_classpatch_registry`            | `{cls: _ClassPatch}` global map. Mutated under `_classpatch_lock` during install/remove; reads from inside patched `__setattr__` use the closure-bound `_ClassPatch` instance directly so the hot path doesn't contend on this lock. |
| `_install_classpatch_attr_watch` / `_remove_classpatch_attr_watch` | Install / undo one classpatch entry. Install builds and assigns the patched `__setattr__` lazily on the first watch for a class; remove restores the original (or `del`s our patched attr) when the last instance's entry on that class goes away. |
| `_find_inherited_setattr`         | MRO walk past `cls` to find the closest `__setattr__`. Used at classpatch install time so the patched function's hot path is a single pre-bound call rather than a per-setattr MRO walk. |
| `_WatchedList` / `_WatchedDict` / `_WatchedSet` | Subclasses of the builtin containers. Every mutating method computes a before/after `__repr__` and calls `_wp_fire_container_change` if they differ. |
| `_wrap_container` / `_unwrap_container` | Constructors / inverse: wrap a list/dict/set in the right `_Watched*` subclass; produce a plain copy back from a wrapper on cleanup. |
| `_wp_fire_container_change`       | Container-side firing path. Reads `_wp_registry` / `_wp_expr` from the wrapper's `__dict__`; null `_wp_registry` is the "dead" signal that user code holding a leaked wrapper alias must respect. |
| `_RECURSIVE_OBJECT_WATCH_DEPTH`   | Recursion cap (4). Depth-N attrs ARE instrumented; depth-(N+1) recursion returns early. |
| `_safe_iter_dict_attrs`           | `obj.__dict__` iteration helper for `_instrument_object_tree`. Skips dunder names and silently no-ops on slotted classes. |
| `_handle_hit`                     | Pause-via-pydevd or raise fallback.           |
| `_get_pydevd_debugger`            | Robust lookup, multiple fallbacks.            |
| `_pause_via_pydevd`               | Scoped step-over: `CMD_STEP_OVER` + `step_stop = user_frame` + `state = RUN`, plus direct `user_frame.f_trace = trace_dispatch` and a disarm loop on our own `<string>` frames. Pauses ONLY when pydevd's tracer hits a LINE event on `user_frame`, never on intervening codec / IO / `<string>` frames. |
| `_is_object_watchable`            | Heuristic for the auto-detection in `add_watch`. |
| `_value_hash`                     | Change-detection hash with type tag.          |
| `_setup_monitoring`               | Claims a tool ID, registers callbacks (LINE / PY_RETURN / PY_START / CALL). Guarded against re-import. |

## Things you might be tempted to do, but shouldn't

- **Re-introduce a `_drop_dead_frame_watches` sweep in `_on_line`.** It looks
  natural ("clean up watches with mismatched fid") but it deletes concurrent
  live frames' watches.
- **Call `do_wait_suspend` directly to "pause right here".** It puts urllib
  into the call stack. Use the scoped step-over pattern in `_pause_via_pydevd`.
- **Set `info.pydev_message`.** Same urllib pause path.
- **Switch back to `set_suspend(... is_pause=True)` + `state = STATE_SUSPEND`.**
  Looks simpler than the current CMD_STEP_OVER setup, and the container path
  even "appears to work" with it. But for any setattr followed by an I/O-doing
  line (a `print`, a `logging.info`, *anything* that writes to stdout), the
  STATE_SUSPEND flag latches on a stdlib codec frame in pydevd's stdout-
  interception chain (`codecs.BufferedIncrementalDecoder.decode` with
  `self = <encodings.utf_8.IncrementalDecoder>`), not on the user's frame.
  CMD_STEP_OVER + `step_stop = user_frame` is the only pattern that pauses
  cleanly regardless of what user code does next. See "The pydevd pause" §.
- **Drop `user_frame.f_trace = py_db.trace_dispatch` and rely solely on
  `set_trace_for_frame_and_parents`.** The official API silently no-ops in
  several scenarios (PEP 669 monitoring mode, filtered files, certain
  pydevd-builds) and may leave user_frame's `f_trace` at `trace_exception`
  – which only handles exception events and never fires our CMD_STEP_OVER.
  Direct assignment guarantees the next LINE event on user_frame routes
  through the full trace_dispatch.
- **Drop the disarm loop in `_pause_via_pydevd`.** With CMD_STEP_OVER, the
  loop is largely belt-and-suspenders, but cheap and removes one category
  of potential failure. Regression test:
  `test_pause_via_pydevd_disarms_own_frames_to_keep_user_frame_topmost`.
- **Register `PY_UNWIND` globally for cleanup.** It works but kills the
  zero-overhead guarantee.
- **Use `weakref` on a frame.** Frames are not weakly referenceable in CPython
  (confirmed empirically on 3.12/3.13/3.14).
- **Hold `self._lock` while calling `_handle_hit`.** Pause would freeze every
  other thread's `watch()` calls.
- **Remove the raise fallback in `_handle_hit`.** The 121-test suite depends
  on it for the no-pydevd environment.
- **Drop the `_LAZY_BODY_FLAGS` check in `_on_call`.** Generator / coroutine /
  async-generator function calls return their iterable WITHOUT entering the
  body – PY_START fires later, in a different stack context. Queueing a
  propagation for them strands the entry on the per-thread stack.
- **Queue propagations for non-Python callables.** The check on
  `_python_code_for_call` returning None is what keeps `print(watched)` and
  similar builtin/C calls from leaking entries. Don't relax it.
- **Drop `__slots__ = ()` from the watcher subclasses.** Without it the
  subclass implicitly adds a `__dict__`, making `__class__ = WatchedSubclass`
  raise "layout differs" TypeError for any class with `__slots__`.
- **Switch propagation matching from `id()` to `==`.** Identity matching is
  precise for unique objects; switching to equality would mean two unrelated
  user-defined objects with `__eq__` could falsely cross-trigger. The
  interned-primitive over-watch (small ints, short strings) is a documented
  trade-off, not a bug.
- **Remove the queue cap in `_on_call`.** Even with lazy-body and non-Python
  filtering, pathological CALL-without-PY_START sequences (rare arg-binding
  failures) would otherwise grow the per-thread queue without bound.
- **Try `__class__` surgery on a list/dict/set instance.** CPython refuses
  class swap between heap and builtin types (`TypeError: __class__
  assignment: only for heap types`). The container path constructs a
  fresh `_WatchedList` / `_WatchedDict` / `_WatchedSet` populated from the
  original's contents and replaces the leaf. Trying surgery first wastes
  cycles and produces a misleading error message.
- **Wrap a container without going through the rebind-detector's guard.**
  `_add_attr_watch` (and `_WatchedSubclass.__setattr__` on reassignment)
  guards the install setattr so it doesn't fire as a "change". Without the
  guard, every freshly-armed container watch would emit a spurious initial
  hit on the wrap.
- **Register sub-watches in `_attr_watches`.** They live ONLY in
  `root_watch.sub_watches`. Registering them too would (a) collide with
  user-issued dotted watches at the same path and (b) make `_remove_attr_watch_locked`
  enumeration tricky. Cleanup walks `sub_watches` in reverse so deepest
  sub-watches undo first.
- **Share a watcher class across instances of the same original class.**
  Each call to `_make_any_attr_watcher_class` produces a fresh dynamic
  class because the closure captures THIS instance's `_expr` + `_root_expr`.
  Sharing the class would cross-pollinate `watch_name`s across unrelated
  watch trees.
- **Raise the depth cap blindly.** The cap is bounded by `_RECURSIVE_OBJECT_WATCH_DEPTH = 4`
  for memory + startup time reasons. Each level multiplies the number of
  installed watcher classes; a fan-out of N at every level gives N^depth
  classes. The pythonvartracker reference chose 4 for the same reason.
- **Drop the per-instance filter from classpatch `patched_setattr`.** The
  `entries = _patch.instance_watches.get(id(self_obj))` gate is what
  keeps unrelated instances of the patched class from firing. Removing
  it would mean a single `watch('one_user_model.field')` fires for
  every Django model of that type in the process – including ones the
  ORM is constructing as part of a query – and the user would never
  understand what's pausing them.
- **Use classpatch from inside `_instrument_object_tree`.** The
  recursive walker explicitly skips nested instances when class-surgery
  fails (`_install_single_object_watch` raises TypeError). Auto-
  classpatching them would install patches on every nested ORM class
  reachable from a parent watch – potentially dozens of classes from a
  single bare-name watch, with cleanup invariants we don't enforce
  recursively. Classpatch is reserved for the top-level dispatch where
  the user explicitly named the instance.
- **Switch from `cls.__setattr__ = patched` / `del cls.__setattr__` to
  in-place mutation of the original.** Restoring on unwatch only works
  if we kept the original separate. Mutating the original would also
  break code that holds a reference to the unpatched function (less
  common, but real for any framework that pre-binds setattr).
- **Forget the `getattr(_reg._guard, "active", False)` check in the
  classpatch `__setattr__`.** Without it, pydevd's protocol code can
  recursively pause when it touches attributes of the watched object
  during a pause – the same trap the in-class `__setattr__` overrides
  in `_WatchedSubclass` / `_WatchedAnyAttrSubclass` were designed to
  avoid. The guard is shared across all three paths for the same reason.

## Diagnostics for live debug sessions

From the PyCharm evaluator while paused:

```python
_pycharm_watchpoint_diag()
```

returns a one-line summary: `pydevd` in `sys.modules`, `_pydevd_bundle`
state, `sys.gettrace` owner, last-known lookup error. Useful when the IDE
shows weird pause behavior – the diag tells you whether we found the
debugger and whether `_pause_via_pydevd` ran at all.

Hit notifications go to stderr (Debug Console) as
`[WATCHPOINT] hit '<watch>': <old> -> <new> at <file>:<line>`.

## Known limitations (pinned down by tests – don't silently "fix" without re-reading)

These behaviors are intentional or unavoidable under the current design. Each
has a regression test so a well-meaning future change doesn't accidentally
reintroduce the symptom and call it a fix.

- **`del obj.attr` is silent.** We override `__setattr__` but not
  `__delattr__`. See `test_watch_attribute_deletion_does_not_crash`.
- **`obj.__dict__['attr'] = value` is silent.** Bypasses `__setattr__`. See
  `test_watch_dict_bypass_via_obj_dict_does_not_crash`.
- **`del watched_local` and untracked `*args`/`**kwargs` unpacking.** No
  propagation for variadic unpacking.
- **Interned primitives over-watch in propagation.** `watch("x")` for
  `x = 1` will also watch any other parameter receiving the small int 1
  (because `id(1) == id(1)`). Test:
  `test_propagation_acknowledged_limitation_interned_primitives`.
- **Default-value parameters don't propagate.** A function's default is
  fixed at definition time and not connected to any caller's watched local.
  Test: `test_propagation_through_default_kwarg_value`.
- **Generators / coroutines / async generators don't propagate.** Their
  body isn't entered at call time. Tests:
  `test_propagation_into_generator_function_does_not_leak`,
  `test_propagation_through_async_function_does_not_leak`.
- **`functools.partial`, C-implemented `__init__`, and other callables
  without a Python `__code__` don't propagate.** They're skipped by
  `_python_code_for_call`.
- **Property setters may or may not be intercepted** depending on whether
  CPython routes `obj.prop = x` through `type.__setattr__` (which our
  override sees) or directly to the descriptor. The test skips itself if
  the descriptor path bypasses us – see
  `test_watch_attribute_with_property_setter`.
- **Frozen dataclasses can't have class surgery applied.**
  `_add_object_watch` catches the `FrozenInstanceError` and raises a clean
  `TypeError`, which `add_watch` then handles by falling back to local-
  variable detection. Test: `test_watch_frozen_dataclass_falls_back_or_skips_gracefully`.
- **Heavily-metaclassed types (Django `Model`, SQLAlchemy declarative
  base) can't be subclass'd dynamically, but the classpatch fallback
  handles them.** Their metaclass refuses
  `class _WatchedAnyAttrSubclass(orig_cls):` and raises (Django:
  `RuntimeError: Model class … doesn't declare an explicit app_label`).
  Both `_add_attr_watch` and the bare-name dispatch in `add_watch` catch
  the failure and call into the classpatch path
  (`_try_classpatch_attr_watch` / `_try_classpatch_object_watch`) which
  monkey-patches `cls.__setattr__` to intercept writes on the watched
  instance. Bare-name `watch('django_obj')` then fires on ANY attribute
  write (wildcard); dotted `watch('django_obj.field')` fires only on
  that attribute. A `[WATCHPOINT]` warning hits stderr explaining the
  fallback. Tests: `test_django_like_dotted_watch_fires_on_specific_attr`,
  `test_django_like_dotted_watch_fires_when_method_rebinds_attribute`
  (the exact user-reported `set_field` pattern),
  `test_django_like_bare_name_watch_fires_on_any_attribute`,
  `test_django_like_specific_takes_priority_over_wildcard`,
  `test_django_like_classpatch_cleanup_independent_for_two_classes`,
  `test_django_like_nested_under_recursive_watch_skipped_gracefully`
  (recursion still skips Django children silently rather than
  classpatching them).
- **Bare-name classpatch wildcard does NOT catch in-place container
  mutations.** `watch('django_obj')` intercepts top-level attribute
  rebinds via the patched `__setattr__`; `obj.somelist.append(x)`
  doesn't go through `__setattr__` at all, so no fire. Users who need
  in-place container tracking on a Django attribute should watch the
  dotted path so the leaf gets a container wrap
  (`_try_classpatch_attr_watch` wraps list/dict/set leaves).
- **Truly-stubborn metaclasses that block class-level `__setattr__`
  assignment too.** When both subclassing AND `cls.__setattr__ = ...`
  are refused, dotted watch raises a clean TypeError naming both
  failures; bare-name watch falls through to local-variable rebind
  detection. Tests: `test_stubborn_metaclass_dotted_watch_raises_clear_type_error`,
  `test_stubborn_metaclass_bare_name_falls_back_to_local_variable`.
- **Container aliases captured BEFORE watch-arm don't fire.** The
  wrap-and-replace approach swaps the host attribute for a `_WatchedList`
  / `_WatchedDict` / `_WatchedSet`. Any reference user code held to the
  original container is now stale; mutations through it never reach the
  wrapper. Test: `test_watch_dotted_list_attr_aliased_ref_before_watch_does_not_fire`.
- **Container contents inside a recursively-watched object aren't recursed
  into.** If `obj.items` is a watched list of user-defined objects,
  `obj.items[0].name = "x"` does NOT fire. The recursion in
  `_instrument_object_tree` walks `__dict__` attributes; it doesn't open
  containers. Watch the inner object directly if you need this.
- **Recursive object-watch has a depth cap.** `_RECURSIVE_OBJECT_WATCH_DEPTH = 4`.
  Mutations beyond that level don't fire. Tests:
  `test_recursive_watch_depth_cap` (no fire) and
  `test_recursive_watch_depth_at_cap_still_fires` (just-inside fires).
- **Slotted classes aren't recursed into.** `_safe_iter_dict_attrs`
  silently no-ops on objects without `__dict__`. The slotted object's own
  direct attr changes still fire (class surgery catches `__setattr__`),
  but its slotted attrs' nested objects aren't auto-discovered.

## Test layout (`test_watchpoint.py`)

Organized in roughly eleven bands (121 tests):

1. **Basics** – fire on change, old/new values, source line, unwatch, clear,
   multiple watches.
2. **Frame lifetime** – repeated calls, recursion, stale-state reset.
3. **Regression** – long-string hash, double-watch rearm, no-pydevd contract,
   `watch_at` lookup.
4. **Concurrency** – thread races, two-thread independence, asyncio gather,
   await survival.
5. **Object-wide watching** – `_RequestLike` fixture; mutation fires, same-value
   silent, class surgery reversed by unwatch, change-from-other-function.
6. **Last-line / PY_RETURN** – ensures change-on-last-line is detected and
   the source line reports correctly (pause-target uses `frame.f_back` so
   pydevd doesn't try to suspend a dying frame).
7. **Cross-function watching** – object survives nested calls, list/dict
   mutation via helper detected on return, primitive follows argument into
   callee via the CALL/PY_START propagation. See
   `test_propagation_*` and `test_watch_*_via_helper_*`.
8. **Edge cases** – methods, kwargs, multiple watched args, chained
   propagation, recursive self, class instantiation (`__init__` lookup),
   lazy-body skip (gen/async), augmented assignment, for-loop rebind,
   subscript mutation, slots, properties, frozen dataclass, classmethod,
   staticmethod, builtin skip, queue-leak sanity checks, interned-primitive
   trade-off, default-arg behavior.
9. **Container mutation watching** – `test_watch_dotted_list_attr_*` /
   `test_watch_dotted_dict_attr_*` / `test_watch_dotted_set_attr_*`:
   every mutating method fires when watching the dotted-path container;
   same-value mutations stay silent; pop()/methods preserve return values;
   reassign-then-mutate auto-wraps the new container; unwatch restores
   plain type; leaked wrapper aliases stop firing post-unwatch;
   captured-before-watch aliases bypass the wrap.
10. **Recursive object-wide watching** – `test_recursive_watch_*`:
    nested attribute changes fire under the user-visible dotted name;
    nested list/dict mutations fire too; cycles + self-refs don't blow
    the stack; unwatch restores every nested class and container; depth
    cap (`_RECURSIVE_OBJECT_WATCH_DEPTH = 4`) is respected at both sides;
    two paths to the same object instrument once; newly-assigned nested
    values get auto-instrumented; helper-function mutation paths work
    without any propagation machinery (class surgery is ambient).
11. **Hostile metaclasses + classpatch fallback** – `test_django_like_*`
    / `test_stubborn_metaclass_*`: the Django Model / SQLAlchemy
    declarative-base failure mode where a parent class's metaclass
    refuses our dynamic subclass. The classpatch fallback monkey-patches
    `cls.__setattr__` so both dotted and bare-name watches still fire.
    Coverage: specific-attr fire, method-rebind fire (the reported user
    case), same-value silent, other-instance unaffected, other-attr
    unaffected, unwatch restores `__setattr__`, wildcard fires on any
    attr, specific-takes-priority-over-wildcard, two-classes-independent
    cleanup, nested-under-recursive-watch still silently skipped
    (recursion doesn't auto-classpatch), `_StubbornDjangoLikeMeta` for
    the truly-unpatchable path (dotted raises TypeError, bare-name
    falls back to local-variable rebind).

Tests use a helper-inner-function pattern (`def _code(): ...; with
pytest.raises(WatchpointHit): _code()`) because of the 3.14 LINE-exception
propagation behavior. See the docstring at the top of the file.
