package com.lukatdamoves.karaokegen

import android.media.MediaCodec
import android.media.MediaCodecList
import android.media.MediaExtractor
import android.media.MediaFormat
import java.io.File
import java.io.FileOutputStream
import java.nio.ByteBuffer

/**
 * Convert a downloaded audio file (WebM/Opus or M4A/AAC from YouTube)
 * to MP3, so the trial only ever deals with .mp3. YouTube does not
 * serve MP3 directly, so this decodes to PCM with MediaCodec and
 * re-encodes to MP3 with the platform MP3 encoder.
 *
 * Returns the MP3 file, or null when this phone has no MP3 encoder
 * (then the caller keeps the original file — the backend decodes any
 * format, so the draft still works).
 */
object AudioConvert {

    private const val TAG = "AudioConvert"

    /** True when this phone can encode MP3 at all. */
    fun canEncodeMp3(): Boolean {
        return try {
            val codecs = MediaCodecList(MediaCodecList.REGULAR_CODECS)
            val infos = codecs.codecInfos
            infos.any { info ->
                info.isEncoder && info.supportedTypes.any { type ->
                    type.equals("audio/mpeg", ignoreCase = true)
                }
            }
        } catch (e: Exception) {
            false
        }
    }

    /**
     * Transcode [input] to MP3 at [dest]. Returns true on success.
     * Any failure returns false (caller keeps the original file).
     * Runs on the calling thread — call from a background coroutine.
     */
    fun toMp3(input: File, dest: File): Boolean {
        val extractor = MediaExtractor()
        try {
            extractor.setDataSource(input.absolutePath)
        } catch (e: Exception) {
            AppLog.e(TAG, "convert: cannot open ${input.name}: ${e.message}")
            return false
        }
        var trackIndex = -1
        var format: MediaFormat? = null
        for (i in 0 until extractor.trackCount) {
            val f = extractor.getTrackFormat(i)
            val mime = f.getString(MediaFormat.KEY_MIME) ?: ""
            if (mime.startsWith("audio/")) {
                trackIndex = i
                format = f
                break
            }
        }
        if (trackIndex < 0 || format == null) {
            AppLog.e(TAG, "convert: no audio track in ${input.name}")
            extractor.release()
            return false
        }
        // Already MP3? Nothing to do.
        val mime = format.getString(MediaFormat.KEY_MIME) ?: ""
        if (mime.equals("audio/mpeg", ignoreCase = true)) {
            extractor.release()
            return false
        }
        extractor.selectTrack(trackIndex)
        val sampleRate = format.getInteger(MediaFormat.KEY_SAMPLE_RATE)
        val channelCount = format.getInteger(MediaFormat.KEY_CHANNEL_COUNT)

        val decoder: MediaCodec
        val encoder: MediaCodec
        try {
            decoder = MediaCodec.createDecoderByType(mime)
            encoder = MediaCodec.createEncoderByType("audio/mpeg")
        } catch (e: Exception) {
            AppLog.e(TAG, "convert: no codec: ${e.message}")
            extractor.release()
            return false
        }
        val outFormat = MediaFormat.createAudioFormat(
            "audio/mpeg", sampleRate, channelCount)
        outFormat.setInteger(MediaFormat.KEY_BIT_RATE, 128_000)
        var ok = false
        try {
            decoder.configure(format, null, null, 0)
            encoder.configure(outFormat, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            decoder.start()
            encoder.start()
            ok = pump(extractor, decoder, encoder, dest)
        } catch (e: Exception) {
            AppLog.e(TAG, "convert failed: ${e.message}")
            ok = false
        } finally {
            try { decoder.stop() } catch (_: Exception) {}
            try { decoder.release() } catch (_: Exception) {}
            try { encoder.stop() } catch (_: Exception) {}
            try { encoder.release() } catch (_: Exception) {}
            extractor.release()
        }
        if (!ok) {
            try { dest.delete() } catch (_: Exception) {}
        }
        return ok
    }

    /**
     * Pump: extractor -> decoder (PCM) -> encoder (MP3) -> [dest].
     * Returns true when the whole file was converted.
     */
    private fun pump(
        extractor: MediaExtractor,
        decoder: MediaCodec,
        encoder: MediaCodec,
        dest: File
    ): Boolean {
        val decoderIn = decoder.inputBuffers
        val decoderOut = decoder.outputBuffers
        val encoderIn = encoder.inputBuffers
        val encoderOut = encoder.outputBuffers
        val info = MediaCodec.BufferInfo()
        var extractorEos = false
        var decoderEos = false
        var encoderEos = false
        FileOutputStream(dest).use { out ->
            while (!encoderEos) {
                // Feed the decoder from the extractor.
                if (!extractorEos) {
                    val inIdx = decoder.dequeueInputBuffer(10_000)
                    if (inIdx >= 0) {
                        val buf = decoderIn[inIdx]
                        val n = extractor.readSampleData(buf, 0)
                        if (n < 0) {
                            decoder.queueInputBuffer(
                                inIdx, 0, 0, 0,
                                MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                            extractorEos = true
                        } else {
                            decoder.queueInputBuffer(
                                inIdx, 0, n,
                                extractor.sampleTime, 0)
                            extractor.advance()
                        }
                    }
                }
                // Drain the decoder (PCM) into the encoder.
                if (!decoderEos) {
                    val outIdx = decoder.dequeueOutputBuffer(info, 10_000)
                    if (outIdx >= 0) {
                        val pcm = decoderOut[outIdx]
                        val isEos = (info.flags and
                            MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0
                        if (info.size > 0) {
                            val chunk = ByteArray(info.size)
                            pcm.get(chunk)
                            feedEncoder(encoder, encoderIn, chunk)
                        }
                        decoder.releaseOutputBuffer(outIdx, false)
                        if (isEos) {
                            // End of PCM: signal EOS to the encoder.
                            val inIdx = encoder.dequeueInputBuffer(10_000)
                            if (inIdx >= 0) {
                                encoder.queueInputBuffer(
                                    inIdx, 0, 0, 0,
                                    MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                            }
                            decoderEos = true
                        }
                    }
                }
                // Drain the encoder (MP3) to the file.
                val encIdx = encoder.dequeueOutputBuffer(info, 10_000)
                if (encIdx >= 0) {
                    val mp3 = encoderOut[encIdx]
                    if (info.size > 0) {
                        val chunk = ByteArray(info.size)
                        mp3.get(chunk)
                        out.write(chunk)
                    }
                    if ((info.flags and
                            MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) {
                        encoderEos = true
                    }
                    encoder.releaseOutputBuffer(encIdx, false)
                }
            }
        }
        return true
    }

    /** Feed one PCM chunk into the encoder (may block briefly). */
    private fun feedEncoder(
        encoder: MediaCodec,
        encoderIn: Array<ByteBuffer>,
        pcm: ByteArray
    ) {
        var offset = 0
        while (offset < pcm.size) {
            val inIdx = encoder.dequeueInputBuffer(10_000)
            if (inIdx < 0) continue
            val buf = encoderIn[inIdx]
            val n = minOf(buf.remaining(), pcm.size - offset)
            buf.put(pcm, offset, n)
            offset += n
            encoder.queueInputBuffer(inIdx, 0, n, 0, 0)
        }
    }
}
