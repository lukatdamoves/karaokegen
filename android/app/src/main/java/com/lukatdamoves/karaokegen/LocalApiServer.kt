package com.lukatdamoves.karaokegen

import android.content.Context
import fi.iki.elonen.NanoHTTPD
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.net.URLDecoder

/**
 * Tiny in-app localhost server that serves the desktop editor
 * (editor.html + editor.js + wavesurfer, single-sourced from
 * local_app/ at build time — see the copyEditorAssets Gradle task)
 * and proxies its /api/* calls to the Modal backend using the saved
 * backend URL + API key.
 *
 * Why a localhost server instead of a JS bridge: editor.js keeps
 * calling /api/* exactly like the desktop, so it needs ZERO changes.
 * The only Android-specific bits live here: the two Jinja placeholders
 * in editor.html are substituted (build stamp + the config overlay
 * visibility), a small touch shim is injected (wavesurfer Regions is
 * pointer-event driven but the page has no touch-action handling, so
 * a drag would scroll the page instead), and a bootstrap script seeds
 * localStorage from the ?draft=/?call=/?name= query so the editor
 * resumes a draft the native flow already finished.
 *
 * Lyric-video mode is NOT served here (deferred): a video upload gets
 * a clear 400, and /api/video-karaoke returns 501.
 */
object LocalApiServer : NanoHTTPD("127.0.0.1", 0) {

    private const val TAG = "LocalApi"
    private const val MAX_AUDIO_BYTES = 100L * 1024 * 1024

    // The desktop deploy UI cannot run on a phone (no `modal deploy`).
    // Backend setup lives in the native Backend settings screen.
    private const val DEPLOY_UNAVAILABLE =
        "Backend setup is done in the app's Backend settings (home screen), not here."

    @Volatile
    private var appContext: Context? = null

    @Volatile
    private var started = false

    /** Start (once) and return the base URL, e.g. http://127.0.0.1:54321 */
    @Synchronized
    fun start(context: Context): String {
        appContext = context.applicationContext
        if (!started) {
            start(SOCKET_READ_TIMEOUT, false)
            started = true
            AppLog.d(TAG, "listening on 127.0.0.1:$listeningPort")
        }
        return "http://127.0.0.1:$listeningPort"
    }

    @Synchronized
    fun stopServer() {
        if (started) {
            try { stop() } catch (_: Exception) {}
            started = false
        }
    }

    private fun modalClient(): ModalClient {
        val ctx = appContext ?: throw IllegalStateException("LocalApiServer not started")
        return ModalClient(SettingsStore.getBaseUrl(ctx), SettingsStore.getApiKey(ctx))
    }

    private fun isConfigured(): Boolean {
        val ctx = appContext ?: return false
        return SettingsStore.isConfigured(ctx)
    }

    // ------------------------------------------------------------------
    // Routing
    // ------------------------------------------------------------------

