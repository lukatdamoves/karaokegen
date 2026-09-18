"""Ensemble alignment: lead-stem dual-aligner + content verification.

  1. stable-ts (rap-tuned) line starts        -> W
  2. LA-Multilingual (Tagalog G2P) line starts -> L
  3. Conservative arbitration:
     - |W-L| <= ens_agree_s          -> midpoint + quiet-onset micro-snap
     - bracket >= ens_bracket_s      -> quietest onset INSIDE the span wins
     - else                          -> onset-energy arbitration (quiet+strength+closeness)
     - early-LA guard                -> discard L when it hallucinates onto intro adlibs
  4. Content verification: for every disagreement, teacher-forced-align the
     line's own text at BOTH candidates on GPU; winner must have prob >= ens_floor
     with margin >= ens_margin ("match only what is in the lyrics").
  5. 16th-note grid snap (LRCLIB truth is grid-quantized, median 24-32ms).
  6. Shift the stable-ts result's segments onto arbitrated starts (word offsets kept,
     monotonicity clamped, moves > ens_max_shift_s reverted).

All heavy imports are lazy so this module imports cleanly without torch.
Failure semantics: every stage raises; callers fall back to the legacy result.
"""
from __future__ import annotations

import csv
import logging
import os
import tempfile
from pathlib import Path

import numpy as np

from KaraokeGen.config import Settings, settings
from KaraokeGen.models import AlignmentResult, Segment

log = logging.getLogger(__name__)

SR = 16000
PUNCT = ';:,.!"?()-'


def _norm_word(w: str) -> str:
    for c in PUNCT:
        w = w.replace(c, "")
    return w.lower().strip()


def _line_words(text: str) -> list[str]:
    return [w for w in (_norm_word(x) for x in text.split()) if w]


# ---------------------------------------------------------------------------
# audio helpers
# ---------------------------------------------------------------------------

def load_mono16k(path) -> np.ndarray:
    import librosa
    import soundfile as sf
    data, orig = sf.read(str(path), dtype="float32")
    if data.ndim > 1:
        data = data.mean(1)
    if orig != SR:
        data = librosa.resample(data, orig_sr=orig, target_sr=SR)
    return data


def vocal_env_and_onsets(y: np.ndarray, sr: int = SR):
    import librosa
    hop = 80
    S = np.abs(librosa.stft(y, n_fft=512, hop_length=hop))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=512)
    band = (freqs > 150) & (freqs < 5000)
    env = S[band].mean(axis=0)
    kernel = np.hanning(9)
    kernel /= kernel.sum()
    env_s = np.convolve(env, kernel, mode="same")
    flux = np.maximum(np.diff(env_s, prepend=env_s[0]), 0)
    peaks = librosa.util.peak_pick(flux, pre_max=12, post_max=12, pre_avg=25,
                                   post_avg=25, delta=0.02, wait=20)
    t = np.arange(len(env_s)) * hop / sr

    def env_at(t0: float, t1: float):
        i0, i1 = np.searchsorted(t, [t0, t1])
        if i1 <= i0 or i0 < 0 or i1 > len(env_s):
            return None
        return float(env_s[i0:i1].mean())

    ostr = flux[peaks] / (flux[peaks].max() if len(peaks) else 1.0)
    return dict(t=t, env=env_s, onsets=np.array(peaks) * hop / sr, ostr=ostr), env_at


def beat_grid16(audio_path, s: Settings = settings):
    import librosa
    import soundfile as sf
    data, sr = sf.read(str(audio_path), dtype="float32")
    if data.ndim > 1:
        data = data.mean(1)
    hop = 256
    onset = librosa.onset.onset_strength(y=data, sr=sr, hop_length=hop)
    tempo, beats = librosa.beat.beat_track(onset_envelope=onset, sr=sr,
                                           hop_length=hop, trim=False)
    bt = librosa.frames_to_time(beats, sr=sr, hop_length=hop)
    grid = []
    for i in range(len(bt) - 1):
        for k in range(4):
            grid.append(bt[i] + (bt[i + 1] - bt[i]) * k / 4)
    return np.array(grid)


def snap_grid(t: float, grid, tol: float = 0.08) -> float:
    j = int(np.searchsorted(grid, t))
    cc = [grid[k] for k in (j - 1, j) if 0 <= k < len(grid)]
    if not cc:
        return float(t)
    g = min(cc, key=lambda x: abs(x - t))
    return float(g) if abs(g - t) <= tol else float(t)


