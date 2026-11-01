package com.lukatdamoves.karaokegen

import android.app.DownloadManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.os.Environment
import android.view.View
import android.webkit.URLUtil
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import com.lukatdamoves.karaokegen.databinding.ActivityEditorBinding

/**
 * The full KaraokeGen editor, running in a WebView.
 *
 * The editor is the desktop editor (editor.html + editor.js + wavesurfer),
 * served unchanged from the in-app localhost server (LocalApiServer), so
 * editor.js keeps calling /api/… exactly like the desktop. The native
 * draft flow hands a finished draft over via ?draft= (draft_job_id),
 * ?call= (Modal call id) and ?name= (song title); the server seeds those
 * into localStorage before editor.js runs, so the editor's own resume
 * logic picks the draft up and shows the waveform + word regions.
 *
 * MP4 downloads: the editor's Download button creates an anchor to
 * /api/file/<id>; the WebView DownloadListener hands that localhost URL
 * to DownloadManager, which saves it into the phone's Downloads folder
 * (no auth needed — the local server adds the Modal key when proxying).
 * File picking: the editor's <input type="file"> is bridged to the
 * Android document picker via onShowFileChooser.
 *
 * Touch drag: the regions plugin is pointer-event driven, and the page
 * ships no touch-action handling, so the server injects
 * `#waveform * { touch-action: none }` — a finger drag moves a region
 * instead of scrolling the page.
 */
class EditorActivity : AppCompatActivity() {

    private lateinit var binding: ActivityEditorBinding
    private var fileChooserCallback: ValueCallback<Array<Uri>>? = null

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
        binding = ActivityEditorBinding.inflate(layoutInflater)
        setContentView(binding.root)

        // Back goes to the previous editor page first, then exits.
        // (Object form, not the lambda overload — that lives in
        // activity-ktx, which this app does not depend on.)
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (binding.editorWebView.canGoBack()) {
                    binding.editorWebView.goBack()
                } else {
                    isEnabled = false
                    onBackPressedDispatcher.onBackPressed()
                }
            }
        })

        val draftJobId = intent.getStringExtra(EXTRA_DRAFT_JOB_ID).orEmpty()
        val callId = intent.getStringExtra(EXTRA_CALL_ID).orEmpty()
        val songName = intent.getStringExtra(EXTRA_SONG_NAME).orEmpty()

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

        val baseUrl = LocalApiServer.start(this)
        val editorUrl = Uri.parse(baseUrl).buildUpon().apply {
            if (draftJobId.isNotEmpty()) appendQueryParameter("draft", draftJobId)
            if (callId.isNotEmpty()) appendQueryParameter("call", callId)
            if (songName.isNotEmpty()) appendQueryParameter("name", songName)
        }.build().toString()
        AppLog.d("Editor", "loading editor: $baseUrl (draft=$draftJobId)")
        webView.loadUrl(editorUrl)
    }

    override fun onDestroy() {
        fileChooserCallback?.onReceiveValue(null)
        fileChooserCallback = null
        if (isFinishing) {
            // The server is a singleton; stopping it here frees the port
            // when the editor is really closed (rotation keeps it).
            LocalApiServer.stopServer()
        }
        super.onDestroy()
    }

    companion object {
        const val EXTRA_DRAFT_JOB_ID = "draft_job_id"
        const val EXTRA_CALL_ID = "call_id"
        const val EXTRA_SONG_NAME = "song_name"

        fun newIntent(
            context: Context,
            draftJobId: String = "",
            callId: String = "",
            songName: String = ""
        ): Intent = Intent(context, EditorActivity::class.java).apply {
            putExtra(EXTRA_DRAFT_JOB_ID, draftJobId)
            putExtra(EXTRA_CALL_ID, callId)
            putExtra(EXTRA_SONG_NAME, songName)
        }
    }
}
