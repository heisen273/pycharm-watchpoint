# pythonwatchpoint – PyCharm plugin handoff notes (trimmed)

> Full version: `CLAUDE.md`. This is the compact reference. Read the full version
> before architectural changes.

## Quick start

```bash
./gradlew compileKotlin     # build
./gradlew runIde            # sandbox PyCharm with plugin loaded
./gradlew buildPlugin       # → build/distributions/pythonwatchpoint-1.0.0.zip
```

## Build pins

`gradle.properties`:
- **`org.gradle.java.home` MUST point at JDK 21** (or ≤23). JDK 25 breaks the Gradle 8.13 bundled Kotlin compiler with `IllegalArgumentException: 25.0.1` (its `JavaVersion.parse()` predates that format). Symptom: `BUILD FAILED in 3s`.
- **Gradle wrapper 8.13** – don't downgrade below 8.10.2.

`build.gradle.kts`:
- `org.jetbrains.kotlin.jvm` **2.2.0** + `org.jetbrains.intellij.platform` **2.16.0**
- Target: `pycharmCommunity("2023.3")` + `bundledPlugin("PythonCore")`
- Source / target: **Java 17** bytecode (`tasks.withType<KotlinCompile>` + `tasks.withType<JavaCompile>` with `options.release.set(17)`), Kotlin toolchain still JBR 21.
  - `jvmToolchain(21)` overrides the `kotlin { compilerOptions }` block and the `java { targetCompatibility }` extension in KGP 2.x – the only reliable override is configuring both compile tasks directly.
  - Java 17 class files (version 61.0) load in PyCharm 2023.x (JBR 17) through 2026.x (JBR 21). Do **not** raise to 21 – breaks older sandboxes.
- `sinceBuild = "231"`, `untilBuild = "261.*"` – keep in sync with tested PyCharm version

## Long-running daemon trap

IntelliJ holds a Gradle daemon when the project is open – can cause `LockTimeoutException`. Either close IntelliJ first, run `./gradlew --stop`, or build from inside IntelliJ. **Do NOT `rm -rf .gradle`** – forces re-downloading ~600 MB PyCharm CE distribution.

## Source layout

```
src/main/kotlin/com/pythonwatchpoint/
├── services/
│   ├── WatchpointSessionManager.kt    # Carries py source from action → listener
│   └── WatchpointMarkerService.kt     # Tracks armed-watch expressions for tree renderer
├── listeners/
│   ├── WatchpointDebugListener.kt     # processStarted/processStopped hooks
│   ├── WatchpointHitHighlighter.kt    # Per-session: line highlight + pulse + inline hint on hit
│   ├── WatchpointTreeCellRenderer.kt  # Wraps Variables-panel cell renderer to mark watched rows
│   ├── WatchpointFrameSync.kt         # Per-pause + on-arm cross-frame watch sync (object identity)
│   └── WatchpointUiUtil.kt            # Reflective Variables-tree lookup + split-mode detection
├── actions/
│   ├── DebugWithWatchpointAction.kt   # Toolbar: clone run config + inject
│   └── AddWatchpointAction.kt         # Variables-panel right-click
└── icons/
    └── WatchpointIcons.kt             # Two Icon fields (Watch + DebugWatch)

src/main/resources/
├── META-INF/plugin.xml
├── icons/                             # Plugin-owned SVGs (light/dark × 16px/20px)
└── python/                            # Bundled watchpoint runtime
    ├── _pycharm_watchpoint/           # Runtime package (minified into the jar at build)
    │   ├── __init__.py                # singleton + public API + builtins publishing + WatchpointHit rebrand
    │   ├── constants.py               # version guard + cross-module mutable globals
    │   ├── hit.py                     # WatchpointHit (leaf)
    │   ├── helpers.py  caller.py      # repr/log/hashing; frame-walk + filename classification
    │   ├── pydevd_pause.py            # debugger lookup, bp install, pause mechanisms
    │   ├── watch_data.py  containers.py  classpatch.py
    │   └── registry.py                # WatchpointRegistry + _setup_monitoring
    ├── tests/                         # themed pytest modules + util.py (shared helpers)
    ├── conftest.py
    ├── pydevd_boost.py
    └── CLAUDE.md                      # Runtime-side handoff
```

