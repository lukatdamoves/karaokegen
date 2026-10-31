package com.lukatdamoves.karaokegen

import android.os.Bundle
import android.view.View
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.lukatdamoves.karaokegen.databinding.ActivitySettingsBinding
import kotlinx.coroutines.launch

/**
 * Backend settings: the Modal backend URL + API key (BYOK).
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

        binding.backendUrlInput.setText(SettingsStore.getBaseUrl(this))
        binding.apiKeyInput.setText(SettingsStore.getApiKey(this))

        binding.saveButton.setOnClickListener {
            val url = binding.backendUrlInput.text?.toString()?.trim().orEmpty()
            val key = binding.apiKeyInput.text?.toString()?.trim().orEmpty()
            if (url.isEmpty() || key.isEmpty()) {
                Toast.makeText(this, "Paste both the backend URL and the API key.", Toast.LENGTH_LONG).show()
                return@setOnClickListener
            }
            if (!url.startsWith("http://") && !url.startsWith("https://")) {
                Toast.makeText(this, "The backend URL must start with https://", Toast.LENGTH_LONG).show()
                return@setOnClickListener
            }
            SettingsStore.save(this, url, key)
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

    override fun onSupportNavigateUp(): Boolean {
        finish()
        return true
    }
}
