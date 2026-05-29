package com.pythonwatchpoint.listeners

import com.intellij.execution.process.ProcessAdapter
import com.intellij.execution.process.ProcessEvent
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.diagnostic.Logger
import com.intellij.openapi.util.Key
import com.intellij.openapi.editor.Editor
import com.intellij.openapi.editor.EditorCustomElementRenderer
import com.intellij.openapi.editor.Inlay
import com.intellij.openapi.editor.colors.EditorFontType
import com.intellij.openapi.editor.ex.RangeHighlighterEx
import com.intellij.openapi.editor.markup.HighlighterLayer
import com.intellij.openapi.editor.markup.HighlighterTargetArea
import com.intellij.openapi.editor.markup.RangeHighlighter
import com.intellij.openapi.editor.markup.TextAttributes
import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.openapi.fileEditor.OpenFileDescriptor
import com.intellij.openapi.project.Project
import com.intellij.openapi.util.Disposer
import com.intellij.openapi.vfs.LocalFileSystem
import com.intellij.ui.JBColor
import com.intellij.util.Alarm
import com.intellij.util.ui.JBUI
import com.intellij.xdebugger.XDebugProcess
import com.intellij.xdebugger.XDebugSession
import com.intellij.xdebugger.XDebugSessionListener
import com.jetbrains.python.debugger.PyDebugProcess
import com.pythonwatchpoint.services.WatchpointSessionManager
import java.awt.Color
import java.awt.Font
import java.awt.Graphics
import java.awt.Rectangle
import java.util.Base64

/**
 * Per-session listener that decorates the line where a watchpoint just fired,
 * so the user understands WHY execution paused.
 *
 * Flow on every `sessionPaused`:
 *  1. Evaluate `_pycharm_consume_last_hit()` against the user's frame – this
 *     is a builtin published by `watchpoint.py`. Empty string ⇒ the pause was
 *     not caused by a watchpoint hit (regular breakpoint, step, etc.) and we
 *     do nothing.
 *  2. Decode the payload (base64 of UTF-8 NUL-separated fields: file, line,
 *     name, old, new) into a [HitInfo].
 *  3. Open the file (without stealing focus from the debugger), then install a
 *     theme-aware line highlighter on the change line plus a gutter scrollbar
 *     mark with a tooltip describing the change. The highlight is cleared on
 *     the next `sessionResumed` / `sessionStopped`.
 *
 * Why we don't reuse the existing exception-breakpoint UX: the breakpoint
 * is just a safety net for the no-pydevd-installed case. The normal pause
 * path goes through pydevd's settrace flow (see watchpoint.py
 * `_pause_via_pydevd`) which produces a clean stop but offers no native
 * "why are we paused?" signal in the editor – this listener provides it.
 */
