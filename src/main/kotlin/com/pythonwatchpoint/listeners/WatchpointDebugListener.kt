package com.pythonwatchpoint.listeners

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.application.ReadAction
import com.intellij.openapi.application.WriteAction
import com.intellij.openapi.diagnostic.Logger
import com.intellij.openapi.project.Project
import com.intellij.xdebugger.XDebugProcess
import com.intellij.xdebugger.XDebugSessionListener
import com.intellij.xdebugger.XDebuggerManager
import com.intellij.xdebugger.XDebuggerManagerListener
import com.intellij.xdebugger.breakpoints.XBreakpoint
import com.intellij.xdebugger.breakpoints.XBreakpointManager
import com.intellij.xdebugger.breakpoints.XBreakpointType
import com.intellij.xdebugger.evaluation.XDebuggerEvaluator
import com.intellij.xdebugger.frame.XValue
import com.jetbrains.python.debugger.PyDebugProcess
import com.jetbrains.python.debugger.PyExceptionBreakpointProperties
import com.jetbrains.python.debugger.PyExceptionBreakpointType
import com.pythonwatchpoint.services.WatchpointMarkerService
import com.pythonwatchpoint.services.WatchpointSessionManager
import java.util.Base64
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Hooks into every debug session that the "Debug with Watchpoint" action queued:
 *
 *  1. On processStarted, verify whether watchpoint.py was already booted via
 *     sitecustomize (the normal path). If not, fall back to a base64+exec inject
 *     via the debug evaluator so the user still gets watch()/unwatch() functions.
 *  2. Register a Python exception breakpoint targeting watchpoint.WatchpointHit
 *     so that the moment one is raised the debugger pauses at the assignment line.
 *  3. On processStopped, remove the breakpoint so it doesn't linger across
 *     non-watchpoint debug sessions.
 *
 * The breakpoint is keyed per-process so two simultaneous debug sessions (rare
 * in PyCharm, but possible) don't trip over each other's cleanup.
 */
class WatchpointDebugListener(private val project: Project) : XDebuggerManagerListener {
    private val logger = Logger.getInstance(WatchpointDebugListener::class.java)
    // `XDebugProcess`-typed (not `PyDebugProcess`) so we can also track sessions
    // started under debugpy's `DapXDebugProcess`. The exception-breakpoint logic
    // still gates on `PyDebugProcess` because `PyExceptionBreakpointType` is
    // pydevd-specific; debugpy ignores it. Tracked as a follow-up: DAP-native
    // exception-breakpoint registration so watchpoint hits also pause under debugpy.
    private val injected = ConcurrentHashMap.newKeySet<XDebugProcess>()
    private val breakpoints = ConcurrentHashMap<XDebugProcess, XBreakpoint<PyExceptionBreakpointProperties>>()

    // One highlighter per session – tracks the change-line marker so we can
    // deregister cleanly on processStopped (the listener itself holds session
    // references that would otherwise outlive the debug process).
    private val highlighters = ConcurrentHashMap<XDebugProcess, WatchpointHitHighlighter>()

