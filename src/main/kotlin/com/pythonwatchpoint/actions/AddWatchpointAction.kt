package com.pythonwatchpoint.actions

import com.intellij.notification.NotificationGroupManager
import com.intellij.notification.NotificationType
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.diagnostic.Logger
import com.intellij.openapi.project.Project
import com.intellij.xdebugger.XDebugProcess
import com.intellij.xdebugger.XDebuggerManager
import com.intellij.xdebugger.evaluation.XDebuggerEvaluator
import com.intellij.xdebugger.frame.XStackFrame
import com.intellij.xdebugger.frame.XValue
import com.intellij.xdebugger.impl.ui.tree.XDebuggerTree
import com.intellij.xdebugger.impl.ui.tree.actions.XDebuggerTreeActionBase
import com.intellij.xdebugger.impl.ui.tree.nodes.XValueNodeImpl
import com.jetbrains.python.debugger.PyDebugValue
import com.jetbrains.python.debugger.PyStackFrame
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
        val nodes = XDebuggerTreeActionBase.getSelectedNodes(e.dataContext)
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
        val allWatched = paths.all { service.isWatched(it) }
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
        val nodes = XDebuggerTreeActionBase.getSelectedNodes(e.dataContext)
        if (nodes.isEmpty()) return

        val session = XDebuggerManager.getInstance(project).currentSession ?: return
        // Accept any XDebugProcess (PyDebugProcess for pydevd, PythonDapDebugProcess for
        // debugpy). The previous `as? PyDebugProcess ?: return` silently swallowed the
        // entire action under debugpy – no log, no notification, no eval.
        val debugProcess = session.debugProcess
        logger.warn("AddWatchpointAction.actionPerformed: debugProcess=${debugProcess::class.simpleName}")
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

        val allWatched = pairs.all { service.isWatched(it.second) }
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
        val currentFrame = session.currentStackFrame
        val frameFile = currentFrame?.sourcePosition?.file?.path
        // Func name: pydevd's PyStackFrame exposes `.name` directly; debugpy's
        // DapXStackFrame uses a different class hierarchy. We try the typed cast
        // first, then fall back to reflection so we work under any XStackFrame
        // subclass that happens to expose a name-ish property.
        val frameFuncName = currentFrame?.let(::extractFrameName)
        logger.warn("Resolved frame: file=$frameFile, func=$frameFuncName, frameType=${currentFrame?.let { it::class.simpleName }}")
        if (frameFile == null) {
            notify(project, "Cannot add watchpoint: no current Python frame (file unavailable)", NotificationType.ERROR)
            return
        }

        // Empty string is the runtime's "match by file only" signal – worse than
        // having the func name (more chance of multiple candidates on recursion),
        // but lets debugpy users with no name resolution still arm watches.
        val funcHint = frameFuncName ?: ""

        // In a mixed selection, only arm the unwatched ones – the already-
        // watched ones stay armed (user can issue an explicit Remove later).
        pairs.filterNot { service.isWatched(it.second) }
            .forEach { (node, path) -> addWatchpoint(project, debugProcess, node, path, frameFile, funcHint) }
    }

    /**
     * Best-effort extraction of the function name from a paused stack frame.
     * pydevd exposes [PyStackFrame.name]; debugpy's DAP stack frame uses a
     * different class entirely, so we fall back to common JavaBean getter names
     * via reflection. Returns null if nothing usable surfaces – the caller can
     * still arm a watch with file-only matching.
     */
    private fun extractFrameName(frame: XStackFrame): String? {
        (frame as? PyStackFrame)?.name?.takeIf { it.isNotEmpty() }?.let { return it }
        // Reflection fallback for DAP / future frame impls. Walk the class
        // hierarchy and try both public methods (`getMethod`) and any-visibility
        // declared methods (`getDeclaredMethod` + setAccessible), since Kotlin
        // backing fields can end up package-private under some compiler settings.
        val candidates = listOf("getName", "getFunctionName", "getMethodName")
        var cls: Class<*>? = frame.javaClass
        while (cls != null) {
            for (methodName in candidates) {
                val method = try {
                    cls.getDeclaredMethod(methodName).also { it.isAccessible = true }
                } catch (_: NoSuchMethodException) {
                    null
                }
                if (method != null) {
                    try {
                        val value = method.invoke(frame) as? String
                        if (!value.isNullOrEmpty()) return value
                    } catch (e: Exception) {
                        logger.warn("extractFrameName: ${cls?.simpleName}.$methodName threw ${e.message}")
                    }
                }
            }
            cls = cls.superclass
        }
        return null
    }

    /**
     * Arm a watch on `fullPath`. Calls into the runtime via
     * `_pycharm_watch_at(name, file, func)` so it can locate the user's
     * paused frame across threads (the evaluator's frame stack doesn't
     * include user code – see runtime CLAUDE.md).
     *
     * Success is observed by a non-"ERROR" return value. We do **not**
     * fire a success notification – the row icon swap done by
     * [WatchpointTreeCellRenderer] is the visible "this is now watched"
     * signal, and stacking a popup on top of it is redundant noise.
     */
    private fun addWatchpoint(
        project: Project,
        debugProcess: XDebugProcess,
        node: XValueNodeImpl,
        fullPath: String,
        frameFile: String,
        frameFuncName: String,
    ) {
        logger.warn("Arming watchpoint on '$fullPath' in '$frameFuncName' ($frameFile)")

        // Escape single-quotes defensively (Windows paths, unusual identifiers).
        val escFullPath = fullPath.replace("'", "\\'")
        val escFile = frameFile.replace("'", "\\'")
        val escFunc = frameFuncName.replace("'", "\\'")
        val expr = "_pycharm_watch_at('$escFullPath', '$escFile', '$escFunc')"

        debugProcess.evaluator?.evaluate(expr, object : XDebuggerEvaluator.XEvaluationCallback {
            override fun evaluated(result: XValue) {
                // Under pydevd we can read PyDebugValue.value to get the actual Python
                // return string. Under debugpy the result is a DAP-specific XValue
                // subclass whose toString() comes back empty, so PyDebugValue.value
                // is unreachable. We optimistically decorate the row when no error
                // arrived on either path – the runtime's own stderr trace
                // ([WATCHPOINT/dbg] watch_at: add_watch returned ...) is the source
                // of truth for "watch armed successfully".
                val rawValue = (result as? PyDebugValue)?.value
                if (rawValue != null && rawValue.contains("ERROR")) {
                    notify(project, "Watchpoint failed: $rawValue", NotificationType.ERROR)
                    return
                }
                logger.warn("Watchpoint arm eval returned: ${rawValue ?: "<no readable value – check Debug Console for runtime trace>"}")
                ApplicationManager.getApplication().invokeLater {
                    decorateNode(project, node.tree, fullPath)
                    // Re-fetch all variables so the row's type/repr reflects the new
                    // `_WatchedAny_<Class>` wrapper instead of the original cached `<Class>`.
                    XDebuggerManager.getInstance(project).currentSession?.rebuildViews()
                }
            }
            override fun errorOccurred(errorMessage: String) {
                logger.warn("Failed to arm watchpoint '$fullPath': $errorMessage")
                notify(project, "Could not add watchpoint on $fullPath: $errorMessage", NotificationType.ERROR)
            }
        }, null)
    }

    /**
     * Disarm the watch for `fullPath`. The runtime's `unwatch()` is silent
     * (no return value), so we treat any non-error eval as success and
     * undecorate the row.
     *
     * After the unwatch lands, we also call `XDebugSession.rebuildViews()` to
     * force the Variables panel to re-fetch the variable's repr. Without
     * this, the panel keeps showing the stale `_WatchedAny_<Class>` /
     * `_Watched<Class>` subclass name (and the wrapped-container types like
     * `_WatchedList`) until the next step / breakpoint hit naturally
     * re-fetches. The runtime has already restored `__class__` on the live
     * object – this is purely a PyCharm side cache-invalidation issue, and
     * `rebuildViews()` is the public API for "re-query everything".
     */
    private fun removeWatchpoint(
        project: Project,
        debugProcess: XDebugProcess,
        node: XValueNodeImpl,
        fullPath: String,
    ) {
        logger.warn("Removing watchpoint on '$fullPath'")
        val escFullPath = fullPath.replace("'", "\\'")
        val expr = "_pycharm_unwatch('$escFullPath')"

        debugProcess.evaluator?.evaluate(expr, object : XDebuggerEvaluator.XEvaluationCallback {
            override fun evaluated(result: XValue) {
                ApplicationManager.getApplication().invokeLater {
                    undecorateNode(project, node.tree, fullPath)
                    // Re-fetch all variables so the row's type/repr reflects
                    // the now-restored __class__ instead of the stale
                    // `_WatchedAny_<Class>` snapshot from before unwatch.
                    XDebuggerManager.getInstance(project).currentSession?.rebuildViews()
                }
            }
            override fun errorOccurred(errorMessage: String) {
                logger.warn("Failed to remove watchpoint '$fullPath': $errorMessage")
                notify(project, "Could not remove watchpoint on $fullPath: $errorMessage", NotificationType.ERROR)
            }
        }, null)
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

    private fun notify(project: Project, message: String, type: NotificationType) {
        // "Debugger messages" is bundled and always available. Reserved for
        // errors only – success is signalled by the in-tree icon swap.
        NotificationGroupManager.getInstance()
            .getNotificationGroup("Debugger messages")
            .createNotification(message, type)
            .notify(project)
    }

    /**
     * Register [fullPath] as a watched expression with the marker service and
     * make sure the Variables tree is running our cell renderer that surfaces
     * the highlight. Both operations are idempotent – calling them again for
     * an already-watched path or already-wrapped tree is a no-op.
     */
    private fun decorateNode(project: Project, tree: XDebuggerTree, fullPath: String) {
        try {
            val service = WatchpointMarkerService.getInstance(project)
            service.add(fullPath)
            ensureCellRendererInstalled(tree, service)
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
    private fun ensureCellRendererInstalled(tree: XDebuggerTree, service: WatchpointMarkerService) {
        val current = tree.cellRenderer
        if (current is WatchpointTreeCellRenderer) return
        tree.cellRenderer = WatchpointTreeCellRenderer(current, service)
    }
}
