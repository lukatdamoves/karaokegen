package com.lukatdamoves.karaokegen

import android.util.Base64
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okio.BufferedSink
import org.json.JSONObject
import java.io.File
import java.io.FileInputStream
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
     * Submit a draft (or transcribe-only) job. The audio is streamed to the
     * request as base64 in small chunks — no giant base64 string in memory,
     * which matters on low-RAM phones.
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
        val body = streamingDraftBody(audioFile, language, lyrics, genre)
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

    /**
     * JSON body for DraftRequest, with the audio base64-encoded in 24 KiB
     * chunks (a multiple of 3 bytes, so chunk boundaries never split a
     * base64 group). Same shape the desktop app sends: audio_b64, language,
     * lyrics, genre, backing_vocals=false, start_s=0.
     */
    private fun streamingDraftBody(
        audioFile: File,
        language: String,
        lyrics: String,
        genre: String
    ): RequestBody {
        val prefix = """{"audio_b64":""".toByteArray(Charsets.UTF_8)
        val suffix = (
            ""","language":""" + JSONObject.quote(language) +
                ""","lyrics":""" + JSONObject.quote(lyrics) +
                ""","genre":""" + JSONObject.quote(genre) +
                ""","backing_vocals":false,"start_s":0}"""
            ).toByteArray(Charsets.UTF_8)
        val b64Len = 4L * ((audioFile.length() + 2) / 3)
        val totalLen = prefix.size + b64Len + suffix.size
        return object : RequestBody() {
            override fun contentType() = jsonType
            override fun contentLength() = totalLen

            override fun writeTo(sink: BufferedSink) {
                sink.write(prefix)
                val buf = ByteArray(3 * 8192)
                FileInputStream(audioFile).use { input ->
                    while (true) {
                        var read = 0
                        while (read < buf.size) {
                            val n = input.read(buf, read, buf.size - read)
                            if (n == -1) break
                            read += n
                        }
                        if (read == 0) break
                        val chunk = if (read == buf.size) buf else buf.copyOf(read)
                        val encoded = Base64.encodeToString(chunk, Base64.NO_WRAP)
                        sink.write(encoded.toByteArray(Charsets.US_ASCII))
                        if (read < buf.size) break
                    }
                }
                sink.write(suffix)
            }
        }
    }
}
