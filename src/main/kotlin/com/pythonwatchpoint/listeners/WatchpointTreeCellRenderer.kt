package com.pythonwatchpoint.listeners

import com.intellij.openapi.project.Project
import com.intellij.ui.JBColor
import com.intellij.ui.SimpleColoredComponent
import com.intellij.xdebugger.XDebuggerManager
import com.intellij.xdebugger.impl.ui.tree.XDebuggerTree
import com.intellij.xdebugger.impl.ui.tree.nodes.XValueNodeImpl
import com.jetbrains.python.debugger.PyDebugValue
import com.jetbrains.python.debugger.PyStackFrame
import com.pythonwatchpoint.icons.WatchpointIcons
import com.pythonwatchpoint.services.WatchpointMarkerService
import java.awt.Color
import java.awt.Component
import java.util.ArrayDeque
import javax.swing.JTree
import javax.swing.tree.TreeCellRenderer

/**
 * Decorates rows in the debugger's Variables tree whose XValueNode corresponds
 * to an armed watchpoint. Wraps the platform's default cell renderer rather
 * than reimplementing it, so name/value/icon rendering keeps following the
 * IDE's normal styling – we only layer extra decoration on top.
 *
 * Decoration applied per row:
 *  - Background tint (pale yellow on light themes, muted amber on dark) so the
 *    row pops out from siblings.
 *  - The leading icon is swapped for the watchpoint icon
 *    ([AllIcons.Debugger.Watch]) – the same glyph used by the toolbar action
 *    and the gutter scroll mark. PyCharm's default variable icons are generic
 *    field markers, so trading them for the watch icon is a fair price for an
 *    unambiguous "this is being watched" signal.
 *
 * Selected rows are deliberately left undecorated so the user's selection
 * colour wins – the alternative (overriding both selection colour AND
 * watchpoint colour) reads as a clash on most themes.
 *
 * Matching: the row's XValueNode is mapped to its full dotted path
 * (`request.user.email`) and compared against [WatchpointMarkerService] by
 * equality. Watching `request.user.email` therefore highlights only the leaf
 * `email` node (when the user has expanded the chain to it) – not its
 * ancestors `request` and `user`, which keep their normal rendering.
 */
