"""KaraokeGen Modal deployment.

Simple flow: one file (audio or video - video has audio extracted locally) + optional lyrics.

  POST /draft           -> spawn GPU job (separate + ASR + align)
  POST /render          -> spawn job (apply hints + ASS + ffmpeg burn)
  POST /cancel/{call_id}-> cancel a queued/running job
  GET  /jobs/{id}       -> poll status/result
  GET  /progress/{id}   -> poll progress stage/pct
  GET  /render-progress/{id} -> poll render progress
  GET  /draft-result/{id}-> fetch a persisted draft result (refresh-resume)
  GET  /files/{id}      -> stream finished MP4 from the Volume

Models are cached on a Volume so cold starts never re-download weights.
Stems are cached on a Volume keyed by audio content hash.

Deploy: modal deploy modal_app.py
Cache models: modal run modal_app.py::download_models
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

import modal

from KaraokeGen.models import (
    AlignmentResult,
    DraftRequest,
    MAX_RENDER_JSON_LEN,
    RenderRequest,
    RenderResult,
)

log = logging.getLogger(__name__)

WORKSPACE_DIR = Path(__file__).parent
MODEL_VOL_PATH = "/vol/models"
FILES_VOL_PATH = "/vol/files"

# Volume hygiene: job dirs (input audio, stems, draft results) older than this
# are deleted by cleanup_volume. Rendered MP4s are already deleted 5 min after
# download; this catches ones never downloaded.
JOB_TTL_S = 7 * 24 * 3600          # 7 days
RENDERED_MP4_TTL_S = 2 * 3600      # 2 hours

_HEX_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def _runtime_env():
    """Point HF caches at the model Volume. Done at runtime, not build time:
    setting these to a Volume mount path during the image build makes uv
    create cache dirs there, so Modal refuses to mount the Volume."""
    import os
    os.environ.setdefault("HF_HOME", MODEL_VOL_PATH + "/hf")
    os.environ.setdefault("XDG_CACHE_HOME", MODEL_VOL_PATH + "/cache")


# --- image: bake the KaraokeGen package + GPU deps ---
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "fonts-noto-cjk")
    .run_commands(
        "pip install torch==2.4.1 torchvision==0.19.1 torchaudio==2.4.1 --index-url https://download.pytorch.org/whl/cu121"
    )
    .pip_install(
        # Pinned: newer releases changed multi-stem output handling
        # (process_all_stems gate in mdxc_separator.separate) which silently
        # drops the instrumental stem of the 3-stem karaoke model.
        "audio-separator[gpu]==0.44.5",
        "stable-ts[fw]>=2.19,<3",
        "faster-whisper",
        # Word-level CTC ensemble (KaraokeGen/word_ctc.py):
        # wav2vec2-base-960h + Khalsuu Filipino XLS-R forced alignment.
        # ==4.49: the CVE-2025-32434 torch.load block (needs torch>=2.6 for
        # .bin checkpoints; Khalsuu has no safetensors) landed in 4.52.x/4.51.3
        # patch releases — this image pins torch 2.4.1 for audio-separator.
        "transformers==4.49.0",
        # Pitch-onset snap: bundled CREPE weights (no download at runtime).
        "torchcrepe",
        "librosa",
        "pyyaml",
        # 0.44.5 needs numpy>=2; LyricsAlignment-Multilingual was patched
        # (np.Inf -> np.inf) so the whole stack is numpy-2 clean now.
        # <2.5: librosa -> numba requires numpy<=2.4; on 2.5 every librosa
        # call raises at import and our onset helpers silently no-op.
        "numpy>=2,<2.5",
        "pydantic<2",
        "sortedcontainers",
        "pandas",
        "beartype",
        "soundfile",
    )
    .env({
        "PYTHONPATH": "/root",
        "KVC_MODELS": MODEL_VOL_PATH,
        "KVC_LA_REPO": "/root/LyricsAlignment-Multilingual",
        # LA-Multilingual's eval.py opens CSVs with the default locale codec;
        # containers are C/POSIX -> ascii, which crashes on tagalog_g2p IPA
        # (ŋ) and silently kills the whole ensemble arbitration for Tagalog.
        "PYTHONUTF8": "1",
    })
    .add_local_dir((WORKSPACE_DIR / "KaraokeGen").as_posix(), "/root/KaraokeGen")
    .add_local_dir((WORKSPACE_DIR / "LyricsAlignment-Multilingual").as_posix(),
                   "/root/LyricsAlignment-Multilingual")
)

# Lightweight image for the web API proxy (no GPU deps — just fastapi + pydantic)
web_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("fastapi[standard]", "pydantic>=2")
    .env({"PYTHONPATH": "/root"})
    .add_local_dir((WORKSPACE_DIR / "KaraokeGen").as_posix(), "/root/KaraokeGen")
)

model_vol = modal.Volume.from_name("karaokengen-models", create_if_missing=True)
files_vol = modal.Volume.from_name("karaokengen-files", create_if_missing=True)

# Qwen3-ASR English auto-transcript engine. Isolated image — Qwen needs
# current transformers, the production image pins 4.49.0 (Khalsuu .bin CVE
# workaround) + torch 2.4.1 (audio-separator). Modal runs each function on
# its own image, so this coexists with the pinned stack.
# Qwen transcribes English singing better than whisper, but loses to it on
# Tagalog — so English no-lyrics drafts use Qwen while Tagalog/Taglish
# stays on whisper. The transcript is a user-editable draft fed into the
# normal language-routed alignment pipeline (never used for timings).
qwen_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("torch==2.7.1", index_url="https://download.pytorch.org/whl/cu126")
    .pip_install("transformers>=4.57", "accelerate", "numpy", "soundfile",
                 "librosa")
    # modal_app.py (this function's defining module) imports
    # KaraokeGen.models at module level -> pydantic v1-style + the package
    # must be importable in this image too.
    .pip_install("pydantic<2")
    .add_local_dir((WORKSPACE_DIR / "KaraokeGen").as_posix(), "/root/KaraokeGen")
)

app = modal.App("karaokengen", image=image, volumes={
    MODEL_VOL_PATH: model_vol,
    FILES_VOL_PATH: files_vol,
})


# ---------------------------------------------------------------------------
# one-time model pre-download (run once: modal run modal_app.py::download_models)
# ---------------------------------------------------------------------------
@app.function(gpu="T4", timeout=1800)
def download_models():
    """Pre-download separation + whisper weights onto the Volume so cold
    starts never hit the network for weights."""
    _runtime_env()
    from KaraokeGen.align import _new_separator, load_whisper
    from KaraokeGen.config import Settings

    s = Settings()
    work = Path("/tmp/dl")
    work.mkdir(exist_ok=True)

    # Download custom 3-stem model from HuggingFace (not in official catalog)
    _download_custom_model(s)

    # Patch audio-separator's models.json to include our custom 3-stem model
    _patch_custom_model()

    print("downloading separation model: %s" % s.vocal_model)
    sep = _new_separator(work, s)
    try:
        sep.load_model(model_filename=s.vocal_model)
    finally:
        del sep
    print("  cached: %s" % s.vocal_model)
    model_vol.commit()

    # karaoke_bed_model (gabox_v2) skipped — with_second_voice=False in run_draft
    # so this 600MB download is dead weight. Re-enable if 2nd voice returns:
    # print("downloading karaoke bed model: %s" % s.karaoke_bed_model)
    # sep = _new_separator(work, s)
    # try:
    #     sep.load_model(model_filename=s.karaoke_bed_model)
    # finally:
    #     del sep
    # print("  cached: %s" % s.karaoke_bed_model)
    # model_vol.commit()

    print("downloading whisper large-v3...")
    load_whisper(s)
    model_vol.commit()

    print("downloading word-CTC models (word ensemble)...")
    from KaraokeGen.config import settings as _settings
    from KaraokeGen.word_ctc import _load_model
    for m in _settings.word_ctc_models:
        _load_model(m)
        print("  cached: %s" % m)
    model_vol.commit()

    print("Models cached on volume.")


def _download_custom_model(s: "Settings"):
    """Download bs_karaoke_3stem_giantailab from HuggingFace to the Volume."""
    import urllib.request
    model_dir = Path(s.model_cache_dir)
    model_dir.mkdir(parents=True, exist_ok=True)

    # Download the model checkpoint from HuggingFace
    model_url = "https://huggingface.co/noblebarkrr/mvsepless_resources/resolve/main/bs_roformer/bs_karaoke_3stem_giantailab.ckpt"
    model_dest = model_dir / f"{s.vocal_model}"
    if not model_dest.exists() or model_dest.stat().st_size < 1000:
        print(f"  downloading {s.vocal_model}...")
        urllib.request.urlretrieve(model_url, str(model_dest))
        print(f"    saved: {model_dest} ({model_dest.stat().st_size / 1024 / 1024:.1f} MB)")
    else:
        print(f"  cached: {s.vocal_model}")

    # Write the clean config (original has !!python/tuple tags that break audio-separator)
    # Filename MUST contain "roformer" so audio-separator's detection loads it as a
    # Roformer model (not TFC_TDF/MDXC)
    config_dest = model_dir / "bs_roformer_karaoke_3stem_giantailab_config.yaml"
    config_dest.write_text(_CLEAN_CONFIG)
    print(f"  wrote clean config: {config_dest.name}")


_CLEAN_CONFIG = """conditional: true
audio:
  chunk_size: 352800
  dim_f: 1024
  dim_t: 801
  hop_length: 441
  n_fft: 2048
  num_channels: 2
  sample_rate: 44100
  min_mean_abs: 0.000

