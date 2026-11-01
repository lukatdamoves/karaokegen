package com.lukatdamoves.karaokegen

import android.util.Base64
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONObject
import java.io.File
import java.io.FilterInputStream
import java.io.IOException
import java.io.InputStream
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

    // One shared client for the whole app (thread-safe). The in-app
    // localhost server makes many requests (stems, polling, render),
    // and a fresh OkHttpClient per request would leak threads/pools.
    private val client: OkHttpClient
        get() = sharedClient

    companion object {
        private val sharedClient = OkHttpClient.Builder()
            .connectTimeout(30, TimeUnit.SECONDS)
            .readTimeout(300, TimeUnit.SECONDS)
            .writeTimeout(300, TimeUnit.SECONDS)
            .build()
    }

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
        transcribeOnly: Boolean,
        startS: Double = 0.0
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
            put("start_s", startS)
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

    // ------------------------------------------------------------------
    // Raw / streaming access for the in-app localhost server (the editor
    // proxy). These return the backend's JSON untouched, or a live stream
    // for stems / the finished MP4 — nothing is re-shaped here.
    // ------------------------------------------------------------------

    /** Full GET /jobs/{call_id} response, untouched ({status, result, error}). */
    @Throws(IOException::class, ModalError::class)
    fun getJobRaw(callId: String): JSONObject {
        val req = authed("$base/jobs/$callId").get().build()
        client.newCall(req).execute().use { resp ->
            val text = resp.body?.string() ?: ""
            if (!resp.isSuccessful) {
                throw ModalError("Backend said HTTP ${resp.code}: ${text.take(300)}")
            }
            return JSONObject(text)
        }
    }

    /** Raw GET /progress/{id} (or /render-progress/{id}); null on any failure. */
    fun getProgressRaw(draftJobId: String, render: Boolean): JSONObject? {
        return try {
            val path = if (render) "render-progress" else "progress"
            val req = authed("$base/$path/$draftJobId").get().build()
            client.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) return null
                JSONObject(resp.body?.string() ?: return null)
            }
        } catch (e: Exception) {
            null
        }
    }

    /** Raw GET /draft-result/{id}; null when it is not there yet. */
    fun getDraftResultRaw(draftJobId: String): JSONObject? {
        return try {
            val req = authed("$base/draft-result/$draftJobId").get().build()
            client.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) return null
                JSONObject(resp.body?.string() ?: return null)
            }
        } catch (e: Exception) {
            null
        }
    }

    /** POST /render (or /render-word) with the editor's JSON body, untouched. */
    @Throws(IOException::class, ModalError::class)
    fun submitRender(body: JSONObject, wordLevel: Boolean): JSONObject {
        val endpoint = if (wordLevel) "/render-word" else "/render"
        val req = authed(base + endpoint)
            .post(body.toString().toRequestBody(jsonType)).build()
        client.newCall(req).execute().use { resp ->
            val text = resp.body?.string() ?: ""
            if (!resp.isSuccessful) {
                throw ModalError("Backend said HTTP ${resp.code}: ${text.take(300)}")
            }
            return JSONObject(text)
        }
    }

    /**
     * GET [path] (relative to the backend base, e.g. "/files/<id>" or
     * "/vocals-raw/<id>") as a live stream. The caller reads it and
     * closes it; closing the stream closes the HTTP response too.
     * Throws ModalError on a non-2xx status.
     */
    @Throws(IOException::class, ModalError::class)
    fun openStream(path: String): InputStream {
        val req = authed(base + path).get().build()
        val resp = client.newCall(req).execute()
        if (!resp.isSuccessful) {
            val text = try { resp.body?.string() ?: "" } catch (_: Exception) { "" }
            resp.close()
            throw ModalError("Backend said HTTP ${resp.code}: ${text.take(300)}")
        }
        return ResponseInputStream(resp)
    }

    /** An OkHttp response body stream that closes the response when closed. */
    private class ResponseInputStream(private val response: Response) :
        FilterInputStream(response.body.byteStream()) {
        override fun close() {
            try { super.close() } finally { response.close() }
        }
    }

}
