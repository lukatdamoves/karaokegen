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
}
