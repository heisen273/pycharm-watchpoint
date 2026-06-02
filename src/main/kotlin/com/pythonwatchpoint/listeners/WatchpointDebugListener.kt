package com.pythonwatchpoint.listeners

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.application.WriteAction
import com.intellij.openapi.diagnostic.Logger
import com.intellij.openapi.project.Project
import com.intellij.xdebugger.XDebugProcess
import com.intellij.xdebugger.XDebuggerManager
import com.intellij.xdebugger.XDebuggerManagerListener
import com.intellij.xdebugger.breakpoints.XBreakpoint
import com.intellij.xdebugger.breakpoints.XBreakpointManager
import com.intellij.xdebugger.breakpoints.XBreakpointType
import com.intellij.xdebugger.evaluation.XDebuggerEvaluator
import com.intellij.xdebugger.frame.XValue
import com.jetbrains.python.debugger.PyDebugProcess
import com.jetbrains.python.debugger.PyDebugValue
import com.jetbrains.python.debugger.PyExceptionBreakpointProperties
import com.jetbrains.python.debugger.PyExceptionBreakpointType
import com.pythonwatchpoint.services.WatchpointMarkerService
import com.pythonwatchpoint.services.WatchpointSessionManager
import java.util.Base64
import java.util.concurrent.ConcurrentHashMap

/**
 * Hooks into every debug session that the "Debug with Watchpoint" action queued:
 *
 *  1. On processStarted, verify whether watchpoint.py was already booted via
 *     sitecustomize (the normal path). If not, fall back to a base64+exec inject
 *     via the debug evaluator so the user still gets watch()/unwatch() functions.
 *  2. Register a Python exception breakpoint targeting _pycharm_watchpoint.WatchpointHit
 *     so that the moment one is raised the debugger pauses at the assignment line.
 *  3. On processStopped, remove the breakpoint so it doesn't linger across
 *     non-watchpoint debug sessions.
 *
 * The breakpoint is keyed per-process so two simultaneous debug sessions (rare
 * in PyCharm, but possible) don't trip over each other's cleanup.
 */
class WatchpointDebugListener(private val project: Project) : XDebuggerManagerListener {
    private val logger = Logger.getInstance(WatchpointDebugListener::class.java)
    private val injected = ConcurrentHashMap.newKeySet<PyDebugProcess>()
    private val breakpoints = ConcurrentHashMap<XDebugProcess, XBreakpoint<PyExceptionBreakpointProperties>>()

    // One highlighter per session – tracks the change-line marker so we can
    // deregister cleanly on processStopped (the listener itself holds session
    // references that would otherwise outlive the debug process).
    private val highlighters = ConcurrentHashMap<XDebugProcess, WatchpointHitHighlighter>()

    override fun processStarted(debugProcess: XDebugProcess) {
        if (debugProcess !is PyDebugProcess) return

        // Defensive: purge entries for dead processes that never got processStopped.
        // This prevents unbounded accumulation if sessions crash without cleanup.
        breakpoints.keys.removeIf { proc ->
            runCatching { proc.session.isStopped }.getOrDefault(true)
        }
        highlighters.keys.removeIf { proc ->
            runCatching { proc.session.isStopped }.getOrDefault(true)
        }

        val watchpointPackage = WatchpointSessionManager.getInstance(project).consumeWatchpointPackage()
        if (watchpointPackage == null) return

        logger.warn("=== WATCHPOINT SESSION STARTED ===")

        // Drop any watched paths inherited from a previous session – otherwise
        // a same-named local variable in this run would render with the watch
        // highlight even though no `watch()` has been called yet.
        WatchpointMarkerService.getInstance(project).clear()

        // Add the exception breakpoint immediately; pydevd picks it up at handshake.
        addWatchpointHitBreakpoint(debugProcess)

        // Install the hit highlighter on this session. It reads the most recent
        // watchpoint hit (published by `watchpoint.py` into builtins) every time
        // the session pauses, and decorates the change line for visual context.
        attachHitHighlighter(debugProcess)

        // Give pydevd a moment to set up the evaluator, then verify/fall-back-inject.
        ApplicationManager.getApplication().executeOnPooledThread {
            // Retry up to 5 times with exponential backoff instead of a single
            // fixed sleep – accommodates slow interpreters / remote debuggers.
            var retries = 0
            val maxRetries = 5
            var delayMs = 300L
            while (retries < maxRetries) {
                Thread.sleep(delayMs)
                if (debugProcess.evaluator != null) break
                retries++
                delayMs = (delayMs * 1.5).toLong()
            }
            verifyOrInject(debugProcess, watchpointPackage)
        }
    }