    override fun serve(session: IHTTPSession): Response {
        val uri = session.uri
        val method = session.method
        return try {
            when {
                method == Method.GET && uri == "/" -> serveEditor(session)
                method == Method.GET && uri.startsWith("/static/") ->
                    serveAsset(uri.removePrefix("/static/"))
                method == Method.GET && uri == "/favicon.ico" ->
                    serveAsset("favicon.ico")
                method == Method.GET && uri == "/api/config" -> serveConfig()
                method == Method.GET && uri == "/api/modal/status" -> serveModalStatus()
                method == Method.POST && uri == "/api/modal/token" -> jsonError(400, DEPLOY_UNAVAILABLE)
                method == Method.POST && uri == "/api/modal/deploy" -> jsonError(400, DEPLOY_UNAVAILABLE)
                method == Method.GET && uri == "/api/modal/deploy/log" -> jsonError(400, DEPLOY_UNAVAILABLE)
                method == Method.POST && uri == "/api/modal/disconnect" -> serveDisconnect()
                method == Method.POST && uri == "/api/draft" -> serveDraft(session, transcribeOnly = false)
                method == Method.POST && uri == "/api/transcribe" -> serveDraft(session, transcribeOnly = true)
                method == Method.GET && uri.startsWith("/api/jobs/") ->
                    serveJob(uri.removePrefix("/api/jobs/"))
                method == Method.GET && uri.startsWith("/api/progress/") ->
                    serveProgress(uri.removePrefix("/api/progress/"), render = false)
                method == Method.GET && uri.startsWith("/api/render-progress/") ->
                    serveProgress(uri.removePrefix("/api/render-progress/"), render = true)
                method == Method.GET && uri.startsWith("/api/draft-result/") ->
                    serveDraftResult(uri.removePrefix("/api/draft-result/"))
                method == Method.POST && uri.startsWith("/api/cancel/") ->
                    serveCancel(uri.removePrefix("/api/cancel/"))
                method == Method.POST && uri == "/api/render" -> serveRender(session, wordLevel = false)
                method == Method.POST && uri == "/api/render-word" -> serveRender(session, wordLevel = true)
                method == Method.GET && uri.startsWith("/api/file/") ->
                    serveStream(uri.removePrefix("/api/file/"), "/files/", "video/mp4", "mp4")
                method == Method.GET && uri.startsWith("/api/vocals-raw/") ->
                    serveStream(uri.removePrefix("/api/vocals-raw/"), "/vocals-raw/", "audio/wav", "wav")
                method == Method.GET && uri.startsWith("/api/instrumental/") ->
                    serveStream(uri.removePrefix("/api/instrumental/"), "/instrumental/", "audio/wav", "wav")
                method == Method.GET && uri.startsWith("/api/second-voice/") ->
                    serveStream(uri.removePrefix("/api/second-voice/"), "/second-voice/", "audio/wav", "wav")
                method == Method.GET && uri.startsWith("/api/backing-vocals/") ->
                    serveStream(uri.removePrefix("/api/backing-vocals/"), "/backing-vocals/", "audio/wav", "wav")
                method == Method.GET && uri.startsWith("/api/video-karaoke/") ->
                    jsonError(501, "Lyric-video mode isn't in the Android app yet.")
                method == Method.GET && uri.startsWith("/api/preview-ass/") ->
                    jsonError(501, "Preview ASS isn't available in the Android app.")
                else -> newFixedLengthResponse(Response.Status.NOT_FOUND, "text/plain", "not found")
            }
        } catch (e: ModalError) {
            AppLog.e(TAG, "Modal error on $uri: ${e.message}")
            jsonError(502, e.message ?: "Backend error.")
        } catch (e: IOException) {
            AppLog.e(TAG, "IO error on $uri: ${e.message}")
            jsonError(502, "Couldn't reach the backend — check your connection.")
        } catch (e: Exception) {
            AppLog.e(TAG, "Error on $uri: ${e.message}")
            jsonError(500, e.message ?: "Server error.")
        }
    }

    // ------------------------------------------------------------------
    // Editor static files
    // ------------------------------------------------------------------

    /** The editor page, with the desktop Jinja placeholders substituted. */
    private fun serveEditor(session: IHTTPSession): Response {
        val html = readAssetText("editor.html")
        val params = session.parameters
        val draftId = params["draft"]?.firstOrNull()?.let { URLDecoder.decode(it, "UTF-8") }.orEmpty()
        val callId = params["call"]?.firstOrNull()?.let { URLDecoder.decode(it, "UTF-8") }.orEmpty()
        val fileName = params["name"]?.firstOrNull()?.let { URLDecoder.decode(it, "UTF-8") }.orEmpty()
        val configured = isConfigured()
        var out = html
            .replace("{{ build_stamp }}", "android-" + BuildConfig.VERSION_NAME)
            .replace(
                "{% if configured %}hidden{% endif %}",
                if (configured) "hidden" else ""
            )
        // Touch shim + handoff bootstrap, injected before editor.js runs.
        // The regions plugin is pointer-event driven; without
        // touch-action:none a finger drag scrolls the page instead of
        // moving a region. The bootstrap seeds the job keys so the
        // editor's own resume logic (checkResumable) picks up a draft
        // the native flow already finished.
        val seed = StringBuilder()
        if (callId.isNotEmpty()) {
            seed.append("localStorage.setItem('kg_call_id',")
                .append(JSONObject.quote(callId)).append(");")
        }
        if (draftId.isNotEmpty()) {
            seed.append("localStorage.setItem('kg_draft_job_id',")
                .append(JSONObject.quote(draftId)).append(");")
        }
        if (fileName.isNotEmpty()) {
            seed.append("localStorage.setItem('kg_file_name',")
                .append(JSONObject.quote(fileName)).append(");")
        }
        val inject = "<style>#waveform,#waveform *{touch-action:none}</style>" +
            "<script>try{" + seed.toString() + "}catch(e){}</script>"
        out = out.replace("</head>", inject + "</head>")
        return newFixedLengthResponse(Response.Status.OK, "text/html", out)
    }