# ---------------------------------------------------------------------------
# LA-Multilingual line-start inference
# ---------------------------------------------------------------------------

def _la_run(lead_path, lines: list[str], s: Settings = settings) -> list[float]:
    """Run LyricsAlignment-Multilingual over the lead stem; return one start
    per normalized word (flattened across lines). Raises on any failure."""
    import sys
    repo = Path(os.environ.get("KVC_LA_REPO", "/root/LyricsAlignment-Multilingual"))
    if not repo.exists():
        raise FileNotFoundError(f"LA repo not mounted: {repo}")
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    ckpt = Path(s.model_cache_dir) / s.la_checkpoint
    if not ckpt.exists():
        raise FileNotFoundError(f"LA checkpoint missing: {ckpt}")

    import argparse
    import shutil
    import eval as la_eval  # noqa: E402  (repo module)

    work = Path(tempfile.mkdtemp(prefix="la_"))
    annot, adir, pdir = work / "annot", work / "audio", work / "pred"
    for d in (annot, adir, pdir):
        d.mkdir(parents=True, exist_ok=True)

    words_flat: list[str] = []
    for ln in lines:
        for w in ln.split():
            wc = _norm_word(w)
            if wc:
                words_flat.append(wc)

    from KaraokeGen.tagalog_g2p import tagalog_g2p
    csvp = annot / "la_in.csv"
    with open(csvp, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["word", "phonemizer", "phone_idx"])
        st = 0
        for w in words_flat:
            ph = tagalog_g2p(w)
            ed = st + max(1, len(ph))
            wr.writerow([w, ";".join(ph), f"[{st}, {ed}]"])
            st = ed + 1

    shutil.copy(str(lead_path), str(adir / "la_in.wav"))
    la_eval.main(argparse.Namespace(
        cuda=True, sepa_dir=str(adir), dataset="jamendo", pred_dir=str(pdir),
        load_model=str(ckpt), model="baseline", annot_dir=str(annot),
        ext=".wav", sr=22050, rnn_dim=256, unit="phone"))

    rows: list[float] = []
    with open(pdir / "la_in_align.csv", encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split(",", 2)
            if len(parts) == 3:
                rows.append(float(parts[0]))
    shutil.rmtree(work, ignore_errors=True)
    return rows


def la_line_starts(lead_path, lines: list[str], s: Settings = settings) -> list[float | None]:
    """One start per input line (None when words ran out). Raises on failure."""
    global _LA_WORD_ROWS
    _LA_WORD_ROWS = []
    rows = _la_run(lead_path, lines, s)
    _LA_WORD_ROWS = list(rows)
    starts: list[float | None] = []
    wi = 0
    for ln in lines:
        lw = _line_words(ln)
        if not lw or wi >= len(rows):
            starts.append(None)
            continue
        starts.append(rows[wi])
        wi += len(lw)
    return starts


def la_word_starts(lead_path, lines: list[str],
                   s: Settings = settings) -> list[float]:
    """Per-word LA starts, flattened over all lines (also an English
    median member — the same rows la_line_starts reads line-firsts from)."""
    return _la_run(lead_path, lines, s)


# ---------------------------------------------------------------------------
# content verification (teacher-forced probability at a candidate position)
# ---------------------------------------------------------------------------

def content_prob(wm, audio16k: np.ndarray, pos: float, text: str) -> float:
    """Mean word probability when the line's own text is force-aligned to the
    window starting at `pos`. Low score => these words are NOT sung there."""
    dur = max(4.0, len(_line_words(text)) * 0.45 + 1.5)
    a = max(0, int((pos - 0.5) * SR))
    b = int(min(len(audio16k) / SR, pos + dur) * SR)
    seg = audio16k[a:b]
    if len(seg) < 8000:
        return 0.0
    res = wm.align(seg, text, language="tl", original_split=True, vad=True,
                   vad_threshold=0.35, min_word_dur=0.08, nonspeech_skip=None,
                   gap_padding=" ...")
    probs = [float(w.probability) for sg in res.segments for w in (sg.words or [])]
    return float(np.mean(probs)) if probs else 0.0


# ---------------------------------------------------------------------------
# arbitration core
# ---------------------------------------------------------------------------

def arbitrate(W_starts: list[float], L_starts: list[float | None], texts: list[str],
              v: dict, env_at, grid, wm, audio16k: np.ndarray,
              s: Settings = settings,
              raw16k: np.ndarray | None = None) -> list[float]:
    n = min(len(W_starts), len(texts))
    preds: list[float] = []

    def classic(i: int, a: float, b: float) -> float:
        lo, hi = min(a, b), max(a, b)
        best_in = None
        if abs(a - b) >= s.ens_bracket_s:
            inspan = [t for t in v["onsets"] if lo + 0.05 < t < hi - 0.05]
            if inspan:
                q = [env_at(t - 0.30, t - 0.05) or 1.0 for t in inspan]
                jj = int(np.argmin(q))
                if q[jj] < 0.60:
                    best_in = float(inspan[jj])
        if best_in is not None:
            return snap_grid(best_in, grid)
        lo2, hi2 = min(a, b) - 0.4, max(a, b) + 0.8
        cand = [t for t in v["onsets"] if lo2 <= t <= hi2]
        if cand:
            quiet = np.array([env_at(t - 0.30, t - 0.05) or 1.0 for t in cand])
            strs = np.array([v["ostr"][int(np.argmin(np.abs(v["onsets"] - t)))] for t in cand])
            close = np.array([-abs(t - (a + b) / 2) for t in cand])
            z = lambda x: (x - x.min()) / (x.max() - x.min()) if x.max() > x.min() else np.zeros_like(x)
            pick = float(cand[int(np.argmax(z(-quiet) * 1.0 + z(strs) * 0.4 + z(close) * 1.0))])
        else:
            pick = a
        return snap_grid(pick, grid)

    for i in range(n):
        a = W_starts[i]
        raw_l = L_starts[i] if i < len(L_starts) else None
        guarded = raw_l is not None and raw_l < s.ens_guard_ratio * a and (a - raw_l) > s.ens_guard_gap
        b = raw_l if raw_l is not None and not guarded else a
        if abs(a - b) <= s.ens_agree_s:
            mid = (a + b) / 2
            near = [t for t in v["onsets"] if abs(t - mid) <= 0.12]
            fin = mid
            if near:
                qq = [env_at(t - 0.25, t - 0.03) or 1.0 for t in near]
                jj = int(np.argmin(qq))
                if qq[jj] < 0.55:
                    fin = near[jj]
            preds.append(snap_grid(fin, grid))
            continue
        # Exp22: content-verified choice
        chosen = None
        sc_a = content_prob(wm, audio16k, a, texts[i])
        sc_b = content_prob(wm, audio16k, b, texts[i]) if b != a else sc_a
        if max(sc_a, sc_b) >= s.ens_floor and abs(sc_a - sc_b) >= s.ens_margin:
            chosen = a if sc_a > sc_b else b
        elif s.ens_rescue and raw16k is not None:
            # Multi-singer rescue (Option A): neither candidate has confident
            # evidence on the LEAD stem — the signature of a second singer
            # filtered out of vocals_lead. Re-score both candidates on the
            # all-voices stem (vocals_raw) where every singer is present.
            rc_a = content_prob(wm, raw16k, a, texts[i])
            rc_b = content_prob(wm, raw16k, b, texts[i]) if b != a else rc_a
            if max(rc_a, rc_b) >= s.ens_rescue_floor:
                chosen = a if rc_a > rc_b else b
                log.info("Rescued line %d on raw stem (lead %.2f/%.2f -> raw %.2f/%.2f)",
                         i, sc_a, sc_b, rc_a, rc_b)
        if chosen is None:
            chosen = classic(i, a, b)
        prev = preds[-1] if preds else None
        if prev is not None and abs(chosen - prev) > s.ens_max_shift_s + 12.0:
            chosen = classic(i, a, b)
        preds.append(chosen)
    return preds


# ---------------------------------------------------------------------------
# apply arbitrated starts onto the stable-ts result
# ---------------------------------------------------------------------------

def shift_segments(result: AlignmentResult, new_starts: list[float],
                   max_shift: float) -> AlignmentResult:
    segs = result.segments
    moved = 0
    for i, seg in enumerate(segs):
        if i >= len(new_starts) or new_starts[i] is None:
            continue
        old = float(seg.start)
        new = float(new_starts[i])
        if abs(new - old) > max_shift:
            continue
        delta = new - old
        # monotonic clamp: never cross neighbours
        if i > 0 and new < float(segs[i - 1].end) - 0.05:
            delta = float(segs[i - 1].end) - old
        if i + 1 < len(segs) and old + delta > float(segs[i + 1].start):
            delta = float(segs[i + 1].start) - old
        if abs(delta) < 1e-6:
            continue
        seg.start = float(old + delta)
        for w in seg.words:
            w.start = float(w.start + delta)
            w.end = float(w.end + delta)
        seg.end = float(seg.end + delta)
        moved += 1
    log.info("Ensemble: shifted %d/%d segments onto arbitrated starts.", moved, len(segs))
    return result


# ---------------------------------------------------------------------------
# English word median {stable-ts, whisper word_ts, LA per-word}
# ---------------------------------------------------------------------------

# last-run member dump for offline diagnosis (texts + A/B/C starts)
_EN_MEDIAN_DEBUG: dict = {}
# Last-run LA word rows (full per-word starts stash for diagnostics;
# cleared per ensemble run, set by la_line_starts)
_LA_WORD_ROWS: list[float] = []


def vocal_duty_cycle(vocals_path: str, floor_db: float = -40.0) -> float:
    """Fraction of the song carrying vocal energy above `floor_db` of peak.

    Mechanism signal: whisper word_ts is the best English word engine on
    dense vocals but drifts when it must traverse long instrumentals. A
    sanity leash on member disagreement does NOT separate the two cases,
    so the routing signal has to be acoustic density, not disagreement."""
    import numpy as np
    import soundfile as sf

    y, sr = sf.read(str(vocals_path), dtype="float32")
    if y.ndim > 1:
        y = y.mean(1)
    hop = max(1, int(0.02 * sr))
    n = len(y) // hop
    if n <= 0:
        return 1.0
    rms = np.sqrt(np.maximum(1e-12, np.array(
        [np.mean(y[i * hop:(i + 1) * hop] ** 2) for i in range(n)])))
    db = 20 * np.log10(rms / max(1e-9, float(rms.max())))
    return float((db > floor_db).mean())


def _lcs_bind(lyr: list[str], asr: list[str]) -> list[int]:
    """Order-preserving LCS binding of ASR words to lyric words (robust to
    ASR insertions — backing vocals/hallucinations — and deletions — VAD
    gaps). Returns asr-index per lyric word, -1 when unbound."""
    n, m = len(lyr), len(asr)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        li = lyr[i]
        row, nxt = dp[i], dp[i + 1]
        for j in range(m - 1, -1, -1):
            if li and li == asr[j]:
                row[j] = nxt[j + 1] + 1
            else:
                row[j] = nxt[j] if nxt[j] >= row[j + 1] else row[j + 1]
    binds = [-1] * n
    i = j = 0
    while i < n and j < m:
        if lyr[i] and lyr[i] == asr[j]:
            binds[i] = j
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return binds


def _en_word_median(model, result, vocals_asr_path: str, lead_raw_path: str,
                    text: str, s: Settings = settings):
    import numpy as np

    from KaraokeGen.align import _asr_prompt
    from KaraokeGen.lyrics import norm_word, word_text
    from KaraokeGen.word_ctc import _hygiene

    lines = [l for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return result

    flat_words = [w for seg in result.segments for w in seg.words
                  if w is not None and word_text(w)]
    n = len(flat_words)
    if n == 0:
        return result

    # member B: whisper word_ts, English-forced, no VAD
    b_words: list[tuple[str, float, float]] = []
    try:
        kwargs = dict(language="en", task="transcribe", word_timestamps=True,
                      temperature=0.0, condition_on_previous_text=False,
                      no_speech_threshold=0.35,
                      compression_ratio_threshold=2.6, beam_size=5)
        prompt = _asr_prompt()
        if prompt:
            kwargs["initial_prompt"] = prompt
        asr = model.transcribe(vocals_asr_path, **kwargs)
        for seg in getattr(asr, "segments", []) or []:
            for w in getattr(seg, "words", None) or []:
                t = word_text(w)
                if t:
                    b_words.append((t, float(w.start), float(w.end)))
        log.info("English median: B transcript %d words, sample: %s",
                 len(b_words), [t for t, _st, _en in b_words[:12]])
    except Exception as exc:
        log.info("English median: ASR member unavailable (%s).", exc)

    # member C: LA per-word starts on the lead stem
    c_rows: list[float] | None = None
    try:
        c_rows = la_word_starts(lead_raw_path, lines, s)
        log.info("English median: C rows %d.", len(c_rows))
    except Exception as exc:
        log.info("English median: LA member unavailable (%s).", exc)

    # bind B to lyric words by normalized order (LCS)
    lyric_norm = [norm_word(word_text(w)) for w in flat_words]
    binds = (_lcs_bind(lyric_norm, [norm_word(t) for t, _st, _en in b_words])
             if b_words else [-1] * n)

    # map C rows to flat index (LA emits one row per non-empty norm word)
    c_for_flat: list[float | None] = [None] * n
    if c_rows is not None:
        ci = 0
        for k in range(n):
            if not lyric_norm[k]:
                continue
            c_for_flat[k] = c_rows[ci] if ci < len(c_rows) else None
            ci += 1

    starts = [float(w.start) for w in flat_words]
    ends = [float(w.end) for w in flat_words]
    a_starts = list(starts)
    min_dur = float(getattr(s, "word_ctc_min_word_dur_s", 0.03))

    # Density gate: dense vocals -> member B alone (whisper word_ts is
    # the best English word engine on dense vocals); sparse -> median3
    # (B drifts through long instrumentals).
    duty = 1.0
    try:
        duty = vocal_duty_cycle(vocals_asr_path)
    except Exception as exc:
        log.info("English median: duty cycle unavailable (%s); median3.", exc)
        duty = 0.0
    dense = duty > float(getattr(s, "en_density_thr", 0.62))

    n_used = 0
    for k in range(n):
        if dense:
            if binds[k] >= 0:
                starts[k] = float(b_words[binds[k]][1])
                n_used += 1
        elif binds[k] >= 0 and c_for_flat[k] is not None:
            starts[k] = float(np.median([
                starts[k], b_words[binds[k]][1], c_for_flat[k]]))
            n_used += 1
    # member dump for offline diagnosis
    _EN_MEDIAN_DEBUG.clear()
    _EN_MEDIAN_DEBUG.update({
        "texts": [word_text(w) for w in flat_words],
        "A": a_starts,
        "B": [b_words[binds[k]][1] if binds[k] >= 0 else None
              for k in range(n)],
        "C": [c_for_flat[k] if c_for_flat[k] is not None else None
              for k in range(n)],
    })
    _hygiene(starts, ends, min_dur)
    for k, w in enumerate(flat_words):
        w.start = float(starts[k])
        w.end = float(ends[k])
    log.info("English median: duty=%.3f -> %s, %d/%d words adjusted.",
             duty, "B (dense)" if dense else "median3 (sparse)", n_used, n)
    return result


# ---------------------------------------------------------------------------
# top-level entry (called from modal_app when align_ensemble is enabled)
# ---------------------------------------------------------------------------

def align_lyrics_ensemble(model, vocals_asr_path: str, lead_raw_path: str,
                           mix_path: str, text: str, s: Settings = settings,
                           raw_vocals_path: str | None = None):
    """Ensemble pipeline. `model` is the loaded faster-whisper wrapper used
    by align_lyrics. Returns an AlignmentResult whose line starts come from
    the arbitrated ensemble; word timings are the stable-ts ones shifted
    along. `raw_vocals_path` (vocals_raw.wav) enables the multi-singer
    rescue: lines with no confident evidence on the lead stem are
    re-verified on it.

    The word-level CTC median ensemble runs AFTER line arbitration so the
    CTC windows are the final (arbitrated) line windows — windowed CTC
    must never inherit a pre-arbitration anchor error."""
    global _LA_WORD_ROWS
    _LA_WORD_ROWS = []
    import dataclasses

    from KaraokeGen.align import align_lyrics

    lines = [l for l in (text or "").splitlines() if l.strip()]
    if len(lines) < 2:
        return align_lyrics(model, vocals_asr_path, text, s)

    # Language router — two-pipeline architecture:
    #   Tagalog/Taglish (default) -> LA arbitration + word-CTC ensemble
    #   full-English lyrics      -> plain stable-ts, NO LA arbitration
    # English is detected from the lyrics (tag_ratio < threshold) OR forced
    # by the user's explicit language selection ("en" in the editor
    # dropdown). Arbitration itself — not the knobs — was what hurt
    # English, so English skips it entirely.
    from KaraokeGen.word_ctc import tag_ratio_text
    en_thr = float(getattr(s, "word_ctc_en_tag_ratio", 0.01))
    forced_en = (getattr(s, "language", "") or "").strip().lower().startswith("en")
    if forced_en or tag_ratio_text(text) < en_thr:
        log.info("English lyrics (%s) -> English pipeline (no line "
                 "arbitration).",
                 "user-selected" if forced_en else "tag_ratio < %.3f" % en_thr)
        result = align_lyrics(model, vocals_asr_path, text,
                              dataclasses.replace(s, word_ensemble=False))
        # English word median {stable-ts, whisper word_ts, LA words}.
        # Any failure keeps the plain stable-ts words (member A).
        if getattr(s, "en_word_ensemble", True):
            try:
                result = _en_word_median(model, result, vocals_asr_path,
                                         lead_raw_path, text, s)
            except Exception as exc:
                log.warning("English word median failed (%s); stable-ts "
                            "words kept.", exc)
        return result

    # Fast-rap clause: dense fast Taglish rap confuses every CTC path
    # (very short median words = few CTC frames of evidence per word), so
    # dense fast songs route to plain whisper with rap-tuned durations.
    # wps>1.8 && tag<0.30 separates the dense-rap class from melodic rap,
    # which stays on the MMS path.
    if getattr(s, "fast_rap_router", False):
        try:
            import soundfile as _sf
            _info = _sf.info(str(vocals_asr_path))
            dur_s = float(_info.frames) / float(_info.samplerate)
        except Exception:
            dur_s = 0.0
        n_words = sum(len(l.split()) for l in lines)
        wps = (n_words / dur_s) if dur_s > 30 else 0.0
        tag_r = tag_ratio_text(text)
        if wps > float(getattr(s, "fast_rap_wps", 1.8)) and \
                tag_r < float(getattr(s, "fast_rap_tag", 0.30)):
            log.info("Fast Taglish rap (wps=%.2f, tag=%.2f) -> whisper "
                     "rap-knob pipeline.", wps, tag_r)
            return align_lyrics(model, vocals_asr_path, text,
                                dataclasses.replace(
                                    s, word_ensemble=False,
                                    align_max_word_dur=1.5,
                                    align_word_dur_factor=1.5))

    # word refine happens here (post-arbitration), not inside align_lyrics
    s_plain = dataclasses.replace(s, word_ensemble=False) if getattr(
        s, "word_ensemble", False) else s
    w_result = align_lyrics(model, vocals_asr_path, text, s_plain)
    try:
        y = load_mono16k(lead_raw_path)
        raw16k = None
        if s.ens_rescue and raw_vocals_path and Path(raw_vocals_path).exists():
            try:
                raw16k = load_mono16k(raw_vocals_path)
            except Exception as exc:
                log.warning("Rescue stem unreadable (%s); rescue disabled.", exc)
        v, env_at = vocal_env_and_onsets(y)
        grid = beat_grid16(mix_path, s)
        L = la_line_starts(lead_raw_path, lines, s)
        W_starts = [float(sg.start) for sg in w_result.segments]
        n = min(len(W_starts), len(L), len(lines))
        texts = lines[:n]
        preds = arbitrate(W_starts[:n], L[:n], texts, v, env_at, grid,
                          model, y, s, raw16k=raw16k)
        w_result = shift_segments(w_result, preds[:len(w_result.segments)],
                                  s.ens_max_shift_s)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Ensemble stages failed (%s); returning stable-ts result.", exc)

    # Word-level CTC median ensemble on the final arbitrated windows.
    # CTC audio: the mix — gated vocals_asr can collapse the trellis on
    # sparse line windows. Onset evidence comes from the mix too: its drum
    # transients co-occur with word starts in rhythmic OPM and give the
    # snap MORE anchors, not fewer.
    if getattr(s, "word_ensemble", False) and (text or "").strip():
        try:
            from KaraokeGen import word_ctc
            ctc_audio = vocals_asr_path
            if getattr(s, "word_ctc_source", "mix") == "mix" and Path(mix_path).exists():
                ctc_audio = mix_path
            w_result, _ = word_ctc.refine_word_timings(w_result, ctc_audio, s)
        except Exception as exc:
            log.warning("Word CTC ensemble failed (%s); keeping stable-ts words.", exc)
    return w_result
