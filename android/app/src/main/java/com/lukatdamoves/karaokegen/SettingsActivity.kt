package com.lukatdamoves.karaokegen

import android.os.Bundle
import android.view.View
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.lukatdamoves.karaokegen.databinding.ActivitySettingsBinding
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
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
    // In-memory guard against double-taps: set synchronously the
    // moment setup is tapped, before any network call. The saved job
    // id covers the long term, but this covers the window between
    // the tap and the job id being saved (a second tap in that
    // window would otherwise start a second deploy).
    private var setupInProgress = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivitySettingsBinding.inflate(layoutInflater)
        setContentView(binding.root)
        supportActionBar?.setDisplayHomeAsUpEnabled(true)

        binding.setupButton.setOnClickListener { onSetupTapped() }

        // Show whether a backend is already set up (and which one),
        // so it is obvious at a glance instead of a blank form.
        refreshBackendStatus()

        // A setup that was already running when this screen was left
        // (or the phone locked) keeps going on the helper — resume
        // watching it instead of starting over.
        val pendingJob = SettingsStore.getSetupJob(this)
        if (pendingJob.isNotEmpty()) {
            AppLog.d("Settings", "Resuming auto-setup job: $pendingJob")
            setupInProgress = true
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

    /** Show the saved backend (if any) in a big obvious card at the
     *  top of this screen — green when ready, amber when the saved
     *  address is the old misspelling and needs reconnecting. */
    private fun refreshBackendStatus() {
        val saved = SettingsStore.getBaseUrl(this)
        when {
            saved.isEmpty() -> {
                binding.backendCard.setCardBackgroundColor(
                    getColor(R.color.kg_panel))
                binding.backendCardTitle.text =
                    getString(R.string.backend_none_title)
                binding.backendCardText.text =
                    getString(R.string.backend_none_text)
            }
            SettingsStore.isStaleBackendUrl(saved) -> {
                binding.backendCard.setCardBackgroundColor(
                    getColor(R.color.kg_warn_bg))
                binding.backendCardTitle.text =
                    getString(R.string.backend_stale_title)
                binding.backendCardText.text =
                    getString(R.string.backend_stale_text)
            }
            else -> {
                binding.backendCard.setCardBackgroundColor(
                    getColor(R.color.kg_ready_bg))
                binding.backendCardTitle.text =
                    getString(R.string.backend_ready_title)
                binding.backendCardText.text = saved
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
        // Double-tap guard: ignore the tap entirely if a setup is
        // already starting or running. The button is disabled first
        // thing, so a second tap can't even reach here — this flag is
        // the backstop for taps that land in the same frame.
        if (setupInProgress) {
            AppLog.d("Settings", "Setup tap ignored — already running")
            return
        }
        setupInProgress = true
        binding.setupButton.isEnabled = false
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
            setupInProgress = false
            binding.setupButton.isEnabled = true
            return
        }
        val (tokenId, tokenSecret) = pair
        showSetupWatching()
        AppLog.d("Settings", "Auto-setup started")
        setupJob = lifecycleScope.launch {
            try {
                // The job id MUST be saved even if the user leaves
                // this screen while startSetup is still running
                // (cold helper = slow first call). Without this, the
                // job id is lost, the deploy is orphaned, and coming
                // back shows a plain settings screen with no loading
                // — because onCreate has no saved job to resume.
                // NonCancellable lets the start + save finish; the
                // watching after it is still cancelled normally, and
                // onCreate resumes it from the saved job id.
                val jobId = withContext(NonCancellable) {
                    val id = SetupClient.startSetup(tokenId, tokenSecret)
                    SettingsStore.saveSetupJob(
                        this@SettingsActivity, id)
                    id
                }
                AppLog.d("Settings", "Auto-setup job: $jobId")
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
            } finally {
                // Let the next tap through once this attempt is over.
                // runSetupWatch also resets it; doing it here covers
                // the case where startSetup itself failed.
                setupInProgress = false
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
            refreshBackendStatus()
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
            setupInProgress = false
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
