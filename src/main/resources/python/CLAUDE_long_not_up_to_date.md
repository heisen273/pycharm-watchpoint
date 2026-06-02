# Python watchpoint runtime – handoff notes

This directory contains the Python side of the **pythonwatchpoint** PyCharm plugin.
It implements *watchpoints* for Python 3.12+ using `sys.monitoring` (PEP 669):
when a watched variable or attribute changes, the debugger pauses at the change
site. The Kotlin plugin is upstream of this directory and is not covered here.

## TL;DR

- `watchpoint.py` – runtime: registry, sys.monitoring callbacks, pydevd integration.
- `test_watchpoint.py` – 189 tests, pure-pytest (no pydevd).
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
walks `obj.__dict__` (cycle-guarded by `root_watch.visited_ids`, depth-capped
by `_RECURSIVE_OBJECT_WATCH_DEPTH = 4`, breadth-capped by
`_MAX_SUB_WATCHES_PER_ROOT = 100`):

- User-defined-object attrs whose type is NOT a class object AND passes
  `_is_user_defined_type` → recursive `_install_single_object_watch`
  on the nested value, then `_instrument_object_tree` one level deeper.
- list/dict/set attrs → `_wrap_container` + guarded `setattr` to install
  the wrapper at the nested attribute.
- Framework-typed attrs (Django QuerySet, SQLAlchemy session, stdlib
  types, anything under site-packages) → skipped at the boundary.
- Class objects (`isinstance(value, type)`) → skipped. Walking a class's
  `__dict__` resolves every descriptor / property / classmethod and
  chains explosively under ORMs.
- Anything else (primitives, builtins, already-watched values) → skip.

Every sub-instrumentation creates an `_AttributeWatch` appended to the
root's `sub_watches` list via `_try_add_sub_watch` (the cap-respecting
helper). Sub-watches are NOT registered in `_attr_watches` (avoids
collision with user-installed dotted watches).
`_remove_attr_watch_locked` reverses `sub_watches` and undoes each via
`_undo_attr_watch_payload` so containers come off while their parent's
class surgery is still active to swallow the un-wrap setattr.

The watcher's `__setattr__` also auto-instruments newly assigned values:
if user code does `obj.x = new_settings_dataclass`, the new value gets
class surgery + a fresh recursive walk under the same root **sharing the
root's `visited_ids` set and respecting the same type / class-object /
breadth filters**. New container assignments get wrapped too. Both
happen under the guard so the auto-instrumentation never fires itself.

Three guarantees to preserve:
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
- **`visited_ids` lives on `root_watch`, not as a per-call argument.**
  This is what stops the Django-shaped explosion: framework descriptors
  fabricate fresh proxy instances on each access, so a per-call set
  never matched across `__setattr__` re-entries and each re-entry
  started a brand-new depth-1 walk. Sharing the set across all entries
  (initial walk + every later auto-instrument) is what bounds the
  recursion under truly cyclic graphs.
  `test_visited_ids_shared_across_setattr_reentry` pins this down.

Limitations the design intentionally accepts:
- Containers' CONTENTS (e.g., a list-of-user-objects) are not recursed
  into. Mutating `obj.items[0].name` doesn't fire even if `items` is the
  watched container's contents.
- Slotted classes (no `__dict__`) aren't iterated by `_safe_iter_dict_attrs`.
  Direct attr changes on them still fire (the class surgery catches
  `__setattr__`), but nested user objects under slotted attrs aren't
  auto-discovered.
- `@property` and descriptor-backed attrs aren't iterated.
- **Framework / stdlib / site-packages types are not recursed into.**
  Watching a Django QuerySet directly catches rebinds of the local
  variable (via local-variable detection if class surgery is also
  blocked by the metaclass) plus top-level setattr via classpatch;
  it does NOT instrument ORM internals. A user DTO whose attribute IS
  a Django QuerySet still gets full instrumentation on the DTO – we
  just stop walking when we reach the QuerySet. Filtering happens
  via `_is_user_defined_type` (denylist of module roots +
  site-packages / dist-packages file-path heuristic). See
  `_FRAMEWORK_MODULE_ROOTS` and the
  `test_is_user_defined_type_*` / `test_recursion_stops_at_framework_boundary`
  tests for what the contract covers.
- **Class objects are not recursed into.** `obj.held_class = SomeClass`
  treats `SomeClass` as opaque – no `__class__` swap on a class, no
  walk of its descriptor-laden `__dict__`. Pinned by
  `test_recursion_skips_class_objects`.

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

### 11. Sequential pre-emptive breakpoints – one IDE pause per mutation

When N back-to-back mutations fire before any of them pause (e.g.
Django `QuerySet._clone()` does `self._hints = ...` then
`self._query = ...` on consecutive lines of `query.py`), each hit
installs its OWN pydevd `LineBreakpoint` at a successive code line
via `_compute_bp_targets` + `_install_bp_at`. The IDE then pauses
N times – once per bp – and each `sessionPaused` calls
`_pycharm_consume_last_hit(pause_file, pause_line)` which drains
ONLY the hit whose bp fired THIS pause, leaving the rest queued
for their own future pauses.

**Why the old `_pause_pending` gate was removed (v8 change):**

The previous design had a process-level boolean `_pause_pending`
that suppressed hits 2..N when a pause was already armed. That was
fine for the Django `QuerySet._clone()` symptom (two yellow lines
on the same `sessionPaused`), but it MASKED the 4-mutation
auth-middleware case where the user explicitly wanted to see EACH
attribute change as a separate debugger pause. The gate silently
dropped 3 of 4 mutations with no diagnostic. The sequential-bps
approach gives N pauses for N mutations – the user steps through
each one individually.

**Sequential allocation mechanism:**

`_next_slot_for_code(code, start_line)` scans the existing hit
queue's `bp_locations` plus in-flight reservations (from
`_bp_slot_reservations`) to find which lines in the given code
object already have bps armed. It searches after
`max(max(used_lines), start_line)` – never backward past the
current mutation line – and returns the next `co_lines()` line
strictly after that. This is what spreads N hits across lines
80, 81, 82, 84 (skipping blank lines) in the same code object.
The `max(..., start_line)` guard (v21 fix) prevents a previous
hit's bp at e.g. line 287 from pulling the search backward when
the current mutation is at line 288 – without it, `_next_code_line_in`
would return 288 itself (the mutation line, already executing).

`_compute_bp_targets(user_frame, source_line)` builds up to TWO slots per hit:
- **Primary**: next future code line in `user_frame.f_code` (the
  mutation site). When `source_line == user_frame.f_lineno`, the runtime
  first tries `_next_code_line_after_frame(user_frame)`, which uses
  `frame.f_lasti` and `dis.get_instructions(...)` to find the next LINE
  event after the current bytecode offset. This is critical for multi-line
  statements: in `request.parsed = ParsedRequest(...)`, `STORE_ATTR`
  still reports line 105, but RHS lines 106-112 already executed before
  the attribute write. The correct primary bp is line 114 (`return
  request`), not line 106.
- **Safety**: next code line in the walked-up user-code frame via
  `_find_user_code_caller`. Fires reliably even when pydevd's
  per-code monitoring doesn't arm LINE for the primary's library
  frame.

When primary is exhausted (mutation on the function's last code
line), we walk `f_back` through caller frames until we find one
with a valid next code line – preferring contextually close frames
over the distant safety net.

**Selective drain in `_pycharm_consume_last_hit(pause_file, pause_line)`:**

A hit matches when ANY of its `bp_locations` tuples has the same
`(file, line)` as the IDE's current pause. Hits with empty
`bp_locations` (the `do_wait_suspend` fallback path) match ANY
pause location so they don't leak. On drain, ALL bps belonging
to matched hits are removed (including the unfired sibling bp)
so they don't surface as phantom pauses later.

The 256-element cap on `_hit_queue` is kept as belt-and-suspenders
– in normal operation the queue stays small (N hits install N bps;
each fires within one or two LINE events).

Test: `test_back_to_back_hits_install_sequential_bps` (core
sequential-allocation behavior: N mutations → N distinct bp
locations, each drainable independently). The openapi multiline
assignment case is pinned by
`test_handle_hit_uses_bytecode_next_line_for_multiline_attr_assignment`.

