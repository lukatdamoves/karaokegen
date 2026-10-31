package com.lukatdamoves.karaokegen

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request as OkRequest
import okhttp3.RequestBody.Companion.toRequestBody
import org.schabi.newpipe.extractor.MediaFormat
import org.schabi.newpipe.extractor.NewPipe
import org.schabi.newpipe.extractor.downloader.Downloader
import org.schabi.newpipe.extractor.downloader.Request
import org.schabi.newpipe.extractor.downloader.Response
import org.schabi.newpipe.extractor.exceptions.AgeRestrictedContentException
import org.schabi.newpipe.extractor.exceptions.ContentNotAvailableException
import org.schabi.newpipe.extractor.exceptions.ExtractionException
import org.schabi.newpipe.extractor.exceptions.GeographicRestrictionException
import org.schabi.newpipe.extractor.exceptions.PrivateContentException
import org.schabi.newpipe.extractor.stream.AudioStream
import org.schabi.newpipe.extractor.stream.StreamInfo
import org.schabi.newpipe.extractor.stream.StreamType
import java.io.File
import java.io.IOException
import java.io.RandomAccessFile
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong
import kotlin.math.min

/** Plain-language failure from the YouTube step. Shown on screen as-is. */
class YoutubeError(message: String) : Exception(message)

/**
 * Resolves a YouTube video to its best audio-only stream (NewPipe Extractor)
 * and downloads it. No fallback to another method: on failure it throws
 * [YoutubeError] and the caller stops and shows the message.
 */
object YoutubeAudio {
    private const val USER_AGENT =
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

    private val http = OkHttpClient.Builder()
        .followRedirects(true)
        .followSslRedirects(true)
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .build()

    @Volatile
    private var initialized = false

    private fun init() {
        if (initialized) return
        synchronized(this) {
            if (initialized) return
            NewPipe.init(object : Downloader() {
                override fun execute(request: Request): Response {
                    val builder = OkRequest.Builder().url(request.url())
                    for ((name, values) in request.headers()) {
                        for (value in values) builder.addHeader(name, value)
                    }
                    builder.header("User-Agent", USER_AGENT)
                    val data = request.dataToSend()
                    val method = request.httpMethod()
                    when {
                        data != null && data.isNotEmpty() ->
                            builder.method(method, data.toRequestBody(null))
                        method.equals("POST", ignoreCase = true) ->
                            builder.method(method, ByteArray(0).toRequestBody(null))
                        else -> builder.method(method, null)
                    }
                    http.newCall(builder.build()).execute().use { resp ->
                        val body = resp.body?.string() ?: ""
                        return Response(
                            resp.code,
                            resp.message,
                            resp.headers.toMultimap(),
                            body,
                            resp.request.url.toString()
                        )
                    }
                }
            })
            initialized = true
        }
    }

    data class ResolvedAudio(
        val title: String,
        val uploader: String,
        val durationSec: Long,
        val audioUrl: String,
        val suffix: String,
        val bitrate: Int
    )

    /** Resolve the video and pick the best audio-only stream. */
    suspend fun resolve(url: String): ResolvedAudio = withContext(Dispatchers.IO) {
        init()
        val service = try {
            NewPipe.getServiceByUrl(url)
        } catch (e: ExtractionException) {
            throw YoutubeError("That doesn't look like a YouTube link.")
        }
        val info = try {
            StreamInfo.getInfo(service, url)
        } catch (e: AgeRestrictedContentException) {
            throw YoutubeError("This video is age-restricted and can't be downloaded.")
        } catch (e: PrivateContentException) {
            throw YoutubeError("This video is private.")
        } catch (e: GeographicRestrictionException) {
            throw YoutubeError("This video is not available in your country.")
        } catch (e: ContentNotAvailableException) {
            throw YoutubeError("This video is unavailable (removed, private, or region-locked).")
        } catch (e: ExtractionException) {
            throw YoutubeError("Couldn't read that video. Check the link and try again.")
        } catch (e: IOException) {
            throw YoutubeError("No network — check your connection and try again.")
        }
        if (info.streamType == StreamType.LIVE_STREAM ||
            info.streamType == StreamType.AUDIO_LIVE_STREAM
        ) {
            throw YoutubeError("Live streams can't be downloaded. Try a normal video.")
        }
        val streams = info.audioStreams
        if (streams.isNullOrEmpty()) {
            throw YoutubeError("No audio stream found for this video.")
        }
        // Highest bitrate wins; M4A (AAC) wins ties — the most compatible format.
        val best = streams.sortedWith(
            compareByDescending<AudioStream> { it.averageBitrate }
                .thenBy { if (it.format == MediaFormat.M4A) 0 else 1 }
        ).first()
        ResolvedAudio(
            title = info.name,
            uploader = info.uploaderName ?: "",
            durationSec = info.duration,
            audioUrl = best.url ?: throw YoutubeError("No audio stream URL for this video."),
            suffix = best.format?.suffix ?: "m4a",
            bitrate = best.averageBitrate
        )
    }

