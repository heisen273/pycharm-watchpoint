package com.pythonwatchpoint.listeners

import com.intellij.openapi.project.Project
import com.intellij.xdebugger.XDebuggerManager
import com.intellij.xdebugger.impl.XDebugSessionImpl
import com.intellij.xdebugger.impl.ui.tree.XDebuggerTree

/**
 * Shared helpers for reaching the debugger's Variables tree from outside the
 * action that armed a watch. Kept reflection-heavy and defensive so the plugin
 * compiles and runs across the full 2023–2026 PyCharm range where the relevant
 * APIs are `@ApiStatus.Internal` and have shifted between versions.
 */
object WatchpointUiUtil {

    /**
     * Returns true when PyCharm's "split debugger" (FE-proxy) mode is active.
     * In that mode `XDebugSessionImpl.getSessionTab()` fires a `Logger.error`
     * before returning, surfacing a user-visible error balloon — so callers
     * must avoid the sessionTab path entirely when this is true.
     *
     * The gate changed across versions:
     *  - 2025.2: `XDebugSessionProxy.Companion.useFeProxy()` (class introduced then)
     *  - 2026.1: `SplitDebuggerMode.isSplitDebugger()` (new dedicated class)
     *
     * Tries the 2026.1 API first, falls back to the 2025.2 API, and returns
     * false on any reflection failure (older builds without split mode).
     */
    fun isSplitDebuggerMode(): Boolean {
        runCatching {
            val cls = Class.forName("com.intellij.xdebugger.SplitDebuggerMode")
            return cls.getMethod("isSplitDebugger").invoke(null) as? Boolean ?: false
        }
        return runCatching {
            val proxyClass = Class.forName("com.intellij.xdebugger.impl.frame.XDebugSessionProxy")
            val companionField = proxyClass.getDeclaredField("Companion")
            companionField.isAccessible = true
            val companion = companionField.get(null)
            companion.javaClass.getMethod("useFeProxy").invoke(companion) as? Boolean ?: false
        }.getOrDefault(false)
    }

    /**
     * Best-effort lookup of the current session's Variables-panel tree, or null
     * if it can't be reached (no session, split mode, or an API that isn't
     * present on this build). Used to force a repaint after the cross-frame
     * watch set is refreshed.
     *
     * `getVariablesView()` is `@Internal` and post-dates 2024.3, so it is called
     * reflectively; `getTree()` on `XVariablesViewBase` is public and stable.
     * In split mode we bail out (returning null) rather than touch
     * `getSessionTab()` and trigger its error balloon.
     */
    @Suppress("UnstableApiUsage")
    fun currentVariablesTree(project: Project): XDebuggerTree? {
        val session = XDebuggerManager.getInstance(project).currentSession as? XDebugSessionImpl ?: return null
        if (isSplitDebuggerMode()) return null
        return runCatching {
            val sessionTab = session.sessionTab ?: return null
            val view = sessionTab.javaClass.getMethod("getVariablesView").invoke(sessionTab) ?: return null
            view.javaClass.getMethod("getTree").invoke(view) as? XDebuggerTree
        }.getOrNull()
    }
}
