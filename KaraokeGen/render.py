"""Pure ASS subtitle generation + ffmpeg MP4 burn. No torch.

result_to_ass operates on the Pydantic AlignmentResult so the local app
can render/re-render instantly without any GPU. from_stable_ts converts a
stable-ts result object into the Pydantic model (GPU side only).
"""
from __future__ import annotations

import logging
import subprocess

from KaraokeGen.config import Settings, settings
from KaraokeGen.lyrics import (
    fmt_time,
    lines_from_result,
    plain_len,
    word_end,
    word_start,
    word_text,
)
from KaraokeGen.models import AlignmentResult, LineNote, Segment, Word

log = logging.getLogger(__name__)


def escape_ass(text: str, s: Settings = settings) -> str:
    """Escape text for ASS. The Text field is the LAST Dialogue field, so
    commas need no protection. Literal braces are backslash-escaped and
    newlines become \\N. The text must NOT be wrapped in braces: libass
    treats anything inside { ... } as an override tag block and hides it."""
    bs = s.bs
    t = (text or "")
    # Replace curly/smart quotes with straight ones (font spacing fix)
    t = t.replace("\u2019", "'")  # right single quotation mark
    t = t.replace("\u2018", "'")  # left single quotation mark
    t = t.replace("\u201c", '"')  # left double quotation mark
    t = t.replace("\u201d", '"')  # right double quotation mark
    t = t.replace("\u2014", "--")  # em dash
    t = t.replace("\u2013", "-")   # en dash
    t = t.replace("\\", bs + "\\")
    t = t.replace("{", bs + "{").replace("}", bs + "}")
    t = t.replace("\n", bs + "N")
    return t


def _line_plain(ln: Segment, s: Settings = settings) -> str:
    # The editor edits seg.text (double-click a chip); words are only the
    # draft-time internals and can go stale after a text edit. Render the
    # line's text verbatim so edited lyrics reach the MP4.
    text = (ln.text or " ".join(word_text(w) for w in ln.words)).strip()
    return escape_ass(text, s)