    override fun processStopped(debugProcess: XDebugProcess) {
        (debugProcess as? PyDebugProcess)?.let { injected.remove(it) }
        removeWatchpointHitBreakpoint(debugProcess)
        detachHitHighlighter(debugProcess)
    }

    // ------------------------------------------------------------------
    // Hit-line highlighter management
    // ------------------------------------------------------------------

    private fun attachHitHighlighter(debugProcess: PyDebugProcess) {
        try {
            val session = debugProcess.session
            val highlighter = WatchpointHitHighlighter(
                project,
                session,
                WatchpointMarkerService.getInstance(project),
            )
            session.addSessionListener(highlighter)
            highlighters[debugProcess] = highlighter
        } catch (e: Exception) {
            // Highlighter is a UX nicety, not load-bearing for debugging. If
            // something refuses to wire up (e.g. session not yet available),
            // log and move on – the rest of the session still works.
            logger.warn("Could not attach watchpoint hit highlighter: ${e.message}")
        }
    }

    private fun detachHitHighlighter(debugProcess: XDebugProcess) {
        val highlighter = highlighters.remove(debugProcess) ?: return
        // Tear the highlighter down BEFORE removing it from the session –
        // a hard "Stop debug" doesn't reliably deliver `sessionStopped` to our
        // listener (the platform may have already torn down the session by the
        // time processStopped fires on us), so we explicitly clear any visible
        // highlight here. `dispose()` itself sets a guard that suppresses any
        // late-arriving sessionPaused callbacks.
        try {
            highlighter.dispose()
        } catch (e: Exception) {
            logger.warn("Could not dispose watchpoint hit highlighter: ${e.message}")
        }
        try {
            (debugProcess as? PyDebugProcess)?.session?.removeSessionListener(highlighter)
        } catch (e: Exception) {
            logger.warn("Could not detach watchpoint hit highlighter: ${e.message}")
        }
    }

    // ------------------------------------------------------------------
    // Exception-breakpoint management
    // ------------------------------------------------------------------

    /**
     * Add a Python exception breakpoint for `_pycharm_watchpoint.WatchpointHit`, configured
     * to pause on raise (not just on uncaught) so the debugger always halts at
     * the assignment site even if user code happens to wrap it in try/except.
     *
     * `notifyOnTerminate=true`  → also break if the exception propagates to top
     * `notifyOnlyOnFirst=false` → break on every occurrence, not just first
     *
     * Before registering, sweeps for any stale WatchpointHit breakpoints left in the
     * persistent breakpoint manager by a previous crashed session or an older plugin
     * version (which used `watchpoint.WatchpointHit` without the underscore prefix).
     */
    private fun addWatchpointHitBreakpoint(debugProcess: XDebugProcess) {
        try {
            val breakpoint = WriteAction.computeAndWait<XBreakpoint<PyExceptionBreakpointProperties>, Exception> {
                val manager: XBreakpointManager = XDebuggerManager.getInstance(project).breakpointManager
                val type = XBreakpointType.EXTENSION_POINT_NAME
                    .findExtensionOrFail(PyExceptionBreakpointType::class.java)

                // Remove stale WatchpointHit breakpoints that may have been persisted
                // by a crashed session or an older plugin version (e.g. the old name
                // `watchpoint.WatchpointHit` without the underscore prefix).
                // `getException()` is declared on the superclass ExceptionBreakpointProperties.
                @Suppress("UNCHECKED_CAST")
                val existing = manager.getBreakpoints(type)
                for (bp in existing.toList()) {  // toList() – copy before mutating
                    val exceptionName = bp.properties?.exception ?: continue
                    if ("WatchpointHit" in exceptionName) {
                        manager.removeBreakpoint(bp)
                        logger.warn("Removed stale WatchpointHit breakpoint: $exceptionName")
                    }
                }

                val props = PyExceptionBreakpointProperties("_pycharm_watchpoint.WatchpointHit")
                props.isNotifyOnTerminate = true
                props.isNotifyOnlyOnFirst = false
                props.isIgnoreLibraries = false

                manager.addBreakpoint(type, props)
            }
            breakpoints[debugProcess] = breakpoint
            logger.warn("Added WatchpointHit exception breakpoint for ${debugProcess.session.sessionName}")
        } catch (e: Exception) {
            logger.error("Failed to add WatchpointHit breakpoint", e)
        }
    }

