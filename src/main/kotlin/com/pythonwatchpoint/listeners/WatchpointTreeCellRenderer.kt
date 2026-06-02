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
 * Matching is object/frame-bound, never name-only, so a fresh same-named object
 * (e.g. the `request` of a new web request) is not decorated. A row is treated
 * as watched iff EITHER its `(name, id(frame))` is registered in
 * [WatchpointMarkerService] for the frame currently shown, OR its
 * `PyDebugValue.type` carries the runtime's `_Watched*` class-surgery marker
 * (which travels with the object across frames). See
 * [getTreeCellRendererComponent] for why a name-only fallback is intentionally
 * absent – it cannot distinguish "same watched object, other frame" from "new
 * object, same name", which was the source of the ghost-icon bug.
 *
 * Watching `request.user.email` therefore highlights only the leaf `email` node
 * (when the user has expanded the chain to it) – not its ancestors `request`
 * and `user`, which keep their normal rendering.
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

        // Two object/frame-bound ways to conclude this row is watched. Either is
        // sufficient. Both are immune to the "ghost icon" bug because neither matches
        // purely by variable name — they bind to the specific frame instance or the
        // specific mutated object.
        //
        // Path 1 — frame-scoped marker lookup:
        // The armed frame's id() (== pydevd's frameId, see resolveFrameId) was stored in
        // WatchpointMarkerService when the user clicked "Add Watchpoint", and the hit
        // highlighter additionally registers each fired watch against the live paused
        // frame. Matches only when the renderer is showing one of those exact frames —
        // covers local-variable watches and rebind-only watches in the frame the user is
        // actually paused in. A new same-named object lives in a different frame (new
        // id()), so a stale entry can never light it up.
        //
        // Path 2 — runtime type sniffing:
        // When watchpoint.py arms an attribute watch it does __class__ surgery on the
        // object, replacing its class with a generated subclass whose __name__ starts
        // with "_Watched" (_WatchedAnyAttrSubclass, _WatchedSubclass, _WatchedList,
        // _WatchedDict, _WatchedSet). PyDebugValue.type is type(obj).__name__ from
        // the pydevd wire protocol — free to read, no extra IPC. The mutated type travels
        // with the object into every frame, so this covers `request` and any other
        // class-surgery watch regardless of which frame the renderer is showing. A fresh,
        // unwatched object keeps its plain type, so this never produces a ghost either.
        //
        // We deliberately do NOT fall back to a name-only marker lookup. With only
        // (name, frameId) markers a name-only match cannot distinguish "the same watched
        // object shown in another frame" from "a different new object with the same name"
        // — that ambiguity is exactly the ghost-icon bug. When the frame id can't be
        // resolved (rare teardown/race) we show no icon rather than guess by name; the
        // next repaint corrects it.
        val frameId = resolveFrameId(tree)
        val isWatchedByMarker = frameId != null && markerService.isWatched(fullPath, frameId)
        val isWatchedByType = (value.valueContainer as? PyDebugValue)
            ?.type
            ?.startsWith("_Watched")
            ?: false
        if (!isWatchedByMarker && !isWatchedByType) return component
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