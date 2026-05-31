package com.pythonwatchpoint.actions

import com.intellij.notification.NotificationGroupManager
import com.intellij.notification.NotificationType
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
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
        val frameFuncName = (currentFrame as? PyStackFrame)?.name
        if (frameFile == null || frameFuncName == null) {
            notify(project, "Cannot add watchpoint: no current Python frame", NotificationType.ERROR)
            return
        }

        // In a mixed selection, only arm the unwatched ones – the already-
        // watched ones stay armed (user can issue an explicit Remove later).
        pairs.filterNot { service.isWatched(it.second) }
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
                    notify(project, "Watchpoint failed: $rawValue", NotificationType.ERROR)
                    return
                }
                ApplicationManager.getApplication().invokeLater {
                    decorateNode(project, node.tree, fullPath)
                    // Re-fetch all variables so the row's type/repr reflects the new
                    // `_WatchedAny_<Class>` wrapper instead of the original cached `<Class>`.
                    refreshVariablesView(project)
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
            }
            override fun errorOccurred(errorMessage: String) {
                logger.warn("Failed to remove watchpoint '$fullPath': $errorMessage")
                notify(project, "Could not remove watchpoint on $fullPath: $errorMessage", NotificationType.ERROR)
            }
        }, null)
    }

    /**
     * Refresh only the Variables panel without touching the Frames panel.
     *
     * `session.rebuildViews()` dispatches a FRAME_CHANGED event to ALL debug
     * views (variables, frames, watches), which causes the Frames panel to
     * reset its selection to the topmost frame – losing the user's scroll
     * position. Instead, we send the event only to the Variables view so
     * variable repr/type is re-fetched while the frame selection stays put.
     */
    private fun refreshVariablesView(project: Project) {
        val session = XDebuggerManager.getInstance(project).currentSession as? XDebugSessionImpl ?: return
        val variablesView = session.sessionTab?.variablesView ?: return
        variablesView.processSessionEvent(XDebugView.SessionEvent.FRAME_CHANGED, session)
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
