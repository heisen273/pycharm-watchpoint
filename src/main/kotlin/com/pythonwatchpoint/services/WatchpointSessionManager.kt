package com.pythonwatchpoint.services

import com.intellij.openapi.components.Service
import com.intellij.openapi.project.Project
import java.util.concurrent.atomic.AtomicReference

/**
 * Per-project carrier for the `_pycharm_watchpoint` runtime package that the
 * "Debug with Watchpoint" action wants the next debug session to inject. The
 * package is carried as a `filename -> source` map (one entry per submodule).
 * The session listener consumes the stored map on processStarted, leaving the
 * manager empty for plain Debug runs.
 */
@Service(Service.Level.PROJECT)
class WatchpointSessionManager(private val project: Project) {

    private val watchpointPackage = AtomicReference<Map<String, String>?>(null)

    /** Stash the runtime package (filename -> source) the next session should inject. */
    fun startSession(pkg: Map<String, String>) {
        watchpointPackage.set(pkg)
    }

    /** Atomically retrieve and clear the queued package (single-use per session). */
    fun consumeWatchpointPackage(): Map<String, String>? {
        return watchpointPackage.getAndSet(null)
    }

    companion object {
        fun getInstance(project: Project): WatchpointSessionManager {
            return project.getService(WatchpointSessionManager::class.java)
        }
    }
}
