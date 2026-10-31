package com.lukatdamoves.karaokegen

import android.os.Bundle
import android.view.View
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.lukatdamoves.karaokegen.databinding.ActivitySettingsBinding
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import java.io.IOException

/**
 * Backend settings: paste ONE line with the Modal backend URL and the API
 * key, like the KaraokeNow ReVanced setup (one paste, no separate fields).
 * Accepts `URL KEY`, `URL|KEY`, or just `URL` (keeps the saved key).
 * Also a manual "Check for updates" — the same silent check that runs on
 * launch, but here it reports the result instead of staying silent.
 */
class SettingsActivity : AppCompatActivity() {

    private lateinit var binding: ActivitySettingsBinding
    private var setupJob: Job? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivitySettingsBinding.inflate(layoutInflater)
        setContentView(binding.root)
        supportActionBar?.setDisplayHomeAsUpEnabled(true)

        binding.setupButton.setOnClickListener { onSetupTapped() }

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

    // ------------------------------------------------------------------
    // Auto-setup (like the KaraokeNow ReVanced patch): paste the
    // `modal token set ...` command, and the setup helper deploys
    // YOUR OWN KaraokeGen backend into your Modal account. The token
    // is used once for that deploy and is never saved on this phone.
    // ------------------------------------------------------------------

    private fun onSetupTapped() {
        val text = binding.tokenInput.text?.toString().orEmpty()
        val pair = SetupClient.parseTokenCommand(text)
        if (pair == null) {
            Toast.makeText(this, R.string.token_bad, Toast.LENGTH_LONG).show()
            return
        }
        val (tokenId, tokenSecret) = pair
        binding.setupButton.isEnabled = false
        binding.setupProgress.visibility = View.VISIBLE
        binding.setupProgress.isIndeterminate = true
        binding.setupProgress.progress = 0
        binding.setupStatus.text = getString(R.string.setup_contacting)
        binding.setupStatus.visibility = View.VISIBLE
        AppLog.d("Settings", "Auto-setup started")
        setupJob = lifecycleScope.launch {
            try {
                val jobId = SetupClient.startSetup(tokenId, tokenSecret)
                AppLog.d("Settings", "Auto-setup job: $jobId")
                val result = SetupClient.pollToDone(jobId) { stage, progress ->
                    runOnUiThread {
                        binding.setupStatus.text = stage
                        if (progress >= 0) {
                            binding.setupProgress.isIndeterminate = false
                            binding.setupProgress.progress = progress
                        }
                    }
                }
                // Save the new backend and show it in the one-line field.
                SettingsStore.save(this@SettingsActivity, result.url, result.apiKey)
                binding.backendLineInput.setText("${result.url} ${result.apiKey}")
                binding.setupStatus.text = getString(R.string.setup_done)
                binding.setupProgress.visibility = View.GONE
                binding.savedText.visibility = View.VISIBLE
                AppLog.d("Settings", "Auto-setup done: ${result.url}")
                Toast.makeText(this@SettingsActivity, R.string.setup_done, Toast.LENGTH_LONG).show()
            } catch (e: SetupError) {
                AppLog.e("Settings", "Auto-setup failed: ${e.message}")
                binding.setupStatus.text = e.message ?: getString(R.string.setup_failed)
                binding.setupProgress.visibility = View.GONE
            } catch (e: IOException) {
                AppLog.e("Settings", "Auto-setup failed: no connection")
                binding.setupStatus.text = getString(R.string.setup_unreachable)
                binding.setupProgress.visibility = View.GONE
            } catch (e: Exception) {
                AppLog.e("Settings", "Auto-setup failed: ${e.message}")
                binding.setupStatus.text = getString(R.string.setup_failed)
                binding.setupProgress.visibility = View.GONE
            } finally {
                binding.setupButton.isEnabled = true
            }
        }
    }

    override fun onDestroy() {
        setupJob?.cancel()
        super.onDestroy()
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
