"""GPU pipeline: stem separation, Whisper ASR, stable-ts line alignment + refine.

All torch/audio-separator/stable-ts imports are lazy (inside functions) so
this module can be imported on a torch-free host without error - only the
actual GPU calls require the deps.

Separation: 3-stem karaoke model, one pass. bs_karaoke_3stem_giantailab
produces lead vocals + backing vocals + instrumental. When align_ensemble
is on, a second mel-band-roformer karaoke pass extracts a cleaner lead-only
stem (vocals_lead) that ASR/alignment consume instead of vocals_raw.

Alignment: plain stable-ts full-text alignment (model.align + single-pass
refine), then the line-polish passes (ghost filter, verbatim remap, onset
snap, end extension/trim, degenerate expansion). Word-level CTC refinement
(word_ctc.refine_word_timings) and the LA-Multilingual line arbitration
(ensemble.align_lyrics_ensemble) run on top of this — see those modules.
"""
from __future__ import annotations

import gc
import logging
import os
import re
import shutil
from pathlib import Path

import numpy as np

from KaraokeGen.audio import prepare_asr_vocals
from KaraokeGen.config import Settings, settings
from KaraokeGen.lyrics import word_text
from KaraokeGen.models import AlignmentResult, Segment, Word
from KaraokeGen.render import from_stable_ts

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# paths (live in the Modal container work dir / Volume)
# ---------------------------------------------------------------------------

def stem_paths(work: Path, s: Settings = settings):
    return {
        "input_wav": work / "input_audio.wav",
        "vocals_raw": work / "vocals_raw.wav",
        "vocals_asr": work / "vocals_asr.wav",
        "vocals_lead": work / "vocals_lead.wav",
        "instrumental_std": work / "instrumental_std.wav",
        "instrumental_bv": work / "instrumental_bv.wav",
        "second_voice": work / "second_voice.wav",
        "backing_vocals": work / "backing_vocals.wav",
        "stem_meta": work / "stems_meta.txt",
        "sep_dir": work / "models",
    }


# ---------------------------------------------------------------------------
# stem separation (audio-separator)
# ---------------------------------------------------------------------------

def _quiet_third_party():
    import warnings
    warnings.filterwarnings("ignore")
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TQDM_DISABLE", "1")
    import logging as _logging
    for name in (
        "audio_separator", "audio_separator.separator",
        "audio_separator.separator.separator",
        "audio_separator.separator.architectures",
        "audio_separator.separator.roformer",
        "torchaudio", "torch", "httpx", "urllib3", "filelock",
        "huggingface_hub", "huggingface_hub.file_download",
    ):
        _logging.getLogger(name).setLevel(_logging.ERROR)
        _logging.getLogger(name).propagate = False


def _new_separator(work: Path, s: Settings = settings):
    _quiet_third_party()
    from audio_separator.separator import Separator
    kwargs = dict(
        model_file_dir=str(s.model_cache_dir),
        output_dir=str(work),
        output_format="WAV",
        use_autocast=True,
    )
    try:
        # process_all_stems: multi-stem models must write EVERY stem in
        # training.instruments (3-stem karaoke model needs vocals_raw +
        # backing_vocals + instrumental_std). Older/newer releases default
        # this differently; pass it explicitly so the instrumental stem is
        # never silently dropped.
        return Separator(log_level=logging.ERROR,
                         mdxc_params={"process_all_stems": True}, **kwargs)
    except TypeError:
        # audio-separator <0.34 has no mdxc_params kwarg
        return Separator(**kwargs)


def _release_separator(sep):
    try:
        if sep is not None and hasattr(sep, "model_instance"):
            del sep.model_instance
    except Exception as exc:
        log.debug("release separator: %s", exc)
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
    gc.collect()


def _cleanup_unused_stems(work: Path):
    for p in work.glob("unused_*.wav"):
        try:
            p.unlink()
        except Exception:
            pass


def _pick_stem(paths, keywords, preferred_name: str, work: Path):
    """Prefer the requested name, then match output stems by keyword, then the
    newest wav in the work dir (excluding input_audio.wav)."""
    preferred = work / preferred_name
    if preferred.exists() and preferred.stat().st_size > 0:
        return preferred
    for p in paths or []:
        pp = Path(p)
        if any(k in pp.name.lower() for k in keywords):
            return pp
    wavs = [p for p in work.glob("*.wav") if p.name != "input_audio.wav"]
    return max(wavs, key=lambda x: x.stat().st_mtime) if wavs else None


def _run_model(model_filename, audio_path, name_map, work: Path, s: Settings = settings):
    sep = _new_separator(work, s)
    try:
        sep.load_model(model_filename=model_filename)
        return sep.separate(audio_path, name_map)
    finally:
        _release_separator(sep)