def _word_karaoke_body(seg: Segment, s: Settings = settings) -> str:
    """Strict word-level karaoke body: per-word {\\k<cs>} tags, never falls
    back to line-level {\\kf}. This is the *word-only* version — it always
    produces per-word highlighting.

    Same interface as _line_plain (Segment -> ASS text) so callers can swap
    `result_to_ass` <-> `result_to_ass_word` without changing anything else.

    Timing reuse: if `seg.words` count matches the display token count, the
    editor's word timings are used verbatim (only the text labels are swapped
    to the display casing). Timings are positions, not text — a typo fix with
    the same word count must NOT discard dragged boundaries. Only when the
    counts differ (words added/deleted) are timings synthesized by evenly
    splitting seg.start..seg.end, guaranteeing every displayed word gets a
    karaoke syllable — no line-level fallback.

    Gaps between words become their own {\\k<gap>} pause syllable so the
    highlight pauses exactly where the singer pauses, instead of jumping.

    No-delay/no-advance guarantee: ASS only resolves centiseconds (10ms), so
    each boundary is rounded ONCE to its absolute centisecond
    (round(t*100)) anchored to the Dialogue Start (which is itself
    round(seg.start*100)). Emitted durations are differences of those rounded
    absolutes, so cumulative error stays <=5ms (one rounding step) no matter
    how many words are on the line. The old per-duration round(dur*100)
    accumulated +-5ms per word (measured +50ms by line end on short-word
    rap) — every follower rendered late.
    """
    display_text = (seg.text or " ".join(word_text(w) for w in seg.words)).strip()
    if not display_text:
        return ""

    display_tokens: list[str] = display_text.split()
    if not display_tokens:
        return ""

    words = [w for w in seg.words if word_text(w).strip()]
    # Decide whether original word timings can be reused verbatim.
    # Count + sanity only — never gate on text equality. A typo fix
    # ("world" -> "wurld") or punct/case change keeps the same positions;
    # discarding all dragged timings for the whole line would lose the
    # user's per-word edits.
    use_original = False
    if words and len(words) == len(display_tokens):
        # Validate timings are sane and monotonic; otherwise synthesize
        sane = True
        for w in words:
            try:
                ws = float(getattr(w, "start", None))
                we = float(getattr(w, "end", None))
            except Exception:
                sane = False
                break
            if we is None or ws is None or we <= ws or ws < -0.01:
                sane = False
                break
        if sane:
            for k in range(1, len(words)):
                if float(words[k].start) < float(words[k - 1].start) - 1e-3:
                    sane = False
                    break
                gap = float(words[k].start) - float(words[k - 1].end)
                if gap > s.word_max_dur_s * 2 + 1e-6:
                    sane = False
                    break
        use_original = sane

    if use_original:
        highlight_words: list[Word] = words  # type: ignore[assignment]
        # Replace text with display tokens (preserve display casing/text)
        # but keep timings from original words.
        fixed: list[Word] = []
        for w, tok in zip(highlight_words, display_tokens):
            fixed.append(Word(text=tok, start=float(w.start), end=float(w.end),
                              probability=float(getattr(w, "probability", 1.0) or 1.0)))
        highlight_words = fixed
    else:
        # Synthesize: evenly split seg span across display tokens
        span = float(seg.end) - float(seg.start)
        if span <= 0.05 or span > 30.0:
            # degenerate line — give each word a minimum slot
            span = max(0.3, len(display_tokens) * 0.4)
        per = span / len(display_tokens)
        highlight_words = []
        for idx, tok in enumerate(display_tokens):
            ws = float(seg.start) + idx * per
            we = ws + per
            # keep last word exactly at seg.end to avoid drift
            if idx == len(display_tokens) - 1:
                we = float(seg.end)
            highlight_words.append(Word(text=tok, start=ws, end=we, probability=1.0))

    bs = s.bs
    # Anchored centiseconds: Dialogue Start is fmt_time(seg.start) =
    # round(seg.start*100). Anchor every boundary to round(abs*100) so the
    # emitted differences telescope exactly to the rounded absolutes.
    t0_cs = int(round(float(seg.start) * 100))
    # Absolute rounded positions for each word start/end.
    start_cs = [int(round(float(w.start) * 100)) for w in highlight_words]
    end_cs = [int(round(float(w.end) * 100)) for w in highlight_words]
    # First word starts at Dialogue Start by construction
    # (seg.start == first word start via syncLineSpanFromWords); clamp so a
    # stale seg.start can never inject a leading offset.
    start_cs[0] = t0_cs
    # Guard monotonicity after rounding (two boundaries <5ms apart can land
    # on the same cs; a word must still occupy >=1cs to stay visible).
    for k in range(len(highlight_words)):
        if end_cs[k] <= start_cs[k]:
            end_cs[k] = start_cs[k] + 1
    parts: list[str] = []
    prev_end_cs = t0_cs
    for idx, w in enumerate(highlight_words):
        txt_esc = escape_ass(word_text(w), s)
        sc = start_cs[idx]
        ec = end_cs[idx]
        if idx > 0:
            gap_cs = sc - prev_end_cs
            if gap_cs > 0:
                # kf on the space too: invisible glyphs -> the sweep glides
                # through the gap, keeping the continuous line-fill look
                parts.append("{" + bs + "kf" + str(gap_cs) + "} ")
            else:
                parts.append(" ")
        dur_cs = max(1, ec - sc)
        # \kf = smooth left-to-right wipe over THIS word's slot: looks like a
        # continuous line-level fill but paced by real word timing.
        parts.append("{" + bs + "kf" + str(dur_cs) + "}" + txt_esc)
        prev_end_cs = sc + dur_cs

    return "".join(parts)


def _style_for_offset(offset: int) -> str:
    ao = abs(offset)
    if ao == 0:
        return "Active"
    if ao == 1:
        return "Near"
    return "Far"


def _pitch_for_style(style: str, s: Settings = settings) -> int:
    if style == "Active":
        return s.row_pitch
    if style == "Near":
        return max(48, int(s.row_pitch * 0.78))
    return max(40, int(s.row_pitch * 0.68))