class WatchpointTreeCellRenderer(
    private val delegate: TreeCellRenderer,
    private val markerService: WatchpointMarkerService,
    private val project: Project,
) : TreeCellRenderer {

    // Tint colours – chosen to match the line-highlight palette used elsewhere
    // in the plugin so a watched variable visually rhymes with a watchpoint hit.
    private val tintLight = Color(255, 245, 175)
    private val tintDark = Color(85, 70, 30)

    override fun getTreeCellRendererComponent(
        tree: JTree,
        value: Any?,
        selected: Boolean,
        expanded: Boolean,
        leaf: Boolean,
        row: Int,
        hasFocus: Boolean,
    ): Component {
        val component = delegate.getTreeCellRendererComponent(
            tree, value, selected, expanded, leaf, row, hasFocus,
        )
        // We only know how to decorate XValue rows; messages / loading-nodes
        // / spinners go through untouched.
        if (value !is XValueNodeImpl) return component
        val fullPath = computeFullPath(value)
        if (fullPath.isEmpty()) return component

        // Three independent ways to conclude this row is watched. Any one is sufficient.
        //
        // Path 1 — frame-scoped marker lookup:
        // The armed frame's id() was stored in WatchpointMarkerService when the user
        // clicked "Add Watchpoint". Works when the renderer is showing the exact frame
        // the watch was armed in (most local-variable watch scenarios).
        //
        // Path 2 — runtime type sniffing:
        // When watchpoint.py arms an attribute watch it does __class__ surgery on the
        // object, replacing its class with a generated subclass whose __name__ starts
        // with "_Watched" (_WatchedAnyAttrSubclass, _WatchedSubclass, _WatchedList,
        // _WatchedDict, _WatchedSet). PyDebugValue.type is type(obj).__name__ from
        // the pydevd wire protocol — free to read, no extra IPC. This covers `request`
        // and any other object travelling through frames where the armed frameId won't
        // match the current paused frameId.
        //
        // Path 3 — name-only marker fallback:
        // Covers rebind-only watches (e.g. a Django Model where __class__ surgery was
        // refused by the metaclass). The type stays plain "Subscription" so Path 2
        // misses it, and the hit fires in a callee frame (e.g. set_attributes) so the
        // frame-scoped Path 1 also misses. The name-only check catches it because the
        // armed expression is in the marker service regardless of frame.
        val frameId = resolveFrameId(tree)
        val isWatchedByMarker = if (frameId != null) {
            markerService.isWatched(fullPath, frameId)
        } else {
            markerService.isWatched(fullPath)
        }
        val isWatchedByType = (value.valueContainer as? PyDebugValue)
            ?.type
            ?.startsWith("_Watched")
            ?: false
        // Third prong: name-only marker lookup. Covers rebind-only watches (e.g. a Django
        // Model where __class__ surgery was refused by the metaclass) — the type stays
        // plain "Subscription" so isWatchedByType misses it, and the hit fires in a callee
        // frame (set_attributes) so the frame-scoped isWatchedByMarker also misses. The
        // name-only check catches it because the armed expression (e.g. "obj") appears
        // in the marker service regardless of which frame the renderer is currently showing.
        val isWatchedByName = markerService.isWatched(fullPath)
        if (!isWatchedByMarker && !isWatchedByType && !isWatchedByName) return component
        decorate(component, selected)
        return component
    }

    /**
     * Extract the Python `id(frame)` for the frame currently selected in the
     * Variables tree. Returns null when the frame id cannot be determined
     * (session not yet ready, non-Python process, teardown race).
     *
     * The id was returned by `watch_at` as `str(id(frame))` and stored in
     * [WatchpointMarkerService] when the watch was armed — so the same numeric
     * value must be recovered here to produce a matching [WatchpointMarkerService.WatchKey].
     *
     * PyStackFrame's `threadId` / `frameId` fields are the pydevd-internal ids
     * (sequential integers, not Python's `id()`). The Python `id(frame)` is the
     * memory address of the frame object, exposed by pydevd as the frame's
     * `id` string in the form `"<threadId>|<frameId>"` — or directly via
     * [PyStackFrame.getFrameId] depending on the platform version. We therefore
     * evaluate `id(sys._getframe(0))` against the paused frame to get the exact
     * same value the runtime returned.
     *
     * This evaluation is cheap (one integer read, no I/O) and runs on the EDT
     * inside a paint call, but `PyStackFrame.getAdditionalFrameInfo` and similar
     * synchronous evaluations are used by the platform itself in the same
     * context, so the pattern is safe.
     */
    private fun resolveFrameId(tree: JTree): Long? {
        if (tree !is XDebuggerTree) return null
        // XDebuggerTree has no .session property — go through the manager.
        val frame = XDebuggerManager.getInstance(project)
            .currentSession?.currentStackFrame as? PyStackFrame ?: return null
        // In the pydevd wire protocol, frameId IS id(frame) cast to a string,
        // so this matches what watch_at returned.
        return try {
            frame.frameId.toLongOrNull()
        } catch (e: Exception) {
            null
        }
    }

    private fun decorate(component: Component, selected: Boolean) {
        if (component !is SimpleColoredComponent) return

        // Swap the row's leading icon for the plugin's own watchpoint glyph.
        // We don't try to preserve the original variable-type icon underneath
        // – PyCharm's type icons for Python locals are generic field markers,
        // so trading them for an unambiguous "watched" signal is worth more
        // than the type-marker overlap a LayeredIcon would give us.
        //
        // The icon swap is safe regardless of selection state: it doesn't
        // touch the row background, so the IDE's selection colour still wins
        // visually – the user just keeps seeing "this row is watched".
        component.icon = WatchpointIcons.Watch

        // The tint background, on the other hand, would compete with the
        // selection colour and read as a clash. Apply it only when the row
        // is not selected; selection alone is signal enough for the focused
        // row's prominence.
        if (!selected) {
            component.background = JBColor(tintLight, tintDark)
            component.isOpaque = true
        }
    }

    /**
     * Walk parent nodes to assemble the dotted path that identifies this row
     * – e.g. `request.user.email`. Mirrors the resolver in `AddWatchpointAction`
     * so the names registered via right-click line up with what we compare here.
     */
    private fun computeFullPath(node: XValueNodeImpl): String {
        val parts = ArrayDeque<String>()
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
}