The runtime is a real package (was one monolithic `watchpoint.py`). Submodules
import each other downward (no cycles); the three cross-module **mutable** globals
(`_WATCHPOINT_LOG`, `_TOOL_ID`, `_installing_watch_thread`) live in `constants.py`
and are accessed as `constants.X` (never `from .constants import X`, which would
snapshot the value). `WatchpointHit` lives in `hit.py` but `__init__.py` rebrands
`WatchpointHit.__module__ = __name__` so the IDE exception breakpoint match
(`_pycharm_watchpoint.WatchpointHit`) still works. `__init__.py` re-exports the
whole internal surface so `from _pycharm_watchpoint import <anything>` works.

The ordered list of submodules that ship is `DebugWithWatchpointAction.MODULE_FILES`
(single source of truth for both injection paths) – add a line there when adding a
submodule, and `build.gradle.kts` minifies every `*.py` in the package dir.

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

Action icon attributes reference `WatchpointIcons.DebugWatch` / `WatchpointIcons.Watch`.
Icons must be `@JvmField val` – without the annotation, Kotlin generates a getter and plugin.xml can't resolve it.

Action groups:

| Group ID                            | Used for                              |
| ----------------------------------- | ------------------------------------- |
| `XDebugger.ToolWindow.TopToolbar`   | "Debug with Watchpoint" toolbar icon  |
| `MainToolbarRight`                  | Main IDE toolbar (same action)        |
| `XDebugger.ValueGroup`              | Variables panel right-click           |

## Architecture flow

### "Debug with Watchpoint" path

