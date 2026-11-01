package com.lukatdamoves.karaokegen

import android.content.Context

/**
 * Trial storage for the Modal backend URL + API key.
 * Plain SharedPreferences for this trial (same as the desktop app's
 * config.json). A production app should use EncryptedSharedPreferences.
 */
object SettingsStore {
    private const val PREFS = "karaokegen_trial"
    private const val KEY_URL = "modal_url"
    private const val KEY_API = "modal_key"
    private const val KEY_SETUP_JOB = "setup_job_id"

    fun getBaseUrl(context: Context): String =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_URL, "")?.trim().orEmpty()

    fun getApiKey(context: Context): String =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_API, "")?.trim().orEmpty()

    fun save(context: Context, baseUrl: String, apiKey: String) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putString(KEY_URL, baseUrl.trim())
            .putString(KEY_API, apiKey.trim())
            .apply()
    }

    fun isConfigured(context: Context): Boolean =
        getBaseUrl(context).isNotEmpty() && getApiKey(context).isNotEmpty()

    /**
     * True when the saved backend address uses the old misspelling
     * ("karaokegen-api" — the app is really "karaokengen-api").
     * Those addresses never answer (Modal returns "invalid function
     * call"), so the phone must re-run setup to get the right address
     * + a fresh key. The correct spelling ("karaokengen-api") does
     * NOT contain this substring, so it is never flagged.
     */
    fun isStaleBackendUrl(url: String): Boolean =
        "--karaokegen-api" in url

    /** The auto-setup job id, saved so a setup survives leaving the
     *  screen (or the phone locking). The deploy keeps running on the
     *  helper even if the phone stops polling — coming back to
     *  Settings resumes watching the same job. */
    fun getSetupJob(context: Context): String =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_SETUP_JOB, "")?.trim().orEmpty()

    fun saveSetupJob(context: Context, jobId: String) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putString(KEY_SETUP_JOB, jobId.trim())
            .apply()
    }

    fun clearSetupJob(context: Context) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .remove(KEY_SETUP_JOB)
            .apply()
    }
}
