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

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.startButton.setOnClickListener { onStartTapped() }
        binding.cancelButton.setOnClickListener { onCancel() }
        binding.settingsButton.setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
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
                // Step 1: resolve + download the audio.
                val resolved = YoutubeAudio.resolve(url)
                AppLog.d("Main", "Resolved: ${resolved.title} (${resolved.durationSec}s, ${resolved.bitrate / 1000} kbps, .${resolved.suffix})")
                binding.audioStatus.text = "Downloading: ${resolved.title}"
                binding.audioProgress.isIndeterminate = false
                val safeName = resolved.title
                    .replace(Regex("[^A-Za-z0-9._-]+"), "_")
                    .take(80)
                    .ifEmpty { "song" }
                val audioFile = File(
                    getExternalFilesDir(Environment.DIRECTORY_MUSIC),
                    "KaraokeGen/$safeName.${resolved.suffix}"
                )
                YoutubeAudio.download(resolved.audioUrl, audioFile) { done, total ->
                    runOnUiThread {
                        if (total > 0) {
                            val pct = (done * 100 / total).toInt()
                            binding.audioProgress.progress = pct
                            binding.audioStatus.text =
                                "Downloading: ${resolved.title} — $pct%"
                        }
                    }
                }
                AppLog.d("Main", "Audio ready: ${audioFile.length()} bytes -> ${audioFile.name}")
                binding.audioProgress.visibility = View.GONE
                val info = buildString {
                    append(resolved.title)
                    append("\n")
                    append(formatDuration(resolved.durationSec))
                    append(" • ")
                    append(formatSize(audioFile.length()))
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
                        audioFile,
                        language = "", // auto-detect
                        lyrics = if (transcribe) "" else lyrics,
                        genre = genre,
                        transcribeOnly = transcribe
                    )
                }
                currentJobId = submitted.jobId
                AppLog.d("Main", "Draft submitted: job=${submitted.jobId} draft=${submitted.draftJobId}")
                binding.draftStatus.text = "Queued — waiting for the GPU…"
                while (true) {
                    delay(3000)
                    val job = withContext(Dispatchers.IO) { client.getJob(submitted.jobId) }
                    val progress = withContext(Dispatchers.IO) {
                        client.getProgress(submitted.draftJobId)
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
                            showDraftDone(job.result, transcribe)
                            break
                        }
                        "error" -> {
                            AppLog.e("Main", "Draft error from backend")
                            showError("The draft failed on the backend. Try again.")
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

    private fun showDraftDone(result: JSONObject?, transcribe: Boolean) {
        if (result == null) {
            showError("The backend finished but sent no result. Try again.")
            binding.draftStatus.text = ""
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
        val lyrics = result.optString("lyrics")
        val summary = buildString {
            append(getString(R.string.draft_ready_title))
            append(" — ")
            append(lines)
            append(" lines, ")
            append(words)
            append(" words, ")
            append(formatDuration(duration.toLong()))
            append(".\n")
            append(getString(R.string.draft_next_trial))
            if (transcribe && lyrics.isNotEmpty()) {
                append("\n\nTranscript:\n")
                append(lyrics.take(500))
            }
        }
        binding.draftStatus.text = ""
        binding.draftResult.text = summary
        binding.draftResult.visibility = View.VISIBLE
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