### 12. Pause anchor walk-up (mutation in library code)

The user_frame that `_handle_hit` receives is the IMMEDIATE caller
of the watcher's `__setattr__` – produced by `_find_user_caller`
which only walks past `<string>` runtime frames. When the watched
mutation happens inside framework code (Django's `QuerySet._clone`
does `self._hints = {}` then `self._query = sql.Query(...)`;
SQLAlchemy session flush; pydantic model rebuild; etc.) that
frame is in `site-packages` – and PyCharm's "do not step into
library code" filter (on by default) causes pydevd's
`CMD_STEP_OVER + step_stop = library_frame` to be silently
skipped. The mutation fires, the IDE highlights the line, but
the debugger never actually pauses. The cascade up via PY_RETURN
dies in further library frames before reaching user code.

`_handle_hit` works around this by computing a separate
**pause anchor**: walk further up the stack past site-packages
frames via `_find_user_code_caller` to find the user's own code,
and anchor `_pause_via_pydevd` on THAT frame. The library
mutation is still recorded as the SOURCE (the `source_file` /
`source_line` parameters carry the actual mutation site, so the
highlight renders e.g. on `query.py:289`), but the debugger
suspends at the user's code that called into the library (e.g.
`users/models.py:956` where they wrote `self.groups.all()`).

The split:
- **Source line** (highlight + stderr log) → actual mutation
  site, from `user_caller` at the watcher's call site.
- **Pause anchor** (pydevd `step_stop`) → nearest user-code
  frame walking up `f_back` from the mutation site.

If the entire chain is library / runtime (no user code in 32
hops), the hit is **dropped silently** before the queue or the
dedupe gate. A phantom highlight with no debugger pause behind
it is worse UX than no signal at all – the user would click
around confused why nothing stopped.

`_is_library_filename` matches `site-packages` / `dist-packages`
(third-party) AND files under the stdlib install directory
(computed once at module load via `os.path.dirname(os.__file__)`,
stored in `_STDLIB_DIR_PREFIX`). Both categories are filtered
because pydevd's "do not step into library code" filter treats
them identically – anchoring on either silently swallows the
pause. The deepcopy case (`copy.deepcopy(qs)` →
`copy.py:143` mutation) was the original report that proved this:
the walk-up MUST skip stdlib too.

Guarantees to preserve:
- **Source line stays at the mutation site.** The user wants to
  see "Django changed `_hints` at `query.py:289`" via the
  highlight + stderr log. Only the pydevd anchor moves.
- **Drop-on-pure-library is silent + leaves the gate cleared.**
  Setting the gate on a dropped hit would silently swallow
  future legitimate user-code hits. The drop must be invisible.
- **Stdlib IS library** for pause-anchor purposes. Anything
  under the Python install root (stdlib OR third-party) goes
  through pydevd's library filter; anchoring there is broken.

Tests:
- `test_find_user_code_caller_walks_past_site_packages`
- `test_find_user_code_caller_returns_none_for_pure_library_chain`
- `test_find_user_code_caller_walks_past_stdlib`
- `test_find_user_code_caller_handles_deepcopy_through_django_chain`
- `test_handle_hit_anchors_pause_on_user_code_when_mutation_is_in_library`
- `test_handle_hit_drops_when_chain_is_entirely_library`

### 13. Pause mechanism – pydevd `LineBreakpoint`, not `CMD_STEP_OVER`

**Originally** `_handle_hit` armed `info.pydev_step_cmd = CMD_STEP_OVER`
with `step_stop = user_frame`, mirroring `pydevd.settrace(stop_at_frame=...)`'s
internal mechanism. This is documented in "The pydevd pause" section
below and is still implemented as `_pause_via_pydevd` for reference /
fallback. **It was unreliable in PEP 669 mode for arbitrary deep user
frames.** Root cause: pydevd's `py_start_callback` decides LINE-tracing
per-code-object at the function's FIRST entry. For functions that had
no breakpoints at that moment, LINE tracing is never enabled. Our later
`set_local_events(LINE | PY_RETURN)` armed the events at the C level
but pydevd's own callback-dispatch bookkeeping ignored them – there's
no way to retroactively run `py_start_callback` for a function that's
already mid-execution. `restart_events()` re-fires events that ARE
armed; it doesn't fabricate fresh PY_STARTs.

**Now** the primary pause mechanism in `_handle_hit` is
`_compute_bp_targets(...)` + `_install_bp_at(...)`: compute one or more
future bp locations, then install pydevd `LineBreakpoint`s there.
Pydevd's breakpoint engine is the most heavily exercised code path in
pydevd – when the bp's line is reached, the pause fires reliably.

Pipeline:

1. **Find the primary line in execution order, not just source order.**
   For attribute/classpatch hits where `source_line == user_frame.f_lineno`,
   `_next_slot_after_frame(user_frame)` calls
   `_next_code_line_after_frame(frame)`, which uses `frame.f_lasti` to
   scan bytecode instructions after the current offset and pick the next
   future LINE event. This fixes the openapi shape:

   ```python
   request.parsed = ParsedRequest(
       body=result.body,
       ...
   )

   return request
   ```

   `STORE_ATTR` reports line 105, but argument lines 106-112 have already
   executed. Numeric lookup (`_next_code_line_in(code, 105)`) would pick
   106, which never fires. Bytecode-order lookup picks line 114.

   **Backward-line rejection (v21 fix):** `_next_code_line_after_frame`
   also requires `line > current_line`. CPython's `RETURN_CONST` at the
   end of a function can be tagged with an earlier source line (e.g. the
   closing bracket of a multi-line expression on line 287 when the
   mutation is at line 288). Without this check, the function returns 287
   -- a line that already executed -- and the bp never fires. With the
   check, it returns None, falling through to the loop-back check or
   the f_back walk.

   **Loop-back bp target (v24 fix):** When no forward code line exists
   but a `JUMP_BACKWARD` instruction is present after `f_lasti`, the
   function resolves the jump's target offset to a source line via
   `_offset_to_line(code, inst.argval)` and returns it as a loop-back
   candidate. This handles tight loops like `for i in range(N):
   setattr(...)` where the for-header IS the next execution point (the
   bytecode loops back). Without this, the primary slot always exhausted
   for tight-loop shapes, forcing the f_back walk to consume (and
   quickly exhaust) caller-frame lines. Forward lines still take
   priority; loop-back is the fallback. The `JUMP_BACKWARD` instruction
   itself has no `starts_line` attribute on any CPython version, so the
   target line MUST be resolved via `co_lines()` -- do not try to read
   `_instruction_starts_line(inst)` on the jump instruction.

   **Exception-handler-line skip (v20 fix):** `_next_code_line_after_frame`
   also skips handler-entry lines (the `except ...:` clause). When the
   mutation is the last normal statement in a try body, bytecode order
   points at the handler next, but that line is unreachable on the
   no-exception path. `_get_except_handler_lines(code)` identifies them.

2. **Fall back to `_next_code_line_in(code, after_line)` for ordinary
   numeric lookup.** It uses `code.co_lines()` (Python 3.10+) and only
   returns actual code lines. Blank lines, lines past the last statement,
   and lines between statements are excluded. The `set_accessible_products`
   case motivated this: the function's last statement was on line 195,
   `f_lineno + 1` = 196 is blank, and a bp at 196 never fires.

3. **Install primary + safety candidates.** The primary is in the mutation
   frame. If the primary is exhausted (no forward line AND no loop-back),
   `_compute_bp_targets` walks `f_back` to find the nearest caller frame
   (skipping only runtime frames) with a valid next line. The safety
   candidate is the nearest walked-up user-code frame from
   `_find_user_code_caller`. Whichever candidate fires first wins; sibling
   bps are removed during `_on_line`/consume cleanup.

4. **`py_db.consolidate_breakpoints(file, id_to_bp, py_db.breakpoints)`**
   is pydevd's standard breakpoint-install API. It rebuilds the line-to-bp
   map and (in PEP 669 mode) calls `restart_events()`. That's not
   sufficient on its own (see step 5) but it's required for pydevd's
   `py_line_callback` to recognize the bp when it eventually fires.

