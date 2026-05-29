# pythonwatchpoint – PyCharm plugin handoff notes

A PyCharm plugin that adds **watchpoint** support to Python debugging: arm a
watch on a local variable or object attribute, and the debugger pauses when
it changes. The Python runtime lives at `src/main/resources/python/` – read
its own `CLAUDE.md` for the runtime contract. This file covers the
**Kotlin / plugin** side: build, project layout, IntelliJ Platform APIs we
use, and the patterns the future-you needs to keep working.

## Quick start

```bash
# Build (uses bundled JDK 21 via gradle.properties).
./gradlew compileKotlin

# Launch a sandbox PyCharm with the plugin loaded.
./gradlew runIde

# Package for distribution.
./gradlew buildPlugin   # → build/distributions/pythonwatchpoint-1.0.0.zip
```

## Build pins – there are reasons for each

`gradle.properties`:

```
kotlin.code.style=official
org.gradle.java.home=/opt/homebrew/Cellar/openjdk@21/21.0.11/libexec/openjdk.jdk/Contents/Home
```

- **`org.gradle.java.home` MUST point at JDK 21** (or JDK 23 max, but 21 is the
  reference). Without this, Gradle picks up the system default `JAVA_HOME`,
  and if that's JDK 25 the bundled Kotlin compiler in Gradle 8.13 dies with
  `IllegalArgumentException: 25.0.1` (its `JavaVersion.parse()` predates the
  format). Symptom: `BUILD FAILED in 3s` with "* What went wrong: 25.0.1".
- **Gradle wrapper 8.13** – `gradle/wrapper/gradle-wrapper.properties`. Don't
  downgrade below 8.10.2; the IntelliJ Platform Gradle Plugin 2.2.1 has
  trouble with older Gradle versions.

`build.gradle.kts`:

- `org.jetbrains.kotlin.jvm` **2.1.0** + `org.jetbrains.intellij.platform` **2.2.1**.
- Target: `pycharmCommunity("2025.1")` + `bundledPlugin("PythonCore")`.
- Source / target: **Java 21**, Kotlin toolchain with JBR vendor.
- `sinceBuild = "222"`, `untilBuild = "251.*"` – the user has been bumping
  the upper bound when testing against newer IDE builds; keep this in sync
  with whatever PyCharm version they're targeting.

`settings.gradle.kts`:

- Has the awkward `rootProject.name = "..."` line *before* `pluginManagement`.
  Despite Gradle docs saying `pluginManagement` should be first, this matches
  the reference `pythonvartracker` setup and works.

## Long-running daemon trap

If IntelliJ is open on this project it holds a Gradle daemon (you'll see the
JDK 23 daemon at PID Xyz in `ps aux | grep gradle`). Running `./gradlew`
from a terminal can hit `LockTimeoutException: Timeout waiting to lock file
hash cache`. Either:

- Close IntelliJ first, or
- Use `./gradlew --stop` to stop spare daemons, or
- Just build from inside IntelliJ.

Do **NOT** run `rm -rf .gradle` – stale lock files clear on `--stop`. Wiping
`.gradle` forces re-downloading the whole PyCharm CE distribution (~600 MB).

## Source layout

```
src/main/kotlin/com/pythonwatchpoint/
├── services/
│   ├── WatchpointSessionManager.kt    # Carries py source from action → listener
│   └── WatchpointMarkerService.kt     # Tracks armed-watch expressions for the tree renderer
├── listeners/
│   ├── WatchpointDebugListener.kt     # processStarted/processStopped hooks
│   ├── WatchpointHitHighlighter.kt    # Per-session: line highlight + pulse + inline hint on hit
│   └── WatchpointTreeCellRenderer.kt  # Wraps Variables-panel cell renderer to mark watched rows
├── actions/
│   ├── DebugWithWatchpointAction.kt   # Toolbar: clone run config + inject
│   └── AddWatchpointAction.kt         # Variables-panel right-click
└── icons/
    └── WatchpointIcons.kt             # Single Icon field consumed by plugin.xml + Kotlin

src/main/resources/
├── META-INF/plugin.xml                # Plugin manifest
├── icons/                             # Plugin-owned SVGs (loaded via IconLoader)
│   ├── watchpoint.svg                 # 16x16 light
│   ├── watchpoint_dark.svg            # 16x16 dark (needs explicit fill or it's invisible)
│   ├── watchpoint@20x20.svg           # New-UI 20x20 light
│   └── watchpoint@20x20_dark.svg      # New-UI 20x20 dark
└── python/                            # Bundled watchpoint runtime
    ├── watchpoint.py
    ├── test_watchpoint.py
    ├── conftest.py
    └── CLAUDE.md                      # Runtime-side handoff
```

