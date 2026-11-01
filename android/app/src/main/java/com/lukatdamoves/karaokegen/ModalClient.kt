package com.lukatdamoves.karaokegen

import android.util.Base64
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit

/** Plain-language failure from the Modal backend step. Shown on screen as-is. */
class ModalError(message: String) : Exception(message)

data class DraftSubmitted(val jobId: String, val draftJobId: String)
data class JobResult(val status: String, val result: JSONObject?)
data class ProgressInfo(val stage: String, val pct: Int, val detail: String)

/**
 * Minimal client for the KaraokeGen Modal backend — the same endpoints the
 * desktop app uses (see modal_app.py): POST /draft, POST /transcribe,
 * GET /jobs/{call_id}, GET /progress/{draft_job_id}, POST /cancel/{call_id}.
 * Auth is `Authorization: Bearer <API key>`. No deploy, no other endpoints.
 */
class ModalClient(baseUrl: String, private val apiKey: String) {
    private val base = baseUrl.trim().trimEnd('/')

    private val client = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(300, TimeUnit.SECONDS)
        .writeTimeout(300, TimeUnit.SECONDS)
        .build()

    private val jsonType = "application/json; charset=utf-8".toMediaType()

    private fun authed(url: String): Request.Builder =
        Request.Builder().url(url).addHeader("Authorization", "Bearer $apiKey")

    /**
     * Submit a draft (or transcribe-only) job. The audio is base64'd
     * into one JSON body (like the desktop app). A 3 MB song becomes
     * ~4 MB of base64 — fine in memory, and far less error-prone than
     * streaming the JSON in chunks (a chunked build once produced a
     * malformed body the backend rejected with 422).
     */
    @Throws(IOException::class, ModalError::class)
    fun submitDraft(
        audioFile: File,
        language: String,
        lyrics: String,
        genre: String,
        transcribeOnly: Boolean
    ): DraftSubmitted {
        val endpoint = if (transcribeOnly) "/transcribe" else "/draft"
        val audioB64 = Base64.encodeToString(
            audioFile.readBytes(), Base64.NO_WRAP)
        val bodyJson = JSONObject().apply {
            put("audio_b64", audioB64)
            put("language", language)
            put("lyrics", lyrics)
            put("genre", genre)
            put("backing_vocals", false)
            put("start_s", 0)
        }
        val body = bodyJson.toString().toRequestBody(jsonType)
        val req = authed(base + endpoint).post(body).build()
        client.newCall(req).execute().use { resp ->
            val text = resp.body?.string() ?: ""
            if (!resp.isSuccessful) {
                throw ModalError("Backend said HTTP ${resp.code}: ${text.take(300)}")
            }
            val json = JSONObject(text)
            val jobId = json.optString("job_id")
            val draftJobId = json.optString("draft_job_id")
            if (jobId.isEmpty() || draftJobId.isEmpty()) {
                throw ModalError("Backend reply was missing the job id.")
            }
            return DraftSubmitted(jobId, draftJobId)
        }
    }

    @Throws(IOException::class, ModalError::class)
    fun getJob(callId: String): JobResult {
        val req = authed("$base/jobs/$callId").get().build()
        client.newCall(req).execute().use { resp ->
            val text = resp.body?.string() ?: ""
            if (!resp.isSuccessful) {
                throw ModalError("Backend said HTTP ${resp.code}: ${text.take(300)}")
            }
            val json = JSONObject(text)
            return JobResult(json.optString("status"), json.optJSONObject("result"))
        }
    }

    /** Live stage progress. Returns null on any failure — polling must not die on it. */
    fun getProgress(draftJobId: String): ProgressInfo? {
        return try {
            val req = authed("$base/progress/$draftJobId").get().build()
            client.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) return null
                val json = JSONObject(resp.body?.string() ?: return null)
                ProgressInfo(
                    json.optString("stage"),
                    json.optInt("pct"),
                    json.optString("detail")
                )
            }
        } catch (e: Exception) {
            null
        }
    }

    /** Best-effort cancel; failures are swallowed (the poll loop is stopping anyway). */
    fun cancel(callId: String) {
        try {
            val req = authed("$base/cancel/$callId")
                .post("".toRequestBody(null)).build()
            client.newCall(req).execute().close()
        } catch (e: Exception) {
            // Ignore — cancelling is best-effort.
        }
    }

}