    private fun removeWatchpointHitBreakpoint(debugProcess: XDebugProcess) {
        val breakpoint = breakpoints.remove(debugProcess) ?: return
        try {
            WriteAction.runAndWait<Exception> {
                XDebuggerManager.getInstance(project).breakpointManager.removeBreakpoint(breakpoint)
            }
            logger.warn("Removed WatchpointHit exception breakpoint")
        } catch (e: Exception) {
            logger.warn("Failed to remove WatchpointHit breakpoint: ${e.message}")
        }
    }

    // ------------------------------------------------------------------
    // watchpoint.py boot verification / fallback inject
    // ------------------------------------------------------------------

    private fun verifyOrInject(process: PyDebugProcess, watchpointPackage: Map<String, String>) {
        val evaluator = process.evaluator ?: return

        // Probe builtins for the registry the script publishes on load.
        val checkCommand = """
import builtins
"ALREADY_LOADED" if hasattr(builtins, '_watchpoint_registry') else "NEED_INJECTION"
""".trimIndent()

        evaluator.evaluate(checkCommand, object : XDebuggerEvaluator.XEvaluationCallback {
            override fun evaluated(result: XValue) {
                // PyDebugValue.toString() returns the EXPRESSION text (the name
                // shown in the Variables tree), NOT the evaluated value. The
                // expression text contains "NEED_INJECTION" as a literal, so
                // checking toString() would always match. Use .value instead.
                val resultStr = (result as? PyDebugValue)?.value ?: result.toString()
                if (resultStr.contains("NEED_INJECTION")) {
                    injectAsFallback(process, watchpointPackage)
                } else {
                    logger.warn("_pycharm_watchpoint already booted via sitecustomize – no fallback needed")
                }
            }

            override fun errorOccurred(errorMessage: String) {
                logger.warn("Probe error – attempting fallback inject: $errorMessage")
                injectAsFallback(process, watchpointPackage)
            }
        }, null)
    }

    private fun injectAsFallback(process: PyDebugProcess, watchpointPackage: Map<String, String>) {
        val evaluator = process.evaluator ?: return
        if (!injected.add(process)) return

        logger.warn("Injecting _pycharm_watchpoint package as fallback")

        // Build a Python dict literal of {filename: base64(source)} for every
        // submodule, then have the debuggee write them to a fresh temp dir and
        // `import _pycharm_watchpoint`. We materialize real files (rather than
        // exec a blob into one module dict) so the package's relative imports and
        // WatchpointHit.__module__ == '_pycharm_watchpoint' both resolve – exactly
        // as the sitecustomize path does. Local debugging shares the filesystem,
        // so files written from the evaluated code are importable in-process.
        val filesDict = watchpointPackage.entries.joinToString(",\n        ") { (name, src) ->
            val b64 = Base64.getEncoder().encodeToString(src.toByteArray())
            "'$name': '$b64'"
        }

        val command = """
import base64, builtins, sys, os, tempfile
try:
    if hasattr(builtins, '_watchpoint_registry'):
        print("[WATCHPOINT] Fallback skipped: _pycharm_watchpoint already loaded")
    else:
        _files = {
        $filesDict
        }
        _d = tempfile.mkdtemp(prefix='pycharm_wp_fb_')
        _pkg = os.path.join(_d, '_pycharm_watchpoint')
        os.makedirs(_pkg, exist_ok=True)
        for _n, _b in _files.items():
            with open(os.path.join(_pkg, _n), 'wb') as _f:
                _f.write(base64.b64decode(_b))
        if _d not in sys.path:
            sys.path.insert(0, _d)
        import _pycharm_watchpoint
        print("[WATCHPOINT] Fallback injection successful")
except Exception as e:
    print(f"[WATCHPOINT] Fallback failed: {e}")
""".trimIndent()

        evaluator.evaluate(command, object : XDebuggerEvaluator.XEvaluationCallback {
            override fun evaluated(result: XValue) {
                logger.warn("Fallback inject result: $result")
            }
            override fun errorOccurred(errorMessage: String) {
                logger.warn("Fallback inject error: $errorMessage")
            }
        }, null)
    }
}