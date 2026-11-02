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
    private const val KEY_SV_ENABLED_LAST = "sv_enabled_last"
    private const val KEY_SV_ENABLED_PREFIX = "sv_enabled_"
    private const val KEY_SV_VOLUME_LAST = "sv_volume_last"
    private const val KEY_SV_VOLUME_PREFIX = "sv_volume_"

    /** "2nd voice" (backing vocals) for the render, matching the
     *  desktop editor: a checkbox (checked by default) + a volume
     *  slider 0-200 (default 100). Stored per draft id so going
     *  back to a job restores its choice, and the last choice is
     *  remembered as the default for a new draft. The render sends
     *  second_voice = enabled and second_voice_gain = volume / 100,
     *  exactly like the desktop's svVolumeGain(). */
    fun getSvEnabled(context: Context, draftId: String): Boolean {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        if (draftId.isNotEmpty() && prefs.contains(KEY_SV_ENABLED_PREFIX + draftId)) {
            return prefs.getBoolean(KEY_SV_ENABLED_PREFIX + draftId, true)
        }
        return prefs.getBoolean(KEY_SV_ENABLED_LAST, true)
    }

    fun saveSvEnabled(context: Context, draftId: String, enabled: Boolean) {
        val edit = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().putBoolean(KEY_SV_ENABLED_LAST, enabled)
        if (draftId.isNotEmpty()) edit.putBoolean(KEY_SV_ENABLED_PREFIX + draftId, enabled)
        edit.apply()
    }

    fun getSvVolume(context: Context, draftId: String): Int {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        if (draftId.isNotEmpty() && prefs.contains(KEY_SV_VOLUME_PREFIX + draftId)) {
            return prefs.getInt(KEY_SV_VOLUME_PREFIX + draftId, 100).coerceIn(0, 200)
        }
        return prefs.getInt(KEY_SV_VOLUME_LAST, 100).coerceIn(0, 200)
    }

    fun saveSvVolume(context: Context, draftId: String, volume: Int) {
        val v = volume.coerceIn(0, 200)
        val edit = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().putInt(KEY_SV_VOLUME_LAST, v)
        if (draftId.isNotEmpty()) edit.putInt(KEY_SV_VOLUME_PREFIX + draftId, v)
        edit.apply()
    }

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