    override fun processStarted(debugProcess: XDebugProcess) {
        val watchpointCode = WatchpointSessionManager.getInstance(project).consumeWatchpointCode()
        if (watchpointCode == null) return  // Not a watchpoint-initiated session.

        // We deliberately accept both PyDebugProcess (pydevd) and DapXDebugProcess (debugpy).
        // What's debugger-specific is gated below, not at this entry point.
        val backend = if (debugProcess is PyDebugProcess) "pydevd" else debugProcess::class.simpleName ?: "unknown"
        logger.warn("=== WATCHPOINT SESSION STARTED (backend=$backend) ===")

        // Drop any watched paths inherited from a previous session – otherwise
        // a same-named local variable in this run would render with the watch
        // highlight even though no `watch()` has been called yet.
        WatchpointMarkerService.getInstance(project).clear()

        // The PyExceptionBreakpointType extension point is owned by pydevd; debugpy
        // does not honor it. Register only when we know pydevd will pick it up.
        // For debugpy the safety-net "pause on raised WatchpointHit" needs a DAP-side
        // mechanism instead – tracked as part of the broader debugpy backend project.
        if (debugProcess is PyDebugProcess) {
            addWatchpointHitBreakpoint(debugProcess)
        } else {
            logger.warn("debugpy backend detected – skipping PyExceptionBreakpointType (pydevd-only); " +
                "WatchpointHit will surface as an uncaught exception until DAP integration lands.")
        }

        // Install the hit highlighter on this session. It reads the most recent
        // watchpoint hit (published by `watchpoint.py` into builtins) every time
        // the session pauses, and decorates the change line for visual context.
        // Uses only the generic XDebugSession API → works under both debuggers.
        attachHitHighlighter(debugProcess)

        // Defer the runtime probe until the program is paused at a breakpoint.
        //
        // Under pydevd, XDebuggerEvaluator.evaluate() works any time after the
        // session is up – pydevd has a console/global eval channel. Under debugpy
        // the DAP evaluator only accepts requests for a paused thread; calling it
        // while the program is running returns "Server is not available", which
        // is exactly what we saw in idea.log before this fix.
        //
        // We register a one-shot session listener for the first sessionPaused, do
        // the probe there, then remove ourselves. Works the same way under both
        // backends; the previous Thread.sleep(500) was always a hack anyway.
        scheduleProbeOnFirstPause(debugProcess, watchpointCode)
    }

    /**
     * Register a one-shot listener that runs the runtime probe the first time the
     * session pauses. Idempotent: if the user resumes and re-pauses, we only fire once.
     */
    private fun scheduleProbeOnFirstPause(debugProcess: XDebugProcess, watchpointCode: String) {
        val session = debugProcess.session ?: run {
            logger.warn("scheduleProbeOnFirstPause: session is null; skipping probe")
            return
        }
        val fired = AtomicBoolean(false)
        val probeListener = object : XDebugSessionListener {
            override fun sessionPaused() {
                if (!fired.compareAndSet(false, true)) return
                try {
                    verifyOrInject(debugProcess, watchpointCode)
                } finally {
                    // Remove ourselves so future pauses don't re-trigger the probe.
                    // Has to happen on a separate dispatch – removing a listener from
                    // inside its own callback is fragile across IntelliJ versions.
                    ApplicationManager.getApplication().invokeLater {
                        try {
                            session.removeSessionListener(this)
                        } catch (e: Exception) {
                            logger.warn("Could not remove one-shot probe listener: ${e.message}")
                        }
                    }
                }
            }
        }
        session.addSessionListener(probeListener)
        logger.warn("Probe scheduled – will run on first sessionPaused")
    }

    override fun processStopped(debugProcess: XDebugProcess) {
        // ConcurrentHashMap rejects null keys with NPE. Under debugpy `debugProcess`
        // is a DapXDebugProcess (not a PyDebugProcess) – the old `as? PyDebugProcess`
        // cast produced null and crashed on remove(). The map is now XDebugProcess-keyed
        // so a direct remove() is safe regardless of backend.
        injected.remove(debugProcess)
        removeWatchpointHitBreakpoint(debugProcess)
        detachHitHighlighter(debugProcess)
    }

    // ------------------------------------------------------------------
    // Hit-line highlighter management
    // ------------------------------------------------------------------

    private fun attachHitHighlighter(debugProcess: XDebugProcess) {
        try {
            val session = debugProcess.session
            // Pass debugProcess explicitly – the highlighter needs a non-null
            // process reference at construction time (to wire up the stderr
            // marker listener) and `session.debugProcess` is sometimes still
            // lateinit-uninitialized under debugpy when we're called from
            // processStarted.
            val highlighter = WatchpointHitHighlighter(project, session, debugProcess)
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
            debugProcess.session?.removeSessionListener(highlighter)
        } catch (e: Exception) {
            logger.warn("Could not detach watchpoint hit highlighter: ${e.message}")
        }
    }

    // ------------------------------------------------------------------
    // Exception-breakpoint management
    // ------------------------------------------------------------------

