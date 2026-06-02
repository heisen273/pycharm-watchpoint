package com.pythonwatchpoint.listeners

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.diagnostic.Logger
import com.intellij.openapi.project.Project
import com.jetbrains.python.debugger.PyDebugProcess
import com.pythonwatchpoint.services.WatchpointMarkerService
import java.util.Base64

/**
 * Refreshes [WatchpointMarkerService]'s cross-frame "synced" set from the
 * runtime's `_pycharm_locate_watches()`, which reports every live
 * `(name, id(frame))` pair where an armed watch currently lives across the
 * whole call stack (by frame identity / object identity).
 *
 * Two callers drive it, both while the session is paused:
 *  - [WatchpointHitHighlighter.sessionPaused] – on every pause/step, so icons
 *    track as the user moves through the program.
 *  - [com.pythonwatchpoint.actions.AddWatchpointAction] – the instant a watch is
 *    armed or removed. Arming doesn't fire `sessionPaused` (the session is
 *    already paused), so without this the caller frames already on the stack at
 *    arm time wouldn't get the icon until the next step. Running the scan here
 *    lights up the whole current stack immediately.
 *
 * SAFETY: the synced set is only ever REPLACED on a well-formed read – never on
 * a timeout, exception, or an `ERROR:`-prefixed result. A transient evaluator
 * hiccup therefore leaves the existing icons untouched instead of wiping them.
 * A genuinely empty result is trusted (it means nothing is watched); the
 * user-armed entries in the service survive regardless.
 */
object WatchpointFrameSync {
    private val logger = Logger.getInstance(WatchpointFrameSync::class.java)

    /**
     * Read the live cross-frame watch set off the EDT and publish it.
     *
     * @param isCancelled checked before the blocking eval and before the
     *   repaint; lets a per-session caller (the highlighter) bail out once its
     *   session has been disposed. Defaults to "never cancelled" for one-shot
     *   callers like the arm/remove action.
     */
    fun refresh(
        project: Project,
        debugProcess: PyDebugProcess,
        markerService: WatchpointMarkerService,
        isCancelled: () -> Boolean = { false },
    ) {
        ApplicationManager.getApplication().executeOnPooledThread {
            if (isCancelled()) return@executeOnPooledThread
            // Verify the session is still alive before the blocking eval call.
            if (runCatching { debugProcess.session.isStopped }.getOrDefault(true)) {
                return@executeOnPooledThread
            }
            val pyValue = try {
                // execute=false, doTrunc=false: the payload can exceed the
                // 256-char truncation limit, so bypass it (same as the hit drain).
                debugProcess.evaluate("_pycharm_locate_watches()", false, false)
            } catch (e: Exception) {
                // Not a watchpoint session (builtin absent) or the evaluator is
                // mid-step in a volatile frame – skip without touching state.
                return@executeOnPooledThread
            }
            val raw = pyValue?.value ?: return@executeOnPooledThread
            val payload = stripOuterQuotes(raw)
            // ERROR:-prefixed → runtime computed nothing reliable; do not wipe.
            if (payload.startsWith("ERROR")) return@executeOnPooledThread
            val synced = decodeLocatePayload(payload)
                ?: return@executeOnPooledThread  // malformed base64 → leave state alone
            markerService.replaceSynced(synced)

            // Repaint the currently-shown frame's tree so the icons appear
            // without waiting for the next natural paint. Other frames repaint
            // when the user selects them – this single scan already covers them.
            ApplicationManager.getApplication().invokeLater {
                if (isCancelled()) return@invokeLater
                WatchpointUiUtil.currentVariablesTree(project)?.repaint()
            }
        }
    }

    /**
     * Strip the single- or double-quote wrapper pydevd's evaluator places
     * around a returned Python string. If there is no wrapper (some builds skip
     * it for plain ASCII), the value is returned unchanged.
     */
    private fun stripOuterQuotes(s: String): String {
        val trimmed = s.trim()
        if (trimmed.length < 2) return trimmed
        val first = trimmed.first()
        val last = trimmed.last()
        return if ((first == '\'' || first == '"') && first == last) {
            trimmed.substring(1, trimmed.length - 1)
        } else {
            trimmed
        }
    }

    /**
     * Decode the `_pycharm_locate_watches()` payload: base64 of UTF-8 whose
     * decoded form is `name`+U+0000+`frameId` records separated by U+0001.
     * Returns the parsed set, an empty set for the empty payload ("no watches",
     * authoritative), or null if the base64 itself is malformed (caller then
     * leaves the existing synced set untouched).
     */
    private fun decodeLocatePayload(payload: String): Set<WatchpointMarkerService.WatchKey>? {
        if (payload.isEmpty()) return emptySet()
        return try {
            val raw = String(Base64.getDecoder().decode(payload), Charsets.UTF_8)
            raw.split('\u0001').mapNotNullTo(HashSet()) { record ->
                val fields = record.split('\u0000')
                if (fields.size != 2) return@mapNotNullTo null
                val frameId = fields[1].toLongOrNull() ?: return@mapNotNullTo null
                WatchpointMarkerService.WatchKey(fields[0], frameId)
            }
        } catch (e: Exception) {
            logger.warn("Could not decode locate-watches payload: ${e.message}")
            null
        }
    }
}
