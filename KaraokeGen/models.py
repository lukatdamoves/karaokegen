"""Pydantic schemas: the single contract crossing Modal <-> local app.

Both sides serialize/deserialize these, so the alignment result is
GPU-agnostic and torch-free on the local side.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Compat shims for pydantic v1 (GPU image pins v1 for stable-ts)
if not hasattr(BaseModel, 'model_dump'):
    BaseModel.model_dump = BaseModel.dict
    BaseModel.model_validate = classmethod(lambda cls, v: cls.parse_obj(v))


class Word(BaseModel):
    text: str
    start: float
    end: float
    probability: float = 1.0


class Segment(BaseModel):
    """One lyric line = one segment. words are ordered left-to-right."""
    words: list[Word]
    start: float
    end: float
    text: str = ""


class LineNote(BaseModel):
    missing: int = 0
    miss_words: list[str] = Field(default_factory=list)
    ratio: float = 0.0
    status: Literal["aligned", "recovered", "approx", "not found"] = "aligned"
    text: str = ""


class AlignmentResult(BaseModel):
    segments: list[Segment]
    line_notes: list[LineNote] = Field(default_factory=list)


# --- Modal job API request/response models ---

# 200 MB base64 cap (~150 MB raw audio)
MAX_AUDIO_B64_LEN = 200_000_000
# Generous caps for lyric text (a whole album of lyrics is still tiny).
MAX_LYRICS_LEN = 200_000
# Render payload cap (alignment JSON; a real song is < 1 MB).
MAX_RENDER_JSON_LEN = 20_000_000


class DraftRequest(BaseModel):
    audio_b64: str = Field(max_length=MAX_AUDIO_B64_LEN)
    # ISO-ish short codes only ("tl", "en", "" = auto). Validated again
    # server-side; unknown values are rejected with 422.
    language: str = Field("tl", max_length=8)
    lyrics: str = Field("", max_length=MAX_LYRICS_LEN)
    backing_vocals: bool = False
    # Manual genre selection (owner decision): the editor always sends
    # "hiphop" (MMS-FA word path) or "ballad" (MMS-1B word path). No auto.
    genre: str = Field("hiphop", max_length=16)
    # Lead-in skip in seconds: live takes open with talk that is not lyrics.
    # Everything the aligner hears is copied with this much cut off the front
    # and the timings are shifted back, so no lyric line can anchor onto the
    # intro (the intro itself still plays in the render). 0 = align from 0:00.
    start_s: float = Field(0.0, ge=0, le=3600)


class DraftResult(BaseModel):
    job_id: str
    alignment: AlignmentResult
    lyrics: str
    report: str
    vocals_audio_b64: str
    duration: float


class RenderRequest(BaseModel):
    draft_job_id: str = Field(max_length=12)
    alignment: AlignmentResult
    final_lyrics: str = Field("", max_length=MAX_LYRICS_LEN)
    # Hex color only (validated again in render._parse_bg_color).
    bg_color: str = Field("#101010", max_length=16)
    output_name: str = Field("karaoke", max_length=64)
    # 2nd voice: mix the derived backing-voice stem into the instrumental bed.
    # Gain 1.0 == the karaoke model's own bed (preview parity guarantee).
    second_voice: bool = False
    second_voice_gain: float = Field(1.0, ge=0, le=4)
    # Highlight mode: 'line' = single \kf per line (production), 'word' = strict
    # per-word {\k} (word-only version). Same interface, runs separately via
    # word_level flag; line-level stays default so prod is untouched.
    word_level: bool = False
    highlight: Literal["line", "word"] = "line"


class RenderResult(BaseModel):
    file_id: str
    filename: str
    size_bytes: int
