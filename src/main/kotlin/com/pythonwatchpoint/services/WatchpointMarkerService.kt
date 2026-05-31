package com.pythonwatchpoint.services

import com.intellij.openapi.components.Service
import com.intellij.openapi.project.Project
import java.util.Collections
import java.util.concurrent.ConcurrentHashMap

/**
 * Project-scoped record of which watch expressions are currently armed, kept
 * in sync with the Python runtime's registry by the actions that arm/disarm
 * watches.
 *
 * Watches are keyed by (expression, frameId) — the Python-side `id(frame)` —
 * so that a watch on `"self"` only highlights the specific frame instance it
 * was armed against, not every variable named `"self"` across all threads and
 * call-stack frames. This mirrors the runtime's own `(name, id(frame))` key
 * from §1 of the design contract.
 *
 * The custom Variables-panel cell renderer reads from this service on every
 * cell paint to decide whether a row should be highlighted, so the data has
 * to be cheap to query (set membership) and safe to access from the EDT and
 * background threads alike. A concurrent hash-backed set covers both.
 *
 * Lifetime: cleared whenever a new debug session starts (see
 * `WatchpointDebugListener.processStarted`) so that watchpoints from a
 * previous run don't decorate same-named variables in the new run.
 */
@Service(Service.Level.PROJECT)
class WatchpointMarkerService {

    /**
     * Identifies a single armed watch. `frameId` is the Python `id(frame)`
     * value returned by `watch_at` on success, so it uniquely identifies one
     * live frame instance — not just a function or a variable name.
     */
    data class WatchKey(val expression: String, val frameId: Long)

    private val watched: MutableSet<WatchKey> =
        Collections.newSetFromMap(ConcurrentHashMap<WatchKey, Boolean>())

    /**
     * Register `expression` as watched in the frame identified by `frameId`
     * (the Python `id(frame)` returned by `watch_at`).
     */
    fun add(expression: String, frameId: Long) {
        watched.add(WatchKey(expression, frameId))
    }

    /**
     * Disarm the watch for `expression` regardless of which frame it was
     * registered against. Called from the Remove path where we only have the
     * expression string — the frame has already been cleaned up by the runtime.
     */
    fun remove(expression: String) {
        watched.removeIf { it.expression == expression }
    }

    /**
     * Return true iff `expression` is watched in the frame identified by
     * `frameId`. This is the renderer's hot path — O(1) set lookup.
     */
    fun isWatched(expression: String, frameId: Long): Boolean =
        WatchKey(expression, frameId) in watched

    /**
     * Return true iff `expression` is watched in ANY frame. Used only by
     * [AddWatchpointAction.update] to decide the context-menu label; do NOT
     * use this from the renderer (it's the source of the original bug).
     */
    fun isWatched(expression: String): Boolean =
        watched.any { it.expression == expression }

    /** Drop all entries. Called on session start so stale watches don't leak across runs. */
    fun clear() {
        watched.clear()
    }

    companion object {
        @JvmStatic
        fun getInstance(project: Project): WatchpointMarkerService =
            project.getService(WatchpointMarkerService::class.java)
    }
}