class WatchpointHitHighlighter(
    private val project: Project,
    private val session: XDebugSession,
    // Passed in directly because `session.debugProcess` is lateinit-uninitialized
    // at construction time under debugpy (`PythonDapDebugProcess`) – touching it
    // throws NPE before the session is fully wired up. Going through the
    // explicit reference avoids that race.
    private val debugProcess: XDebugProcess,
) : XDebugSessionListener {
    private val logger = Logger.getInstance(WatchpointHitHighlighter::class.java)

    // Multiple highlights at once: a single pause can coalesce several hits if
    // they fire on consecutive lines (e.g. inside a function with no
    // intermediate non-mutation lines). We render all of them and clear them
    // all on the next sessionResumed.
    private val currentHighlights: MutableList<HighlightHandle> = mutableListOf()

    // Set to true the first (and only) time `dispose()` is called. After that
    // every entry point (sessionPaused → evaluator callback, sessionResumed,
    // applyHighlight queued onto the EDT) early-returns – this prevents the
    // listener from installing a fresh highlight after the session has been
    // stopped, which the platform doesn't otherwise guarantee.
    @Volatile
    private var disposed = false

    private data class HighlightHandle(
        val editor: Editor,
        val highlighter: RangeHighlighter,
        val inlay: Inlay<*>?,
        val pulseAlarm: Alarm,
    )

    private data class HitInfo(
        val file: String,
        val line: Int,
        val name: String,
        val old: String,
        val new: String,
    )

    // ---- pulse animation tunables ----
    // 1700ms total: long enough to draw the eye without lingering.
    // 60ms tick is the slowest cadence where motion looks smooth on a
    // non-ProMotion display.
    private val pulseTotalMs = 1500L
    private val pulseTickMs = 60

    // Warm ember base; peak flares to bright orange so the initial pop
    // reads like a glowing coal catching light.
    private val baseLight = Color(255, 210, 140)
    private val baseDark = Color(105, 90, 30)
    private val peakLight = Color(255, 140, 50)
    private val peakDark = Color(190, 145, 30)

    // Buffer for the most recent `[WATCHPOINT/event]<base64>` line emitted by
    // the runtime to stderr. Updated by [markerListener]; consumed (set to
    // null) by [sessionPaused] when used as the data source under debugpy.
    //
    // Why a buffer instead of consuming directly inside the listener: the
    // listener fires from a background thread on every stderr chunk, but the
    // UI rendering must happen from sessionPaused (which is the actual debug
    // event we attach to). Two events arriving close together is fine – the
    // newer one overwrites the older, which matches the runtime's
    // `_last_hit` single-slot semantics.
    @Volatile
    private var pendingMarkerEncoded: String? = null

    // Stderr can arrive in fragments – debugpy's launcher sometimes splits
    // long lines around buffer boundaries. We accumulate a per-stream tail
    // until we see a newline, then process complete lines only. Map keyed by
    // outputType (Key<String>) keeps stderr and stdout fragments separate.
    private val streamTails = java.util.concurrent.ConcurrentHashMap<Key<*>, StringBuilder>()

    private val markerListener = object : ProcessAdapter() {
        override fun onTextAvailable(event: ProcessEvent, outputType: Key<*>) {
            if (disposed) return
            val text = event.text
            if (text.isEmpty()) return
            val tail = streamTails.computeIfAbsent(outputType) { StringBuilder() }
            tail.append(text)
            // Process every complete line; keep any trailing partial in the tail.
            while (true) {
                val nl = tail.indexOf('\n')
                if (nl < 0) break
                val line = tail.substring(0, nl)
                tail.delete(0, nl + 1)
                handleLine(line)
            }
        }

        private fun handleLine(line: String) {
            val trimmed = line.trim()
            if (!trimmed.startsWith(MARKER_PREFIX)) return
            pendingMarkerEncoded = trimmed.substring(MARKER_PREFIX.length).trim()
        }
    }

    init {
        // Attach to the user-process's stderr/stdout stream so we can buffer
        // hit markers without going through the evaluator round-trip.
        // Safe to attach even if the process has already started: ProcessHandler
        // delivers an `onTextAvailable` for every subsequent chunk; we miss the
        // (typically empty at this point) backlog before us, which is fine
        // because hits can only happen after sessionStarted finishes anyway.
        //
        // Uses the directly-passed [debugProcess] (NOT `session.debugProcess`):
        // under debugpy the session's process reference isn't initialized yet
        // at our construction time, and accessing it throws a NPE that swallows
        // the attach silently.
        try {
            val handler = debugProcess.processHandler
            if (handler == null) {
                logger.warn("WatchpointHitHighlighter: processHandler null at init – marker listener not attached")
            } else {
                handler.addProcessListener(markerListener)
                logger.warn("WatchpointHitHighlighter: marker listener attached to ${handler::class.simpleName}")
            }
        } catch (e: Exception) {
            logger.warn("Could not attach process marker listener: ${e::class.simpleName}: ${e.message}")
        }
    }

    override fun sessionPaused() {
        if (disposed) return
        logger.warn("WatchpointHitHighlighter.sessionPaused: backend=${debugProcess::class.simpleName}")

        // Two data sources:
        //   1. pydevd: synchronous PyDebugProcess.evaluate('_pycharm_consume_last_hit()',
        //      doTrunc=false). Returns the full untruncated base64 payload.
        //   2. debugpy (or any backend without PyDebugProcess): file-based per-process
        //      hit JSON, plus a `[WATCHPOINT/event]<base64>` stderr marker as fallback.
        //
        // We try (1) first to keep behavior identical under pydevd, then fall back
        // to (2). Both ultimately produce a HitInfo through the same decode path.
        // Drain queued hits from the JSON-lines file. Same path under pydevd and
        // debugpy – the runtime appends one line per hit, we read & delete the
        // whole file in one pass. Multiple hits coalesced into a single pause
        // (consecutive mutations with no intervening non-mutation lines) all
        // render.
        //
        // ProcessListener is async on a background thread; the runtime flushes
        // before `_pause_via_pydevd` returns, but the IDE-side dispatch is
        // decoupled. Poll briefly for the file to materialize.
        ApplicationManager.getApplication().executeOnPooledThread {
            var attempt = 0
            while (attempt < 5 && !disposed) {
                val hits = readHitsFromFile()
                if (hits.isNotEmpty()) {
                    logger.warn("sessionPaused: ${hits.size} hit(s) resolved from FILE on attempt #$attempt")
                    // Drop any stream-buffered marker too – the file is authoritative.
                    pendingMarkerEncoded = null
                    ApplicationManager.getApplication().invokeLater {
                        hits.forEach { applyHighlight(it) }
                    }
                    return@executeOnPooledThread
                }
                // Stderr marker as a secondary signal (single hit only; the
                // file should always be the primary path going forward).
                val markerHit = consumeBufferedMarker()
                if (markerHit != null) {
                    logger.warn("sessionPaused: hit resolved from STDERR MARKER on attempt #$attempt")
                    ApplicationManager.getApplication().invokeLater { applyHighlight(markerHit) }
                    return@executeOnPooledThread
                }
                attempt++
                try {
                    Thread.sleep(50)
                } catch (e: InterruptedException) {
                    return@executeOnPooledThread
                }
            }
            logger.warn(
                "sessionPaused: no hit data resolved after 250ms – treating as non-watchpoint pause. " +
                "pendingMarkerEncoded=${pendingMarkerEncoded?.let { "len=${it.length}" } ?: "null"}"
            )
        }
    }

    /**
     * Try to read and consume the per-process last-hit JSON file the runtime
     * writes to `<tempdir>/pycharm_watchpoint_lasthit_<pid>.json`.
     *
     * The runtime uses `tempfile.gettempdir()` which resolves the same way as
     * Java's `java.io.tmpdir` on macOS/Linux/Windows for processes started
     * from the same shell environment – good enough across IDE-launched debug
     * sessions. Atomic rename on the writer side guarantees we never read a
     * half-written document.
     */
    /**
     * Drain the JSON-lines hit file. Returns every hit that's been queued since
     * the last pause and deletes the file – so consecutive mutations coalesced
     * into one pause all render, instead of only the most recent.
     *
     * Empty list (or null) means no watchpoint pause is pending.
     */
    private fun readHitsFromFile(): List<HitInfo> {
        val sessionDir = WatchpointSessionManager.getInstance(project).currentSessionTempDir()
        if (sessionDir.isNullOrEmpty()) {
            logger.warn("readHitsFromFile: no session temp dir registered")
            return emptyList()
        }
        val file = java.io.File(sessionDir, "lasthit.json")
        if (!file.exists()) return emptyList()
        val lines = try {
            file.readLines()
        } catch (e: Exception) {
            logger.warn("readHitsFromFile: read failed at $sessionDir: ${e.message}")
            return emptyList()
        }
        try { file.delete() } catch (_: Exception) {}
        val hits = lines.mapNotNull { line ->
            val trimmed = line.trim()
            if (trimmed.isEmpty()) null else parseHitJson(trimmed)
        }
        if (hits.isNotEmpty()) {
            logger.warn("readHitsFromFile: drained ${hits.size} hit(s) from session dir $sessionDir")
        }
        return hits
    }

    private fun consumeBufferedMarker(): HitInfo? {
        val encoded = pendingMarkerEncoded ?: return null
        pendingMarkerEncoded = null
        if (encoded.isEmpty()) return null
        val hit = decodeHit(encoded)
        if (hit == null) {
            logger.warn("Watchpoint stream marker could not be decoded: $encoded")
        }
        return hit
    }

    private fun parseHitJson(text: String): HitInfo? {
        // Minimal JSON parsing – just five known string/int fields. Using
        // Gson or kotlinx-serialization would bring in a dependency we don't
        // otherwise need; the format is fixed on the writer side so a hand
        // parser is fine.
        return try {
            val map = simpleJsonObject(text) ?: return null
            HitInfo(
                file = map["file"] ?: return null,
                line = map["line"]?.toIntOrNull() ?: return null,
                name = map["name"] ?: return null,
                old = map["old"] ?: "",
                new = map["new"] ?: "",
            )
        } catch (e: Exception) {
            logger.warn("parseHitJson failed: ${e.message}")
            null
        }
    }

    /**
     * Tiny JSON-object parser. Handles the runtime's `json.dump` output for
     * 5 known string/int fields. Not a general JSON parser – it only walks
     * `{"key": "string-or-number-value", ...}` with `\\` escapes inside strings.
     */
    private fun simpleJsonObject(text: String): Map<String, String>? {
        val s = text.trim()
        if (!s.startsWith("{") || !s.endsWith("}")) return null
        val body = s.substring(1, s.length - 1)
        val map = mutableMapOf<String, String>()
        var i = 0
        while (i < body.length) {
            while (i < body.length && body[i].isWhitespace()) i++
            if (i >= body.length) break
            if (body[i] == ',') { i++; continue }
            // key
            if (body[i] != '"') return null
            val keyEnd = readJsonString(body, i) ?: return null
            val key = unescapeJson(body.substring(i + 1, keyEnd))
            i = keyEnd + 1
            while (i < body.length && body[i].isWhitespace()) i++
            if (i >= body.length || body[i] != ':') return null
            i++
            while (i < body.length && body[i].isWhitespace()) i++
            // value: either a JSON string or a number (json.dump emits int for line)
            if (i >= body.length) return null
            val value = if (body[i] == '"') {
                val end = readJsonString(body, i) ?: return null
                val v = unescapeJson(body.substring(i + 1, end))
                i = end + 1
                v
            } else {
                val start = i
                while (i < body.length && body[i] != ',' && !body[i].isWhitespace()) i++
                body.substring(start, i)
            }
            map[key] = value
        }
        return map
    }

    /** Find the closing quote of the JSON string starting at `s[start]`. */
    private fun readJsonString(s: String, start: Int): Int? {
        if (s[start] != '"') return null
        var i = start + 1
        while (i < s.length) {
            when (s[i]) {
                '\\' -> i += 2
                '"' -> return i
                else -> i++
            }
        }
        return null
    }

    private fun unescapeJson(s: String): String {
        if (!s.contains('\\')) return s
        val sb = StringBuilder(s.length)
        var i = 0
        while (i < s.length) {
            val c = s[i]
            if (c != '\\' || i == s.length - 1) {
                sb.append(c)
                i++
                continue
            }
            when (val next = s[i + 1]) {
                '"', '\\', '/' -> sb.append(next)
                'n' -> sb.append('\n')
                't' -> sb.append('\t')
                'r' -> sb.append('\r')
                'b' -> sb.append('\b')
                'f' -> sb.append('\u000C')
                'u' -> {
                    if (i + 5 < s.length) {
                        try {
                            sb.append(Integer.parseInt(s.substring(i + 2, i + 6), 16).toChar())
                            i += 4
                        } catch (e: NumberFormatException) {
                            sb.append(next)
                        }
                    } else {
                        sb.append(next)
                    }
                }
                else -> sb.append(next)
            }
            i += 2
        }
        return sb.toString()
    }

    /**
     * Extract the OS PID of the user process from our debug process handler.
     * Uses reflection so it doesn't bind to a specific ProcessHandler subclass
     * (we've seen SimpleProcessHandler, OSProcessHandler, KillableProcessHandler,
     * and various DAP-specific subclasses across IDE versions).
     */
    private fun extractProcessPid(): Long? {
        val handler = debugProcess.processHandler ?: return null
        return try {
            val method = handler.javaClass.methods.firstOrNull {
                it.name == "getProcess" && it.parameterCount == 0
            }
            if (method == null) {
                logger.warn("extractProcessPid: ${handler.javaClass.simpleName} has no getProcess() method")
                return null
            }
            val rawResult = method.invoke(handler)
            val process = rawResult as? java.lang.Process
            if (process == null) {
                logger.warn("extractProcessPid: getProcess() returned ${rawResult?.javaClass?.name ?: "null"}")
                return null
            }
            process.pid()
        } catch (e: Exception) {
            logger.warn("extractProcessPid: ${e::class.simpleName}: ${e.message}")
            null
        }
    }

    private companion object {
        private const val MARKER_PREFIX = "[WATCHPOINT/event]"
    }

    override fun sessionResumed() {
        clearHighlight()
    }

    override fun sessionStopped() {
        clearHighlight()
    }

    /**
     * Strip the single- or double-quote wrapper pydevd's evaluator places around
     * a returned Python string. Defensive: if there is no wrapper (some pydevd
     * builds skip it for plain ASCII results), the value is returned unchanged.
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
     * Decode the payload produced by watchpoint.py's `_pycharm_consume_last_hit`:
     * base64 of UTF-8 bytes whose decoded form is five NUL-separated fields.
     * Returns null on any structural mismatch (truncated payload, missing
     * fields, non-numeric line, malformed base64), in which case the highlight
     * is silently skipped – we'd rather miss a marker than throw across the
     * debugger boundary.
     */
    private fun decodeHit(encoded: String): HitInfo? {
        return try {
            val rawBytes = Base64.getDecoder().decode(encoded)
            val raw = String(rawBytes, Charsets.UTF_8)
            val parts = raw.split(' ')
            if (parts.size != 5) return null
            HitInfo(
                file = parts[0],
                line = parts[1].toInt(),
                name = parts[2],
                old = parts[3],
                new = parts[4],
            )
        } catch (e: Exception) {
            null
        }
    }

    /**
     * Open the change-site file (without stealing focus from the debugger),
     * then install:
     *  - a line highlight at the change line (animated for ~4.5s, see [startPulse])
     *  - a gutter scrollbar mark + tooltip describing the change
     *  - an inline hint at the end of the line summarising the change
     *
     * Replaces any previously-installed decoration first – we only ever want
     * one watchpoint marker visible per session.
     */
    private fun applyHighlight(hit: HitInfo) {
        // If the session ended while this call was queued on the EDT, skip –
        // otherwise we'd paint a highlight that nobody would ever clear.
        if (disposed) return
        val vFile = LocalFileSystem.getInstance().findFileByPath(hit.file)
        if (vFile == null) {
            logger.warn("Watchpoint hit at ${hit.file}:${hit.line} but file is not visible to the IDE")
            return
        }

        // `focusEditor = false` preserves the debugger panel's focus so the
        // user's Resume / Step keystrokes still go where they expect.
        val descriptor = OpenFileDescriptor(project, vFile, (hit.line - 1).coerceAtLeast(0), 0)
        val editor = FileEditorManager.getInstance(project).openTextEditor(descriptor, false) ?: return

        clearHighlightInternal()

        val lineCount = editor.document.lineCount
        if (lineCount == 0) return
        val lineIndex = (hit.line - 1).coerceIn(0, lineCount - 1)
        val startOffset = editor.document.getLineStartOffset(lineIndex)
        val endOffset = editor.document.getLineEndOffset(lineIndex)

        // Initial settled-colour background – the pulse animation will mutate
        // textAttributes on every tick, finally landing back on this value.
        val baseBg = JBColor(baseLight, baseDark)
        val attrs = TextAttributes().apply { backgroundColor = baseBg }

        val markup = editor.markupModel
        val highlighter = markup.addRangeHighlighter(
            startOffset,
            endOffset,
            HighlighterLayer.SELECTION - 1,
            attrs,
            HighlighterTargetArea.LINES_IN_RANGE,
        )
        // Use explicit setters: the getters take an optional `EditorColorsScheme`
        // parameter, so Kotlin won't expose these as properties.
        highlighter.setErrorStripeMarkColor(baseBg)
        highlighter.setErrorStripeTooltip(
            "Watchpoint '${hit.name}' fired: ${hit.old} -> ${hit.new}"
        )

        val inlay = installInlineHint(editor, endOffset, hit)
        val alarm = startPulse(editor, highlighter)

        currentHighlights.add(HighlightHandle(editor, highlighter, inlay, alarm))
        logger.warn("Highlighted ${hit.file}:${hit.line} for watchpoint '${hit.name}'")
    }

    /**
     * Install an end-of-line inline hint summarising the change. We render via
     * a small custom [EditorCustomElementRenderer] rather than the platform's
     * `HintRenderer` because the latter has moved across packages in recent
     * platform releases – owning the renderer keeps the plugin compatible.
     *
     * Returns null if the inlay model refuses (e.g. document was modified
     * mid-call); we tolerate that – the line highlight by itself is still useful.
     */
    private fun installInlineHint(editor: Editor, endOffset: Int, hit: HitInfo): Inlay<*>? {
        val text = "  <- watchpoint '${hit.name}' fired: ${hit.old} -> ${hit.new}"
        return try {
            editor.inlayModel.addAfterLineEndElement(
                endOffset,
                /* relatesToPrecedingText = */ true,
                WatchpointInlineHintRenderer(editor, text),
            )
        } catch (e: Exception) {
            logger.warn("Could not install watchpoint inline hint: ${e.message}")
            null
        }
    }

    /**
     * Kick off a [pulseTotalMs] "BAM-then-fade" animation on the highlighter's background.
     *
     * Intensity follows `exp(-4t)`: full peak at t=0, fast initial drop, gentle tail.
     * After [pulseTotalMs] the highlighter is left on the static base colour and the
     * alarm stops scheduling itself. The returned [Alarm] lets the caller cancel
     * mid-animation if the user resumes / re-pauses early.
     *
     * The repaint is implicit: setting `textAttributes` on a RangeHighlighter
     * fires a markup-changed event that the editor honours by redrawing the
     * affected range.
     */
    private fun startPulse(editor: Editor, highlighter: RangeHighlighter): Alarm {
        // Project-parented alarm: gets cleaned up automatically if the project
        // closes mid-animation. The no-arg constructor is deprecated in 2025.1.
        val alarm = Alarm(project)
        val startMs = System.currentTimeMillis()
        val tick = object : Runnable {
            override fun run() {
                if (alarm.isDisposed) return
                if (!highlighter.isValid) return
                val elapsed = System.currentTimeMillis() - startMs
                val intensity = pulseIntensity(elapsed)
                // Mutating an active highlighter's appearance needs the `Ex`
                // interface – the base `RangeHighlighter` exposes a getter
                // but not the setter in 2025.1. The cast always succeeds:
                // MarkupModelImpl returns RangeHighlighterImpl which implements
                // RangeHighlighterEx.
                (highlighter as RangeHighlighterEx).setTextAttributes(pulseAttrs(intensity))
                if (elapsed < pulseTotalMs) {
                    alarm.addRequest(this, pulseTickMs)
                }
            }
        }
        tick.run()
        return alarm
    }

    private fun pulseIntensity(elapsedMs: Long): Float {
        if (elapsedMs >= pulseTotalMs) return 0f
        val t = elapsedMs.toDouble() / pulseTotalMs.toDouble()
        // Exponential decay: instant peak at t=0, fast initial drop, gentle tail.
        // exp(-4) ≈ 0.018 at the end of the animation – effectively zero.
        return Math.exp(-4.0 * t).toFloat()
    }

    private fun pulseAttrs(intensity: Float): TextAttributes {
        val light = blendColor(baseLight, peakLight, intensity)
        val dark = blendColor(baseDark, peakDark, intensity)
        return TextAttributes().apply { backgroundColor = JBColor(light, dark) }
    }

    private fun blendColor(base: Color, peak: Color, t: Float): Color {
        val clamped = t.coerceIn(0f, 1f)
        val r = (base.red + (peak.red - base.red) * clamped).toInt().coerceIn(0, 255)
        val g = (base.green + (peak.green - base.green) * clamped).toInt().coerceIn(0, 255)
        val b = (base.blue + (peak.blue - base.blue) * clamped).toInt().coerceIn(0, 255)
        return Color(r, g, b)
    }

    /**
     * Tear the listener down for good: prevent any further highlight from
     * being installed and clear the one currently visible (if any). Called by
     * [WatchpointDebugListener.processStopped] so a session that ends while a
     * pulse is still animating doesn't leave the editor decorated.
     *
     * Safe to call from any thread; the actual editor cleanup is queued onto
     * the EDT with `ModalityState.any()` so it still runs while the IDE is
     * displaying modal teardown dialogs around session shutdown.
     */
    fun dispose() {
        disposed = true
        // Detach the stderr marker listener; the ProcessHandler can outlive us
        // during a hard "Stop debug" sequence, so leaving the listener attached
        // would leak this whole highlighter instance.
        try {
            debugProcess.processHandler?.removeProcessListener(markerListener)
        } catch (e: Exception) {
            logger.warn("Could not remove process marker listener: ${e.message}")
        }
        ApplicationManager.getApplication().invokeLater(
            { clearHighlightInternal() },
            com.intellij.openapi.application.ModalityState.any(),
        )
    }

    private fun clearHighlight() {
        if (currentHighlights.isEmpty()) return
        ApplicationManager.getApplication().invokeLater {
            clearHighlightInternal()
        }
    }

    /**
     * Caller must be on the EDT. Safe to invoke when there is nothing to clear
     * or when the editor has been disposed since the highlight was installed.
     *
     * Cleanup order per handle is line highlighter → inlay → alarm; each step
     * is guarded because any one of them can throw if its host (editor,
     * document, alarm executor) was already disposed by the platform.
     */
    private fun clearHighlightInternal() {
        if (currentHighlights.isEmpty()) return
        val toClear = currentHighlights.toList()
        currentHighlights.clear()
        for (handle in toClear) {
            try {
                handle.pulseAlarm.cancelAllRequests()
                Disposer.dispose(handle.pulseAlarm)
            } catch (e: Exception) {
                // Alarm already disposed – fine.
            }
            try {
                handle.inlay?.dispose()
            } catch (e: Exception) {
                // Inlay already invalid – fine.
            }
            try {
                handle.editor.markupModel.removeHighlighter(handle.highlighter)
            } catch (e: Exception) {
                // Editor / markup model already disposed – nothing left to clean up.
            }
        }
    }
}