def dual_separate(path, work: Path, s: Settings = settings, cb=None, with_second_voice: bool = True):
    """Returns (vocals_asr_path, instrumental_std_path).

    3-stem karaoke model: one pass produces vocals_raw (lead vocal
    only), backing_vocals (isolated backing vocals), and instrumental_std
    (clean instrumental bed).

    with_second_voice: additionally runs the karaoke bed model
    (mel_band_roformer_karaoke_gabox) whose output keeps every non-lead voice
    on top of the music (instrumental_bv), then derives second_voice = bv − std
    — the music-free 2nd voice for the editor preview and the render bed.
    cb is an optional progress helper."""
    _quiet_third_party()
    work = Path(work)

    if cb:
        cb.set_detail("Loading 3-stem karaoke model...")

    # 3-stem output: vocals (lead), backing_vocal, instrumental
    _run_model(
        s.vocal_model, path,
        {"Vocals": "vocals_raw", "Backing_vocal": "backing_vocals", "Instrumental": "instrumental_std"},
        work, s,
    )

    vocals_raw = work / "vocals_raw.wav"
    backing_vocals = work / "backing_vocals.wav"
    instrumental_std = work / "instrumental_std.wav"

    for _name, _p in (("vocals", vocals_raw), ("backing vocals", backing_vocals),
                      ("instrumental", instrumental_std)):
        if not _p.exists():
            produced = sorted(q.name for q in work.glob("*.wav"))
            raise FileNotFoundError(
                "No %s stem produced in %s (files present: %s)"
                % (_name, work, produced or "none"))

    _cleanup_unused_stems(work)

    # Extra mel-band-roformer-karaoke pass on the MIX -> true lead-only
    # stem. Alignment (whisper + LA ensemble) consumes THIS, not
    # vocals_raw, so backing-vocal bleed can't drag the anchors.
    if s.align_ensemble:
        p = stem_paths(work, s)
        try:
            if cb:
                cb.set_detail("Extracting clean lead vocal (karaoke model)...")
            _run_model(
                s.lead_model, path,
                {"Vocals": "vocals_lead", "Instrumental": "unused_instr_lead"},
                work, s,
            )
            _cleanup_unused_stems(work)
            if not p["vocals_lead"].exists():
                raise FileNotFoundError("lead stem missing after karaoke pass")
            log.info("Lead-stem pass complete (%s).", s.lead_model)
        except Exception as exc:
            # never fail the job over the enhancement: fall back to 3-stem vocal
            log.warning("Lead-stem pass failed (%s); using vocals_raw.", exc)
            shutil.copy2(p["vocals_raw"], p["vocals_lead"])

        source_for_asr = p["vocals_lead"]
    else:
        source_for_asr = work / "vocals_raw.wav"

    vocals_asr = prepare_asr_vocals(source_for_asr, work / "vocals_asr.wav", s)

    if with_second_voice:
        from KaraokeGen.audio import derive_second_voice
        if cb:
            cb.set_detail("Loading karaoke model (extracting 2nd voice)...")
        karaoke_out = _run_model(
            s.karaoke_bed_model, path,
            {"Instrumental": "instrumental_bv", "Vocals": "unused_vocals_bv",
             "Other": "unused_other_bv"},
            work, s,
        )
        instrumental_bv = work / "instrumental_bv.wav"
        if not instrumental_bv.exists():
            picked = _pick_stem(karaoke_out,
                                ["instrument", "karaoke", "no_vocal", "backing"],
                                "instrumental_bv.wav", work)
            if picked is None or not picked.exists():
                raise FileNotFoundError(
                    "No karaoke-bed stem produced. Outputs: %s" % karaoke_out)
            shutil.copy2(picked, instrumental_bv)
        _cleanup_unused_stems(work)
        derive_second_voice(instrumental_std, instrumental_bv,
                            work / "second_voice.wav")

    if cb:
        cb.set_detail("Separation complete.")
    _write_stem_meta(work, s, with_second_voice)
    log.info("Separation done (3-stem + karaoke bed %s).",
             "with 2nd-voice" if with_second_voice else "instrumental-only")
    return vocals_asr, instrumental_std


def _stem_meta(s: Settings = settings, with_second_voice: bool = True) -> str:
    # Include the ASR pre-clean params so cached vocals_asr.wav (made with a
    # different gate/highpass) get re-separated instead of silently reused.
    # The karaoke model + flag are part of the key so lineup changes
    # invalidate cached stems.
    return "|".join([
        s.vocal_model,
        s.lead_model if s.align_ensemble else "-",
        "ens" if s.align_ensemble else "noens",
        s.karaoke_bed_model if with_second_voice else "-",
        "2nd" if with_second_voice else "no2nd",
        "hp%g" % s.asr_highpass_hz,
        "gt%g" % (s.asr_gate_threshold_db if s.asr_gate else 0.0),
    ])


# --- lead-in skip (live recordings) ---------------------------------------
# A live take usually opens with talk that is NOT lyrics ("check check",
# "what's up guys", banter). Deleting those lines from the lyrics box does
# NOT delete the audio: the aligner still hears several seconds of speech
# with real vocal energy, and every anchoring pass (stable-ts, LA line
# starts, onset/env arbitration, CTC windows) can pull the first lyric
# lines onto it. The fix is to remove the intro from the audio the aligner
# hears and put the timings back on the full-song clock afterwards — see
# trim_lead_in + shift_result.
_ALIGN_INPUTS = (
    ("vocals_asr", "vocals_asr.wav"),
    ("vocals_lead", "vocals_lead.wav"),
    ("vocals_raw", "vocals_raw.wav"),
    ("mix", "input_audio.wav"),
)


def trim_lead_in(work, start_s: float, s: Settings = settings) -> dict[str, str]:
    """Trimmed copies of every audio input alignment consumes.

    Returns {logical name: path} for vocals_asr / vocals_lead / vocals_raw /
    mix. start_s <= 0 (or a failing trim) returns the untrimmed paths, so
    callers can index the mapping unconditionally.
    """
    work = Path(work)
    out = {name: str(work / fname) for name, fname in _ALIGN_INPUTS}
    if float(start_s or 0.0) <= 0:
        return out
    from KaraokeGen.audio import trim_audio
    dest_dir = work / "song"
    dest_dir.mkdir(parents=True, exist_ok=True)
    for name, fname in _ALIGN_INPUTS:
        src = work / fname
        if not src.exists() or src.stat().st_size == 0:
            continue
        try:
            out[name] = trim_audio(src, dest_dir / fname, start_s)
        except Exception as exc:
            log.warning("Lead-in trim failed for %s (%s); using it untrimmed.", fname, exc)
    return out


def shift_result(result: AlignmentResult, delta: float) -> AlignmentResult:
    """Move every segment and word by `delta` seconds.

    Counterpart of trim_lead_in: alignment ran on stems that start `delta`
    seconds into the song, so this puts the result back on the full-song
    clock the editor and the renderer use. Never raises."""
    if result is None or not delta:
        return result
    try:
        from KaraokeGen.lyrics import _shift_segment
        for seg in result.segments:
            _shift_segment(seg, float(delta))
        log.info("Lead-in skip: shifted all timings by +%.2fs.", float(delta))
    except Exception as exc:
        log.warning("Lead-in shift failed (%s); timings left on the trimmed clock.", exc)
    return result


def _write_stem_meta(work, s: Settings = settings, with_second_voice: bool = True):
    (work / "stems_meta.txt").write_text(_stem_meta(s, with_second_voice))


def stems_reusable(work, s: Settings = settings, with_second_voice: bool = True) -> bool:
    work = Path(work)
    p = stem_paths(work, s)
    needed = [p["vocals_raw"], p["vocals_asr"], p["instrumental_std"], p["backing_vocals"]]
    if s.align_ensemble:
        needed += [p["vocals_lead"]]
    if with_second_voice:
        needed += [p["instrumental_bv"], p["second_voice"]]
    # A 0-byte file is never a valid stem (e.g. left behind by a killed
    # container) — re-separate instead of reusing a truncated cache.
    if not all(x.exists() and x.stat().st_size > 0 for x in needed):
        return False
    if not p["input_wav"].exists() or p["input_wav"].stat().st_size == 0:
        return False
    input_mt = p["input_wav"].stat().st_mtime
    if not all(x.stat().st_mtime >= input_mt for x in needed):
        return False
    meta = p["stem_meta"].exists() and p["stem_meta"].read_text()
    return meta == _stem_meta(s, with_second_voice)


# ---------------------------------------------------------------------------
# Whisper ASR
# ---------------------------------------------------------------------------

_WHISPER_MODEL = None


