package com.lukatdamoves.karaokegen

import android.os.Bundle
import android.view.View
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.lukatdamoves.karaokegen.databinding.ActivitySettingsBinding
import kotlinx.coroutines.CancellationException
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

        // A setup that was already running when this screen was left
        // (or the phone locked) keeps going on the helper — resume
        // watching it instead of starting over.
        val pendingJob = SettingsStore.getSetupJob(this)
        if (pendingJob.isNotEmpty()) {
            AppLog.d("Settings", "Resuming auto-setup job: $pendingJob")
            showSetupWatching()
            setupJob = lifecycleScope.launch { runSetupWatch(pendingJob) }
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
        // If a setup is already running, just watch that one — don't
        // start a second deploy.
        val pendingJob = SettingsStore.getSetupJob(this)
        if (pendingJob.isNotEmpty()) {
            AppLog.d("Settings", "Setup already running, watching: $pendingJob")
            showSetupWatching()
            setupJob = lifecycleScope.launch { runSetupWatch(pendingJob) }
            return
        }
        val text = binding.tokenInput.text?.toString().orEmpty()
        val pair = SetupClient.parseTokenCommand(text)
        if (pair == null) {
            Toast.makeText(this, R.string.token_bad, Toast.LENGTH_LONG).show()
            return
        }
        val (tokenId, tokenSecret) = pair
        showSetupWatching()
        AppLog.d("Settings", "Auto-setup started")
        setupJob = lifecycleScope.launch {
            try {
                val jobId = SetupClient.startSetup(tokenId, tokenSecret)
                AppLog.d("Settings", "Auto-setup job: $jobId")
                SettingsStore.saveSetupJob(this@SettingsActivity, jobId)
                runSetupWatch(jobId)
            } catch (e: SetupError) {
                AppLog.e("Settings", "Auto-setup failed: ${e.message}")
                binding.setupStatus.text = e.message ?: getString(R.string.setup_failed)
                binding.setupProgress.visibility = View.GONE
                binding.setupButton.isEnabled = true
            } catch (e: IOException) {
                AppLog.e("Settings", "Auto-setup failed: no connection")
                binding.setupStatus.text = getString(R.string.setup_unreachable)
                binding.setupProgress.visibility = View.GONE
                binding.setupButton.isEnabled = true
            } catch (e: CancellationException) {
                // Leaving the screen (or the phone locking) stops the
                // watching, not the deploy — it keeps going on the
                // helper and resumes when this screen is opened again.
                // Don't show it as a failure.
                AppLog.d("Settings", "Auto-setup watching stopped (deploy continues)")
                throw e
            } catch (e: Exception) {
                AppLog.e("Settings", "Auto-setup failed: ${e.message}")
                binding.setupStatus.text = getString(R.string.setup_failed)
                binding.setupProgress.visibility = View.GONE
                binding.setupButton.isEnabled = true
            }
        }
    }

    /** Show the setup progress UI (button off, spinner + status on). */
    private fun showSetupWatching() {
        binding.setupButton.isEnabled = false
        binding.setupProgress.visibility = View.VISIBLE
        binding.setupProgress.isIndeterminate = true
        binding.setupProgress.progress = 0
        binding.setupStatus.text = getString(R.string.setup_contacting)
        binding.setupStatus.visibility = View.VISIBLE
    }

    /** Watch one setup job to completion (suspend — call from a
     *  coroutine). The job id is saved, so leaving this screen and
     *  coming back resumes the same job. */
    private suspend fun runSetupWatch(jobId: String) {
        try {
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
            SettingsStore.clearSetupJob(this@SettingsActivity)
            binding.setupStatus.text = getString(R.string.setup_done)
            binding.setupProgress.visibility = View.GONE
            AppLog.d("Settings", "Auto-setup done: ${result.url}")
            Toast.makeText(this@SettingsActivity, R.string.setup_done, Toast.LENGTH_LONG).show()
        } catch (e: SetupError) {
            SettingsStore.clearSetupJob(this@SettingsActivity)
            AppLog.e("Settings", "Auto-setup failed: ${e.message}")
            binding.setupStatus.text = e.message ?: getString(R.string.setup_failed)
            binding.setupProgress.visibility = View.GONE
        } catch (e: IOException) {
            AppLog.e("Settings", "Auto-setup failed: no connection")
            binding.setupStatus.text = getString(R.string.setup_unreachable)
            binding.setupProgress.visibility = View.GONE
        } catch (e: CancellationException) {
            // Leaving the screen (or the phone locking) stops the
            // watching, not the deploy — it keeps going on the
            // helper and resumes when this screen is opened again.
            // Don't show it as a failure.
            AppLog.d("Settings", "Auto-setup watching stopped (deploy continues)")
            throw e
        } catch (e: Exception) {
            SettingsStore.clearSetupJob(this@SettingsActivity)
            AppLog.e("Settings", "Auto-setup failed: ${e.message}")
            binding.setupStatus.text = getString(R.string.setup_failed)
            binding.setupProgress.visibility = View.GONE
        } finally {
            binding.setupButton.isEnabled = true
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