    private fun serveAsset(name: String): Response {
        val (mime, bytes) = readAsset(name)
        val resp = newFixedLengthResponse(Response.Status.OK, mime, bytes.inputStream(), bytes.size.toLong())
        return resp
    }

    private fun readAssetText(name: String): String {
        val ctx = appContext ?: throw IllegalStateException("LocalApiServer not started")
        return ctx.assets.open("editor/$name").bufferedReader(Charsets.UTF_8).use { it.readText() }
    }

    private fun readAsset(name: String): Pair<String, ByteArray> {
        val ctx = appContext ?: throw IllegalStateException("LocalApiServer not started")
        val bytes = ctx.assets.open("editor/$name").use { it.readBytes() }
        val mime = when (name.substringAfterLast('.', "")) {
            "html" -> "text/html"
            "js" -> "application/javascript"
            "css" -> "text/css"
            "ico" -> "image/x-icon"
            "png" -> "image/png"
            else -> "application/octet-stream"
        }
        return mime to bytes
    }

    // ------------------------------------------------------------------
    // Config / deploy stubs
    // ------------------------------------------------------------------

    private fun serveConfig(): Response {
        val ctx = appContext ?: throw IllegalStateException("LocalApiServer not started")
        val url = SettingsStore.getBaseUrl(ctx)
        val key = SettingsStore.getApiKey(ctx)
        val json = JSONObject().apply {
            put("modal_api_url", url)
            put("modal_api_key_set", key.isNotEmpty())
            put("modal_api_key_preview", if (key.length >= 8) key.take(4) + "..." + key.takeLast(4) else if (key.isEmpty()) "" else "...")
            put("configured", SettingsStore.isConfigured(ctx))
            put("config_path", "Android app settings")
        }
        return jsonResponse(json)
    }

    private fun serveModalStatus(): Response {
        val ctx = appContext ?: throw IllegalStateException("LocalApiServer not started")
        val url = SettingsStore.getBaseUrl(ctx)
        val key = SettingsStore.getApiKey(ctx)
        val json = JSONObject().apply {
            put("modal_api_url", url)
            put("modal_api_key_preview", if (key.length >= 8) key.take(4) + "..." + key.takeLast(4) else "")
            put("logged_in", SettingsStore.isConfigured(ctx))
            put("configured", SettingsStore.isConfigured(ctx))
        }
        return jsonResponse(json)
    }

    private fun serveDisconnect(): Response {
        val ctx = appContext ?: throw IllegalStateException("LocalApiServer not started")
        SettingsStore.save(ctx, "", "")
        AppLog.d(TAG, "backend disconnected from the editor settings overlay")
        return jsonResponse(JSONObject().put("ok", true))
    }

    // ------------------------------------------------------------------
    // Draft / transcribe (multipart upload from the editor)
    // ------------------------------------------------------------------

    private fun serveDraft(session: IHTTPSession, transcribeOnly: Boolean): Response {
        if (!isConfigured()) {
            return jsonError(412, "Modal backend not configured — set it up in the app's Backend settings first.")
        }
        val files = HashMap<String, String>()
        try {
            session.parseBody(files)
        } catch (e: Exception) {
            return jsonError(400, "Couldn't read the upload: ${e.message}")
        }
        val params = session.parameters
        // Lyric-video mode is deferred — a video upload gets a clear
        // error instead of a confusing failure downstream.
        val videoPath = files["video"]
        if (videoPath != null) {
            deleteQuietly(videoPath)
            return jsonError(400, "Lyric-video mode isn't in the Android app yet — pick an audio file.")
        }
        val audioPath = files["audio"]
            ?: return jsonError(400, "Provide an audio file.")
        val audioFile = File(audioPath)
        try {
            if (audioFile.length() > MAX_AUDIO_BYTES) {
                return jsonError(413, "Audio too large (max 100 MB).")
            }
            if (audioFile.length() == 0L) {
                return jsonError(400, "The audio file is empty.")
            }
            val lyrics = params["lyrics"]?.firstOrNull().orEmpty()
            val language = params["language"]?.firstOrNull().orEmpty()
            val genre = params["genre"]?.firstOrNull().orEmpty().ifEmpty { "hiphop" }
            val startS = params["start_s"]?.firstOrNull()?.toDoubleOrNull() ?: 0.0
            val submitted = modalClient().submitDraft(
                audioFile,
                language = language,
                lyrics = lyrics,
                genre = genre,
                transcribeOnly = transcribeOnly,
                startS = startS
            )
            val json = JSONObject().apply {
                put("job_id", submitted.jobId)
                put("draft_job_id", submitted.draftJobId)
                put("status", "queued")
                put("mode", "audio")
            }
            AppLog.d(TAG, "draft submitted: job=${submitted.jobId} draft=${submitted.draftJobId} transcribe=$transcribeOnly")
            return jsonResponse(json)
        } finally {
            deleteQuietly(audioPath)
        }
    }

