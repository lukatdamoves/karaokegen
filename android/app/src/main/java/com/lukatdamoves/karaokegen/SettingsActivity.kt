package com.lukatdamoves.karaokegen

import android.os.Bundle
import android.view.View
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.lukatdamoves.karaokegen.databinding.ActivitySettingsBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Backend settings: paste ONE line with the Modal backend URL and the API
 * key, like the KaraokeNow ReVanced setup (one paste, no separate fields).
 * Accepts `URL KEY`, `URL|KEY`, or just `URL` (keeps the saved key).
 * Also a manual "Check for updates" — the same silent check that runs on
 * launch, but here it reports the result instead of staying silent.
 */
class SettingsActivity : AppCompatActivity() {

    private lateinit var binding: ActivitySettingsBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivitySettingsBinding.inflate(layoutInflater)
        setContentView(binding.root)
        supportActionBar?.setDisplayHomeAsUpEnabled(true)

        // Show the saved backend as one line, so it can be copied/edited.
        val savedUrl = SettingsStore.getBaseUrl(this)
        val savedKey = SettingsStore.getApiKey(this)
        binding.backendLineInput.setText(
            when {
                savedUrl.isEmpty() -> ""
                savedKey.isEmpty() -> savedUrl
                else -> "$savedUrl $savedKey"
            }
        )

        binding.generateUrlButton.setOnClickListener { onGenerateUrl() }

        binding.saveButton.setOnClickListener {
            val line = binding.backendLineInput.text?.toString()?.trim().orEmpty()
            val parsed = parseBackendLine(line)
            if (parsed == null) {
                Toast.makeText(this, R.string.backend_line_bad, Toast.LENGTH_LONG).show()
                return@setOnClickListener
            }
            val (url, key) = parsed
            if (!url.startsWith("http://") && !url.startsWith("https://")) {
                Toast.makeText(this, R.string.backend_url_bad, Toast.LENGTH_LONG).show()
                return@setOnClickListener
            }
            // A pasted URL on its own keeps the key already saved.
            val finalKey = key.ifEmpty { SettingsStore.getApiKey(this) }
            if (finalKey.isEmpty()) {
                Toast.makeText(this, R.string.backend_key_missing, Toast.LENGTH_LONG).show()
                return@setOnClickListener
            }
            SettingsStore.save(this, url, finalKey)
            AppLog.d("Settings", "Backend saved: $url (key ${finalKey.length} chars)")
            binding.savedText.visibility = View.VISIBLE
        }

        binding.checkUpdatesButton.setOnClickListener {
            binding.updateCheckResult.text = getString(R.string.checking_updates)
            lifecycleScope.launch {
                val update = UpdateManager.checkForUpdate(this@SettingsActivity)
                binding.updateCheckResult.text = when {
                    update != null ->
                        "Update available: v${update.versionName} — go back to the main screen and tap Update."
                    else -> getString(R.string.up_to_date)
                }
            }
        }
    }

    // ------------------------------------------------------------------
    // URL helper: build the Modal URL from the workspace name, fill it
    // in, and check that it answers (a 401/503 means it IS a KaraokeGen
    // backend — the backend requires the API key, so no key = 401).
    // The URL shape is the same one the desktop app builds after deploy:
    // https://<workspace>--karaokegen-api.modal.run
    // ------------------------------------------------------------------

    private val checkHttp = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(10, TimeUnit.SECONDS)
        .build()

    private fun onGenerateUrl() {
        val raw = binding.workspaceInput.text?.toString()?.trim().orEmpty()
        if (raw.isEmpty()) {
            Toast.makeText(this, R.string.workspace_empty, Toast.LENGTH_SHORT).show()
            return
        }
        val workspace = normalizeWorkspace(raw)
        if (workspace.isEmpty() || !workspace.matches(Regex("[a-z0-9][a-z0-9_-]*"))) {
            Toast.makeText(this, R.string.workspace_bad, Toast.LENGTH_LONG).show()
            return
        }
        val url = "https://$workspace--karaokegen-api.modal.run"
        // Keep the API key already in the one-line field, if any.
        val current = parseBackendLine(
            binding.backendLineInput.text?.toString()?.trim().orEmpty()
        )
        val key = current?.second.orEmpty()
        binding.backendLineInput.setText(if (key.isEmpty()) url else "$url $key")
        binding.generateResult.text = getString(R.string.generate_checking)
        binding.generateResult.visibility = View.VISIBLE
        AppLog.d("Settings", "Generated URL: $url")
        lifecycleScope.launch {
            val code = withContext(Dispatchers.IO) { probeUrl(url) }
            binding.generateResult.text = when (code) {
                401 -> getString(R.string.generate_ok)
                503 -> getString(R.string.generate_no_key)
                -1 -> getString(R.string.generate_unreachable)
                else -> getString(R.string.generate_answered, code)
            }
            if (code == 401 || code == 503) {
                AppLog.d("Settings", "URL check OK (HTTP $code)")
            } else {
                AppLog.d("Settings", "URL check: HTTP $code")
            }
        }
    }

    /** Accept a bare workspace name, or a full pasted Modal URL — pull the
     *  workspace out of either. Returns lowercase. */
    private fun normalizeWorkspace(raw: String): String {
        var s = raw.lowercase().trim()
        if (s.startsWith("http://")) s = s.removePrefix("http://")
        if (s.startsWith("https://")) s = s.removePrefix("https://")
        val marker = "--karaokegen-api.modal.run"
        if (marker in s) s = s.substringBefore(marker)
        s = s.substringBefore("/").substringBefore(".")
        return s.trim()
    }

    /** GET the URL with no auth. Returns the HTTP code, or -1 if it can't
     *  be reached at all (wrong workspace / no network). */
    private fun probeUrl(url: String): Int {
        return try {
            val req = Request.Builder().url(url).get().build()
            checkHttp.newCall(req).execute().use { resp -> resp.code }
        } catch (e: IOException) {
            -1
        }
    }
    private fun parseBackendLine(line: String): Pair<String, String>? {
        if (line.isEmpty()) return null
        if ("|" in line) {
            val parts = line.split("|", limit = 2)
            val url = parts[0].trim()
            val key = parts.getOrNull(1)?.trim().orEmpty()
            return if (url.isEmpty()) null else url to key
        }
        val parts = line.split(Regex("\\s+")).filter { it.isNotEmpty() }
        if (parts.isEmpty()) return null
        val url = parts[0]
        val key = if (parts.size > 1) parts[1] else ""
        return url to key
    }

    override fun onSupportNavigateUp(): Boolean {
        finish()
        return true
    }
}
