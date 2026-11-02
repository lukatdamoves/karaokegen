package com.lukatdamoves.karaokegen

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

/**
 * Small job history for the sidebar Jobs view.
 *
 * One entry per submitDraft: the song title, the draft id (audio hash),
 * the Modal call id, the status and the time. Newest first, capped at
 * 20 entries. Stored as a JSON array in SharedPreferences — jobs are
 * few and short-lived, so no database is needed.
 */
object JobStore {
    private const val PREFS = "karaokegen_jobs"
    private const val KEY_JOBS = "jobs_json"
    private const val MAX_JOBS = 20

    data class JobEntry(
        val title: String,
        val draftJobId: String,
        val callId: String,
        val status: String, // "running" | "done" | "error"
        val timestamp: Long
    )

    /** Record a newly submitted draft (status "running"). */
    fun record(context: Context, title: String, draftJobId: String, callId: String) {
        val jobs = getAll(context).toMutableList()
        jobs.add(
            0,
            JobEntry(title, draftJobId, callId, "running", System.currentTimeMillis())
        )
        save(context, jobs.take(MAX_JOBS))
    }

    /** Update the newest entry for this draft id (a re-run of the same
     *  song shares the draft id, so the newest match is the right one). */
    fun updateStatus(context: Context, draftJobId: String, status: String) {
        val jobs = getAll(context).toMutableList()
        val idx = jobs.indexOfFirst { it.draftJobId == draftJobId }
        if (idx >= 0) {
            jobs[idx] = jobs[idx].copy(status = status)
            save(context, jobs)
        }
    }

    /** Delete exactly one entry. The call id is unique per submit — a
     *  re-run of the same song shares the draft id but gets a new call
     *  id — so this removes only the tapped row, never a sibling. */
    fun remove(context: Context, callId: String) {
        val jobs = getAll(context).toMutableList()
        val idx = jobs.indexOfFirst { it.callId == callId }
        if (idx >= 0) {
            jobs.removeAt(idx)
            save(context, jobs)
        }
    }

    /** True when some remaining job still uses this draft id (a re-run
     *  of the same song) — its cached editor data must be kept. */
    fun hasDraft(context: Context, draftJobId: String): Boolean =
        getAll(context).any { it.draftJobId == draftJobId }

    /** Newest first. */
    fun getAll(context: Context): List<JobEntry> {
        val raw = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_JOBS, null) ?: return emptyList()
        return try {
            val arr = JSONArray(raw)
            buildList {
                for (i in 0 until arr.length()) {
                    val o = arr.optJSONObject(i) ?: continue
                    add(
                        JobEntry(
                            title = o.optString("title"),
                            draftJobId = o.optString("draftJobId"),
                            callId = o.optString("callId"),
                            status = o.optString("status").ifEmpty { "running" },
                            timestamp = o.optLong("timestamp")
                        )
                    )
                }
            }
        } catch (_: Exception) {
            emptyList()
        }
    }

    /** Newest finished ("done") job, for resuming the editor from the
     *  sidebar when there is no in-memory draft. */
    fun getLatestDone(context: Context): JobEntry? =
        getAll(context).firstOrNull { it.status == "done" }

    private fun save(context: Context, jobs: List<JobEntry>) {
        val arr = JSONArray()
        for (job in jobs) {
            arr.put(
                JSONObject().apply {
                    put("title", job.title)
                    put("draftJobId", job.draftJobId)
                    put("callId", job.callId)
                    put("status", job.status)
                    put("timestamp", job.timestamp)
                }
            )
        }
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putString(KEY_JOBS, arr.toString())
            .apply()
    }
}
