package com.pythonwatchpoint.services

import com.intellij.openapi.components.Service
import com.intellij.openapi.project.Project
import java.util.concurrent.atomic.AtomicReference

/**
 * Per-project carrier for the watchpoint.py source that the "Debug with Watchpoint"
 * action wants the next debug session to inject. The session listener consumes the
 * stored code on processStarted, leaving the manager empty for plain Debug runs.
 */
@Service(Service.Level.PROJECT)
class WatchpointSessionManager(private val project: Project) {

    private val watchpointCode = AtomicReference<String?>(null)

    /** Stash the watchpoint.py source that the next debug session should inject. */
    fun startSession(code: String) {
        watchpointCode.set(code)
    }

    /** Atomically retrieve and clear the queued code (single-use per session). */
    fun consumeWatchpointCode(): String? {
        return watchpointCode.getAndSet(null)
    }

    companion object {
        fun getInstance(project: Project): WatchpointSessionManager {
            return project.getService(WatchpointSessionManager::class.java)
        }
    }
}
