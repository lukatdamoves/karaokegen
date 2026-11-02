package com.lukatdamoves.karaokegen

import android.media.MediaCodec
import android.media.MediaExtractor
import android.media.MediaFormat
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.abs
import kotlin.math.max

/**
 * Decode an audio file to mono PCM samples (float, -1..1) so the
 * waveform view can draw it.
 *
 * Two paths, in order:
 *  1. Direct WAV parse — the backend's /vocals-raw/{id} serves a
 *     plain WAV (16-bit PCM, usually mono). Reading the header +
 *     PCM data directly is the most reliable path: no codec, no
 *     surprises. Also handles 32-bit float WAVs.
 *  2. MediaExtractor + MediaCodec — the fallback, for the original
 *     downloaded song (.webm/Opus, .m4a/AAC) when the vocals WAV
 *     can't be decoded. Returns null only when both fail; the
 *     caller then shows a plain time ruler instead of a waveform.
 */
object AudioDecode {

    data class Decoded(val samples: FloatArray, val sampleRate: Int)

    fun decode(file: File): Decoded? {
        if (!file.exists() || file.length() < 44) return null
        return decodeWav(file) ?: decodeWithCodec(file)
    }

    /** Per-bucket peak amplitude (0..1) across the whole file, for
     *  the waveform view. `buckets` is the resolution (more buckets
     *  = finer detail). Normalised by the file's own peak so a quiet
     *  song still draws a full-height waveform. */
    fun computePeaks(samples: FloatArray, buckets: Int): FloatArray {
        if (samples.isEmpty() || buckets <= 0) return FloatArray(0)
        val peaks = FloatArray(buckets)
        val perBucket = samples.size.toDouble() / buckets
        var globalPeak = 0f
        for (b in 0 until buckets) {
            val start = (b * perBucket).toInt().coerceIn(0, samples.size)
            val end = ((b + 1) * perBucket).toInt().coerceIn(0, samples.size)
            var peak = 0f
            for (i in start until end) {
                val v = abs(samples[i])
                if (v > peak) peak = v
            }
            peaks[b] = peak
            if (peak > globalPeak) globalPeak = peak
        }
        if (globalPeak > 0f) {
            for (b in peaks.indices) peaks[b] = peaks[b] / globalPeak
        }
        return peaks
    }

    // ------------------------------------------------------------------
    // Direct WAV parse (RIFF header + "fmt " + "data" chunks).
    // ------------------------------------------------------------------