5. **`sys.monitoring.set_local_events(DEBUGGER_ID, target_code, existing | LINE | PY_RETURN)`**
   on `target_code` – the code object that contains the bp's line.
   This is the critical addition: it FORCES events armed on the code
   object for pydevd's tool, regardless of what `py_start_callback`
   previously decided. Without this step, the bp lives in pydevd's
   table but pydevd's `py_line_callback` never fires for the current
   invocation (because LINE was disabled for the code object at the
   function's start) and the bp can't be checked.

6. **Track installed bps in `WatchpointRegistry._temp_breakpoints`**
   and remove them in `_pycharm_consume_last_hit` (drains every
   sessionPaused, watchpoint or not). The bp_id is negative so it
   won't collide with pydevd's positive IDE-assigned IDs.

**Last-resort fallback: `_pause_via_do_wait_suspend`.** When the user's
mutation is at the last statement of a module/function AND f_back is
not user code (e.g., `script.py` last-line case, or a script run via
`python script.py` where the only user frame IS the module), neither
candidate yields a valid line. `_compute_bp_targets` returns empty.
Falling back to a do_wait_suspend on the pause anchor puts
`urllib.parse.quote` on the user thread's stack at pause time (rule-1
trade-off in "The pydevd pause" §) but guarantees the IDE pauses. The
user can click their own frame in the Frames panel to see their code;
the highlight on the actual mutation line still renders. Silent no-pause
on a deliberate `watch(...)` is worse UX than ugly-stack pause.

Guarantees to preserve:

- **Source line stays at the mutation site.** The IDE highlighter
  uses the `source_file`/`source_line` arguments to `_handle_hit`, NOT
  the bp's line. So the yellow highlight renders on e.g.
  `user_hotel_relationship.py:195` (the actual mutation) even though
  the IDE pauses at `features_calculation.py:594` (the bp).
- **`set_local_events` on `target_code` is load-bearing.** Without it,
  `consolidate_breakpoints` + `restart_events` is not enough: pydevd's
  `py_line_callback` won't fire for code objects whose LINE tracing
  was disabled at their first PY_START. We have to override that
  decision at the kernel level.
- **`_next_code_line_in` MUST use `code.co_lines()`**, not arithmetic
  (`f_lineno + 1`). Blank/synthetic lines have no LINE events, so a
  bp installed there sits inert and the pause is silently dropped.
- **For real frames at the mutating line, use `f_lasti` first.**
  Source order can point backward inside already-executed multi-line
  statements. `_next_code_line_after_frame` is what keeps
  `openapi_validate_request` from arming an impossible bp at line 106.
  It also rejects backward-pointing lines (`line > current_line` guard)
  and exception-handler lines.
