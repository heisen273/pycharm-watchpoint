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
│   └── WatchpointTreeCellRenderer.kt  # Wraps Variables-panel cell renderer to mark watched rows
├── actions/
│   ├── DebugWithWatchpointAction.kt   # Toolbar: clone run config + inject
│   └── AddWatchpointAction.kt         # Variables-panel right-click
└── icons/
    └── WatchpointIcons.kt             # Two Icon fields (Watch + DebugWatch)

src/main/resources/
├── META-INF/plugin.xml
├── icons/                             # Plugin-owned SVGs (light/dark × 16px/20px)
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
   - `cleanAllConfigurations(project)` – strips leftover `PYCHARM_WATCHPOINT_ACTIVE` env + temp-dir `PYTHONPATH` from all saved configs.
   - Loads `watchpoint.py`, base64-encodes it.
   - `WatchpointSessionManager.startSession(code)` stashes the source.
   - **Clones** the currently-selected run config (does NOT mutate the user's saved config), renames to `"[WATCHPOINT] <original>"`.
   - `injectViaSiteCustomize(clonedConfig, code)`:
     - Writes `sitecustomize.py` to a fresh temp dir (`/tmp/pycharm_watchpoint_XXX/`).
     - Sets `PYTHONPATH = <tempdir>:<existing>`, `PYCHARM_WATCHPOINT_ACTIVE=1`, `PYCHARM_WATCHPOINT_USER_ROOTS=<project.basePath>`.
   - `ProgramRunnerUtil.executeConfiguration(...)`.

2. `WatchpointDebugListener.processStarted`:
   - Consumes queued source from session manager.
   - Registers Python exception breakpoint for `_pycharm_watchpoint.WatchpointHit` (safety net).
   - 500ms delay → probes `hasattr(builtins, ...)`. If sitecustomize didn't load, base64+exec the runtime via evaluator as fallback.

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

### WatchpointMarkerService – frame-scoped keys

Watches are stored as `WatchKey(expression: String, frameId: Long)` where `frameId` is the Python `id(frame)` returned by `watch_at()`. This prevents a watch on `"self"` from lighting up every row named `"self"` across all threads and stack frames.

`add(expression, frameId)` – arm.  
`remove(expression)` – disarm (by expression only; frameId not needed on remove).  
`isWatched(expression, frameId)` – exact match.  
`isWatched(expression)` – name-only fallback for rebind-only watches where the frame id has changed.

The `WatchpointTreeCellRenderer` and `AddWatchpointAction.update()` use a three-pronged check:
1. Frame-scoped marker (`isWatched(path, frameId)`) – exact frame.
2. Name-only fallback (`isWatched(path)`) – Django model rebind-only watches.
3. Type-sniff on `PyDebugValue.type.startsWith("_Watched")` – class-surgery watches travelling through middleware frames.

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

```python
if os.environ.get('PYCHARM_WATCHPOINT_ACTIVE') == '1':
    _wp_mod = types.ModuleType('_pycharm_watchpoint')   # register before exec
    sys.modules['_pycharm_watchpoint'] = _wp_mod         # so WatchpointHit.__module__ == '_pycharm_watchpoint'
    exec(base64.b64decode('<...>').decode(), _wp_mod.__dict__)
```

The `__module__` matters because the IDE exception breakpoint is registered as `"_pycharm_watchpoint.WatchpointHit"`. Exec'ing into `globals()` of sitecustomize makes the class's `__module__` become `"sitecustomize"` and the breakpoint never matches.

Module is named `_pycharm_watchpoint` (not `watchpoint`) to avoid colliding with user packages. If you rename it, update: sitecustomize bootstrap, fallback injection, exception breakpoint name, and the framework denylist in `watchpoint.py`.

## Hit-line decorations (`WatchpointHitHighlighter`)

On every `sessionPaused`, queries `_pycharm_consume_last_hit()` – returns base64-encoded UTF-8 of NUL-separated `file\0line\0name\0old\0new\0caller_file\0caller_line` (7 fields) or `""` if not a watchpoint hit.

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

`WatchpointMarkerService` – project-scoped service holding armed watch expressions keyed by `(expression, frameId)`. Cleared on `processStarted` to prevent ghost-highlighting from previous sessions.

`WatchpointTreeCellRenderer` – wraps the default renderer. Installed lazily on first watch arm (idempotent via instanceof check). For each cell, computes the row's full dotted path and checks via the three-pronged `isWatched` logic (frame-scoped → name-only → type-sniff); if matched:
- Swaps icon to `WatchpointIcons.Watch` (always, even on selected rows).
- Tints background pale yellow – **only when NOT selected** (selection colour wins on selected rows).

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

After editing `watchpoint.py`, rebuild the plugin (`processResources` + relaunch sandbox). The runtime is base64-encoded into the resource jar at build time.

If `./gradlew runIde` doesn't pick up changes, run `./gradlew clean` first. Check `_RUNTIME_VERSION` in `/tmp/pythonwatchpoint.log` to confirm the expected version is loaded.

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