def _layout_y_positions(i, j_lo, j_hi, s: Settings = settings) -> dict:
    ys = {i: float(s.center_y)}
    y = float(s.center_y)
    for j in range(i + 1, j_hi):
        prev_style = _style_for_offset((j - 1) - i)
        style = _style_for_offset(j - i)
        step = 0.5 * (_pitch_for_style(prev_style, s) + _pitch_for_style(style, s))
        y = y + step
        ys[j] = y
    y = float(s.center_y)
    for j in range(i - 1, j_lo - 1, -1):
        next_style = _style_for_offset((j + 1) - i)
        style = _style_for_offset(j - i)
        step = 0.5 * (_pitch_for_style(next_style, s) + _pitch_for_style(style, s))
        y = y - step
        ys[j] = y
    return ys


def result_to_ass(result: AlignmentResult, s: Settings = settings) -> str:
    header = (
        f"[Script Info]\n"
        f"ScriptType: v4.00+\n"
        f"PlayResX: {s.video_w}\n"
        f"PlayResY: {s.video_h}\n"
        f"WrapStyle: 2\n"
        f"ScaledBorderAndShadow: yes\n"
        f"\n"
        f"[V4+ Styles]\n"
        f"Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Active,{s.ass_font},{s.active_font},&H00FFFFFF,&H0000FFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,{s.active_outline},1,5,60,60,40,1\n"
        f"Style: Near,{s.ass_font},{s.near_font},&H00DDDDDD,&H0000DDDD,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,{s.near_outline},0,5,60,60,40,1\n"
        f"Style: Far,{s.ass_font},{s.far_font},&H00AAAAAA,&H0000AAAA,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,2,0,5,60,60,40,1\n"
        f"\n"
        f"[Events]\n"
        f"Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = lines_from_result(result, s)
    n = len(lines)
    if not n:
        return header + "\n"

    cx = s.video_w // 2
    bs = s.bs
    T0 = float(lines[0].start)
    out = [header]
    white = "{" + bs + "c&HFFFFFF&}"
    yellow = "{" + bs + "c&H00FFFF&}"
    dim_white = "{" + bs + "c&HDDDDDD&}"
    dim_yellow = "{" + bs + "c&H00DDDD&}"

    def emit_row(t0, t1, style, y, body, fade_in=False, fade_out=False, layer=0, karaoke_tag="", move_tag=""):
        pos_tag = move_tag or (bs + "pos(%d,%d)" % (cx, int(round(y))))
        tags = "{" + bs + "an5" + bs + "q2" + pos_tag
        if karaoke_tag:
            tags += karaoke_tag
        if fade_in:
            tags += bs + "fad(300,0)"
        if fade_out:
            tags += bs + "fad(0,300)"
        tags += "}"
        out.append("Dialogue: %d,%s,%s,%s,,0,0,0,,%s" % (
            layer, fmt_time(t0, s), fmt_time(t1, s), style, tags + body))

    if T0 > 0.1:
        # Opening lines appear only shortly before the first verse starts, so
        # lyrics don't sit on screen for the whole instrumental intro.
        t0_init = max(0.0, T0 - 1.0)
        j_hi = min(n, s.line_window + 1)
        ys = _layout_y_positions(0, 0, j_hi, s)
        for j in range(0, j_hi):
            y = ys[j]
            if y < 40 or y > s.video_h - 40:
                continue
            style = _style_for_offset(j - 0)
            body = (yellow if j == 0 else dim_yellow) + _line_plain(lines[j], s)
            emit_row(t0_init, T0, style, y, body, fade_in=True, layer=(1 if j == 0 else 0))

    for i in range(n):
        t_start = float(lines[i].start)
        if i < n - 1:
            t_end = float(lines[i + 1].start)
        else:
            t_end = float(lines[i].end) + s.lead_s
        if t_end <= t_start:
            t_end = t_start + 0.2

        window = s.line_window
        j_lo = max(0, i - window)
        j_hi = min(n, i + window + 1)
        ys = _layout_y_positions(i, j_lo, j_hi, s)

        # Target positions for the NEXT section (active line i+1). Lines animate
        # from their current position up to these targets, so the whole block
        # scrolls up smoothly and the next line arrives at center exactly when
        # it becomes active. The active line stays centered for most of its
        # section and only scrolls up near the end.
        if i < n - 1:
            j_lo_next = max(0, i + 1 - window)
            j_hi_next = min(n, i + 1 + window + 1)
            ys_next = _layout_y_positions(i + 1, j_lo_next, j_hi_next, s)
        else:
            ys_next = ys

        # The whole visible block drifts upward CONTINUOUSLY over the entire
        # section (one step per verse), like a teleprompter. Each line animates
        # from its position now to its position when the next line is active,
        # so motion is constant and there is never a static-then-jump feel.
        # The active line sits at center when its verse starts and drifts up
        # gently as it is sung; the next line arrives at center exactly when
        # it becomes active.
        D = max(0.05, t_end - t_start)
        move_t2 = int(round(D * 1000))
        up_step = (ys[i - 1] - float(s.center_y)) if i > 0 else -float(s.row_pitch)

        for j in range(j_lo, j_hi):
            y = ys[j]
            if y < 40 or y > s.video_h - 40:
                continue
            style = _style_for_offset(j - i)
            leaving = j not in ys_next
            if leaving:
                # Leaving line: keeps drifting up one step (same speed as the
                # block) and fades out so it never pops out of existence.
                y_end = y + up_step
            else:
                y_end = ys_next[j]
            if i == n - 1 or abs(int(round(y_end)) - int(round(y))) <= 2:
                move_tag = ""
            else:
                move_tag = bs + "move(%d,%d,%d,%d,0,%d)" % (
                    cx, int(round(y)), cx, int(round(y_end)), move_t2)
            # Fade in lines at the very start of the video (no intro), and fade
            # in each new line as it enters the bottom of the visible block.
            # Skip the entering fade in the first section when the intro block
            # already showed those lines, to avoid a blink at the boundary.
            entering_fade = (j == i + window and i + window < n) and not (i == 0 and T0 > 0.1)
            first_section_fade = (i == 0 and T0 <= 0.1)
            fade_in = entering_fade or first_section_fade
            # Every state renders the verse as ONE row with the style's own
            # fixed font size — no per-line \fs, no \N anywhere, so \kf timing
            # is exact and the layout never changes between states.
            if j < i:
                body = dim_white + _line_plain(lines[j], s)
            elif j == i:
                line_end_f = float(lines[i].end)
                line_duration_cs = max(1, int(round((line_end_f - t_start) * 100)))
                kf_tag = bs + "kf" + str(line_duration_cs)
                body = _line_plain(lines[j], s)
                actual_row_end = max(t_end, float(lines[i].end))
                emit_row(
                    t_start, actual_row_end, style, y, body,
                    fade_in=fade_in,
                    fade_out=(i == n - 1),
                    layer=1,
                    karaoke_tag=kf_tag,
                    move_tag=move_tag,
                )
                continue
            else:
                body = (yellow if style == "Near" else dim_yellow) + _line_plain(lines[j], s)
            emit_row(
                t_start, t_end, style, y, body,
                fade_in=fade_in,
                fade_out=leaving,
                layer=0,
                move_tag=move_tag,
            )

    return "\n".join(out) + "\n"


def result_to_ass_word(result: AlignmentResult, s: Settings = settings) -> str:
    """Word-level karaoke: same layout/scroll as result_to_ass but the
    ACTIVE line uses per-word {\\k} tags from _word_karaoke_body — strictly
    word-level, no line-level fallback.

    Interface is identical to result_to_ass (AlignmentResult -> ASS string) so
    experiments can swap the two functions one-for-one; line-level production
    stays untouched and word-level runs separately via this entry point.
    """
    header = (
        f"[Script Info]\n"
        f"ScriptType: v4.00+\n"
        f"PlayResX: {s.video_w}\n"
        f"PlayResY: {s.video_h}\n"
        f"WrapStyle: 2\n"
        f"ScaledBorderAndShadow: yes\n"
        f"\n"
        f"[V4+ Styles]\n"
        f"Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Active,{s.ass_font},{s.active_font},&H00FFFFFF,&H0000FFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,{s.active_outline},1,5,60,60,40,1\n"
        f"Style: Near,{s.ass_font},{s.near_font},&H00DDDDDD,&H0000DDDD,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,{s.near_outline},0,5,60,60,40,1\n"
        f"Style: Far,{s.ass_font},{s.far_font},&H00AAAAAA,&H0000AAAA,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,2,0,5,60,60,40,1\n"
        f"\n"
        f"[Events]\n"
        f"Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = lines_from_result(result, s)
    n = len(lines)
    if not n:
        return header + "\n"

    cx = s.video_w // 2
    bs = s.bs
    T0 = float(lines[0].start)
    out = [header]
    yellow = "{" + bs + "c&H00FFFF&}"
    dim_yellow = "{" + bs + "c&H00DDDD&}"
    dim_white = "{" + bs + "c&HDDDDDD&}"

    def emit_row(t0, t1, style, y, body, fade_in=False, fade_out=False, layer=0, karaoke_tag="", move_tag=""):
        pos_tag = move_tag or (bs + "pos(%d,%d)" % (cx, int(round(y))))
        tags = "{" + bs + "an5" + bs + "q2" + pos_tag
        if karaoke_tag:
            tags += karaoke_tag
        if fade_in:
            tags += bs + "fad(300,0)"
        if fade_out:
            tags += bs + "fad(0,300)"
        tags += "}"
        out.append("Dialogue: %d,%s,%s,%s,,0,0,0,,%s" % (
            layer, fmt_time(t0, s), fmt_time(t1, s), style, tags + body))

    if T0 > 0.1:
        t0_init = max(0.0, T0 - 1.0)
        j_hi = min(n, s.line_window + 1)
        ys = _layout_y_positions(0, 0, j_hi, s)
        for j in range(0, j_hi):
            y = ys[j]
            if y < 40 or y > s.video_h - 40:
                continue
            style = _style_for_offset(j - 0)
            body = (yellow if j == 0 else dim_yellow) + _line_plain(lines[j], s)
            emit_row(t0_init, T0, style, y, body, fade_in=True, layer=(1 if j == 0 else 0))

    for i in range(n):
        t_start = float(lines[i].start)
        if i < n - 1:
            t_end = float(lines[i + 1].start)
        else:
            t_end = float(lines[i].end) + s.lead_s
        if t_end <= t_start:
            t_end = t_start + 0.2

        window = s.line_window
        j_lo = max(0, i - window)
        j_hi = min(n, i + window + 1)
        ys = _layout_y_positions(i, j_lo, j_hi, s)

        if i < n - 1:
            j_lo_next = max(0, i + 1 - window)
            j_hi_next = min(n, i + 1 + window + 1)
            ys_next = _layout_y_positions(i + 1, j_lo_next, j_hi_next, s)
        else:
            ys_next = ys

        D = max(0.05, t_end - t_start)
        move_t2 = int(round(D * 1000))
        up_step = (ys[i - 1] - float(s.center_y)) if i > 0 else -float(s.row_pitch)

        for j in range(j_lo, j_hi):
            y = ys[j]
            if y < 40 or y > s.video_h - 40:
                continue
            style = _style_for_offset(j - i)
            leaving = j not in ys_next
            if leaving:
                y_end = y + up_step
            else:
                y_end = ys_next[j]
            if i == n - 1 or abs(int(round(y_end)) - int(round(y))) <= 2:
                move_tag = ""
            else:
                move_tag = bs + "move(%d,%d,%d,%d,0,%d)" % (
                    cx, int(round(y)), cx, int(round(y_end)), move_t2)
            entering_fade = (j == i + window and i + window < n) and not (i == 0 and T0 > 0.1)
            first_section_fade = (i == 0 and T0 <= 0.1)
            fade_in = entering_fade or first_section_fade
            if j < i:
                body = dim_white + _line_plain(lines[j], s)
            elif j == i:
                # WORD-LEVEL ONLY — no line-level fallback
                body = _word_karaoke_body(lines[j], s)
                actual_row_end = max(t_end, float(lines[j].end))
                emit_row(
                    t_start, actual_row_end, style, y, body,
                    fade_in=fade_in,
                    fade_out=(i == n - 1),
                    layer=1,
                    karaoke_tag="",  # per-word \\k lives inside body
                    move_tag=move_tag,
                )
                continue
            else:
                body = (yellow if style == "Near" else dim_yellow) + _line_plain(lines[j], s)
            emit_row(
                t_start, t_end, style, y, body,
                fade_in=fade_in,
                fade_out=leaving,
                layer=0,
                move_tag=move_tag,
            )

    return "\n".join(out) + "\n"


# --- ffmpeg render ---

def _escape_ffmpeg_ass_path(path) -> str:
    p = str(path).replace("\\", "/")
    p = p.replace("'", r"\'").replace("[", "\\[").replace("]", "\\]")
    p = p.replace(":", "\\:").replace(",", "\\,").replace("=", "\\=").replace(";", "\\;")
    return p


def _parse_bg_color(bg: str) -> str:
    """Whitelist hex colors only. The value lands in an ffmpeg lavfi filter
    graph (color=c=...), so anything but a strict hex color is rejected and
    falls back to the default — this closes filter-graph injection via the
    render API's bg_color field."""
    import re
    bg = (bg or "#101010").strip()
    if re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})", bg):
        return "0x" + bg[1:]
    log.warning("rejecting non-hex bg_color %r, using default", bg[:32])
    return "0x101010"


