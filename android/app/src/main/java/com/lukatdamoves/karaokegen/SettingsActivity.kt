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
                // Save the new backend. The URL + key live in SettingsStore;
                // there is no paste field anymore — setup is the only way in.
                SettingsStore.save(this@SettingsActivity, result.url, result.apiKey)
                binding.setupStatus.text = getString(R.string.setup_done)
                binding.setupProgress.visibility = View.GONE
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

    override fun onSupportNavigateUp(): Boolean {
        finish()
        return true
    }
}
