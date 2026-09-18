"""CPU audio helpers: librosa onset/energy (timing polish) + ffmpeg wrappers.

Fixes the stale _AUDIO_CACHE bug: keyed on (path, mtime, size) so a
re-separation writing new bytes to the same path invalidates the cache.
"""
from __future__ import annotations

import logging
import subprocess
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np

from KaraokeGen.config import Settings, settings

log = logging.getLogger(__name__)


def run(cmd, **kw):
    subprocess.run(cmd, check=True, **kw)


def audio_duration(path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=True)
        return float(out.stdout.strip())
    except Exception as exc:
        log.debug("ffprobe failed for %s: %s", path, exc)
        return None


def prepare_asr_vocals(vocals_path, dest, s: Settings = settings) -> str:
    """mono 16 kHz (Whisper native format), highpassed + noise-gated.

    The gate zeros out low-level instrumental bleed between phrases so
    Whisper/stable-ts can't lock onto it as ghost speech during alignment.
    agate's threshold is a LINEAR amplitude (0-1), so convert from dB."""
    filt = []
    if s.asr_highpass_hz:
        filt.append("highpass=f=%g" % s.asr_highpass_hz)
    if s.asr_gate:
        lin = 10.0 ** (s.asr_gate_threshold_db / 20.0)
        filt.append("agate=threshold=%g:ratio=10:attack=10:release=250:makeup=1:range=0.0001"
                    % lin)
    filt.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(vocals_path),
        "-ac", "1", "-ar", "16000",
        "-af", ",".join(filt),
        str(dest),
    ])
    return str(dest)


def trim_audio(src, dest, start_s: float) -> str:
    """Copy `src` to `dest` with the first `start_s` seconds cut off.

    Live recordings open with talk that is NOT lyrics (mic checks, banter,
    crowd work). Trimming the stem the aligner hears keeps every downstream
    pass (stable-ts, LA lines, onset/env, CTC) from anchoring a lyric line
    onto that speech; the caller shifts the resulting timings back onto the
    full-song clock, so the intro still plays in the rendered video.

    Output is 16-bit PCM at the input's own rate/channels — the trim only
    has to be right to the frame, and `-ss` AFTER `-i` is the sample-exact
    seek (the fast pre-`-i` seek is not).
    """
    if float(start_s or 0.0) <= 0:
        return str(src)
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src), "-ss", "%.3f" % float(start_s),
        "-c:a", "pcm_s16le", str(dest),
    ])
    return str(dest)


def to_mp3_mono(src, dst, bitrate: str = "64k") -> str:
    """Downmix to mono mp3 for lightweight browser playback."""
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src), "-ac", "1", "-c:a", "libmp3lame", "-b:a", bitrate, str(dst),
    ])
    return str(dst)


# --- onset/energy for timing polish ---

_AUDIO_CACHE: OrderedDict[tuple, tuple[np.ndarray, int]] = OrderedDict()
_AUDIO_CACHE_LOCK = threading.Lock()


def _load_mono16(path, s: Settings = settings) -> tuple[np.ndarray, int]:
    st = Path(path).stat()
    key = (str(path), st.st_mtime, st.st_size)
    with _AUDIO_CACHE_LOCK:
        if key in _AUDIO_CACHE:
            _AUDIO_CACHE.move_to_end(key)
            return _AUDIO_CACHE[key]
    import librosa
    y, sr = librosa.load(str(path), sr=16000, mono=True)
    with _AUDIO_CACHE_LOCK:
        _AUDIO_CACHE[key] = (y, sr)
        _AUDIO_CACHE.move_to_end(key)
        while len(_AUDIO_CACHE) > 8:
            _AUDIO_CACHE.popitem(last=False)
    return y, sr


def detect_onsets(y, sr, hop: int = 160) -> np.ndarray:
    import librosa
    try:
        env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop, aggregate=np.median)
        frames = librosa.onset.onset_detect(onset_envelope=env, sr=sr, hop_length=hop, backtrack=True)
        return np.asarray(frames, dtype=np.float64) * (hop / float(sr))
    except Exception as exc:
        log.debug("onset detection failed: %s", exc)
        return np.asarray([])


# --- 2nd-voice stem: deprecated in simple flow (with_second_voice=False) ---
# Kept for backward compat; not called when run_draft uses with_second_voice=False.
# Do not delete until 2nd-voice feature is re-evaluated.

def decode_s16_stereo(path, sr: int = 44100) -> np.ndarray:
    """Decode any audio file to int16 stereo @ sr (resample as needed).

    Returns an (N, 2) int16 array. Raises RuntimeError on ffmpeg failure."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path),
         "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "2", "-ar", str(sr), "-"],
        capture_output=True)
    if out.returncode != 0 or not out.stdout:
        raise RuntimeError("ffmpeg decode failed for %s: %s" % (
            path, out.stderr.decode("utf-8", "replace")[:300]))
    return np.frombuffer(out.stdout, dtype="<i2").reshape(-1, 2)


def write_s16_wav(path, arr: np.ndarray, sr: int = 44100) -> str:
    """Write an (N, 2) int16 array as a 16-bit stereo WAV."""
    import wave
    a = np.asarray(arr, dtype="<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(a.tobytes())
    return str(path)


def derive_second_voice(inst_std, inst_bv, dest) -> str:
    """second_voice = karaoke_bed − clean_instrumental (sample-exact).

    The gabox karaoke model output (instrumental_bv) keeps every non-lead
    voice on top of the music; subtracting the clean instrumental leaves the
    2nd voice with no audible instrumental (the 3-stem model's own backing
    stem bleeds the music in and cannot be cleaned)."""
    a = decode_s16_stereo(inst_std)
    b = decode_s16_stereo(inst_bv)
    n = min(len(a), len(b))
    if n <= 0:
        raise RuntimeError("empty stems for second-voice derivation")
    diff = b[:n].astype(np.int32) - a[:n].astype(np.int32)
    np.clip(diff, -32768, 32767, out=diff)
    write_s16_wav(dest, diff)
    log.info("Derived 2nd-voice stem %s (%.1f s)", Path(dest).name, n / 44100.0)
    return str(dest)


def mix_bed_with_gain(inst_std, second_voice, gain: float, dest) -> str:
    """Bed = instrumental + 2nd voice × gain.

    gain 1.0 reconstructs the karaoke model output exactly (b == a + (b − a)),
    so the rendered MP4 voice level matches the editor preview, which applies
    the same gain to the same stem in the browser."""
    a = decode_s16_stereo(inst_std)
    b = decode_s16_stereo(second_voice)
    n = min(len(a), len(b))
    if n <= 0:
        raise RuntimeError("empty stems for bed mixing")
    mixed = a[:n].astype(np.int32) + np.round(b[:n].astype(np.float64) * float(gain)).astype(np.int32)
    np.clip(mixed, -32768, 32767, out=mixed)
    write_s16_wav(dest, mixed)
    return str(dest)