    private fun decodeWav(file: File): Decoded? {
        return try {
            val bytes = file.readBytes()
            if (bytes.size < 44) return null
            val buf = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN)
            // "RIFF" + size + "WAVE"
            if (buf.int != 0x46464952) return null // "RIFF"
            buf.int // chunk size
            if (buf.int != 0x45564157) return null // "WAVE"
            var audioFormat = -1
            var channels = 0
            var sampleRate = 0
            var bitsPerSample = 0
            var dataStart = -1
            var dataLen = 0
            // Walk the chunks until the "data" chunk.
            var pos = 12
            while (pos + 8 <= bytes.size) {
                val id = ByteBuffer.wrap(bytes, pos, 4).order(ByteOrder.LITTLE_ENDIAN).int
                val size = ByteBuffer.wrap(bytes, pos + 4, 4).order(ByteOrder.LITTLE_ENDIAN).int
                val body = pos + 8
                when (id) {
                    0x20746D66 -> { // "fmt "
                        if (body + 16 > bytes.size) return null
                        val f = ByteBuffer.wrap(bytes, body, 16).order(ByteOrder.LITTLE_ENDIAN)
                        audioFormat = f.short.toInt()
                        channels = f.short.toInt()
                        sampleRate = f.int
                        f.int // byte rate
                        f.short // block align
                        bitsPerSample = f.short.toInt()
                    }
                    0x61746164 -> { // "data"
                        dataStart = body
                        dataLen = size
                        break
                    }
                }
                if (size < 0) return null
                pos = body + size + (size and 1) // chunks are word-aligned
            }
            if (audioFormat != 1 && audioFormat != 3) return null
            if (channels <= 0 || sampleRate <= 0) return null
            if (dataStart < 0 || dataStart + dataLen > bytes.size) return null
            val frameSize = channels * (bitsPerSample / 8)
            if (frameSize <= 0) return null
            val frames = dataLen / frameSize
            if (frames <= 0) return null
            val samples = FloatArray(frames)
            val data = ByteBuffer.wrap(bytes, dataStart, dataLen)
                .order(ByteOrder.LITTLE_ENDIAN)
            for (f in 0 until frames) {
                var sum = 0f
                for (c in 0 until channels) {
                    val v: Float = when {
                        audioFormat == 1 && bitsPerSample == 16 ->
                            data.short / 32768f
                        audioFormat == 1 && bitsPerSample == 8 ->
                            (data.get().toInt() and 0xFF - 128) / 128f
                        audioFormat == 3 && bitsPerSample == 32 ->
                            data.float
                        else -> return null
                    }
                    sum += v
                }
                samples[f] = (sum / channels).coerceIn(-1f, 1f)
            }
            Decoded(samples, sampleRate)
        } catch (_: Exception) {
            null
        }
    }

    // ------------------------------------------------------------------
    // MediaExtractor + MediaCodec (WebM/Opus, M4A/AAC, anything else).
    // ------------------------------------------------------------------

    private fun decodeWithCodec(file: File): Decoded? {
        var extractor: MediaExtractor? = null
        var codec: MediaCodec? = null
        return try {
            extractor = MediaExtractor()
            extractor.setDataSource(file.path)
            var track = -1
            var format: MediaFormat? = null
            for (i in 0 until extractor.trackCount) {
                val f = extractor.getTrackFormat(i)
                val mime = f.getString(MediaFormat.KEY_MIME) ?: ""
                if (mime.startsWith("audio/")) {
                    track = i
                    format = f
                    break
                }
            }
            if (track < 0 || format == null) return null
            val mime = format.getString(MediaFormat.KEY_MIME) ?: return null
            val sampleRate = format.getInteger(MediaFormat.KEY_SAMPLE_RATE)
            val channels = format.getInteger(MediaFormat.KEY_CHANNEL_COUNT)
            extractor.selectTrack(track)
            codec = MediaCodec.createDecoderByType(mime)
            codec.configure(format, null, null, 0)
            codec.start()
            val out = ArrayList<Float>()
            val info = MediaCodec.BufferInfo()
            var inputDone = false
            var outputDone = false
            val timeoutUs = 10_000L
            while (!outputDone) {
                if (!inputDone) {
                    val inIdx = codec.dequeueInputBuffer(timeoutUs)
                    if (inIdx >= 0) {
                        val inBuf = codec.getInputBuffer(inIdx)!!
                        val n = extractor.readSampleData(inBuf, 0)
                        if (n < 0) {
                            codec.queueInputBuffer(
                                inIdx, 0, 0, 0,
                                MediaCodec.BUFFER_FLAG_END_OF_STREAM
                            )
                            inputDone = true
                        } else {
                            codec.queueInputBuffer(
                                inIdx, 0, n, extractor.sampleTime, 0
                            )
                            extractor.advance()
                        }
                    }
                }
                val outIdx = codec.dequeueOutputBuffer(info, timeoutUs)
                when {
                    outIdx >= 0 -> {
                        val outBuf = codec.getOutputBuffer(outIdx)!!
                        val pcm = ByteArray(info.size)
                        outBuf.get(pcm)
                        // 16-bit PCM, little-endian, interleaved.
                        val frames = info.size / 2 / max(1, channels)
                        val bb = ByteBuffer.wrap(pcm).order(ByteOrder.LITTLE_ENDIAN)
                        for (f in 0 until frames) {
                            var sum = 0f
                            for (c in 0 until channels) {
                                sum += bb.short / 32768f
                            }
                            out.add((sum / max(1, channels)).coerceIn(-1f, 1f))
                        }
                        codec.releaseOutputBuffer(outIdx, false)
                        if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) {
                            outputDone = true
                        }
                    }
                    outIdx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> { /* ignore */ }
                    outIdx == MediaCodec.INFO_TRY_AGAIN_LATER -> {
                        if (inputDone) outputDone = true
                    }
                }
            }
            if (out.isEmpty()) null else Decoded(out.toFloatArray(), sampleRate)
        } catch (_: Exception) {
            null
        } finally {
            try { codec?.stop() } catch (_: Exception) {}
            try { codec?.release() } catch (_: Exception) {}
            try { extractor?.release() } catch (_: Exception) {}
        }
    }
}
