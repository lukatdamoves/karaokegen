package com.lukatdamoves.karaokegen

import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.content.FileProvider
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Over-the-air updates from the repo's GitHub Releases.
 *
 * How it works: the app checks the releases of lukatdamoves/karaokegen for
 * the newest release carrying an APK asset named
 * `karaokegen-<version>-vc<versionCode>.apk` (written by the release
 * workflow). If that versionCode is higher than the installed one, the app
 * offers the update, downloads it in-app, and hands it to the system
 * installer — which updates the app in place and keeps all data. The APK
 * must be signed with the SAME release keystore every time, or Android
 * refuses the update (see android/README.md).
 *
 * Every failure here is silent (returns null): no release yet, no network,
 * no APK asset, or a bad response all mean "no update" — the app just
 * works normally and says nothing.
 */
object UpdateManager {
    private const val RELEASES_URL =
        "https://api.github.com/repos/lukatdamoves/karaokegen/releases"
    private const val APK_SUFFIX = ".apk"
    private val VC_REGEX = Regex("""vc(\d+)\.apk$""")

    private val http = OkHttpClient.Builder()
        .followRedirects(true)
        .followSslRedirects(true)
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    data class UpdateInfo(
        val versionName: String,
        val versionCode: Long,
        val downloadUrl: String,
        val tag: String
    )

    fun currentVersionCode(context: Context): Long {
        val info = packageInfo(context)
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            info.longVersionCode
        } else {
            @Suppress("DEPRECATION")
            info.versionCode.toLong()
        }
    }

    fun currentVersionName(context: Context): String =
        packageInfo(context).versionName ?: ""

    private fun packageInfo(context: Context) =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            context.packageManager.getPackageInfo(
                context.packageName,
                PackageManager.PackageInfoFlags.of(0)
            )
        } else {
            @Suppress("DEPRECATION")
            context.packageManager.getPackageInfo(context.packageName, 0)
        }

    /**
     * Find a newer APK release, or null. Releases are newest-first: the
     * first APK found decides — if it is not newer than the installed app,
     * we are up to date; releases without an APK are skipped (desktop-only
     * releases carry just the .exe).
     */
    suspend fun checkForUpdate(context: Context): UpdateInfo? =
        withContext(Dispatchers.IO) {
            try {
                val req = Request.Builder()
                    .url(RELEASES_URL)
                    .header("Accept", "application/vnd.github+json")
                    .header("User-Agent", "KaraokeGen-Android")
                    .build()
                http.newCall(req).execute().use { resp ->
                    if (!resp.isSuccessful) return@withContext null
                    val releases = JSONArray(resp.body?.string() ?: return@withContext null)
                    val currentCode = currentVersionCode(context)
                    val currentName = currentVersionName(context)
                    for (i in 0 until releases.length()) {
                        val rel = releases.getJSONObject(i)
                        if (rel.optBoolean("draft")) continue
                        val assets = rel.optJSONArray("assets") ?: continue
                        for (j in 0 until assets.length()) {
                            val asset = assets.getJSONObject(j)
                            val name = asset.optString("name")
                            if (!name.endsWith(APK_SUFFIX)) continue
                            val url = asset.optString("browser_download_url")
                            if (url.isEmpty()) continue
                            val vc = VC_REGEX.find(name)?.groupValues?.get(1)?.toLongOrNull()
                            val vn = versionNameFromAsset(name)
                                ?: rel.optString("tag_name").removePrefix("v")
                            val isNewer = if (vc != null) {
                                vc > currentCode
                            } else {
                                vn.isNotEmpty() && vn != currentName
                            }
                            return@withContext if (isNewer) {
                                UpdateInfo(vn, vc ?: -1, url, rel.optString("tag_name"))
                            } else {
                                null
                            }
                        }
                        // No APK in this release — keep looking at older ones.
                    }
                    null
                }
            } catch (e: Exception) {
                null
            }
        }

    private fun versionNameFromAsset(assetName: String): String? {
        // karaokegen-0.1.0-trial-vc1.apk -> 0.1.0-trial
        val noExt = assetName.removeSuffix(APK_SUFFIX)
        if (!noExt.startsWith("karaokegen-")) return null
        val body = noExt.removePrefix("karaokegen-")
        val vcIndex = body.lastIndexOf("-vc")
        if (vcIndex <= 0) return null
        return body.substring(0, vcIndex).ifEmpty { null }
    }

    /** Download the update APK into app storage, reporting (downloaded, total). */
    @Throws(IOException::class)
    suspend fun downloadUpdate(
        context: Context,
        info: UpdateInfo,
        onProgress: (downloaded: Long, total: Long) -> Unit
    ): File = withContext(Dispatchers.IO) {
        val dir = File(context.getExternalFilesDir(null), "updates").apply { mkdirs() }
        val dest = File(dir, "karaokegen-update.apk")
        val req = Request.Builder()
            .url(info.downloadUrl)
            .header("User-Agent", "KaraokeGen-Android")
            .build()
        http.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) {
                throw IOException("Update download failed (HTTP ${resp.code}).")
            }
            val body = resp.body ?: throw IOException("Update download failed (empty response).")
            val total = body.contentLength()
            var downloaded = 0L
            dest.outputStream().use { out ->
                body.byteStream().use { input ->
                    val buf = ByteArray(64 * 1024)
                    while (true) {
                        val n = input.read(buf)
                        if (n == -1) break
                        out.write(buf, 0, n)
                        downloaded += n
                        onProgress(downloaded, total)
                    }
                }
            }
        }
        dest
    }

    /** True if the system will let this app install APKs (Android 8+ gate). */
    fun canInstall(context: Context): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return true
        return context.packageManager.canRequestPackageInstalls()
    }

    /** Opens the system screen where the user allows this app to install APKs. */
    fun openInstallPermissionSettings(context: Context) {
        val intent = Intent(
            Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
            Uri.parse("package:${context.packageName}")
        ).apply { addFlags(Intent.FLAG_ACTIVITY_NEW_TASK) }
        context.startActivity(intent)
    }

    /** Hand the APK to the system installer (in-place update, data kept). */
    fun installUpdate(context: Context, apkFile: File) {
        val uri = FileProvider.getUriForFile(
            context,
            "${context.packageName}.fileprovider",
            apkFile
        )
        val intent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, "application/vnd.android.package-archive")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        context.startActivity(intent)
    }
}
