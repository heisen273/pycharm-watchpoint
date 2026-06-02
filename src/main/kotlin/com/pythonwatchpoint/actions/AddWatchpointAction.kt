package com.pythonwatchpoint.actions

import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.actionSystem.DataContext
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.diagnostic.Logger
import com.intellij.openapi.project.Project
import com.intellij.xdebugger.XDebuggerManager
import com.intellij.xdebugger.evaluation.XDebuggerEvaluator
import com.intellij.xdebugger.frame.XValue
import com.intellij.xdebugger.impl.XDebugSessionImpl
import com.intellij.xdebugger.impl.frame.XDebugView
import com.intellij.xdebugger.impl.ui.tree.XDebuggerTree
import com.intellij.xdebugger.impl.ui.tree.actions.XDebuggerTreeActionBase
import com.intellij.xdebugger.impl.ui.tree.nodes.XValueNodeImpl
import com.jetbrains.python.debugger.PyDebugProcess
import com.jetbrains.python.debugger.PyDebugValue
import com.jetbrains.python.debugger.PyStackFrame
import com.pythonwatchpoint.listeners.WatchpointFrameSync
import com.pythonwatchpoint.listeners.WatchpointTreeCellRenderer
import com.pythonwatchpoint.services.WatchpointMarkerService
import java.util.LinkedList

/**
 * Variables-panel right-click action that toggles a watchpoint on the selected
 * variable(s). The same menu entry serves both directions of the toggle and
 * its text reflects the operation it will perform:
 *
 *  - Selection contains at least one un-watched path → label "Add Watchpoint(s)"
 *    and the action arms every un-watched path (already-watched ones are
 *    skipped so toggling a mixed selection doesn't accidentally remove them).
 *  - Every selected path is already watched → label "Remove Watchpoint(s)" and
 *    the action calls `_pycharm_unwatch(...)` for each.
 *
 * Single and multi-selection are both supported; with multi-select the menu
 * entry is visible (the previous version only enabled itself for a single
 * row, which made the entry vanish on multi-select – discoverability bug).
 *
 * Path resolution: the row clicked may be a leaf in a nested expansion
 * (`request → user → email`), so we walk parent XValueNodes to recover the
 * full dotted path (`request.user.email`) and pass that to the runtime. Same
 * walk powers the in-tree highlight in [WatchpointTreeCellRenderer].
 *
 * Note on extension hierarchy: we extend [AnAction] rather than
 * [XDebuggerTreeActionBase] because the latter's `perform(node, name, e)`
 * contract is fundamentally single-row. We still use its static
 * `getSelectedNodes(DataContext)` helper – multi-row selection support
 * lives there and there's no point reimplementing it.
 */
class AddWatchpointAction : AnAction() {
    private val logger = Logger.getInstance(AddWatchpointAction::class.java)

