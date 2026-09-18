"""ffmpeg helpers for video uploads (local, CPU-only, no GPU).

A lyric video upload keeps the ORIGINAL video: the audio track is extracted
locally and sent to Modal for stem separation; the instrumental is then
remuxed back onto the original video (burned-in lyrics stay, vocals removed).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _ffmpeg(*args: str):
    kwargs: dict = dict(check=True, capture_output=True)
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
            **kwargs)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "ffmpeg not found on PATH — place ffmpeg.exe next to KaraokeGen.exe "
            "or install it") from exc
    except subprocess.CalledProcessError as exc:
        # surface ffmpeg's own error (no audio stream, corrupt file…) instead
        # of a bare "exit status 1" so the UI can show the real cause.
        try:
            err = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
        except Exception:
            err = ""
        tail = err[-500:] if err else "ffmpeg exited with status %s" % exc.returncode
        raise RuntimeError(f"ffmpeg failed: {tail}") from exc


def extract_audio(video_path: Path, dest: Path) -> Path:
    """Video -> mono 16kHz WAV (Whisper-native format, no decode overhead)."""
    _ffmpeg("-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", str(dest))
    return dest


def remux_karaoke(video_path: Path, instrumental_wav: Path, out_path: Path) -> Path:
    """Keep the original video track (burned-in lyrics), swap the audio for
    the instrumental stem, and re-encode the audio to AAC for the MP4."""
    _ffmpeg(
        "-i", str(video_path),
        "-i", str(instrumental_wav),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        str(out_path),
    )
    return out_path