def load_whisper(s: Settings = settings):
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        import stable_whisper
        _WHISPER_MODEL = stable_whisper.load_faster_whisper(
            "large-v3", device="cuda", compute_type="float16")
    return _WHISPER_MODEL


def _asr_prompt() -> str | None:
    """Optional whisper initial_prompt from the KVC_ASR_PROMPT env var
    (capped at 400 chars). None = no prompt."""
    prompt = (os.environ.get("KVC_ASR_PROMPT", "") or "").strip()
    return prompt[:400] if prompt else None


# --- RMS-VAD long-form segmentation ---------------------------------------
# Syed et al. 2025 (arXiv:2506.15514, AudioShake/QMUL): whisper's native
# long-form algorithm advances its 30s window by its own predicted
# timestamps, which are inaccurate on singing -> windows cut mid-phrase ->
# hallucination loops + deletions. Their RMS-VAD derives segment
# boundaries from the amplitude of the (already separated) vocal stem
# instead: VAD[n] = RMS[n]/max(RMS), Cut&Merge (onset/offset 0.1, min
# silence 1s, max segment 30s, merge gap 4s so ballads don't shatter into
# per-line short samples - the paper's known-bad short-sample regime).
# NOT a vad_filter that removes audio; this only places boundaries on a
# vocals-only stem and transcribes every segment whole. Kill switch:
# KVC_ASR_RMSVAD=off.
class _MergedASR:
    def __init__(self, segments, text):
        self.segments = segments
        self.text = text


def _rms_vad_segments(y, sr, thr=0.1, min_sil_s=1.0, max_seg_s=30.0,
                      merge_gap_s=4.0):
    """Cut&Merge-style vocal-activity segmentation from normalized RMS."""
    hop = int(sr * 0.1)          # 100 ms resolution
    frame = int(sr * 0.2)
    if len(y) < frame:
        return []
    n = 1 + (len(y) - frame) // hop
    rms = np.array([np.sqrt(np.mean(y[i * hop:i * hop + frame] ** 2) + 1e-12)
                    for i in range(n)])
    vad = rms / max(float(rms.max()), 1e-9)
    active = vad >= thr
    sil_frames = int(min_sil_s / 0.1)

    segs = []
    i = 0
    while i < n:
        if not active[i]:
            i += 1
            continue
        j, last = i, i
        while j < n:
            if active[j]:
                last = j
            elif j - last > sil_frames:
                break
            j += 1
        segs.append((i * hop, min(len(y), (last + 1) * hop)))
        i = last + sil_frames + 1

    # split overlong segments at the quietest internal frame (recursively)
    out = []

    def _split(a, b):
        if (b - a) <= max_seg_s * sr:
            out.append((a, b))
            return
        mid_lo = a + int((b - a) * 0.25)
        mid_hi = a + int((b - a) * 0.75)
        k = mid_lo // hop + int(np.argmin(rms[mid_lo // hop:mid_hi // hop]))
        cut = max(a + int(2.0 * sr), min(b - int(2.0 * sr), k * hop))
        _split(a, cut)
        _split(cut, b)

    for a, b in segs:
        _split(a, b)

    # merge consecutive segments while the span stays <= 30s (gap <= 4s;
    # big instrumental breaks stay cut)
    merged = []
    for a, b in out:
        if (merged and b - merged[-1][0] <= max_seg_s * sr
                and a - merged[-1][1] <= merge_gap_s * sr):
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged


def _transcribe_segmented(model, vocals, kwargs):
    """Per-segment short-form decode over RMS-VAD boundaries, timestamps
    rebased onto the song clock. Any failure falls back to whole-file."""
    try:
        import soundfile as sf
        y, sr = sf.read(str(vocals), dtype="float32")
        if getattr(y, "ndim", 1) > 1:
            y = y.mean(axis=1)
        segs = [s for s in _rms_vad_segments(y, sr)
                if s[1] - s[0] >= int(0.3 * sr)]
        if segs:
            parts = []
            for a, b in segs:
                r = model.transcribe(np.ascontiguousarray(y[a:b]), **kwargs)
                for seg in getattr(r, "segments", []) or []:
                    off = a / sr
                    try:
                        seg.start = float(getattr(seg, "start", 0.0) or 0.0) + off
                        seg.end = float(getattr(seg, "end", 0.0) or 0.0) + off
                    except Exception:
                        pass
                    for w in getattr(seg, "words", None) or []:
                        for at in ("start", "end"):
                            v = getattr(w, at, None)
                            if v is not None:
                                try:
                                    setattr(w, at, float(v) + off)
                                except (TypeError, ValueError):
                                    pass
                    parts.append(seg)
            if parts:
                return _MergedASR(
                    parts, " ".join(str(getattr(p, "text", "")).strip()
                                    for p in parts))
    except Exception as exc:
        log.info("RMS-VAD segmented decode failed (%s) - whole-file", exc)
    return model.transcribe(vocals, **kwargs)


def transcribe_vocals(model, vocals, s: Settings = settings,
                      auto_lang: bool = False):
    # Production recipe: VAD OFF + temperature fallback. Speech-tuned VAD
    # chops sustained sung notes and drops whole verses; without VAD
    # pre-filtering the fallback's compression/logprob gates actually
    # engage on confabulated segments.
    kwargs = dict(
        language=None if auto_lang else (s.language or None),
        task="transcribe",
        word_timestamps=True,
        vad_filter=False,
        vad_parameters=dict(min_silence_duration_ms=300,
                             speech_pad_ms=200,
                             threshold=0.35),
        temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
        condition_on_previous_text=False,
        no_speech_threshold=0.35,
        compression_ratio_threshold=2.6,
        beam_size=5,
    )
    prompt = _asr_prompt()
    if prompt:
        kwargs["initial_prompt"] = prompt
    if (os.environ.get("KVC_ASR_RMSVAD", "on").strip().lower()
            not in ("off", "0", "false")):
        return _transcribe_segmented(model, vocals, kwargs)
    return model.transcribe(vocals, **kwargs)


def _mean_word_conf(result) -> float:
    probs = []
    for seg in getattr(result, "segments", []) or []:
        for w in getattr(seg, "words", None) or []:
            p = getattr(w, "probability", None)
            if p is not None:
                try:
                    probs.append(float(p))
                except (TypeError, ValueError):
                    pass
    return sum(probs) / len(probs) if probs else 0.0