    /**
     * Decide whether the entry should appear, and pick its label.
     *
     * - Hidden when nothing is selected, no project, or the selection
     *   resolves to no usable dotted paths (defensive – the platform's
     *   `getSelectedNodes` is already filtered to XValueNodeImpl rows).
     * - Label: "Remove Watchpoint(s)" iff every selected path is watched;
     *   otherwise "Add Watchpoint(s)" (covers all-unwatched and mixed).
     *
     * We never set `presentation.icon` here – plugin.xml's `icon=...`
     * attribute owns the icon, and setting it from update() would clobber
     * it on every toolbar refresh (see CLAUDE.md "Toolbar-action icon stomp").
     */
    override fun update(e: AnActionEvent) {
        val project = e.project
        val nodes = getSelectedNodes(e.dataContext)
        if (project == null || nodes.isEmpty()) {
            e.presentation.isEnabledAndVisible = false
            return
        }
        val paths = nodes.mapNotNull { calculateFullPath(it).takeIf(String::isNotEmpty) }.toSet()
        if (paths.isEmpty()) {
            e.presentation.isEnabledAndVisible = false
            return
        }
        val service = WatchpointMarkerService.getInstance(project)
        val session = XDebuggerManager.getInstance(project).currentSession
        val labelFrameId: Long? = try {
            (session?.currentStackFrame as? PyStackFrame)?.frameId?.toLongOrNull()
        } catch (e: Exception) { null }
        // Mirror the object/frame-bound watched check from WatchpointTreeCellRenderer so
        // the menu label always agrees with what the icon shows (a row with no icon must
        // never read "Remove Watchpoint"):
        //   1. Frame-scoped marker (works when paused in the same frame the watch was armed,
        //      or a frame a hit fired in — the hit highlighter registers those).
        //   2. Type-sniff on the node's PyDebugValue (works for __class__-surgery watches
        //      like `request` travelling through middleware frames).
        // Deliberately NO name-only fallback — see WatchpointTreeCellRenderer for why
        // name matching cannot be made ghost-free with (name, frameId) markers alone.
        fun isNodeWatched(path: String, node: XValueNodeImpl): Boolean {
            if (labelFrameId != null && service.isWatched(path, labelFrameId)) return true
            val pyType = (node.valueContainer as? PyDebugValue)?.type ?: return false
            return pyType.startsWith("_Watched")
        }
        val allWatched = nodes
            .mapNotNull { n -> calculateFullPath(n).takeIf(String::isNotEmpty)?.let { n to it } }
            .all { (node, path) -> isNodeWatched(path, node) }
        val plural = paths.size > 1
        e.presentation.isEnabledAndVisible = true
        e.presentation.text = when {
            allWatched && plural -> "Remove Watchpoints"
            allWatched -> "Remove Watchpoint"
            plural -> "Add Watchpoints"
            else -> "Add Watchpoint"
        }
    }

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val nodes = getSelectedNodes(e.dataContext)
        if (nodes.isEmpty()) return

        val session = XDebuggerManager.getInstance(project).currentSession ?: return
        val debugProcess = session.debugProcess as? PyDebugProcess ?: return
        val service = WatchpointMarkerService.getInstance(project)

        // Dedupe by path so two tree rows pointing at the same logical
        // expression (e.g. selected in two different expansions) don't cause
        // a double arm / double unwatch.
        val pairs = nodes
            .mapNotNull { node ->
                val p = calculateFullPath(node)
                if (p.isEmpty()) null else node to p
            }
            .distinctBy { it.second }
        if (pairs.isEmpty()) return

        // Resolve the current frame up-front — needed both to scope isWatched
        // (so "self" in a different frame doesn't look watched) and to pass
        // (file, func) to watch_at for arming.
        val currentFrame = session.currentStackFrame
        val frameFile = currentFrame?.sourcePosition?.file?.path
        val frameFuncName = (currentFrame as? PyStackFrame)?.name
        val currentFrameId: Long? = try {
            (currentFrame as? PyStackFrame)?.frameId?.toLongOrNull()
        } catch (e: Exception) { null }

        // Object/frame-bound check matching update() and WatchpointTreeCellRenderer so
        // the toggle decision agrees with the label the user clicked:
        //   1. Frame-scoped marker: the frame the watch was armed against (or a frame a
        //      hit fired in — registered by the hit highlighter).
        //   2. Type-sniff: __class__-surgery watches (request, etc.) whose type was
        //      mutated to _WatchedAny* by watchpoint.py — travels across frames.
        // No name-only fallback (see WatchpointTreeCellRenderer): keeps label and action
        // consistent and avoids the ghost-icon ambiguity. Remove still works regardless,
        // since `_pycharm_unwatch` keys by expression string.
        fun isWatchedHere(expr: String, node: XValueNodeImpl): Boolean {
            if (currentFrameId != null && service.isWatched(expr, currentFrameId)) return true
            val pyType = (node.valueContainer as? PyDebugValue)?.type ?: return false
            return pyType.startsWith("_Watched")
        }