1. `DebugWithWatchpointAction.actionPerformed`:
   - `cleanAllConfigurations(project)` – pre-filters dirty configs, then wraps mutations in `WriteAction` to avoid read-access assertions on 2024+. Strips leftover `PYCHARM_WATCHPOINT_ACTIVE` env + temp-dir `PYTHONPATH` from saved configs. Skips entirely if no configs need cleaning (avoids spurious "Settings Modified" prompts).
   - `loadWatchpointPackage()` reads each `MODULE_FILES` entry from `/python/_pycharm_watchpoint/` into a `filename -> source` map.
   - `WatchpointSessionManager.startSession(pkg)` stashes the map.
   - **Clones** the currently-selected run config (does NOT mutate the user's saved config), renames to `"[WATCHPOINT] <original>"`.
   - `injectViaSiteCustomize(clonedConfig, pkg)`:
     - Writes each submodule to `<tempdir>/_pycharm_watchpoint/<name>.py` and a `sitecustomize.py` that does `import _pycharm_watchpoint` (the temp dir is on `PYTHONPATH`, so normal import + relative imports work; no base64/exec for the runtime – boost is still base64-exec'd).
     - Sets `PYTHONPATH = <tempdir>:<existing>`, `PYCHARM_WATCHPOINT_ACTIVE=1`, `PYCHARM_WATCHPOINT_USER_ROOTS=<project.basePath>`.
   - `ProgramRunnerUtil.executeConfiguration(...)`.

2. `WatchpointDebugListener.processStarted`:
   - Purges stale entries from `breakpoints` / `highlighters` maps for dead processes that never got `processStopped` (crash resilience).
   - Consumes the queued package map from session manager.
   - Registers Python exception breakpoint for `_pycharm_watchpoint.WatchpointHit` (safety net).
   - Exponential-backoff retry (300ms initial, ×1.5, up to 5 attempts) waiting for evaluator readiness, then probes `hasattr(builtins, ...)`. If sitecustomize didn't load, the fallback evaluates a command that writes the base64-embedded package files to a fresh `tempfile.mkdtemp()`, inserts it on `sys.path`, and `import _pycharm_watchpoint` (mirrors the sitecustomize path – real files, not an exec-into-one-dict blob).

3. `WatchpointDebugListener.processStopped`:
   - Removes the exception breakpoint.

### "Add Python Watchpoint" path

1. User pauses at a breakpoint, right-clicks a variable.
2. `AddWatchpointAction.perform`:
   - `calculateFullPath(node)` – walks `XValueNodeImpl` parents to get `"request.user"` from leaf `"user"`.
   - Reads `session.currentStackFrame.sourcePosition.file.path` and `(currentStackFrame as PyStackFrame).name`.
   - Evaluates `_pycharm_watch_at('<path>', '<file>', '<func>')`.
   - Notifies via `NotificationGroupManager → "Debugger messages"`.

## IntelliJ Platform API cheatsheet

### Evaluating Python during a paused session

```kotlin
// (A) Callback-based, EDT-friendly – result.value truncated to ~256 chars
debugProcess.evaluator?.evaluate(expr, object : XDebuggerEvaluator.XEvaluationCallback {
    override fun evaluated(result: XValue) {
        val raw = (result as? PyDebugValue)?.value  // NOT result.toString() – that's the expression text
    }
    override fun errorOccurred(errorMessage: String) { }
}, null)

// (B) Synchronous, no truncation – off-EDT only (wrap in executeOnPooledThread)
val pyValue = debugProcess.evaluate(expr, false, false)
// pyValue.value is the full untruncated string
```

**The toString-vs-value trap**: `PyDebugValue.toString()` returns the EXPRESSION text (name shown in Variables tree), NOT the value. Always use `.value`. Use path (B) for base64 payloads – they routinely exceed 256 chars.

The evaluator runs in the user's paused frame's globals/locals context, but `sys._getframe()` sees pydevd's stack, not the user's. Pass file path + function name and let Python find the frame via `sys._current_frames()`.

### Refreshing the Variables panel

```kotlin
val session = XDebuggerManager.getInstance(project).currentSession as? XDebugSessionImpl ?: return
val variablesView = session.sessionTab?.variablesView ?: return
variablesView.processSessionEvent(XDebugView.SessionEvent.FRAME_CHANGED, session)
```

**Do NOT use `session.rebuildViews()`** – it dispatches `FRAME_CHANGED` to ALL debug views including the Frames panel, which resets the selected stack frame to the topmost one, discarding the user's scroll position.

**Split-debugger mode (2025.2+):** `getSessionTab()` and `getRunContentDescriptor()` both call `Logger.error()` when split mode is active, producing a user-visible error balloon even though they still return their values. Pre-check split mode before calling either method. The API to detect split mode **changed between versions**:

- 2025.2: `XDebugSessionProxy.Companion.useFeProxy()` (no `SplitDebuggerMode` class yet)  
- 2026.1: `SplitDebuggerMode.isSplitDebugger()` (new dedicated class in `com.intellij.xdebugger`)

```kotlin
private fun isSplitDebuggerMode(): Boolean {
    // 2026.1+: dedicated class
    runCatching {
        val cls = Class.forName("com.intellij.xdebugger.SplitDebuggerMode")
        return cls.getMethod("isSplitDebugger").invoke(null) as? Boolean ?: false
    }
    // 2025.2 fallback: XDebugSessionProxy companion
    return runCatching {
        val proxyClass = Class.forName("com.intellij.xdebugger.impl.frame.XDebugSessionProxy")
        val companionField = proxyClass.getDeclaredField("Companion")
        companionField.isAccessible = true
        val companion = companionField.get(null)
        companion.javaClass.getMethod("useFeProxy").invoke(companion) as? Boolean ?: false
    }.getOrDefault(false)
}
```

Falls back to `false` on older builds where neither class exists. When `isSplitDebuggerMode()` returns true, skip the `sessionTab` path and call `session.rebuildViews()` directly. Note: `PyDebugRunner.execute()` (PyCharm's own code) also calls `getRunContentDescriptor()` internally – that split-mode error comes from PyCharm itself and cannot be suppressed by the plugin.

Call inside `invokeLater { ... }`.

`getVariablesView()` is `@ApiStatus.Internal` and was added after 2024.3 – call it **reflectively** so the plugin compiles on 2023.x. `getSessionTab()` is also `@Internal` but exists on all target builds. Pattern:

```kotlin
@Suppress("UnstableApiUsage")
private fun refreshVariablesView(project: Project) {
    val session = XDebuggerManager.getInstance(project).currentSession as? XDebugSessionImpl ?: return
    val refreshed = runCatching {
        val sessionTab = session.sessionTab ?: return@runCatching false
        val view = sessionTab.javaClass.getMethod("getVariablesView").invoke(sessionTab)
            ?: return@runCatching false
        view.javaClass.getMethod("processSessionEvent",
            XDebugView.SessionEvent::class.java,
            com.intellij.xdebugger.XDebugSession::class.java)
            .invoke(view, XDebugView.SessionEvent.FRAME_CHANGED, session)
        true
    }.getOrDefault(false)
    if (!refreshed) session.rebuildViews()  // fallback on 2023.x
}
```

### Mutating a RangeHighlighter

`RangeHighlighter` has the getter but not the setter. The setter is on `RangeHighlighterEx`:
```kotlin
import com.intellij.openapi.editor.ex.RangeHighlighterEx
(highlighter as RangeHighlighterEx).setTextAttributes(newAttrs)
```
`setErrorStripeMarkColor` / `setErrorStripeTooltip` are explicit setter methods (not Kotlin properties).

### Alarm deprecation

No-arg `Alarm()` is deprecated in 2025.1. Use `Alarm(project)` so the alarm is cleaned up automatically when the project closes.

### WatchpointMarkerService – frame-scoped keys + cross-frame synced set

Watches are stored as `WatchKey(expression: String, frameId: Long)` where `frameId` is the Python `id(frame)` returned by `watch_at()` (== pydevd's `PyStackFrame.frameId`, confirmed via pydevd `find_frame`: `if lookingFor == id(frame)`). This prevents a watch on `"self"` from lighting up every row named `"self"` across all threads and stack frames.

Two independent sets:
- **`watched`** – user-armed entries. `add(expression, frameId)` arms; `remove(expression)` disarms by expression only (the frame is already gone by remove time). Persistent until unwatch / session start, so a freshly-armed row keeps its icon the instant it's armed, before any sync runs.
- **`syncedFrames`** – the authoritative cross-frame set, **replaced wholesale** by `replaceSynced(set)` from a successful `_pycharm_locate_watches()` read (see [WatchpointFrameSync](#watchpointframesync--cross-frame-icon-by-object-identity)). Never touched on a failed/`ERROR:` read, so a transient evaluator hiccup can't wipe icons.

`isWatched(expression, frameId)` returns true if the key is in **either** set. `clear()` (called on `processStarted`) clears both. There is **no** name-only `isWatched(expression)` lookup any more – it was removed (along with the unused `expressionCounts` index) because name-only matching can't tell "same watched object, other frame" from "new object, same name" (the ghost-icon bug). All matching is `(expression, frameId)` or type-sniff.

The `WatchpointTreeCellRenderer` and `AddWatchpointAction` use a **two-pronged, object/frame-bound** check (NOT name-only):
1. Frame-scoped marker (`isWatched(path, frameId)`) – matches armed OR synced entries for the frame currently shown.
2. Type-sniff on `PyDebugValue.type.startsWith("_Watched")` – class-surgery watches, whose mutated type travels with the object into every frame.

### WatchpointFrameSync – cross-frame icon by object identity

The icon follows the watched **object** (by `id()`) into caller/callee frames, not just the frame it was armed in – without the ghost that the old name-only fallback caused. The runtime answers the identity question (Kotlin can't: `PyDebugValue` carries no Python `id()`).

`WatchpointFrameSync.refresh(project, debugProcess, markerService, isCancelled)`:
- Off-EDT (pooled thread), evaluates `_pycharm_locate_watches()` (full payload, `doTrunc=false`), which returns every live `(name, id(frame))` pair across the whole stack (live `_local_watches` keys + an identity scan of `sys._current_frames()` for `_AttributeWatch._obj_ref` objects).
- **Replace-on-success-only**: parses, calls `markerService.replaceSynced(...)`, then repaints the current tree via `WatchpointUiUtil.currentVariablesTree`. Skips silently on eval exception / `ERROR:` prefix / malformed base64 → never wipes. Empty payload is trusted ("nothing watched").

Driven from two places, both while paused:
- `WatchpointHitHighlighter.sessionPaused` – every pause/step (passes `{ disposed }` as `isCancelled`).
- `AddWatchpointAction` arm **and** remove callbacks – **the instant a watch is (un)armed**. Arming doesn't fire `sessionPaused`, so without this the caller ("past") frames already on the stack wouldn't get the icon until the next step. `decorateNode` alone only marks the armed frame.

This is the **safe** version of the previously-failed evaluator sync (the old attempt wiped Kotlin state on a single eval timeout). Payload encoding: base64 of UTF-8, records separated by U+0001, each record `name`+U+0000+`frameId` (mirrors the hit-payload convention).

### Stale WatchpointHit breakpoint cleanup

`addWatchpointHitBreakpoint` in `WatchpointDebugListener` sweeps for any persisted `WatchpointHit` breakpoints from crashed sessions or older plugin versions (old name was `watchpoint.WatchpointHit` without the underscore prefix) before registering the new one:

```kotlin
val existing = manager.getBreakpoints(type)
for (bp in existing.toList()) {
    if ("WatchpointHit" in (bp.properties?.exception ?: "")) {
        manager.removeBreakpoint(bp)
    }
}
```

### Notifications – reflection required on 2023.x

`com.intellij.notification` may not be on the compile classpath when building against 2023.x SDKs under Gradle IntelliJ Platform plugin 2.x. All notification calls go through `notifyError(project, message)` which resolves `NotificationGroupManager` and `NotificationType` fully via `Class.forName` + reflection at runtime. Falls back to `logger.warn()` on any exception.

### Exception-breakpoint API

```kotlin
val type = XBreakpointType.EXTENSION_POINT_NAME.findExtensionOrFail(PyExceptionBreakpointType::class.java)
val bp = WriteAction.computeAndWait {
    XDebuggerManager.getInstance(project).breakpointManager.addBreakpoint(type, props)
}
```
`PyExceptionBreakpointProperties(exceptionName)` constructor; key fields: `isNotifyOnTerminate`, `myNotifyOnlyOnFirst`, `myIgnoreLibraries`.

### XValueNodeImpl tree walking

```kotlin
private fun calculateFullPath(node: XValueNodeImpl): String {
    val parts = LinkedList<String>()
    var current: XValueNodeImpl? = node
    while (current != null) {
        current.name?.takeIf { it.isNotEmpty() }?.let { parts.addFirst(it) }
        current = current.parent as? XValueNodeImpl
    }
    return parts.joinToString(".")
}
```

### Cloning a run configuration

```kotlin
val clonedConfig = originalConfig.clone() as AbstractPythonRunConfiguration<*>
clonedConfig.name = "[WATCHPOINT] ${originalConfig.name}"
val newSettings = runManager.createConfiguration(clonedConfig, selectedSettings.factory)
newSettings.isTemporary = true
ProgramRunnerUtil.executeConfiguration(newSettings, DefaultDebugExecutor.getDebugExecutorInstance())
```

## sitecustomize injection pattern

The runtime is now a package written to disk, not a base64 blob exec'd into one
module dict. `injectViaSiteCustomize` writes `<tempdir>/_pycharm_watchpoint/*.py`
and a sitecustomize that imports it:

```python
if os.environ.get('PYCHARM_WATCHPOINT_ACTIVE') == '1':
    import _pycharm_watchpoint   # tempdir is on PYTHONPATH; real import machinery
```

The `__module__` of `WatchpointHit` must be `"_pycharm_watchpoint"` because the IDE
exception breakpoint is registered as `"_pycharm_watchpoint.WatchpointHit"`. The
class is defined in the leaf `hit.py` (so its natural `__module__` would be
`_pycharm_watchpoint.hit`); `__init__.py` rebrands it with
`WatchpointHit.__module__ = __name__`. Importing the real package as
`_pycharm_watchpoint` makes `__name__` resolve correctly in both prod and tests.

The package dir is named `_pycharm_watchpoint` (not `watchpoint`) to avoid colliding
with user packages. If you rename it, update: the source dir, sitecustomize bootstrap,
fallback injection (writes the same dir name), exception breakpoint name, the
`__init__.py` rebrand, and the framework denylist in `helpers.py`.

## Hit-line decorations (`WatchpointHitHighlighter`)

On every `sessionPaused`, queries `_pycharm_consume_last_hit()` – returns base64-encoded UTF-8 of NUL-separated `file\0line\0name\0old\0new\0caller_file\0caller_line` (7 fields) or `""` if not a watchpoint hit. The pooled-thread eval is guarded by `session.isStopped` check before the blocking call – prevents indefinite hangs when the session dies between dispatch and execution.

**Two-phase highlight** (critical – do not collapse into one):
- **Phase 1 (immediate via `invokeLater`)**: open the mutation file + install decorations:
  - Primary line highlighter (pale-yellow background + gutter stripe).
  - Inline hint: `← watchpoint 'name' fired: old → new` via custom `EditorCustomElementRenderer`.
  - Pulse animation: 1.5s decaying amber pulse (exponential decay). `Alarm(project)` every 60ms.
  - Secondary "call-site" highlight at `caller_file`/`caller_line` – subtler, no pulse, indicates which call led to the mutation.
- **Phase 2 (after 150ms `Alarm`)**: re-select the mutation file's tab + scroll to hit line. The delay lets the IDE's post-pause focus settle first so our tab selection wins.

**Why two phases**: PyCharm's post-pause UI focuses on the pause-anchor file (the bp's file), NOT the mutation file. If we jump to the mutation file immediately, the IDE wins (runs later) and our file flashes for ~20ms before snapping back. Phase 2's 150ms delay ensures our tab selection runs last. If feedback says it's still flashy, increase the delay.

`focusEditor=false` is deliberate – selects the tab without keyboard focus. Using `true` puts a blinking caret on the hit line, which feels wrong for a debugger pause.

**Cleanup gotcha**: `sessionStopped` doesn't reliably fire on hard "Stop debug". The highlighter exposes a public `dispose()` (sets `@Volatile disposed` flag, clears highlights via `invokeLater(runnable, ModalityState.any())`). `ModalityState.any()` is load-bearing – shutdown can put up a modal dialog that would swallow the cleanup.

**Secondary highlight skip conditions**: same file+line as primary; `callerFile` empty or `callerLine == 0`; caller equals mutation line.

## Variables-panel highlighting

`WatchpointMarkerService` – project-scoped service holding two `(expression, frameId)` sets (armed + synced; see [above](#watchpointmarkerservice--frame-scoped-keys--cross-frame-synced-set)). Cleared on `processStarted` to prevent ghost-highlighting from previous sessions.

`WatchpointTreeCellRenderer` – wraps the default renderer. Installed lazily on first watch arm (idempotent via instanceof check). For each cell, computes the row's full dotted path and checks via the **two-pronged** `isWatched` logic (frame-scoped marker incl. synced cross-frame entries → type-sniff). **No name-only fallback** – that was the ghost-icon bug. If matched:
- Swaps icon to `WatchpointIcons.Watch` (always, even on selected rows).
- Tints background pale yellow – **only when NOT selected** (selection colour wins on selected rows).

Cross-frame coverage (the icon following the object into caller/callee frames) comes from `WatchpointFrameSync` repopulating the synced set on every pause and on arm/remove – the renderer itself stays a pure, name-agnostic painter.

**Why the tree renderer survives steps**: the main Variables tree is *reused* across step / frame-switch – `XDebuggerTree` calls `setCellRenderer(...)` once in its constructor; `XVariablesViewBase` keeps one tree and only `setRoot(...)`s on frame change. So a one-time install persists (the earlier "PyCharm rebuilds the tree, orphaning our renderer" theory is wrong for the main panel; genuinely-new trees only appear for detached/split tabs and the Watches view).

## Custom icons

Two icons: `Watch` (spectacles, Variables panel) and `DebugWatch` (bug+spectacles, toolbar).

SVG naming convention (platform resolves automatically):

| File                          | Resolved when                    |
|-------------------------------|----------------------------------|
| `watchpoint.svg`              | Classic UI, light theme          |
| `watchpoint_dark.svg`         | Classic UI, dark theme           |
| `watchpoint@20x20.svg`        | New UI, light theme              |
| `watchpoint@20x20_dark.svg`   | New UI, dark theme               |

**Dark variants require `fill="#AFB1B3"`** – SVG path defaults to black, invisible on dark themes. The IDE does NOT auto-invert generic SVGs.

**Toolbar-action icon stomp**: if `update(e)` sets `e.presentation.icon = ...`, it clobbers plugin.xml's `icon="..."` on every toolbar refresh. Reference `WatchpointIcons.Watch` from inside `update()` too, not just plugin.xml.

## Extracting IntelliJ APIs from bundled jars

```bash
cd "/Applications/PyCharm CE 4.app/Contents/plugins/python-ce/lib"
unzip -p python-ce.jar com/jetbrains/python/debugger/PyExceptionBreakpointProperties.class > /tmp/prop.class
javap -p /tmp/prop.class
```

Field names with `my` prefix (JetBrains Java convention) – setters drop the prefix. Kotlin sees setter as property: `props.isNotifyOnTerminate = true`.

## When adding a new action

- Variables-panel actions: extend `XDebuggerTreeActionBase`, override `update()` + `perform(node, nodeName, e)`.
- Toolbar actions: extend `AnAction`, implement `DumbAware`.

## When changing the runtime

After editing any `_pycharm_watchpoint/` submodule, rebuild the plugin
(`processResources` + relaunch sandbox). Each submodule is minified (pyminify,
`build.gradle.kts`) and written as a real file into the resource jar at build
time; the plugin materializes the package to a temp dir and imports it.

Run the tests from `src/main/resources/python/` with `pytest` (themed modules live
in `tests/`, sharing helpers in `tests/util.py`; `conftest.py` boots the package +
resets state between tests). Bump `_RUNTIME_VERSION` in `caller.py` on behavioral
changes. If `./gradlew runIde` doesn't pick up changes, run `./gradlew clean` first.
Check `_RUNTIME_VERSION` in `/tmp/pythonwatchpoint.log` to confirm the loaded version.

If you add a submodule, also add it to `DebugWithWatchpointAction.MODULE_FILES`.

## When bumping PyCharm version

- Update `pycharmCommunity("...")` + `untilBuild` in `build.gradle.kts`.
- Re-extract `PyExceptionBreakpointProperties.class` with `javap` – field set has shifted across versions; `WatchpointDebugListener.addWatchpointHitBreakpoint` is the only file that touches it.

## What the listener / action assume about pydevd

- The primary pause mechanism is `_install_pause_breakpoint` (real pydevd `LineBreakpoint`s), NOT `_pause_via_pydevd` (CMD_STEP_OVER). See python/CLAUDE.md §13.
- `WatchpointHit` exception breakpoint is a **safety net** – the bp path doesn't raise; the no-pydevd fallback does.
- PEP 669 `DEBUGGER_ID` (= 0) is claimed by pydevd at session start.
- If a future PyCharm version refactors pydevd APIs, the **Python** side needs the fix, not the plugin.
- Hits appearing in PyCharm's Breakpoints panel or phantom pauses after hits → `_temp_breakpoints` cleanup misfiring on the Python side.

## Logger notes

`Logger.getInstance(<KClass>::class.java).warn(...)` – use `warn` (default log level skips `info`). Don't use `error` for non-error events (shows red balloon).

## Known rough edges

- Long debug sessions accumulate `_local_watches` / `_attr_watches` entries for unwatched objects. No auto-cleanup yet.
- `NotificationGroupManager.getNotificationGroup("Debugger messages")` – called via **reflection** (`notifyError()`) so the plugin compiles on 2023.x where `com.intellij.notification` may be absent from the Gradle classpath. Falls back to `logger.warn()` if reflection fails.
- No Kotlin-side tests yet. `intellijPlatform.testFramework(TestFrameworkType.Platform)` is wired in `build.gradle.kts`; tests go under `src/test/kotlin/`.
- `WatchpointHitHighlighter` skips (rather than clamps) when hit line exceeds the file's current line count.
- `WatchpointSessionManager.consumeWatchpointCode()` uses `AtomicReference.getAndSet(null)` for atomicity across concurrent session launches.

## Where to go for runtime details

Everything Python: `src/main/resources/python/CLAUDE.md` or `CLAUDE_trimmed.md`. Critical sections:
- "Design contract" §1–§18 – key invariants (§9 container mutation + recursive instrumentation; §10 classpatch fallback; §13 pause mechanism).
- "The pydevd pause – tread carefully" – the two rules; why CMD_STEP_OVER became a fallback.
- "Anti-patterns" – things that look right but break in subtle ways (many backed by debugging sessions).

## pydevd_boost – debugger performance patches

Separate module (`src/main/resources/python/pydevd_boost.py`) that injects performance
fixes into pydevd's PEP 669 tracing layer. Full docs: `src/main/resources/python/PYDEVD_BOOST.md`.

Key constraints (hard-won lessons – do NOT retry):
- **Cannot wrap monitoring callbacks** (`py_start_callback`, `py_raise_callback`) with Python functions – breaks `_getframe(1)` depth assumptions everywhere.
- **Cannot call `sys.monitoring.register_callback()` after pydevd arms breakpoints** – invalidates local LINE events, breakpoints stop hitting.
- **Must verify module is fully loaded** before patching – Python adds modules to `sys.modules` before executing their body.
- Use `inspect.getsource()` + `exec()` for source-level injection (patch 5) – no extra frame, same depth contract.
