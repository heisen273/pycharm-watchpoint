package com.pythonwatchpoint.listeners

import com.intellij.ui.JBColor
import com.intellij.ui.SimpleColoredComponent
import com.intellij.xdebugger.impl.ui.tree.nodes.XValueNodeImpl
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
        if (fullPath.isEmpty() || !markerService.isWatched(fullPath)) return component
        decorate(component, selected)
        return component
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
