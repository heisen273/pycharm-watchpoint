# Python watchpoint runtime – handoff notes (trimmed)

> Full version: `CLAUDE.md`. This file is a compact reference. Read the full
> version before making non-trivial changes to the runtime.

## TL;DR

- `watchpoint.py` – runtime: registry, sys.monitoring callbacks, pydevd integration.
- `test_watchpoint.py` – 190 tests, pure-pytest (no pydevd).
- `conftest.py` – per-test cleanup of registry + frame state.
- Targets **Python 3.12, 3.13, 3.14**.
- **Zero global sys.monitoring overhead** until the first `watch()` call.

```bash
python3.12 -m pytest test_watchpoint.py
python3.13 -m pytest test_watchpoint.py
python3.14 -m pytest test_watchpoint.py
```

## Public API

```python
watch(expr, *, frame=None)   # arm a watchpoint
unwatch(expr)                # remove
clear_watches()              # remove all

# Plugin-side entry points on builtins:
builtins._pycharm_watch          # alias of watch
builtins._pycharm_watch_at       # name, file_hint, func_hint – locates the paused user frame
builtins._pycharm_unwatch
builtins._pycharm_clear_watches
builtins._pycharm_watchpoint_diag  # diagnostic about pydevd lookup
builtins._watchpoint_registry    # the singleton, for conftest cleanup
```

`watch("name")` auto-picks a flavor:

| Resolved value             | Watch installed                                                           |
| -------------------------- | ------------------------------------------------------------------------- |
| primitive / list / dict …  | **local-variable** (LINE-event diff per frame)                            |
| user-defined object        | **object-wide attribute** + recursive instrumentation to depth 4 (§9)    |
| `"a.b.c"` (dotted)        | **specific attribute**; if leaf is list/dict/set, also container-wrap (§9)|

## Design contract – critical invariants

### §1 Per-frame keying
`_local_watches` is keyed by `(name, id(frame))`. Makes recursive/concurrent/asyncio
watches work correctly. Each frame instance gets its own watch row.

### §2 Watches are frame-scoped, NOT function-scoped
- PY_RETURN removes the row and pops `_frame_state`.
- PY_START removes leftover rows whose `frame_id` matches the new frame's `id()` (handles CPython frame-address reuse after exception unwind).
- **DO NOT** add a dead-frame sweep in `_on_line` – it deletes concurrent live frames' watches (broke asyncio tests).

### §3 Frame state tagged with `code`
`_frame_state[fid]` carries `code`, `prev_line`, `prev_hashes`. If `code` doesn't match on LINE event → state is stale, reinitialize.

### §4 PY_UNWIND can't be a local event
Only global, which defeats zero-overhead goal. PY_RETURN + PY_START + code-tag check is sufficient.

### §5 Tool ID priority
`_setup_monitoring` tries `[5, 4, 3, PROFILER_ID, COVERAGE_ID]`. **DEBUGGER_ID (0) is never tried** – pydevd owns it.

### §6 Concurrency: lock-snapshot pattern
All callbacks acquire `self._lock` to snapshot/mutate state. Lock is **released before** any call to `_handle_hit → _pause_via_pydevd`. Holding through a pause freezes other threads.

### §7 Per-thread reentrancy guard
`self._guard = threading.local()`. Set in callbacks before work. Prevents recursive LINE events and prevents pydevd's protocol code from re-triggering our `__setattr__` overrides while paused.

### §8 Cross-function watch propagation
When a watching frame calls a function with a watched value as argument, the watch follows into the callee:
1. `_add_local_watch` enables LINE + PY_RETURN + PY_START + **CALL** on the watched code object.
2. `_on_call` snapshots `{id(value): caller_name}` and pushes `(callee_code, snapshot)` onto a thread-local stack.
3. `_on_py_start` pops the matching entry and arms watches on callee params whose `id()` matches.

