package com.lukatdamoves.karaokegen

import android.os.Bundle
import android.view.View
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.lukatdamoves.karaokegen.databinding.ActivitySettingsBinding
import kotlinx.coroutines.launch

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

    /**
     * Split one pasted line into (url, key). Accepts `URL|KEY`,
     * `URL KEY`, or a bare `URL` (key empty — the saved key is kept).
     * Returns null when there is no URL at all.
     */
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
