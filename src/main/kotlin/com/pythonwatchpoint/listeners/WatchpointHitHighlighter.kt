package com.pythonwatchpoint.listeners

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.diagnostic.Logger
import com.intellij.openapi.editor.Editor
import com.intellij.openapi.editor.EditorCustomElementRenderer
import com.intellij.openapi.editor.Inlay
import com.intellij.openapi.editor.colors.EditorFontType
import com.intellij.openapi.editor.ex.RangeHighlighterEx
import com.intellij.openapi.editor.markup.HighlighterLayer
import com.intellij.openapi.editor.markup.HighlighterTargetArea
import com.intellij.openapi.editor.markup.RangeHighlighter
import com.intellij.openapi.editor.markup.TextAttributes
import com.intellij.openapi.editor.ScrollType
import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.openapi.fileEditor.TextEditor
import com.intellij.openapi.project.Project
import com.intellij.openapi.util.Disposer
import com.intellij.openapi.vfs.LocalFileSystem
import com.intellij.ui.JBColor
import com.intellij.util.Alarm
import com.intellij.util.ui.JBUI
import com.intellij.xdebugger.XDebugSession
import com.intellij.xdebugger.XDebugSessionListener
import com.jetbrains.python.debugger.PyDebugProcess
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
) : XDebugSessionListener {
    private val logger = Logger.getInstance(WatchpointHitHighlighter::class.java)

    // A single pause may need to render multiple highlights: the runtime
    // queues every hit and the IDE drains the whole queue per pause, so
    // mutations that fire in fast-returning sibling functions all show up
    // even though pydevd's `CMD_STEP_OVER` mechanism coalesces them into one
    // pause UI event. All entries are cleared together on the next
    // sessionResumed / sessionStopped.
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
        val pulseAlarm: Alarm?,
    )

    private data class HitInfo(
        val file: String,
        val line: Int,
        val name: String,
        val old: String,
        val new: String,
        val callerFile: String,
        val callerLine: Int,
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

    // Secondary highlight (call-site/pause line): very pale amber, static –
    // visually distinct from the primary so the user reads it as "breadcrumb"
    // rather than "mutation site."
    private val secondaryLight = Color(255, 235, 180)
    private val secondaryDark = Color(70, 60, 20)

    override fun sessionPaused() {
        if (disposed) return
        val debugProcess = session.debugProcess as? PyDebugProcess ?: return

        // Snapshot the current pause location on the EDT before hopping off
        // for the synchronous evaluator call. The Python side filters its
        // hit queue by `(pause_file, pause_line)` so each pause returns
        // only the hit whose bp fired here – sibling hits whose bps are
        // armed at OTHER lines stay queued for their own future pauses.
        //
        // If the position isn't available (rare race during session
        // teardown or before the first stack frame is resolved), fall
        // back to the legacy no-arg call which drains everything. The
        // worst case is the original "two yellow lines at once" symptom
        // for a single pause, which is still better than no highlight.
        val sourcePosition = session.currentStackFrame?.sourcePosition
        val pauseFile = sourcePosition?.file?.path
        val pauseLine = sourcePosition?.line?.plus(1)  // line is 0-based; Python is 1-based

        // Why not the generic XDebuggerEvaluator: that path returns a PyDebugValue
        // whose `value` field is the variables-panel DISPLAY string, truncated to
        // PyDebugValue.MAX_VALUE (256 chars). Our base64 payload routinely exceeds
        // that – even the file path alone can be >150 base64 chars on macOS test
        // trees – so the payload arrived chopped mid-field and decode silently
        // failed. PyDebugProcess.evaluate(expr, execute=false, doTrunc=false)
        // bypasses the truncation and returns the full string.
        //
        // The call is synchronous and goes through pydevd's protocol, so we run
        // it on a pooled thread; the UI work hops back to the EDT via invokeLater.
        ApplicationManager.getApplication().executeOnPooledThread {
            if (disposed) return@executeOnPooledThread
            val expr = if (pauseFile != null && pauseLine != null) {
                // Escape backslashes + single quotes so the file path
                // survives as a Python string literal.
                val escaped = pauseFile.replace("\\", "\\\\").replace("'", "\\'")
                "_pycharm_consume_last_hit('$escaped', $pauseLine)"
            } else {
                "_pycharm_consume_last_hit()"
            }
            val pyValue = try {
                debugProcess.evaluate(expr, /* execute = */ false, /* doTrunc = */ false)
            } catch (e: Exception) {
                // Expected when this isn't a watchpoint session – the builtin
                // simply isn't defined. Don't log to avoid spamming idea.log.
                return@executeOnPooledThread
            }
            val raw = pyValue?.value ?: return@executeOnPooledThread
            val payload = stripOuterQuotes(raw)
            if (payload.isEmpty()) {
                // Pause wasn't caused by a watchpoint – plain breakpoint, step, etc.
                return@executeOnPooledThread
            }
            // Payload is one or more base64-encoded hit entries separated by `;`
            // (see watchpoint.py `_pycharm_consume_last_hit`). Multiple entries
            // occur when several mutations fired between consecutive pauses –
            // each one becomes its own highlighted line.
            val hits = payload.split(';').mapNotNull { entry ->
                val trimmed = entry.trim()
                if (trimmed.isEmpty()) null else decodeHit(trimmed)
            }
            if (hits.isEmpty()) {
                logger.warn("Watchpoint hit payload could not be decoded: $raw")
                return@executeOnPooledThread
            }
            if (hits.size > 1) {
                logger.warn("sessionPaused: drained ${hits.size} queued watchpoint hits")
            }
            // Two-phase approach:
            // Phase 1 (immediate): apply the highlight decoration so the
            //   user sees the coloured line as soon as possible. If the
            //   file is already open we grab its editor silently; if not
            //   we open it (tab appears but IDE's own focus may override).
            // Phase 2 (after 150ms): re-select the tab and scroll to the
            //   line. The delay lets the IDE's post-pause focus settle
            //   first so our tab selection wins and sticks.
            ApplicationManager.getApplication().invokeLater {
                if (disposed) return@invokeLater
                hits.forEach { applyHighlight(it, jumpToLine = false) }

                // Secondary highlight on the call-site line – a subtle
                // breadcrumb so the user sees "the watchpoint fired inside this
                // call" when they navigate back to the execution file. Uses the
                // caller frame info captured by the Python runtime at hit time
                // (the exact line that called into the code that mutated).
                hits.forEach { hit ->
                    if (hit.callerFile.isNotEmpty() && hit.callerLine > 0) {
                        val sameLine = (hit.callerFile == hit.file && hit.callerLine == hit.line)
                        if (!sameLine) {
                            applySecondaryHighlight(hit)
                        }
                    }
                }

                // Phase 2: delayed jump ensures the tab sticks.
                val jumpAlarm = Alarm(project)
                jumpAlarm.addRequest({
                    if (disposed) return@addRequest
                    hits.forEach { jumpToHitLine(it) }
                }, 150)
            }
        }
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
     * base64 of UTF-8 bytes whose decoded form is seven NUL-separated fields
     * (file, line, name, old, new, caller_file, caller_line). Backward-compat
     * with the legacy 5-field format (caller fields default to empty/0).
     * Returns null on any structural mismatch (truncated payload, missing
     * fields, non-numeric line, malformed base64), in which case the highlight
     * is silently skipped – we'd rather miss a marker than throw across the
     * debugger boundary.
     */
    private fun decodeHit(encoded: String): HitInfo? {
        return try {
            val rawBytes = Base64.getDecoder().decode(encoded)
            val raw = String(rawBytes, Charsets.UTF_8)
            val parts = raw.split('\u0000')
            if (parts.size < 5) return null
            HitInfo(
                file = parts[0],
                line = parts[1].toInt(),
                name = parts[2],
                old = parts[3],
                new = parts[4],
                callerFile = if (parts.size > 5) parts[5] else "",
                callerLine = if (parts.size > 6) parts[6].toIntOrNull() ?: 0 else 0,
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
     * Appends to the existing highlight set – multiple hits coalesced into one
     * pause each get their own decoration, all cleared together on resume.
     *
     * @param jumpToLine if true, also selects the tab and scrolls to the line.
     *   When false, only the decoration is applied (the file must already be open
     *   or will be opened silently) – used for the immediate phase so the user
     *   sees the highlight without waiting for the IDE's focus fight to settle.
     */
    private fun applyHighlight(hit: HitInfo, jumpToLine: Boolean = true) {
        // If the session ended while this call was queued on the EDT, skip –
        // otherwise we'd paint a highlight that nobody would ever clear.
        if (disposed) return
        val vFile = LocalFileSystem.getInstance().findFileByPath(hit.file)
        if (vFile == null) {
            logger.warn("Watchpoint hit at ${hit.file}:${hit.line} but file is not visible to the IDE")
            return
        }

        val fileEditorManager = FileEditorManager.getInstance(project)

        // Try to get an editor without changing tab selection first. If the
        // file isn't open yet, open it (which selects the tab – acceptable
        // since the delayed jump will re-select anyway).
        val editor = fileEditorManager.getEditors(vFile)
            .filterIsInstance<TextEditor>().firstOrNull()?.editor
            ?: fileEditorManager.openFile(vFile, /* focusEditor = */ false)
                .filterIsInstance<TextEditor>().firstOrNull()?.editor
            ?: return

        val lineCount = editor.document.lineCount
        if (lineCount == 0) return
        val lineIndex = hit.line - 1
        // If the file was edited while the debug session is active, the hit line
        // may exceed the current document length. Skip rather than clamping – a
        // clamped highlight would decorate an unrelated line, which is confusing.
        if (lineIndex < 0 || lineIndex >= lineCount) {
            logger.warn("Watchpoint hit at ${hit.file}:${hit.line} is out of range (file has $lineCount lines) – skipping highlight")
            return
        }

        if (jumpToLine) {
            // Select the tab and scroll to the hit line.
            fileEditorManager.openFile(vFile, /* focusEditor = */ false)
            val scrollOffset = editor.document.getLineStartOffset(lineIndex)
            editor.scrollingModel.scrollTo(
                editor.offsetToLogicalPosition(scrollOffset),
                ScrollType.CENTER,
            )
        }

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
     * Install a subtle static highlight on the call-site line – the line in the
     * caller's code that invoked the function where the mutation happened. The
     * Python runtime captures caller_file/caller_line from `f_lineno` of the
     * nearest user-code caller frame at hit time, so this points at the exact
     * call expression rather than guessing offsets from the bp fire location.
     *
     * Visually quieter than the primary: pale amber background, no pulse, no
     * gutter mark, shorter inline hint. Serves as a breadcrumb linking the
     * pause location to the watchpoint event.
     */
    private fun applySecondaryHighlight(hit: HitInfo) {
        if (disposed) return
        val vFile = LocalFileSystem.getInstance().findFileByPath(hit.callerFile) ?: return

        val fileEditorManager = FileEditorManager.getInstance(project)
        // The caller file is very likely already open (the IDE focused the
        // pause file post-pause, which is typically at or near the caller).
        val editor = fileEditorManager.getEditors(vFile)
            .filterIsInstance<TextEditor>().firstOrNull()?.editor
            ?: fileEditorManager.openFile(vFile, /* focusEditor = */ false)
                .filterIsInstance<TextEditor>().firstOrNull()?.editor
            ?: return

        val lineCount = editor.document.lineCount
        if (lineCount == 0) return
        // callerLine is 1-based (Python f_lineno); convert to 0-based editor index.
        val lineIndex = (hit.callerLine - 1).coerceIn(0, lineCount - 1)

        val startOffset = editor.document.getLineStartOffset(lineIndex)
        val endOffset = editor.document.getLineEndOffset(lineIndex)

        val bg = JBColor(secondaryLight, secondaryDark)
        val attrs = TextAttributes().apply { backgroundColor = bg }

        val highlighter = editor.markupModel.addRangeHighlighter(
            startOffset,
            endOffset,
            HighlighterLayer.SELECTION - 2,  // below primary highlight layer
            attrs,
            HighlighterTargetArea.LINES_IN_RANGE,
        )

        // Shorter inline hint – signals the call-site relationship without
        // repeating the full old→new detail (that's on the primary line).
        val hintText = "  <- watchpoint '${hit.name}' changed inside this call"
        val inlay = try {
            editor.inlayModel.addAfterLineEndElement(
                endOffset,
                /* relatesToPrecedingText = */ true,
                WatchpointInlineHintRenderer(editor, hintText),
            )
        } catch (e: Exception) {
            null
        }

        currentHighlights.add(HighlightHandle(editor, highlighter, inlay, /* pulseAlarm = */ null))
        logger.warn("Secondary highlight at ${hit.callerFile}:${hit.callerLine} for watchpoint '${hit.name}'")
    }

    /**
     * Phase 2 of the two-phase highlight: select the mutation file's tab and
     * scroll to the hit line. Called after a short delay so the IDE's own
     * post-pause focus has already settled and our tab selection sticks.
     */
    private fun jumpToHitLine(hit: HitInfo) {
        if (disposed) return
        val vFile = LocalFileSystem.getInstance().findFileByPath(hit.file) ?: return
        val fileEditorManager = FileEditorManager.getInstance(project)
        // openFile with focusEditor=false: selects the tab (makes file
        // visible) without giving the editor keyboard focus – debug tool
        // window keeps focus, no blinking caret.
        val editor = fileEditorManager.openFile(vFile, /* focusEditor = */ false)
            .filterIsInstance<TextEditor>().firstOrNull()?.editor ?: return

        val lineCount = editor.document.lineCount
        if (lineCount == 0) return
        val lineIndex = hit.line - 1
        if (lineIndex < 0 || lineIndex >= lineCount) return
        val scrollOffset = editor.document.getLineStartOffset(lineIndex)
        editor.scrollingModel.scrollTo(
            editor.offsetToLogicalPosition(scrollOffset),
            ScrollType.CENTER,
        )
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
     * Iterates every handle that's been installed since the last clear (a
     * pause that drained multiple queued hits installs one per hit), and for
     * each one cleans up in the order line highlighter → inlay → alarm. Each
     * step is guarded because any one of them can throw if its host (editor,
     * document, alarm executor) was already disposed by the platform.
     */
    private fun clearHighlightInternal() {
        if (currentHighlights.isEmpty()) return
        val toClear = currentHighlights.toList()
        currentHighlights.clear()
        for (handle in toClear) {
            try {
                handle.pulseAlarm?.cancelAllRequests()
                handle.pulseAlarm?.let { Disposer.dispose(it) }
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