    // ------------------------------------------------------------------
    // Job polling / results
    // ------------------------------------------------------------------

    private fun serveJob(callId: String): Response {
        if (callId.isBlank()) return jsonError(400, "missing call id")
        return jsonResponse(modalClient().getJobRaw(callId))
    }

    private fun serveProgress(draftJobId: String, render: Boolean): Response {
        if (draftJobId.isBlank()) return jsonError(400, "missing job id")
        val json = modalClient().getProgressRaw(draftJobId, render)
            ?: return jsonError(502, "Couldn't reach the backend.")
        return jsonResponse(json)
    }

    private fun serveDraftResult(draftJobId: String): Response {
        if (draftJobId.isBlank()) return jsonError(400, "missing job id")
        val json = modalClient().getDraftResultRaw(draftJobId)
            ?: return jsonError(404, "Draft result not found (run a draft first).")
        return jsonResponse(json)
    }

    private fun serveCancel(callId: String): Response {
        if (callId.isBlank()) return jsonError(400, "missing call id")
        modalClient().cancel(callId)
        return jsonResponse(JSONObject().put("ok", true))
    }

    // ------------------------------------------------------------------
    // Render
    // ------------------------------------------------------------------

    private fun serveRender(session: IHTTPSession, wordLevel: Boolean): Response {
        if (!isConfigured()) {
            return jsonError(500, "Modal backend not configured.")
        }
        val files = HashMap<String, String>()
        try {
            session.parseBody(files)
        } catch (e: Exception) {
            return jsonError(400, "Couldn't read the render request: ${e.message}")
        }
        val rawPath = files["postData"]
            ?: return jsonError(400, "invalid JSON body")
        val raw = try {
            File(rawPath).readText(Charsets.UTF_8)
        } finally {
            deleteQuietly(rawPath)
        }
        val body = try {
            JSONObject(raw)
        } catch (e: Exception) {
            return jsonError(400, "invalid JSON body")
        }
        val result = modalClient().submitRender(body, wordLevel)
        AppLog.d(TAG, "render submitted (word=$wordLevel): ${result.optString("job_id")}")
        return jsonResponse(result)
    }

    // ------------------------------------------------------------------
    // Streaming (finished MP4 + stems)
    // ------------------------------------------------------------------

    private fun serveStream(
        id: String,
        modalPath: String,
        mime: String,
        ext: String
    ): Response {
        if (!isConfigured()) {
            return jsonError(500, "Modal backend not configured.")
        }
        if (id.isBlank() || id.length != 12 || !id.all { it in '0'..'9' || it in 'a'..'f' }) {
            return jsonError(400, "invalid id")
        }
        val stream: InputStream = modalClient().openStream(modalPath + id)
        val resp = newChunkedResponse(Response.Status.OK, mime, stream)
        resp.addHeader("Content-Disposition", "attachment; filename=\"$id.$ext\"")
        return resp
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    private fun jsonResponse(json: JSONObject): Response {
        return newFixedLengthResponse(
            Response.Status.OK, "application/json", json.toString()
        )
    }

    private fun jsonError(status: Int, detail: String): Response {
        val json = JSONObject().put("detail", detail)
        val code = Response.Status.lookup(status) ?: Response.Status.INTERNAL_ERROR
        return newFixedLengthResponse(code, "application/json", json.toString())
    }

    private fun deleteQuietly(path: String) {
        try { File(path).delete() } catch (_: Exception) {}
    }
}
