package com.pythonwatchpoint.icons

import com.intellij.openapi.util.IconLoader
import javax.swing.Icon

/**
 * Plugin-owned icons. `IconLoader.getIcon` discovers the size and theme
 * variants placed next to the base path automatically:
 *  - `watchpoint.svg`                    – classic 16x16, light theme
 *  - `watchpoint_dark.svg`               – classic 16x16, dark theme
 *  - `watchpoint@20x20.svg`              – New UI 20x20, light theme
 *  - `watchpoint@20x20_dark.svg`         – New UI 20x20, dark theme
 *  - `debugwatchpoint.svg`               – classic 16x16, light theme
 *  - `debugwatchpoint_dark.svg`          – classic 16x16, dark theme
 *  - `debugwatchpoint@20x20.svg`         – New UI 20x20, light theme
 *  - `debugwatchpoint@20x20_dark.svg`    – New UI 20x20, dark theme
 *
 * The `@JvmField` is required so that the `icon="..."` attribute in plugin.xml
 * can resolve fields as Java-accessible statics.
 */
object WatchpointIcons {
    /** Spectacles glyph used to mark anything the plugin is currently watching. */
    @JvmField
    val Watch: Icon = IconLoader.getIcon("/icons/watchpoint.svg", WatchpointIcons::class.java)

    /** Bug + spectacles badge – used on the "Debug with Watchpoint" toolbar action. */
    @JvmField
    val DebugWatch: Icon = IconLoader.getIcon("/icons/debugwatchpoint.svg", WatchpointIcons::class.java)
}
