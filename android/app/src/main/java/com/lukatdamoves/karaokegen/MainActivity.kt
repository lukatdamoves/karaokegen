package com.lukatdamoves.karaokegen

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.Environment
import android.view.View
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.lukatdamoves.karaokegen.databinding.ActivityMainBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.io.IOException

/**
 * KaraokeGen trial: paste a YouTube link -> one Start button does the whole
 * thing (download the audio, then draft it on the Modal backend — with
 * lyrics, or transcribe when the lyrics box is empty). The editor and
 * render come later. Update check runs silently on launch.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding

    private var flowJob: Job? = null
    private var currentJobId: String? = null
    private var pendingUpdate: UpdateManager.UpdateInfo? = null
    // Last downloaded audio, so "Run alignment" (the follow-up /draft
    // after a transcript) reuses the file already on disk instead of
    // downloading the same song again. Same URL + file still present.
    private var cachedAudioUrl: String? = null
    private var cachedResolved: YoutubeAudio.ResolvedAudio? = null
    private var cachedSendFile: File? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.startButton.setOnClickListener { onStartTapped() }
        binding.cancelButton.setOnClickListener { onCancel() }
        binding.settingsButton.setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }
        binding.openEditorButton.setOnClickListener {
            // Resume a previous job in the editor (it restores from its
            // own saved state), or start fresh from the editor's draft form.
            startActivity(EditorActivity.newIntent(this))
        }
        binding.copyLogsButton.setOnClickListener { onCopyLogs() }
        binding.updateButton.setOnClickListener { onUpdateTapped() }
        binding.updateLaterButton.setOnClickListener {
            binding.updateCard.visibility = View.GONE
            pendingUpdate = null
        }

        checkForUpdateSilently()
    }

    override fun onDestroy() {
        flowJob?.cancel()
        super.onDestroy()
    }

    // ------------------------------------------------------------------
    // In-app updates (silent on failure — no popup if there is no update).
    // ------------------------------------------------------------------

    private fun checkForUpdateSilently() {
        lifecycleScope.launch {
            val update = UpdateManager.checkForUpdate(this@MainActivity) ?: return@launch
            pendingUpdate = update
            binding.updateTitle.text =
                getString(R.string.update_available) + " — v" + update.versionName
            binding.updateCard.visibility = View.VISIBLE
        }
    }

    private fun onUpdateTapped() {
        val update = pendingUpdate ?: return
        if (!UpdateManager.canInstall(this)) {
            Toast.makeText(this, R.string.update_allow_install, Toast.LENGTH_LONG).show()
            UpdateManager.openInstallPermissionSettings(this)
            return
        }
        binding.updateButton.isEnabled = false
        binding.updateLaterButton.isEnabled = false
        binding.updateProgress.visibility = View.VISIBLE
        binding.updateProgress.isIndeterminate = false
        binding.updateProgress.progress = 0
        binding.updateText.text = getString(R.string.update_downloading)
        lifecycleScope.launch {
            try {
                val apk = UpdateManager.downloadUpdate(this@MainActivity, update) { done, total ->
                    runOnUiThread {
                        if (total > 0) {
                            binding.updateProgress.progress = (done * 100 / total).toInt()
                        }
                    }
                }
                UpdateManager.installUpdate(this@MainActivity, apk)
            } catch (e: IOException) {
                binding.updateText.text = "Update download failed — check your connection and try again."
            } catch (e: Exception) {
                binding.updateText.text = "Update failed: ${e.message}"
            } finally {
                binding.updateButton.isEnabled = true
                binding.updateLaterButton.isEnabled = true
                binding.updateProgress.visibility = View.GONE
            }
        }
    }

    // ------------------------------------------------------------------
    // Copy logs — paste them in chat so issues can be fixed.
    // ------------------------------------------------------------------

    private fun onCopyLogs() {
        val logs = AppLog.snapshot()
        if (logs.isEmpty()) {
            Toast.makeText(this, R.string.logs_empty, Toast.LENGTH_SHORT).show()
            return
        }
        val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        clipboard.setPrimaryClip(ClipData.newPlainText("KaraokeGen logs", logs))
        Toast.makeText(this, R.string.logs_copied, Toast.LENGTH_SHORT).show()
        AppLog.d("Main", "Logs copied (${logs.length} chars)")
    }

    // ------------------------------------------------------------------
    // One Start button: link -> audio -> draft/transcribe -> result.
    // ------------------------------------------------------------------

    private fun showError(msg: String) {
        binding.errorText.text = msg
        binding.errorText.visibility = View.VISIBLE
    }

    private fun clearError() {
        binding.errorText.visibility = View.GONE
    }

    private fun setRunning(running: Boolean) {
        binding.startButton.isEnabled = !running
        binding.cancelButton.visibility = if (running) View.VISIBLE else View.GONE
        if (!running) {
            binding.audioProgress.visibility = View.GONE
            binding.draftProgress.visibility = View.GONE
        }
    }

    private fun onStartTapped() {
        val url = binding.urlInput.text?.toString()?.trim().orEmpty()
        if (url.isEmpty()) {
            showError("Paste a YouTube link first.")
            return
        }
        if (!url.contains("youtube.com") && !url.contains("youtu.be")) {
            showError("That doesn't look like a YouTube link.")
            return
        }
        if (!SettingsStore.isConfigured(this)) {
            showError("Set up your backend first — open Backend settings and paste your Modal URL and API key.")
            startActivity(Intent(this, SettingsActivity::class.java))
            return
        }
        // A fresh run is always "Start" — the "Run alignment" label only
        // applies right after a transcribe, and showDraftDone sets it.
        binding.startButton.text = getString(R.string.start)
        clearError()
        val lyrics = binding.lyricsInput.text?.toString().orEmpty()
        // Empty lyrics means "transcribe" automatically.
        val transcribe = lyrics.isBlank()
        val genre = if (binding.genreBallad.isChecked) "ballad" else "hiphop"
        val baseUrl = SettingsStore.getBaseUrl(this)
        val apiKey = SettingsStore.getApiKey(this)

        setRunning(true)
        binding.audioResult.visibility = View.GONE
        binding.draftResult.visibility = View.GONE
        binding.audioProgress.visibility = View.VISIBLE
        binding.audioProgress.isIndeterminate = true
        binding.audioProgress.progress = 0
        binding.audioStatus.text = "Finding the audio…"
        binding.draftStatus.text = ""
        AppLog.d("Main", "Start: $url (transcribe=$transcribe genre=$genre)")

        flowJob = lifecycleScope.launch {
            try {
                // Step 1: resolve + download the audio — unless this is
                // the follow-up "Run alignment" for the same link, in
                // which case the audio is already on disk (and the
                // backend has the stems cached), so reuse it.
                val reuseFile = if (cachedAudioUrl == url) {
                    cachedSendFile?.takeIf { it.exists() }
                } else null
                val reuseResolved = if (reuseFile != null) cachedResolved else null
                val resolved: YoutubeAudio.ResolvedAudio
                var sendFile: File
                if (reuseFile != null && reuseResolved != null) {
                    resolved = reuseResolved
                    sendFile = reuseFile
                    AppLog.d("Main", "Reusing downloaded audio: ${sendFile.length()} bytes -> ${sendFile.name}")
                    binding.audioStatus.text = "Using downloaded audio: ${resolved.title}"
                    binding.audioProgress.visibility = View.GONE
                } else {
                    val r = YoutubeAudio.resolve(url)
                    AppLog.d("Main", "Resolved: ${r.title} (${r.durationSec}s, ${r.bitrate / 1000} kbps, .${r.suffix})")
                    binding.audioStatus.text = "Downloading: ${r.title}"
                    binding.audioProgress.isIndeterminate = false
                    val safeName = r.title
                        .replace(Regex("[^A-Za-z0-9._-]+"), "_")
                        .take(80)
                        .ifEmpty { "song" }
                    val audioFile = File(
                        getExternalFilesDir(Environment.DIRECTORY_MUSIC),
                        "KaraokeGen/$safeName.${r.suffix}"
                    )
                    YoutubeAudio.download(r.audioUrl, audioFile) { done, total ->
                        runOnUiThread {
                            if (total > 0) {
                                val pct = (done * 100 / total).toInt()
                                binding.audioProgress.progress = pct
                                binding.audioStatus.text =
                                    "Downloading: ${r.title} — $pct%"
                            }
                        }
                    }
                    AppLog.d("Main", "Audio ready: ${audioFile.length()} bytes -> ${audioFile.name}")
                    // YouTube serves WebM/Opus or M4A/AAC, never MP3 — convert
                    // to MP3 on-device so the trial only ever sends .mp3. If
                    // this phone has no MP3 encoder, keep the original file
                    // (the backend decodes any format, so the draft still works).
                    sendFile = audioFile
                    if (audioFile.extension.lowercase() != "mp3") {
                        binding.audioStatus.text = "Converting to MP3…"
                        val mp3File = File(
                            audioFile.parentFile,
                            "${audioFile.nameWithoutExtension}.mp3"
                        )
                        val converted = withContext(Dispatchers.IO) {
                            AudioConvert.toMp3(audioFile, mp3File)
                        }
                        if (converted) {
                            AppLog.d("Main", "Converted to MP3: ${mp3File.length()} bytes -> ${mp3File.name}")
                            try { audioFile.delete() } catch (_: Exception) {}
                            sendFile = mp3File
                        } else {
                            AppLog.d("Main", "MP3 convert unavailable — sending original .${audioFile.extension}")
                        }
                    }
                    resolved = r
                    // Cache for a follow-up "Run alignment" on this link.
                    cachedAudioUrl = url
                    cachedResolved = r
                    cachedSendFile = sendFile
                }
                binding.audioProgress.visibility = View.GONE
                val info = buildString {
                    append(resolved.title)
                    append("\n")
                    append(formatDuration(resolved.durationSec))
                    append(" • ")
                    append(formatSize(sendFile.length()))
                    append(" • ")
                    append(resolved.bitrate / 1000)
                    append(" kbps")
                    if (resolved.uploader.isNotEmpty()) {
                        append(" • ")
                        append(resolved.uploader)
                    }
                }
                binding.audioResult.text = info
                binding.audioResult.visibility = View.VISIBLE

                // Step 2: send it to the backend (draft or transcribe).
                binding.draftProgress.visibility = View.VISIBLE
                binding.draftStatus.text =
                    if (transcribe) "Uploading audio for transcription…" else "Uploading audio…"
                val client = ModalClient(baseUrl, apiKey)
                val submitted = withContext(Dispatchers.IO) {
                    client.submitDraft(
                        sendFile,
                        language = "", // auto-detect
                        lyrics = if (transcribe) "" else lyrics,
                        genre = genre,
                        transcribeOnly = transcribe
                    )
                }
                currentJobId = submitted.jobId
                AppLog.d("Main", "Draft submitted: job=${submitted.jobId} draft=${submitted.draftJobId}")
                binding.draftStatus.text = "Queued — waiting for the GPU…"
                // Poll for the result. A single failed poll (a DNS blip,
                // a dropped signal) must NOT kill the flow — the job is
                // already running on the backend. Only give up after 10
                // failed polls in a row (~30s with no connection at all).
                var failedPolls = 0
                while (true) {
                    delay(3000)
                    val job = try {
                        withContext(Dispatchers.IO) { client.getJob(submitted.jobId) }
                    } catch (e: IOException) {
                        failedPolls++
                        AppLog.d("Main", "Poll failed ($failedPolls/10): ${e.message}")
                        binding.draftStatus.text =
                            "Connection hiccup — retrying… ($failedPolls/10)"
                        if (failedPolls >= 10) {
                            AppLog.e("Main", "Poll gave up after 10 failures")
                            showError(
                                "Lost connection while waiting. The draft is " +
                                "still running on the backend — try again " +
                                "and it will pick up.")
                            binding.draftStatus.text = ""
                            break
                        }
                        continue
                    }
                    failedPolls = 0
                    val progress = try {
                        withContext(Dispatchers.IO) {
                            client.getProgress(submitted.draftJobId)
                        }
                    } catch (_: IOException) {
                        null // Progress is nice-to-have; the job status decides.
                    }
                    if (progress != null) {
                        binding.draftStatus.text = buildString {
                            append(progress.stage)
                            append(" — ")
                            append(progress.pct)
                            append("%")
                            if (progress.detail.isNotEmpty()) {
                                append("\n")
                                append(progress.detail)
                            }
                        }
                    }
                    when (job.status) {
                        "done" -> {
                            AppLog.d("Main", "Draft done")
                            showDraftDone(
                                job.result, transcribe,
                                submitted.draftJobId, submitted.jobId,
                                resolved.title
                            )
                            break
                        }
                        "error" -> {
                            val detail = job.error.ifBlank { "no detail from backend" }
                            AppLog.e("Main", "Draft error from backend: $detail")
                            showError("The draft failed on the backend: $detail")
                            binding.draftStatus.text = ""
                            break
                        }
                        else -> {
                            // Still pending — keep polling.
                        }
                    }
                }
            } catch (e: YoutubeError) {
                AppLog.e("Main", "Failed: ${e.message}")
                showError(e.message ?: "Couldn't get that video.")
                binding.audioStatus.text = ""
                binding.draftStatus.text = ""
            } catch (e: ModalError) {
                AppLog.e("Main", "Draft failed: ${e.message}")
                showError(e.message ?: "Backend error.")
                binding.draftStatus.text = ""
            } catch (e: IOException) {
                AppLog.e("Main", "Failed: no connection (${e.message})")
                showError("Couldn't reach the backend — check the URL and your connection.")
                binding.draftStatus.text = ""
            } catch (e: Exception) {
                AppLog.e("Main", "Failed: ${e.message}")
                showError("Something went wrong: ${e.message}")
                binding.audioStatus.text = ""
                binding.draftStatus.text = ""
            } finally {
                setRunning(false)
            }
        }
    }

    /**
     * Draft finished.
     * - With lyrics: the alignment is ready — open the editor (Step 2)
     *   with this draft, where the user drags word timings and renders.
     * - Transcribe-only: no alignment yet. Put the transcript into the
     *   lyrics box for editing; the user fixes the text and taps Start
     *   (now "Run alignment") to align it. The follow-up /draft reuses
     *   the cached stems server-side, so separation is skipped.
     */
    private fun showDraftDone(
        result: JSONObject?,
        transcribe: Boolean,
        draftJobId: String,
        callId: String,
        songTitle: String
    ) {
        if (result == null) {
            AppLog.e("Main", "Draft done but the backend sent no result")
            showError("The backend finished but sent no result. Try again.")
            binding.draftStatus.text = ""
            return
        }
        if (transcribe) {
            // The backend's transcribe-only result carries the text under
            // "transcript" (not "lyrics" — that key only exists on a full
            // DraftResult). Read "transcript" first, "lyrics" as fallback.
            val transcript = result.optString("transcript")
                .ifBlank { result.optString("lyrics") }
            if (transcript.isBlank()) {
                AppLog.e("Main", "Transcribe done but no transcript text in result: ${result.keys().asSequence().toList()}")
                showError("Transcription came back empty — try again or paste lyrics manually.")
                binding.draftStatus.text = ""
                return
            }
            binding.lyricsInput.setText(transcript)
            binding.draftStatus.text = ""
            binding.draftResult.text = getString(R.string.transcript_ready_hint)
            binding.draftResult.visibility = View.VISIBLE
            binding.startButton.text = getString(R.string.run_alignment)
            AppLog.d("Main", "Transcript ready (${transcript.length} chars) — waiting for edits")
            return
        }
        val alignment = result.optJSONObject("alignment")
        val segments = alignment?.optJSONArray("segments")
        var lines = 0
        var words = 0
        if (segments != null) {
            lines = segments.length()
            for (i in 0 until segments.length()) {
                words += segments.optJSONObject(i)?.optJSONArray("words")?.length() ?: 0
            }
        }
        val duration = result.optDouble("duration", 0.0)
        AppLog.d("Main", "Draft ready: $lines lines, $words words, ${formatDuration(duration.toLong())} — opening editor")
        binding.draftStatus.text = ""
        binding.draftResult.text = getString(R.string.draft_ready_title) +
            " — $lines lines, $words words. Opening the editor…"
        binding.draftResult.visibility = View.VISIBLE
        binding.startButton.text = getString(R.string.start)
        startActivity(
            EditorActivity.newIntent(this, draftJobId, callId, songTitle)
        )
    }

    private fun onCancel() {
        flowJob?.cancel()
        flowJob = null
        val jobId = currentJobId
        if (jobId != null && SettingsStore.isConfigured(this)) {
            lifecycleScope.launch(Dispatchers.IO) {
                ModalClient(
                    SettingsStore.getBaseUrl(this@MainActivity),
                    SettingsStore.getApiKey(this@MainActivity)
                ).cancel(jobId)
            }
        }
        currentJobId = null
        setRunning(false)
        binding.audioStatus.text = "Cancelled."
        binding.draftStatus.text = ""
        AppLog.d("Main", "Cancelled by user")
    }

    private fun formatDuration(seconds: Long): String {
        if (seconds <= 0) return "0:00"
        val m = seconds / 60
        val s = seconds % 60
        return "%d:%02d".format(m, s)
    }

    private fun formatSize(bytes: Long): String {
        return when {
            bytes >= 1024 * 1024 -> "%.1f MB".format(bytes / (1024.0 * 1024.0))
            bytes >= 1024 -> "%d KB".format(bytes / 1024)
            else -> "$bytes B"
        }
    }
}