# --- transcript canonicalization ("one word for sound-alikes") ---
# Owner directive: pick ONE lyric-display spelling for common Tagalog
# sound-alikes; formal grammatical distinctions are intentionally not
# preserved. Standard forms are used where they do not conflict with lyric
# convention (paano, puwede, kaniyang); contracted lyric forms are used
# where song convention strongly prefers them (di, yung, pag, oh).
# Number-word maps were removed: "to" is frequently English/Taglish and
# must never become "2".
_CANON_T1 = {
    "pwede": "puwede",
    "pano": "paano", "pa'no": "paano",
    "kanyang": "kaniyang",
    "nang": "ng",
    "di": "di",
    "yung": "yung", "iyong": "yung", "yong": "yung",
    "pag": "pag",
    "o": "oh", "ooh": "oh",
}
# The former "full" tier is now the owner-approved default. Keep the env
# switch for rollback/forward extensions without changing its public shape.
_CANON_T2 = {}

_CANON_TOKEN_RE = re.compile(r"^([^\w']*)([\w']+)([^\w']*)$")
_APOSTROPHE_RE = re.compile(r"['\u2019]")


def _copy_case(source: str, target: str) -> str:
    """Copy the first alphabetic case from source onto target."""
    src = next((c for c in source if c.isalpha()), "")
    if not src.isupper():
        return target
    for i, c in enumerate(target):
        if c.isalpha():
            return target[:i] + c.upper() + target[i + 1:]
    return target


# Tagalog/Taglish enclitic "y" (e.g. "ako'y") is often emitted as a
# separate token. Joining it keeps displayed lyrics pure letters while
# preserving the base word's timing span.
_Y_CLITIC_BASES = {"ako", "ba", "ka", "ko", "mo", "na", "nga",
                  "pa", "siya", "tayo", "ika"}
_Y_CLITIC_BASE_RE = re.compile(
    r"^(?:" + "|".join(
        re.escape(b) for b in sorted(_Y_CLITIC_BASES, key=len, reverse=True))
    + r")[^\w']*$", re.IGNORECASE)


def _join_split_clitic(token: str, prev: str) -> str:
    """Join a lone y back to the preceding Tagalog base token."""
    if re.fullmatch(r"[Yy][^\w]*", token or "") and _Y_CLITIC_BASE_RE.match(prev or ""):
        core = re.sub(r"[^\w']", "", prev.lstrip())
        return core + ("Y" if token[0].isupper() else "y")
    return token


def _canonical_token(token: str, table: dict[str, str]) -> str:
    """Map one token while preserving surrounding punctuation and case."""
    m = _CANON_TOKEN_RE.match(token or "")
    if not m:
        return token
    pre, core, post = m.groups()
    # Owner preference: lyric words are pure letters. Strip apostrophes from
    # the word core, but keep sentence punctuation around the token.
    core = _APOSTROPHE_RE.sub("", core)
    key = core.lower()
    if key in SLANG_PROTECT:
        return token
    return pre + _copy_case(core, table.get(key, core)) + post


def _canon_mode(s: Settings = settings) -> str:
    return (os.environ.get("KVC_ASR_CANON", "") or "").strip().lower()


# Slang PROTECT set: tokens a corrector must never "fix" into formal
# words. Mined 2026-09-08: Gen Z seeds x TagaSenti counts x bench vocab.
# Audit result: lexical slang is nearly ABSENT from the 26 bench songs
# (only marupok/kilig/haha/tea) - so this set is a no-op guard today and
# becomes load-bearing the day an LLM corrector (Phase 4b) touches text:
# vanilla LLMs formalize slang (amats->promise class), a slang-fluent
# brain (Mazoku-8B-Qwen3, CPT on PH forums) must be used instead.
SLANG_PROTECT = frozenset(
    "accla charot chariz dasurv korique lods petmalu werpa eme jowa "
    "fafa mima mars baks teh mhie chz dzuh awit sanaol skrrt slay rizz "
    "delulu lowkey ghosting redflag marupok kilig hugot olats yawa atik "
    "gagi leche bwiset pucha shuta engot jeje haha bestie periodt shook "
    "tea shade cancel ate kuya bunso bagets toxic amats norem norim".split())


# Confab signatures. Whisper emits these when it hallucinates a
# video-description / outro over instrumentals; they must never reach
# displayed lyrics. High-precision list only — extend with evidence, never
# speculatively.
CONFAB_PHRASES = [
    "thank you for watching", "thanks for watching",
    "thank you for listening", "thanks for listening",
    "don't forget to subscribe", "please subscribe",
    "subscribe for more", "thanks for subscribing",
    "please like and subscribe", "like and subscribe",
    "thank you so much for watching",
]
_CONFAB_RE = re.compile(
    "(" + "|".join(re.escape(p) for p in CONFAB_PHRASES) + ")",
    re.IGNORECASE)
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\u0400-\u04ff]")


def strip_confab_result(result):
    """Remove outro/confab hallucination phrases from segment texts and drop
    CJK fragment tokens. Word objects inside a matched span are dropped too
    (their spans are hallucinated). Never raises."""
    try:
        for seg in getattr(result, "segments", []) or []:
            try:
                txt = getattr(seg, "text", None)
                if txt:
                    txt = " ".join(str(txt).split())
                    if _CONFAB_RE.search(txt):
                        txt = _CONFAB_RE.sub(" ", txt)
                    if _CJK_RE.search(txt):
                        txt = _CJK_RE.sub(" ", txt)
                    if txt != getattr(seg, "text", None):
                        seg.text = txt
            except Exception:
                pass
            words = getattr(seg, "words", None) or []
            if not words:
                continue
            # word-level sweep: drop words belonging to a confab phrase
            # (handles punctuation attached to words and phrase tokens
            # split across word boundaries) + CJK tokens.
            toks = []
            for w in words:
                v = (getattr(w, "word", "") or "") or (getattr(w, "text", "") or "")
                toks.append(str(v).strip().strip("'\".,!?").lower())
            drop = [False] * len(words)
            for i, t in enumerate(toks):
                if t and _CJK_RE.search(t):
                    drop[i] = True
            nz = [i for i, t in enumerate(toks) if t]
            for phrase in CONFAB_PHRASES:
                ptoks = phrase.split()
                j = 0
                while j + len(ptoks) <= len(nz):
                    if all(toks[nz[j + k]] == ptoks[k]
                           for k in range(len(ptoks))):
                        for k in range(len(ptoks)):
                            drop[nz[j + k]] = True
                        j += len(ptoks)
                    else:
                        j += 1
            if any(drop):
                kept = [w for w, d in zip(words, drop) if not d]
                try:
                    seg.words = kept
                    seg.text = " ".join(word_text(w) for w in kept).strip()
                except Exception:
                    pass
    except Exception as exc:
        log.debug("strip_confab: %s", exc)
    return result