model:
  dim: 512
  depth: 12
  stereo: true
  num_stems: 3
  time_transformer_depth: 1
  freq_transformer_depth: 1
  linear_transformer_depth: 0
  freqs_per_bands:
    - 2
    - 2
    - 2
    - 2
    - 2
    - 2
    - 2
    - 2
    - 2
    - 2
    - 2
    - 2
    - 2
    - 2
    - 2
    - 2
    - 2
    - 2
    - 2
    - 2
    - 2
    - 2
    - 2
    - 2
    - 4
    - 4
    - 4
    - 4
    - 4
    - 4
    - 4
    - 4
    - 4
    - 4
    - 4
    - 4
    - 12
    - 12
    - 12
    - 12
    - 12
    - 12
    - 12
    - 12
    - 24
    - 24
    - 24
    - 24
    - 24
    - 24
    - 24
    - 24
    - 48
    - 48
    - 48
    - 48
    - 48
    - 48
    - 48
    - 48
    - 128
    - 129
  dim_head: 64
  heads: 8
  attn_dropout: 0.1
  ff_dropout: 0.1
  flash_attn: true
  dim_freqs_in: 1025
  stft_n_fft: 2048
  stft_hop_length: 441
  stft_win_length: 2048
  stft_normalized: false
  mask_estimator_depth: 2
  multi_stft_resolution_loss_weight: 1.0
  multi_stft_resolutions_window_sizes:
    - 4096
    - 2048
    - 1024
    - 512
    - 256
  multi_stft_hop_size: 147
  multi_stft_normalized: false

training:
  batch_size: 1
  gradient_accumulation_steps: 1
  grad_clip: 0
  instruments:
    - vocals
    - backing_vocal
    - instrumental
  gan_model:
    - music
    - music
    - none
  mix_instruments:
    - vocals
    - backing_vocal
    - instrumental
  diffusion_model:
    - dit
    - none
    - none
  lr: 1.0e-05
  patience: 2
  reduce_factor: 0.95
  target_instrument: null
  num_epochs: 1000
  num_steps: 1000
  q: 0.95
  coarse_loss_clip: true
  ema_momentum: 0.999
  optimizer: adam
  other_fix: true
  use_amp: true

augmentations:
  enable: true
  loudness: true
  loudness_min: 0.5
  loudness_max: 1.5
  mixup: false
  mixup_probs:
    - 0.2
    - 0.02
  mixup_loudness_min: 0.5
  mixup_loudness_max: 1.5
  mp3_compression_on_mixture: 0.01
  mp3_compression_on_mixture_bitrate_min: 32
  mp3_compression_on_mixture_bitrate_max: 320
  mp3_compression_on_mixture_backend: "lameenc"
  all:
    channel_shuffle: 0.5
    random_inverse: 0.1
    random_polarity: 0.5

inference:
  batch_size: 4
  dim_t: 801
  num_overlap: 2

loss_multistft:
  fft_sizes:
    - 1024
    - 2048
    - 4096
  hop_sizes:
    - 147
    - 256
    - 512
  win_lengths:
    - 1024
    - 2048
    - 4096
  window: "hann_window"
  scale: "mel"
  n_bins: 128
  sample_rate: 44100
  perceptual_weighting: true
  w_sc: 1.0
  w_log_mag: 1.0
  w_lin_mag: 0.0
  w_phs: 0.0
  mag_distance: "L1"
