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
    private val watched: MutableSet<String> =
        Collections.newSetFromMap(ConcurrentHashMap<String, Boolean>())

    /** Register `expression` (e.g. `"x"` or `"request.user.email"`) as currently watched. */
    fun add(expression: String) {
        watched.add(expression)
    }

    /** Stop treating `expression` as watched – the matching row stops being highlighted on next paint. */
    fun remove(expression: String) {
        watched.remove(expression)
    }

    /** Return true iff `expression` was registered via [add] and not since [remove]d. */
    fun isWatched(expression: String): Boolean = expression in watched

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
