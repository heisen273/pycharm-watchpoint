package com.pythonwatchpoint.services

import com.intellij.openapi.components.Service
import com.intellij.openapi.project.Project

/**
 * Per-project carrier for state that the "Debug with Watchpoint" action wants the
 * next debug session to consume:
 *  - the watchpoint.py source to inject via sitecustomize
 *  - the temp directory path (where sitecustomize.py + per-session hit files live)
 *
 * The session listener consumes both on processStarted; the temp dir is then
 * passed to `WatchpointHitHighlighter` so it can look up the per-hit JSON file
 * without needing to extract the user-process PID. Going via this manager avoids
 * fragile reflection on SimpleProcessHandler (which doesn't expose getProcess()).
 */
@Service(Service.Level.PROJECT)
class WatchpointSessionManager(private val project: Project) {

    @Volatile
    private var watchpointCode: String? = null

    @Volatile
    private var sessionTempDir: String? = null

    /** Stash the watchpoint.py source + per-session temp dir for the next debug session. */
    fun startSession(code: String, tempDir: String) {
        watchpointCode = code
        sessionTempDir = tempDir
    }

    /** Atomically retrieve and clear the queued code (single-use per session). */
    fun consumeWatchpointCode(): String? {
        val result = watchpointCode
        watchpointCode = null
        return result
    }

    /**
     * The temp dir for the most recently-started session. NOT cleared on consume –
     * the highlighter reads it on every sessionPaused for the duration of the
     * session, and a new "Debug with Watchpoint" overwrites it. Multiple
     * concurrent watchpoint sessions in the same project would race on this slot;
     * acceptable trade-off given that PyCharm seldom runs two debug sessions
     * concurrently.
     */
    fun currentSessionTempDir(): String? = sessionTempDir

    companion object {
        fun getInstance(project: Project): WatchpointSessionManager {
            return project.getService(WatchpointSessionManager::class.java)
        }
    }
}