## plugin.xml essentials

```xml
<id>com.pythonwatchpoint</id>
<depends>com.intellij.modules.python</depends>
<depends>com.intellij.modules.platform</depends>

<projectService serviceImplementation=".services.WatchpointSessionManager"/>
<projectService serviceImplementation=".services.WatchpointMarkerService"/>
<projectListeners>
  <listener class=".listeners.WatchpointDebugListener"
            topic="com.intellij.xdebugger.XDebuggerManagerListener"/>
</projectListeners>
```

The `icon="..."` attribute on each `<action>` references the plugin's own icon
via the Kotlin object's `@JvmField`:

```xml
<action ... icon="com.pythonwatchpoint.icons.WatchpointIcons.Watch"/>
```

This works **only** because `WatchpointIcons.Watch` is declared `@JvmField val
Watch: Icon = ...`. Without the annotation, Kotlin generates a getter and the
attribute can't resolve to a static field. Same gotcha if you ever add a
second icon.

Action groups in use:

| Group ID                            | What it is                                |
| ----------------------------------- | ----------------------------------------- |
| `XDebugger.ToolWindow.TopToolbar`   | Debug-tool-window top toolbar (DebugWithWatchpointAction) |
| `MainToolbarRight`                  | Main IDE toolbar right segment (same action) |
| `XDebugger.ValueGroup`              | Right-click menu on a node in the Variables panel (AddWatchpointAction) |

## Architecture flow

### "Debug with Watchpoint" path

1. `DebugWithWatchpointAction.actionPerformed`:
   - Calls `cleanAllConfigurations(project)` – scans every saved Python run
     config and strips leftover `PYCHARM_WATCHPOINT_ACTIVE` env + temp-dir
     `PYTHONPATH` entries. Defensive against half-finished prior runs.
   - Loads `watchpoint.py` from plugin resources, base64-encodes it.
   - `WatchpointSessionManager.startSession(code)` stashes the source.
   - **Clones** the currently-selected run config (don't mutate the user's
     saved config) and renames to `"[WATCHPOINT] <original name>"`.
   - `injectViaSiteCustomize(clonedConfig, code)`:
     - Writes a `sitecustomize.py` to a fresh temp dir
       (`/tmp/pycharm_watchpoint_XXX/`).
     - The sitecustomize, gated by `PYCHARM_WATCHPOINT_ACTIVE=1`, registers
       a `watchpoint` module in `sys.modules` and `exec`'s the base64-decoded
       runtime into it.
     - Sets `PYTHONPATH = <tempdir>:<existing>` and `PYCHARM_WATCHPOINT_ACTIVE=1`
       on the cloned config.
   - Launches via `ProgramRunnerUtil.executeConfiguration(...)`.

2. `WatchpointDebugListener.processStarted`:
   - Consumes the queued source from the session manager.
   - Registers a Python exception breakpoint for `watchpoint.WatchpointHit`
     (safety net – the runtime's pause-via-pydevd path doesn't raise, but
     the no-pydevd fallback does).
   - On a 500ms delay, probes via `evaluator.evaluate("hasattr(builtins, ...)`
     whether sitecustomize already booted the runtime. If not, base64+exec
     the runtime through the evaluator as a fallback.

3. `WatchpointDebugListener.processStopped`:
   - Removes the exception breakpoint we added.

### "Add Python Watchpoint" path

1. User pauses at any breakpoint, right-clicks a variable in the Variables
   panel.
2. `AddWatchpointAction.perform`:
   - Reconstructs the full dotted path by walking `XValueNodeImpl` parents
     (`calculateFullPath`) – the leaf node only knows its own name like
     `"user"`; we climb to get `"request.user"`.
   - Grabs `session.currentStackFrame.sourcePosition.file.path` and
     `(currentStackFrame as PyStackFrame).name`. Critical: the evaluator
     runs in a separate context, so `sys._getframe(1)` from inside the
     evaluator does **not** reach the user's paused frame. We pass the
     file + func to `_pycharm_watch_at` and let the Python side find the
     paused frame via `sys._current_frames()` (see Python `CLAUDE.md`).
   - Evaluates `_pycharm_watch_at('<path>', '<file>', '<func>')`.
   - Notifies via `NotificationGroupManager → "Debugger messages"`.

