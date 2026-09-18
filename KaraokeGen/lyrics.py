"""Pure lyric/timing helpers. No GPU, no audio, no heavy deps.

Operates on the Pydantic AlignmentResult model so the local app can run
timing edits + verification with zero torch dependency.
"""
from __future__ import annotations

import re
from collections import Counter

from KaraokeGen.config import Settings, settings
from KaraokeGen.models import AlignmentResult, LineNote, Segment, Word

# --- hint regexes ([mm:ss.mmm] start, <<+0.5>> shift) ---
HINT_START_RE = re.compile(r"\[\s*(\d+)\s*(?::\s*(\d{1,2})\s*(?:[.:]\s*(\d{1,3}))?)?\s*\]")
HINT_SHIFT_RE = re.compile(r"<<\s*([+-]?\d+(?:\.\d+)?)\s*s?>>")
HINT_TOKEN_RE = re.compile(
    r"^(\[\s*\d+(\s*:\s*\d{1,2}([.:]\s*\d{1,3})?)?\s*\]"
    r"|<<\s*[+-]?\d+(?:\.\d+)?\s*s?>>)$"
)


# --- time formatting ---

def transcript_to_lines(text: str, max_words: int = 10) -> str:
    """Split a punctuated transcript blob (Qwen3-ASR returns one paragraph)
    into karaoke lines: sentence-ending punctuation breaks lines, and longer
    sentences break at commas near `max_words`. Without this split an
    auto-transcribed paragraph would become one unusable mega-line."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", text)
    lines: list[str] = []
    for sent in parts:
        words = sent.split()
        if len(words) <= max_words:
            lines.append(sent)
            continue
        cur: list[str] = []
        best_break = -1  # last comma position within the hard-cap window
        for i, w in enumerate(words):
            cur.append(w)
            if w.rstrip('"\'').endswith((",", ";")):
                best_break = len(cur)
            # comma break once past max_words, or hard cap without one
            if (len(cur) >= max_words and best_break >= max(3, max_words - 3)) \
                    or len(cur) >= 2 * max_words:
                k = best_break if (len(cur) >= 2 * max_words
                                   and 3 <= best_break <= len(cur)) else len(cur)
                lines.append(" ".join(cur[:k]).rstrip(",;"))
                cur = cur[k:]
                best_break = -1
        if cur:
            lines.append(" ".join(cur))
    return "\n".join(l.strip() for l in lines if l.strip())


# --- transcript noise (model-emitted musical-note emojis) ---
# ASR models decorate sung parts with musical notes ("Ang tanging kailangan",
# wrapped in note emojis). Whisper does this itself — it was trained on
# subtitled video containing such emojis (openai/whisper#1205) — and Qwen can
# too. They are singing detectors, not lyrics: strip them before the text ever
# reaches the lyrics box or the aligner. User-pasted lyrics never pass here.
_TRANSCRIPT_NOISE_RE = re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE00-\uFE0F"
    "\U0001F000-\U0001F2FF]+"
)


def strip_transcript_noise(text: str) -> str:
    """Remove musical-note emojis/pictographs from a model transcript and drop
    lines left empty. Keeps all letters, punctuation and spacing intact."""
    if not text:
        return text
    cleaned = _TRANSCRIPT_NOISE_RE.sub("", text)
    return "\n".join(ln.strip() for ln in cleaned.splitlines() if ln.strip())


def fmt_time(t: float, s: Settings = settings) -> str:
    """H:MM:SS.cs for ASS timestamps."""
    cs = int(round(max(0.0, float(t or 0.0)) * 100))
    sec, cs = divmod(cs, 100)
    minute, sec = divmod(sec, 60)
    hour, minute = divmod(minute, 60)
    return "{:d}:{:02d}:{:02d}.{:02d}".format(hour, minute, sec, cs)


# --- word accessors (work on pydantic Word or duck-typed stable-ts word) ---

def word_text(w) -> str:
    return (getattr(w, "text", None) or getattr(w, "word", None) or "").strip()


def word_start(w, default: float = 0.0) -> float:
    try:
        return float(getattr(w, "start", default) or default)
    except (TypeError, ValueError):
        return float(default)


def word_end(w, default: float | None = None) -> float:
    if default is None:
        default = word_start(w, 0.0)
    try:
        return float(getattr(w, "end", default) or default)
    except (TypeError, ValueError):
        return float(default)


# --- hint parsing / stripping ---

def parse_line_hints(line: str) -> tuple[float | None, float | None]:
    """[mm:ss.mmm] must be the FIRST token on the line; <<+0.5>> must be the
    LAST token. Anything else in the line is treated as real lyric text, so
    songs containing bracketed numbers or <<..>> as literal text are never
    misparsed as hints."""
    start_s, shift_s = None, None
    ln = line.lstrip()
    m = HINT_START_RE.match(ln)
    if m:
        after = ln[m.end():]
        if not after or after[0].isspace():
            a, b, c = m.groups()
            if b is None:
                start_s = float(a)
            else:
                frac = float("0." + c) if c else 0.0
                start_s = int(a) * 60 + int(b) + frac
    m2 = HINT_SHIFT_RE.search(line)
    if m2:
        if not line[m2.end():].strip():
            shift_s = float(m2.group(1))
    return start_s, shift_s


def strip_hints(text: str) -> str:
    out = []
    for ln in (text or "").splitlines():
        t = HINT_START_RE.sub("", ln)
        t = HINT_SHIFT_RE.sub("", t).strip()
        if t:
            out.append(t)
    return "\n".join(out)


# --- normalization ---

def norm_word(w: str) -> str:
    return re.sub(r"[^a-z0-9\u00e0-\u024f]+", "", (w or "").lower())


# --- apply hints to an AlignmentResult (in place) ---

def _shift_segment(seg: Segment, delta: float) -> None:
    if not delta:
        return
    seg.start = max(0.0, seg.start + delta)
    seg.end = max(0.0, seg.end + delta)
    for w in seg.words:
        w.start = max(0.0, w.start + delta)
        w.end = max(0.0, w.end + delta)


def _seg_has_words(seg: Segment) -> bool:
    return any(word_text(w) for w in seg.words)


def apply_timing_hints(result: AlignmentResult, text: str) -> AlignmentResult:
    """[mm:ss.mmm] at start of a line sets that line's start ABSOLUTELY;
    <<+0.5>> / <<-0.3>> at end shifts it relative to alignment.
    Editor lines map to segments in order (one editor line = one segment)."""
    segs = result.segments
    si = 0
    for ln in (text or "").splitlines():
        if not ln.strip():
            continue
        start_s, shift_s = parse_line_hints(ln)
        if start_s is None and shift_s is None:
            si += 1
            continue
        while si < len(segs) and not _seg_has_words(segs[si]):
            si += 1
        if si >= len(segs):
            break
        seg = segs[si]
        delta = 0.0
        if start_s is not None:
            delta += start_s - seg.start
        if shift_s is not None:
            delta += shift_s
        if delta:
            _shift_segment(seg, delta)
        si += 1
    _strip_hint_tokens(result)
    return result


def _strip_hint_tokens(result: AlignmentResult) -> None:
    for seg in result.segments:
        keep = [w for w in seg.words if not HINT_TOKEN_RE.match(word_text(w))]
        if len(keep) != len(seg.words):
            seg.words = keep
            # Only rewrite seg.text when a hint word was actually stripped —
            # otherwise an editor-edited line (seg.text != words) would be
            # silently clobbered back to stale word text during render.
            seg.text = " ".join(word_text(w) for w in keep)


# --- line shaping (deprecated: simple flow always honors line breaks) ---
# These helpers were for the auto-split/merge path when honor_line_breaks=False.
# Kept for backward compat but not used in simple flow (honor=True always).
# Do not delete until tests cover the verbatim path below.

def plain_len(words) -> int:
    return len(" ".join(word_text(w) for w in words))


def split_words_to_chunks(words, s: Settings = settings) -> list[list]:
    if not words:
        return []
    chunks: list[list] = []
    cur: list = []
    for w in words:
        trial = cur + [w]
        plain = plain_len(trial)
        nwords = len(trial)
        if cur:
            t0 = word_start(cur[0])
            t1 = word_end(w)
            dur = max(0.0, t1 - t0)
        else:
            dur = 0.0
        overflow = (
            cur
            and (
                plain > s.max_chars_per_line
                or nwords > s.max_words_per_line
                or dur > s.max_line_duration_s
            )
        )
        if overflow:
            chunks.append(cur)
            cur = [w]
        else:
            cur = trial
    if cur:
        chunks.append(cur)
    return chunks


def line_from_words(words, s: Settings = settings) -> Segment | None:
    words = [w for w in words if w is not None and word_text(w)]
    if not words:
        return None
    cursor = max(0.0, word_start(words[0], 0.0))
    fixed: list[Word] = []
    for w in words:
        ws = max(cursor, word_start(w, cursor))
        we = max(ws + 0.03, word_end(w, ws + 0.12))
        if isinstance(w, Word):
            w.start, w.end = ws, we
            fixed.append(w)
        else:
            fixed.append(Word(text=word_text(w), start=ws, end=we,
                              probability=getattr(w, "probability", 1.0)))
        cursor = we
    start = fixed[0].start
    end = fixed[-1].end
    if end <= start:
        end = start + 0.2
    return Segment(words=fixed, start=start, end=end,
                   text=" ".join(word_text(w) for w in fixed))


def merge_short_lines(lines: list[Segment], s: Settings = settings) -> list[Segment]:
    if not lines:
        return []
    out = [lines[0]]
    for ln in lines[1:]:
        prev = out[-1]
        combined_words = prev.words + ln.words
        plain = plain_len(combined_words)
        nwords = len(combined_words)
        dur = max(0.0, ln.end - prev.start)
        prev_short = len(prev.words) < s.min_words_keep or (prev.end - prev.start) < s.min_line_duration_s
        cur_short = len(ln.words) < s.min_words_keep or (ln.end - ln.start) < s.min_line_duration_s
        can_merge = (
            (prev_short or cur_short)
            and plain <= s.max_chars_per_line
            and nwords <= s.max_words_per_line
            and dur <= s.max_line_duration_s * 1.25
            and (ln.start - prev.end) < 0.45
        )
        if can_merge:
            merged = line_from_words(combined_words, s)
            if merged:
                out[-1] = merged
            else:
                out.append(ln)
        else:
            out.append(ln)
    return out


def lines_from_result(result: AlignmentResult, s: Settings = settings) -> list[Segment]:
    """Simple flow: one segment = one lyric line, verbatim start/end.
    Honors editor line breaks; word timings only provide text."""
    lines: list[Segment] = []
    for seg in result.segments:
        words = [w for w in seg.words if w is not None and word_text(w)]
        if not words:
            continue
        # Verbatim — simple flow always honors line breaks
        start = max(0.0, seg.start)
        lines.append(Segment(
            words=words,
            start=start,
            end=max(seg.end, start + 0.05),
            text=seg.text or " ".join(word_text(w) for w in words),
        ))
    lines.sort(key=lambda ln: ln.start)
    return lines


# --- verification report ---

def verify_alignment(result: AlignmentResult, expected_text: str, s: Settings = settings) -> str:
    """Return a human-readable verification report string."""
    lines = [ln for ln in lines_from_result(result, s) if ln]
    exp_words = [norm_word(w) for w in re.split(r"\s+", (expected_text or "").strip()) if norm_word(w)]
    act_words: list[str] = []
    low_conf: list[tuple[str, float, float, float]] = []
    for ln in lines:
        for w in ln.words:
            act_words.append(norm_word(word_text(w)))
            try:
                p = float(getattr(w, "probability", 1.0) or 1.0)
            except (TypeError, ValueError):
                p = 1.0
            if p < 0.5:
                low_conf.append((word_text(w), p, ln.start, ln.end))

    remaining = Counter(act_words)
    missing: list[str] = []
    for w in exp_words:
        if remaining.get(w, 0) > 0:
            remaining[w] -= 1
        else:
            missing.append(w)

    out: list[str] = []
    out.append("\n=== Lyric alignment verification ===")
    out.append("Lyrics words expected : %d" % len(exp_words))
    out.append("Words aligned on audio: %d" % len(act_words))
    out.append("Words NOT found in audio: %d" % len(missing))
    if missing:
        out.append("  missing examples: %s" % ", ".join(missing[:12]))
    out.append("Low-confidence words (p<0.5): %d" % len(low_conf))
    for w, p, st, en in low_conf[:10]:
        out.append("  %-20s p=%.2f  at %s" % (w, p, fmt_time(st)))

    notes = result.line_notes
    if notes:
        out.append("Per-line word check (official lyrics vs audio):")
        for i, n in enumerate(notes[:len(lines)]):
            if n.missing:
                out.append("  line %d: %d word(s) NOT detected on vocals: %s"
                           % (i + 1, n.missing, ", ".join(n.miss_words[:10])))
        bad = [n for n in notes if n.status != "aligned"]
        if bad:
            out.append("  %d line(s) with approx/not-found timing - check them in the editor" % len(bad))

    issues = 0
    for i, ln in enumerate(lines):
        dur = ln.end - ln.start
        if dur > 12.0:
            head = " ".join(word_text(w) for w in ln.words[:6])
            out.append("  WARN line %d is %.1fs long (%s...) - split or shift with <<+0.5>>" % (i + 1, dur, head[:40]))
            issues += 1
    for i in range(len(lines) - 1):
        gap = lines[i + 1].start - lines[i].end
        if gap > 4.0:
            out.append("  WARN gap of %.1fs between line %d and line %d" % (gap, i + 1, i + 2))
            issues += 1
    if not lines:
        out.append("  WARN: nothing aligned at all!")
        issues += 1
    if not issues and not missing and not low_conf:
        out.append("Alignment looks good - no issues found.")
    out.append("===================================")
    return "\n".join(out)
