package com.lukatdamoves.karaokegen

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RectF
import android.util.AttributeSet
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

/**
 * Native waveform + word-region editor view.
 *
 * Draws the vocals waveform (per-bucket peaks) on a canvas, with
 * one region block per word over it, and a playhead line. The user
 * can:
 *  - TAP a word region to select it (callback onWordTap).
 *  - DRAG a region's BODY to move the word (start+end shift
 *    together), clamped so it cannot pass its neighbours.
 *  - DRAG a region's LEFT/RIGHT EDGE to resize it, clamped to its
 *    neighbours and to a 0.05s minimum duration.
 *  - TAP empty waveform to seek the playhead there (onSeekTo).
 *
 * Only the visible time window is drawn ([windowStart,
 * windowStart + windowDuration]); zooming changes windowDuration
 * and the parent follows the playhead by moving windowStart.
 * When there are no peaks (audio could not be decoded) the view
 * still draws the time ruler + word regions, so the editor stays
 * usable.
 */
class WaveformView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null
) : View(context, attrs) {

    /** One word as the view sees it (line/word indices into the model). */
    data class WaveWord(
        val line: Int,
        val word: Int,
        val text: String,
        var start: Double,
        var end: Double
    )

    var peaks: FloatArray? = null
        private set
    var duration: Double = 0.0
        private set
    var words: List<WaveWord> = emptyList()
        private set
    var windowStart: Double = 0.0
        private set
    var windowDuration: Double = 10.0
        private set
    var playheadSec: Double = -1.0
    var selected: WordRef? = null
    var currentWord: WordRef? = null

    /** Called when a word is tapped (select it). */
    var onWordTap: ((WordRef) -> Unit)? = null
    /** Called when the user taps empty waveform (seek there). */
    var onSeekTo: ((Double) -> Unit)? = null
    /** Called once when a drag starts (the parent pushes an undo snapshot). */
    var onWordDragStart: (() -> Unit)? = null
    /** Called live during a drag with the word's new start/end. */
    var onWordChanged: ((WordRef, Double, Double) -> Unit)? = null
    /** Called when a drag finishes. */
    var onWordDragEnd: (() -> Unit)? = null

    private val density = resources.displayMetrics.density
    private val touchSlop = ViewConfiguration.get(context).scaledTouchSlop

    private val bgPaint = Paint().apply { color = Color.parseColor("#0F1626") }
    private val rulerPaint = Paint().apply {
        color = Color.parseColor("#8A9AB8")
        textSize = 11f * density
        isAntiAlias = true
    }
    private val tickPaint = Paint().apply {
        color = Color.parseColor("#3A3A5A")
        strokeWidth = 1f * density
    }
    private val wavePaint = Paint().apply {
        color = Color.parseColor("#4A6CF7")
        alpha = 200
        strokeWidth = 1f
        isAntiAlias = true
    }
    private val regionFill = Paint().apply {
        color = Color.parseColor("#4A6CF7")
        alpha = 90
        style = Paint.Style.FILL
        isAntiAlias = true
    }
    private val regionFillCurrent = Paint().apply {
        color = Color.parseColor("#4A6CF7")
        alpha = 170
        style = Paint.Style.FILL
        isAntiAlias = true
    }
    private val regionFillSelected = Paint().apply {
        color = Color.parseColor("#4A6CF7")
        alpha = 235
        style = Paint.Style.FILL
        isAntiAlias = true
    }
    private val regionBorder = Paint().apply {
        color = Color.parseColor("#7A96FF")
        style = Paint.Style.STROKE
        strokeWidth = 1.5f * density
        isAntiAlias = true
    }
    private val regionText = Paint().apply {
        color = Color.WHITE
        textSize = 12f * density
        isAntiAlias = true
        textAlign = Paint.Align.CENTER
    }
    private val playheadPaint = Paint().apply {
        color = Color.WHITE
        strokeWidth = 2f * density
        isAntiAlias = true
    }
    private val hintPaint = Paint().apply {
        color = Color.parseColor("#8A9AB8")
        textSize = 13f * density
        isAntiAlias = true
        textAlign = Paint.Align.CENTER
    }

    private val regionRect = RectF()

    fun setData(peaks: FloatArray?, duration: Double, words: List<WaveWord>) {
        this.peaks = peaks
        this.duration = duration
        this.words = words
        if (windowDuration > duration && duration > 0) {
            windowDuration = duration
        }
        if (windowStart + windowDuration > duration && duration > 0) {
            windowStart = max(0.0, duration - windowDuration)
        }
        invalidate()
    }

    /** Show [start, start+duration] seconds across the width. */
    fun setWindow(start: Double, duration: Double) {
        val dur = duration.coerceIn(1.0, max(1.0, this.duration))
        windowStart = start.coerceIn(0.0, max(0.0, this.duration - dur))
        windowDuration = dur
        invalidate()
    }

    /** Scroll the window just enough to keep `time` visible. */
    fun ensureVisible(time: Double) {
        if (time < windowStart) {
            setWindow(time, windowDuration)
        } else if (time > windowStart + windowDuration) {
            setWindow(time - windowDuration * 0.9, windowDuration)
        }
    }

    fun timeToX(t: Double): Float =
        ((t - windowStart) / windowDuration * width).toFloat()

    fun xToTime(x: Float): Double =
        windowStart + x / width * windowDuration

    // ------------------------------------------------------------------
    // Drawing
    // ------------------------------------------------------------------

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val w = width.toFloat()
        val h = height.toFloat()
        if (w <= 0 || h <= 0) return
        canvas.drawRect(0f, 0f, w, h, bgPaint)

        val rulerH = 22f * density
        val waveTop = rulerH + 4f * density
        val waveBottom = h - 4f * density
        val waveMid = (waveTop + waveBottom) / 2f
        val waveHalf = (waveBottom - waveTop) / 2f

        // Time ruler: ticks + labels, spaced so they don't crowd.
        val step = when {
            windowDuration <= 30 -> 1.0
            windowDuration <= 120 -> 5.0
            else -> 10.0
        }
        var t = (windowStart / step).toInt() * step
        if (t < windowStart) t += step
        while (t <= windowStart + windowDuration) {
            val x = timeToX(t)
            canvas.drawLine(x, rulerH - 8f * density, x, rulerH, tickPaint)
            canvas.drawText(formatRuler(t), x + 3f * density, rulerH - 10f * density, rulerPaint)
            t += step
        }
        // Ruler baseline.
        canvas.drawLine(0f, rulerH, w, rulerH, tickPaint)

        // Waveform bars (or a hint when there are no peaks).
        val pk = peaks
        if (pk != null && pk.isNotEmpty() && duration > 0) {
            var x = 0f
            while (x < w) {
                val time = xToTime(x)
                val bucket = ((time / duration) * pk.size).toInt()
                    .coerceIn(0, pk.size - 1)
                val amp = pk[bucket].coerceIn(0f, 1f) * waveHalf
                canvas.drawLine(x, waveMid - amp, x, waveMid + amp, wavePaint)
                x += 2f
            }
        } else {
            canvas.drawText(
                "No waveform — timings still editable",
                w / 2f, waveMid, hintPaint
            )
        }

        // Word regions (only the ones in the visible window).
        for (word in words) {
            if (word.end < windowStart || word.start > windowStart + windowDuration) continue
            val x0 = timeToX(word.start).coerceIn(0f, w)
            val x1 = timeToX(word.end).coerceIn(0f, w)
            if (x1 - x0 < 2f) continue
            val ref = WordRef(word.line, word.word)
            val fill = when (ref) {
                selected -> regionFillSelected
                currentWord -> regionFillCurrent
                else -> regionFill
            }
            regionRect.set(x0, waveTop, x1, waveBottom)
            val radius = 6f * density
            canvas.drawRoundRect(regionRect, radius, radius, fill)
            canvas.drawRoundRect(regionRect, radius, radius, regionBorder)
            // Word text, when the region is wide enough to read.
            if (x1 - x0 > 30f * density && word.text.isNotEmpty()) {
                val text = regionText
                val maxW = (x1 - x0) - 6f * density
                var label = word.text
                while (text.measureText(label) > maxW && label.length > 1) {
                    label = label.dropLast(1)
                }
                canvas.drawText(
                    label, (x0 + x1) / 2f,
                    waveMid + regionText.textSize / 3f, regionText
                )
            }
        }

        // Playhead.
        if (playheadSec >= 0 && playheadSec >= windowStart &&
            playheadSec <= windowStart + windowDuration
        ) {
            val x = timeToX(playheadSec)
            canvas.drawLine(x, rulerH, x, h, playheadPaint)
        }
    }

    private fun formatRuler(seconds: Double): String {
        val total = seconds.toLong()
        val m = total / 60
        val s = total % 60
        return "%d:%02d".format(m, s)
    }

    // ------------------------------------------------------------------
    // Touch: tap to select/seek, drag body to move, drag edges to resize.
    // ------------------------------------------------------------------

    private enum class DragMode { NONE, BODY, LEFT, RIGHT }

    private var dragMode = DragMode.NONE
    private var dragWord: WaveWord? = null
    private var dragOrigStart = 0.0
    private var dragOrigEnd = 0.0
    private var dragDownX = 0f
    private var dragDownTime = 0.0
    private var dragLowBound = 0.0
    private var dragHighBound = 0.0
    private var dragMoved = false
    private val edgeSlopPx = 14f * density

    override fun onTouchEvent(event: MotionEvent): Boolean {
        val x = event.x
        val time = xToTime(x)
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                dragMoved = false
                dragDownX = x
                dragDownTime = time
                val hit = hitWord(time, x)
                if (hit != null) {
                    dragWord = hit.first
                    dragMode = hit.second
                    dragOrigStart = hit.first.start
                    dragOrigEnd = hit.first.end
                    // Neighbours in flat time order clamp the drag.
                    val flat = words.sortedBy { it.start }
                    val idx = flat.indexOf(hit.first)
                    dragLowBound = if (idx > 0) flat[idx - 1].end else 0.0
                    dragHighBound = if (idx < flat.size - 1) flat[idx + 1].start else duration
                    parent.requestDisallowInterceptTouchEvent(true)
                    // Note: onWordDragStart (undo snapshot) is NOT
                    // called here — only once the finger actually
                    // moves (see ACTION_MOVE), so a plain tap does
                    // not push a useless undo entry.
                } else {
                    dragMode = DragMode.NONE
                    dragWord = null
                }
                return true
            }
            MotionEvent.ACTION_MOVE -> {
                if (dragWord == null) return true
                if (!dragMoved && abs(x - dragDownX) > touchSlop) {
                    // A real drag just began — snapshot for undo now.
                    dragMoved = true
                    onWordDragStart?.invoke()
                }
                if (!dragMoved) return true
                val word = dragWord ?: return true
                val dt = (x - dragDownX) / width * windowDuration
                when (dragMode) {
                    DragMode.BODY -> {
                        // Shift both ends; clamp the shift so the word
                        // stays between its neighbours (and in the song).
                        val shift = dt.coerceIn(
                            dragLowBound - dragOrigStart,
                            dragHighBound - dragOrigEnd
                        )
                        word.start = dragOrigStart + shift
                        word.end = dragOrigEnd + shift
                    }
                    DragMode.LEFT -> {
                        word.start = (dragOrigStart + dt).coerceIn(
                            dragLowBound, dragOrigEnd - 0.05
                        )
                    }
                    DragMode.RIGHT -> {
                        word.end = (dragOrigEnd + dt).coerceIn(
                            dragOrigStart + 0.05, dragHighBound
                        )
                    }
                    DragMode.NONE -> { /* no-op */ }
                }
                onWordChanged?.invoke(
                    WordRef(word.line, word.word), word.start, word.end
                )
                invalidate()
                return true
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                val wasDrag = dragMoved
                val word = dragWord
                val mode = dragMode
                val upTime = xToTime(event.x)
                dragWord = null
                dragMode = DragMode.NONE
                parent.requestDisallowInterceptTouchEvent(false)
                if (event.actionMasked == MotionEvent.ACTION_UP) {
                    if (wasDrag && word != null && mode != DragMode.NONE) {
                        onWordDragEnd?.invoke()
                    } else if (word != null) {
                        onWordTap?.invoke(WordRef(word.line, word.word))
                    } else {
                        // Tapped empty waveform: seek there.
                        onSeekTo?.invoke(upTime.coerceIn(0.0, max(0.0, duration)))
                    }
                }
                invalidate()
                return true
            }
        }
        return super.onTouchEvent(event)
    }

    /** Hit-test: returns the word under `time` and which part (edge/body). */
    private fun hitWord(time: Double, x: Float): Pair<WaveWord, DragMode>? {
        for (word in words) {
            if (time < word.start || time > word.end) continue
            val x0 = timeToX(word.start)
            val x1 = timeToX(word.end)
            return when {
                abs(x - x0) <= edgeSlopPx -> Pair(word, DragMode.LEFT)
                abs(x - x1) <= edgeSlopPx -> Pair(word, DragMode.RIGHT)
                else -> Pair(word, DragMode.BODY)
            }
        }
        return null
    }
}