/**
 * Renders the inline hint that follows a hit line, summarising what changed.
 *
 * Drawn in the editor's italic font with a slightly faded gray foreground so
 * the hint reads as metadata rather than competing with the line's syntax
 * highlighting. We own this renderer instead of using `HintRenderer` because
 * `HintRenderer`'s package has shifted across recent platform releases –
 * keeping the rendering local means the plugin survives those moves.
 */
private class WatchpointInlineHintRenderer(
    private val editor: Editor,
    private val text: String,
) : EditorCustomElementRenderer {

    private fun font(): Font = editor.colorsScheme.getFont(EditorFontType.ITALIC)

    override fun calcWidthInPixels(inlay: Inlay<*>): Int {
        val fm = editor.contentComponent.getFontMetrics(font())
        return fm.stringWidth(text) + JBUI.scale(4)
    }

    override fun paint(
        inlay: Inlay<*>,
        g: Graphics,
        targetRegion: Rectangle,
        textAttributes: TextAttributes,
    ) {
        g.font = font()
        // Theme-aware faded gray so it works on both light and dark backgrounds
        // without clashing with the (also yellow) line highlight underneath.
        g.color = JBColor(Color(120, 120, 120), Color(155, 155, 145))
        val fm = g.fontMetrics
        val y = targetRegion.y + (targetRegion.height - fm.height) / 2 + fm.ascent
        g.drawString(text, targetRegion.x + JBUI.scale(2), y)
    }
}
