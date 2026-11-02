package com.lukatdamoves.karaokegen

import android.content.ClipData
import android.content.ClipboardManager
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.media.MediaPlayer
import android.os.Bundle
import android.os.Environment
import android.provider.MediaStore
import android.util.TypedValue
import android.view.MotionEvent
import android.view.View
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.SeekBar
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.ActionBarDrawerToggle
import androidx.appcompat.app.AlertDialog
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
import java.util.ArrayDeque
import java.util.Locale

/**
 * KaraokeGen trial, one page: paste a YouTube link -> one Start button
 * does the whole thing (download the audio, then draft it on the Modal
 * backend — with lyrics, or transcribe when the lyrics box is empty).
 *
 * The page has two views, switched by visibility: home (the form,
 * with the native editor inline at the bottom once an alignment
 * finishes) and jobs (the job queue). The sidebar holds Home, Jobs,
 * Backend settings and Copy logs. Update check runs silently
 * on launch.
 *
 * The editor is fully native (v0.3.0): a WaveformView draws the
 * vocals waveform with draggable word regions, a native word list
 * edits the text, and Render MP4 posts the edited alignment to the
 * backend. No WebView — the desktop editor's look never matched
 * the app, so it is gone.
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

    // Native editor state (replaces the old WebView editor).
    // The draft id currently loaded (null = nothing loaded yet).
    // A repeat open of the same draft does NOT reload, so
    // in-progress edits survive switching views. The draft id is
    // the AUDIO HASH, so a different run of the same song shares it
    // — callers that must force a reload (a Jobs tap) clear this
    // first.
    private var loadedEditorDraft: String? = null
    private var editorAlignment: EditorAlignment? = null
    private var editorDraftJobId: String = ""
    private var editorSongTitle: String = ""
    private var editorUndoStack: ArrayDeque<EditorAlignment> = ArrayDeque()
    private var editorSelected: WordRef? = null
    // Editor playback (separate MediaPlayer from the homepage one).
    private var editorPlayer: MediaPlayer? = null
    private var editorPlayerFile: File? = null
    private var editorTicker: Job? = null
    private var editorPlayWordEnd: Double? = null
    private var editorPeaks: FloatArray? = null
    private var editorWindowStart: Double = 0.0
    private var editorWindowDuration: Double = 0.0
    private var editorRenderJob: Job? = null
    private var editorRenderFileId: String? = null
    private var editorRenderFilename: String? = null
    // Word chip views (WordRef -> chip), so the ticker can highlight
    // the currently playing word without rebuilding the list.
    private var editorChipViews: MutableMap<WordRef, TextView> = mutableMapOf()
    private var editorCurrentWord: WordRef? = null

    // Audio player (listen while fixing the lyrics).
    private var mediaPlayer: MediaPlayer? = null
    private var playerFile: File? = null
    private var playerTicker: Job? = null
    private var playerSeeking = false
    // The player row is only allowed to show once a transcript is in
    // the lyrics box (the user is fixing lyrics). It is prepared as
    // soon as the audio downloads, but stays hidden until then.
    private var playerRevealAllowed = false

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

        // Native editor controls (waveform, playback, word list,
        // render) — wired once; the model is swapped per draft.
        setupNativeEditor()

        // Back: close the drawer first; from jobs, return home.
        // The editor lives inline at the bottom of the home page —
        // there is no separate editor view to leave.
        // (Object form, not the lambda overload — that lives in
        // activity-ktx, which this app does not depend on.)
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                when {
                    binding.drawerLayout.isDrawerOpen(GravityCompat.START) ->
                        binding.drawerLayout.closeDrawer(GravityCompat.START)
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
        editorRenderJob?.cancel()
        releasePlayer()
        releaseEditorPlayer()
        super.onDestroy()
    }

    // ------------------------------------------------------------------
    // Views: home / jobs (one Activity, toggled by visibility). The
    // editor lives inline at the bottom of the home page.
    // ------------------------------------------------------------------

    private fun showHome() {
        binding.jobsView.visibility = View.GONE
        binding.homeView.visibility = View.VISIBLE
        binding.toolbar.title = getString(R.string.app_name)
        binding.navView.setCheckedItem(R.id.nav_home)
    }

    private fun showJobs() {
        binding.homeView.visibility = View.GONE
        binding.jobsView.visibility = View.VISIBLE
        binding.toolbar.title = getString(R.string.jobs_title)
        binding.navView.setCheckedItem(R.id.nav_jobs)
        rebuildJobsList()
    }

    /** Show the native editor inline at the bottom of the home
     *  page and load the draft. The toolbar title stays "KaraokeGen"
     *  — this is the same page, not a new one. Re-opening the same
     *  draft does not reload, so in-progress edits survive switching
     *  views. A Jobs tap clears loadedEditorDraft first (see
     *  rebuildJobsList) so tapping a job always reloads it. */
    private fun showEditor(draftJobId: String, songTitle: String) {
        if (draftJobId == loadedEditorDraft && editorAlignment != null) {
            // Already loaded — just reveal and scroll to it.
            revealEditorSection()
            return
        }
        loadNativeEditor(draftJobId, songTitle)
        revealEditorSection()
    }

    private fun revealEditorSection() {
        binding.jobsView.visibility = View.GONE
        binding.homeView.visibility = View.VISIBLE
        binding.toolbar.title = getString(R.string.app_name)
        binding.navView.setCheckedItem(R.id.nav_home)
        binding.editorSection.visibility = View.VISIBLE
        binding.editorSection.post {
            binding.homeView.smoothScrollTo(0, binding.editorSection.top)
        }
    }

    /**
     * Load a draft into the native editor: fetch the DraftResult
     * (alignment + duration), download the vocals WAV for the
     * waveform + playback, decode the waveform peaks, and populate
     * the UI. Never crashes on a missing draft or failed audio —
     * it shows a clear status instead.
     */
    private fun loadNativeEditor(draftJobId: String, songTitle: String) {
        editorRenderJob?.cancel()
        releaseEditorPlayer()
        editorUndoStack.clear()
        editorSelected = null
        editorPeaks = null
        editorDraftJobId = draftJobId
        editorSongTitle = songTitle
        loadedEditorDraft = draftJobId
        binding.editorSongTitle.text = songTitle
        binding.editorStatus.visibility = View.GONE
        binding.editorRenderStatus.visibility = View.GONE
        binding.editorDownloadButton.visibility = View.GONE
        binding.editorRenderButton.isEnabled = false
        binding.editorLoading.visibility = View.VISIBLE
        binding.editorWaveform.setData(null, 0.0, emptyList())
        binding.editorWordList.removeAllViews()
        if (!SettingsStore.isConfigured(this)) {
            binding.editorLoading.visibility = View.GONE
            binding.editorStatus.text = "Set up your backend first."
            binding.editorStatus.visibility = View.VISIBLE
            return
        }
        val baseUrl = SettingsStore.getBaseUrl(this)
        val apiKey = SettingsStore.getApiKey(this)
        lifecycleScope.launch {
            // 1. The alignment (segments/words + duration).
            val result = withContext(Dispatchers.IO) {
                ModalClient(baseUrl, apiKey).getDraftResultRaw(draftJobId)
            }
            val alignment = EditorAlignment.fromDraftResult(result)
            if (alignment == null) {
                binding.editorLoading.visibility = View.GONE
                binding.editorStatus.text = getString(R.string.editor_no_alignment)
                binding.editorStatus.visibility = View.VISIBLE
                AppLog.e("Editor", "No alignment for draft $draftJobId")
                return@launch
            }
            editorAlignment = alignment
            // 2. The vocals WAV (waveform + playback). Fall back to
            // the original downloaded song when it can't be fetched.
            val vocalsFile = withContext(Dispatchers.IO) {
                downloadVocalsWav(baseUrl, apiKey, draftJobId)
            }
            val audioFile = vocalsFile
                ?: cachedSendFile?.takeIf { it.exists() }
            var peaks: FloatArray? = null
            if (audioFile != null) {
                val decoded = withContext(Dispatchers.IO) {
                    AudioDecode.decode(audioFile)
                }
                if (decoded != null) {
                    peaks = AudioDecode.computePeaks(decoded.samples, 1500)
                }
            }
            editorPeaks = peaks
            // 3. The editor's own MediaPlayer (separate from the
            // homepage player).
            if (audioFile != null) {
                setupEditorPlayer(audioFile)
            }
            // 4. Populate the UI.
            binding.editorLoading.visibility = View.GONE
            editorWindowStart = 0.0
            editorWindowDuration = alignment.duration
            binding.editorZoomBar.progress = 0
            syncWaveform()
            rebuildWordList()
            binding.editorRenderButton.isEnabled = true
            if (audioFile == null) {
                binding.editorStatus.text = getString(R.string.editor_no_audio)
                binding.editorStatus.visibility = View.VISIBLE
            }
            AppLog.d(
                "Editor",
                "Loaded draft $draftJobId: ${alignment.lines.size} lines"
            )
        }
    }

    /** Download /vocals-raw/<draftId> (a WAV) to the cache dir.
     *  Returns null on any failure — the caller falls back. */
    private fun downloadVocalsWav(
        baseUrl: String, apiKey: String, draftJobId: String
    ): File? {
        return try {
            val client = ModalClient(baseUrl, apiKey)
            val file = File(cacheDir, "vocals-$draftJobId.wav")
            client.openStream("/vocals-raw/$draftJobId").use { input ->
                file.outputStream().use { out -> input.copyTo(out) }
            }
            if (file.exists() && file.length() > 0) file else null
        } catch (e: Exception) {
            AppLog.e("Editor", "Vocals download failed: ${e.message}")
            null
        }
    }

    /** Push the current alignment into the waveform view (peaks,
     *  words, window, selection, playhead). */
    private fun syncWaveform() {
        val alignment = editorAlignment ?: return
        val waveWords = alignment.flatWords().map { (li, wi, w) ->
            WaveformView.WaveWord(li, wi, w.text, w.start, w.end)
        }
        binding.editorWaveform.setData(editorPeaks, alignment.duration, waveWords)
        binding.editorWaveform.selected = editorSelected
        binding.editorWaveform.currentWord = currentWordAt(editorPlayheadSec())
        binding.editorWaveform.setWindow(editorWindowStart, editorWindowDuration)
        binding.editorWaveform.playheadSec = editorPlayheadSec()
    }

    private fun editorPlayheadSec(): Double {
        val mp = editorPlayer ?: return -1.0
        return mp.currentPosition / 1000.0
    }

    /** The word playing at `time`, or null. */
    private fun currentWordAt(time: Double): WordRef? {
        if (time < 0) return null
        val alignment = editorAlignment ?: return null
        for ((li, wi, w) in alignment.flatWords()) {
            if (time >= w.start && time < w.end) return WordRef(li, wi)
        }
        return null
    }

    /** Push a deep copy onto the undo stack (before an edit). */
    private fun pushEditorUndo() {
        val alignment = editorAlignment ?: return
        editorUndoStack.addLast(alignment.deepCopy())
        if (editorUndoStack.size > 50) editorUndoStack.removeFirst()
        binding.editorUndoButton.isEnabled = true
    }

    private fun undoEditorEdit() {
        val previous = editorUndoStack.pollLast() ?: return
        editorAlignment = previous
        editorSelected = null
        binding.editorUndoButton.isEnabled = editorUndoStack.isNotEmpty()
        syncWaveform()
        rebuildWordList()
        AppLog.d("Editor", "Undo — ${editorUndoStack.size} left")
    }

    /** Wire the native editor controls (called once from onCreate).
     *  The waveform callbacks read the live model, so they are set
     *  once here — the model is swapped when a new draft loads. */
    private fun setupNativeEditor() {
        val wave = binding.editorWaveform
        wave.onWordTap = { ref ->
            editorSelected = ref
            syncWaveform()
            refreshChipHighlights()
            // Seek the playhead to the word's start so Play starts there.
            val word = editorAlignment?.wordAt(ref)
            if (word != null) seekEditorTo(word.start)
        }
        wave.onSeekTo = { time ->
            seekEditorTo(time)
        }
        wave.onWordDragStart = {
            pushEditorUndo()
        }
        wave.onWordChanged = { ref, newStart, newEnd ->
            val word = editorAlignment?.wordAt(ref)
            if (word != null) {
                word.start = newStart
                word.end = newEnd
            }
            // The waveform already redraws itself; the word list
            // shows no times, so it needs no rebuild mid-drag.
        }
        wave.onWordDragEnd = {
            rebuildWordList()
        }
        binding.editorZoomBar.setOnSeekBarChangeListener(
            object : SeekBar.OnSeekBarChangeListener {
                override fun onProgressChanged(
                    seekBar: SeekBar?, progress: Int, fromUser: Boolean
                ) {
                    if (!fromUser) return
                    val alignment = editorAlignment ?: return
                    val duration = alignment.duration
                    if (duration <= 0) return
                    // progress 0 = whole song; 100 = a 10s window (or
                    // the whole song when it is shorter than that).
                    val minWindow = minOf(10.0, duration)
                    val window = duration - (duration - minWindow) * progress / 100.0
                    val centre = editorPlayheadSec().let {
                        if (it >= 0) it else editorWindowStart + editorWindowDuration / 2
                    }
                    val start = (centre - window / 2)
                        .coerceIn(0.0, maxOf(0.0, duration - window))
                    editorWindowStart = start
                    editorWindowDuration = window
                    binding.editorWaveform.setWindow(start, window)
                }

                override fun onStartTrackingTouch(seekBar: SeekBar?) {}
                override fun onStopTrackingTouch(seekBar: SeekBar?) {}
            }
        )
        binding.editorPlayButton.setOnClickListener { onEditorPlayPause() }
        binding.editorPrevButton.setOnClickListener { editorJumpLine(-1) }
        binding.editorNextButton.setOnClickListener { editorJumpLine(1) }
        binding.editorAddLineButton.setOnClickListener { addEditorLine() }
        binding.editorUndoButton.setOnClickListener { undoEditorEdit() }
        binding.editorRenderButton.setOnClickListener { onRenderTapped() }
        binding.editorDownloadButton.setOnClickListener { downloadRenderedMp4() }
    }

    // ------------------------------------------------------------------
    // Native editor: word list (chips), playback, render.
    // ------------------------------------------------------------------

    /** Rebuild the word chips from the model. Each line is a row:
     *  a wrapping flow of word chips + a delete-line button. */
    private fun rebuildWordList() {
        val alignment = editorAlignment
        val container = binding.editorWordList
        container.removeAllViews()
        editorChipViews.clear()
        if (alignment == null) return
        val density = resources.displayMetrics.density
        val chipPadH = (10 * density).toInt()
        val chipPadV = (6 * density).toInt()
        for ((li, line) in alignment.lines.withIndex()) {
            val row = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = android.view.Gravity.CENTER_VERTICAL
            }
            val flow = FlowLayout(this@MainActivity).apply {
                layoutParams = LinearLayout.LayoutParams(
                    0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f
                )
            }
            for ((wi, word) in line.words.withIndex()) {
                val ref = WordRef(li, wi)
                val chip = TextView(this@MainActivity).apply {
                    text = word.text.ifEmpty { "…" }
                    textSize = 15f
                    setPadding(chipPadH, chipPadV, chipPadH, chipPadV)
                    setTextColor(getColor(R.color.kg_text))
                    setOnClickListener {
                        editorSelected = ref
                        syncWaveform()
                        refreshChipHighlights()
                        playEditorWord(ref)
                    }
                    setOnLongClickListener {
                        editorSelected = ref
                        syncWaveform()
                        refreshChipHighlights()
                        editEditorWord(ref)
                        true
                    }
                }
                editorChipViews[ref] = chip
                flow.addView(chip)
            }
            row.addView(flow)
            val del = TextView(this@MainActivity).apply {
                text = "✕"
                textSize = 18f
                setPadding(chipPadH, chipPadV, chipPadH, chipPadV)
                setTextColor(getColor(R.color.kg_error))
                setOnClickListener { deleteEditorLine(li) }
            }
            row.addView(del)
            container.addView(row)
        }
        refreshChipHighlights()
        binding.editorUndoButton.isEnabled = editorUndoStack.isNotEmpty()
    }

    /** Set each chip's background from its state: selected (solid
     *  blue), currently playing (darker blue + white border), or
     *  plain (panel + border). Cheap — no rebuild. */
    private fun refreshChipHighlights() {
        for ((ref, chip) in editorChipViews) {
            val selected = ref == editorSelected
            val current = ref == editorCurrentWord
            chip.background = chipBackground(selected, current)
        }
    }

    private fun chipBackground(selected: Boolean, current: Boolean): GradientDrawable {
        val density = resources.displayMetrics.density
        return GradientDrawable().apply {
            shape = GradientDrawable.RECTANGLE
            cornerRadius = 8f * density
            when {
                selected -> setColor(getColor(R.color.kg_primary))
                current -> {
                    setColor(getColor(R.color.kg_primary_dark))
                    setStroke(
                        (1 * density).toInt(), getColor(android.R.color.white)
                    )
                }
                else -> {
                    setColor(getColor(R.color.kg_panel))
                    setStroke(
                        (1 * density).toInt(), getColor(R.color.kg_border)
                    )
                }
            }
        }
    }

    private fun addEditorLine() {
        val alignment = editorAlignment ?: return
        pushEditorUndo()
        // A placeholder word at the end the user fills in (long-press
        // the chip to edit its text, drag it on the waveform to place
        // it). Blank words are dropped when the render JSON is built.
        val lastEnd = alignment.flatWords()
            .maxOfOrNull { it.third.end } ?: 0.0
        val start = lastEnd
        val end = minOf(alignment.duration, lastEnd + 1.0)
            .let { if (it <= start) start else it }
        alignment.lines.add(
            EditorLine(mutableListOf(EditorWord("", start, end)))
        )
        syncWaveform()
        rebuildWordList()
    }

    private fun deleteEditorLine(lineIndex: Int) {
        val alignment = editorAlignment ?: return
        if (lineIndex !in alignment.lines.indices) return
        pushEditorUndo()
        alignment.lines.removeAt(lineIndex)
        if (editorSelected?.line == lineIndex) editorSelected = null
        syncWaveform()
        rebuildWordList()
    }

    /** Long-press a word chip to fix its wording. */
    private fun editEditorWord(ref: WordRef) {
        val alignment = editorAlignment ?: return
        val word = alignment.wordAt(ref) ?: return
        val input = EditText(this).apply {
            setText(word.text)
            setSelection(word.text.length)
        }
        AlertDialog.Builder(this)
            .setTitle(R.string.editor_edit_word)
            .setView(input)
            .setPositiveButton(R.string.editor_save) { _, _ ->
                val newText = input.text?.toString()?.trim().orEmpty()
                if (newText.isNotEmpty() && newText != word.text) {
                    pushEditorUndo()
                    word.text = newText
                    syncWaveform()
                    rebuildWordList()
                }
            }
            .setNegativeButton(R.string.editor_cancel, null)
            .show()
    }

    // ------------------------------------------------------------------
    // Native editor: playback (vocals WAV, separate from the homepage
    // player). The ticker moves the playhead, highlights the current
    // word and follows it on the waveform.
    // ------------------------------------------------------------------

    private fun setupEditorPlayer(file: File) {
        releaseEditorPlayer()
        editorPlayerFile = file
        try {
            val mp = MediaPlayer()
            mp.setDataSource(file.path)
            mp.prepare()
            editorPlayer = mp
            mp.setOnCompletionListener {
                editorPlayWordEnd = null
                binding.editorPlayButton.text = getString(R.string.player_play)
                stopEditorTicker()
                binding.editorWaveform.playheadSec = -1.0
                binding.editorWaveform.currentWord = null
                editorCurrentWord = null
                binding.editorWaveform.invalidate()
                refreshChipHighlights()
                updateEditorTimeLabel()
            }
        } catch (e: Exception) {
            AppLog.e("Editor", "Editor player failed: ${e.message}")
            editorPlayer = null
        }
        updateEditorTimeLabel()
    }

    private fun onEditorPlayPause() {
        val mp = editorPlayer ?: return
        if (mp.isPlaying) {
            mp.pause()
            editorPlayWordEnd = null
            binding.editorPlayButton.text = getString(R.string.player_play)
            stopEditorTicker()
        } else {
            // At the end — restart from the top.
            if (mp.currentPosition >= mp.duration - 200) mp.seekTo(0)
            editorPlayWordEnd = null
            mp.start()
            binding.editorPlayButton.text = getString(R.string.player_pause)
            startEditorTicker()
        }
        updateEditorTimeLabel()
    }

    /** Play JUST this word: seek to its start, stop at its end. */
    private fun playEditorWord(ref: WordRef) {
        val word = editorAlignment?.wordAt(ref) ?: return
        val mp = editorPlayer ?: return
        editorPlayWordEnd = word.end
        mp.seekTo((word.start * 1000).toInt().coerceIn(0, mp.duration))
        if (!mp.isPlaying) {
            mp.start()
            binding.editorPlayButton.text = getString(R.string.player_pause)
            startEditorTicker()
        }
    }

    private fun seekEditorTo(seconds: Double) {
        val mp = editorPlayer ?: return
        val ms = (seconds * 1000).toInt().coerceIn(0, mp.duration)
        mp.seekTo(ms)
        binding.editorWaveform.playheadSec = seconds
        binding.editorWaveform.currentWord = currentWordAt(seconds)
        editorCurrentWord = currentWordAt(seconds)
        binding.editorWaveform.ensureVisible(seconds)
        editorWindowStart = binding.editorWaveform.windowStart
        binding.editorWaveform.invalidate()
        refreshChipHighlights()
        updateEditorTimeLabel()
    }

    /** Jump to the previous/next LINE's first word. */
    private fun editorJumpLine(direction: Int) {
        val alignment = editorAlignment ?: return
        if (alignment.lines.isEmpty()) return
        val pos = editorPlayheadSec().let { if (it < 0) 0.0 else it }
        var currentLine = 0
        for ((li, _, w) in alignment.flatWords()) {
            if (w.start <= pos) currentLine = li
        }
        val target = (currentLine + direction)
            .coerceIn(0, alignment.lines.size - 1)
        val first = alignment.lines[target].words.firstOrNull() ?: return
        editorSelected = WordRef(target, 0)
        syncWaveform()
        refreshChipHighlights()
        seekEditorTo(first.start)
    }

    private fun startEditorTicker() {
        stopEditorTicker()
        editorTicker = lifecycleScope.launch {
            while (true) {
                delay(100)
                val mp = editorPlayer ?: break
                if (!mp.isPlaying) break
                val posSec = mp.currentPosition / 1000.0
                // End of a "play just this word" request — stop.
                val endAt = editorPlayWordEnd
                if (endAt != null && posSec >= endAt) {
                    mp.pause()
                    editorPlayWordEnd = null
                    binding.editorPlayButton.text = getString(R.string.player_play)
                    stopEditorTicker()
                    break
                }
                binding.editorWaveform.playheadSec = posSec
                val cur = currentWordAt(posSec)
                if (cur != editorCurrentWord) {
                    editorCurrentWord = cur
                    binding.editorWaveform.currentWord = cur
                    refreshChipHighlights()
                }
                binding.editorWaveform.ensureVisible(posSec)
                editorWindowStart = binding.editorWaveform.windowStart
                binding.editorWaveform.invalidate()
                updateEditorTimeLabel()
            }
        }
    }

    private fun stopEditorTicker() {
        editorTicker?.cancel()
        editorTicker = null
    }

    private fun releaseEditorPlayer() {
        stopEditorTicker()
        editorPlayerFile = null
        editorPlayWordEnd = null
        editorCurrentWord = null
        editorPlayer?.let { runCatching { it.release() } }
        editorPlayer = null
    }

    private fun updateEditorTimeLabel() {
        val mp = editorPlayer
        val pos = if (mp != null) mp.currentPosition / 1000 else 0
        val dur = editorAlignment?.duration?.toLong()
            ?: if (mp != null) (mp.duration / 1000).toLong() else 0
        binding.editorTimeLabel.text =
            "${formatDuration(pos.toLong())} / ${formatDuration(dur)}"
    }

    // ------------------------------------------------------------------
    // Native editor: render MP4 (POST /render-word, poll, download).
    // ------------------------------------------------------------------

    private fun selectedBgColor(): String {
        return when {
            binding.editorBgNavy.isChecked -> "#14142A"
            binding.editorBgWhite.isChecked -> "#FFFFFF"
            else -> "#000000"
        }
    }

    private fun onRenderTapped() {
        val alignment = editorAlignment ?: return
        if (editorDraftJobId.isEmpty()) return
        if (!SettingsStore.isConfigured(this)) {
            binding.editorRenderStatus.text = "Set up your backend first."
            binding.editorRenderStatus.visibility = View.VISIBLE
            return
        }
        val baseUrl = SettingsStore.getBaseUrl(this)
        val apiKey = SettingsStore.getApiKey(this)
        val outputName = editorSongTitle
            .replace(Regex("[^A-Za-z0-9._-]+"), "_")
            .take(64)
            .ifEmpty { editorDraftJobId }
        // Same body shape as the desktop editor's startRender():
        // draft id, the CURRENT edited alignment, final lyrics,
        // background colour, output name. /render-word forces
        // word_level=true + highlight=word server-side.
        val body = JSONObject().apply {
            put("draft_job_id", editorDraftJobId)
            put("alignment", alignment.toAlignmentJson())
            put("final_lyrics", alignment.finalLyrics())
            put("bg_color", selectedBgColor())
            put("output_name", outputName)
            put("second_voice", false)
            put("second_voice_gain", 1.0)
        }
        binding.editorRenderButton.isEnabled = false
        binding.editorDownloadButton.visibility = View.GONE
        editorRenderFileId = null
        binding.editorRenderStatus.text = "Rendering on Modal…"
        binding.editorRenderStatus.visibility = View.VISIBLE
        editorRenderJob = lifecycleScope.launch {
            try {
                val submitted = withContext(Dispatchers.IO) {
                    ModalClient(baseUrl, apiKey).submitRender(body, wordLevel = true)
                }
                val renderCallId = submitted.optString("job_id")
                if (renderCallId.isEmpty()) {
                    throw ModalError("Backend reply was missing the render job id.")
                }
                AppLog.d("Editor", "Render submitted: job=$renderCallId")
                var failedPolls = 0
                while (true) {
                    delay(3000)
                    val job = try {
                        withContext(Dispatchers.IO) {
                            ModalClient(baseUrl, apiKey).getJob(renderCallId)
                        }
                    } catch (e: IOException) {
                        failedPolls++
                        if (failedPolls >= 10) {
                            throw IOException("Lost connection while rendering.")
                        }
                        continue
                    }
                    failedPolls = 0
                    val progress = withContext(Dispatchers.IO) {
                        ModalClient(baseUrl, apiKey)
                            .getProgressRaw(editorDraftJobId, render = true)
                    }
                    if (progress != null) {
                        val stage = progress.optString("stage")
                        val pct = progress.optInt("pct")
                        binding.editorRenderStatus.text = if (stage.isNotEmpty()) {
                            "Rendering… $stage — $pct%"
                        } else {
                            "Rendering… $pct%"
                        }
                    }
                    when (job.status) {
                        "done" -> {
                            val result = job.result
                            val fileId = result?.optString("file_id").orEmpty()
                            val filename = result?.optString("filename")
                                .orEmpty().ifEmpty { "karaoke.mp4" }
                            if (fileId.isEmpty()) {
                                throw ModalError("Render finished but sent no file.")
                            }
                            editorRenderFileId = fileId
                            editorRenderFilename = filename
                            binding.editorRenderStatus.text =
                                "Render complete — tap Download MP4."
                            binding.editorDownloadButton.text =
                                "${getString(R.string.editor_download)} — $filename"
                            binding.editorDownloadButton.visibility = View.VISIBLE
                            break
                        }
                        "error" -> {
                            val detail = job.error.ifBlank { "no detail from backend" }
                            throw ModalError(detail)
                        }
                        else -> { /* still pending */ }
                    }
                }
            } catch (e: ModalError) {
                AppLog.e("Editor", "Render failed: ${e.message}")
                binding.editorRenderStatus.text =
                    getString(R.string.editor_render_failed, e.message ?: "Backend error.")
            } catch (e: IOException) {
                AppLog.e("Editor", "Render failed: ${e.message}")
                binding.editorRenderStatus.text =
                    getString(R.string.editor_render_failed, e.message ?: "No connection.")
            } catch (e: Exception) {
                AppLog.e("Editor", "Render failed: ${e.message}")
                binding.editorRenderStatus.text =
                    getString(R.string.editor_render_failed, e.message ?: "Something went wrong.")
            } finally {
                binding.editorRenderButton.isEnabled = true
            }
        }
    }

    /** Download the rendered MP4 (GET /files/<id>) into Downloads. */
    private fun downloadRenderedMp4() {
        val fileId = editorRenderFileId ?: return
        val filename = editorRenderFilename ?: "karaoke.mp4"
        if (!SettingsStore.isConfigured(this)) return
        val baseUrl = SettingsStore.getBaseUrl(this)
        val apiKey = SettingsStore.getApiKey(this)
        binding.editorDownloadButton.isEnabled = false
        binding.editorRenderStatus.text = "Downloading $filename…"
        lifecycleScope.launch(Dispatchers.IO) {
            try {
                val client = ModalClient(baseUrl, apiKey)
                val uri = saveStreamToDownloads(client, fileId, filename)
                withContext(Dispatchers.Main) {
                    if (uri != null) {
                        binding.editorRenderStatus.text =
                            getString(R.string.editor_saved, filename)
                        AppLog.d("Editor", "MP4 saved to Downloads: $filename")
                    } else {
                        binding.editorRenderStatus.text =
                            getString(R.string.editor_render_failed, "Couldn't save the file.")
                    }
                    binding.editorDownloadButton.isEnabled = true
                }
            } catch (e: Exception) {
                AppLog.e("Editor", "MP4 download failed: ${e.message}")
                withContext(Dispatchers.Main) {
                    binding.editorRenderStatus.text =
                        getString(R.string.editor_render_failed, e.message ?: "Download failed.")
                    binding.editorDownloadButton.isEnabled = true
                }
            }
        }
    }

    /**
     * Stream [client.openStream] into the public Downloads folder.
     * MediaStore on Android 10+ (no storage permission needed);
     * the legacy public dir below that. Returns the saved Uri, or
     * null on failure.
     */
    private fun saveStreamToDownloads(
        client: ModalClient, fileId: String, filename: String
    ): android.net.Uri? {
        return try {
            if (android.os.Build.VERSION.SDK_INT >=
                android.os.Build.VERSION_CODES.Q
            ) {
                val values = ContentValues().apply {
                    put(MediaStore.Downloads.DISPLAY_NAME, filename)
                    put(MediaStore.Downloads.MIME_TYPE, "video/mp4")
                    put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS)
                }
                val uri = contentResolver.insert(
                    MediaStore.Downloads.EXTERNAL_CONTENT_URI, values
                ) ?: return null
                contentResolver.openOutputStream(uri)?.use { out ->
                    client.openStream("/files/$fileId").use { input ->
                        input.copyTo(out)
                    }
                }
                uri
            } else {
                @Suppress("DEPRECATION")
                val dir = Environment.getExternalStoragePublicDirectory(
                    Environment.DIRECTORY_DOWNLOADS
                )
                if (!dir.exists()) dir.mkdirs()
                val file = File(dir, filename)
                file.outputStream().use { out ->
                    client.openStream("/files/$fileId").use { input ->
                        input.copyTo(out)
                    }
                }
                android.net.Uri.fromFile(file)
            }
        } catch (e: Exception) {
            AppLog.e("Editor", "Save to Downloads failed: ${e.message}")
            null
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
                    // Open a PREVIOUS job: force a reload even for
                    // the same draft, so a re-tap retries after a
                    // failed load.
                    loadedEditorDraft = null
                    showEditor(job.draftJobId, job.title)
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
     *  path). Replaces any previous song's player. The player is
     *  prepared here but the row stays HIDDEN until a transcript is
     *  in the lyrics box (showDraftDone reveals it) — showing it as
     *  soon as the audio downloads was too early. */
    private fun setupAudioPlayer(file: File) {
        releasePlayer()
        playerFile = file
        binding.audioPlayer.visibility = View.GONE
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
                // Only reveal the row if a transcript is already waiting
                // to be fixed (playerRevealAllowed). Otherwise it stays
                // hidden until showDraftDone turns it on.
                if (playerRevealAllowed) {
                    binding.audioPlayer.visibility = View.VISIBLE
                }
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
        // Fresh run: the player hides again until a new transcript is
        // there to fix (showDraftDone reveals it).
        playerRevealAllowed = false
        binding.audioPlayer.visibility = View.GONE
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
                        // Stay quiet for the first couple of blips — keep
                        // showing the last progress instead of flashing a
                        // scary message. Only after 3 in a row is it
                        // worth saying anything.
                        if (failedPolls >= 3) {
                            binding.draftStatus.text =
                                "Connection hiccup — retrying… ($failedPolls/10)"
                        }
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
            // The transcript is in the box — now the player may show,
            // so the song can be heard while the lyrics are fixed.
            playerRevealAllowed = true
            if (mediaPlayer != null) {
                binding.audioPlayer.visibility = View.VISIBLE
            }
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
        // Past the lyric-fixing step now — the player hides again.
        playerRevealAllowed = false
        binding.audioPlayer.visibility = View.GONE
        showEditor(draftJobId, songTitle)
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