"""


def _patch_custom_model():
    """Add bs_karaoke_3stem_giantailab to audio-separator's models.json so the
    Separator class recognizes it. This model is not in the official catalog."""
    import json
    import audio_separator

    # Newer audio-separator beartype-wraps roformer constructors and demands
    # tuple[int, ...] for freqs_per_bands. YAML has no native tuple, and the
    # original config's !!python/tuple tags break safe_load — so make
    # SafeLoader construct ALL sequences as tuples in-process. models.json is
    # read via json (unaffected); no other yaml consumers run during separation.
    import yaml
    _orig_safe_load = yaml.safe_load
    def _model_section_tuples(x):
        # The original upstream config marks model-section sequences as
        # !!python/tuple (beartype enforces tuple hints on every BSRoformer
        # kwarg). Replicate: inside the "model" section, ALL lists become
        # tuples, recursively. Other sections stay lists (stem naming etc).
        def conv(v):
            if isinstance(v, list):
                return tuple(conv(i) for i in v)
            if isinstance(v, dict):
                return {k2: conv(v2) for k2, v2 in v.items()}
            return v
        if isinstance(x, dict) and isinstance(x.get("model"), dict):
            x = {**x, "model": conv(x["model"])}
        return x
    def _safe_load_tuples(stream, *a, **kw):
        return _model_section_tuples(_orig_safe_load(stream, *a, **kw))
    yaml.safe_load = _safe_load_tuples
    _orig_yaml_load = yaml.load
    def _yaml_load_tuples(stream, *a, **kw):
        kw.setdefault("Loader", yaml.FullLoader)
        return _model_section_tuples(_orig_yaml_load(stream, *a, **kw))
    yaml.load = _yaml_load_tuples

    pkg_dir = Path(audio_separator.__file__).parent
    models_json = pkg_dir / "models.json"
    if not models_json.exists():
        print("  WARNING: models.json not found at %s" % models_json)
        return

    with open(models_json, "r") as f:
        data = json.load(f)

    # Add to roformer_download_list if not already present
    roformer = data.setdefault("roformer_download_list", {})
    key = "Roformer Model: BS Karaoke 3-Stem by GiantaiLab"
    if key not in roformer:
        roformer[key] = {
            "bs_karaoke_3stem_giantailab.ckpt": "bs_roformer_karaoke_3stem_giantailab_config.yaml"
        }
        with open(models_json, "w") as f:
            json.dump(data, f, indent=2)
        print("  patched models.json: added bs_karaoke_3stem_giantailab")
    else:
        print("  models.json already has bs_karaoke_3stem_giantailab")


# ---------------------------------------------------------------------------
# volume cleanup: sweep stale job dirs + rendered MP4s off the files Volume
# ---------------------------------------------------------------------------
@app.function(schedule=modal.Cron("0 4 * * *"), timeout=600, cpu=0.5, memory=512)
def cleanup_volume():
    """Delete job dirs older than JOB_TTL_S (stems/input/draft results) and
    rendered MP4s older than RENDERED_MP4_TTL_S from the files Volume.

    Runs daily at 04:00 UTC. Safe to run at any time — rendered files are
    only ever served via /files/{id}, and job dirs are regenerated on demand
    (stems are content-hashed, so re-submitting the same audio re-creates
    them; draft results are just a server-side copy of what the browser
    already has)."""
    files_vol.reload()
    now = time.time()
    removed = 0
    freed = 0

    jobs = Path(FILES_VOL_PATH) / "jobs"
    if jobs.is_dir():
        for d in jobs.iterdir():
            if not d.is_dir():
                continue
            try:
                if now - d.stat().st_mtime > JOB_TTL_S:
                    freed += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
            except OSError:
                pass

    for p in Path(FILES_VOL_PATH).glob("*.mp4"):
        try:
            if now - p.stat().st_mtime > RENDERED_MP4_TTL_S:
                freed += p.stat().st_size
                p.unlink(missing_ok=True)
                removed += 1
        except OSError:
            pass

    files_vol.commit()
    print("cleanup_volume: removed %d stale entries (~%.1f MB)" % (
        removed, freed / 1024 / 1024))


@app.function(gpu=None, timeout=600, cpu=0.5, memory=512)
def reset_job_cache():
    """One-off: delete ALL job dirs from the files Volume. Use after model
    lineup changes so old stems without new outputs get regenerated."""
    _runtime_env()
    files_vol.reload()
    jobs = Path(FILES_VOL_PATH) / "jobs"
    removed = 0
    if jobs.is_dir():
        for d in jobs.iterdir():
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
    files_vol.commit()
    print("reset_job_cache: deleted %d job dirs" % removed)


# ---------------------------------------------------------------------------
# stem reuse: content-addressed work dir on the files Volume
# ---------------------------------------------------------------------------
def _audio_hash(audio_bytes: bytes) -> str:
    return hashlib.sha256(audio_bytes).hexdigest()[:12]


def _input_already_present(work: Path, audio_bytes: bytes, job_id: str) -> bool:
    """True when work/input_audio.wav already holds exactly these bytes.

    run_draft/run_video_separate must NOT rewrite the input in that case:
    rewriting bumps mtime, which breaks stems_reusable()'s staleness check
    (cached stems must be newer than the input) and forces a full
    re-separation on phase 2 of the transcribe-first flow. Keeping the file
    preserves its mtime so the cache check can pass. Never raises: on any
    doubt return False (rewrite is always safe, just slower).
    """
    try:
        inp = work / "input_audio.wav"
        if not inp.exists():
            return False
        if inp.stat().st_size != len(audio_bytes):
            return False
        # Size alone is not identity — hash-verify (cheap vs a separation
        # run). job_id IS the sha256[:12] of audio_bytes, so comparing the
        # on-disk hash against it proves byte equality.
        return hashlib.sha256(inp.read_bytes()).hexdigest()[:12] == job_id
    except Exception as exc:
        log.debug("input reuse check failed (%s); rewriting.", exc)
        return False


# Volume commits are throttled: progress.json updates are cosmetic and the web
# API's existence cache already tolerates multi-second staleness, so committing
# on every write (~every 1.5s during long stages) wasted volume I/O. Real
# checkpoints (Progress.set / Progress.done) force an immediate commit.
_VOLUME_COMMIT_INTERVAL_S = 10.0
_volume_commit_lock = threading.Lock()
_last_volume_commit = 0.0


def _commit_volume(force: bool = False) -> None:
    """Commit the files Volume at most every _VOLUME_COMMIT_INTERVAL_S seconds
    unless force=True. On failure the next call retries."""
    global _last_volume_commit
    with _volume_commit_lock:
        now = time.time()
        if not force and now - _last_volume_commit < _VOLUME_COMMIT_INTERVAL_S:
            return
        try:
            files_vol.commit()
        except Exception as exc:
            log.debug("volume commit failed: %s", exc)
            return
        _last_volume_commit = now


def _write_progress(work, stage, pct, detail="", filename="progress.json",
                    force_commit=False):
    try:
        (work / filename).write_text(json.dumps({
            "stage": stage, "pct": pct, "detail": detail,
        }))
    except Exception as exc:
        log.debug("progress write failed: %s", exc)
        return
    _commit_volume(force=force_commit)


class Progress:
    """Throttled progress writer + estimator thread for black-box stages.

    Real checkpoints go through `set()` (cancels any running estimator, writes
    immediately). For stages with no callback (transcribe/align) call
    `estimate()` — it spawns a background thread that smoothly advances the
    pct toward a target so the bar never sits frozen. The next real `set()`
    cancels the estimator, so real updates always win.
    """

    def __init__(self, work, throttle_s=1.5, filename="progress.json"):
        self.work = work
        self.filename = filename
        self.throttle_s = throttle_s
        self.stage = "starting"
        self.pct = 0
        self.detail = ""
        self._last_write = 0.0
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    def _write(self, stage, pct, detail):
        now = time.time()
        with self._lock:
            self.stage, self.pct, self.detail = stage, pct, detail
        if now - self._last_write < self.throttle_s:
            return
        self._last_write = now
        _write_progress(self.work, stage, pct, detail, self.filename)

    def set(self, stage, pct, detail=""):
        self._stop_estimator()
        self._write(stage, pct, detail)
        # Real checkpoint: sync to the cloud now so the timeline lights up.
        _write_progress(self.work, stage, pct, detail, self.filename, force_commit=True)

    def set_detail(self, detail):
        """Update only the detail text (e.g. per-model status) without moving
        the bar or cancelling a running estimator. Writes immediately."""
        with self._lock:
            self.detail = detail
        _write_progress(self.work, self.stage, self.pct, detail, self.filename)

    def _stop_estimator(self, join: bool = True):
        """Stop the running estimator thread, if any.

        The stop event is NEVER cleared here: clearing it lets a thread that
        wakes from wait() re-enter its loop and keep overwriting real progress
        writes with stale estimator pct values (the "stuck bar" bug). Each
        new estimate() gets a fresh event instead."""
        if self._thread is not None:
            self._stop.set()
            thread, self._thread = self._thread, None
            if join:
                thread.join(timeout=2.0)

    def estimate(self, stage, target_pct, duration_s, detail=""):
        """Smoothly advance pct toward target_pct over `duration_s` seconds."""
        self._stop_estimator()
        self._stop = threading.Event()  # fresh event for this estimator
        start_pct = self.pct
        self._write(stage, start_pct, detail)

        def run():
            t0 = time.time()
            while not self._stop.is_set():
                elapsed = time.time() - t0
                frac = min(1.0, elapsed / max(0.01, duration_s))
                eased = frac * frac * (3 - 2 * frac)  # smoothstep
                cur = start_pct + (target_pct - start_pct) * eased
                self._write(stage, cur, detail)
                if frac >= 1.0:
                    break
                self._stop.wait(0.5)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def done(self, detail="Done!"):
        self._stop_estimator()
        self._write("done", 100, detail)
        _write_progress(self.work, "done", 100, detail, self.filename, force_commit=True)


@app.function(gpu="T4", timeout=600)
def probe_deps() -> str:
    out = []
    import subprocess, sys
    for mod in ("beartype", "sortedcontainers", "pandas", "soundfile",
                "audio_separator"):
        r = subprocess.run([sys.executable, "-c", f"import {mod}"],
                           capture_output=True, text=True)
        out.append(f"{mod}: {'OK' if r.returncode == 0 else r.stderr.strip()[-100:]}")
    import numpy
    out.append(f"numpy={numpy.__version__}")
    return "\n".join(out)

@app.local_entrypoint()
def probe_main():
    print(probe_deps.remote())


@app.function(image=qwen_image, gpu="T4", timeout=900,
              volumes={MODEL_VOL_PATH: model_vol, FILES_VOL_PATH: files_vol})
def run_transcribe_qwen(vocals_remote_path: str) -> str:
    """Qwen3-ASR-1.7B English transcript of a separated vocals stem.

    Called from run_draft's auto-transcribe flow when the song is English
    (user-selected "en", or whisper's transcript routes there via tag_ratio).
    Runs on the isolated qwen_image (see its comment for the pin rationale).
    Returns plain transcript text — a user-editable lyric draft, never used
    for timings."""
    import logging
    logging.basicConfig(level=logging.INFO)
    import os
    os.environ.setdefault("HF_HOME", MODEL_VOL_PATH + "/hf")

    import torch
    from transformers import AutoProcessor, Qwen3ASRForConditionalGeneration

    files_vol.reload()
    if not Path(vocals_remote_path).exists():
        raise FileNotFoundError(f"vocals stem not found: {vocals_remote_path}")

    model = Qwen3ASRForConditionalGeneration.from_pretrained(
        "Qwen/Qwen3-ASR-1.7B-hf", dtype=torch.float16, device_map="cuda:0")
    proc = AutoProcessor.from_pretrained("Qwen/Qwen3-ASR-1.7B-hf")
    inputs = proc.apply_transcription_request(
        audio=str(vocals_remote_path), language="English")
    inputs = inputs.to(model.device, model.dtype)
    with torch.inference_mode():
        output_ids = model.generate(**inputs, max_new_tokens=2048)
    generated = output_ids[:, inputs["input_ids"].shape[1]:]
    parsed = proc.decode(generated, return_format="parsed")[0]
    text = str(parsed.get("transcription", "")).strip()
    log.info("qwen transcript: %d chars (language=%s)",
             len(text), parsed.get("language"))
    return text


@app.function(gpu="T4", timeout=3600, retries=1, cpu=2, memory=4096)
def run_draft(audio_b64: str, language: str, lyrics: str,
              genre: str = "hiphop", transcribe_only: bool = False,
              start_s: float = 0.0) -> dict:
    """Heavy GPU job: separate (vocals + instrumental) -> Whisper ->
    stable-ts alignment -> onset-snap. Modal only ever sees audio (video
    files have their audio extracted locally). Stems are cached by audio
    content hash so re-submitting the same audio with edited lyrics skips
    separation. Returns DraftResult as dict.

    transcribe_only=True (no-lyrics flow, phase 1): stop right after the
    transcript so the user can fix the text BEFORE any alignment runs.
    Returns {"job_id", "transcript", "duration"} — no alignment, no MP3.
    The follow-up /draft with edited lyrics reuses the cached stems.

    start_s (lead-in skip): live takes open with talk that is not lyrics.
    The intro is cut from the copies the aligner/ASR hear and all returned
    timings are shifted back onto the full-song clock, so a lyric line can
    never anchor onto the intro — while the rendered video still contains
    it (the render bed uses the untouched instrumental)."""
    _runtime_env()
    # The models.json catalog patch applied by download_models lives only in
    # that one-off container — job containers boot from the unpatched image,
    # so load_model rejects the custom 3-stem model before checking the
    # Volume. Patch at every GPU entry point (idempotent, ~ms).
    _patch_custom_model()
    from KaraokeGen.align import (
        align_lyrics, dual_separate, load_whisper,
        result_to_lyrics, shift_result, transcribe_best, stems_reusable,
        trim_lead_in,
    )
    from KaraokeGen.audio import audio_duration, to_mp3_mono
    from KaraokeGen.config import Settings
    from KaraokeGen.lyrics import strip_hints, verify_alignment
    from KaraokeGen.models import DraftResult
    from KaraokeGen.render import from_stable_ts

    audio_bytes = base64.b64decode(audio_b64)
    job_id = _audio_hash(audio_bytes)

    work = Path(FILES_VOL_PATH) / "jobs" / job_id
    work.mkdir(parents=True, exist_ok=True)
    files_vol.reload()
    s = Settings(language=language, genre=genre, work_dir=work)

    prog = Progress(work)
    prog.set("starting", 5, "GPU container ready, writing audio...")

    inp = work / "input_audio.wav"
    if _input_already_present(work, audio_bytes, job_id):
        log.info("Input unchanged, keeping input_audio.wav (audio hash: %s).", job_id)
    else:
        inp.write_bytes(audio_bytes)

    result = None
    out_lyrics = ""
    vocals_path = None
    dur = 0.0

    dur = audio_duration(inp) or 0.0

    if stems_reusable(work, s, with_second_voice=False):
        vocals_path = str(work / "vocals_asr.wav")
        log.info("Reusing stems from a previous run (audio hash: %s).", job_id)
        prog.set("separating", 30, "Reusing cached stems (same audio detected).")
    else:
        est_s = min(300, max(30, int(dur * 5))) if dur else 90
        prog.estimate("separating", 30, est_s,
                      "Separating vocals + instrumental (3-stem model)...")
        vocals_path, _ = dual_separate(str(inp), work, s, cb=prog, with_second_voice=False)
        vocals_path = str(vocals_path)
        files_vol.commit()
        prog.set("separating", 35, "Stem separation complete.")

    # Lead-in skip: copy every audio input the aligner consumes with the
    # first `lead_in` seconds removed (see align.trim_lead_in). Nothing
    # downstream can then lock onto the spoken intro, and the timings are
    # shifted back below. A start_s that would swallow the whole song is
    # ignored rather than producing a silent job.
    lead_in = max(0.0, min(float(start_s or 0.0), 3600.0))
    if lead_in and dur and lead_in > max(5.0, dur - 10.0):
        log.warning("start_s=%.1fs leaves nothing to align (duration %.1fs) — ignoring.",
                    lead_in, dur)
        lead_in = 0.0
    trim_paths = {}
    if lead_in:
        prog.set("separating", 38,
                 "Skipping the first %d:%02d (intro) before timing..."
                 % divmod(int(lead_in), 60))
        trim_paths = trim_lead_in(work, lead_in, s)
        log.info("Lead-in skip: %s onward (%.2fs cut from the alignment stems).",
                 trim_paths["vocals_asr"], lead_in)
    asr_vocals = trim_paths.get("vocals_asr", vocals_path)
    lead_vocals = trim_paths.get("vocals_lead", str(work / "vocals_lead.wav"))
    raw_vocals = trim_paths.get("vocals_raw", str(work / "vocals_raw.wav"))
    mix_audio = trim_paths.get("mix", str(inp))

    # The aligner itself runs on the Whisper weights, so this load happens
    # in BOTH flows — but only the no-lyrics flow transcribes. Word the
    # progress honestly so an align-only run never claims to transcribe.
    _has_lyrics = bool(lyrics.strip())
    prog.set("loading_model", 40,
             "Loading alignment model..."
             if _has_lyrics else "Loading Whisper large-v3 model...")
    prog.estimate("loading_model", 47, 60,
                  "Loading alignment model (~3 GB)..."
                  if _has_lyrics else "Loading Whisper large-v3 model (~3 GB)...")
    model = load_whisper(s)
    prog.set("loading_model", 50,
             "Alignment model loaded."
             if _has_lyrics else "Whisper model loaded.")

    # transcribe + align is a black-box blocking call with no callback, so run
    # an estimator that smoothly advances the bar over an estimated duration.
    # The next real prog.set() cancels it, so real updates always win.
    # Whisper large-v3 on T4 ~20x realtime; alignment adds some. ~0.2x total.
    est_s = min(240, max(30, int(dur * 0.2))) if dur else 60
    if not lyrics.strip():
        # Auto-transcribe: English songs use Qwen3-ASR (better than
        # whisper on English singing); Tagalog/Taglish stays on whisper.
        # The transcript then flows through the NORMAL language-routed
        # alignment below — same machinery as pasted lyrics, not
        # whisper's raw word timestamps.
        prog.set("transcribing", 60, "Auto-transcribing vocals...")
        asr = transcribe_best(model, s, asr_vocals)
        from KaraokeGen.lyrics import strip_transcript_noise
        detected_lyrics = strip_transcript_noise(result_to_lyrics(from_stable_ts(asr)))
        try:
            from KaraokeGen.word_ctc import tag_ratio_text
            forced_en = (language or "").strip().lower().startswith("en")
            if forced_en or tag_ratio_text(detected_lyrics) < 0.01:
                prog.set("transcribing", 68,
                         "English detected — upgrading transcript with Qwen3-ASR...")
                qwen_text = run_transcribe_qwen.remote(asr_vocals)
                if qwen_text.strip():
                    from KaraokeGen.lyrics import transcript_to_lines
                    lyrics = transcript_to_lines(strip_transcript_noise(qwen_text))
                    log.info("auto-transcribe: Qwen transcript (%d chars, "
                             "%d lines).", len(qwen_text),
                             len(lyrics.splitlines()))
                else:
                    lyrics = detected_lyrics
            else:
                lyrics = detected_lyrics
        except Exception as exc:
            log.warning("Qwen transcript upgrade failed (%s); using whisper "
                        "transcript.", exc)
            lyrics = detected_lyrics
        out_lyrics = lyrics

    if transcribe_only:
        # Phase 1 of the no-lyrics flow: hand the transcript back for editing
        # BEFORE alignment. Stems are already committed above, so phase 2
        # (normal /draft with edited lyrics) skips separation entirely.
        final_text = out_lyrics if out_lyrics.strip() else lyrics
        if not final_text.strip():
            raise RuntimeError(
                "Transcription produced no text — check the audio has audible vocals.")
        files_vol.commit()
        try:
            (work / "transcript.json").write_text(json.dumps({
                "job_id": job_id, "transcript": final_text, "duration": dur,
            }))
            files_vol.commit()
        except Exception:
            pass
        prog.done("Transcription complete — edit the text, then align.")
        return {"job_id": job_id, "transcript": final_text, "duration": dur}

    if lyrics.strip():
        # Align-only path: NO transcription happens here (the timeline must
        # not light up "Transcribing" — that stage is for the no-lyrics flow).
        prog.estimate("aligning", 75, est_s,
                      "Aligning your lyrics (dual-aligner ensemble + content verify)...")
        from KaraokeGen.align import align_lyrics as _align_legacy
        from KaraokeGen.config import Settings as _S
        from KaraokeGen.ensemble import align_lyrics_ensemble
        ens_ok = s.align_ensemble and len([l for l in lyrics.splitlines() if l.strip()]) >= 2
        if ens_ok:
            try:
                result = align_lyrics_ensemble(
                    model, asr_vocals, lead_vocals, mix_audio, lyrics, s,
                    raw_vocals_path=raw_vocals)
            except Exception as exc:
                log.warning("Ensemble failed (%s); falling back to legacy align.", exc)
                result = _align_legacy(model, asr_vocals, lyrics, s)
        else:
            result = _align_legacy(model, asr_vocals, lyrics, s)
        if lead_in:
            # Alignment ran on stems that start `lead_in` seconds in; put the
            # timings back on the full-song clock the editor/render use.
            result = shift_result(result, lead_in)
        prog.set("aligning", 80, "Line-level alignment complete.")

    if result is None:
        raise RuntimeError(
            "Alignment produced no result — check the audio has audible vocals.")
    # Align-only runs have no `out_lyrics` (the user's text came in with the
    # request), so verify against the lyrics that were actually aligned.
    report = verify_alignment(result, strip_hints(out_lyrics or lyrics), s)

    prog.set("preparing", 92, "Encoding audio for browser playback...")
    mp3_path = work / "vocals.mp3"
    to_mp3_mono(vocals_path, mp3_path, "64k")
    vocals_b64 = base64.b64encode(mp3_path.read_bytes()).decode("ascii")

    files_vol.commit()

    result_dict = DraftResult(
        job_id=job_id,
        alignment=result,
        lyrics=out_lyrics,
        report=report,
        vocals_audio_b64=vocals_b64,
        duration=dur,
    ).model_dump()

    # persist for resumability (browser refresh can re-fetch this)
    try:
        (work / "draft_result.json").write_text(json.dumps(result_dict))
        files_vol.commit()
    except Exception:
        pass

    prog.done("Draft complete!")
    return result_dict


@app.function(gpu="T4", timeout=3600, retries=1, cpu=2, memory=4096)
def run_video_separate(audio_b64: str) -> dict:
    """Lyric-video mode: separation only (no ASR, no alignment).

    The local app extracts the audio track from the user's lyric video and
    sends it here. We split vocals/instrumental with the 3-stem karaoke
    model (align_ensemble off — the lead-vocal pass and ASR prep are draft
    work the remux never needs) and persist the stems on the files Volume.
    The local app then remuxes instrumental_std onto the ORIGINAL video
    (burned-in lyrics stay, vocals removed).
    """
    _runtime_env()
    # Same models.json catalog patch as run_draft — see comment there.
    _patch_custom_model()
    from KaraokeGen.align import dual_separate, stems_reusable
    from KaraokeGen.audio import audio_duration
    from KaraokeGen.config import Settings

    audio_bytes = base64.b64decode(audio_b64)
    job_id = _audio_hash(audio_bytes)

    work = Path(FILES_VOL_PATH) / "jobs" / job_id
    work.mkdir(parents=True, exist_ok=True)
    files_vol.reload()
    s = Settings(work_dir=work, align_ensemble=False)

    prog = Progress(work)
    prog.set("starting", 5, "GPU container ready, writing audio...")

    inp = work / "input_audio.wav"
    if _input_already_present(work, audio_bytes, job_id):
        log.info("Input unchanged, keeping input_audio.wav (audio hash: %s).", job_id)
    else:
        inp.write_bytes(audio_bytes)
    dur = audio_duration(inp) or 0.0

    if stems_reusable(work, s, with_second_voice=False):
        log.info("Reusing stems from a previous run (audio hash: %s).", job_id)
        prog.set("separating", 60, "Reusing cached stems (same audio detected).")
    else:
        # separation runs the 3-stem karaoke model only (remux uses the clean
        # instrumental; no 2nd-voice karaoke pass needed in video mode)
        est_s = min(240, max(20, int(dur * 5))) if dur else 90
        prog.estimate("separating", 60, est_s,
                      "Separating vocals + instrumental (3-stem model)...")
        dual_separate(str(inp), work, s, cb=prog, with_second_voice=False)
        files_vol.commit()
        prog.set("separating", 70, "Stem separation complete.")

    dur = audio_duration(work / "instrumental_std.wav") or 0.0

    prog.set("preparing", 90, "Instrumental ready for your video...")
    files_vol.commit()
    prog.done("Done!")
    return {"job_id": job_id, "duration": dur}



@app.function(timeout=600, retries=1, cpu=2, memory=4096)
def run_render(draft_job_id: str, alignment: dict, final_lyrics: str, bg_color: str,
               output_name: str, second_voice: bool = False,
               second_voice_gain: float = 1.0,
               word_level: bool = False, highlight: str = "line") -> dict:
    """Render job (CPU-only): apply hints to the client-sent alignment,
    generate ASS, ffmpeg burn to MP4. The instrumental is read from the
    files Volume (persisted by the draft job). No re-alignment, no GPU.
    When second_voice=True, the bed = instrumental_std + second_voice × gain
    (same stem + gain as the editor preview; gain 1.0 reconstructs the karaoke
    model's own bed exactly).

    highlight/word_level: line-level (single \\kf per line, production) vs
    word-level (strict per-word {\\k}, separate version, same interface).
    Word-level is the word-only experiment track; it shares the same
    AlignmentResult -> ASS interface but runs separately via this flag.
    """
    from KaraokeGen.config import Settings
    from KaraokeGen.lyrics import apply_timing_hints
    from KaraokeGen.render import result_to_ass, result_to_ass_word, render_video
    from KaraokeGen.models import AlignmentResult, RenderResult

    if not _HEX_ID_RE.match(draft_job_id):
        raise ValueError("invalid draft_job_id")

    s = Settings()
    job_files = Path(FILES_VOL_PATH) / "jobs" / draft_job_id
    if not job_files.exists():
        files_vol.reload()
        if not job_files.exists():
            raise FileNotFoundError("draft job files not found: %s" % draft_job_id)

    # Render writes to its own progress file so it never clobbers the draft's
    # progress.json (the browser polls /render-progress during a render).
    prog = Progress(job_files, filename="render_progress.json")
    prog.set("starting", 5, "Render container ready.")

    result = AlignmentResult.model_validate(alignment)
    if final_lyrics.strip():
        result = apply_timing_hints(result, final_lyrics)

    ass_path = job_files / ("subtitle-%s.ass" % uuid.uuid4().hex[:8])
    # Word-level is a separate version (strict per-word), same interface
    use_word = bool(word_level) or (highlight or "line") == "word"
    if use_word:
        ass_text = result_to_ass_word(result, s)
    else:
        ass_text = result_to_ass(result, s)
    ass_path.write_text(ass_text, encoding="utf-8")
    prog.set("rendering", 25, "ASS subtitles generated (%s)." % ("word" if use_word else "line"))

    try:
        second_voice_gain = max(0.0, min(float(second_voice_gain or 0.0), 4.0))
    except (TypeError, ValueError):
        second_voice_gain = 1.0
    safe_name = "".join(c for c in (output_name or "karaoke") if c.isalnum() or c in "._-") or "karaoke"
    mp4_name = "%s.mp4" % safe_name
    out_path = job_files / mp4_name
    inst_std = job_files / "instrumental_std.wav"
    sv = job_files / "second_voice.wav"
    bv = job_files / "backing_vocals.wav"
    if second_voice and bv.exists():
        # bs-karaoke 3-stem path: backing_vocals.wav is the isolated backing
        # stem; gain 1.0 = full backing level.
        from KaraokeGen.audio import mix_bed_with_gain
        prog.set("rendering", 28, "Mixing backing vocals into the bed...")
        instrumental = job_files / "_bed_bv.wav"
        mix_bed_with_gain(inst_std, bv, second_voice_gain,
                          instrumental)
    elif second_voice and sv.exists():
        # legacy editor-preview path (derived second-voice stem)
        from KaraokeGen.audio import mix_bed_with_gain
        prog.set("rendering", 28, "Mixing 2nd voice into the bed...")
        instrumental = job_files / "_bed_2nd.wav"
        mix_bed_with_gain(inst_std, sv, second_voice_gain,
                          instrumental)
    else:
        instrumental = inst_std
    if not instrumental.exists():
        raise FileNotFoundError("instrumental not found for job %s" % draft_job_id)
    prog.set("rendering", 30, "Burning lyrics into video (ffmpeg)...")
    prog.estimate("rendering", 88, 60, "Rendering video (ffmpeg)...")
    render_video(instrumental, ass_path, out_path, bg_color, s)
    prog.set("finalizing", 92, "Finalizing MP4...")
    try:
        ass_path.unlink()
    except OSError:
        pass

    file_id = uuid.uuid4().hex[:12]
    final_path = Path(FILES_VOL_PATH) / (file_id + ".mp4")
    shutil.move(str(out_path), str(final_path))
    files_vol.commit()
    prog.done("Render complete!")

    return RenderResult(
        file_id=file_id,
        filename=mp4_name,
        size_bytes=final_path.stat().st_size,
    ).model_dump()


# ---------------------------------------------------------------------------
# FastAPI job-queue wrapper
# ---------------------------------------------------------------------------
@app.function(image=web_image, volumes={FILES_VOL_PATH: files_vol}, secrets=[modal.Secret.from_dotenv()])
@modal.asgi_app()
@modal.concurrent(max_inputs=20)
def api() -> "FastAPI":
    import asyncio
    import os
    from fastapi import Depends, FastAPI, Header, HTTPException
    from fastapi.responses import StreamingResponse

    # The modal.run URL is public: without auth, anyone who finds it can POST
    # /draft and burn GPU credits. The deploy env must carry MODAL_API_KEY
    # (via modal.Secret.from_dotenv() — same key the local app sends).
    # Fail CLOSED: a deploy without the secret rejects everything with 503
    # instead of serving GPU time to the internet.
    api_key = os.environ.get("MODAL_API_KEY", "")

    def _check_auth(authorization: str = Header(default="")) -> None:
        if not api_key:
            raise HTTPException(
                503, "server misconfigured: MODAL_API_KEY secret is missing")
        if authorization != "Bearer " + api_key:
            raise HTTPException(401, "unauthorized")

    web = FastAPI(title="KaraokeGen", dependencies=[Depends(_check_auth)])

    # Hard caps as a second line of defense (local app already enforces these).
    MAX_AUDIO_B64 = 100 * 1024 * 1024 * 4 // 3  # ~100 MB decoded
    _CALL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_]{3,127}$")
    _LANG_OK = {"tl", "en", ""}
    _GENRE_OK = {"hiphop", "ballad"}

    def _decode_audio(req_audio_b64: str) -> bytes:
        if len(req_audio_b64) > MAX_AUDIO_B64:
            raise HTTPException(413, "audio too large (max 100 MB)")
        try:
            return base64.b64decode(req_audio_b64, validate=True)
        except Exception:
            raise HTTPException(400, "audio_b64 is not valid base64")

    def _check_draft_fields(language: str, genre: str) -> None:
        if (language or "").strip().lower() not in _LANG_OK:
            raise HTTPException(422, "unsupported language (use tl, en, or auto)")
        if (genre or "").strip().lower() not in _GENRE_OK:
            raise HTTPException(422, "unsupported genre (use hiphop or ballad)")

    def _check_call_id(call_id: str) -> None:
        if not _CALL_ID_RE.match(call_id or ""):
            raise HTTPException(400, "invalid call id")

    _volume_cache: dict[str, tuple[float, bool]] = {}
    _VOLUME_CACHE_TTL = 5.0

    def _cached_exists(path: str) -> bool:
        now = time.time()
        cached = _volume_cache.get(path)
        if cached and (now - cached[0]) < _VOLUME_CACHE_TTL:
            return cached[1]
        p = Path(path)
        files_vol.reload()
        result = p.exists()
        _volume_cache[path] = (now, result)
        return result

    def _invalidate_cache(path: str):
        _volume_cache.pop(path, None)

    @web.post("/draft")
    async def draft(req: DraftRequest):
        _check_draft_fields(req.language, req.genre)
        audio_bytes = _decode_audio(req.audio_b64)
        draft_job_id = _audio_hash(audio_bytes)
        call = run_draft.spawn(
            req.audio_b64, req.language, req.lyrics, req.genre, False,
            req.start_s,
        )
        return {"job_id": call.object_id, "draft_job_id": draft_job_id, "status": "queued"}

    @web.post("/video-draft")
    async def video_draft(req: DraftRequest):
        """Lyric-video mode: separation-only job (see run_video_separate).
        The audio is the track the local app extracted from the video."""
        _check_draft_fields(req.language, req.genre)
        audio_bytes = _decode_audio(req.audio_b64)
        draft_job_id = _audio_hash(audio_bytes)
        call = run_video_separate.spawn(req.audio_b64)
        return {"job_id": call.object_id, "draft_job_id": draft_job_id, "status": "queued"}

    @web.post("/transcribe")
    async def transcribe(req: DraftRequest):
        """No-lyrics flow, phase 1: separate + transcribe only, NO alignment.
        Returns {"transcript"} via the job result for user editing; the
        follow-up /draft with edited lyrics reuses cached stems and aligns."""
        _check_draft_fields(req.language, req.genre)
        audio_bytes = _decode_audio(req.audio_b64)
        draft_job_id = _audio_hash(audio_bytes)
        call = run_draft.spawn(
            req.audio_b64, req.language, req.lyrics, req.genre, True,
            req.start_s,
        )
        return {"job_id": call.object_id, "draft_job_id": draft_job_id, "status": "queued"}

    @web.get("/progress/{draft_job_id}")
    async def get_progress(draft_job_id: str):
        if not _HEX_ID_RE.match(draft_job_id):
            raise HTTPException(400, "invalid id")
        p = Path(FILES_VOL_PATH) / "jobs" / draft_job_id / "progress.json"
        if not _cached_exists(str(p)):
            return {"stage": "starting", "pct": 0, "detail": "Waiting for GPU container..."}
        return json.loads(p.read_text())

    @web.get("/render-progress/{draft_job_id}")
    async def get_render_progress(draft_job_id: str):
        if not _HEX_ID_RE.match(draft_job_id):
            raise HTTPException(400, "invalid id")
        p = Path(FILES_VOL_PATH) / "jobs" / draft_job_id / "render_progress.json"
        if not _cached_exists(str(p)):
            return {"stage": "starting", "pct": 0, "detail": "Waiting for render container..."}
        return json.loads(p.read_text())

    @web.get("/draft-result/{draft_job_id}")
    async def get_draft_result(draft_job_id: str):
        if not _HEX_ID_RE.match(draft_job_id):
            raise HTTPException(400, "invalid id")
        p = Path(FILES_VOL_PATH) / "jobs" / draft_job_id / "draft_result.json"
        if not _cached_exists(str(p)):
            raise HTTPException(404, "draft result not found")
        return json.loads(p.read_text())

    @web.post("/cancel/{call_id}")
    async def cancel_job(call_id: str):
        _check_call_id(call_id)
        try:
            fc = modal.FunctionCall.from_id(call_id)
            if hasattr(fc, "cancel"):
                fc.cancel()
                return {"status": "cancelled"}
            return {"status": "unsupported", "error": "cancel not available in this Modal version"}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    @web.post("/render")
    async def render(req: RenderRequest):
        if len(req.model_dump_json()) > MAX_RENDER_JSON_LEN:
            raise HTTPException(413, "render payload too large")
        call = run_render.spawn(
            req.draft_job_id, req.alignment.model_dump(),
            req.final_lyrics, req.bg_color, req.output_name,
            req.second_voice, req.second_voice_gain,
            req.word_level, req.highlight,
        )
        return {"job_id": call.object_id, "status": "queued"}

    @web.post("/render-word")
    async def render_word(req: RenderRequest):
        """Word-level alias: forces word highlight regardless of payload value.
        Separate endpoint for experiments; same interface as /render."""
        if len(req.model_dump_json()) > MAX_RENDER_JSON_LEN:
            raise HTTPException(413, "render payload too large")
        req.word_level = True
        req.highlight = "word"
        call = run_render.spawn(
            req.draft_job_id, req.alignment.model_dump(),
            req.final_lyrics, req.bg_color, req.output_name,
            req.second_voice, req.second_voice_gain,
            True, "word",
        )
        return {"job_id": call.object_id, "status": "queued"}

    @web.get("/jobs/{call_id}")
    async def get_job(call_id: str):
        _check_call_id(call_id)
        function_call = modal.FunctionCall.from_id(call_id)
        try:
            result = await function_call.get.aio(timeout=0)
            return {"status": "done", "result": result}
        except modal.exception.OutputExpiredError:
            raise HTTPException(404, "job expired")
        except TimeoutError:
            return {"status": "pending", "result": None}
        except Exception:
            log.exception("job %s failed", call_id)
            return {"status": "error", "error": "job failed"}

    @web.get("/files/{file_id}")
    async def get_file(file_id: str):
        if not _HEX_ID_RE.match(file_id):
            raise HTTPException(400, "invalid file id")
        p = Path(FILES_VOL_PATH) / (file_id + ".mp4")
        if not _cached_exists(str(p)):
            raise HTTPException(404, "file not found")

        file_age = time.time() - p.stat().st_mtime
        if file_age > 300:
            try:
                p.unlink()
                files_vol.commit()
                _invalidate_cache(str(p))
            except OSError:
                pass
            raise HTTPException(404, "file not found (auto-deleted after 5 minutes)")

        def iterfile():
            try:
                with open(p, "rb") as f:
                    while chunk := f.read(1024 * 1024):
                        yield chunk
            except (FileNotFoundError, OSError):
                raise HTTPException(404, "file expired")

        return StreamingResponse(iterfile(), media_type="video/mp4",
                                  headers={"Content-Disposition": "attachment; filename=%s.mp4" % file_id})

    # --- stem streaming for the waveform editor (draft stems live on the volume) ---
    _STEM_FILES = {
        "vocals-raw": ("vocals_raw.wav", "audio/wav", "vocals_raw.wav"),
        "instrumental": ("instrumental_std.wav", "audio/wav", "instrumental.wav"),
        "second-voice": ("second_voice.wav", "audio/wav", "second_voice.wav"),
        "backing-vocals": ("backing_vocals.wav", "audio/wav", "backing_vocals.wav"),
    }

    def _stem_endpoint(stem_key):
        fname, media, dl = _STEM_FILES[stem_key]

        async def handler(draft_job_id: str):
            if not _HEX_ID_RE.match(draft_job_id):
                raise HTTPException(400, "invalid draft_job_id")
            p = Path(FILES_VOL_PATH) / "jobs" / draft_job_id / fname
            if not _cached_exists(str(p)):
                raise HTTPException(404, "stem not found (run a draft first)")

            async def iterstem():
                try:
                    with open(p, "rb") as f:
                        while chunk := f.read(1024 * 1024):
                            yield chunk
                except (FileNotFoundError, OSError):
                    raise HTTPException(404, "stem expired")

            return StreamingResponse(iterstem(), media_type=media,
                                     headers={"Content-Disposition":
                                              "attachment; filename=%s" % dl})
        return handler

    for _key in _STEM_FILES:
        web.get("/" + _key + "/{draft_job_id}")(_stem_endpoint(_key))

    return web