## IntelliJ Platform API cheatsheet

### Exception-breakpoint API (used in `WatchpointDebugListener`)

Class extraction from PyCharm bundle (for when you need to verify signatures):

```bash
cd "/Applications/PyCharm CE 4.app/Contents/plugins/python-ce/lib"
unzip -p python-ce.jar com/jetbrains/python/debugger/PyExceptionBreakpointProperties.class > prop.class
javap -p prop.class
```

`PyExceptionBreakpointProperties` (key public fields, post-2024):
- `PyExceptionBreakpointProperties(String exceptionName)` constructor.
- `boolean myNotifyOnTerminate` / `isNotifyOnTerminate()` / `setNotifyOnTerminate(boolean)`.
- `boolean myNotifyOnlyOnFirst`.
- `boolean myIgnoreLibraries`.
- `String myCondition`, `String myLogExpression`.

`PyExceptionBreakpointType`:
- Look up via `XBreakpointType.EXTENSION_POINT_NAME.findExtensionOrFail(PyExceptionBreakpointType::class.java)`.
- Add via `XDebuggerManager.getInstance(project).breakpointManager.addBreakpoint(type, props)`.
- Must be inside a `WriteAction.computeAndWait { ... }` block.

### Cloning a run configuration

```kotlin
val clonedConfig = originalConfig.clone() as AbstractPythonRunConfiguration<*>
clonedConfig.name = "[WATCHPOINT] ${originalConfig.name}"
// Mutate clonedConfig.envs / clonedConfig.PYTHONPATH freely – the original is untouched.
val newSettings = runManager.createConfiguration(clonedConfig, selectedSettings.factory)
newSettings.isTemporary = true  // doesn't pollute the saved list
ProgramRunnerUtil.executeConfiguration(newSettings, DefaultDebugExecutor.getDebugExecutorInstance())
```

### Evaluating Python from Kotlin during a paused session

Two paths, with very different return-value semantics:

```kotlin
// (A) Generic XDebuggerEvaluator – callback-based, EDT-friendly.
debugProcess.evaluator?.evaluate(pythonExpr, object : XDebuggerEvaluator.XEvaluationCallback {
    override fun evaluated(result: XValue) {
        // result.toString() returns the EXPRESSION text (the name shown in
        // the Variables tree), NOT the Python return value. Use:
        val raw = (result as? PyDebugValue)?.value
        // raw is the variables-panel display string – TRUNCATED to
        // PyDebugValue.MAX_VALUE (~256 chars).
    }
    override fun errorOccurred(errorMessage: String) {
        logger.warn("Evaluation failed: $errorMessage")
    }
}, /* position = */ null)

// (B) PyDebugProcess.evaluate – synchronous, no truncation, off-EDT only.
val pyValue = debugProcess.evaluate(pythonExpr,
                                    /* execute = */ false,
                                    /* doTrunc = */ false)
// pyValue.value is the full, untruncated string.
```

Pick (A) for short return values (single-word OK/ERROR signals) and (B) for
anything that might exceed ~256 chars (base64 payloads, multi-line dumps).
(B) blocks the calling thread on pydevd's protocol round-trip, so wrap it in
`ApplicationManager.getApplication().executeOnPooledThread { ... }` and hop
back to the EDT with `invokeLater` for any UI work.

**The toString-vs-value trap caught us twice this codebase:**
`PyDebugValue.toString()` returns the EXPRESSION text (the name field shown
in the Variables tree), not the evaluated value. Always use `.value`. The
`AddWatchpointAction`'s old "ERROR" check accidentally worked because the
expression didn't contain the substring "ERROR" – it would silently fail to
detect actual Python-side errors.

The evaluator runs the expression in the user's paused frame's
globals/locals **context**, but the actual `sys._getframe()` stack the
expression sees is pydevd's, not the user's. Pass identifying info
(file path, function name) into the expression and let the Python side
locate the real frame via `sys._current_frames()`.

### Refreshing the Variables panel after Python-side mutations

When the evaluator runs a side-effecting expression that changes a live
object's `__class__` or wraps a container in place (which is exactly what
`_pycharm_watch_at` / `_pycharm_unwatch` do), the IDE's Variables panel
keeps showing the **previous** type/repr until the next step or breakpoint
hit naturally re-fetches. PyCharm snapshots variables at fetch time; it
doesn't poll for changes the debugger didn't announce.