- **Cleanup runs on EVERY sessionPaused.** Not just watchpoint pauses –
  any pause (manual breakpoints, manual pause button, exception
  breakpoints, ...). This ensures bp leaks self-heal: if a session
  ends abnormally and no `_pycharm_consume_last_hit` runs, the bps
  die with the process (pydevd's state is per-process).
- **Negative bp_ids prevent collision with pydevd's IDE bps.** The
  IDE assigns positive integer IDs. Hashing `(watch_name, file, line)`
  to a negative ID gives us a unique handle without polluting that
  range.

Tests:
- `test_next_code_line_finds_actual_code_line_skipping_blanks` – pins
  the blank-line case via `co_lines()`-based skip.
- `test_handle_hit_uses_bytecode_next_line_for_multiline_attr_assignment`
  – pins the `request.parsed = ParsedRequest(...)` case where numeric
  source order points into already-executed RHS lines.
- `test_next_code_line_after_frame_skips_handler_only_lines` – pins the
  exception-handler-line skip.
- `test_next_code_line_after_frame_rejects_backward_line` – pins the
  backward-pointing RETURN_CONST line rejection (v21 fix for
  `_authorization` line 288 → line 287 regression).
- `test_next_slot_for_code_never_returns_mutation_line_or_earlier` – pins
  the `max(used_lines, start_line)` guard (v21 fix).

### 14. Magic-number caps moved to named constants (all 1024)

Six inline literals that capped runaway scenarios were extracted to
three named constants and bumped to 1024 each:

- `_MAX_HIT_QUEUE_SIZE` (was `256` in `_handle_hit`) – cap on
  `WatchpointRegistry._hit_queue` before dropping oldest entries.
  With the dedupe gate this is normally 0-or-1, so the cap only
  matters for test paths or pathological bypass scenarios.
- `_MAX_PROPAGATION_QUEUE_SIZE` (was `128` in `_on_call`) – per-thread
  cross-function propagation queue cap. CALL events queue here
  waiting for the matching PY_START to consume.
- `_MAX_FRAME_WALK_HOPS` (was `32` in `_find_user_caller`,
  `_find_user_code_caller`, and `_pause_via_pydevd`'s disarm loop) –
  max f_back hops before giving up. 1024 matches Python's default
  recursion limit – above that you'd hit `RecursionError` from the
  interpreter before us.

All three are "should never hit in practice; if you do, something is
genuinely pathological" limits. Pulled into named constants so a
single edit covers all sites if a future use case exceeds them.

### 15. `_wp_container_repr` must be safe against repr-raising values

The container wrappers (`_WatchedList`, `_WatchedDict`, `_WatchedSet`)
snapshot before-and-after reprs in every mutating method to detect
change. The repr path goes through `_wp_container_repr(self)`, which
delegates to the BASE type's `__repr__` (`list.__repr__` / etc.) and
that recursively reprs every contained value.

If any contained value's `__repr__` raises, the whole snapshot raises,
and the exception propagates out of our `__setitem__` / `append` / etc.
into the user's code that triggered the mutation. **This kills user
code that was working fine before we wrapped its container.**

The user-reported scenario: watch `self` on a Django `TestCase`. The
recursive walker wraps `self._testdata_memo` (a plain dict) as
`_WatchedDict`. Django then passes that dict as `memo` to
`copy.deepcopy(...)`. Deepcopy's `memo[id(x)] = y` flow calls our
`__setitem__` while `y` is a half-reconstructed Django Model whose
`__repr__` accesses `_state` (not yet restored) → AttributeError →
test fails through no fault of the user.

Fix: `_wp_container_repr` wraps the entire repr path in try/except
and returns `"<unreprable>"` on any exception. Same-string before-
and-after means no hit fires, but the underlying mutation succeeds.

**Guarantees to preserve**:

- **ALL `dict.__repr__(self)` / `list.__repr__(self)` / `set.__repr__(self)`
  calls inside the wrappers MUST go through `_wp_container_repr(self)`.**
  Direct calls to the base type's `__repr__` bypass the try/except
  and re-introduce the bug. There are 33 such call sites (every
  mutating method on every wrapper class) – all converted.
- **The `repr(value)` fallback at the end of `_wp_container_repr` also
  needs the try/except.** Some custom objects override `__repr__` and
  raise.
- **Trade-off**: when contained values can't be repr'd, we can't
  detect changes via repr-diff. Those mutations are silent. In
  practice this only happens for mid-construction objects (deepcopy,
  pickle.load, `__setstate__`) which aren't typically the mutations
  the user cares about.

Tests:
- `test_watched_dict_setitem_swallows_value_repr_errors` (the Django
  TestCase scenario)
- `test_watched_list_mutations_swallow_value_repr_errors` (symmetric)
- `test_watched_set_mutations_swallow_value_repr_errors` (symmetric)

### 16. Module-load fingerprint + file-based diagnostic log

Two debugging affordances added during a long debugging session and
kept because they're cheap and load-bearing for bug reports:

**`_RUNTIME_VERSION`** is a string at module load that gets logged
via `_log_warn` once on import. When a user reports "I rebuilt the
plugin and it still doesn't work," the first thing to check is
whether the version stamp in their log matches the latest code.
This distinguishes "fix didn't help" from "you're running a stale
build." Bump the version string on every meaningful behavioral
change to the runtime.

**`_log_warn` tees to `/tmp/pythonwatchpoint.log`.** Under pytest's
default capture mode, stderr is hidden – `[WATCHPOINT] ...` lines
never reach the user's terminal or Debug Console. Pydevd's
stdout/stderr interception can also rewrite or drop lines. The
file sink at a fixed path (NOT env-driven – one less moving part
for users to set up) is the durable log the user can `tail -f`
during a session.

File-size guard: when the file grows past 2 MB, we truncate to the
last 1 MB on next write. Bounds growth over long sessions without
losing recent history.

Both are surfaced unconditionally; there's no production-vs-debug
toggle. The cost is one syscall + one timestamp formatting per
log line, which is negligible compared to anything else the
runtime does.

### 17. Direct-pause dispatch via `_bp_pause_pending` (v13)

Belt-and-suspenders mechanism for the `_install_pause_breakpoint` path.
After installing a `LineBreakpoint` via pydevd's `consolidate_breakpoints`
and force-arming `LINE | PY_RETURN` on the target code object with
`sys.monitoring.set_local_events(DEBUGGER_ID, ...)`, the runtime ALSO
registers the `(id(target_code), line)` key in
`WatchpointRegistry._bp_pause_pending` and arms the same events under
our OWN `_TOOL_ID`'s monitoring slot.

Why: in PEP 669 mode, pydevd's `py_start_callback` can return `DISABLE`
for code objects that had no breakpoints at their first entry. Even
though we later arm events with `set_local_events(DEBUGGER_ID, ...)`,
pydevd's internal `py_line_callback` may not fire for the current
invocation – the `DISABLE` decision from the first PY_START predates
our bp install. Our OWN tool's `_on_line` fires independently (it has
no prior `DISABLE` history for any code object) and checks
`_bp_pause_pending` on every LINE event. When the key matches, it pops
the entry and calls `_trigger_direct_pause` – which does
`py_db.do_wait_suspend(...)` directly (rule-1 trade-off accepted here
because the alternative is silent no-pause). The IDE pauses immediately.

Pipeline:
1. `_install_bp_at` registers `(id(target_code), line)` in
   `_bp_pause_pending` after `consolidate_breakpoints` succeeds.
2. `_on_line` checks `(id(code), line_number)` at the top of every
   LINE callback, before the normal local-watch diff logic.
3. On match: pop the entry (prevents double-fire on re-entrant calls),
   call `_trigger_direct_pause(code, line_number)`.
4. `_trigger_direct_pause` locates `py_db`, finds the current thread's
   frame at the code+line location, and calls `do_wait_suspend`.
5. Cleanup: `_pycharm_consume_last_hit` clears `_bp_pause_pending`
   entries for drained hits. `clear_watches()` clears all entries.

Guarantees to preserve:
- **Pop before dispatch.** If `_trigger_direct_pause` itself triggers
  a LINE event on the same code (e.g. via pydevd's protocol code),
  the entry is already gone – no infinite loop.
- **Best-effort.** `_trigger_direct_pause` catches all exceptions
  gracefully (pydevd not loaded, thread gone, etc.). The primary bp
  path through pydevd's own `py_line_callback` is still the intended
  path; this is the backup.
- **Cleanup on drain.** Stale entries in `_bp_pause_pending` that
  were never reached (the primary pydevd path fired first, or the
  code line was never executed) are cleared when the hit is consumed
  or when `clear_watches()` runs.

Tests:
- `test_install_bp_registers_in_bp_pause_pending`
- `test_on_line_dispatches_direct_pause_for_pending_bp`
- `test_on_line_ignores_non_pending_lines`
- `test_consume_clears_bp_pause_pending_for_drained_hits`

### 18. Installation side-effect suppression (`_installing_watch_thread`) (v14)

When `watch("obj")` is called, `_instrument_object_tree` walks the
object's `__dict__` calling `getattr(obj, attr_name)` for each entry
and installing class surgery / container wraps on nested values. Some
objects have lazy-evaluation semantics: reading an attribute triggers a
side-effect write to another attribute (Django's `SimpleLazyObject`,
ORMs with lazy-loading descriptors, cached properties that populate
backing fields). These side-effect writes go through our freshly-
installed `__setattr__` hook and would fire a spurious `WatchpointHit`
during the installation – confusing because the user hasn't mutated
anything yet.

**Fix:** a module-level `_installing_watch_thread: Optional[int]` flag
is set to `threading.get_ident()` around the `_registry.add_watch()`
call in both `watch()` and `watch_at()`. `_handle_hit` checks this
flag at the top: if the current thread's ident matches, the hit is
silently dropped (early return before queueing or pausing).

Key design decisions:
- **Thread-scoped, not a simple bool.** The IDE's evaluator thread
  calls `watch_at()` while user threads may be running. A simple
  `True/False` flag would suppress legitimate user-thread mutations
  that happen concurrently with installation. The thread-ident
  comparison ensures only the installing thread's side-effects are
  suppressed.
- **`finally`-guarded clearing.** Both `watch()` and `watch_at()`
  wrap `add_watch()` in `try/finally` with
  `_installing_watch_thread = None` in the `finally`. This prevents
  the flag from leaking if `add_watch` raises (e.g. slotted class,
  frozen dataclass). A leaked flag would permanently suppress all
  future hits on that thread.
- **Baseline is NOT discarded by suppression.** When the
  `__setattr__` hook fires during installation, the assignment still
  executes (via `super().__setattr__`). The object's actual state
  reflects the side-effect. The `_value_hash` baseline captured at
  arm-time is whatever the object holds POST-installation, so a
  subsequent real assignment of the SAME value is correctly silent
  (the baseline matches current), and a genuinely different value
  fires.
- **Complements, does not replace, the `_guard` mechanism.** The
  per-thread `_guard.active` flag handles re-entrancy from WITHIN
  the watcher's own `__setattr__` (e.g. our container-wrap setattr
  during `_instrument_object_tree`). `_installing_watch_thread`
  handles the broader case: side-effect writes that happen OUTSIDE
  the guard window but still during the installation flow (e.g. a
  property getter that writes a backing attribute before the guard
  is set for that particular iteration step).

Tests:
- `test_handle_hit_suppressed_during_installation` – SimpleLazyObject
  scenario
- `test_installing_watch_flag_cleared_on_exception` – flag doesn't
  leak on slotted/failing objects
- `test_installation_suppression_thread_scoped` – other threads still
  fire during installation
- `test_installation_suppression_same_thread_does_suppress` – same
  thread is silent
- `test_multiple_watches_in_sequence_each_suppresses_independently` –
  sequential watch() calls each work correctly
- `test_suppression_does_not_discard_baseline` – same-value check
  uses post-installation state as baseline
- `test_suppression_only_active_during_installation_window` – flag
  cleared immediately after watch() returns

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

### PEP 669 supplement – force-arm pydevd's monitoring on user_frame + caller

The scoped-step-over flow above relies on `set_trace_for_frame_and_parents`
to arm `f_trace` on `user_frame` and its callers so pydevd's tracer fires
on their next line / return events. Under sys.settrace that's enough.
**Under sys.monitoring (PEP 669) it isn't.** pydevd's tracer is only
dispatched when an event is enabled on the relevant code object's
`set_local_events` mask for `DEBUGGER_ID` (tool 0), and
`set_trace_for_frame_and_parents` has several silent early-return paths
where it doesn't touch the monitoring mask at all (PEP 669 mode, filtered
files, certain pydevd builds).

