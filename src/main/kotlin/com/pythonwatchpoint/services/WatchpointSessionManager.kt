package com.pythonwatchpoint.services

import com.intellij.openapi.components.Service
import com.intellij.openapi.project.Project
import com.intellij.xdebugger.XDebugProcess
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicReference

/**
 * Per-project carrier for the `_pycharm_watchpoint` runtime package that the
 * "Debug with Watchpoint" action wants the next debug session to inject. The
 * package is carried as a `filename -> source` map (one entry per submodule).
 * The session listener consumes the stored map on processStarted, leaving the
 * manager empty for plain Debug runs.
 *
 * Also tracks which debug processes are active watchpoint sessions, so that
 * the "Add Watchpoint" action can hide itself during plain (non-watchpoint) runs.
 */
@Service(Service.Level.PROJECT)
class WatchpointSessionManager(private val project: Project) {

    private val watchpointPackage = AtomicReference<Map<String, String>?>(null)
    private val activeProcesses = ConcurrentHashMap.newKeySet<XDebugProcess>()

    /** Stash the runtime package (filename -> source) the next session should inject. */
    fun startSession(pkg: Map<String, String>) {
        watchpointPackage.set(pkg)
    }

    /** Atomically retrieve and clear the queued package (single-use per session). */
    fun consumeWatchpointPackage(): Map<String, String>? {
        return watchpointPackage.getAndSet(null)
    }

    /** Register [process] as a live watchpoint session (called by the debug listener on start). */
    fun markActive(process: XDebugProcess) {
        activeProcesses.add(process)
    }

    /** Deregister [process] when it stops (called by the debug listener on stop). */
    fun markInactive(process: XDebugProcess) {
        activeProcesses.remove(process)
    }

    /** True only when [process] was launched via "Debug with Watchpoint". */
    fun isActiveWatchpointSession(process: XDebugProcess): Boolean {
        return process in activeProcesses
    }

    companion object {
        fun getInstance(project: Project): WatchpointSessionManager {
            return project.getService(WatchpointSessionManager::class.java)
        }
    }
}