Force the re-fetch yourself in the `evaluated` callback:

```kotlin
XDebuggerManager.getInstance(project).currentSession?.rebuildViews()
```

Call it inside `ApplicationManager.getApplication().invokeLater { ... }`
so the rebuild lands on the EDT after the callback. Both `addWatchpoint`
and `removeWatchpoint` in `AddWatchpointAction` do this – without it, the
user sees `_WatchedAny_Order` (or `_WatchedList` for wrapped containers)
linger in the panel until they step.

### Mutating an existing `RangeHighlighter`

The base `RangeHighlighter` interface exposes the getter
`getTextAttributes(EditorColorsScheme?)` but **not** the setter – the setter
lives on `RangeHighlighterEx`. The cast always succeeds in practice
(`MarkupModelImpl` returns `RangeHighlighterImpl` which implements `Ex`):

```kotlin
import com.intellij.openapi.editor.ex.RangeHighlighterEx

(highlighter as RangeHighlighterEx).setTextAttributes(newAttrs)
```

This is how the hit-line pulse animation updates the background colour each
tick without rebuilding the highlighter.

Same trap with `setErrorStripeMarkColor` / `setErrorStripeTooltip` – getters
take an optional `EditorColorsScheme` parameter, so Kotlin won't expose them
as properties. Use the explicit setter methods:

```kotlin
highlighter.setErrorStripeMarkColor(bg)
highlighter.setErrorStripeTooltip("Watchpoint hit...")
```

### `Alarm` deprecation

The no-arg `Alarm()` constructor is deprecated in 2025.1 (preference is
coroutines). Use `Alarm(project)` so the alarm gets cleaned up automatically
when the project closes mid-animation. Disposing manually via
`Disposer.dispose(alarm)` is still needed for short-lived alarms not parented
to a project lifecycle.

### XValueNodeImpl tree walking

```kotlin
private fun calculateFullPath(node: XValueNodeImpl): String {
    val parts = LinkedList<String>()
    var current: XValueNodeImpl? = node
    while (current != null) {
        current.name?.takeIf { it.isNotEmpty() }?.let { parts.addFirst(it) }
        current = current.parent as? XValueNodeImpl  // null when we hit the root container
    }
    return parts.joinToString(".")
}
```

Used by `AddWatchpointAction` to recover `"obj.a.b"` from a click on a leaf
`"b"`.

## sitecustomize injection pattern

The injected `sitecustomize.py` is the heredoc inside
`DebugWithWatchpointAction.injectViaSiteCustomize`. Key invariants:

```python
if os.environ.get('PYCHARM_WATCHPOINT_ACTIVE') == '1':
    _wp_mod = types.ModuleType('watchpoint')   # register before exec
    sys.modules['watchpoint'] = _wp_mod         # so WatchpointHit.__module__ == 'watchpoint'
    exec(base64.b64decode('<...>').decode(), _wp_mod.__dict__)
```

The `__module__` matters because the IDE-side exception breakpoint is
registered with the fully-qualified name `"watchpoint.WatchpointHit"`. If
you exec into `globals()` of `sitecustomize` (no synthetic module), the
class's `__module__` becomes `"sitecustomize"` and the breakpoint never
matches.

The temp dir convention is `pycharm_watchpoint_XXX/` – `cleanAllConfigurations`
identifies stale leftovers by that prefix.

## Hit-line decorations (`WatchpointHitHighlighter`)

When the debugger pauses for a watchpoint hit, the user needs to know WHY –
the pause itself is silent because the runtime goes through pydevd's settrace
flow (not an exception). This listener provides that signal.

Per-session listener registered from `WatchpointDebugListener.processStarted`.
On every `sessionPaused`:

1. Query `_pycharm_consume_last_hit()` (Python builtin, see Python `CLAUDE.md`)
   – returns base64-encoded UTF-8 of NUL-separated `file\0line\0name\0old\0new`
   or `""` if the pause wasn't a watchpoint hit. The runtime sets and clears
   this field with consume-once semantics, so a plain breakpoint pause sees `""`.
