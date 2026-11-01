package com.lukatdamoves.karaokegen

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/** Failure from the auto-setup helper. Shown on screen as-is. */
class SetupError(message: String) : Exception(message)

data class SetupResult(val url: String, val apiKey: String)
data class SetupStatus(val state: String, val stage: String, val progress: Int)

/**
 * Auto-setup, like the KaraokeNow ReVanced patch: paste the `modal
 * token set ...` command Modal gives you, and the setup helper
 * deploys YOUR OWN KaraokeGen backend into your Modal account and
 * hands back its URL + API key. The token is used once for that
 * deploy and is never saved on this phone.
 *
 * Protocol (same as the ReVanced helper):
 *   POST /setup/start {token_id, token_secret} -> {job_id}
 *   GET  /setup/status/{job_id} -> {state, stage, progress, url?, api_key?, error?}
 */
object SetupClient {
    // The setup helper lives on the maintainer's Modal account.
    // Deploy it once with: modal deploy server/setup_app.py::setup_app
    // NOTE: the URL ends in -api because the web endpoint function
    // is named `api` — Modal names the endpoint after the function,
    // not the app. The app is karaokegen-setup, the URL is
    // ...--karaokegen-setup-api.modal.run (same as the ReVanced
    // helper, which is ...--karaoke-setup-api.modal.run).
    const val HELPER_URL = "https://lukatdamoves--karaokegen-setup-api.us-east.modal.run"

    private val http = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    private val jsonType = "application/json; charset=utf-8".toMediaType()
    private val tokenIdRe = Regex("""ak-[A-Za-z0-9_-]+""")
    private val tokenSecretRe = Regex("""as-[A-Za-z0-9_-]+""")

    /** Extract {tokenId, tokenSecret} from a pasted `modal token set ...`
     *  command. Returns null when the line has no valid token pair. */
    fun parseTokenCommand(text: String): Pair<String, String>? {
        val id = tokenIdRe.find(text)?.value ?: return null
        val secret = tokenSecretRe.find(text)?.value ?: return null
        return id to secret
    }

    /** Start a setup job. Returns the job id. */
    @Throws(IOException::class, SetupError::class)
    suspend fun startSetup(tokenId: String, tokenSecret: String): String =
        withContext(Dispatchers.IO) {
            val body = JSONObject()
                .put("token_id", tokenId)
                .put("token_secret", tokenSecret)
                .toString()
                .toRequestBody(jsonType)
            val req = Request.Builder()
                .url("$HELPER_URL/setup/start")
                .post(body)
                .build()
            http.newCall(req).execute().use { resp ->
                val text = resp.body?.string() ?: ""
                if (!resp.isSuccessful) {
                    throw SetupError(setupErrorMessage(text, resp.code))
                }
                val json = JSONObject(text)
                val jobId = json.optString("job_id")
                if (jobId.isEmpty()) {
                    throw SetupError("The setup helper didn't start a job. Try again.")
                }
                return@withContext jobId
            }
        }

    /** One status read. Returns null when the helper can't be reached. */
    suspend fun getStatus(jobId: String): SetupStatus? =
        withContext(Dispatchers.IO) {
            try {
                val req = Request.Builder()
                    .url("$HELPER_URL/setup/status/$jobId")
                    .get()
                    .build()
                http.newCall(req).execute().use { resp ->
                    if (!resp.isSuccessful) return@withContext null
                    val json = JSONObject(resp.body?.string() ?: return@withContext null)
                    SetupStatus(
                        state = json.optString("state", "running"),
                        stage = json.optString("stage", ""),
                        progress = json.optInt("progress", -1)
                    )
                }
            } catch (e: IOException) {
                null
            }
        }

    /** The finished job's URL + API key. Returns null when not done. */
    suspend fun getResult(jobId: String): SetupResult? =
        withContext(Dispatchers.IO) {
            try {
                val req = Request.Builder()
                    .url("$HELPER_URL/setup/status/$jobId")
                    .get()
                    .build()
                http.newCall(req).execute().use { resp ->
                    if (!resp.isSuccessful) return@withContext null
                    val json = JSONObject(resp.body?.string() ?: return@withContext null)
                    if (json.optString("state") != "done") return@withContext null
                    val url = json.optString("url")
                    val key = json.optString("api_key")
                    if (url.isEmpty() || key.isEmpty()) return@withContext null
                    SetupResult(url, key)
                }
            } catch (e: IOException) {
                null
            }
        }

    /** Poll a job to completion (up to ~15 min), reporting stage/progress.
     *  Returns the URL + API key when done. Throws SetupError on failure. */
    @Throws(IOException::class, SetupError::class)
    suspend fun pollToDone(
        jobId: String,
        onStatus: (stage: String, progress: Int) -> Unit
    ): SetupResult {
        val startedAt = System.currentTimeMillis()
        var consecutiveNulls = 0
        while (true) {
            delay(4000)
            if (System.currentTimeMillis() - startedAt > 15 * 60 * 1000) {
                throw SetupError("Setup timed out — try again.")
            }
            val status = getStatus(jobId)
            if (status == null) {
                consecutiveNulls++
                if (consecutiveNulls >= 5) {
                    throw SetupError("Setup helper unreachable — try again.")
                }
                onStatus("Waiting for the setup helper…", -1)
                continue
            }
            consecutiveNulls = 0
            when (status.state) {
                "done" -> {
                    return getResult(jobId)
                        ?: throw SetupError("Setup finished but sent no address. Try again.")
                }
                "error" -> {
                    // The helper's error text is plain and safe to show.
                    throw SetupError("Setup failed. Try again.")
                }
                "unknown" -> {
                    throw SetupError("Setup job not found — try again.")
                }
                else -> {
                    val stage = status.stage.ifEmpty { "Deploying…" }
                    onStatus(stage, status.progress)
                }
            }
        }
    }

    /** Plain-language reason from a failed /setup/start call. */
    private fun setupErrorMessage(body: String, code: Int): String {
        if (body.isEmpty()) return "Setup helper unreachable (HTTP $code). Try again."
        return try {
            val json = JSONObject(body)
            val err = json.optString("error")
            if (err.isNotEmpty()) err else "Setup failed (HTTP $code). Try again."
        } catch (e: Exception) {
            "Setup failed (HTTP $code). Try again."
        }
    }
}
