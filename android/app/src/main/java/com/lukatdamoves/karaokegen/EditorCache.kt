package com.lukatdamoves.karaokegen

import android.content.Context
import java.io.File
import java.io.InputStream

/**
 * On-disk cache for the native editor, keyed by draft id (the audio
 * hash — one song = one id).
 *
 * Going back to a job used to re-download everything, every time:
 * GET /draft-result (the full DraftResult JSON, including a ~2.4MB
 * base64 vocals MP3 the editor never uses — only the alignment and
 * duration are read) and then GET /vocals-raw (the whole vocals WAV,
 * ~20-39MB for a 3-4 minute song), one after the other. On weak
 * WiFi that is tens of seconds before the editor appears.
 *
 * Now the first load saves both to the app's private files (which
 * survive the app being closed and reopened), and a re-open reads
 * them from disk instead — no network at all on a cache hit.
 *
 * - draft-<id>.json: a MINIMAL json, just {"alignment": ..., "duration": ...}
 *   (the alignment shape EditorAlignment.fromDraftResult already parses).
 *   The multi-MB base64 audio blob is deliberately NOT cached.
 * - vocals-<id>.wav: the vocals WAV bytes, for the waveform + playback.
 *
 * Writes go to a .tmp file and are renamed into place, so a file in
 * the cache is always complete — a download killed halfway never
 * counts as cached. Vocals WAVs are capped at 5 (newest by
 * modification time); draft JSONs are tiny (~30KB) and capped at 20.
 *
 * NOTE: the draft id is the AUDIO hash, so re-running the same song
 * with different lyrics shares the id but produces a different
 * alignment. Every fresh load overwrites the cached draft, so a
 * Jobs tap always gets the latest alignment for that song.
 */
object EditorCache {
    private const val DIR = "editor-cache"
    private const val MAX_VOCALS = 5
    private const val MAX_DRAFTS = 20

    private fun dir(context: Context): File =
        File(context.filesDir, DIR).apply { if (!exists()) mkdirs() }

    fun draftFile(context: Context, draftId: String): File =
        File(dir(context), "draft-$draftId.json")

    fun vocalsFile(context: Context, draftId: String): File =
        File(dir(context), "vocals-$draftId.wav")

    /** The cached alignment for this draft, or null on a miss (or a
     *  corrupt entry, which is deleted so the next load retries). */
    fun readDraft(context: Context, draftId: String): EditorAlignment? {
        val file = draftFile(context, draftId)
        if (!file.exists() || file.length() == 0L) return null
        return try {
            val json = org.json.JSONObject(file.readText())
            EditorAlignment.fromDraftResult(json)
        } catch (_: Exception) {
            runCatching { file.delete() }
            null
        }
    }

    /** Save an alignment (the minimal json — no base64 audio blob). */
    fun saveDraft(context: Context, draftId: String, alignment: EditorAlignment) {
        val file = draftFile(context, draftId)
        try {
            val json = org.json.JSONObject().apply {
                put("alignment", alignment.toAlignmentJson())
                put("duration", alignment.duration)
            }
            val tmp = File(file.parent, file.name + ".tmp")
            tmp.writeText(json.toString())
            tmp.renameTo(file)
            pruneDrafts(context)
        } catch (e: Exception) {
            AppLog.e("EditorCache", "Could not save draft $draftId: ${e.message}")
        }
    }

    /** A complete cached vocals WAV, or null. */
    fun readVocals(context: Context, draftId: String): File? {
        val file = vocalsFile(context, draftId)
        return if (file.exists() && file.length() > 0) file else null
    }

    /** Delete a draft's cached files (draft JSON + vocals WAV). Used
     *  when a job is deleted, so it leaves no orphaned cache behind.
     *  Safe to call when the files do not exist. */
    fun delete(context: Context, draftId: String) {
        runCatching { draftFile(context, draftId).delete() }
        runCatching { vocalsFile(context, draftId).delete() }
    }

    /** Stream the vocals download into the cache (temp file + rename,
     *  so a half-finished download never counts as cached). Returns
     *  the file on success, null on failure. */
    fun saveVocals(context: Context, draftId: String, input: InputStream): File? {
        val file = vocalsFile(context, draftId)
        val tmp = File(file.parent, file.name + ".tmp")
        return try {
            tmp.outputStream().use { out -> input.copyTo(out) }
            if (tmp.length() == 0L) {
                runCatching { tmp.delete() }
                return null
            }
            tmp.renameTo(file)
            pruneVocals(context)
            if (file.exists() && file.length() > 0) file else null
        } catch (e: Exception) {
            runCatching { tmp.delete() }
            AppLog.e("EditorCache", "Could not save vocals $draftId: ${e.message}")
            null
        }
    }

    /** Keep only the newest few vocals WAVs — they are tens of MB each. */
    private fun pruneVocals(context: Context) {
        val files = dir(context).listFiles()
            ?.filter { it.name.startsWith("vocals-") && it.name.endsWith(".wav") }
            ?.sortedBy { it.lastModified() }
            ?: return
        for (old in files.dropLast(MAX_VOCALS)) {
            runCatching { old.delete() }
        }
    }

    private fun pruneDrafts(context: Context) {
        val files = dir(context).listFiles()
            ?.filter { it.name.startsWith("draft-") && it.name.endsWith(".json") }
            ?.sortedBy { it.lastModified() }
            ?: return
        for (old in files.dropLast(MAX_DRAFTS)) {
            runCatching { old.delete() }
        }
    }
}