def canonicalize_result(result, s: Settings = settings):
    """Rewrite segment/word texts to canonical spellings. Never raises:
    a display normalizer must not break transcription."""
    try:
        mode = _canon_mode(s)
        table = dict(_CANON_T1)
        if mode == "full":
            table.update(_CANON_T2)
        elif mode == "off":
            return result
        for seg in getattr(result, "segments", []) or []:
            try:
                words = list(getattr(seg, "words", None) or [])
                merged_words = []
                for w in words:
                    for attr in ("text", "word"):
                        try:
                            v = getattr(w, attr, None)
                            if not v:
                                continue
                            setattr(w, attr, _canonical_token(str(v), table))
                        except Exception:
                            pass
                    if not merged_words:
                        merged_words.append(w)
                        continue
                    prev = merged_words[-1]
                    cur = str(getattr(w, "text", "") or
                              getattr(w, "word", ""))
                    pv = str(getattr(prev, "text", "") or
                             getattr(prev, "word", ""))
                    nxt = _join_split_clitic(cur, pv)
                    if nxt != cur:
                        try:
                            setattr(prev, "text", nxt)
                        except Exception:
                            pass
                        try:
                            setattr(prev, "word", nxt)
                        except Exception:
                            pass
                        try:
                            prev.end = float(getattr(w, "end", prev.end))
                        except (TypeError, ValueError):
                            pass
                        try:
                            p0 = float(getattr(prev, "probability", 0.0)
                                       or 0.0)
                            p1 = float(getattr(w, "probability", 0.0) or 0.0)
                            setattr(prev, "probability", max(p0, p1))
                        except (TypeError, ValueError):
                            pass
                        continue
                    merged_words.append(w)
                if merged_words != words:
                    seg.words = merged_words
                seg.text = " ".join(
                    str(getattr(w, "text", "") or
                        getattr(w, "word", "")).strip()
                    for w in merged_words).strip()
                if not merged_words and getattr(seg, "text", None):
                    seg.text = str(seg.text).strip()
            except Exception:
                pass
    except Exception as exc:
        log.debug("canonicalize: %s", exc)
    return result


def transcribe_best(model, s: Settings = settings, vocals_asr: str | None = None):
    """ASR on vocals_asr.wav. If the transcript's mean word confidence lands
    below the threshold, the fixed language choice is likely hurting
    (code-switched Taglish): retry with Whisper auto-detected language and
    keep the better transcript.

    vocals_asr overrides the file to transcribe (the lead-in-trimmed copy —
    see trim_lead_in — so a spoken intro never reaches the transcript)."""
    vocals_asr = str(vocals_asr or (s.work_dir / "vocals_asr.wav"))
    best = transcribe_vocals(model, vocals_asr, s)
    best_conf = _mean_word_conf(best)
    # With RMS-VAD segmentation the conf-fallback to auto-language is
    # harmful (cold-start segments depress mean conf -> fallback triggers
    # often and the auto pass wins on conf while losing on text). Default:
    # fb runs only on the native path; KVC_ASR_CONF_FB=on/off forces
    # either way.
    rmsvad_on = (os.environ.get("KVC_ASR_RMSVAD", "on").strip().lower()
                 not in ("off", "0", "false"))
    fb = os.environ.get("KVC_ASR_CONF_FB", "").strip().lower()
    run_fb = (fb in ("on", "1", "true")) if fb else (not rmsvad_on)
    if best_conf < s.asr_conf_fallback and run_fb:
        log.info("  mean word confidence %.3f < %.3f — retrying with auto-detected language",
                 best_conf, s.asr_conf_fallback)
        res = transcribe_vocals(model, vocals_asr, s, auto_lang=True)
        res_conf = _mean_word_conf(res)
        if res_conf > best_conf:
            log.info("  auto-language transcript better (%.3f vs %.3f).", res_conf, best_conf)
            best = res
    return canonicalize_result(strip_confab_result(best), s)


# ---------------------------------------------------------------------------
# plain stable-ts full-text alignment (production path)
# ---------------------------------------------------------------------------


def _replace_asr_with_official(result: AlignmentResult, text: str) -> AlignmentResult:
    """Replace ASR word text with official lyrics by POSITIONAL MATCHING.

    With original_split=True, segment[i] == lyric line[i]. This function
    assigns the words of each lyric line to its matching segment.

    If segment count != line count (edge case: a line had zero aligned words
    and got dropped by the ghost filter), surviving segments match lines by
    position: segment 0 -> line 0, segment 1 -> line 1, etc. Unmatched lines
    at the end are lost (they had no aligned words anyway).

    Within each segment the original refined word boundaries are preserved —
    only the text changes. Words are stretched/compressed to fit the official
    count while preserving the segment's overall span."""
    official_lines = [ln.strip() for ln in (text or "").split("\n") if ln.strip()]
    if not official_lines or not result.segments:
        return result

    new_segs: list[Segment] = []
    for i, seg in enumerate(result.segments):
        if i < len(official_lines):
            assigned = official_lines[i].split()
        else:
            assigned = []
        keep = [w for w in seg.words if w is not None]
        if not assigned:
            if keep:
                new_segs.append(Segment(
                    words=keep, start=seg.start, end=seg.end,
                    text=seg.text or " ".join(word_text(w) for w in keep)))
            continue
        n = len(assigned)
        boundaries = []
        for w in keep:
            boundaries.append((float(getattr(w, "start", 0)), float(getattr(w, "end", 0))))
        if len(boundaries) < n:
            span = max(0.05, seg.end - seg.start)
            boundaries = [(seg.start + span * k / n, seg.start + span * (k + 1) / n) for k in range(n)]
        elif len(boundaries) > n:
            boundaries = boundaries[:n - 1] + [(boundaries[n - 1][0], boundaries[-1][1])]
        probs = [_word_prob(w) for w in keep]
        prob = (sum(probs) / len(probs)) if probs else 1.0
        words = [
            Word(text=txt, start=boundaries[k][0], end=boundaries[k][1], probability=prob)
            for k, txt in enumerate(assigned)
        ]
        new_segs.append(Segment(
            words=words, start=seg.start, end=seg.end,
            text=" ".join(assigned)))
    return AlignmentResult(segments=new_segs, line_notes=result.line_notes)


def _sync_segment_boundaries(result: AlignmentResult) -> AlignmentResult:
    """After official-text remap, sync each segment's start/end to its
    first/last word. The renderer uses seg.start/seg.end for the \\kf
    fill and line display — they must match the actual word coverage."""
    for seg in result.segments:
        if seg.words:
            seg.start = float(seg.words[0].start)
            seg.end = float(seg.words[-1].end)
    return result