2. Decode, open the change-site file without stealing focus, install:
   - **Line highlighter** with a base pale-yellow background and a coloured
     gutter scrollbar mark (`setErrorStripeMarkColor` + `setErrorStripeTooltip`).
   - **Inline hint** at end of line: `← watchpoint 'name' fired: old → new`,
     rendered via a tiny custom `EditorCustomElementRenderer` (we own the
     renderer because the platform's `HintRenderer` has moved packages
     between releases).
   - **Pulse animation**: 3s decaying-amplitude amber pulse on the line
     background. Sinusoidal intensity * linear envelope. Implemented via an
     `Alarm(project)` that re-schedules itself every 60ms and lands back on
     the static base colour when elapsed >= `pulseTotalMs`.
3. On `sessionResumed` / `sessionStopped`, clear everything.

**Critical evaluator gotcha** – not in the generic IntelliJ docs:

The `XDebuggerEvaluator.evaluate(...)` path returns a `PyDebugValue` whose
`value` field is the **variables-panel display string**, truncated to
`PyDebugValue.MAX_VALUE` (~256 chars). Base64-encoded payloads routinely
exceed that – even a single file path on macOS test trees can blow past – and
decode silently fails halfway through. Use the underlying

```kotlin
debugProcess.evaluate("_pycharm_consume_last_hit()",
                      /* execute = */ false,
                      /* doTrunc = */ false)
```

instead. This call is synchronous and goes through pydevd's protocol, so it
must run off the EDT (the listener uses `executeOnPooledThread` for the eval
and `invokeLater` to hop back to EDT for the UI work).

**Cleanup gotcha** – `XDebugSessionListener.sessionStopped` does **not**
reliably fire on hard "Stop debug" (the red button). The platform tears the
session down before delivering the event. To survive that, the listener
exposes a public `dispose()` that sets a `@Volatile disposed` flag (every
entry point early-returns) and queues `clearHighlightInternal` via
`invokeLater(runnable, ModalityState.any())` – the `ModalityState.any()` is
load-bearing because shutdown can put up a modal dialog that would otherwise
swallow the cleanup. `dispose()` is called from
`WatchpointDebugListener.processStopped` **before** the session listener is
removed.

## Variables-panel highlighting (`WatchpointTreeCellRenderer` + `WatchpointMarkerService`)

`WatchpointMarkerService` is a project-scoped service holding the set of
currently-armed watch expressions (full dotted paths like `request.user.email`).
Populated by `AddWatchpointAction` on right-click success, cleared on
`processStarted` so stale paths from a previous session don't ghost-highlight.

`WatchpointTreeCellRenderer` wraps the platform's default `TreeCellRenderer`.
Installed lazily by `AddWatchpointAction` the first time a watch is armed
(`tree.cellRenderer = WatchpointTreeCellRenderer(currentRenderer, service)`;
idempotent via instance-of check).

For each cell, it:
1. Computes the row's full dotted path by walking `XValueNodeImpl.parent`
   (same logic as `AddWatchpointAction.calculateFullPath`).