def render_video(instrumental, ass_path, out_path, bg_color: str = "#101010",
                 s: Settings = settings) -> str:
    color = _parse_bg_color(bg_color)
    ass_esc = _escape_ffmpeg_ass_path(ass_path)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=%s:s=%dx%d:r=%d" % (color, s.video_w, s.video_h, s.video_fps),
        "-i", str(instrumental),
        "-vf", "ass=%s" % ass_esc, "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-shortest", str(out_path),
    ]
    subprocess.run(cmd, check=True)
    return str(out_path)


# --- converter from stable-ts result -> Pydantic model (GPU side only) ---

def from_stable_ts(result, line_notes=None) -> AlignmentResult:
    """Convert a stable-ts/stable_whisper result (or our _Result) into the
    torch-free Pydantic AlignmentResult. line_notes is optional list of dicts
    matching LineNote fields."""
    segs: list[Segment] = []
    for seg in getattr(result, "segments", []) or []:
        words: list[Word] = []
        for w in getattr(seg, "words", None) or []:
            txt = word_text(w)
            if not txt:
                continue
            words.append(Word(
                text=txt,
                start=word_start(w, 0.0),
                end=word_end(w, word_start(w, 0.0) + 0.1),
                probability=float(getattr(w, "probability", 1.0) or 1.0),
            ))
        if not words:
            continue
        segs.append(Segment(
            words=words,
            start=float(getattr(seg, "start", words[0].start)),
            end=float(getattr(seg, "end", words[-1].end)),
            text=getattr(seg, "text", " ".join(w.text for w in words)),
        ))
    notes = []
    for n in line_notes or []:
        if isinstance(n, LineNote):
            notes.append(n)
        elif hasattr(n, "get"):
            notes.append(LineNote(
                missing=int(n.get("missing", 0)),
                miss_words=list(n.get("miss_words", [])),
                ratio=float(n.get("ratio", 0.0)),
                status=n.get("status", "aligned"),
                text=n.get("text", ""),
            ))
        else:
            notes.append(LineNote(
                missing=int(getattr(n, "missing", 0)),
                miss_words=list(getattr(n, "miss_words", [])),
                ratio=float(getattr(n, "ratio", 0.0)),
                status=getattr(n, "status", "aligned"),
                text=getattr(n, "text", ""),
            ))
    return AlignmentResult(segments=segs, line_notes=notes)