def _clamp_stretched_words(result: AlignmentResult, audio_path, s: Settings = settings) -> AlignmentResult:
    """Collapse words stretched across a structurally impossible intra-line gap.

    On repeated lyrics the aligner can anchor a word onto a LATER occurrence
    of the same text, stretching the line across the interlude — e.g. 'Ikaw,
    ikaw ay dilaw' got 'dilaw' at [139.36-139.66] while the previous word
    ended at 125.23 (a 14s intra-line gap). Continuous singing never has a
    3s+ gap between consecutive words of one line, regardless of what vocal
    energy fills the space (echo phrases are a DIFFERENT line's audio).

    Purely structural rule (audio energy cannot distinguish echo fills):
    a segment spanning > clamp_seg_span_s whose word arrives more than
    clamp_word_gap_s after the previous word collapses onto the previous
    word's end (the line's real end).
    """
    seg_span_max_s = float(getattr(s, "clamp_seg_span_s", 8.0))
    word_gap_trigger_s = float(getattr(s, "clamp_word_gap_s", 3.0))
    for seg in result.segments:
        if not seg.words:
            continue
        if float(seg.end) - float(seg.start) <= seg_span_max_s:
            continue
        for wi, w in enumerate(seg.words):
            if wi == 0:
                continue
            prev_end = float(seg.words[wi - 1].end)
            if float(w.start) - prev_end <= word_gap_trigger_s:
                continue
            log.info("Clamped stretched word '%s' [%.2f-%.2f] -> [%.2f-%.2f]",
                     word_text(w), float(w.start), float(w.end), prev_end, prev_end)
            w.start = prev_end
            w.end = prev_end
        if seg.words:
            seg.end = float(seg.words[-1].end)
    return result