    /**
     * Download the audio stream to [dest], reporting (downloaded, total) bytes.
     *
     * YouTube throttles a plain single-connection download to ~30 KB/s, so
     * this downloads in parallel 1 MB Range chunks (4 at a time) — the same
     * trick the KaraokeNow extension uses. If the server won't do ranges,
     * it falls back to one plain download.
     */
    suspend fun download(
        audioUrl: String,
        dest: File,
        onProgress: (downloaded: Long, total: Long) -> Unit
    ): File = withContext(Dispatchers.IO) {
        val total = probeTotal(audioUrl)
        if (total == null || total <= 0L) {
            AppLog.d("YoutubeAudio", "Range probe failed (total=$total) — plain download")
            return@withContext downloadPlain(audioUrl, dest, onProgress)
        }
        AppLog.d("YoutubeAudio", "Range download: $total bytes in 1MB chunks")
        val chunkSize = 1024L * 1024L // 1 MB
        val chunkCount = ((total + chunkSize - 1) / chunkSize).toInt()
        val downloaded = AtomicLong(0L)
        val semaphore = Semaphore(4) // 4 chunks at a time
        dest.parentFile?.mkdirs()
        try {
            RandomAccessFile(dest, "rw").use { raf ->
                raf.setLength(total)
                coroutineScope {
                    (0 until chunkCount).map { index ->
                        async {
                            semaphore.withPermit {
                                val start = index * chunkSize
                                val end = min(start + chunkSize - 1, total - 1)
                                downloadChunk(audioUrl, start, end, raf, downloaded, total, onProgress)
                            }
                        }
                    }.awaitAll()
                }
            }
        } catch (e: YoutubeError) {
            throw e
        } catch (e: IOException) {
            throw YoutubeError("Download failed — check your connection and try again.")
        }
        if (dest.length() != total) {
            throw YoutubeError("Download came back incomplete. Try again.")
        }
        AppLog.d("YoutubeAudio", "Download done: ${dest.length()} bytes")
        return@withContext dest
    }

    /** Probe with a 1-byte Range request. Returns the full size if ranges work, else null. */
    private fun probeTotal(audioUrl: String): Long? {
        val req = OkRequest.Builder()
            .url(audioUrl)
            .header("User-Agent", USER_AGENT)
            .header("Range", "bytes=0-0")
            .build()
        return try {
            http.newCall(req).execute().use { resp ->
                if (resp.code != 206) return null
                // Content-Range: bytes 0-0/<total>
                resp.header("Content-Range")
                    ?.substringAfter('/', "")
                    ?.toLongOrNull()
            }
        } catch (e: IOException) {
            null
        }
    }

    /** Download one byte range (retries 3x) and write it at its offset. */
    private fun downloadChunk(
        audioUrl: String,
        start: Long,
        end: Long,
        raf: RandomAccessFile,
        downloaded: AtomicLong,
        total: Long,
        onProgress: (Long, Long) -> Unit
    ) {
        val expected = (end - start + 1).toInt()
        var lastError: IOException? = null
        repeat(3) { attempt ->
            try {
                val req = OkRequest.Builder()
                    .url(audioUrl)
                    .header("User-Agent", USER_AGENT)
                    .header("Range", "bytes=$start-$end")
                    .build()
                http.newCall(req).execute().use { resp ->
                    if (resp.code != 206) {
                        throw YoutubeError("Download failed (HTTP ${resp.code}). Try again.")
                    }
                    val body = resp.body ?: throw YoutubeError("Download failed (empty response).")
                    val bytes = body.bytes() // chunk is 1 MB at most
                    if (bytes.size != expected) {
                        throw IOException("Short chunk: got ${bytes.size}, wanted $expected")
                    }
                    synchronized(raf) {
                        raf.seek(start)
                        raf.write(bytes)
                    }
                    val done = downloaded.addAndGet(bytes.size.toLong())
                    onProgress(done, total)
                }
                return
            } catch (e: YoutubeError) {
                throw e
            } catch (e: IOException) {
                lastError = e
                AppLog.d("YoutubeAudio", "Chunk $start-$end attempt ${attempt + 1} failed: ${e.message}")
            }
        }
        throw YoutubeError("Download failed — check your connection and try again. (${lastError?.message})")
    }

    /** Plain single-connection download — only used when ranges aren't supported. */
    private suspend fun downloadPlain(
        audioUrl: String,
        dest: File,
        onProgress: (downloaded: Long, total: Long) -> Unit
    ): File = withContext(Dispatchers.IO) {
        val req = OkRequest.Builder()
            .url(audioUrl)
            .header("User-Agent", USER_AGENT)
            .build()
        try {
            http.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) {
                    throw YoutubeError("Download failed (HTTP ${resp.code}). Try again.")
                }
                val body = resp.body ?: throw YoutubeError("Download failed (empty response).")
                val total = body.contentLength()
                var downloaded = 0L
                dest.parentFile?.mkdirs()
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
                return@withContext dest
            }
        } catch (e: YoutubeError) {
            throw e
        } catch (e: IOException) {
            throw YoutubeError("Download failed — check your connection and try again.")
        }
    }
}
