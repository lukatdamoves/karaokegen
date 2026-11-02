package com.lukatdamoves.karaokegen

import android.app.DownloadManager
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.graphics.Typeface
import android.media.MediaPlayer
import android.net.Uri
import android.os.Bundle
import android.os.Environment
import android.util.TypedValue
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import android.webkit.URLUtil
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.LinearLayout
import android.widget.SeekBar
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.ActionBarDrawerToggle
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.GravityCompat
import androidx.lifecycle.lifecycleScope
import com.lukatdamoves.karaokegen.databinding.ActivityMainBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.text.SimpleDateFormat
import java.util.Locale

/**
 * KaraokeGen trial, one page: paste a YouTube link -> one Start button
 * does the whole thing (download the audio, then draft it on the Modal
 * backend — with lyrics, or transcribe when the lyrics box is empty).
 *
 * The page has three views, switched by visibility: home (the form),
 * editor (the desktop editor in a WebView, served from the in-app
 * localhost server) and jobs (the job queue). The sidebar holds Home,
 * Editor, Jobs, Backend settings and Copy logs. Update check runs
 * silently on launch.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding

    private var flowJob: Job? = null
    private var currentJobId: String? = null
    private var pendingUpdate: UpdateManager.UpdateInfo? = null
    // Last downloaded audio, so "Run alignment" (the follow-up /draft
    // after a transcript) reuses the file already on disk instead of
    // downloading the same song again. Same URL + file still present.
    private var cachedAudioUrl: String? = null
    private var cachedResolved: YoutubeAudio.ResolvedAudio? = null
    private var cachedSendFile: File? = null

    // Editor (inline WebView) state.
    private var fileChooserCallback: ValueCallback<Array<Uri>>? = null
    private var webViewSetupDone = false
    // Draft id currently loaded in the WebView (null = nothing loaded
    // yet). A repeat open of the same draft does NOT reload the page,
    // so in-progress edits in the editor survive switching views.
    private var loadedEditorDraft: String? = null
    // Last draft shown in the editor, for the sidebar Editor item.
    private var lastDraftJobId = ""
    private var lastCallId = ""
    private var lastSongTitle = ""

    // Audio player (listen while fixing the lyrics).
    private var mediaPlayer: MediaPlayer? = null
    private var playerFile: File? = null
    private var playerTicker: Job? = null
    private var playerSeeking = false

    private val fileChooserLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        val cb = fileChooserCallback
        fileChooserCallback = null
        if (result.resultCode == RESULT_OK) {
            val data = result.data
            val uris = mutableListOf<Uri>()
            data?.data?.let { uris.add(it) }
            data?.clipData?.let { clip ->
                for (i in 0 until clip.itemCount) {
                    clip.getItemAt(i).uri?.let { uris.add(it) }
                }
            }
            cb?.onReceiveValue(uris.toTypedArray())
        } else {
            cb?.onReceiveValue(null)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        // Sidebar (hamburger in the toolbar).
        setSupportActionBar(binding.toolbar)
        val drawerToggle = ActionBarDrawerToggle(
            this, binding.drawerLayout, binding.toolbar,
            R.string.drawer_open, R.string.drawer_close
        )
        binding.drawerLayout.addDrawerListener(drawerToggle)
        drawerToggle.syncState()
        binding.navView.setNavigationItemSelectedListener { item ->
            binding.drawerLayout.closeDrawer(GravityCompat.START)
            when (item.itemId) {
                R.id.nav_home -> {
                    showHome()
                    true
                }
                R.id.nav_editor -> {
                    openEditorFromDrawer()
                    true
                }
                R.id.nav_jobs -> {
                    showJobs()
                    true
                }
                R.id.nav_settings -> {
                    startActivity(Intent(this, SettingsActivity::class.java))
                    true
                }
                R.id.nav_copy_logs -> {
                    onCopyLogs()
                    true
                }
                else -> false
            }
        }
        binding.navView.setCheckedItem(R.id.nav_home)

        // Lyrics box touch scroll: the lyrics EditText sits inside the
        // home ScrollView, which steals the drag gesture, so the box
        // could not be scrolled by touch. While the finger is down on
        // the box, tell the parent chain not to intercept, so the
        // EditText scrolls internally; release on lift/cancel.
        binding.lyricsInput.setOnTouchListener { v, event ->
            when (event.action) {
                MotionEvent.ACTION_DOWN, MotionEvent.ACTION_MOVE ->
                    v.parent.requestDisallowInterceptTouchEvent(true)
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL ->
                    v.parent.requestDisallowInterceptTouchEvent(false)
            }
            // False: the EditText still handles the touch itself
            // (cursor placement, internal scrolling).
            false
        }

        binding.startButton.setOnClickListener { onStartTapped() }
        binding.cancelButton.setOnClickListener { onCancel() }
        binding.editorBackButton.setOnClickListener { showHome() }
        binding.playerPlayButton.setOnClickListener { onPlayerPlayPause() }
        binding.playerSeekBar.setOnSeekBarChangeListener(
            object : SeekBar.OnSeekBarChangeListener {
                override fun onProgressChanged(
                    seekBar: SeekBar?, progress: Int, fromUser: Boolean
                ) {
                    if (!fromUser) return
                    mediaPlayer?.seekTo(progress)
                    val dur = mediaPlayer?.duration ?: 0
                    binding.playerTimeLabel.text =
                        "${formatDuration((progress / 1000).toLong())} / " +
                        formatDuration((dur / 1000).toLong())
                }

                override fun onStartTrackingTouch(seekBar: SeekBar?) {
                    playerSeeking = true
                }

                override fun onStopTrackingTouch(seekBar: SeekBar?) {
                    playerSeeking = false
                }
            }
        )
        binding.updateButton.setOnClickListener { onUpdateTapped() }
        binding.updateLaterButton.setOnClickListener {
            binding.updateCard.visibility = View.GONE
            pendingUpdate = null
        }

        // Back: close the drawer first; in the editor, go back in the
        // WebView first; from the editor or jobs, return home.
        // (Object form, not the lambda overload — that lives in
        // activity-ktx, which this app does not depend on.)
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                when {
                    binding.drawerLayout.isDrawerOpen(GravityCompat.START) ->
                        binding.drawerLayout.closeDrawer(GravityCompat.START)
                    binding.editorView.visibility == View.VISIBLE &&
                        binding.editorWebView.canGoBack() ->
                        binding.editorWebView.goBack()
                    binding.editorView.visibility == View.VISIBLE ||
                        binding.jobsView.visibility == View.VISIBLE ->
                        showHome()
                    else -> {
                        isEnabled = false
                        onBackPressedDispatcher.onBackPressed()
                    }
                }
            }
        })

        checkForUpdateSilently()
    }

    override fun onDestroy() {
        flowJob?.cancel()
        releasePlayer()
        fileChooserCallback?.onReceiveValue(null)
        fileChooserCallback = null
        if (isFinishing && webViewSetupDone) {
            // The server is a singleton; stopping it here frees the
            // port when the app is really closed (rotation keeps it —
            // the manifest holds configChanges for this activity).
            (binding.editorWebView.parent as? ViewGroup)?.removeView(binding.editorWebView)
            try { binding.editorWebView.destroy() } catch (_: Exception) {}
            LocalApiServer.stopServer()
        }
        super.onDestroy()
    }

    // ------------------------------------------------------------------
    // Views: home / editor / jobs (one Activity, toggled by visibility).
    // ------------------------------------------------------------------

    private fun showHome() {
        binding.editorView.visibility = View.GONE
        binding.jobsView.visibility = View.GONE
        binding.homeView.visibility = View.VISIBLE
        binding.toolbar.title = getString(R.string.app_name)
        binding.navView.setCheckedItem(R.id.nav_home)
    }

    private fun showJobs() {
        binding.homeView.visibility = View.GONE
        binding.editorView.visibility = View.GONE
        binding.jobsView.visibility = View.VISIBLE
        binding.toolbar.title = getString(R.string.jobs_title)
        binding.navView.setCheckedItem(R.id.nav_jobs)
        rebuildJobsList()
    }

    /** Sidebar Editor: resume the last draft if there is one, else
     *  the editor's own draft form. */
    private fun openEditorFromDrawer() {
        if (lastDraftJobId.isNotEmpty()) {
            showEditor(lastDraftJobId, lastCallId, lastSongTitle)
            return
        }
        val latest = JobStore.getLatestDone(this)
        if (latest != null) {
            showEditor(latest.draftJobId, latest.callId, latest.title)
        } else {
            showEditor("", "", "")
        }
    }

    /** Show the editor view and load the draft (?draft= draft id,
     *  ?call= Modal call id, ?name= song title). The server seeds those
     *  into localStorage before editor.js runs, so the editor's own
     *  resume logic picks the draft up and shows the waveform + word
     *  regions. Re-opening the same draft does not reload the page. */
    private fun showEditor(draftJobId: String, callId: String, songTitle: String) {
        binding.homeView.visibility = View.GONE
        binding.jobsView.visibility = View.GONE
        binding.editorView.visibility = View.VISIBLE
        // Cap the title: a long YouTube title ran under the status-bar
        // icons. ~40 chars with an ellipsis.
        val title = songTitle.ifEmpty { getString(R.string.nav_editor) }
        binding.toolbar.title =
            if (title.length > 40) title.take(37) + "…" else title
        binding.navView.setCheckedItem(R.id.nav_editor)
        lastDraftJobId = draftJobId
        lastCallId = callId
        lastSongTitle = songTitle
        if (!webViewSetupDone) {
            setupEditorWebView()
            webViewSetupDone = true
        }
        if (draftJobId != loadedEditorDraft) {
            loadEditor(draftJobId, callId, songTitle)
            loadedEditorDraft = draftJobId
        }
    }

    private fun loadEditor(draftJobId: String, callId: String, songTitle: String) {
        binding.editorLoading.visibility = View.VISIBLE
        val baseUrl = LocalApiServer.start(this)
        val editorUrl = Uri.parse(baseUrl).buildUpon().apply {
            if (draftJobId.isNotEmpty()) appendQueryParameter("draft", draftJobId)
            if (callId.isNotEmpty()) appendQueryParameter("call", callId)
            if (songTitle.isNotEmpty()) appendQueryParameter("name", songTitle)
        }.build().toString()
        AppLog.d("Main", "loading editor: $baseUrl (draft=$draftJobId)")
        binding.editorWebView.loadUrl(editorUrl)
    }

    /** The editor WebView, moved here from the old separate editor
     *  screen (deleted in v0.2.6 — the editor now lives on this page):
     *  links stay in-app, external links open in the browser, the
     *  file chooser is bridged to the document picker, and MP4
     *  downloads go to DownloadManager in Downloads. */
    private fun setupEditorWebView() {
        val webView = binding.editorWebView
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true // localStorage: job list, alignment edits
            databaseEnabled = true // IndexedDB: cached audio blobs
            allowFileAccess = true
            allowContentAccess = true
            mediaPlaybackRequiresUserGesture = false
            cacheMode = WebSettings.LOAD_DEFAULT
        }
        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(
                view: WebView, request: WebResourceRequest
            ): Boolean {
                // Keep every link inside the editor (external links open
                // in the browser instead).
                val url = request.url.toString()
                return if (url.startsWith("http://127.0.0.1")) {
                    false
                } else {
                    try {
                        startActivity(Intent(Intent.ACTION_VIEW, request.url))
                    } catch (_: Exception) {
                    }
                    true
                }
            }

            override fun onPageFinished(view: WebView, url: String) {
                binding.editorLoading.visibility = View.GONE
            }
        }
        webView.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(
                view: WebView,
                filePathCallback: ValueCallback<Array<Uri>>,
                fileChooserParams: FileChooserParams
            ): Boolean {
                fileChooserCallback?.onReceiveValue(null)
                fileChooserCallback = filePathCallback
                val intent = Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
                    addCategory(Intent.CATEGORY_OPENABLE)
                    type = "*/*"
                    putExtra(Intent.EXTRA_MIME_TYPES, arrayOf("audio/*", "video/*"))
                }
                return try {
                    fileChooserLauncher.launch(intent)
                    true
                } catch (e: Exception) {
                    fileChooserCallback = null
                    false
                }
            }
        }
        webView.setDownloadListener { url, _, contentDisposition, mimetype, _ ->
            // The editor's Download button (MP4) — and the lyric-video
            // link — land here as a localhost /api/file/<id> URL.
            try {
                val fileName = URLUtil.guessFileName(url, contentDisposition, mimetype)
                val req = DownloadManager.Request(Uri.parse(url))
                    .setMimeType(mimetype)
                    .setNotificationVisibility(
                        DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED
                    )
                    .setDestinationInExternalPublicDir(
                        Environment.DIRECTORY_DOWNLOADS, fileName
                    )
                val dm = getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager
                dm.enqueue(req)
                AppLog.d("Editor", "download started: $fileName")
                Toast.makeText(
                    this, "Downloading $fileName to Downloads…",
                    Toast.LENGTH_SHORT
                ).show()
            } catch (e: Exception) {
                AppLog.e("Editor", "download failed: ${e.message}")
                Toast.makeText(
                    this, "Download failed: ${e.message}",
                    Toast.LENGTH_LONG
                ).show()
            }
        }
    }

    // ------------------------------------------------------------------
    // Jobs view: a simple list, newest first. Tap a row to open that
    // draft in the editor.
    // ------------------------------------------------------------------

    private fun rebuildJobsList() {
        val jobs = JobStore.getAll(this)
        binding.jobsList.removeAllViews()
        binding.jobsEmpty.visibility =
            if (jobs.isEmpty()) View.VISIBLE else View.GONE
        if (jobs.isEmpty()) return
        val density = resources.displayMetrics.density
        val pad = (12 * density).toInt()
        val dateFmt = SimpleDateFormat("MMM d, h:mm a", Locale.getDefault())
        for (job in jobs) {
            val row = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(pad, pad, pad, pad)
                isClickable = true
                isFocusable = true
                // selectableItemBackground is a THEME ATTRIBUTE, not a
                // drawable — passing android.R.attr.* straight to
                // setBackgroundResource crashes with
                // Resources$NotFoundException. Resolve it first.
                val outValue = TypedValue()
                theme.resolveAttribute(
                    android.R.attr.selectableItemBackground, outValue, true
                )
                setBackgroundResource(outValue.resourceId)
                setOnClickListener {
                    showEditor(job.draftJobId, job.callId, job.title)
                }
            }
            val titleView = TextView(this).apply {
                text = job.title
                textSize = 16f
                setTypeface(Typeface.DEFAULT_BOLD)
            }
            val metaView = TextView(this).apply {
                text = "${jobStatusLabel(job.status)} • ${dateFmt.format(job.timestamp)}"
                textSize = 13f
                setTextColor(getColor(R.color.kg_muted))
            }
            row.addView(titleView)
            row.addView(metaView)
            binding.jobsList.addView(row)
        }
    }

    private fun jobStatusLabel(status: String): String = when (status) {
        "done" -> "Done"
        "error" -> "Failed"
        else -> "Running…"
    }

    // ------------------------------------------------------------------
    // Audio player: listen to the downloaded song while fixing the
    // lyrics in the box. Plain MediaPlayer on the file on disk
    // (app-private, so no permission needed). Any failure hides the
    // player instead of crashing.
    // ------------------------------------------------------------------

    /** Set the player up for the downloaded audio file (called once
     *  the file is ready — both the fresh-download and the reuse
     *  path). Replaces any previous song's player. */
    private fun setupAudioPlayer(file: File) {
        releasePlayer()
        playerFile = file
        binding.audioPlayer.visibility = View.VISIBLE
        binding.playerPlayButton.isEnabled = false
        binding.playerPlayButton.text = getString(R.string.player_play)
        binding.playerSeekBar.progress = 0
        binding.playerSeekBar.max = 0
        binding.playerTimeLabel.text = getString(R.string.player_loading)
        lifecycleScope.launch(Dispatchers.IO) {
            val mp = MediaPlayer()
            try {
                mp.setDataSource(file.path)
                mp.prepare()
            } catch (e: Exception) {
                AppLog.e("Main", "Player failed for ${file.name}: ${e.message}")
                runCatching { mp.release() }
                withContext(Dispatchers.Main) {
                    if (playerFile == file) {
                        binding.audioPlayer.visibility = View.GONE
                        Toast.makeText(
                            this@MainActivity, R.string.player_error,
                            Toast.LENGTH_SHORT
                        ).show()
                    }
                }
                return@launch
            }
            withContext(Dispatchers.Main) {
                // A newer song may have replaced this one while it
                // was preparing — don't install a stale player.
                if (playerFile != file) {
                    runCatching { mp.release() }
                    return@withContext
                }
                mediaPlayer = mp
                val dur = mp.duration
                binding.playerSeekBar.max = dur
                binding.playerTimeLabel.text =
                    "${formatDuration(0)} / ${formatDuration((dur / 1000).toLong())}"
                binding.playerPlayButton.isEnabled = true
                mp.setOnCompletionListener {
                    binding.playerSeekBar.progress = 0
                    binding.playerTimeLabel.text =
                        "${formatDuration(0)} / ${formatDuration((dur / 1000).toLong())}"
                    binding.playerPlayButton.text = getString(R.string.player_play)
                    stopPlayerTicker()
                }
            }
        }
    }

    private fun onPlayerPlayPause() {
        val mp = mediaPlayer ?: return
        if (mp.isPlaying) {
            mp.pause()
            binding.playerPlayButton.text = getString(R.string.player_play)
            stopPlayerTicker()
        } else {
            mp.start()
            binding.playerPlayButton.text = getString(R.string.player_pause)
            startPlayerTicker()
        }
    }

    private fun startPlayerTicker() {
        stopPlayerTicker()
        playerTicker = lifecycleScope.launch {
            while (true) {
                delay(500)
                val mp = mediaPlayer ?: break
                if (!mp.isPlaying) break
                if (!playerSeeking) {
                    binding.playerSeekBar.progress = mp.currentPosition
                    binding.playerTimeLabel.text =
                        "${formatDuration((mp.currentPosition / 1000).toLong())} / " +
                        formatDuration((mp.duration / 1000).toLong())
                }
            }
        }
    }

    private fun stopPlayerTicker() {
        playerTicker?.cancel()
        playerTicker = null
    }

    private fun releasePlayer() {
        stopPlayerTicker()
        playerFile = null
        mediaPlayer?.let { runCatching { it.release() } }
        mediaPlayer = null
    }

    // ------------------------------------------------------------------
    // In-app updates (silent on failure — no popup if there is no update).
    // ------------------------------------------------------------------

    private fun checkForUpdateSilently() {
        lifecycleScope.launch {
            val update = UpdateManager.checkForUpdate(this@MainActivity) ?: return@launch
            pendingUpdate = update
            binding.updateTitle.text =
                getString(R.string.update_available) + " — v" + update.versionName
            binding.updateCard.visibility = View.VISIBLE
        }
    }

    private fun onUpdateTapped() {
        val update = pendingUpdate ?: return
        if (!UpdateManager.canInstall(this)) {
            Toast.makeText(this, R.string.update_allow_install, Toast.LENGTH_LONG).show()
            UpdateManager.openInstallPermissionSettings(this)
            return
        }
        binding.updateButton.isEnabled = false
        binding.updateLaterButton.isEnabled = false
        binding.updateProgress.visibility = View.VISIBLE
        binding.updateProgress.isIndeterminate = false
        binding.updateProgress.progress = 0
        binding.updateText.text = getString(R.string.update_downloading)
        lifecycleScope.launch {
            try {
                val apk = UpdateManager.downloadUpdate(this@MainActivity, update) { done, total ->
                    runOnUiThread {
                        if (total > 0) {
                            binding.updateProgress.progress = (done * 100 / total).toInt()
                        }
                    }
                }
                UpdateManager.installUpdate(this@MainActivity, apk)
            } catch (e: IOException) {
                binding.updateText.text = "Update download failed — check your connection and try again."
            } catch (e: Exception) {
                binding.updateText.text = "Update failed: ${e.message}"
            } finally {
                binding.updateButton.isEnabled = true
                binding.updateLaterButton.isEnabled = true
                binding.updateProgress.visibility = View.GONE
            }
        }
    }

    // ------------------------------------------------------------------
    // Copy logs — paste them in chat so issues can be fixed.
    // ------------------------------------------------------------------

    private fun onCopyLogs() {
        val logs = AppLog.snapshot()
        if (logs.isEmpty()) {
            Toast.makeText(this, R.string.logs_empty, Toast.LENGTH_SHORT).show()
            return
        }
        val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        clipboard.setPrimaryClip(ClipData.newPlainText("KaraokeGen logs", logs))
        Toast.makeText(this, R.string.logs_copied, Toast.LENGTH_SHORT).show()
        AppLog.d("Main", "Logs copied (${logs.length} chars)")
    }

    // ------------------------------------------------------------------
    // One Start button: link -> audio -> draft/transcribe -> result.
    // ------------------------------------------------------------------

    private fun showError(msg: String) {
        binding.errorText.text = msg
        binding.errorText.visibility = View.VISIBLE
    }

    private fun clearError() {
        binding.errorText.visibility = View.GONE
    }

    private fun setRunning(running: Boolean) {
        binding.startButton.isEnabled = !running
        binding.cancelButton.visibility = if (running) View.VISIBLE else View.GONE
        if (!running) {
            binding.audioProgress.visibility = View.GONE
            binding.draftProgress.visibility = View.GONE
        }
    }

    private fun onStartTapped() {
        val url = binding.urlInput.text?.toString()?.trim().orEmpty()
        if (url.isEmpty()) {
            showError("Paste a YouTube link first.")
            return
        }
        if (!url.contains("youtube.com") && !url.contains("youtu.be")) {
            showError("That doesn't look like a YouTube link.")
            return
        }
        if (!SettingsStore.isConfigured(this)) {
            showError("Set up your backend first — open Backend settings and paste your Modal URL and API key.")
            startActivity(Intent(this, SettingsActivity::class.java))
            return
        }
        // A fresh run is always "Start" — the "Run alignment" label only
        // applies right after a transcribe, and showDraftDone sets it.
        binding.startButton.text = getString(R.string.start)
        clearError()
        val lyrics = binding.lyricsInput.text?.toString().orEmpty()
        // Empty lyrics means "transcribe" automatically.
        val transcribe = lyrics.isBlank()
        val genre = if (binding.genreBallad.isChecked) "ballad" else "hiphop"
        // Song language from the selector (same three choices as the
        // desktop editor). Resolved to a concrete code before sending:
        // the backend accepts "" (auto) but its aligner requires a real
        // code and dies on "". Auto-detect reads the lyrics text.
        val language = resolveLanguage(binding, lyrics)
        val baseUrl = SettingsStore.getBaseUrl(this)
        val apiKey = SettingsStore.getApiKey(this)

        setRunning(true)
        binding.audioResult.visibility = View.GONE
        binding.draftResult.visibility = View.GONE
        binding.audioProgress.visibility = View.VISIBLE
        binding.audioProgress.isIndeterminate = true
        binding.audioProgress.progress = 0
        binding.audioStatus.text = "Finding the audio…"
        binding.draftStatus.text = ""
        AppLog.d("Main", "Start: $url (transcribe=$transcribe genre=$genre)")

        flowJob = lifecycleScope.launch {
            try {
                // Step 1: resolve + download the audio — unless this is
                // the follow-up "Run alignment" for the same link, in
                // which case the audio is already on disk (and the
                // backend has the stems cached), so reuse it.
                val reuseFile = if (cachedAudioUrl == url) {
                    cachedSendFile?.takeIf { it.exists() }
                } else null
                val reuseResolved = if (reuseFile != null) cachedResolved else null
                val resolved: YoutubeAudio.ResolvedAudio
                var sendFile: File
                if (reuseFile != null && reuseResolved != null) {
                    resolved = reuseResolved
                    sendFile = reuseFile
                    AppLog.d("Main", "Reusing downloaded audio: ${sendFile.length()} bytes -> ${sendFile.name}")
                    binding.audioStatus.text = "Using downloaded audio: ${resolved.title}"
                    binding.audioProgress.visibility = View.GONE
                } else {
                    val r = YoutubeAudio.resolve(url)
                    AppLog.d("Main", "Resolved: ${r.title} (${r.durationSec}s, ${r.bitrate / 1000} kbps, .${r.suffix})")
                    binding.audioStatus.text = "Downloading: ${r.title}"
                    binding.audioProgress.isIndeterminate = false
                    val safeName = r.title
                        .replace(Regex("[^A-Za-z0-9._-]+"), "_")
                        .take(80)
                        .ifEmpty { "song" }
                    val audioFile = File(
                        getExternalFilesDir(Environment.DIRECTORY_MUSIC),
                        "KaraokeGen/$safeName.${r.suffix}"
                    )
                    YoutubeAudio.download(r.audioUrl, audioFile) { done, total ->
                        runOnUiThread {
                            if (total > 0) {
                                val pct = (done * 100 / total).toInt()
                                binding.audioProgress.progress = pct
                                binding.audioStatus.text =
                                    "Downloading: ${r.title} — $pct%"
                            }
                        }
                    }
                    AppLog.d("Main", "Audio ready: ${audioFile.length()} bytes -> ${audioFile.name}")
                    // YouTube serves WebM/Opus or M4A/AAC, never MP3 — convert
                    // to MP3 on-device so the trial only ever sends .mp3. If
                    // this phone has no MP3 encoder, keep the original file
                    // (the backend decodes any format, so the draft still works).
                    sendFile = audioFile
                    if (audioFile.extension.lowercase() != "mp3") {
                        binding.audioStatus.text = "Converting to MP3…"
                        val mp3File = File(
                            audioFile.parentFile,
                            "${audioFile.nameWithoutExtension}.mp3"
                        )
                        val converted = withContext(Dispatchers.IO) {
                            AudioConvert.toMp3(audioFile, mp3File)
                        }
                        if (converted) {
                            AppLog.d("Main", "Converted to MP3: ${mp3File.length()} bytes -> ${mp3File.name}")
                            try { audioFile.delete() } catch (_: Exception) {}
                            sendFile = mp3File
                        } else {
                            AppLog.d("Main", "MP3 convert unavailable — sending original .${audioFile.extension}")
                        }
                    }
                    resolved = r
                    // Cache for a follow-up "Run alignment" on this link.
                    cachedAudioUrl = url
                    cachedResolved = r
                    cachedSendFile = sendFile
                }
                binding.audioProgress.visibility = View.GONE
                val info = buildString {
                    append(resolved.title)
                    append("\n")
                    append(formatDuration(resolved.durationSec))
                    append(" • ")
                    append(formatSize(sendFile.length()))
                    append(" • ")
                    append(resolved.bitrate / 1000)
                    append(" kbps")
                    if (resolved.uploader.isNotEmpty()) {
                        append(" • ")
                        append(resolved.uploader)
                    }
                }
                binding.audioResult.text = info
                binding.audioResult.visibility = View.VISIBLE
                // The audio file is ready (fresh download or reused) —
                // set the player up so the song can be heard while the
                // lyrics are fixed.
                setupAudioPlayer(sendFile)

                // Step 2: send it to the backend (draft or transcribe).
                binding.draftProgress.visibility = View.VISIBLE
                binding.draftStatus.text =
                    if (transcribe) "Uploading audio for transcription…" else "Uploading audio…"
                val client = ModalClient(baseUrl, apiKey)
                val submitted = withContext(Dispatchers.IO) {
                    client.submitDraft(
                        sendFile,
                        language = language,
                        lyrics = if (transcribe) "" else lyrics,
                        genre = genre,
                        transcribeOnly = transcribe
                    )
                }
                currentJobId = submitted.jobId
                JobStore.record(
                    this@MainActivity, resolved.title,
                    submitted.draftJobId, submitted.jobId
                )
                AppLog.d("Main", "Draft submitted: job=${submitted.jobId} draft=${submitted.draftJobId}")
                binding.draftStatus.text = "Queued — waiting for the GPU…"
                // Poll for the result. A single failed poll (a DNS blip,
                // a dropped signal) must NOT kill the flow — the job is
                // already running on the backend. Only give up after 10
                // failed polls in a row (~30s with no connection at all).
                var failedPolls = 0
                while (true) {
                    delay(3000)
                    val job = try {
                        withContext(Dispatchers.IO) { client.getJob(submitted.jobId) }
                    } catch (e: IOException) {
                        failedPolls++
                        AppLog.d("Main", "Poll failed ($failedPolls/10): ${e.message}")
                        binding.draftStatus.text =
                            "Connection hiccup — retrying… ($failedPolls/10)"
                        if (failedPolls >= 10) {
                            AppLog.e("Main", "Poll gave up after 10 failures")
                            showError(
                                "Lost connection while waiting. The draft is " +
                                "still running on the backend — try again " +
                                "and it will pick up.")
                            binding.draftStatus.text = ""
                            break
                        }
                        continue
                    }
                    failedPolls = 0
                    val progress = try {
                        withContext(Dispatchers.IO) {
                            client.getProgress(submitted.draftJobId)
                        }
                    } catch (_: IOException) {
                        null // Progress is nice-to-have; the job status decides.
                    }
                    if (progress != null) {
                        binding.draftStatus.text = buildString {
                            append(progress.stage)
                            append(" — ")
                            append(progress.pct)
                            append("%")
                            if (progress.detail.isNotEmpty()) {
                                append("\n")
                                append(progress.detail)
                            }
                        }
                    }
                    when (job.status) {
                        "done" -> {
                            AppLog.d("Main", "Draft done")
                            JobStore.updateStatus(
                                this@MainActivity, submitted.draftJobId, "done"
                            )
                            showDraftDone(
                                job.result, transcribe,
                                submitted.draftJobId, submitted.jobId,
                                resolved.title
                            )
                            break
                        }
                        "error" -> {
                            val detail = job.error.ifBlank { "no detail from backend" }
                            AppLog.e("Main", "Draft error from backend: $detail")
                            JobStore.updateStatus(
                                this@MainActivity, submitted.draftJobId, "error"
                            )
                            showError("The draft failed on the backend: $detail")
                            binding.draftStatus.text = ""
                            break
                        }
                        else -> {
                            // Still pending — keep polling.
                        }
                    }
                }
            } catch (e: YoutubeError) {
                AppLog.e("Main", "Failed: ${e.message}")
                showError(e.message ?: "Couldn't get that video.")
                binding.audioStatus.text = ""
                binding.draftStatus.text = ""
            } catch (e: ModalError) {
                AppLog.e("Main", "Draft failed: ${e.message}")
                showError(e.message ?: "Backend error.")
                binding.draftStatus.text = ""
            } catch (e: IOException) {
                AppLog.e("Main", "Failed: no connection (${e.message})")
                showError("Couldn't reach the backend — check the URL and your connection.")
                binding.audioStatus.text = ""
            } catch (e: Exception) {
                AppLog.e("Main", "Failed: ${e.message}")
                showError("Something went wrong: ${e.message}")
                binding.audioStatus.text = ""
                binding.draftStatus.text = ""
            } finally {
                setRunning(false)
            }
        }
    }

    /**
     * Draft finished.
     * - With lyrics: the alignment is ready — switch this page to the
     *   editor (same Activity) with this draft, where the user drags
     *   word timings and renders.
     * - Transcribe-only: no alignment yet. Put the transcript into the
     *   lyrics box for editing; the user fixes the text and taps Start
     *   (now "Run alignment") to align it. The follow-up /draft reuses
     *   the cached stems server-side, so separation is skipped.
     */
    private fun showDraftDone(
        result: JSONObject?,
        transcribe: Boolean,
        draftJobId: String,
        callId: String,
        songTitle: String
    ) {
        if (result == null) {
            AppLog.e("Main", "Draft done but the backend sent no result")
            showError("The backend finished but sent no result. Try again.")
            binding.draftStatus.text = ""
            return
        }
        if (transcribe) {
            // The backend's transcribe-only result carries the text under
            // "transcript" (not "lyrics" — that key only exists on a full
            // DraftResult). Read "transcript" first, "lyrics" as fallback.
            val transcript = result.optString("transcript")
                .ifBlank { result.optString("lyrics") }
            if (transcript.isBlank()) {
                AppLog.e("Main", "Transcribe done but no transcript text in result: ${result.keys().asSequence().toList()}")
                showError("Transcription came back empty — try again or paste lyrics manually.")
                binding.draftStatus.text = ""
                return
            }
            binding.lyricsInput.setText(transcript)
            binding.draftStatus.text = ""
            binding.draftResult.text = getString(R.string.transcript_ready_hint)
            binding.draftResult.visibility = View.VISIBLE
            binding.startButton.text = getString(R.string.run_alignment)
            AppLog.d("Main", "Transcript ready (${transcript.length} chars) — waiting for edits")
            return
        }
        val alignment = result.optJSONObject("alignment")
        val segments = alignment?.optJSONArray("segments")
        var lines = 0
        var words = 0
        if (segments != null) {
            lines = segments.length()
            for (i in 0 until segments.length()) {
                words += segments.optJSONObject(i)?.optJSONArray("words")?.length() ?: 0
            }
        }
        val duration = result.optDouble("duration", 0.0)
        AppLog.d("Main", "Draft ready: $lines lines, $words words, ${formatDuration(duration.toLong())} — opening editor")
        binding.draftStatus.text = ""
        binding.draftResult.text = getString(R.string.draft_ready_title) +
            " — $lines lines, $words words. Opening the editor…"
        binding.draftResult.visibility = View.VISIBLE
        binding.startButton.text = getString(R.string.start)
        showEditor(draftJobId, callId, songTitle)
    }

    private fun onCancel() {
        flowJob?.cancel()
        flowJob = null
        val jobId = currentJobId
        if (jobId != null && SettingsStore.isConfigured(this)) {
            lifecycleScope.launch(Dispatchers.IO) {
                ModalClient(
                    SettingsStore.getBaseUrl(this@MainActivity),
                    SettingsStore.getApiKey(this@MainActivity)
                ).cancel(jobId)
            }
        }
        currentJobId = null
        setRunning(false)
        binding.audioStatus.text = "Cancelled."
        binding.draftStatus.text = ""
        AppLog.d("Main", "Cancelled by user")
    }

    private fun formatDuration(seconds: Long): String {
        if (seconds <= 0) return "0:00"
        val m = seconds / 60
        val s = seconds % 60
        return "%d:%02d".format(m, s)
    }

    private fun formatSize(bytes: Long): String {
        return when {
            bytes >= 1024 * 1024 -> "%.1f MB".format(bytes / (1024.0 * 1024.0))
            bytes >= 1024 -> "%d KB".format(bytes / 1024)
            else -> "$bytes B"
        }
    }

    /**
     * Resolve the language selector to the concrete code sent to /draft.
     * Tagalog/Taglish -> "tl", English -> "en". Auto-detect reads the
     * lyrics text: if Tagalog marker words make up at least 1% of the
     * words it's Tagalog ("tl"), otherwise English ("en"). With no
     * lyrics (transcribe flow) it defaults to "tl" — the desktop
     * default, and safe because the backend auto-detects English from
     * the lyrics once it has them. A blank "" is NEVER sent: the
     * backend accepts it but its aligner (stable-ts) requires a
     * concrete code and dies with "expected argument for language".
     * The marker list and 1% threshold are ported from the backend's
     * KaraokeGen/word_ctc.py (TAG_MARKERS / word_ctc_en_tag_ratio).
     */
    private fun resolveLanguage(
        binding: ActivityMainBinding,
        lyrics: String
    ): String {
        if (binding.langEnglish.isChecked) return "en"
        if (!binding.langAuto.isChecked) return "tl" // Tagalog selected.
        // Auto-detect from the lyrics text.
        val words = lyrics.lowercase()
            .split(Regex("[^a-zñ]+"))
            .filter { it.isNotEmpty() }
        if (words.isEmpty()) return "tl"
        val markers = TAG_MARKERS
        val hits = words.count { it in markers }
        return if (hits.toDouble() / words.size >= 0.01) "tl" else "en"
    }

    companion object {
        // Tagalog marker words (backend KaraokeGen/word_ctc.py TAG_MARKERS).
        private val TAG_MARKERS = setOf(
            "ang", "ng", "sa", "na", "ako", "ikaw", "ay", "mga", "hindi",
            "kita", "para", "kaya", "bakit", "kung", "nang", "iyong",
            "akin", "mahal", "puso", "araw", "gabi", "buwan", "ilaw",
            "tayo", "ito", "iyon", "lang", "naman", "kami", "sila",
            "siya", "mo", "ko"
        )
    }
}
