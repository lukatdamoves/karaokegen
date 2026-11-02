package com.lukatdamoves.karaokegen

import org.json.JSONArray
import org.json.JSONObject

/**
 * Native editor data model: the alignment for one draft.
 *
 * A draft's alignment is a list of LINES (one lyric line = one
 * Segment), each line a list of WORDS in time order. Every word has
 * its text and its start/end in seconds. This is the same shape as
 * the backend's AlignmentResult (KaraokeGen/models.py: Word,
 * Segment, AlignmentResult) and the desktop editor's `alignment`
 * object — the native editor just holds it in Kotlin instead of JS.
 *
 * Segment start/end/text are DERIVED from the words when the render
 * JSON is built (first word's start, last word's end, words joined
 * by a space), so an edited word always stays in sync with its line.
 */
data class EditorWord(
    var text: String,
    var start: Double,
    var end: Double
)

data class EditorLine(
    val words: MutableList<EditorWord>
)

/** One flat word reference (line + word index) — used by the
 *  waveform view and the word list so both point at the same word. */
data class WordRef(val line: Int, val word: Int)

class EditorAlignment(
    val lines: MutableList<EditorLine>,
    val duration: Double
) {
    /** Deep copy, for the undo stack. */
    fun deepCopy(): EditorAlignment {
        val copied = lines.map { line ->
            EditorLine(line.words.map { EditorWord(it.text, it.start, it.end) }.toMutableList())
        }.toMutableList()
        return EditorAlignment(copied, duration)
    }

    /** All words in time order, with their line/word indices. Lines
     *  are already in time order and words inside a line are too, so
     *  a flat walk is time order. */
    fun flatWords(): List<Triple<Int, Int, EditorWord>> {
        val out = ArrayList<Triple<Int, Int, EditorWord>>()
        for ((li, line) in lines.withIndex()) {
            for ((wi, word) in line.words.withIndex()) {
                out.add(Triple(li, wi, word))
            }
        }
        return out
    }

    fun wordAt(ref: WordRef): EditorWord? =
        lines.getOrNull(ref.line)?.words?.getOrNull(ref.word)

    /** The lyric text as the backend expects it: one line per row. */
    fun finalLyrics(): String =
        lines.joinToString("\n") { line ->
            line.words.joinToString(" ") { it.text }
        }

    /**
     * The alignment JSON for POST /render-word, matching the
     * backend's RenderRequest.alignment (AlignmentResult). Blank
     * words (an "Add line" the user never filled in) and lines left
     * with no words are dropped, so they can't break the render.
     * Segment start/end/text are derived from the words.
     */
    fun toAlignmentJson(): JSONObject {
        val segments = JSONArray()
        for (line in lines) {
            val words = line.words.filter { it.text.isNotBlank() }
            if (words.isEmpty()) continue
            val wordsJson = JSONArray()
            for (w in words) {
                wordsJson.put(JSONObject().apply {
                    put("text", w.text)
                    put("start", w.start)
                    put("end", w.end)
                })
            }
            val text = words.joinToString(" ") { it.text }
            segments.put(JSONObject().apply {
                put("words", wordsJson)
                put("start", words.first().start)
                put("end", words.last().end)
                put("text", text)
            })
        }
        return JSONObject().apply {
            put("segments", segments)
        }
    }

    companion object {
        /**
         * Parse a DraftResult JSONObject (GET /draft-result/{id}).
         * Returns null when there is no usable alignment (no
         * segments, or every segment is empty).
         */
        fun fromDraftResult(result: JSONObject?): EditorAlignment? {
            if (result == null) return null
            val alignment = result.optJSONObject("alignment") ?: return null
            val segments = alignment.optJSONArray("segments") ?: return null
            val duration = result.optDouble("duration", 0.0)
            val lines = ArrayList<EditorLine>()
            for (i in 0 until segments.length()) {
                val seg = segments.optJSONObject(i) ?: continue
                val wordsJson = seg.optJSONArray("words") ?: continue
                val words = ArrayList<EditorWord>()
                for (j in 0 until wordsJson.length()) {
                    val w = wordsJson.optJSONObject(j) ?: continue
                    val text = w.optString("text")
                    val start = w.optDouble("start", -1.0)
                    val end = w.optDouble("end", -1.0)
                    if (start < 0 || end < 0 || end <= start) continue
                    words.add(EditorWord(text, start, end))
                }
                if (words.isEmpty()) continue
                // Keep words in time order inside the line.
                words.sortBy { it.start }
                lines.add(EditorLine(words))
            }
            if (lines.isEmpty()) return null
            lines.sortBy { it.words.firstOrNull()?.start ?: 0.0 }
            // Duration: trust the backend value; if it is missing,
            // use the last word's end so the waveform still spans it.
            val dur = if (duration > 0) duration else
                (lines.lastOrNull()?.words?.lastOrNull()?.end ?: 0.0)
            return EditorAlignment(lines, dur)
        }
    }
}
