"""Word-level CTC refinement: WhisperX-style trellis forced alignment inside
production line windows + per-word median with stable-ts + onset snap.

The CTCs run on vocals_asr (gated lead stem) instead of the raw mix.

Invariants baked in (do not break):
  - whole-line fallback to stable-ts words on collapse: windowed CTC must
    never inherit a broken anchor silently
  - soft-clamp degenerate words to >=30ms, never reject the song
  - NO score gating of any kind
  - fp32 only, no autocast (fp16 XLS-R NaNs)
  - blank token = config.pad_token_id, NOT id 0

All torch/transformers imports are lazy: importing this module on a
torch-free host (local app) must not raise.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np

from KaraokeGen.config import Settings, settings
from KaraokeGen.lyrics import word_text
from KaraokeGen.models import AlignmentResult

log = logging.getLogger(__name__)

FRAME_S = 0.02  # wav2vec2 conv stride 320 @ 16 kHz
_SPECIAL_RE = re.compile(r"[^0-9a-z\u00f1]")  # keep letters + ñ

# Tagalog marker lexicon for the English-song router. tag_ratio = marker
# words / total words; near-zero means the lyrics are English -> whisper
# words beat CTC chars on English singing.
TAG_MARKERS = frozenset(
    "ang ng sa na ako ikaw ay mga hindi kita para kaya bakit kung nang "
    "iyong akin mahal puso araw gabi buwan ilaw tayo ito iyon lang naman "
    "kami sila siya mo ko".split())


def _tag_ratio(words: list[str]) -> float:
    if not words:
        return 0.0
    n = sum(1 for w in words if _word_chars(w) in TAG_MARKERS)
    return n / len(words)


def tag_ratio_text(text: str) -> float:
    """Tagalog-marker density of a whole lyric text (0.0 = pure English)."""
    words = [w for w in (text or "").split() if w]
    return _tag_ratio(words)

_MODELS: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# model loading (lazy, cached per process)
# ---------------------------------------------------------------------------

def _char_vocab(tokenizer) -> dict[str, int]:
    """Single-character, non-special tokens -> id."""
    vocab = tokenizer.get_vocab()
    out = {}
    for tok, tid in vocab.items():
        t = tok.strip()
        if len(t) == 1:
            out[t] = tid
    return out


def _load_model(name: str) -> dict:
    if name in _MODELS:
        return _MODELS[name]
    import torch
    from transformers import Wav2Vec2ForCTC

    tok = None
    try:
        from transformers import Wav2Vec2Processor
        proc = Wav2Vec2Processor.from_pretrained(name)
        tok = proc.tokenizer
    except Exception:
        try:
            from transformers import Wav2Vec2CTCTokenizer
            tok = Wav2Vec2CTCTokenizer.from_pretrained(name)
        except Exception:
            tok = None
    model = Wav2Vec2ForCTC.from_pretrained(name)
    if tok is None:
        # last resort: build charmap from the model's own label dict
        l2i = model.config.label2id
        tok = type("T", (), {"get_vocab": lambda self, l2i=l2i: dict(l2i)})()
    model.eval()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)
    charmap = _char_vocab(tok)
    blank_id = int(getattr(model.config, "pad_token_id", None) or 0)
    pipe_id = charmap.get("|")
    entry = dict(model=model, charmap=charmap, blank_id=blank_id,
                 pipe_id=pipe_id, device=device, star_id=None)
    _MODELS[name] = entry
    log.info("word_ctc loaded %s (%d chars, blank=%d, pipe=%s, %s)",
             name, len(charmap), blank_id, pipe_id, device)
    return entry


def _load_mms() -> dict:
    """Whole-song member: torchaudio MMS_FA (window-FREE monotone Viterbi).
    Char tokens straight from the bundle dict (Tagalog is latin script — no
    uroman needed); <star> is the model's native wildcard for OOV chars;
    blank = <pad>."""
    key = "mms-fa"
    if key in _MODELS:
        return _MODELS[key]
    import torch
    import torchaudio

    bundle = torchaudio.pipelines.MMS_FA
    model = bundle.get_model()
    model.eval()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)
    d = bundle.get_dict()
    charmap = {t: i for t, i in d.items() if len(t) == 1}
    # blank token name varies by torchaudio version ("<pad>" or "-", per the
    # campaign's W3 note: "-" is the CTC blank and must never be a word char)
    blank_id = None
    for k in ("<pad>", "-", "<blank>"):
        if k in d:
            blank_id = int(d[k])
            break
    if blank_id is None:
        raise KeyError("no blank token in MMS_FA dict; keys=%s" % sorted(d))
    star_id = d.get("<star>")
    entry = dict(model=model, charmap=charmap, blank_id=blank_id,
                 pipe_id=None, device=device, star_id=star_id, mms=True)
    _MODELS[key] = entry
    log.info("word_ctc loaded torchaudio MMS_FA (%d chars, blank=%d, star=%s, %s)",
             len(charmap), blank_id, star_id, device)
    return entry


MMS1B_ID = "facebook/mms-1b-all"
MMS1B_FL102_ID = "facebook/mms-1b-fl102"
MMS1B_LANG = "tgl"


def _load_mms1b_family(model_id: str, key: str) -> dict:
    """Shared MMS-1B tgl-adapter loader."""
    if key in _MODELS:
        return _MODELS[key]
    import torch
    from transformers import Wav2Vec2ForCTC, Wav2Vec2Processor

    processor = Wav2Vec2Processor.from_pretrained(model_id)
    processor.tokenizer.set_target_lang(MMS1B_LANG)
    model = Wav2Vec2ForCTC.from_pretrained(
        model_id, target_lang=MMS1B_LANG, ignore_mismatched_sizes=True)
    model.load_adapter(MMS1B_LANG)
    model.eval()
    vocab = processor.tokenizer.get_vocab()
    head_dim = int(model.lm_head.weight.shape[0])
    if head_dim != len(vocab):
        raise RuntimeError(
            f"mms-1b {MMS1B_LANG} adapter mismatch: head {head_dim} != "
             f"vocab {len(vocab)} (adapter/vocab mismatch — refusing to run)")
    blank_id = processor.tokenizer.pad_token_id
    if blank_id is None:
        raise RuntimeError("no pad/blank token in mms-1b tgl vocab")
    charmap = {t: i for t, i in vocab.items() if len(t) == 1}
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)
    model.half()
    entry = dict(model=model, processor=processor, charmap=charmap,
                 blank_id=int(blank_id), pipe_id=None, device=device,
                 star_id=None, mms1b=True, do_normalize=True)
    _MODELS[key] = entry
    log.info("word_ctc loaded %s/%s (vocab %d, head %d, %d chars, "
             "blank=%d, %s, fp16)", model_id, MMS1B_LANG, len(vocab),
             head_dim, len(charmap), blank_id, device)
    return entry


def _load_mms1b() -> dict:
    """facebook/mms-1b-all with the correctly-loaded tgl adapter as an
    alternative whole-song engine (handles slide-class songs better).

    Load path: explicit tokenizer.set_target_lang + model.load_adapter +
    head-shape assert. fp16 on GPU with NaN check. Emits 20ms frames, same
    as MMS_FA; charmap from the tgl vocab (single-char tokens only); blank
    = pad. No <star> — OOV chars fall to the any-speech wildcard."""
    return _load_mms1b_family(MMS1B_ID, "mms-1b-tgl")


def _load_mms1b_fl102() -> dict:
    """facebook/mms-1b-fl102 tgl adapter (experimental Fleurs-102 sibling of
    the mms-1b-all adapter). Same base architecture, same load path; own
    cache key."""
    return _load_mms1b_family(MMS1B_FL102_ID, "mms-1b-fl102-tgl")


def _mms1b_emission_lp(entry: dict, audio: np.ndarray) -> np.ndarray:
    """(T, V) log-softmax for mms-1b (processor normalization + fp16 with
    NaN fallback to fp32)."""
    import torch
    x = (audio - audio.mean()) / (np.sqrt(audio.var() + 1e-7) + 1e-10)
    t = torch.tensor(x, dtype=torch.float32).unsqueeze(0).to(entry["device"])
    with torch.no_grad():
        try:
            logits = entry["model"](t.half()).logits
            if torch.isnan(logits).any():
                raise FloatingPointError
        except FloatingPointError:
            log.warning("word_ctc: mms-1b fp16 NaN — retrying fp32.")
            entry["model"].float()
            logits = entry["model"](t).logits
    lp = torch.log_softmax(logits.float(), dim=-1)
    return lp[0].cpu().numpy()


# ---------------------------------------------------------------------------
# token sequence: per-word char tokens + "|" separators + wildcard for OOV words
# ---------------------------------------------------------------------------

def _word_chars(w: str) -> str:
    return _SPECIAL_RE.sub("", (w or "").lower())


def _build_tokens(words: list[str], charmap: dict[str, int],
                  wild_id: int | None = None):
    """Returns (token_ids, token_word_idx, wildcard_mask).

    token_ids: vocab ids per token (wildcards get wild_id, or -1 when the
               model has no native wildcard -> any-speech column)
    token_word_idx: which word each token belongs to (None for separators)
    wildcard_mask: True where the token is a wildcard
    """
    ids: list[int] = []
    owner: list[int | None] = []
    wild: list[bool] = []
    for wi, w in enumerate(words):
        chars = _word_chars(w)
        in_vocab = [c for c in chars if c in charmap]
        if not in_vocab:
            ids.append(-1 if wild_id is None else int(wild_id))
            owner.append(wi)
            wild.append(True)
            continue
        for c in in_vocab:
            ids.append(charmap[c])
            owner.append(wi)
            wild.append(False)
    return ids, owner, wild


def _token_seq(entry, words: list[str]):
    """Char-token sequence for a word list, with pipe separators between
    words when the model has one. Returns (ids, owner, wild)."""
    ids, owner, wild = _build_tokens(words, entry["charmap"],
                                     wild_id=entry.get("star_id"))
    if entry["pipe_id"] is not None:
        seq_ids, seq_owner, seq_wild = [], [], []
        for k, (tid, ow, wd) in enumerate(zip(ids, owner, wild)):
            if k > 0 and ow != owner[k - 1] and owner[k - 1] is not None:
                seq_ids.append(entry["pipe_id"])
                seq_owner.append(None)
                seq_wild.append(False)
            seq_ids.append(tid)
            seq_owner.append(ow)
            seq_wild.append(wd)
        ids, owner, wild = seq_ids, seq_owner, seq_wild
    return ids, owner, wild


def _content_score(entry, audio: np.ndarray, words: list[str]) -> float | None:
    """Mean token probability of teacher-forcing `words` onto an audio window
    (direction check: right content -> high, wrong words/silence -> low).
    None when the window/words are unusable."""
    if len(audio) < 8000 or not words:
        return None
    ids, _owner, wild = _token_seq(entry, words)
    if not ids:
        return None
    lp = _emission_lp(entry, audio)
    frames, score = _align_tokens(lp, ids, entry["blank_id"], wild)
    return score if frames else None


# ---------------------------------------------------------------------------
# trellis DP (whisperx-style, numpy; model forward on GPU, DP on CPU)
# ---------------------------------------------------------------------------

def _align_tokens(lp: np.ndarray, token_ids: list[int], blank_id: int,
                  wild_mask: list[bool], attack: np.ndarray | None = None,
                  first_mask: np.ndarray | None = None
                  ) -> tuple[list[int], float]:
    """Frame index per token + mean token prob. Wildcard tokens use the
    model's native wildcard column when the id is >= 0 (MMS <star>), else
    the any-speech column log(1 - p_blank).

    Attack prior: `attack` (T,) is a per-frame bonus (e.g. alpha * onset
    novelty from the lead stem) added to the EMIT transition of
    word-initial tokens only (first_mask (N,) in {0,1}). This makes the
    decoder seek vocal attacks for word starts instead of vowel peaks —
    on dense rap, MMS word starts carry a uniform late bias (emissions peak
    on vowel steady-states) and dense material has no silence gaps for the
    post-hoc snap to fire on."""
    T, V = lp.shape
    N = len(token_ids)
    if N == 0 or T == 0:
        return [], 0.0
    blank_lp = lp[:, blank_id]
    # per-token emission logprobs, (T, N) — float32 keeps whole-song DP ~500MB
    tok_lp = np.empty((T, N), dtype=np.float32)
    any_lp = np.log(np.maximum(1e-12, 1.0 - np.exp(blank_lp))).astype(np.float32)
    for j, (tid, w) in enumerate(zip(token_ids, wild_mask)):
        if w and tid >= 0:
            tok_lp[:, j] = lp[:, tid]          # native wildcard (MMS <star>)
        elif w or tid < 0:
            tok_lp[:, j] = any_lp              # any-speech wildcard
        else:
            tok_lp[:, j] = lp[:, tid]
    emit_extra = None
    if attack is not None and first_mask is not None:
        # (T,) bonus broadcast onto word-initial tokens: (T, N) via outer
        emit_extra = (attack.astype(np.float32)[:, None]
                      * first_mask.astype(np.float32)[None, :])
    # trellis[t, j]: best score consuming frames 0..t-1, tokens 0..j-1 emitted
    NEG = np.float32(-1e30)
    trellis = np.full((T + 1, N + 1), NEG, dtype=np.float32)
    trellis[0, 0] = 0.0
    bl = blank_lp.astype(np.float32)
    for t in range(1, T + 1):
        row = trellis[t - 1] + bl[t - 1]                 # stay (blank)
        emit = tok_lp[t - 1]
        if emit_extra is not None:
            emit = emit + emit_extra[t - 1]
        np.maximum(row[1:], trellis[t - 1, :-1] + emit, out=row[1:])
        trellis[t] = row
    # backtrack
    frames = [0] * N
    t, j = T, N
    while t > 0 and j > 0:
        a = trellis[t - 1, j - 1] + tok_lp[t - 1, j - 1]
        if emit_extra is not None:
            a = a + emit_extra[t - 1, j - 1]
        b = trellis[t - 1, j] + bl[t - 1]
        if a > b:
            frames[j - 1] = t - 1
            j -= 1
        t -= 1
    score = float(np.mean(np.exp(tok_lp[frames, np.arange(N)])))
    return frames, score


def _attack_bonus(audio16k: np.ndarray, alpha: float,
                  hop: int = 320, centered: bool = False) -> np.ndarray | None:
    """Per-frame vocal-attack bonus (T,) aligned to the CTC frame grid
    (FRAME_S = hop/sr = 20ms). Gaussian bumps on backtracked onset times —
    attacks mark true word starts. `centered` mean-subtracts the bonus so
    non-attack frames are PENALIZED (doubles the effective gradient).
    Returns None when alpha <= 0 or detection fails."""
    if alpha <= 0 or len(audio16k) < 16000:
        return None
    try:
        import librosa
        env = librosa.onset.onset_strength(y=np.asarray(audio16k), sr=16000,
                                           hop_length=hop)
        frames = librosa.onset.onset_detect(onset_envelope=env, sr=16000,
                                            hop_length=hop, backtrack=True)
        T = len(env)
        bonus = np.zeros(T, dtype=np.float32)
        for f in frames:
            fi = int(f)
            # gaussian bump, sigma ~1.5 frames (30ms)
            lo, hi = max(0, fi - 4), min(T, fi + 5)
            idx = np.arange(lo, hi)
            bonus[lo:hi] += np.exp(-0.5 * ((idx - fi) / 1.5) ** 2)
        m = float(bonus.max())
        if m <= 0:
            return None
        out = alpha * bonus / m
        if centered:
            out = out - float(out.mean())
        return out.astype(np.float32)
    except Exception as exc:
        log.debug("word_ctc: attack bonus unavailable (%s).", exc)
        return None


def _ctc_word_spans(entry, audio: np.ndarray, words: list[str]):
    """Align one line window. Returns list of (start_s, end_s, score) per word
    or None when the window is unusable."""
    if len(audio) < 8000 or not words:
        return None
    ids, owner, wild = _token_seq(entry, words)
    if not ids:
        return None
    lp = _emission_lp(entry, audio)
    frames, _score = _align_tokens(lp, ids, entry["blank_id"], wild)
    if not frames:
        return None
    spans = [None] * len(words)
    for f, ow in zip(frames, owner):
        if ow is None:
            continue
        st, en = f * FRAME_S, (f + 1) * FRAME_S
        cur = spans[ow]
        if cur is None:
            spans[ow] = [st, en]
        else:
            cur[0] = min(cur[0], st)
            cur[1] = max(cur[1], en)
    # words that ended up with no frame (impossible in theory) -> None out
    if any(sp is None for sp in spans):
        return None
    # token probs per word for the score field (diagnostic only, never gated)
    return [(sp[0], sp[1], 1.0) for sp in spans]


# ---------------------------------------------------------------------------
# median ensemble + hygiene
# ---------------------------------------------------------------------------

def _median3(a: float, b: float, c: float) -> float:
    return float(np.median([a, b, c]))


def pitch_onsets(audio: np.ndarray, sr: int = 16000) -> np.ndarray:
    """Voiced-note onsets from f0 (torchcrepe).

    Returns onset times: unvoiced->voiced transitions plus sustained-note
    boundaries (|semitone jump| >= 1.0 between consecutive voiced frames).
    Held vowels have flat amplitude but a SHARP f0 restart at the next note,
    which is exactly the border-word failure mode this targets.
    """
    import torch
    import torchcrepe

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    x = torch.tensor(audio, dtype=torch.float32, device=dev).unsqueeze(0)
    hop = int(sr * 0.010)  # 10 ms
    with torch.no_grad():
        f0, periodicity = torchcrepe.predict(
            x, sr, hop_length=hop, model="tiny", batch_size=512,
            device=dev, return_periodicity=True, fmin=60.0, fmax=1000.0)
    f0 = f0[0].cpu().numpy()
    per = periodicity[0].cpu().numpy()
    voiced = (per > 0.35) & np.isfinite(f0) & (f0 > 0)
    times = np.arange(len(f0)) * 0.010
    onsets: list[float] = []
    prev_v = False
    last_f0 = None
    for i in range(len(f0)):
        v = bool(voiced[i])
        if v and not prev_v:
            onsets.append(float(times[i]))
            last_f0 = f0[i]
        elif v and last_f0 and last_f0 > 0:
            semi = 12.0 * np.log2(max(1e-6, f0[i]) / last_f0)
            if abs(semi) >= 1.0:
                onsets.append(float(times[i]))
                last_f0 = f0[i]
        prev_v = v
    return np.array(sorted(set(onsets)), dtype=np.float64)


def _pitch_snap(starts: list[float], onsets: np.ndarray, back_s: float,
                fwd_s: float, ok_s: float,
                amp_onsets: np.ndarray | None = None,
                amp_ok_s: float = 0.12) -> int:
    """Snap word starts to a pitch onset, preferring the closest PRECEDING
    one (CTC fires after onset evidence accumulates, so starts run late).
    Words already within `ok_s` of a pitch onset are left alone.

    When `amp_onsets` is given, words that already sit on an AMPLITUDE onset
    (within `amp_ok_s`) are also left alone — pitch only adds information
    where amplitude evidence is absent (held vowels, legato), so the snap
    is amplitude-gated to avoid damaging healthy songs.
    """
    if onsets is None or len(onsets) == 0:
        return 0
    moved = 0
    for i, st in enumerate(starts):
        if amp_onsets is not None and len(amp_onsets):
            if float(np.min(np.abs(amp_onsets - st))) <= amp_ok_s:
                continue
        d = onsets - st
        if float(np.min(np.abs(d))) <= ok_s:
            continue
        back = onsets[(d >= -back_s) & (d < 0)]
        fwd = onsets[(d > 0) & (d <= fwd_s)]
        pick = None
        if len(back):
            pick = float(back[-1])
        elif len(fwd):
            pick = float(fwd[0])
        if pick is not None and abs(pick - st) > 1e-6:
            starts[i] = pick
            moved += 1
    return moved


def _hygiene(starts: list[float], ends: list[float], min_dur: float) -> None:
    for k in range(len(starts)):
        if ends[k] - starts[k] < min_dur:
            ends[k] = starts[k] + min_dur
    for k in range(1, len(starts)):
        if starts[k] < ends[k - 1]:
            mid = (starts[k] + ends[k - 1]) / 2.0
            mid = min(max(mid, starts[k - 1] + min_dur), max(mid, starts[k]))
            ends[k - 1] = mid
            if starts[k] < mid:
                starts[k] = mid


def _slide_guard_shifts(seg_words, segments, mms_spans, s,
                        entries=None, audio=None) -> list[float] | None:
    """Per-line shifts re-anchoring block-slid MMS words onto the
    (arbitrated, content-verified) line anchors.

    The whole-song MMS Viterbi occasionally locks a whole section onto the
    wrong repetition of a hook while the line starts in the result carry
    the LA-arbitration + content-verification evidence. When MMS's per-line
    first-word starts deviate from the incoming words' by a SUSTAINED
    RELATIVE shift (> word_slide_thr_s over >= word_slide_min_words words),
    the run is a slide candidate.

    DIRECTION CHECK: the deviation alone cannot tell WHICH side slid — the
    anchors themselves can be wrong while MMS rescued them. So each
    candidate run is decided by CONTENT: the run's first substantial line
    is teacher-forced through the windowed CTC trellis at BOTH placements
    (anchor window vs MMS window). The shift is applied ONLY when the
    anchor placement scores clearly higher (delta >
    word_slide_content_margin); ties, unavailable scores and errors default
    to NO shift.

    Deviations are measured relative to the song-wide median deviation, so a
    global MMS-vs-line calibration (present on every song) never triggers the
    guard; only block-shaped slides do. Returns None when nothing moved.
    """
    if mms_spans is None:
        return None
    thr = float(getattr(s, "word_slide_thr_s", 1.0))
    min_words = int(getattr(s, "word_slide_min_words", 6))

    valid: list[tuple[int, float, int]] = []  # (line_idx, deviation, n_words)
    gi = 0
    for li, words in enumerate(seg_words):
        nw = len(words)
        if nw and gi + nw <= len(mms_spans):
            valid.append((li, float(mms_spans[gi][0]) - float(words[0].start), nw))
        gi += nw
    if len(valid) < 3:
        return None

    # Lookup tables for the self-consistency gate:
    #   _g0[li]       = global word index of line li's first word
    #   _anchor_at[k] = the incoming (arbitrated) start of global word k
    _g0: dict[int, int] = {}
    _anchor_at: dict[int, float] = {}
    gi = 0
    for li, words in enumerate(seg_words):
        if words:
            _g0[li] = gi
            for w in words:
                _anchor_at[gi] = float(w.start)
                gi += 1

    base = float(np.median([d for _, d, _ in valid]))
    rel = {li: d - base for li, d, _ in valid}
    nw_of = {li: nw for li, _, nw in valid}
    order = [li for li, _, _ in valid]

    # median-of-3 smoothing so a single jittery line can't split a real run
    sm: dict[int, float] = {}
    vals = [rel[li] for li in order]
    for k, li in enumerate(order):
        lo, hi = max(0, k - 1), min(len(vals), k + 2)
        sm[li] = float(np.median(vals[lo:hi]))

    def _direction_delta(run: list[int]) -> float | None:
        """content(anchor placement) - content(mms placement) for the run's
        first substantial line. > 0 favors re-anchoring onto the line anchors.
        None = check not applicable (no models passed); -inf = scoring failed
        -> do NOT shift (conservative: preserve ship behavior)."""
        if not entries or audio is None:
            return None
        dur = len(audio) / 16000.0
        for li in run:
            words = seg_words[li]
            texts = [t for t in (word_text(w) for w in words) if t]
            if len(texts) < 3:
                continue
            gi0 = sum(len(seg_words[x]) for x in range(li))
            nw = len(words)
            if gi0 + nw > len(mms_spans):
                return float("-inf")
            seg = segments[li]
            aw0 = max(0.0, float(seg.start) - 0.2)
            aw1 = min(dur, float(seg.end) + 0.35)
            mw0 = max(0.0, float(mms_spans[gi0][0]) - 0.2)
            mw1 = min(dur, float(mms_spans[gi0 + nw - 1][1]) + 0.35)
            if aw1 - aw0 < 0.5 or mw1 - mw0 < 0.5:
                return float("-inf")
            sa: list[float] = []
            smm: list[float] = []
            for entry in entries:
                va = _content_score(
                    entry, audio[int(aw0 * 16000):int(aw1 * 16000)], texts)
                vm = _content_score(
                    entry, audio[int(mw0 * 16000):int(mw1 * 16000)], texts)
                if va is not None:
                    sa.append(va)
                if vm is not None:
                    smm.append(vm)
            if not sa or not smm:
                return float("-inf")
            return (sum(sa) / len(sa)) - (sum(smm) / len(smm))
        return float("-inf")

    margin = float(getattr(s, "word_slide_content_margin", 0.05))
    per_line = bool(getattr(s, "word_slide_per_line", True))
    # Engine self-consistency gate: the content check was tuned on MMS_FA
    # emissions and can mis-fire on mms-1b. New necessary condition: the
    # ENGINE's own word-vs-anchor disagreement must be SMALL just before
    # the run and HUGE inside it (a true slide is a discrete break;
    # engine-everywhere-off is an anchor problem, not an engine slide).
    # Measured from the engine spans directly — engine-agnostic by
    # construction.
    selfcons_on = bool(getattr(s, "word_slide_selfcons", True))
    sc_prev_win = int(getattr(s, "word_slide_selfcons_prev", 30))
    sc_ratio = float(getattr(s, "word_slide_selfcons_ratio", 3.0))
    shifts: dict[int, float] = {}
    run: list[int] = []

    def _selfcons_ok(r: list[int]) -> bool:
        if not selfcons_on or len(order) == 0:
            return True
        li_first = r[0]
        gi_first = _g0[li_first]
        prev_lo = max(0, gi_first - sc_prev_win)
        prev_idx = list(range(prev_lo, gi_first))
        d_run = [abs(float(mms_spans[gi][0]) - float(seg_words[li][0].start))
                 for li in r
                 for gi in [_g0[li]]
                 if gi < len(mms_spans) and seg_words[li]]
        if not d_run:
            return False
        d_run_med = float(np.median(d_run))
        if d_run_med < thr:
            return True  # not even a big engine-anchor gap — nothing to fix
        if not prev_idx:
            return True  # song start: no before-side evidence, allow
        d_prev = [abs(float(mms_spans[gi][0]) - _anchor_at[gi])
                  for gi in prev_idx if gi < len(mms_spans)]
        if not d_prev:
            return True
        d_prev_med = float(np.median(d_prev))
        ok = d_run_med > sc_ratio * max(d_prev_med, 0.25)
        if not ok:
            log.info("word_ctc: slide guard self-consistency veto "
                     "(D_run=%.2f vs D_prev=%.2f on lines %s) — anchors "
                     "look broken, keeping engine words.",
                     d_run_med, d_prev_med, r[:3] + ["..."] if len(r) > 3 else r)
        return ok

    def _flush():
        if not run:
            return
        if sum(nw_of[li] for li in run) >= min_words:
            delta = _direction_delta(run)
            if (delta is None or delta > margin) and _selfcons_ok(run):
                if per_line:
                    # Land EVERY line exactly on its anchor: per-line shifts
                    # preserve MMS's within-line rhythm (its best property)
                    # while pinning each line start to the verified anchor.
                    for li in run:
                        shifts[li] = -sm[li]
                else:
                    sh = -float(np.median([sm[li] for li in run]))
                    for li in run:
                        shifts[li] = sh

    for li in order:
        if run and li != run[-1] + 1:  # non-adjacent line: run broken
            _flush()
            run = []
        if abs(sm[li]) > thr:
            run.append(li)
        elif run:
            _flush()
            run = []
    _flush()
    if not shifts:
        return None
    out = [0.0] * len(seg_words)
    for li, sh in shifts.items():
        out[li] = sh
    return out


def _warp_spans(seg_words, segments, mms_spans, s,
                ) -> list[tuple[float, float]] | None:
    """Per-line de-warp of whole-song MMS spans onto anchor spans.

    For each gated line, maps 1B/FA word starts linearly from the engine
    span [b0, b1] onto the arbitrated anchor span [A0, A1], preserving
    within-line rhythm (relative distribution) while fixing the Viterbi
    stretch dense lines can carry. Word durations preserved (ends shifted
    with starts); starts clamped >= 0.

    Gate (per line, truth-free): span ratio (A1-A0)/(b1-b0) outside
    [word_warp_span_lo, word_warp_span_hi]. Span comparison is invariant
    to uniform anchor bias, so systematic anchor calibration can never
    trigger it — only shape disagreement. Veto: skip the line when BOTH
    endpoints disagree wildly with the anchors (> word_warp_broken_anchor:
    anchors broken, not the engine).
    Degenerate spans (either < 0.5s) skipped; last line uses the
    stable-ts end as A1 with an extra |A1-b1| > 2.0 veto.
    Returns the full replacement span list, or None when nothing fired.
    """
    lo = float(getattr(s, "word_warp_span_lo", 0.85))
    hi = float(getattr(s, "word_warp_span_hi", 1.18))
    broken = float(getattr(s, "word_warp_broken_anchor", 5.0))
    out = [tuple(sp) for sp in mms_spans]
    gi = 0
    n_warped = 0
    n_lines = len(seg_words)
    for li, words in enumerate(seg_words):
        nw = len(words)
        if not nw or gi + nw > len(mms_spans):
            gi += nw
            continue
        try:
            b0 = float(mms_spans[gi][0])
            b1 = float(mms_spans[gi + nw - 1][1])
            A0 = float(words[0].start)
            if li + 1 < n_lines and seg_words[li + 1]:
                A1 = float(seg_words[li + 1][0].start)
                last = False
            else:
                A1 = float(segments[li].end)
                last = True
        except (IndexError, TypeError, ValueError):
            gi += nw
            continue
        gi += nw
        if b1 - b0 < 0.5 or A1 - A0 < 0.5:
            continue
        if abs(A0 - b0) > broken and abs(A1 - b1) > broken:
            continue  # anchors broken here, not the engine
        if last and abs(A1 - b1) > 2.0:
            continue  # unstable last-line anchor end
        ratio = (A1 - A0) / (b1 - b0)
        if lo <= ratio <= hi:
            continue
        for k in range(nw):
            st, en = out[gi - nw + k]
            dur = float(en) - float(st)
            st2 = A0 + (float(st) - b0) * ratio
            st2 = max(0.0, st2)
            out[gi - nw + k] = (st2, st2 + max(0.0, dur))
        n_warped += 1
    if not n_warped:
        return None
    log.info("word_ctc: warp re-spanned %d line(s).", n_warped)
    return out


def _emission_lp(entry, audio: np.ndarray, normalize: bool = True) -> np.ndarray:
    """(T, V) log-softmax emission matrix for a mono-16k clip."""
    import torch
    x = audio
    if normalize:
        x = (audio - audio.mean()) / (np.sqrt(audio.var() + 1e-7) + 1e-10)
    t = torch.tensor(x, dtype=torch.float32).unsqueeze(0).to(entry["device"])
    with torch.no_grad():
        out = entry["model"](t)
    if hasattr(out, "logits"):            # transformers ModelOutput
        logits = out.logits
    elif isinstance(out, tuple):          # torchaudio (output, lengths)
        logits = out[0]
    else:
        logits = out
    logits = logits[0] if logits.dim() == 3 else logits
    lp = torch.log_softmax(logits.float(), dim=-1)
    return lp.cpu().numpy()


def _wholesong_mms_spans(entry, audio: np.ndarray, all_words: list[str],
                         attack: np.ndarray | None = None
                         ) -> list[tuple[float, float]] | None:
    """ALL lyric tokens in one monotone Viterbi path across the full-song
    emission. No windows -> no anchor-inheritance problem. Soft-clamps
    degenerate words to >=30ms.

    `attack` (T,) bonus nudges word-initial tokens onto vocal attacks
    (see _align_tokens).

    OOM robustness: the full-song forward needs ~2.7 GiB on GPU and can
    OOM when several songs run in parallel on a T4 — silently degrading
    long sparse songs to their drifting stable-ts fallback. On OOM the
    pass retries on CPU: MMS_FA is a 315.5M-param wav2vec2, so CPU is
    exact (identical computation, bit-deterministic) and merely slower."""
    if not all_words or len(audio) < 16000:
        return None
    ids, owner, wild = _build_tokens(all_words, entry["charmap"],
                                      wild_id=entry.get("star_id"))
    if not ids:
        return None
    # torchaudio MMS_FA tutorial convention: raw waveform, no normalization
    try:
        lp = _emission_lp(entry, audio, normalize=False)
    except Exception as exc:
        if "out of memory" not in str(exc).lower():
            raise
        import torch
        torch.cuda.empty_cache()
        log.warning("word_ctc: whole-song MMS OOM on GPU (%.0fs audio) — "
                    "retrying on CPU (exact, slower).", len(audio) / 16000)
        model, dev = entry["model"], entry["device"]
        entry["device"] = "cpu"
        model.to("cpu")
        try:
            lp = _emission_lp(entry, audio, normalize=False)
        finally:
            entry["device"] = dev
            model.to(dev)
            torch.cuda.empty_cache()
    first_mask = None
    if attack is not None:
        first_mask = np.zeros(len(ids), dtype=np.float32)
        prev = None
        for j, ow in enumerate(owner):
            if ow != prev:
                first_mask[j] = 1.0
            prev = ow
    frames, _score = _align_tokens(lp, ids, entry["blank_id"], wild,
                                   attack=attack, first_mask=first_mask)
    if not frames:
        return None
    spans: list[list[float] | None] = [None] * len(all_words)
    for f, ow in zip(frames, owner):
        if ow is None:
            continue
        st, en = f * FRAME_S, (f + 1) * FRAME_S
        cur = spans[ow]
        if cur is None:
            spans[ow] = [st, en]
        else:
            cur[0] = min(cur[0], st)
            cur[1] = max(cur[1], en)
    if any(sp is None for sp in spans):
        return None
    return [(sp[0], sp[1]) for sp in spans]


def refine_word_timings(result: AlignmentResult, audio_path,
                        s: Settings = settings, onset_audio_path=None,
                        attack_audio_path=None
                        ) -> tuple[AlignmentResult, dict[str, AlignmentResult]]:
    """Refine per-word timings: whole-song MMS word engine + English router.

    Primary (returned first, mutating `result`): the whole-song MMS member
    outperformed the per-word median ensembles, so the primary is now:
      - tag_ratio < word_ctc_en_tag_ratio (lyrics are English) -> keep the
        stable-ts words (whisper beats CTC chars on English singing)
      - otherwise -> the whole-song MMS spans (onset-snapped + hygiene).

    Diagnostic variants returned in the dict (same run, ignored by
    production):
      med3      — median {stable, w2v2, Khalsuu} windowed
      ctc_only  — mean of the two windowed CTCs
      mms       — whole-song MMS solo

    Failure semantics: never raises past a line — a bad line keeps its
    existing (stable-ts) words. A total failure returns the input unchanged.
    """
    # Primary routing decision (per song), BEFORE any audio/model work:
    # English lyrics keep the stable-ts words (whisper beats CTC chars on
    # English singing). The ensemble path routes English away earlier
    # still; this guard covers direct/plain-path calls so English songs
    # never pay the CTC model-load + MMS cost for nothing.
    seg_words = [[w for w in seg.words if w is not None] for seg in result.segments]
    all_words = [word_text(w) for ws in seg_words for w in ws]
    en_ratio = float(getattr(s, "word_ctc_en_tag_ratio", 0.01))
    forced_en = (getattr(s, "language", "") or "").strip().lower().startswith("en")
    is_english = forced_en or _tag_ratio(all_words) < en_ratio
    if is_english:
        if all_words:
            log.info("word_ctc: tag_ratio=%.4f%s -> stable-ts (English), "
                     "CTC skipped.", _tag_ratio(all_words),
                     " (forced en)" if forced_en else "")
        return result, {}

    try:
        import soundfile as sf
        audio, sr = sf.read(str(audio_path), dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(1)
        if sr != 16000:
            import librosa
            audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)
    except Exception as exc:
        log.warning("word_ctc: audio unreadable (%s) — unchanged.", exc)
        return result, {}

    try:
        entries = [_load_model(m) for m in s.word_ctc_models]
    except Exception as exc:
        log.warning("word_ctc: model load failed (%s) — unchanged.", exc)
        return result, {}

    # whole-song MMS member (window-free anchor). The engine is
    # selectable — "mms_fa" (default) or "mms_1b" (facebook/mms-1b-all
    # tgl adapter, better on slide-class songs). Both feed the SAME
    # downstream machinery (slide guard, gap snap, pitch snap, hygiene)
    # unchanged.
    mms_entry = None
    mms_spans = None
    mms1b = False
    route_1b = False
    if getattr(s, "word_wholesong", True):
        engine = (getattr(s, "word_engine", "mms_fa") or "mms_fa").lower()
        # Engine selection is MANUAL (owner decision): the editor always
        # sends genre="ballad" (ballad/pop -> mms_1b with slide guard off)
        # or genre="hiphop" (MMS-FA path). The dense-rap wps router below
        # is OFF by default (KVC_DENSE_1B_ROUTER=1 re-enables it as an
        # escape hatch). Explicit KVC_WORD_ENGINE wins over everything.
        genre = (getattr(s, "genre", "") or "").strip().lower()
        if genre == "ballad" and engine == "mms_fa":
            engine = "mms_1b"
            route_1b = True
            log.info("word_ctc: genre=ballad -> mms_1b engine, "
                     "slide guard off.")
        elif getattr(s, "dense_1b_router", False) and engine == "mms_fa":
            dur_audio = len(audio) / 16000.0
            wps = (len([t for t in all_words if t]) / dur_audio
                   if dur_audio > 30 else 0.0)
            if wps > float(getattr(s, "dense_1b_wps", 1.4)):
                engine = "mms_1b"
                route_1b = True
                log.info("word_ctc: dense rap (wps=%.2f) -> mms_1b engine, "
                         "slide guard off.", wps)
        try:
            if engine == "mms_1b":
                mms_entry = _load_mms1b()
                mms1b = True
            elif engine == "mms_1b_fl102":
                mms_entry = _load_mms1b_fl102()
                mms1b = True
            else:
                mms_entry = _load_mms()
        except Exception as exc:
            log.warning("word_ctc: %s unavailable (%s) — windowed only.",
                        engine, exc)

    onsets = None
    if s.word_ctc_onset_snap:
        try:
            from KaraokeGen.audio import detect_onsets
            # Onset evidence from the UNGATED LEAD STEM when provided —
            # mix onsets include drum/bass transients and gated-vocals
            # onsets fire on gate-bleed during interludes. The lead stem
            # has vocal-only transients. Falls back to the alignment audio.
            onset_src = None
            if onset_audio_path is not None:
                try:
                    import soundfile as _sf
                    y_o, sr_o = _sf.read(str(onset_audio_path),
                                         dtype="float32")
                    if y_o.ndim > 1:
                        y_o = y_o.mean(1)
                    if sr_o != 16000:
                        import librosa
                        y_o = librosa.resample(y_o, orig_sr=sr_o,
                                               target_sr=16000)
                    onset_src = np.asarray(y_o)
                except Exception as exc:
                    log.debug("word_ctc: onset stem unreadable (%s).", exc)
            if onset_src is None:
                onset_src = np.asarray(audio)
            onsets = detect_onsets(onset_src, 16000)
        except Exception as exc:
            log.debug("word_ctc: onsets unavailable (%s).", exc)

    pre_s = float(getattr(s, "word_ctc_window_pre_s", 0.2))
    post_s = float(getattr(s, "word_ctc_window_post_s", 0.35))
    min_dur = float(getattr(s, "word_ctc_min_word_dur_s", 0.03))
    snap_w = float(getattr(s, "word_ctc_snap_window_s", 0.12))
    collapse_s = float(getattr(s, "word_ctc_collapse_s", 0.04))

    # Fast-song pitch snap trigger: the amplitude-gated pitch snap helps
    # fast songs but is net-zero elsewhere, so triggering it ONLY on fast
    # songs (wps > threshold) keeps the gain and leaves healthy songs
    # byte-identical.
    fast_song = False
    p_on = None
    if getattr(s, "word_pitch_apply", True):
        dur_audio = len(audio) / 16000.0
        wps = (len([t for t in all_words if t]) / dur_audio
               if dur_audio > 30 else 0.0)
        fast_song = wps > float(getattr(s, "word_pitch_wps_thr", 1.8))
        if fast_song:
            try:
                p_on = pitch_onsets(np.asarray(audio), 16000)
                log.info("word_ctc: fast song (wps=%.2f) -> pitch snap "
                         "armed (%d onsets).", wps, len(p_on))
            except Exception as exc:
                log.debug("word_ctc: pitch onsets unavailable (%s).", exc)
                fast_song = False
    # Vocal-attack decode prior. Computed from the LEAD stem when the
    # caller provides one (vocal attacks without drum transients); falls
    # back to the CTC audio. alpha=0 (default) keeps the decode
    # byte-identical to the no-prior path.
    attack = None
    alpha = float(getattr(s, "word_attack_alpha", 0.0))
    if alpha > 0 and mms_entry is not None:
        try:
            src = audio
            if attack_audio_path is not None and Path(str(attack_audio_path)).exists():
                try:
                    import soundfile as _sf
                    y_a, sr_a = _sf.read(str(attack_audio_path), dtype="float32")
                    if y_a.ndim > 1:
                        y_a = y_a.mean(1)
                    if sr_a != 16000:
                        import librosa
                        y_a = librosa.resample(y_a, orig_sr=sr_a, target_sr=16000)
                    src = np.asarray(y_a)
                except Exception as exc:
                    log.debug("word_ctc: attack source unreadable (%s) — "
                              "using CTC audio.", exc)
            attack = _attack_bonus(src, alpha)
            if attack is not None:
                log.info("word_ctc: attack prior on (alpha=%.2f, %d frames "
                         "of bonus).", alpha, int((attack > 0).sum()))
        except Exception as exc:
            log.debug("word_ctc: attack prior failed (%s).", exc)
            attack = None

    if mms_entry is not None:
        try:
            if mms1b:
                # mms-1b path — same monotone decode, engine-specific
                # emission (processor normalization + fp16/NaN handling).
                lp = _mms1b_emission_lp(mms_entry, audio)
                expect = len(audio) / 320
                if abs(lp.shape[0] - expect) > 2:
                    raise RuntimeError(
                        f"mms-1b frame count {lp.shape[0]} != {expect:.0f}")
                ids, owner, wild = _build_tokens(
                    all_words, mms_entry["charmap"],
                    wild_id=mms_entry.get("star_id"))
                if not ids:
                    raise RuntimeError("no tokens for mms-1b decode")
                first_mask = None
                if attack is not None:
                    first_mask = np.zeros(len(ids), dtype=np.float32)
                    prev = None
                    for j, ow in enumerate(owner):
                        if ow != prev:
                            first_mask[j] = 1.0
                        prev = ow
                frames, _score = _align_tokens(
                    lp, ids, mms_entry["blank_id"], wild,
                    attack=attack, first_mask=first_mask)
                spans: list[list[float] | None] = [None] * len(all_words)
                for f, ow in zip(frames, owner):
                    if ow is None:
                        continue
                    st, en = f * FRAME_S, (f + 1) * FRAME_S
                    cur = spans[ow]
                    if cur is None:
                        spans[ow] = [st, en]
                    else:
                        cur[0] = min(cur[0], st)
                        cur[1] = max(cur[1], en)
                if any(sp is None for sp in spans):
                    raise RuntimeError("mms-1b decode left words unassigned")
                mms_spans = [(sp[0], sp[1]) for sp in spans]
            else:
                mms_spans = _wholesong_mms_spans(mms_entry, audio, all_words,
                                                 attack=attack)
            if mms_spans is None:
                log.warning("word_ctc: whole-song MMS returned no spans.")
        except Exception as exc:
            log.warning("word_ctc: whole-song MMS failed (%s).", exc)
            mms_spans = None

    # primary: MMS whole-song spans when available (English exited above;
    # when MMS failed, keep the stable-ts words)
    use_mms_primary = mms_spans is not None
    if all_words:
        log.info("word_ctc: tag_ratio=%.4f -> %s", _tag_ratio(all_words),
                 "MMS whole-song" if use_mms_primary
                 else "stable-ts (MMS unavailable)")

    # Line-anchor slide guard. Compute BEFORE the main loop (runs span
    # multiple lines). Applied to mss at slice time so the primary path, the
    # gap snap and the mms/mmswin variants all see the corrected spans.
    # (A w_result.ens_fallback abstention flag was tried and REMOVED —
    # AlignmentResult is a pydantic model, undeclared attrs raise, so the
    # flag never stuck; the route_1b guard-skip stands as the only 1B
    # protection. Do not re-add without declaring the field AND validating
    # the FA path.)
    slide_shifts = None
    if use_mms_primary and getattr(s, "word_slide_guard", True) \
            and not route_1b:
        try:
            slide_shifts = _slide_guard_shifts(
                seg_words, result.segments, mms_spans, s,
                entries=entries, audio=audio)
            if slide_shifts:
                n_sh = sum(len(seg_words[li]) for li, sh in
                           enumerate(slide_shifts) if sh)
                n_runs = sum(1 for li, sh in enumerate(slide_shifts)
                             if sh and (li == 0 or not slide_shifts[li - 1]))
                mx = max(abs(sh) for sh in slide_shifts)
                log.info("word_ctc: slide guard re-anchored %d word(s) in "
                         "%d run(s) (max shift %.2fs).", n_sh, n_runs, mx)
        except Exception as exc:
            log.debug("word_ctc: slide guard failed (%s).", exc)
            slide_shifts = None

    # Per-line de-warp (engine-agnostic; runs after the guard so the
    # guard's anchor-relative geometry sees raw engine spans; warped spans
    # feed the primary path and the mms/mmswin variants at slice time).
    if use_mms_primary and mms_spans is not None and getattr(
            s, "word_warp_lines", False):
        try:
            warped = _warp_spans(seg_words, result.segments, mms_spans, s)
            if warped is not None:
                mms_spans = warped
        except Exception as exc:
            log.debug("word_ctc: warp failed (%s).", exc)

    # per-line variant spans: (seg, {variant: [(start,end), ...]})
    line_variants: list[tuple] = []
    n_refined = n_fallback = 0
    gi = 0  # global word index cursor
    prev_end_global = -1e9  # for the gap-conditional snap

    for seg in result.segments:
        words = seg_words[len(line_variants)]
        texts = [t for t in (word_text(w) for w in words) if t]
        nw = len(words)

        runs = None
        if words and len(texts) == len(words) and (seg.end - seg.start) > 0.2:
            w0 = max(0.0, float(seg.start) - pre_s)
            w1 = min(len(audio) / 16000.0, float(seg.end) + post_s)
            window = audio[int(w0 * 16000):int(w1 * 16000)]
            runs = []
            for entry in entries:
                try:
                    spans = _ctc_word_spans(entry, window, texts)
                except Exception as exc:
                    log.debug("word_ctc: line align failed (%s).", exc)
                    spans = None
                if spans is None:
                    runs = None
                    break
                runs.append([(st + w0, en + w0) for (st, en, _sc) in spans])
            if runs is not None:
                durs = [en - st for spans in runs for (st, en) in spans]
                if durs and float(np.median(durs)) < collapse_s:
                    runs = None  # collapsed window -> void windowed evidence

        mss = None
        if mms_spans is not None and gi + nw <= len(mms_spans):
            mss = mms_spans[gi:gi + nw]
            if slide_shifts is not None and slide_shifts[len(line_variants)]:
                sh = slide_shifts[len(line_variants)]
                mss = [(st + sh, en + sh) for (st, en) in mss]
        gi += nw

        stable = [(float(w.start), float(w.end)) for w in words]
        variants: dict[str, list[tuple[float, float]]] = {
            "med3": list(stable), "ctc_only": list(stable),
            "mms": mss if mss is not None else list(stable),
            "mmswin": mss if mss is not None else list(stable),
            # E3: raw windowed members for the agreement gate (c1 = w2v2,
            # c2 = Khalsuu-fil; stable fallback when the window collapsed)
            "winA": list(stable), "winB": list(stable),
        }

        if runs is not None:
            n_refined += 1
            c1, c2 = runs[0], runs[1]
            variants["ctc_only"] = [
                ((c1[k][0] + c2[k][0]) / 2.0, (c1[k][1] + c2[k][1]) / 2.0)
                for k in range(nw)]
            variants["winA"] = [(c1[k][0], c1[k][1]) for k in range(nw)]
            variants["winB"] = [(c2[k][0], c2[k][1]) for k in range(nw)]
            for k in range(nw):
                variants["med3"][k] = (_median3(stable[k][0], c1[k][0], c2[k][0]),
                                       _median3(stable[k][1], c1[k][1], c2[k][1]))
        else:
            n_fallback += 1

        # MMS-anchored windows for the windowed CTC pair: windows derived
        # from the whole-song MMS spans, then per-word median {MMS, w2v2,
        # Khalsuu} — three evidence sources sharing GOOD anchors.
        if mss is not None and words and len(texts) == len(words):
            try:
                mw0 = max(0.0, mss[0][0] - pre_s)
                mw1 = min(len(audio) / 16000.0, mss[-1][1] + post_s)
                if mw1 - mw0 > 0.3:
                    mwindow = audio[int(mw0 * 16000):int(mw1 * 16000)]
                    mruns = []
                    for entry in entries:
                        try:
                            mspans = _ctc_word_spans(entry, mwindow, texts)
                        except Exception:
                            mspans = None
                        if mspans is None:
                            mruns = None
                            break
                        mruns.append([(st + mw0, en + mw0)
                                      for (st, en, _sc) in mspans])
                    if mruns is not None:
                        mdurs = [en - st for spans in mruns
                                 for (st, en) in spans]
                        if mdurs and float(np.median(mdurs)) < collapse_s:
                            mruns = None
                    if mruns is not None:
                        variants["mmswin"] = [
                            (_median3(mss[k][0], mruns[0][k][0],
                                      mruns[1][k][0]),
                             _median3(mss[k][1], mruns[0][k][1],
                                      mruns[1][k][1]))
                            for k in range(nw)]
            except Exception as exc:
                log.debug("word_ctc: mmswin line failed (%s).", exc)

        # primary: MMS whole-song spans (or keep stable on English songs /
        # when MMS is unavailable). Medians all dragged the MMS member
        # below its solo score — primary is MMS, not a median.
        if use_mms_primary and mss is not None:
            starts = [mss[k][0] for k in range(nw)]
            ends = [mss[k][1] for k in range(nw)]
            if onsets is not None:
                # Gap-conditional snap (nearest onset, either side): words
                # after >=gap_thr of silence get a wider window than the
                # default 0.12s. Backward-only lost overall — most of the
                # gain comes from forward snaps onto the true onset just
                # after the CTC start. Keep nearest-both-sides.
                gap_thr = float(getattr(s, "word_gap_snap_thr_s", 0.5))
                wide_w = float(getattr(s, "word_gap_snap_window_s", 0.35))
                for k in range(nw):
                    d = onsets - starts[k]
                    window = (wide_w if (starts[k] - prev_end_global) >= gap_thr
                              else snap_w)
                    m = np.abs(d)
                    j = int(np.argmin(m))
                    if m[j] <= window:
                        starts[k] = float(onsets[j])
                    if ends[k] > prev_end_global:
                        prev_end_global = ends[k]
            _hygiene(starts, ends, min_dur)
            # amplitude-gated pitch snap on fast songs only
            if fast_song and p_on is not None:
                _pitch_snap(starts, p_on,
                            float(getattr(s, "word_pitch_back_s", 0.30)),
                            float(getattr(s, "word_pitch_fwd_s", 0.10)),
                            float(getattr(s, "word_pitch_ok_s", 0.06)),
                            amp_onsets=onsets, amp_ok_s=snap_w)
                _hygiene(starts, ends, min_dur)
            for k, w in enumerate(words):
                w.start = float(starts[k])
                w.end = float(ends[k])

        line_variants.append((seg, variants, [(float(w.start), float(w.end))
                                              for w in words]))

    # build the diagnostic variant results
    others: dict[str, AlignmentResult] = {}
    try:
        from KaraokeGen.models import Segment, Word
        for key in ("med3", "ctc_only", "mms", "mmswin", "winA", "winB"):
            segs = []
            for seg, variants, primary in line_variants:
                spans = variants[key]
                ws = [Word(text=word_text(w), start=float(st), end=float(en),
                           probability=float(getattr(w, "probability", 1.0)))
                      for w, (st, en) in zip(seg.words, spans)] if seg.words else []
                segs.append(Segment(words=ws, start=float(seg.start),
                                    end=float(seg.end), text=seg.text))
            others[key] = AlignmentResult(segments=segs,
                                          line_notes=result.line_notes)
    except Exception as exc:
        log.debug("word_ctc: variant build failed (%s).", exc)

    # LA word-starts diagnostic variant (cross-arch member for the
    # agreement gate; rows stashed by la_line_starts, absent on ensemble
    # fallback — words omitted past the rows' end).
    try:
        from KaraokeGen import ensemble as _ens
        _rows = list(getattr(_ens, "_LA_WORD_ROWS", []) or [])
    except Exception:
        _rows = []
    if _rows:
        try:
            from KaraokeGen.models import Segment, Word
            segs, _ri = [], 0
            for seg in result.segments:
                ws = []
                for w in (seg.words or []):
                    if _ri >= len(_rows):
                        break
                    ws.append(Word(
                        text=word_text(w), start=float(_rows[_ri]),
                        end=float(_rows[_ri]) + 0.1,
                        probability=float(getattr(w, "probability", 1.0))))
                    _ri += 1
                segs.append(Segment(words=ws, start=float(seg.start),
                                    end=float(seg.end), text=seg.text))
            others["law"] = AlignmentResult(segments=segs,
                                            line_notes=result.line_notes)
            log.info("word_ctc: law variant dumped (%d words).", _ri)
        except Exception as exc:
            log.debug("word_ctc: law variant failed (%s).", exc)

    # Pitch-onset snap variants: snap the primary word starts to voiced
    # note onsets (f0), targeting the border band where amplitude onsets
    # are flat but pitch restarts sharply.
    #   "pitch"  — ungated (helps fast songs, damages healthy ones)
    #   "pitchg" — only where NO amplitude onset is near
    if getattr(s, "word_pitch_snap", True):
        try:
            from KaraokeGen.models import Segment, Word
            if p_on is None:  # already computed for a fast song above
                p_on = pitch_onsets(np.asarray(audio), 16000)
            back_s = float(getattr(s, "word_pitch_back_s", 0.30))
            fwd_s = float(getattr(s, "word_pitch_fwd_s", 0.10))
            ok_s = float(getattr(s, "word_pitch_ok_s", 0.06))
            for key, amp in (("pitch", None), ("pitchg", onsets)):
                segs, moved_tot, n_tot = [], 0, 0
                for seg, variants, primary in line_variants:
                    if not primary:
                        segs.append(Segment(words=[], start=float(seg.start),
                                            end=float(seg.end), text=seg.text))
                        continue
                    st_l = [p[0] for p in primary]
                    en_l = [p[1] for p in primary]
                    moved_tot += _pitch_snap(st_l, p_on, back_s, fwd_s, ok_s,
                                             amp_onsets=amp, amp_ok_s=snap_w)
                    n_tot += len(st_l)
                    _hygiene(st_l, en_l, min_dur)
                    ws = [Word(text=word_text(w), start=float(a), end=float(b),
                               probability=float(getattr(w, "probability", 1.0)))
                          for w, a, b in zip(seg.words, st_l, en_l)]
                    segs.append(Segment(words=ws, start=float(seg.start),
                                        end=float(seg.end), text=seg.text))
                others[key] = AlignmentResult(segments=segs,
                                              line_notes=result.line_notes)
                log.info("word_ctc: %s moved %d/%d words (%d pitch onsets).",
                         key, moved_tot, n_tot, len(p_on))
        except Exception as exc:
            log.warning("word_ctc: pitch snap unavailable (%s).", exc)

    log.info("word_ctc: windowed %d line(s) (fallback %d), primary=%s, mms=%s.",
             n_refined, n_fallback,
             "mms" if use_mms_primary else "stable-ts",
             "on" if mms_spans is not None else "off")
    return result, others