def _autocorrect_bias(result: AlignmentResult, audio_path, s: Settings = settings) -> AlignmentResult:
    """Exp7: per-song bias auto-correction.

    Whisper timestamps rap pickup syllables late — line starts land on the
    first *confident* word instead of the true sung onset. This pass computes
    the median signed offset between each line start and the nearest vocal
    onset (wider window than _snap_start_to_onset), and if a systematic bias
    exists, shifts ALL lines by it.

    Uses only local audio — no ground truth needed.
    """
    if not getattr(s, "autocorrect_bias", True):
        return result
    try:
        from KaraokeGen.audio import _load_mono16, detect_onsets
        y, sr = _load_mono16(audio_path, s)
        onsets = detect_onsets(y, sr)
    except Exception as exc:
        log.debug("Bias autocorrect unavailable (%s) — skipping.", exc)
        return result
    if len(onsets) == 0 or not result.segments:
        return result
    offsets = []
    for seg in result.segments:
        if not seg.words:
            continue
        st = float(seg.start)
        idx = int(np.searchsorted(onsets, st))
        cands = []
        if idx < len(onsets):
            cands.append(float(onsets[idx]))
        if idx > 0:
            cands.append(float(onsets[idx - 1]))
        if not cands:
            continue
        best = min(cands, key=lambda o: abs(o - st))
        # wide window: we want the systematic pull, not per-line snapping
        if abs(best - st) <= 1.2:
            offsets.append(best - st)
    if len(offsets) < max(3, len(result.segments) // 3):
        log.debug("Bias autocorrect: too few onset-anchored lines (%d) — skipping.", len(offsets))
        return result
    bias = float(np.median(offsets))
    min_bias = float(getattr(s, "autocorrect_bias_min_s", 0.25))
    if abs(bias) < min_bias:
        log.info("Bias autocorrect: median %.3fs below threshold %.2fs — no shift.", bias, min_bias)
        return result
    from KaraokeGen.lyrics import _shift_segment
    for seg in result.segments:
        _shift_segment(seg, bias)
    log.info("Bias autocorrect: shifted all lines by %+.3fs (median of %d onset deltas).", bias, len(offsets))
    return result


def _snap_start_to_onset(result: AlignmentResult, audio_path, s: Settings = settings) -> AlignmentResult:
    """Snap each line's start to the nearest vocal onset within a small
    window. This makes line starts land right when the singer actually
    starts singing — the #1 timing fix users need in the editor.

    If the energy AT the chosen start is low (silence/pickup — the aligner
    landed before the true first word), snap FORWARD to the first vocal
    onset within +0.9s. Rap lines frequently start after a quick pickup;
    Whisper timestamps the pickup late and lands on silence before the real
    phrase."""
    if not s.snap_to_onsets:
        return result
    try:
        from KaraokeGen.audio import _load_mono16, detect_onsets
        y, sr = _load_mono16(audio_path, s)
        onsets = detect_onsets(y, sr)
        # frame energy for low-energy detection (10ms hop)
        hop = 160
        nfr = len(y) // hop
        yf = y[:nfr * hop].reshape(nfr, hop)
        energy = np.sqrt((yf.astype("float64") ** 2).mean(axis=1))
        noise = float(np.percentile(energy, 15))
    except Exception as exc:
        log.debug("Onset detection unavailable (%s) — skipping snap.", exc)
        return result
    if len(onsets) == 0:
        return result

    def _energy_at(t: float) -> float:
        i = int(t * sr / hop)
        # max energy in a small ±30ms window to avoid zero-crossing artifacts
        i0, i1 = max(0, i - 2), min(len(energy), i + 3)
        return float(energy[i0:i1].max()) if i1 > i0 else 1.0

    prev_start = -1.0
    for seg in result.segments:
        if not seg.words:
            continue
        old_start = float(seg.start)

        # Exp8 forward-snap: start sits on silence → find first onset ahead
        e_here = _energy_at(old_start)
        if e_here < noise * 1.8:
            fwd = [float(o) for o in onsets
                   if old_start + 0.05 <= o <= old_start + 0.9 and o >= prev_start + 0.25]
            if fwd:
                from KaraokeGen.lyrics import _shift_segment
                _shift_segment(seg, fwd[0] - old_start)
                prev_start = fwd[0]
                continue

        idx = int(np.searchsorted(onsets, old_start))
        cands = []
        if idx < len(onsets):
            cands.append(float(onsets[idx]))
        if idx > 0:
            cands.append(float(onsets[idx - 1]))
        best = min(cands, key=lambda o: abs(o - old_start))
        if abs(best - old_start) <= s.word_snap_window_s and best >= prev_start + 0.25:
            delta = best - old_start
            from KaraokeGen.lyrics import _shift_segment
            _shift_segment(seg, delta)
        prev_start = float(seg.start)
    return result


def _extend_line_ends(result: AlignmentResult, audio_path, s: Settings = settings) -> AlignmentResult:
    """Extend each line's end forward if there's still vocal energy.

    Fixes the systematic early-end issue where the highlight finishes
    while the singer is still holding the note.

    The extension stops at the first sustained SILENCE GAP (energy below
    threshold for `line_end_confirm_s`) after the last active run. This is
    what keeps a repeated chorus line from swallowing the interlude before
    the next line: the interlude is quiet, so the extension ends there
    instead of walking across it to the next line's singing.
    """
    try:
        from KaraokeGen.audio import _load_mono16
        y, sr = _load_mono16(audio_path, s)
    except Exception as exc:
        log.debug("Audio unavailable (%s) — skipping end extension.", exc)
        return result
    # Frame-level RMS energy (10 ms hop). Sample-level |y| oscillates through
    # any threshold at every zero crossing; runs/gaps must be detected on
    # smoothed energy. (The pre-fix code also indexed samples with frame
    # numbers — 160x too early — making the whole pass a no-op.)
    hop = 160
    energies = np.sqrt(np.maximum(0, np.asarray(y, dtype=np.float64) ** 2))
    n_frames = len(energies) // hop
    frames = energies[:n_frames * hop].reshape(n_frames, hop)
    energy_f = np.sqrt((frames ** 2).mean(axis=1))
    frame_s = hop / float(sr)
    noise = float(np.percentile(energy_f, 15)) if n_frames else 0.0
    extend_s = getattr(s, "line_end_extend_s", 0.5)
    confirm_s = getattr(s, "line_end_confirm_s", 0.12)
    confirm_frames = max(1, int(confirm_s / frame_s))
    for i, seg in enumerate(result.segments):
        if not seg.words:
            continue
        # Relative threshold: a soft tail that survived normalization is still
        # singing — use the line's own peak so tails aren't gated out.
        start_f = int(seg.start / frame_s)
        end_f = min(n_frames, int(seg.end / frame_s))
        line_peak = float(energy_f[start_f:end_f].max()) if end_f > start_f else 0.0
        thr = max(noise * 2.5, line_peak * 0.05)
        # Scan forward from the current end
        scan_start = int(seg.end / frame_s)
        scan_end = min(n_frames, int((seg.end + extend_s) / frame_s))
        if scan_end <= scan_start:
            continue
        chunk = energy_f[scan_start:scan_end]
        # Find the last active run BEFORE a sustained gap. Walk the chunk;
        # whenever the energy stays below threshold for confirm_s, the
        # singing is over — everything after that is the next line's territory.
        last_active = scan_start
        quiet = 0
        for j in range(len(chunk)):
            if chunk[j] > thr:
                last_active = scan_start + j
                quiet = 0
            else:
                quiet += 1
                # Only a sustained gap AFTER real activity (not the initial
                # quiet before the tail arrives) ends the extension.
                if (quiet >= confirm_frames and
                        last_active - scan_start >= confirm_frames):
                    break
        new_end = (last_active + 1) * frame_s
        # Don't extend past the next line's start
        if i < len(result.segments) - 1:
            new_end = min(new_end, result.segments[i + 1].start - 0.05)
        if new_end > seg.end:
            seg.end = new_end
            if seg.words:
                seg.words[-1].end = new_end
    return result


def _trim_end_silence(result: AlignmentResult, audio_path, s: Settings = settings) -> AlignmentResult:
    """Pull a line's end back when it sits well past the last sung frame.

    The stretched-word clamp collapses mis-anchored words but leaves the
    segment end at the collapsed word position; echo phrases re-anchored
    mid-air can also end after the singing stops. If the line's end is more
    than end_trim_grace_s past the last vocal-energy frame inside its own
    span, trim it there (+ the grace decay).
    """
    try:
        from KaraokeGen.audio import _load_mono16
        y, sr = _load_mono16(audio_path, s)
    except Exception as exc:
        log.debug("Audio unavailable (%s) — skipping end trim.", exc)
        return result
    hop = 160
    energies = np.sqrt(np.maximum(0, np.asarray(y, dtype=np.float64) ** 2))
    n_frames = len(energies) // hop
    frames = energies[:n_frames * hop].reshape(n_frames, hop)
    energy_f = np.sqrt((frames ** 2).mean(axis=1))
    frame_s = hop / float(sr)
    noise = float(np.percentile(energy_f, 15)) if n_frames else 0.0
    thr = noise * 2.5
    grace_s = float(getattr(s, "end_trim_grace_s", 0.5))
    trigger_s = float(getattr(s, "end_trim_trigger_s", 0.8))
    for seg in result.segments:
        start_f = int(seg.start / frame_s)
        end_f = min(n_frames, int(seg.end / frame_s))
        if end_f <= start_f:
            continue
        span = energy_f[start_f:end_f]
        active = np.nonzero(span > thr)[0]
        if not len(active):
            continue
        # float() coerces numpy scalars — np.float64 leaking into the result
        # breaks deserialization in the numpy-less web container.
        last_vocal_s = float((start_f + int(active[-1])) * frame_s)
        if seg.end - last_vocal_s > trigger_s:
            new_end = float(last_vocal_s + grace_s)
            if new_end < seg.end:
                log.info("Trimmed line end %.2f -> %.2f (past last vocal %.2f)",
                         seg.end, new_end, last_vocal_s)
                seg.end = new_end
                if seg.words:
                    seg.words[-1].end = new_end
    return result


def _expand_degenerate_lines(result: AlignmentResult, s: Settings = settings) -> AlignmentResult:
    """Give flash-lines a visible span bounded by their neighbors.

    A line the aligner could not anchor (vocals missing from the stem — e.g.
    a soft intro the separator lost, or an echo classified as backing)
    collapses to a degenerate <0.15s span and flashes on screen. Expand it
    into the free gap between its neighbors (up to max 6s), so the highlight
    covers the region where that line's vocal actually lives in the full mix.
    """
    max_expand_s = float(getattr(s, "degenerate_max_expand_s", 6.0))
    for i, seg in enumerate(result.segments):
        span = float(seg.end) - float(seg.start)
        if span >= 0.15 or not seg.words:
            continue
        prev_end = float(result.segments[i - 1].end) if i > 0 else 0.0
        next_start = float(result.segments[i + 1].start) \
            if i + 1 < len(result.segments) else float(seg.end) + max_expand_s
        new_start = max(prev_end + 0.05, float(seg.start) - max_expand_s)
        new_end = min(next_start - 0.05, float(seg.end) + max_expand_s)
        if new_end - new_start > 0.15:
            log.info("Expanded degenerate line '%s' [%.2f-%.2f] -> [%.2f-%.2f]",
                     (seg.text or "")[:20], seg.start, seg.end, new_start, new_end)
            delta = new_start - float(seg.start)
            from KaraokeGen.lyrics import _shift_segment
            _shift_segment(seg, delta)
            seg.end = new_end
            if seg.words:
                seg.words[-1].end = new_end
    return result


def align_lyrics(model, vocals, text, s: Settings = settings) -> AlignmentResult | None:
    """Top-level entry: stable-ts full-text alignment + refine.

    Tuned for singing audio (karaoke vocals) rather than speech:
    - VAD enabled with a STRICTER threshold so instrumental bleed isn't
      treated as speech (ghost pickup is the main alignment killer)
    - Larger min_word_dur (sung syllables are longer than spoken)
    - max_word_dur caps runaway alignment on held notes
    - nonspeech_skip handles gaps between phrases
    After align/refine, _filter_ghost_words drops hallucinated spans and
    _replace_asr_with_official remaps the official text by segment duration."""
    text = (text or "").strip()
    align_kwargs = dict(
        language=s.language,
        original_split=True,
        suppress_silence=bool(getattr(s, "align_suppress_silence", True)),
        vad=True,
        vad_threshold=s.align_vad_threshold,
        min_word_dur=0.08,
        max_word_dur=float(getattr(s, "align_max_word_dur", 4.0)),
        word_dur_factor=float(getattr(s, "align_word_dur_factor", 2.5)),
        nonspeech_skip=float(getattr(s, "align_nonspeech_skip", 3.0)),
        nonspeech_error=float(getattr(s, "align_nonspeech_error", 0.3)),
        use_word_position=bool(getattr(s, "align_use_word_position", True)),
        gap_padding=' ...',
    )
    # stable-ts "new" aligner (redesigned attention-head selection).
    # Env-gated, off by default.
    if getattr(s, "align_new_heads", False):
        align_kwargs["aligner"] = "new"
    try:
        st_result = model.align(vocals, text, **align_kwargs)
    except TypeError:
        st_result = model.align(vocals, text, language=s.language, original_split=True)
    if s.refine_timings:
        try:
            st_result = _refine_for_singing(model, str(vocals), st_result, s)
        except Exception as exc:
            log.warning("Refine failed (%s).", exc)
    result = from_stable_ts(st_result)
    result = _filter_ghost_words(result, s)
    if text.strip():
        result = _replace_asr_with_official(result, text)
    result = _sync_segment_boundaries(result)
    result = _clamp_stretched_words(result, vocals, s)
    result = _snap_start_to_onset(result, vocals, s)
    result = _autocorrect_bias(result, vocals, s)
    result = _extend_line_ends(result, vocals, s)
    result = _trim_end_silence(result, vocals, s)
    result = _expand_degenerate_lines(result, s)

    # Word-level CTC median ensemble (plain path). The ensemble path
    # (align_lyrics_ensemble) applies it AFTER line arbitration instead.
    if getattr(s, "word_ensemble", False) and text.strip():
        try:
            from KaraokeGen import word_ctc
            result, _ = word_ctc.refine_word_timings(result, vocals, s)
        except Exception as exc:
            log.warning("Word CTC ensemble failed (%s); keeping stable-ts words.", exc)

    offset = float(getattr(s, "timing_offset_s", 0.0) or 0.0)
    if offset:
        from KaraokeGen.lyrics import _shift_segment
        for seg in result.segments:
            _shift_segment(seg, offset)
        log.info("Timing offset: %+.3fs", offset)

    return result


def _refine_for_singing(model, vocals, result, s: Settings = settings):
    """Refine optimized for singing audio (stable-whisper 2.19+ API).

    refine() iteratively mutes portions of the audio and monitors token
    probability to find the latest start / earliest end of each word. For
    karaoke vocals:
      - only_voice_freq band-limits to 200-5000 Hz so instrumental bleed
        can't hold token probability up and widen the timing window
      - small rel_prob_decrease so refinement stops right at the true edge
    Falls back to progressively simpler/coarser kwargs so a slightly
    different stable-whisper version can't silently disable refinement."""
    passes = (
        dict(steps=s.refine_steps, word_level=s.refine_word_level,
             precision=s.refine_precision, prob_threshold=s.refine_prob_threshold,
             rel_dur_change=s.refine_rel_dur_change,
             only_voice_freq=s.refine_only_voice_freq),
        dict(steps=s.refine_steps, word_level=s.refine_word_level,
             precision=s.refine_precision, prob_threshold=s.refine_prob_threshold,
             rel_dur_change=s.refine_rel_dur_change),
        dict(steps=s.refine_steps, word_level=s.refine_word_level, precision=s.refine_precision),
        dict(steps=s.refine_steps, word_level=True, precision=0.3),
        dict(steps='se', word_level=True, precision=0.5),
        dict(steps='se', precision=0.5),
    )
    for pass_kwargs in passes:
        try:
            result = model.refine(vocals, result, **pass_kwargs)
            log.info("Refine: %s", pass_kwargs)
            break
        except TypeError:
            continue
        except Exception as exc:
            log.warning("Refine failed (%s) — trying simpler kwargs.", exc)
            continue
    return result


def _word_prob(w) -> float:
    try:
        return float(getattr(w, "probability", 1.0) or 1.0)
    except (TypeError, ValueError):
        return 1.0


_PAD_TOKENS = {".", "..", "...", "…", "", " "}


def _is_padding_word(w) -> bool:
    """True when a word is stable-ts alignment padding (' ...' fill) or is
    punctuation-only — a strong signal it was interpolated, not sung."""
    txt = word_text(w).strip()
    if not txt:
        return True
    t = re.sub(r"\W+", "", txt)
    return (not t) or txt in _PAD_TOKENS


def _filter_ghost_words(result: AlignmentResult, s: Settings = settings) -> AlignmentResult:
    """Remove hallucinated padding/blip words — never drop whole segments.

    With original_split=True each segment is a lyric line from the user.
    Dropping even one segment causes _replace_asr_with_official to fall
    into the water-fill path which scrambles word order across lines.

    So the rule is simple: KEEP every segment. Only strip out padding
    tokens (' ...', '…', etc.) and words that are clearly noise
    (very short AND very low confidence). If a segment has zero real
    words left after stripping, it still stays — it just has no words."""
    min_prob = 0.3
    min_dur = 0.06
    new_segs = []
    removed_words = 0
    for seg in result.segments:
        kept = []
        for w in seg.words:
            if w is None:
                continue
            p = _word_prob(w)
            dur = max(0.0, float(getattr(w, "end", 0)) - float(getattr(w, "start", 0)))
            if p < min_prob and dur < 0.15:
                removed_words += 1
                continue
            if dur < min_dur and p < 0.5:
                removed_words += 1
                continue
            kept.append(w)
        new_segs.append(Segment(
            words=kept, start=seg.start, end=seg.end,
            text=seg.text or " ".join(word_text(w) for w in kept)))
    if removed_words:
        log.info("Filtered %d ghost word(s) from alignment.", removed_words)
    return AlignmentResult(segments=new_segs, line_notes=result.line_notes)


def result_to_lyrics(result: AlignmentResult) -> str:
    lines = []
    for seg in result.segments:
        line = " ".join(word_text(w) for w in seg.words).strip()
        if not line:
            line = (seg.text or "").strip()
        if line:
            lines.append(line)
    return "\n".join(lines)
