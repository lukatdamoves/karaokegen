"""Single source of truth for all tunable constants.

No module-level side effects (the old helpers.py did WORK.mkdir() at import).
Settings is a frozen dataclass; the global `settings` can be swapped for tests.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    # --- video / ASS layout ---
    video_w: int = 1280
    video_h: int = 720
    video_fps: int = 30
    row_pitch: int = 72
    line_window: int = 2
    lead_s: float = 2.5

    max_chars_per_line: int = 32
    max_words_per_line: int = 6
    max_line_duration_s: float = 3.0
    min_line_duration_s: float = 0.35
    min_words_keep: int = 2

    active_font: int = 42
    near_font: int = 42
    far_font: int = 42
    active_outline: int = 2
    near_outline: int = 2
    far_outline: int = 2
    ass_font: str = "Noto Sans CJK SC"
    # Hygiene bounds for per-word \kf durations (seconds).
    word_min_dur_s: float = 0.08
    word_max_dur_s: float = 3.0

    # --- vocals pre-clean before ASR/alignment (kill bleed ghost/noise) ---
    # The separated vocals stem always carries some instrumental bleed. Before
    # it reaches Whisper/stable-ts we highpass rumble and noise-gate the quiet
    # bits between phrases, so the aligner can't hallucinate words there.
    # A -40dB gate was found to eat quiet sung verses, so the gate defaults
    # OFF; KVC_ASR_GATE=on restores it for bleed-heavy dense material.
    asr_highpass_hz: float = 80.0
    asr_gate: bool = field(
        default_factory=lambda: os.environ.get("KVC_ASR_GATE", "off") == "on")
    asr_gate_threshold_db: float = -40.0

    # --- alignment ghost resistance ---
    # Stricter VAD threshold (0.35 = faster-whisper default) rejects bleed that
    # Whisper's permissive default would treat as speech.
    align_vad_threshold: float = 0.4
    # A whole segment is dropped ONLY when it is mostly padding/interpolated
    # tokens (prob ~0, ' ...'), or it is a tiny blip with no confident word.
    # We deliberately do NOT drop whole segments on low mean/median confidence:
    # sung verses carry low Whisper word-probability, and dropping them
    # collapses the lyric line structure (line breaks must survive the filter).

    # --- timing ---
    timing_offset_s: float = 0.0

    # --- timing polish (onset snap) ---
    # Snap segment starts to vocal onsets — makes line starts land right
    # when the singer actually starts singing. Only affects the first word
    # of each line (segment start), not individual words.
    snap_to_onsets: bool = True
    # Nearest-onset snap window. Wide windows (0.4) snapped good starts onto
    # WRONG boundaries (previous word tails) — measured regression on shanti.
    word_snap_window_s: float = 0.12
    # Extend line ends forward when there's still vocal energy. Fixes the
    # systematic early-end issue where highlight finishes before the singer
    # stops. Max extension in seconds.
    line_end_extend_s: float = 2.5
    # The extension stops at the first sustained silence gap (energy below
    # threshold for this long) after the last active run. Keeps a repeated
    # chorus line from swallowing the quiet interlude before the next line.
    line_end_confirm_s: float = 0.12

    # --- end trim (past-the-last-vocal) ---
    # A line whose end sits more than end_trim_trigger_s past the last vocal
    # frame in its own span is trimmed to last-vocal + end_trim_grace_s.
    end_trim_trigger_s: float = 0.8
    end_trim_grace_s: float = 0.5

    # --- degenerate line expansion ---
    # A line the aligner couldn't anchor (vocals missing from the stem — soft
    # intro lost in separation, echo classified as backing) collapses to a
    # <0.15s flash. Expand it into the free gap between neighbors (max 6s).
    degenerate_max_expand_s: float = 6.0

    # --- stretched-word clamp (mis-anchored repeats) ---
    # On repeated lyrics the aligner can anchor words onto a LATER occurrence
    # of the same text, stretching the line across the interlude ('Ikaw,
    # ikaw ay dilaw' got 'dilaw' 14s after the previous word). Continuous
    # singing never has a 3s+ gap between consecutive words of one line, so
    # segments spanning longer than clamp_seg_span_s get such words collapsed
    # onto the previous word's end. Purely structural — echo phrases fill the
    # gap with real vocal energy, so audio cannot distinguish them.
    clamp_seg_span_s: float = 8.0
    clamp_word_gap_s: float = 3.0

    # --- alignment method ---
    # OWNER PREFERENCE (2026-08): plain stable-ts full-text alignment
    # (model.align + single-pass refine).
    # Set KVC_REFINE_TIMINGS=0 to skip refine — ~2x faster alignment with
    # slightly less precise word timings (line-level render is unaffected).
    refine_timings: bool = field(
        default_factory=lambda: os.environ.get("KVC_REFINE_TIMINGS", "1") == "1")

    # --- word-level CTC ensemble ---
    # After line alignment, refine per-word timings with a per-word median of
    # {stable-ts, wav2vec2-base-960h, Khalsuu Filipino XLS-R}, run inside
    # production line windows on vocals_asr. Whole-line fallback to
    # stable-ts words on collapse; never gates on CTC scores.
    word_ensemble: bool = field(
        default_factory=lambda: os.environ.get("KVC_WORD_ENSEMBLE", "1") == "1")
    word_ctc_models: tuple = (
        "facebook/wav2vec2-base-960h",
        "Khalsuu/filipino-wav2vec2-l-xls-r-300m-official",
    )
    word_ctc_window_pre_s: float = 0.2
    word_ctc_window_post_s: float = 0.35
    # audio the CTC trellis aligns against. "mix" = the original audio
    # (the models were validated on ungated mixes; gated vocals_asr can
    # collapse line windows on sparse material).
    # "vocals" = vocals_asr (only used by the plain path, which has no mix).
    word_ctc_source: str = "mix"
    # soft-clamp degenerate words (never reject the song)
    word_ctc_min_word_dur_s: float = 0.03
    # a line whose median CTC word duration falls below this is a collapsed
    # window -> keep stable-ts words for the whole line
    word_ctc_collapse_s: float = 0.04
    # onset snap on median word starts
    word_ctc_onset_snap: bool = True
    word_ctc_snap_window_s: float = 0.12
    # gap-conditional snap: words preceded by >= this much silence get
    # the wider snap window (CTC fires late on the first word after a
    # silence; the onset there is unambiguous).
    word_gap_snap_thr_s: float = 0.5
    word_gap_snap_window_s: float = 0.35
    # --- whole-song word engine selection ---
    # "mms_fa": torchaudio MMS_FA — the default ship engine.
    # "mms_1b": facebook/mms-1b-all tgl adapter — alternative engine that
    # handles slide-class songs better; slide guard runs off on its spans.
    # "mms_1b_fl102": experimental Fleurs-102 sibling adapter.
    # Downstream machinery (slide guard, gap snap, pitch snap, hygiene) runs
    # unchanged on top of either engine's spans. KVC_WORD_ENGINE selects.
    word_engine: str = field(
        default_factory=lambda: os.environ.get("KVC_WORD_ENGINE", "mms_fa"))
    # --- line-anchor slide guard for the whole-song MMS path ---
    # The whole-song MMS Viterbi occasionally block-slides (locks a section
    # Slide guard: the whole-song MMS Viterbi occasionally block-slides
    # (locks a section onto the wrong repetition of a hook) while the
    # arbitrated+content-verified line anchors stay correct. When MMS's
    # per-line first words deviate from the incoming words by a SUSTAINED
    # RELATIVE shift (> thr over >= min_words words), the slid block is
    # re-anchored onto the line anchors (uniform per-run shift; MMS's
    # within-line distribution preserved). Relative to the song median, so a
    # global MMS-vs-line calibration never triggers it. KVC_WORD_SLIDE_GUARD=0
    # disables.
    word_slide_guard: bool = field(
        default_factory=lambda: os.environ.get("KVC_WORD_SLIDE_GUARD", "1") == "1")
    word_slide_thr_s: float = 1.0
    word_slide_min_words: int = 6
    # Direction check: a candidate run is only re-anchored when the
    # anchor placement's content score beats the MMS placement's by more than
    # this margin (ties/scoring failures keep the MMS words).
    word_slide_content_margin: float = 0.05
    # Per-line shifts (every line pinned to its anchor) instead of a
    # uniform per-run shift.
    word_slide_per_line: bool = True
    # --- vocal-attack decode prior ---
    # On dense rap the whole-song MMS words can carry a uniform late bias
    # (emissions peak on vowel steady-states; dense material has no silence
    # gaps for the post-hoc snap). The fix: a per-frame attack bonus
    # (gaussian bumps on backtracked lead-stem onsets) added to the EMIT
    # transition of word-initial tokens in the Viterbi — the decoder seeks
    # attacks instead of vowel peaks. alpha=0 disables.
    word_attack_alpha: float = field(
        default_factory=lambda: float(os.environ.get("KVC_WORD_ATTACK_ALPHA", "0")))
    # Pitch-onset snap: snaps word starts to voiced-note onsets (torchcrepe),
    # preferring the closest PRECEDING onset because CTC fires late. Targets
    # the border band where held vowels have flat amplitude but a sharp f0
    # restart.
    word_pitch_snap: bool = field(
        default_factory=lambda: os.environ.get("KVC_PITCH_SNAP", "1") == "1")
    # Apply the amplitude-gated pitch snap to the PRIMARY word starts on
    # fast songs only (wps > thr) — the trigger isolates the gain to songs
    # that need it.
    word_pitch_apply: bool = field(
        default_factory=lambda: os.environ.get("KVC_PITCH_APPLY", "1") == "1")
    word_pitch_wps_thr: float = 1.8
    word_pitch_back_s: float = 0.30
    word_pitch_fwd_s: float = 0.10
    word_pitch_ok_s: float = 0.06
    # whole-song MMS_FA member: window-free monotone Viterbi over the full
    # song — the anchor-inheritance fix for lines whose stable-ts/CTC
    # windows collapse.
    word_wholesong: bool = True
    # Fast-rap router: wps > this AND tag_ratio < this -> plain whisper
    # with rap-tuned durations. DISABLED (the plain path underperforms the
    # MMS path on dense material in production).
    fast_rap_router: bool = False
    fast_rap_wps: float = 1.8
    fast_rap_tag: float = 0.30
    # Dense-rap 1B router: RETIRED as a default (owner decision — genre is
    # now always user-selected, so Hiphop/R&B takes the FA path and
    # Ballad/Pop takes the 1B path explicitly). The router code stays
    # behind KVC_DENSE_1B_ROUTER=1 as an opt-in escape hatch. Explicit
    # KVC_WORD_ENGINE selection wins over everything.
    dense_1b_router: bool = field(
        default_factory=lambda: os.environ.get("KVC_DENSE_1B_ROUTER", "0") == "1")
    # Words-per-second threshold for the retired router above.
    dense_1b_wps: float = 1.4
    # --- per-line de-warp of whole-song MMS spans ---
    # The whole-song Viterbi can stretch dense lines slow within the line,
    # resetting at line starts. Warping is gated per line on SPAN
    # disagreement (bias-invariant: uniform anchor bias cannot trigger it)
    # with a broken-anchor veto. Maps each gated line's span [b0,b1] onto
    # the arbitrated anchor span [A0,A1] (next line's anchor start; last
    # line: stable-ts end). KVC_WORD_WARP_LINES=1 enables. Engine-agnostic
    # (works on FA spans).
    word_warp_lines: bool = field(
        default_factory=lambda: os.environ.get("KVC_WORD_WARP_LINES", "0") == "1")
    word_warp_span_lo: float = 0.85
    word_warp_span_hi: float = 1.18
    word_warp_broken_anchor: float = 5.0
    # stable-ts aligner="new" (redesigned head selection), never tested
    # upstream of us. Env-gated: KVC_ALIGN_NEW=1.
    align_new_heads: bool = field(
        default_factory=lambda: os.environ.get("KVC_ALIGN_NEW", "0") == "1")
    # English-song router: tag_ratio below this -> lyrics are English ->
    # plain whisper pipeline, no LA arbitration, no word-CTC.
    word_ctc_en_tag_ratio: float = 0.01
    # English word ensemble — per-word median {stable-ts, whisper word_ts,
    # LA per-word} on the English branch. All three members must be present
    # per word, else that word keeps stable-ts.
    en_word_ensemble: bool = field(
        default_factory=lambda: os.environ.get("KVC_EN_WORD_ENSEMBLE", "1") == "1")
    # Density gate: vocal duty cycle above this -> member B (whisper-en
    # word_ts) alone, the best English word engine on dense vocals. Below
    # -> median3, because B drifts through long instrumentals.
    en_density_thr: float = 0.62

    # --- ensemble (lead-stem dual-aligner + content verification) ---
    # Master switch. True = separate an extra mel-band-roformer-karaoke LEAD stem,
    # align stable-ts AND LA-Multilingual on it, then content-verify disagreements.
    # False = legacy single-aligner path (stable-ts on vocals_raw-derived stem).
    align_ensemble: bool = field(
        default_factory=lambda: os.environ.get("KVC_ALIGN_ENSEMBLE", "1") == "1")
    # Lead-vocal extraction model (second separation pass on the mix).
    lead_model: str = "mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt"
    # LA-Multilingual checkpoint filename under model_cache_dir.
    la_checkpoint: str = "checkpoint_Baseline"
    # Arbitration knobs (tuned during development).
    ens_agree_s: float = 0.35      # |W-L| below this -> midpoint + micro-snap
    ens_bracket_s: float = 1.0     # disagreement bracket >= this -> in-span onset priority
    ens_margin: float = 0.03       # content-prob margin to override classic choice
    ens_floor: float = 0.45        # winner prob must exceed this
    ens_guard_ratio: float = 0.6   # early-LA guard: L < ratio*W ...
    ens_guard_gap: float = 3.0     # ... and gap > this => discard L (intro hallucination)
    ens_max_shift_s: float = 2.5   # safety: revert any line move larger than this
    # Multi-singer rescue (Option A): when the content verifier finds NO
    # confident evidence on the lead stem (the signature of a second singer
    # filtered out of vocals_lead), re-score the same candidates on the
    # all-voices stem before falling back to onset arbitration.
    ens_rescue: bool = field(
        default_factory=lambda: os.environ.get("KVC_ENS_RESCUE", "1") == "1")
    ens_rescue_floor: float = 0.40  # min raw-stem prob to accept the rescued choice

    # --- stable-ts refine (stable-whisper 2.19+ API) ---
    # refine() mutes audio to find the latest start / earliest end of each
    # word. only_voice_freq band-limits to 200-5000 Hz so instrumental bleed
    # can't hold token probability up (ghost resistance at refine time).
    # Defaults are deliberately CONSERVATIVE: too-aggressive refine makes
    # word timing much worse on singing (it moves boundaries away from the
    # cross-attention alignment), and faster-whisper refine is slow.
    refine_steps: str = "se"
    refine_word_level: bool = True
    refine_precision: float = 0.3
    refine_only_voice_freq: bool = False
    refine_prob_threshold: float = 0.5
    refine_rel_dur_change: float = 0.5

    # --- separation (audio-separator auto-downloads) ---
    # 3-stem karaoke model: one pass produces lead vocals + backing vocals +
    # instrumental. bs_karaoke_3stem_giantailab is NOT in the official
    # audio-separator catalog; it's manually cached on the Modal Volume and
    # added to models.json at runtime in modal_app.py.
    vocal_model: str = "bs_karaoke_3stem_giantailab.ckpt"
    # Karaoke bed model: removes ONLY the lead vocal, keeps the 2nd voice /
    # harmony in the bed. instrumental_bv (this model) minus instrumental_std
    # (3-stem) derives a clean backing-voice stem the 3-stem model can't
    # isolate on its own (its Backing_vocal stem bleeds the music in).
    # Gabox V2 = the community's chosen single karaoke model (the basis of the
    # SDR ~10.6 "karaoke" ensemble preset) at the same runtime as V1. If max
    # backing retention is ever wanted over speed, swap this pass for the
    # 3-model ensemble (viperx + v2 + becruily, avg_wave) — ~3x separation.
    # Official audio-separator catalog model (no custom patching needed).
    karaoke_bed_model: str = "mel_band_roformer_karaoke_gabox_v2.ckpt"

    # --- language ---
    # Tagalog/Taglish and English are EQUAL priority (owner decision): the
    # default ASR language stays "tl", but the English pipeline (plain
    # whisper + word median, Qwen upgrade for auto-transcripts) is a
    # first-class path, not a fallback. If a song is mostly another
    # language, asr_conf_fallback below auto-detects it.
    language: str = "tl"
    # --- genre (2x2 product matrix: language x genre) ---
    # MANUAL SELECTION ONLY (owner decision): the editor always sends
    # "hiphop" or "ballad" per song — there is no auto/router path anymore.
    # "ballad" = ballad/pop cell -> mms_1b engine with the slide guard off.
    # "hiphop" (and any other value, including "") = MMS-FA path.
    # KVC_GENRE selects.
    genre: str = field(
        default_factory=lambda: os.environ.get("KVC_GENRE", "hiphop"))
    # When the transcript's mean word confidence lands below this, the ASR
    # language choice is hurting: retry with Whisper auto-detected language
    # (handles code-switched Taglish songs where "tl" mangles English
    # sections and vice versa).
    asr_conf_fallback: float = 0.55

    # --- stable-ts word duration (rap vs ballad) ---
    align_max_word_dur: float = 4.0
    align_word_dur_factor: float = 2.5
    align_nonspeech_skip: float = 3.0
    # --- Exp9: timestamp placement inside probability window ---
    # suppress_silence=False keeps candidates that fall on the silent side of a
    # boundary (issue #48); use_word_position=False stops preferring end-of-first-
    # word; nonspeech_error 0.5 lets timestamps sit closer to true vocal attacks.
    align_suppress_silence: bool = True
    align_use_word_position: bool = True
    align_nonspeech_error: float = 0.3

    # --- per-song bias auto-correction (Exp7) ---
    # Rap pickup syllables get timestamped late by Whisper (first confident word,
    # not the true sung onset). After alignment, compute the median offset between
    # each line start and the nearest vocal onset; if a systematic bias exists,
    # shift all lines by it. Uses only local audio, no ground truth needed.
    autocorrect_bias: bool = True
    # Only apply when |median bias| exceeds this (avoid shifting well-aligned songs)
    autocorrect_bias_min_s: float = 0.25

    # --- paths (overridable; defaults target the Modal container) ---
    work_dir: Path = field(default_factory=lambda: Path(os.environ.get("KVC_WORK", "/content/karaokegen_work")))

    # --- model cache (Modal Volume mount) ---
    model_cache_dir: Path = field(default_factory=lambda: Path(os.environ.get("KVC_MODELS", "/vol/models")))

    @property
    def center_y(self) -> int:
        return self.video_h // 2

    @property
    def bs(self) -> str:
        # backslash for ASS escaping
        return chr(92)


settings = Settings()