        val allWatched = pairs.all { (node, path) -> isWatchedHere(path, node) }
        if (allWatched) {
            // Remove path: doesn't need any frame info – the runtime keys
            // watches by expression string, so `_pycharm_unwatch('expr')` is
            // self-contained.
            pairs.forEach { (node, path) -> removeWatchpoint(project, debugProcess, node, path) }
            return
        }

        // Add path: we need (file, function-name) for the paused user frame so
        // watchpoint.py can locate the actual frame via sys._current_frames()
        // and arm a watch against its real code object. See CLAUDE.md
        // "Frame discovery for the PyCharm action" for why the evaluator's
        // own sys._getframe() stack is the WRONG one to use here.
        if (frameFile == null || frameFuncName == null) {
            notifyError(project, "Cannot add watchpoint: no current Python frame")
            return
        }

        // In a mixed selection, only arm the unwatched ones – the already-
        // watched ones stay armed (user can issue an explicit Remove later).
        pairs.filterNot { (node, path) -> isWatchedHere(path, node) }
            .forEach { (node, path) -> addWatchpoint(project, debugProcess, node, path, frameFile, frameFuncName) }
    }

    /**
     * Arm a watch on `fullPath`. Calls into the runtime via
     * `_pycharm_watch_at(name, file, func)` so it can locate the user's
     * paused frame across threads (the evaluator's frame stack doesn't
     * include user code – see runtime CLAUDE.md).
     *
     * Success is observed by a non-"ERROR:" return value. We do **not**
     * fire a success notification – the row icon swap done by
     * [WatchpointTreeCellRenderer] is the visible "this is now watched"
     * signal, and stacking a popup on top of it is redundant noise.
     */
    private fun addWatchpoint(
        project: Project,
        debugProcess: PyDebugProcess,
        node: XValueNodeImpl,
        fullPath: String,
        frameFile: String,
        frameFuncName: String,
    ) {
        logger.warn("Arming watchpoint on '$fullPath' in $frameFuncName ($frameFile)")

        val expr = "_pycharm_watch_at(" +
            "${pythonStringLiteral(fullPath)}, " +
            "${pythonStringLiteral(frameFile)}, " +
            "${pythonStringLiteral(frameFuncName)})"

        debugProcess.evaluator?.evaluate(expr, object : XDebuggerEvaluator.XEvaluationCallback {
            override fun evaluated(result: XValue) {
                // PyDebugValue.toString() returns the EXPRESSION text shown in
                // the Variables tree (the row name), NOT the evaluated value.
                // Use PyDebugValue.value for the actual Python return.
                val rawValue = (result as? PyDebugValue)?.value ?: result.toString()
                if (rawValue.startsWith("ERROR:")) {
                    notifyError(project, "Watchpoint failed: $rawValue")
                    return
                }
                // watch_at now returns str(id(frame)) on success – the Python
                // id of the exact frame instance that was armed. We store it
                // alongside the expression so the tree renderer can match only
                // the specific frame, not every variable with the same name.
                val frameId = rawValue.trim().toLongOrNull() ?: 0L
                ApplicationManager.getApplication().invokeLater {
                    decorateNode(project, node.tree, fullPath, frameId)
                    // Re-fetch all variables so the row's type/repr reflects the new
                    // `_WatchedAny_<Class>` wrapper instead of the original cached `<Class>`.
                    refreshVariablesView(project)
                }
                // Scan the whole paused stack now so the icon lights up in the
                // caller ("past") frames already on the wall – arming doesn't
                // fire sessionPaused, so without this they'd stay un-iconed until
                // the next step. decorateNode above only marks the armed frame.
                WatchpointFrameSync.refresh(
                    project, debugProcess, WatchpointMarkerService.getInstance(project),
                )
            }
            override fun errorOccurred(errorMessage: String) {
                logger.warn("Failed to arm watchpoint '$fullPath': $errorMessage")
                notifyError(project, "Could not add watchpoint on $fullPath: $errorMessage")
            }
        }, null)
    }

    /**
     * Disarm the watch for `fullPath`. The runtime's `unwatch()` is silent
     * (no return value), so we treat any non-error eval as success and
     * undecorate the row.
     *
     * After the unwatch lands, we refresh the Variables panel to re-fetch the
     * variable's repr. Without this, the panel keeps showing the stale
     * `_WatchedAny_<Class>` / `_Watched<Class>` subclass name (and the
     * wrapped-container types like `_WatchedList`) until the next step /
     * breakpoint hit naturally re-fetches. The runtime has already restored
     * `__class__` on the live object – this is purely a PyCharm-side
     * cache-invalidation issue.
     */
    private fun removeWatchpoint(
        project: Project,
        debugProcess: PyDebugProcess,
        node: XValueNodeImpl,
        fullPath: String,
    ) {
        logger.warn("Removing watchpoint on '$fullPath'")
        val expr = "_pycharm_unwatch(${pythonStringLiteral(fullPath)})"

        debugProcess.evaluator?.evaluate(expr, object : XDebuggerEvaluator.XEvaluationCallback {
            override fun evaluated(result: XValue) {
                ApplicationManager.getApplication().invokeLater {
                    undecorateNode(project, node.tree, fullPath)
                    // Re-fetch all variables so the row's type/repr reflects
                    // the now-restored __class__ instead of the stale
                    // `_WatchedAny_<Class>` snapshot from before unwatch.
                    refreshVariablesView(project)
                }
                // Re-scan the stack so the icon is dropped from every frame the
                // removed watch had lit up, not just the row clicked.
                WatchpointFrameSync.refresh(
                    project, debugProcess, WatchpointMarkerService.getInstance(project),
                )
            }
            override fun errorOccurred(errorMessage: String) {
                logger.warn("Failed to remove watchpoint '$fullPath': $errorMessage")
                notifyError(project, "Could not remove watchpoint on $fullPath: $errorMessage")
            }
        }, null)
    }

    /**
     * Refresh only the Variables panel without touching the Frames panel.
     *
     * `session.rebuildViews()` dispatches a FRAME_CHANGED event to ALL debug
     * views (variables, frames, watches), which causes the Frames panel to
     * reset its selection to the topmost frame – losing the user's scroll
     * position. Instead we send FRAME_CHANGED only to the Variables view.
     *
     * `getSessionTab()` and `getVariablesView()` are both `@ApiStatus.Internal`
     * and `getVariablesView()` didn't exist before PyCharm 2025. We therefore
     * call them via reflection so the plugin compiles and runs on the full
     * 2024–2026 range. Falls back to `rebuildViews()` on builds where either
     * method is absent (the frame-reset side-effect is acceptable vs. stale icons).
     *
     * In PyCharm 2025.2+ "split debugger" (FE proxy) mode, calling `getSessionTab()`
     * triggers a `Logger.error` inside XDebugSessionImpl that surfaces as a
     * user-visible error balloon even though the method still returns a value.
     * We detect split mode up-front via [isSplitDebuggerMode] and skip straight
     * to `rebuildViews()` in that case, avoiding the error entirely.
     */
    @Suppress("UnstableApiUsage")
    private fun refreshVariablesView(project: Project) {
        val session = XDebuggerManager.getInstance(project).currentSession as? XDebugSessionImpl ?: return

        // In split/FE-proxy mode getSessionTab() logs an error before returning –
        // skip the sessionTab path entirely and fall straight to rebuildViews().
        if (isSplitDebuggerMode()) {
            session.rebuildViews()
            return
        }

        val refreshed = runCatching {
            // Step 1: sessionTab – exists on all target builds but is @Internal.
            val sessionTab = session.sessionTab ?: return@runCatching false

            // Step 2: variablesView – added after 2024.3, must be called reflectively.
            val view = sessionTab.javaClass
                .getMethod("getVariablesView")
                .invoke(sessionTab) ?: return@runCatching false

            // Step 3: dispatch FRAME_CHANGED to the view only.
            view.javaClass
                .getMethod(
                    "processSessionEvent",
                    XDebugView.SessionEvent::class.java,
                    com.intellij.xdebugger.XDebugSession::class.java,
                )
                .invoke(view, XDebugView.SessionEvent.FRAME_CHANGED, session)
            true
        }.getOrDefault(false)

        if (!refreshed) {
            // Fallback for 2024.x: full rebuild resets the Frames panel selection
            // but keeps variable icons up-to-date on older builds.
            session.rebuildViews()
        }
    }

    /**
     * Returns true when PyCharm's "split debugger" mode is active, meaning
     * [XDebugSessionImpl.getSessionTab] would fire a [Logger.error] before
     * returning – producing a user-visible error balloon.
     *
     * The gate changed between platform versions:
     *  - 2025.2: `XDebugSessionProxy.Companion.useFeProxy()` (class introduced then)
     *  - 2026.1: `SplitDebuggerMode.isSplitDebugger()` (new dedicated class)
     *
     * We try the 2026.1 API first; if the class doesn't exist we fall back to
     * the 2025.2 API. Returns false on any reflection failure so the existing
     * sessionTab path is used on older builds where split mode doesn't exist.
     */
    private fun isSplitDebuggerMode(): Boolean {
        // 2026.1+: com.intellij.xdebugger.SplitDebuggerMode.isSplitDebugger()
        runCatching {
            val cls = Class.forName("com.intellij.xdebugger.SplitDebuggerMode")
            return cls.getMethod("isSplitDebugger").invoke(null) as? Boolean ?: false
        }
        // 2025.2: XDebugSessionProxy.Companion.useFeProxy()
        return runCatching {
            val proxyClass = Class.forName("com.intellij.xdebugger.impl.frame.XDebugSessionProxy")
            val companionField = proxyClass.getDeclaredField("Companion")
            companionField.isAccessible = true
            val companion = companionField.get(null)
            companion.javaClass.getMethod("useFeProxy").invoke(companion) as? Boolean ?: false
        }.getOrDefault(false)
    }

    /**
     * Return a single-quoted Python literal for evaluator calls.
     *
     * Backslashes must be escaped before quotes: Windows paths like
     * `C:\Users\me\app.py` otherwise contain Python unicode escape prefixes
     * such as `\U`, and the evaluator rejects the whole command before the
     * runtime sees it.
     */
    private fun pythonStringLiteral(value: String): String {
        val escaped = value
            .replace("\\", "\\\\")
            .replace("'", "\\'")
        return "'$escaped'"
    }

    /**
     * Walk parent XValueNodes to reconstruct the full dotted path. PyCharm's
     * tree shows nested attributes as a hierarchy where each node only knows
     * its own leaf name; only by climbing to the root do we recover something
     * like "obj.a.b".
     */
    private fun calculateFullPath(node: XValueNodeImpl): String {
        val parts = LinkedList<String>()
        var current: XValueNodeImpl? = node
        while (current != null) {
            val name = current.name
            if (!name.isNullOrEmpty()) {
                parts.addFirst(name)
            }
            current = current.parent as? XValueNodeImpl
        }
        return parts.joinToString(".")
    }


    /**
     * Reflective wrapper for XDebuggerTreeActionBase.getSelectedNodes.
     *
     * The method changed shape across builds: Kotlin companion function (2023.3,
     * 2026.1+) vs. plain Java static (2025.x). A direct Kotlin call compiles to
     * Companion-field access bytecode that throws NoSuchFieldError on builds where
     * the companion was removed. We probe both shapes so every build in sinceBuild..
     * untilBuild works without a compile-time dependency on the exact class shape.
     */
    private fun getSelectedNodes(dataContext: DataContext): List<XValueNodeImpl> {
        // 2025.x path: plain static method on the class itself
        runCatching {
            val m = XDebuggerTreeActionBase::class.java
                .getMethod("getSelectedNodes", DataContext::class.java)
            @Suppress("UNCHECKED_CAST")
            return (m.invoke(null, dataContext) as? List<XValueNodeImpl>) ?: emptyList()
        }
        // 2023.3 / 2026.1+ path: Kotlin companion object holds the method
        return runCatching {
            val f = XDebuggerTreeActionBase::class.java.getDeclaredField("Companion")
            f.isAccessible = true
            val companion = f.get(null)
            val m = companion.javaClass.getMethod("getSelectedNodes", DataContext::class.java)
            @Suppress("UNCHECKED_CAST")
            (m.invoke(companion, dataContext) as? List<XValueNodeImpl>) ?: emptyList()
        }.getOrDefault(emptyList())
    }

    /**
     * Show an IDE error balloon for watchpoint failures.
     *
     * Uses reflection for the entire `NotificationGroupManager` call chain so
     * the plugin compiles against PyCharm 2022.2+ where `com.intellij.notification`
     * may not be in the default compile classpath for the Gradle 2.x plugin. Falls
     * back to a logger.warn() so the message is never silently swallowed.
     */
    private fun notifyError(project: Project, message: String) {
        val shown = runCatching {
            val managerClass = Class.forName("com.intellij.notification.NotificationGroupManager")
            val manager = managerClass.getMethod("getInstance").invoke(null)
            val group = manager.javaClass
                .getMethod("getNotificationGroup", String::class.java)
                .invoke(manager, "Debugger messages") ?: return@runCatching false
            val typeClass = Class.forName("com.intellij.notification.NotificationType")
            val errorType = typeClass.enumConstants
                ?.find { (it as Enum<*>).name == "ERROR" } ?: return@runCatching false
            val notif = group.javaClass
                .getMethod("createNotification", String::class.java, typeClass)
                .invoke(group, message, errorType) ?: return@runCatching false
            notif.javaClass
                .getMethod("notify", Class.forName("com.intellij.openapi.project.Project"))
                .invoke(notif, project)
            true
        }.getOrDefault(false)
        if (!shown) logger.warn("Watchpoint error (could not display as balloon): $message")
    }

    /**
     * Register [fullPath] as a watched expression (armed against [frameId]) with
     * the marker service and make sure the Variables tree is running our cell
     * renderer that surfaces the highlight. Both operations are idempotent –
     * calling them again for an already-watched path or already-wrapped tree is a no-op.
     */
    private fun decorateNode(project: Project, tree: XDebuggerTree, fullPath: String, frameId: Long) {
        try {
            val service = WatchpointMarkerService.getInstance(project)
            service.add(fullPath, frameId)
            ensureCellRendererInstalled(tree, service, project)
            tree.repaint()
        } catch (e: Exception) {
            // Decoration is a UX nicety, not load-bearing. If anything refuses,
            // the watchpoint itself is still armed and works.
            logger.warn("Could not decorate watched variable node: ${e.message}")
        }
    }

    /**
     * Counterpart to [decorateNode] – drop the path from the marker service
     * and repaint so the row's icon falls back to whatever the platform
     * renderer chooses. We deliberately leave the cell renderer installed
     * (it's idempotent and harmless on unwatched rows), so subsequent re-arms
     * in the same session don't have to re-wrap.
     */
    private fun undecorateNode(project: Project, tree: XDebuggerTree, fullPath: String) {
        try {
            val service = WatchpointMarkerService.getInstance(project)
            service.remove(fullPath)
            tree.repaint()
        } catch (e: Exception) {
            logger.warn("Could not undecorate unwatched variable node: ${e.message}")
        }
    }

    /**
     * Wrap the tree's current cell renderer with [WatchpointTreeCellRenderer]
     * unless it is already wrapped. The wrapping is one-way: the original
     * renderer becomes a delegate, our wrapper checks each cell against the
     * marker service before deciding whether to layer decoration.
     */
    private fun ensureCellRendererInstalled(tree: XDebuggerTree, service: WatchpointMarkerService, project: Project) {
        val current = tree.cellRenderer
        if (current is WatchpointTreeCellRenderer) return
        tree.cellRenderer = WatchpointTreeCellRenderer(current, service, project)
    }
}