    /**
     * Add a Python exception breakpoint for `watchpoint.WatchpointHit`, configured
     * to pause on raise (not just on uncaught) so the debugger always halts at
     * the assignment site even if user code happens to wrap it in try/except.
     *
     * `notifyOnTerminate=true`  → also break if the exception propagates to top
     * `notifyOnlyOnFirst=false` → break on every occurrence, not just first
     */
    private fun addWatchpointHitBreakpoint(debugProcess: XDebugProcess) {
        try {
            val breakpoint = WriteAction.computeAndWait<XBreakpoint<PyExceptionBreakpointProperties>, Exception> {
                val manager: XBreakpointManager = XDebuggerManager.getInstance(project).breakpointManager
                val type = XBreakpointType.EXTENSION_POINT_NAME
                    .findExtensionOrFail(PyExceptionBreakpointType::class.java)

                val props = PyExceptionBreakpointProperties("watchpoint.WatchpointHit")
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

    private fun verifyOrInject(process: XDebugProcess, watchpointCode: String) {
        val evaluator = process.evaluator ?: return

        // The probe is now called from a paused session (see scheduleProbeOnFirstPause)
        // so the evaluator is usable under both pydevd and debugpy. We do two passes:
        //   1. Probe: is the runtime present? (`hasattr(builtins, '_watchpoint_registry')`)
        //   2. Log the runtime's view of what backend it's running under, by evaluating
        //      `_pycharm_watchpoint_state()`. This is the post-debugpy-import view, so
        //      it reflects the real backend (vs. the boot-time `none` we print at
        //      sitecustomize).
        val checkCommand = """
import builtins
"ALREADY_LOADED" if hasattr(builtins, '_watchpoint_registry') else "NEED_INJECTION"
""".trimIndent()

        evaluator.evaluate(checkCommand, object : XDebuggerEvaluator.XEvaluationCallback {
            override fun evaluated(result: XValue) {
                val resultStr = result.toString()
                if (resultStr.contains("NEED_INJECTION")) {
                    injectAsFallback(process, watchpointCode)
                } else {
                    logger.warn("watchpoint.py already booted via sitecustomize – no fallback needed")
                    logRuntimeState(evaluator)
                }
            }

            override fun errorOccurred(errorMessage: String) {
                logger.warn("Probe error – attempting fallback inject: $errorMessage")
                injectAsFallback(process, watchpointCode)
            }
        }, null)
    }

    /**
     * Ask the runtime to print its state to stderr (Debug Console).
     *
     * `_pycharm_log_state()` is a side-effect-only helper: it prints to stderr and
     * returns just "OK". That matters under debugpy, where `XValue.toString()` on
     * the IDE side comes back empty for DAP-evaluated expressions – so reading the
     * return value gives us nothing. Routing through stderr means the diagnostic
     * shows up in the Debug Console regardless of how the IDE wraps the result.
     *
     * Under pydevd this works identically; the runtime simply prints to the same
     * stderr stream the user sees as the Debug Console.
     */
    private fun logRuntimeState(evaluator: XDebuggerEvaluator) {
        evaluator.evaluate(
            "_pycharm_log_state()",
            object : XDebuggerEvaluator.XEvaluationCallback {
                override fun evaluated(result: XValue) {
                    logger.warn("Runtime state probe sent – check Debug Console for [WATCHPOINT/probe] line.")
                }
                override fun errorOccurred(errorMessage: String) {
                    logger.warn("Could not invoke _pycharm_log_state: $errorMessage")
                }
            },
            null,
        )
    }

    private fun injectAsFallback(process: XDebugProcess, watchpointCode: String) {
        if (!injected.add(process)) return

        logger.warn("Injecting watchpoint.py as fallback")
        val evaluator = process.evaluator ?: return
        val encoded = Base64.getEncoder().encodeToString(watchpointCode.toByteArray())

        // Run in a synthetic module named 'watchpoint' so the WatchpointHit class
        // resolves to module 'watchpoint' – which matches the exception breakpoint
        // we registered for "watchpoint.WatchpointHit".
        val command = """
import base64, types, sys
try:
    _wp_mod = types.ModuleType('watchpoint')
    sys.modules['watchpoint'] = _wp_mod
    exec(base64.b64decode('$encoded').decode('utf-8'), _wp_mod.__dict__)
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
