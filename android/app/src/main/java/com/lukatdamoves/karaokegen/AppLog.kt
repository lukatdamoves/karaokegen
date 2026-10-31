package com.lukatdamoves.karaokegen

import android.util.Log
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Tiny in-app log: writes to logcat AND keeps the last ~200 KB in memory,
 * so the "Copy logs" button can put it on the clipboard for debugging.
 * Nothing sensitive goes here — no API keys, no full URLs with tokens.
 */
object AppLog {
    private const val TAG = "KaraokeGen"
    private const val MAX_CHARS = 200_000
    private val lock = Any()
    private val buffer = StringBuilder()
    private val timeFormat = SimpleDateFormat("HH:mm:ss", Locale.US)

    fun d(tag: String, message: String) {
        Log.d(TAG, "[$tag] $message")
        append("D", tag, message)
    }

    fun e(tag: String, message: String) {
        Log.e(TAG, "[$tag] $message")
        append("E", tag, message)
    }

    private fun append(level: String, tag: String, message: String) {
        val line = "${timeFormat.format(Date())} $level [$tag] $message\n"
        synchronized(lock) {
            buffer.append(line)
            if (buffer.length > MAX_CHARS) {
                buffer.delete(0, buffer.length - MAX_CHARS)
            }
        }
    }

    /** Full buffered log, oldest first. */
    fun snapshot(): String = synchronized(lock) { buffer.toString() }
}