The user-visible bug this caused: when the watched mutation is the **last
statement of a helper function**, e.g. `order.status = "paid"` as the only
line of `charge_card` in the test_demo_b demo. After our `__setattr__`
returns, `charge_card` has no more lines to fire a LINE event on, and
without `PY_RETURN` enabled on its code object pydevd never learns it
returned. The next watch hit overwrites `step_stop` and the pause
materialises only for the LAST of N back-to-back hits ("stops 2 times for
3 mutations"). Adding a `pass` after the assignment "fixes" it because
that creates the follow-up LINE event pydevd was waiting for.

The supplement, after the f_trace plumbing, calls `sys.monitoring.set_local_events`
on `DEBUGGER_ID` (= 0) for **two** code objects with **two** events:

```python
debugger_tool_id = sys.monitoring.DEBUGGER_ID  # 0
wanted = sys.monitoring.events.LINE | sys.monitoring.events.PY_RETURN

# user_frame.f_code: LINE for "next line in this function" stepping;
# PY_RETURN for the "no follow-up line, function just returned" case.
existing = sys.monitoring.get_local_events(debugger_tool_id, user_frame.f_code)
sys.monitoring.set_local_events(
    debugger_tool_id, user_frame.f_code, existing | wanted,
)

# user_frame.f_back.f_code: same events. After PY_RETURN completes the
# step-over, the pause needs a landing site – the caller's next LINE event.
# Without LINE armed there, pydevd's tracer never fires when the caller
# resumes after the helper returns, and the pause is silently dropped.
f_back = user_frame.f_back
if f_back is not None:
    existing_back = sys.monitoring.get_local_events(debugger_tool_id, f_back.f_code)
    sys.monitoring.set_local_events(
        debugger_tool_id, f_back.f_code, existing_back | wanted,
    )
```

Why OR with the existing mask: pydevd may already have events armed on
these code objects (e.g. a breakpoint in user_frame's caller). Overwriting
with just our two would silently clear pydevd's own events.

Why include `PY_RETURN` on `f_back.f_code` too: symmetry. If the caller is
ALSO the last-line-of-a-helper case (chained one-liner helpers calling
each other), pydevd needs to see the caller's unwind too. The bit is cheap
and covers a class of edge cases without needing to walk the whole stack.

Why we don't walk deeper than `f_back`: the assumption is that at least
ONE frame between `user_frame` and the top of the user's stack has a
line after the watched call – the typical pattern is a multi-line caller
(loop, sequence of calls) that's interested in inspecting state across
several mutation sites. If the user's *entire* call stack is one-liner
helpers all the way to the top, the pause still happens; it just lands
in whichever frame finally has a follow-up LINE event. Walking the whole
stack would arm events on dozens of unrelated code objects for marginal
benefit.

Tool ID gotcha: if pydevd hasn't claimed `DEBUGGER_ID` (older Python,
sys.settrace mode), `set_local_events(0, ...)` raises `ValueError`. The
supplement catches all exceptions silently because in that scenario the
scoped-step-over above is enough on its own – the supplement is a strict
addition for the PEP 669 mode case.

Regression test:
`test_pause_via_pydevd_enables_line_and_py_return_on_user_and_caller_frames`
claims `DEBUGGER_ID` itself (impersonating pydevd), calls `_pause_via_pydevd`
through a real call chain, and asserts both code objects have both events
enabled afterwards.

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
quirks. When multiple candidates match (recursion), it picks the
**innermost** (deepest) frame – this matches PyCharm's typical "currently
executing frame" focus. Tests: `test_watch_at_locates_paused_frame_in_other_thread`,
`test_watch_at_prefers_innermost_recursive_frame`.

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
| `WatchpointRegistry._instrument_object_tree` | Recurses `obj.__dict__` to depth `_RECURSIVE_OBJECT_WATCH_DEPTH = 4`. Cycle-guarded by `root_watch.visited_ids` (persistent across the initial walk AND every later `__setattr__`-triggered re-entry). Filters via `_is_user_defined_type` (skip framework / stdlib / site-packages types) and `isinstance(value, type)` (skip class objects). Records each nested sub-instrumentation in `root_watch.sub_watches` via `_try_add_sub_watch` (breadth-cap-respecting). |
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
| `_MAX_SUB_WATCHES_PER_ROOT`       | Breadth cap (100). `_try_add_sub_watch` flips `root_watch.sub_watches_capped` and logs a one-time warning when reached. Belt-and-suspenders against cyclic graphs the type filter + visited set miss. |
| `_FRAMEWORK_MODULE_ROOTS`         | Frozenset of top-level module names (`django`, `sqlalchemy`, `pydevd`, `_pydevd_bundle`, `numpy`, `pandas`, `pydantic`, `werkzeug`, `flask`, `pytest`, `setuptools`, `_pycharm_watchpoint`, `watchpoint`, ...) whose types are framework code. Consulted by `_is_user_defined_type`. Both `_pycharm_watchpoint` (IDE runtime module name) and `watchpoint` (test-mode file import) are listed. |
| `_is_user_defined_type`           | Returns True iff `t` is a type from user code – rejects `builtins`, `sys.stdlib_module_names` roots, `_FRAMEWORK_MODULE_ROOTS` roots, and any module whose `__file__` lives under `site-packages` / `dist-packages`. Used to gate recursive instrumentation; the root object the user passes is unaffected (it goes through `add_watch`'s own dispatch). |
| `_try_add_sub_watch`              | Appends a sub-watch unless the per-root cap is reached. First time the cap engages on a given root, logs one warning naming the root expression. |
| `_RUNTIME_FILENAMES`              | Frozenset of filenames whose frames are runtime code (`"<string>"` for the sitecustomize-exec'd module; this file's `__file__` for tests). `_find_user_caller` walks past frames matching either to find the real caller, so hits report the user's mutation line instead of `super().__setattr__` inside `_WatchedAnyAttrSubclass`. |
| `_find_user_caller`               | Walks `f_back` from a starting frame past `_RUNTIME_FILENAMES` until it finds a user frame, or returns None after 32 hops. Callers that get None must drop the hit – the mutation didn't originate from user code (e.g. descriptor side-effect during pydevd's variable-display call). |
| `_safe_iter_dict_attrs`           | `obj.__dict__` iteration helper for `_instrument_object_tree`. Skips dunder names and silently no-ops on slotted classes. |
| `_handle_hit`                     | Three gates at the top: (1) `_installing_watch_thread` suppression – early-return if the hit is on the installing thread (side-effect from tree walk, see §18); (2) `_find_user_code_caller` existence check – drop silently if no user code anywhere in the chain; (3) compute bp targets via `_compute_bp_targets(user_frame)` – up to two slots (primary at mutation site + safety at walked-up user code), then install each via `_install_bp_at`. Falls back to `_pause_via_do_wait_suspend` only when ALL bp installs fail AND the queue was empty. `source_file/line` always points at the actual mutation site so the highlighter renders on the right line regardless of where the pause lands. Each hit gets its OWN bp at a successive code line (see §11). |
| `_installing_watch_thread`        | Module-level `Optional[int]` – set to `threading.get_ident()` around `_registry.add_watch()` in `watch()` and `watch_at()`. Checked at the top of `_handle_hit`: if matching, the hit is silently dropped (side-effect from our own tree walk). Cleared in `finally` to prevent leaks. Thread-scoped so real mutations on other threads still fire during installation. See design contract §18. |
| `WatchpointRegistry._bp_pause_pending` | `dict[(id(code), line): True]` – keyed by code-object identity + line number. Populated by `_install_bp_at` after `consolidate_breakpoints`. Checked at the top of `_on_line`: on match, the entry is popped and `_trigger_direct_pause` fires `do_wait_suspend`. Cleared per-hit in `_pycharm_consume_last_hit` and wholesale in `clear_watches()`. See design contract §17. |
| `_trigger_direct_pause`           | Belt-and-suspenders pause: called from our `_on_line` when a `_bp_pause_pending` entry matches. Finds the current thread's frame at the target code+line and calls `py_db.do_wait_suspend(...)`. Best-effort: catches all exceptions. See design contract §17. |
| `WatchpointRegistry._temp_breakpoints` | List of `(file, line, bp_id)` tuples for `LineBreakpoint`s installed by `_install_pause_breakpoint`. Removed in bulk in `_pycharm_consume_last_hit` (every sessionPaused, watchpoint or not). Negative bp_ids prevent collision with pydevd's IDE-assigned positive IDs. See design contract §13. |
| `_is_library_filename`            | Path check on `co_filename`: matches `site-packages` / `dist-packages` (third-party) AND any path under `_STDLIB_DIR_PREFIX` (computed once from `os.__file__`'s directory at module load). Used by `_find_user_code_caller` to filter both library categories – pydevd's "do not step into library code" filter treats them identically, so anchoring on EITHER produces the same silent-pause-failure. **Override:** if a path falls under a directory listed in `PYCHARM_WATCHPOINT_USER_ROOTS` (env var, path-separator-delimited, set by the Kotlin launcher from `project.basePath`), it's treated as user code even if it's under `site-packages` – this supports editable-install workflows (`pip install -e .`). |
| `_find_user_code_caller`          | Walks `f_back` past runtime AND library (site-packages / dist-packages / stdlib) frames to find the nearest user-code frame. Used by `_handle_hit` to anchor the pause on user code even when the watched mutation happened inside library code. Returns None if no user code is in the chain – callers drop the hit. See design contract §12. |
| `_offset_to_line`                 | Maps a bytecode offset to its source line via `code.co_lines()`. Used by `_next_code_line_after_frame` for loop-back detection: `JUMP_BACKWARD` has no `starts_line` attribute, so the target line must be resolved from the jump's `argval` (absolute target offset). |
| `_next_code_line_in`              | `code.co_lines()`-based lookup for the smallest line strictly greater than `after_line`. Returns None if no later code line exists in the code object (the function's body ends at or before `after_line`). Critical for `_install_pause_breakpoint` to avoid landing the bp on a blank line / line past the function's last statement, where no LINE event fires. See design contract §13. |
| `_install_pause_breakpoint`       | Primary pause mechanism. Builds up to two candidates: user_frame's next code line, and f_back's next code line (if f_back is user code). For each, creates a `LineBreakpoint`, registers it via `py_db.consolidate_breakpoints`, AND forces `LINE | PY_RETURN` armed on the target code object via `sys.monitoring.set_local_events(DEBUGGER_ID, target_code, ...)`. Returns a list of `(file, line, bp_id)` tuples or empty list on failure. See design contract §13. |
| `_remove_temp_breakpoints`        | Removes `LineBreakpoint`s previously installed by `_install_pause_breakpoint`. Best-effort: logs but doesn't raise on failure (leaked bps die with the process; not a correctness issue). |
| `_pause_via_do_wait_suspend`      | Last-resort pause when `_install_pause_breakpoint` returns empty. Calls `py_db.set_suspend(thread, CMD_SET_BREAK)` + `py_db.do_wait_suspend(thread, frame, 'line', None)`. Blocks until the user resumes. Trade-off: `urllib.parse.quote` ends up on the user thread's stack (rule-1 from "The pydevd pause" §); the IDE shows urllib as topmost frame with the actual user frame below in the Frames panel. Used for the corner case where the mutation is at the last statement of a module/function AND there's no user-code caller above with a follow-up line. See design contract §13. |
| `_get_pydevd_debugger`            | Robust lookup, multiple fallbacks.            |
| `_pause_via_pydevd`               | LEGACY / FALLBACK: Scoped step-over: `CMD_STEP_OVER` + `step_stop = user_frame` + `state = RUN`, plus direct `user_frame.f_trace = trace_dispatch` and a disarm loop on our own `<string>` frames. Ends with the PEP 669 supplement that force-arms `LINE + PY_RETURN` on `user_frame.f_code` AND `user_frame.f_back.f_code` for pydevd's `DEBUGGER_ID` tool. Was the primary pause mechanism until PEP 669 mode proved it unreliable for arbitrary deep user frames (see design contract §13's rationale). Now kept as reference for a future pydevd version that might expose a public "pause at this deep frame" API. NOT called from `_handle_hit` anymore. |
| `_is_object_watchable`            | Heuristic for the auto-detection in `add_watch`. |
| `_value_hash`                     | Change-detection hash with type tag.          |
| `_setup_monitoring`               | Claims a tool ID, registers callbacks (LINE / PY_RETURN / PY_START / CALL). Guarded against re-import. |

## Things you might be tempted to do, but shouldn't

- **Re-introduce a `_drop_dead_frame_watches` sweep in `_on_line`.** It looks
  natural ("clean up watches with mismatched fid") but it deletes concurrent
  live frames' watches.
- **Re-introduce a global `_pause_pending` boolean gate ("dedupe back-to-back
  hits to one pause").** The gate was removed in v8 because it silently
  dropped hits 2..N. The 4-mutation auth-middleware case proved users want
  one pause per mutation, not one pause per "perceived event". The
  sequential-bps approach (`_compute_bp_targets` + `_next_slot_for_code`)
  gives N pauses for N mutations – each with its own bp at a distinct code
  line. If you see "two yellow lines simultaneously" again, the fix is in
  the Kotlin-side highlighter (it should render only the hit matching the
  current pause), NOT a gate that drops hits on the Python side.
- **Drop the pause-anchor walk-up (`_find_user_code_caller` in
  `_handle_hit`) ("the immediate `user_frame` is the right anchor").**
  Without the walk-up, watched mutations inside framework code
  (Django `QuerySet._clone()` setting `self._hints` / `self._query`,
  SQLAlchemy session flush, pydantic model build) silently fail to
  pause: PyCharm's "do not step into library code" filter causes
  pydevd's `CMD_STEP_OVER + step_stop = library_frame` to be
  short-circuited, and the cascade-via-PY_RETURN doesn't reach
  user code either. The user-visible symptom is "watchpoint hit
  fires (highlight + stderr log appear) but the debugger never
  actually stops." Tests:
  `test_handle_hit_anchors_pause_on_user_code_when_mutation_is_in_library`.
- **Remove the stdlib filter from `_is_library_filename` ("user code
  passing through stdlib helpers is still user code").** This was
  the initial design and it was wrong. Pydevd's "do not step into
  library code" filter treats stdlib the same as site-packages, so
  anchoring `CMD_STEP_OVER` on a stdlib frame produces the same
  silent-pause-failure as anchoring on a site-packages frame. The
  user-reported case that proved this: `copy.deepcopy(qs)` on a
  watched Django QuerySet. The watcher fires from
  `django/query.py:289` (site-packages, skipped fine), the next
  frame up is `copy.py:143` (stdlib), the walk-up landed there,
  and the debugger never paused because pydevd's filter rejected
  the stdlib anchor. Tests:
  `test_find_user_code_caller_walks_past_stdlib`,
  `test_find_user_code_caller_handles_deepcopy_through_django_chain`.
- **On `_find_user_code_caller` returning None, fall back to the
  immediate `user_frame` and pause there anyway.** It looks like the
  right "best effort" thing to do – "we tried our best, pause
  somewhere" – but it brings back the exact bug we just fixed:
  pause on a library frame, filter swallows it, phantom highlight
  with no real pause. Dropping silently is the correct behavior
  when there's no user code to anchor on. Test:
  `test_handle_hit_drops_when_chain_is_entirely_library`.
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
- **Drop `PY_RETURN` from the PEP 669 supplement, or skip the supplement's
  `f_back` arm.** Both are load-bearing for the last-line-of-helper case
  (the test_demo_b "stops 2 times for 3 mutations" symptom):
  `PY_RETURN` on `user_frame.f_code` is how pydevd learns the function
  returned when the watched mutation was the last statement; LINE on
  `user_frame.f_back.f_code` is the pause's landing site once that return
  fires. Dropping either re-introduces the silent-pause-drop bug for any
  helper whose final action is the watched mutation – which is the natural
  shape of demo / DTO setter / late-binding wiring code. Regression test:
  `test_pause_via_pydevd_enables_line_and_py_return_on_user_and_caller_frames`.
- **Drop the broad `except Exception:` around the PEP 669 supplement.** Under
  sys.settrace mode (older Python or pydevd builds without monitoring
  support), `sys.monitoring.use_tool_id` was never called for `DEBUGGER_ID`,
  so `get_local_events(0, ...)` raises `ValueError: tool ID is not in use`.
  The except is what keeps that path silent – the scoped-step-over above
  is enough on its own without the supplement.
- **Register `PY_UNWIND` globally for cleanup.** It works but kills the
  zero-overhead guarantee.
- **Switch the primary pause mechanism back to `_pause_via_pydevd`
  (`CMD_STEP_OVER + step_stop`).** It looks elegant – it's literally
  what `pydevd.settrace(stop_at_frame=...)` does internally. But it
  silently fails in PEP 669 mode whenever the pause anchor is a code
  object whose `py_start_callback` decided "no LINE tracing needed"
  at the function's FIRST entry (i.e., file had no breakpoints at
  that moment). `restart_events()` re-fires armed events; it doesn't
  fabricate a fresh PY_START to re-evaluate. The `LineBreakpoint`
  path in `_install_pause_breakpoint` works because pydevd's
  `py_line_callback` checks `py_db.breakpoints[file]` directly
  without depending on the per-function "should-trace" decision.
  Plus the `set_local_events` step we layered on top forces events
  armed at the kernel level so pydevd's callback actually gets
  invoked. Keep `_pause_via_pydevd` as a reference / future fallback
  if pydevd ever exposes a real public API for "pause at this deep
  frame," but don't use it as the primary path. See design contract §13.
- **Skip the `set_local_events(LINE | PY_RETURN)` call after
  `consolidate_breakpoints` in `_install_pause_breakpoint`.**
  `consolidate_breakpoints` clears the skip caches and calls
  `restart_events()` in PEP 669 mode, which LOOKS sufficient – but
  `restart_events` only re-fires events that are already armed.
  For a function that's currently mid-execution and was set up
  without LINE tracing on first entry, LINE events stay disabled.
  The bp lives in pydevd's table but `py_line_callback` never fires
  for the current invocation. `set_local_events` on the target code
  object is what forces LINE events armed at the kernel level,
  bypassing pydevd's internal "should we trace this code object"
  bookkeeping. See design contract §13.
- **Use `f_lineno + 1` instead of `_next_code_line_in(code, f_lineno)`
  for the bp install line.** Blank lines, lines past the last
  statement, and lines between statements have NO bytecode and no
  LINE events. A bp at such a line is inert: pydevd's
  `py_line_callback` is never invoked for it because no event ever
  fires. The user-reported `set_accessible_products` case is exactly
  this: the function ends on line 195, line 196 is blank, bp at 196
  silently never fires, IDE never pauses. `code.co_lines()` returns
  only ACTUAL code lines – use it. See design contract §13 and the
  `test_next_code_line_finds_actual_code_line_skipping_blanks` test.
- **Remove the `_pause_via_do_wait_suspend` last-resort fallback
  ("we should never use do_wait_suspend, rule 1 said so").** Rule 1
  is the right default. But when `_install_pause_breakpoint` returns
  empty AND there's no user-code caller above with a next code line
  (the `script.py` last-line-of-module case), the only alternative
  is silent no-pause on a deliberate `watch(...)`. The user explicitly
  asked the debugger to break on the change – honoring that with an
  ugly stack (urllib on top, user frame at the bottom of the call
  stack panel) is better UX than ignoring the request. The fallback
  fires only in this corner case; the clean bp path covers
  everything else.
- **Drop the bare-`__repr__` calls from `_wp_container_repr`'s
  explicit-type paths (the `list.__repr__(value)` / `dict.__repr__(value)` /
  `set.__repr__(value)` branches before the generic fallback).**
  The fallback `repr(value)` is already wrapped in try/except; the
  explicit-type branches MUST also be inside that try/except. If
  any contained value's `__repr__` raises, `dict.__repr__(self)`
  propagates that exception out of our `__setitem__`, out of the
  user's `deepcopy` / `pickle` / etc., killing their code. The
  Django TestCase + `_testdata_memo` user-reported case is exactly
  this. See design contract §15.
- **Snapshot the container with `dict.__repr__(self)` (or
  `list.__repr__(self)`, `set.__repr__(self)`) directly inside the
  mutating methods, bypassing `_wp_container_repr`.** Same trap as
  above – any contained value with a raising `__repr__` (half-built
  Django Model, custom `__repr__` that touches uninitialized state)
  blows up `__setitem__`. ALL 33 mutating-method snapshots go
  through `_wp_container_repr(self)` for the try/except guard.
- **Use `weakref` on a frame.** Frames are not weakly referenceable in CPython
  (confirmed empirically on 3.12/3.13/3.14).
- **Hold `self._lock` while calling `_handle_hit`.** Pause would freeze every
  other thread's `watch()` calls.
- **Remove the raise fallback in `_handle_hit`.** The test suite depends
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
  classes. 4 is a pragmatic default chosen for the same reason.
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
- **Drop the `_is_user_defined_type` filter in `_instrument_object_tree`
  / `_WatchedAnyAttrSubclass.__setattr__`'s re-entry path.** Without it,
  watching a user DTO that references a Django QuerySet recurses into
  the QuerySet, then into its `.model` (a Django Model CLASS, not
  instance), then through every FK descriptor on that class. Django's
  descriptors fabricate FRESH proxy instances on each access, so the
  per-id() cycle detection never matches, and reading those
  descriptors does internal setattrs as a side effect, retriggering
  our watcher in an unbounded loop. End result: hundreds of queued
  hits per Variables-panel expansion + frozen IDE. Test:
  `test_recursion_stops_at_framework_boundary`. The filter has TWO
  legs (the `_FRAMEWORK_MODULE_ROOTS` denylist AND the
  `site-packages` / `dist-packages` path heuristic); keep both.
- **Drop the `isinstance(value, type)` filter and trust the metaclass
  conflict TypeError to catch class objects.** It does catch them (a
  watcher subclass of `_Inner` can't replace `_Inner.__class__`,
  which is `type`), so behaviorally removing the filter doesn't break
  the regression test. But the filter saves us from constructing the
  watcher subclass + running through `_install_single_object_watch`'s
  full setup before the TypeError lands. Cheaper to reject upfront.
  Test: `test_recursion_skips_class_objects`.
- **Use a per-call `visited` set in `_instrument_object_tree`.** The
  set MUST live on `root_watch.visited_ids` and be shared across the
  initial walk AND every later `__setattr__`-triggered re-entry.
  Pre-fix, each `__setattr__` call into the watcher re-entered with
  `visited={id(wrapped_value)}` – a fresh set that didn't carry the
  ids the initial walk already covered. Combined with Django's
  fabricated-on-each-access proxy instances, every assignment
  restarted a depth-4 walk that wasn't bounded by cycle detection.
  Test: `test_visited_ids_shared_across_setattr_reentry`.
- **Drop the breadth cap (`_try_add_sub_watch`) and let sub_watches
  grow without bound.** Belt-and-suspenders against the cycles the
  type filter + visited set miss. If a user-defined class has cyclic
  proxy semantics like Django's ORM, the cap stops the explosion
  before pydevd's hit queue overflows. Test:
  `test_breadth_cap_engages_with_warning`.
- **Drop `_find_user_caller` and fire hits with `sys._getframe(1)`
  directly.** The `<string>` runtime frame may be ABOVE the user's
  frame in the chain (it usually is, since the watcher's
  `__setattr__` IS the runtime), but sometimes the entire chain
  consists of runtime + descriptor side-effects on a pydevd worker
  thread – no user frame at all. Firing a hit in that case reports
  `<string>:NNN` as the source location and floods pydevd's queue,
  triggering "Trying to stop on non-existent thread" SEVEREs in the
  IDE log. Tests: `test_find_user_caller_walks_past_runtime_frames`,
  `test_find_user_caller_returns_none_for_empty_chain`.
- **Drop the guard around `obj.__class__ = watcher_cls` in
  `_install_single_object_watch`.** When this method is called
  recursively from `_instrument_object_tree` for a nested attribute,
  the holder already has a watcher class installed. The `__class__`
  swap on `obj` would propagate up to the holder's `__setattr__`
  watcher and queue a spurious hit at `<string>:549` (the line of the
  assignment in this file). The guard keeps the swap silent.
- **Replace `_installing_watch_thread` with a simple boolean.** A plain
  `True/False` flag would suppress mutations on ALL threads, including
  legitimate user-thread writes that happen concurrently with the IDE's
  evaluator calling `watch_at()`. The thread-ident comparison scopes
  suppression to the installing thread only. Test:
  `test_installation_suppression_thread_scoped`.
- **Replace `_installing_watch_thread` with a counter (to handle nested
  `watch()` calls).** Nested `watch()` inside a `__setattr__` is
  theoretically possible but nearly impossible in practice because the
  `_guard.active` flag prevents re-entry through the watcher's hook,
  and `_instrument_object_tree` uses `_guard.active = True` around its
  own setattr calls. A counter adds complexity without covering a real
  scenario. If nested `watch()` ever becomes real, the correct fix is
  making `_installing_watch_thread` a per-thread counter in a
  `threading.local()`, not a global counter.
- **Move the `_installing_watch_thread` check below the
  `_find_user_caller` walk in `_handle_hit`.** The suppression check
  MUST be the first gate: `_find_user_caller` walks `f_back` which can
  trigger frame access on descriptors, potentially causing more side-
  effect writes. Checking `_installing_watch_thread` first is both
  cheaper (one int compare) and prevents cascading re-entry.
- **Remove `_bp_pause_pending` cleanup from `_pycharm_consume_last_hit`
  ("let them die naturally when _on_line fires").** Stale entries that
  were never reached (the primary pydevd bp path fired first, or the
  code line was simply never executed) would accumulate forever, and
  a future execution of the same code+line would spuriously trigger
  `_trigger_direct_pause` -- a phantom pause from a prior session's
  watchpoint. Cleanup on consume is what keeps this self-healing.
- **Coalesce bulk mutations from the same source line into one queued
  hit ("150 setattr calls all from the same line -- merge to 1 stop").**
  This was tried and reverted. Users want N pauses for N mutations, not
  a single coalesced stop with a batch count. The tight-loop exhaustion
  problem (150 mutations from a 2-line for-loop) is solved correctly by
  the loop-back bp target (v24): `_next_code_line_after_frame` detects
  `JUMP_BACKWARD` and returns the loop header, giving each mutation its
  own bp at the for-header that fires once per iteration.
- **Filter library frames in the f_back walk of `_compute_bp_targets`
  ("skip site-packages, only use user-code frames").** This was tried
  and reverted. The f_back walk finds the NEAREST caller with a next
  code line, skipping only runtime frames. Adding library-frame filtering
  pushes bps to distant frames that fire late, scrambling hit ordering
  in Django middleware stacks (session/csp_nonce/auser/_messages fire
  at the END instead of in order). The tight-loop case (150 mutations
  exhausting user-code lines) is solved by the loop-back bp target (v24),
  not by f_back filtering.

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
- **Recursive object-watch has a breadth cap too.**
  `_MAX_SUB_WATCHES_PER_ROOT = 100`. Once a root accumulates 100
  sub-watches the cap engages, `root_watch.sub_watches_capped` flips
  to True, and a one-line `[WATCHPOINT]` warning is logged naming
  the root. Belt-and-suspenders against any cyclic / framework-shaped
  graph the type filter + visited set miss. Test:
  `test_breadth_cap_engages_with_warning`.
- **Slotted classes aren't recursed into.** `_safe_iter_dict_attrs`
  silently no-ops on objects without `__dict__`. The slotted object's own
  direct attr changes still fire (class surgery catches `__setattr__`),
  but its slotted attrs' nested objects aren't auto-discovered.
- **Framework / stdlib / site-packages types aren't recursed into.**
  When watching a user DTO that holds a Django QuerySet / SQLAlchemy
  session / `pathlib.Path` / etc., we instrument the DTO fully but
  stop at the framework boundary – the framework object itself is
  left untouched. This is INTENTIONAL: without it, watching a Django
  QuerySet causes hundreds of queued hits per Variables-panel
  expansion as Django's descriptors fabricate fresh proxy instances
  on each access (cycle detection by `id()` never matches) and
  reading those descriptors does internal setattrs as a side effect,
  re-triggering our watcher in an unbounded loop. The user can still
  watch the framework value directly – `watch("queryset")` arms
  classpatch (if the metaclass allows) or local-variable rebind
  detection, both of which catch what the user actually cares about
  (rebinds of their local) without instrumenting ORM internals.
  See `_FRAMEWORK_MODULE_ROOTS` / `_is_user_defined_type` for the
  heuristic. Tests:
  `test_recursion_stops_at_framework_boundary`,
  `test_is_user_defined_type_rejects_known_frameworks`,
  `test_is_user_defined_type_rejects_stdlib_modules`,
  `test_is_user_defined_type_rejects_site_packages_heuristic`.
- **Class objects aren't recursed into.** If a user attribute holds a
  reference to a class (e.g. `self.registry_cls = SomeClass`), we don't
  walk the class's `__dict__` – it's full of descriptors (properties,
  classmethods, ORM relationship descriptors) that would each get
  instrumented and trigger explosive growth. The class itself is left
  untouched; the user's attribute REBIND on `self.registry_cls` still
  fires because the holder is watched. Test:
  `test_recursion_skips_class_objects`.
- **Hits originating from runtime frames are dropped.** When a
  descriptor side-effect or a debugger-protocol attribute access
  triggers our watcher's `__setattr__` from a frame chain entirely
  inside `<string>` (the runtime's exec'd module), `_find_user_caller`
  returns None and the hit is silently discarded rather than queued.
  Without this filter, the IDE's Variables-panel expansion would flood
  pydevd's hit queue with `<string>:NNN` lines. Test:
  `test_find_user_caller_returns_none_for_empty_chain`.

## Test layout (`test_watchpoint.py`)

Organized in roughly fifteen bands (189 tests):

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
12. **Loop-back bp target** --
    `test_next_code_line_after_frame_returns_loop_header_for_tight_loop`,
    `test_next_code_line_after_frame_prefers_forward_line_over_loop_back`,
    `test_handle_hit_installs_primary_bp_at_loop_header`:
    `_next_code_line_after_frame` returns the for-header via
    `JUMP_BACKWARD` target detection when no forward line exists.
    `_handle_hit` installs the primary bp at the loop header for tight
    loops, giving each mutation its own bp that fires per-iteration.
13. **v13 direct-pause dispatch** -- `test_compute_bp_targets_*`,
    `test_install_bp_registers_in_bp_pause_pending`,
    `test_on_line_dispatches_direct_pause_for_pending_bp`,
    `test_on_line_ignores_non_pending_lines`,
    `test_consume_clears_bp_pause_pending_for_drained_hits`:
    belt-and-suspenders mechanism where our own `_TOOL_ID`'s `_on_line`
    triggers `do_wait_suspend` when pydevd's DEBUGGER_ID callback
    doesn't fire (the f_back-intermediate walk-up for last-line
    mutations, the `_bp_pause_pending` dict, and its cleanup on
    consume/clear).
14. **Installation side-effect suppression** --
    `test_handle_hit_suppressed_during_installation`,
    `test_installing_watch_flag_cleared_on_exception`,
    `test_installation_suppression_thread_scoped`,
    `test_installation_suppression_same_thread_does_suppress`,
    `test_multiple_watches_in_sequence_each_suppresses_independently`,
    `test_suppression_does_not_discard_baseline`,
    `test_suppression_only_active_during_installation_window`,
    `test_watch_obj_then_watch_dotted_both_fire`,
    `test_getattr_side_effect_suppressed_during_tree_walk`,
    `test_overlapping_watch_obj_x_then_obj_fires_on_x`:
    the `_installing_watch_thread` flag that silences side-effect
    mutations triggered by our own tree walk during `watch()` setup,
    plus overlapping-watch scenarios verifying stacked class-surgery
    doesn't break either watcher.
15. **Hit payload caller info (secondary highlight)** --
    `test_hit_payload_includes_caller_file_and_line`,
    `test_caller_walks_to_bp_file_through_intermediates`,
    `test_caller_walk_same_file_as_primary_bp`,
    `test_caller_fallback_when_no_frame_matches_bp_file`:
    the `caller_file` / `caller_line` fields (6 and 7) in the hit
    payload, populated by the frame-walk-to-bp-file logic in
    `_handle_hit`. After `_compute_bp_targets` returns, walks up from
    `user_frame.f_back` looking for a frame in the same file as
    `targets[0][0]`; if found, uses that frame's `f_lineno` as the
    call-site line. Falls back to `f_back` when no ancestor matches.
    The Kotlin side uses this for the secondary "call-site" highlight.

Tests use a helper-inner-function pattern (`def _code(): ...; with
pytest.raises(WatchpointHit): _code()`) because of the 3.14 LINE-exception
propagation behavior. See the docstring at the top of the file.