2. If the path is in the service, decorates the rendered component:
   - **Icon swap** → `WatchpointIcons.Watch` (applied **always**, even on
     selected rows; the icon doesn't conflict with selection colour).
   - **Tint background** (pale yellow / muted amber) → applied **only when
     not selected**; on a selected row the IDE's selection colour wins.

**SimpleColoredComponent gotcha** – not visible in the public IntelliJ docs:

`SimpleColoredComponent.iterator()` returns its `ColoredIterator` impl (the
specific inner class, not just `Iterator<String>`). `ColoredIterator` exposes
`setTextAttributes(SimpleTextAttributes)` to mutate an individual fragment's
styling **in place** – no need to clear and re-append the row to recolour the
name fragment. We aren't currently using this (icon-swap was enough signal),
but it's the right hook if you ever need to bold/colour a fragment.

**Persistence model** – the renderer survives tree refreshes (the tree
instance persists across pauses; only data changes), but doesn't survive
session restarts. The service's `clear()` on `processStarted` is what keeps
this honest.

**Out of scope, intentionally**: highlighting variables that were `watch()`-ed
from user code rather than via right-click. To support that, the renderer
would need to poll `_pycharm_list_watches()` on each `sessionPaused` and
sync the service. The hook is easy to add but no one has asked for it.

## Custom icons (`WatchpointIcons` + SVG variants)

Loaded once via `IconLoader.getIcon("/icons/watchpoint.svg", WatchpointIcons::class.java)`.
The platform finds the size/theme variants by filename convention:

| File                          | Resolved when                       |
|-------------------------------|-------------------------------------|
| `watchpoint.svg`              | Classic UI, light theme             |
| `watchpoint_dark.svg`         | Classic UI, dark theme              |
| `watchpoint@20x20.svg`        | New UI, light theme                 |
| `watchpoint@20x20_dark.svg`   | New UI, dark theme                  |

**Dark-variant fill is mandatory** – the SVG path defaults to black, which is
invisible on a dark background. The dark variants set `fill="#AFB1B3"` (a
neutral grey that reads on dark themes). Without the dark variant the icon
disappears entirely on dark themes; the IDE doesn't auto-invert generic SVGs.

**Toolbar-action icon stomp** – this one cost a session to find:

If an `AnAction` overrides `update(e)`, **every** toolbar refresh
(~sub-second) calls it. If `update()` sets `e.presentation.icon = ...`, that
value clobbers whatever `icon="..."` in plugin.xml installed. Symptom:
toolbar shows the custom icon for ~1 second on IDE start, then snaps back to
whatever fallback the action's `update()` hard-coded. Fix: reference the
plugin's `WatchpointIcons.Watch` field from inside `update()` too, not just
plugin.xml. See `DebugWithWatchpointAction.update()` for the pattern.

## Things to do, things to avoid

### When adding a new action

- Decide which action group it belongs to. For debug-session-paused actions,
  `XDebugger.ValueGroup` (variable right-click) or `XDebugger.ToolWindow.LeftToolbar`.
- For Variables-panel actions extend `XDebuggerTreeActionBase`, override
  `update()` to gate visibility and `perform(node, nodeName, e)` to act.
- For toolbar actions extend `AnAction`, implement `DumbAware`.

### When you need a new IntelliJ API

The bytecode-extraction trick saves a lot of guessing:

```bash
# Find which jar contains a class.
cd "/Applications/PyCharm CE 4.app/Contents/plugins/python-ce/lib"
find . -name "*.jar" -exec sh -c 'unzip -l "$1" 2>/dev/null | grep -q "<ClassName>.class" && echo "$1"' _ {} \;

# Extract and inspect.
unzip -p <jar> path/to/<ClassName>.class > /tmp/x.class
javap -p /tmp/x.class
```

Field names with `my` prefix (e.g. `myNotifyOnTerminate`) are JetBrains' Java
convention; their setters drop the prefix (`setNotifyOnTerminate`). Kotlin
sees the setter as a property: `props.isNotifyOnTerminate = true`.

### When changing the runtime

After editing `src/main/resources/python/watchpoint.py`, you **must** rebuild
the plugin (or at least re-run `processResources` + relaunch the IDE
sandbox). The runtime is base64-encoded into the resource jar at build time;
hot-reload doesn't reach it.

### When the user reports a debugger-side bug

First questions to answer (Python `CLAUDE.md` has these in detail):

1. Is `_pycharm_watchpoint_diag()` returning a debugger instance?
2. Are the `[WATCHPOINT] hit ...` lines showing up in the Debug Console?
3. Is the user paused inside `urllib/parse.py` with a `<thread ...>` XML
   string as a local? That's pydevd's protocol-encoding chain – means
   something is calling `do_wait_suspend` directly. The runtime is
   structured to **never** do that; if you see it back, look for new
   direct calls to `py_db.do_wait_suspend(...)`.

### When you bump PyCharm version

- Update `pycharmCommunity("2025.1")` in `build.gradle.kts`.
- Update `untilBuild` to match.
- Re-extract `PyExceptionBreakpointProperties.class` and `javap` it – the
  field set has shifted across versions before; if it shifts again,
  `WatchpointDebugListener.addWatchpointHitBreakpoint` is the only Kotlin
  file that touches it.

## What the listener / action assume about pydevd

- The bundled pydevd is reachable as `import pydevd` from user code (see
  Python `CLAUDE.md` §"The pydevd pause"). PyCharm 2025.1 ships pydevd that
  exposes the APIs we use for the pause flow:
  - `CMD_STEP_OVER` (in `_pydevd_bundle.pydevd_comm_constants`)
  - `STATE_RUN` and `PYTHON_SUSPEND` (in `_pydevd_bundle.pydevd_constants`)
  - `set_trace_for_frame_and_parents`, `trace_dispatch` (on the PyDB instance)
  - `_pydevd_bundle.pydevd_constants.GlobalDebuggerHolder` (for debugger lookup)

  Our pause flow uses the same scoped-step-over mechanism as
  `pydevd.settrace(stop_at_frame=user_frame)` – setting `step_cmd =
  CMD_STEP_OVER` + `step_stop = user_frame` and letting pydevd's tracer
  fire the actual pause when a LINE event lands on `user_frame`.

  We deliberately do NOT use the two more "obvious" alternatives, even
  though they look simpler:
  - **`py_db.do_wait_suspend(...)` called directly from our code.** That
    blocks inside our `<string>`-exec'd frame, which puts pydevd's protocol-
    encoding (urllib.parse.quote) on top of the user thread's stack. The
    IDE shows `urllib/parse.py` as the topmost stopped-at frame with our
    code as `<frame not available>` underneath.
  - **`py_db.set_suspend(thread, CMD_THREAD_SUSPEND, is_pause=True)` +
    `state = STATE_SUSPEND`.** That sets up "pause on the next event in
    ANY frame", which means the suspend latches on the FIRST `trace_dispatch`-
    armed frame pydevd encounters as code resumes – including stdlib codec
    frames in pydevd's stdout-interception chain
    (`codecs.BufferedIncrementalDecoder.decode` with
    `self = <encodings.utf_8.IncrementalDecoder>`) if the user's next line
    contains a `print` or any I/O. The container-mutation path "appeared to
    work" with this approach only because the next event after a
    `.append(...)` was usually a LINE event in the same loop – no
    intervening stdlib code. Both alternatives took multiple debug sessions
    to unwind; the rationale is in Python `CLAUDE.md` §"The pydevd pause –
    tread carefully" and the corresponding anti-pattern entries.

  If a future PyCharm version refactors any of these APIs, the **Python**
  side needs the fix, not the plugin.
- The `WatchpointHit` exception breakpoint is a **safety net** – the
  pause-via-pydevd path doesn't raise, so the breakpoint should never
  fire in normal operation. Don't remove it: in the rare case
  `_pause_via_pydevd` returns early (e.g. `info.is_tracing` was already
  set), the runtime's no-pydevd fallback raises, and the breakpoint
  catches it.

## Logger notes

`Logger.getInstance(<KClass>::class.java).warn(...)` – use `warn` (not `info`)
for anything we want to see in `idea.log` during development. PyCharm's
default log level skips `info`. Don't use `error` for non-error events; it
shows a red notification balloon to the user.

## Known rough edges to keep in mind

- Long-running debug sessions accumulate `_local_watches` / `_attr_watches`
  entries for unwatched-but-not-cleared objects. There's no auto-cleanup
  yet; users should call `unwatch` explicitly. Future work: scan for
  dead-referent attr watches on session end.
- The `AddWatchpointAction` notifies via
  `NotificationGroupManager.getNotificationGroup("Debugger messages")` –
  this group is always registered, but if PyCharm renames it the
  notifications will silently fail. We don't currently fall back.
- No tests on the Kotlin side yet. Adding `Test` framework wiring with
  `intellijPlatform.testFramework(TestFrameworkType.Platform)` is already
  in `build.gradle.kts`; tests would go under `src/test/kotlin/`.

## Where to go for runtime details

Anything Python: `src/main/resources/python/CLAUDE.md`. Key sections to
re-read before touching the runtime:

- "Design contract" – the 10 invariants. #8 covers cross-function watch
  propagation (CALL → queue → PY_START → arm-on-callee-param); #9 covers
  container-mutation watchers (`_WatchedList`/`Dict`/`Set` wrap-and-replace)
  + recursive object-wide instrumentation (`_instrument_object_tree`
  walking nested attrs to depth 4); #10 covers the classpatch fallback
  for hostile metaclasses (Django Model / SQLAlchemy declarative-base)
  where dynamic subclassing fails and we monkey-patch `cls.__setattr__`
  scoped to the watched instance via a per-class `instance_watches`
  table keyed by `id(obj)`.
- "The pydevd pause – tread carefully" – the urllib trap.
- "Things you might be tempted to do, but shouldn't" – the anti-pattern
  list. Several entries directly correspond to bugs we lived through in
  earlier sessions.
- "Known limitations" – behaviors that are intentionally off (`del attr`,
  `__dict__` bypass, interned-primitive over-watch, container aliases
  captured before watch-arm, recursion depth cap, slotted-class skip,
  etc.) and have regression tests; don't "fix" them without re-reading
  the rationale.