Key rules:
- **Identity-based matching** (`id(value)`). Do NOT switch to `==` – breaks user-defined-object case.
- **Lazy callees are skipped** (`CO_GENERATOR | CO_COROUTINE | CO_ASYNC_GENERATOR` – their body isn't entered at call time).
- Hard cap of `_MAX_PROPAGATION_QUEUE_SIZE` entries as belt-and-suspenders.

### §9 Container-mutation + recursive object instrumentation

**Container wrap** (`watch("obj.attr")` where attr is list/dict/set):
`__class__` surgery is impossible on builtins. Instead, construct `_WatchedList`/`_WatchedDict`/`_WatchedSet` and replace the leaf attribute. Every mutating method snapshots before/after repr and calls `_wp_fire_container_change` if they differ. `_AttributeWatch` remembers the install for cleanup (restores plain container on unwatch).

**Recursive object-wide** (`watch("obj")` on a user-defined object):
`_add_object_watch` calls `_install_single_object_watch` then `_instrument_object_tree` walks `obj.__dict__` to depth 4, breadth-capped at 100 sub-watches per root, cycle-guarded by `root_watch.visited_ids`.

Filters applied per attribute:
- User-defined objects → recursive `_install_single_object_watch` + deeper walk.
- list/dict/set → container wrap + guarded setattr.
- Framework types / stdlib / site-packages → **skip** (avoids Django QuerySet explosion).
- Class objects (`isinstance(value, type)`) → **skip** (descriptor-laden `__dict__`).

Critical: **`visited_ids` lives on `root_watch`, not per-call** – this stops the Django descriptor explosion where fresh proxies fabricate on each access so per-call sets never match.

Known trade-offs: `type(obj.attr) is list` becomes False (wrapper is subclass); aliases captured before watch-arm bypass the wrapper.

### §10 Classpatch fallback for hostile metaclasses
When `class _WatchedSubclass(orig_cls):` raises (Django `ModelBase`, SQLAlchemy `DeclarativeMeta`), monkey-patch `cls.__setattr__` instead:
- `_install_classpatch_attr_watch`: specific-attribute entry.
- `_try_classpatch_object_watch`: wildcard `'__any__'` entry (fires on any attribute write).

Per-instance gate: `instance_watches.get(id(self_obj))` – unrelated instances pay one dict miss. DO NOT remove this gate.

Re-entrancy guard, specific-over-wildcard priority, and symmetric cleanup apply identically to class-surgery paths. Classpatch is NOT used from `_instrument_object_tree` – only from top-level dispatch.

### §11 Sequential pre-emptive breakpoints – one IDE pause per mutation
Each hit installs its OWN pydevd `LineBreakpoint` at a successive code line via `_compute_bp_targets` + `_install_bp_at`. N mutations → N distinct bp locations, each drainable independently.

`_next_slot_for_code(code, start_line)` finds the next available line after `max(max(used_lines), start_line)` – the `max` guard (v21) prevents backward-pointing bps at already-executed lines.

`_compute_bp_targets(user_frame, source_line)` builds up to two slots:
- **Primary**: next future code line in `user_frame.f_code`. Uses `frame.f_lasti` + `dis.get_instructions` for multi-line statements where numeric source order points into already-executed RHS lines. When no forward line exists but a `JUMP_BACKWARD` is present (tight loop), the loop header is used as bp target via `_offset_to_line(code, jump_target)` (v24 fix).
- **Safety**: next code line in `_find_user_code_caller`'s walked-up frame.

When primary is exhausted AND no loop-back is available, the f_back walk finds the nearest caller frame with a valid next line (skipping only our runtime frames).

`_pycharm_consume_last_hit(pause_file, pause_line)` drains the hit whose bp location matches the current pause. ALL bps for drained hits are removed (including unfired siblings).

### §12 Pause anchor walk-up (mutation inside library code)
When `__setattr__` fires from inside framework code (Django `QuerySet._clone`, SQLAlchemy flush, pydantic rebuild), PyCharm's "do not step into library code" filter silently drops `CMD_STEP_OVER` on a library frame.

`_handle_hit` computes a separate **pause anchor** by walking further up via `_find_user_code_caller` past site-packages AND stdlib frames (both filtered identically by pydevd). The library mutation is still recorded as SOURCE (highlight renders on e.g. `query.py:289`); the debugger suspends at user code above.

If the entire chain is library/runtime (no user code in `_MAX_FRAME_WALK_HOPS` hops) → hit is **silently dropped**. A phantom highlight with no pause is worse UX than no signal.

### §13 Pause mechanism – pydevd `LineBreakpoint` (primary)
The primary pause is `_install_pause_breakpoint`, NOT `_pause_via_pydevd` (the CMD_STEP_OVER approach is unreliable in PEP 669 mode for frames whose `py_start_callback` decided "no LINE tracing" at first entry).

Pipeline:
1. Find the next future bytecode-order line using `f_lasti` + `co_lines()` (NOT `f_lineno + 1` – blank lines have no LINE events).
2. Install pydevd `LineBreakpoint` via `py_db.consolidate_breakpoints`.
3. **Force** `sys.monitoring.set_local_events(DEBUGGER_ID, target_code, existing | LINE | PY_RETURN)` – this overrides pydevd's internal "should-trace" decision. Without this step, the bp is in pydevd's table but `py_line_callback` never fires.
4. Track bp in `_temp_breakpoints` with **negative bp_id** (avoids colliding with IDE's positive IDs).
5. Cleanup runs on EVERY `sessionPaused` (not just watchpoint pauses).

**Last-resort fallback**: `_pause_via_do_wait_suspend` – used only when `_install_pause_breakpoint` returns empty (mutation at last statement of module with no user-code caller). Puts `urllib.parse.quote` on the stack (rule-1 trade-off) but guarantees the IDE pauses.

### §14 Named caps (all 1024)
- `_MAX_HIT_QUEUE_SIZE` – `_hit_queue` size before dropping oldest.
- `_MAX_PROPAGATION_QUEUE_SIZE` – per-thread CALL propagation queue.
- `_MAX_FRAME_WALK_HOPS` – max `f_back` hops before giving up.

### §15 `_wp_container_repr` must swallow repr errors
All 33 mutating-method snapshot calls go through `_wp_container_repr(self)` which wraps `list.__repr__` / `dict.__repr__` / `set.__repr__` in try/except, returning `"<unreprable>"` on any error. Without this, `deepcopy` into a `_WatchedDict` raises when a half-constructed Django Model's `__repr__` touches uninitialized `_state`.

### §16 Runtime fingerprint + file log
- `_RUNTIME_VERSION` string logged at module load. Bump on every meaningful behavioral change. Lets you distinguish "fix didn't help" from "stale build."
- `_log_warn` tees to `/tmp/pythonwatchpoint.log` (fixed path, append-mode, truncated at 2 MB → 1 MB). Durable across pytest capture and pydevd stdout interception.

### §17 Direct-pause dispatch via `_bp_pause_pending`
Belt-and-suspenders: after `consolidate_breakpoints`, also register `(id(target_code), line)` in `_bp_pause_pending` and arm the same events under our own `_TOOL_ID`. Our `_on_line` fires this path independently when pydevd's `py_line_callback` doesn't fire (prior `DISABLE` decision). Pops before dispatch (no double-fire). Cleared on drain and `clear_watches()`.

### §18 Installation side-effect suppression (`_installing_watch_thread`)
`_installing_watch_thread: Optional[int]` is set to `threading.get_ident()` around `_registry.add_watch()`. `_handle_hit` early-returns when the hit is on the installing thread (catches lazy-evaluation side-effects during `_instrument_object_tree` walk). Thread-scoped (not a bool) so other threads still fire. Cleared in `finally` – must not leak.

## The pydevd pause – two rules (never break these)

**Rule 1: never call `py_db.do_wait_suspend(...)` directly from our callback chain.** It puts `urllib.parse.quote` on the thread's stack at pause time. The user sees `urllib/parse.py` as topmost frame.

**Rule 2: never use `set_suspend(... is_pause=True)` + `state = STATE_SUSPEND`.** Sets "pause on next event in ANY frame" – latches on stdlib codec frames in pydevd's stdout-interception chain when user code prints anything.

**What we do instead** (scoped step-over / legacy reference – actual primary mechanism is §13):
```python
info.pydev_state = STATE_RUN
info.pydev_step_cmd = CMD_STEP_OVER
info.pydev_step_stop = user_frame
# disarm f_trace on our <string> frames
# set user_frame.f_trace = py_db.trace_dispatch
# PEP 669 supplement: set_local_events(DEBUGGER_ID, user_frame.f_code, existing | LINE | PY_RETURN)
#                     set_local_events(DEBUGGER_ID, user_frame.f_back.f_code, existing | LINE | PY_RETURN)
```
`_pause_via_pydevd` is kept as reference. The `LineBreakpoint` path in §13 is the primary.

**No-pydevd fallback**: `_handle_hit` raises `WatchpointHit` when `_get_pydevd_debugger()` returns None. The test suite depends on this – do NOT remove.

## Cross-version notes

| Concern                             | Behavior                                                                   |
| ----------------------------------- | -------------------------------------------------------------------------- |
| `sys.monitoring` API                | Stable since 3.12. Same callback signatures.                               |
| `frame.f_locals`                    | 3.13: fresh `FrameLocalsProxy` each access. Always `dict(frame.f_locals)` once. |
| LINE-callback exception propagation | **3.14 bypasses local `try/except` in the monitored frame.** Tests wrap monitored code in an inner helper. |
| `PY_UNWIND` as local event          | Rejected on all 3.12+ (`ValueError`). Confirmed empirically.              |
| `PY_START` as local event           | Works on 3.12+. Used for id-reuse cleanup.                                |

## `_value_hash` semantics

- Immutables: `hash((type, value))` – type-tagged so `1 == True` doesn't mask type change.
- Mutable containers: `hash(repr(value))`.
- Custom objects: `id(value) ^ hash(type.__qualname__)` – in-place mutation NOT detected (use object-wide watching).

## Frame discovery for the PyCharm action

`_find_paused_user_frame` walks every thread's stack via `sys._current_frames()` matching by `co_name == func_hint` + multiple file-suffix comparisons (absolute / `/private/var/...` / basename). When multiple candidates match (recursion) → picks the **innermost** frame.

## Key code map (`watchpoint.py`)

| Symbol | Purpose |
| --- | --- |
| `WatchpointRegistry` | Singleton holding all state. |
| `add_watch` | Dispatch: local / object-wide / specific-attr. |
| `_add_local_watch` | Per-frame local watch + LINE/PY_RETURN/PY_START/CALL events. |
| `_add_attr_watch` | Class surgery for `obj.attr`; container wrap for list/dict/set; falls back to classpatch. |
| `_add_object_watch` | Top-level object: class surgery + `_instrument_object_tree` to depth 4. |
| `_install_single_object_watch` | Per-object class-surgery installer (root + nested). |
| `_make_any_attr_watcher_class` | Fresh dynamic `_WatchedAnyAttrSubclass` per object (closure-captured `_expr`). |
| `_instrument_object_tree` | Recurses `__dict__` to depth 4; cycle-guarded via `root_watch.visited_ids`. |
| `_handle_hit` | Three gates: (1) installation suppression, (2) user-code-caller existence, (3) bp install. Source file/line stays at mutation site regardless of pause anchor. |
| `_install_pause_breakpoint` | Primary pause: `consolidate_breakpoints` + `set_local_events(DEBUGGER_ID, ...)`. |
| `_next_code_line_in` | `co_lines()`-based lookup for next code line (skips blanks). |
| `_offset_to_line` | Maps a bytecode offset to its source line via `co_lines()`. Used by loop-back detection. |
| `_next_code_line_after_frame` | Bytecode-order lookup using `f_lasti`; rejects backward lines unless they are loop back-edges (`JUMP_BACKWARD` target via `_offset_to_line`). |
| `_find_user_code_caller` | Walks `f_back` past runtime + site-packages + stdlib to find user code. |
| `_find_user_caller` | Walks `f_back` past `_RUNTIME_FILENAMES` only. |
| `_pause_via_pydevd` | Legacy scoped step-over (kept as reference; not the primary path). |
| `_pause_via_do_wait_suspend` | Last-resort fallback when all bp installs fail. |
| `_wp_container_repr` | Safe repr for `_WatchedList`/`Dict`/`Set` – swallows repr errors. |
| `_WatchedList/Dict/Set` | Builtin subclasses; every mutating method fires change detection. |
| `_ClassPatch` | Per-class state for classpatch: original setattr, `instance_watches`. |
| `_bp_pause_pending` | `{(id(code), line): True}` – direct-pause dispatch (§17). |
| `_temp_breakpoints` | `[(file, line, bp_id)]` – cleaned up on every sessionPaused. |
| `_installing_watch_thread` | Side-effect suppression during tree walk (§18). |
| `_FRAMEWORK_MODULE_ROOTS` | Frozenset of module roots to skip during recursion (django, sqlalchemy, pydevd, ...). |
| `_is_user_defined_type` | Returns True iff type is from user code (not builtins/stdlib/framework/site-packages). |
| `_is_library_filename` | True for site-packages / dist-packages / stdlib paths. Overridable via `PYCHARM_WATCHPOINT_USER_ROOTS`. |
| `_RUNTIME_VERSION` | Version string – bump on every behavioral change; logged at import. |

## Anti-patterns – DON'T do these

- **DON'T** add a dead-frame sweep in `_on_line` (`frame_id != id(current_frame)`). Deletes concurrent live frames' watches (broke asyncio).
- **DON'T** re-introduce `_pause_pending` boolean gate. Silently drops hits 2..N. Use sequential bps instead.
- **DON'T** skip `_find_user_code_caller` walk-up in `_handle_hit`. Library-frame anchor is silently dropped by PyCharm's "no step into library" filter. Result: hit highlights but debugger never pauses.
- **DON'T** remove stdlib from `_is_library_filename`. Pydevd treats stdlib identically to site-packages – anchoring there fails. (deepcopy case)
- **DON'T** fall back to the immediate `user_frame` when `_find_user_code_caller` returns None. Same bug.
- **DON'T** call `do_wait_suspend` directly from the callback chain. Rule 1 – urllib on the stack.
- **DON'T** set `info.pydev_message`. Same urllib path.
- **DON'T** switch back to `set_suspend(is_pause=True) + STATE_SUSPEND`. Rule 2 – latches on codec frames.
- **DON'T** drop `user_frame.f_trace = py_db.trace_dispatch` and rely solely on `set_trace_for_frame_and_parents`. It silently no-ops in PEP 669 mode.
- **DON'T** skip `set_local_events(LINE | PY_RETURN)` after `consolidate_breakpoints`. `restart_events()` is NOT sufficient for mid-execution code objects with prior "no LINE" decisions.
- **DON'T** use `f_lineno + 1` for bp install line. Blank lines have no LINE events – bp sits inert. Use `_next_code_line_in(code, f_lineno)`.
- **DON'T** remove `_pause_via_do_wait_suspend` fallback. Silent no-pause is worse UX for the last-line-of-module corner case.
- **DON'T** drop PY_RETURN from the PEP 669 supplement or skip `f_back` arm. Both needed for the last-line-of-helper case ("stops 2 times for 3 mutations" symptom).
- **DON'T** register PY_UNWIND globally (kills zero-overhead guarantee).
- **DON'T** switch primary pause back to `_pause_via_pydevd`. Unreliable in PEP 669 mode for frames with prior "no-trace" decisions.
- **DON'T** try `__class__` surgery on list/dict/set. CPython refuses (`__class__` assignment only for heap types). Use wrap-and-replace.
- **DON'T** register sub-watches in `_attr_watches`. They live ONLY in `root_watch.sub_watches`.
- **DON'T** share a watcher class across instances. Each `_make_any_attr_watcher_class` call captures `_expr` + `_root_expr` per object.
- **DON'T** drop `__slots__ = ()` from watcher subclasses. Without it, `__class__` swap raises "layout differs" for slotted original classes.
- **DON'T** switch propagation matching from `id()` to `==`. Breaks user-defined-object case; interned-primitive over-watch is a documented trade-off.
- **DON'T** use classpatch from inside `_instrument_object_tree`. Reserved for top-level dispatch only.
- **DON'T** drop the per-instance `instance_watches.get(id(self_obj))` gate in classpatch `__setattr__`. Would fire on all instances of the patched class.
- **DON'T** drop the `_guard` check in classpatch `__setattr__`. Prevents pydevd's protocol code from recursively pausing on watched attributes.
- **DON'T** drop `_is_user_defined_type` filter in `_instrument_object_tree`. Without it, Django QuerySet recursion explodes (descriptors fabricate fresh proxies on every access, cycle detection by `id()` never matches, unbounded loop).
- **DON'T** use a per-call `visited` set in `_instrument_object_tree`. Must be `root_watch.visited_ids` shared across the initial walk AND every `__setattr__` re-entry. Per-call set doesn't stop Django proxy cycles.
- **DON'T** drop the breadth cap (`_try_add_sub_watch`). Belt-and-suspenders against cycles the type filter + visited set miss.
- **DON'T** drop `_LAZY_BODY_FLAGS` check in `_on_call`. Generator/coroutine bodies aren't entered at call time; queueing propagation for them leaks entries.
- **DON'T** drop the repr error guard in `_wp_container_repr`. All 33 mutation-method snapshot calls must go through it. Django TestCase `_testdata_memo` bug.
- **DON'T** use `weakref` on a frame. Not weakly referenceable in CPython.
- **DON'T** hold `self._lock` while calling `_handle_hit`. Pause would freeze all other threads.
- **DON'T** use a simple bool for `_installing_watch_thread`. Thread-scoped so other threads still fire during installation.
- **DON'T** clean up `_bp_pause_pending` lazily ("let them die in `_on_line`"). Stale entries would spuriously trigger direct-pause for future executions of the same code+line.
- **DON'T** coalesce bulk mutations from the same source line into one queued hit. Users want N pauses for N mutations, not 1 coalesced stop. The loop-back bp target (v24) solves the tight-loop case properly.
- **DON'T** filter library frames in the f_back walk of `_compute_bp_targets`. It pushes bps to distant frames that fire late, scrambling hit ordering in Django middleware stacks. The f_back walk must find the NEAREST frame (skipping only runtime frames), not the nearest user-code frame. Tried and reverted (v24).

## Known limitations (each pinned by a test – don't "fix" without reading the rationale)

- **`del obj.attr` is silent.** We override `__setattr__` not `__delattr__`.
- **`obj.__dict__['attr'] = value` is silent.** Bypasses `__setattr__`.
- **Interned primitives over-watch in propagation.** `watch("x")` for `x = 1` also watches any param receiving the same small int.
- **Default-value parameters don't propagate.** Fixed at definition time.
- **Generators / coroutines / async generators don't propagate.** Body isn't entered at call time.
- **`functools.partial` and C-implemented callables don't propagate.** No Python `__code__`.
- **Property setters may bypass `__setattr__`** depending on CPython's descriptor routing.
- **Frozen dataclasses** – `_add_object_watch` catches `FrozenInstanceError`, falls back to local-variable detection.
- **Django `Model` / SQLAlchemy declarative base** – class-surgery fails (metaclass refuses dynamic subclass). Classpatch fallback handles both dotted and bare-name watches. A `[WATCHPOINT]` warning is emitted. Bare-name classpatch does NOT catch in-place container mutations (watch the dotted path for that).
- **Truly-stubborn metaclasses** (both subclassing AND `cls.__setattr__=` refused) – dotted watch raises TypeError; bare-name falls back to local-variable detection.
- **Container aliases captured before watch-arm bypass the wrapper.**
- **Container contents inside a recursively-watched object aren't recursed into.** `obj.items[0].name = "x"` won't fire if `items` is a watched list.
- **Recursive depth cap**: `_RECURSIVE_OBJECT_WATCH_DEPTH = 4`.
- **Recursive breadth cap**: `_MAX_SUB_WATCHES_PER_ROOT = 100`.
- **Slotted classes** – `_safe_iter_dict_attrs` no-ops; nested attrs not auto-discovered.
- **Framework / stdlib / site-packages types** – recursion stops at the boundary to prevent QuerySet explosion.
- **Class objects** – not recursed into (descriptor-laden `__dict__`).
- **Hits from pure runtime frame chains are silently dropped** to prevent flooding pydevd's queue with `<string>:NNN`.
- **Mutations that happen mid-deepcopy / mid-pickle** are silent when the contained values can't be repr'd (returns `"<unreprable>"`, diff produces no hit – correct trade-off).

## Test layout (15 bands, 189 tests)

1. **Basics** – fire on change, old/new, source line, unwatch, clear, multiple watches.
2. **Frame lifetime** – repeated calls, recursion, stale-state reset.
3. **Regression** – long-string hash, double-watch rearm, no-pydevd contract, `watch_at` lookup.
4. **Concurrency** – thread races, two-thread independence, asyncio gather, await survival.
5. **Object-wide watching** – `_RequestLike` fixture; mutation/silent/surgery-reversal/cross-function.
6. **Last-line / PY_RETURN** – change-on-last-line detected; source line correct.
7. **Cross-function watching** – CALL/PY_START propagation; `test_propagation_*`.
8. **Edge cases** – methods, kwargs, augmented assignment, for-loop, subscript, slots, properties, frozen dataclass, classmethod, staticmethod, builtin skip, queue-leak, interned-primitive trade-off.
9. **Container mutation** – every mutating method on list/dict/set; same-value silent; reassign auto-wraps; unwatch restores; leaked alias stops firing.
10. **Recursive object-wide** – nested fires; unwatch restores; depth cap; breadth cap; newly-assigned values auto-instrumented.
11. **Hostile metaclasses + classpatch** – Django/SQLAlchemy fallback; specific-over-wildcard; two-class cleanup; stubborn metaclass TypeError.
12. **Loop-back bp target** – `_next_code_line_after_frame` returns loop header via `JUMP_BACKWARD` target; `_handle_hit` installs primary bp at loop header for tight loops.
13. **v13 direct-pause dispatch** – `_bp_pause_pending` mechanics; `_on_line` dispatch; cleanup on consume.
14. **Installation side-effect suppression** – `_installing_watch_thread` flag; thread-scoped; flag cleared on exception; baseline preserved.
15. **Hit payload caller info** – `caller_file`/`caller_line` fields for secondary Kotlin-side highlight.

Tests use `def _code(): ...; with pytest.raises(WatchpointHit): _code()` pattern because 3.14 LINE-exception propagation bypasses `try/except` in the monitored frame.

## Diagnostics

```python
_pycharm_watchpoint_diag()  # from PyCharm evaluator while paused
```
Returns: pydevd in `sys.modules`, `_pydevd_bundle` state, `sys.gettrace` owner, last-known lookup error.

Hit notifications: `[WATCHPOINT] hit '<watch>': <old> -> <new> at <file>:<line>` → stderr + `/tmp/pythonwatchpoint.log`.

