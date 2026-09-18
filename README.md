# KaraokeGen

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Windows-0078D6)
![Python](https://img.shields.io/badge/python-3.12-blue)
![GPU](https://img.shields.io/badge/GPU-Modal-black)

Turn any song into a **karaoke MP4** — studio-style per-word highlighting, timed automatically and perfected by hand.

Drop in a song, with lyrics or without. KaraokeGen splits out the vocals, transcribes them when you don't provide lyrics, and pins every word to the moment it's sung. Then drag any word on the waveform until it sits perfectly, hit render, and get a 720p karaoke video. Made for **Tagalog / Taglish and English** songs.

## Quick start

1. Download `KaraokeGen.exe` from the [latest release](https://github.com/lukatdamoves/karaokegen/releases) (or build it yourself with `.\build_exe.ps1` — ffmpeg is bundled, no separate download).
2. Double-click it. On first launch, open [modal.com](https://modal.com) → **Settings → API Tokens → New Token**, paste the `modal token set --token-id ak-... --token-secret as-...` command it shows into the app, and hit **Save token & Deploy backend**. No terminal needed — the GPU backend deploys itself.
3. Pick the song genre → upload audio → paste lyrics (or skip — hit **Transcribe**, fix the text while listening to your original audio, then **Run alignment**) → drag words into place → **Render MP4** → download.

Your settings are saved to `%APPDATA%\KaraokeGen\config.json` on this PC — edits survive updates. To switch account, hit **Disconnect** in the same panel first.

> Have a lyric video instead? Upload the MP4 and KaraokeGen strips the vocals, keeping your original video and on-screen lyrics untouched. No editing needed.
> First launch can take a minute (Windows scans the file); approve the SmartScreen prompt (the exe is unsigned).

## Features

- **No lyrics? No problem.** Upload just the audio and KaraokeGen transcribes the vocals itself — Qwen3-ASR for English, Whisper for Tagalog. The transcript lands in the editor as editable, pre-timed lyrics.
- **Word timing, automatic.** Every word gets pinned to the moment it's sung by GPU alignment on [Modal](https://modal.com) — no local GPU, no manual tapping.
- **An editor that feels like play.** Each word is a draggable region on the waveform: click to hear it, double-click to fix its text, drag the body or edges to seat it exactly. No shortcuts to memorize — everything is point, click, drag.
- **Renders that look pro.** Each word lights up precisely as it's sung, over a background color you choose, in crisp 720p.
- **Backing vocals, your mix.** A dedicated slider sets the second-voice level, and the preview is sample-identical to what's burned into the video — what you hear is what you get.
- **Two languages, tuned separately.** Tagalog/Taglish runs a dual-aligner ensemble; English runs its own whisper pipeline. Pick Hiphop/R&B or Ballad/Pop per song and the right word engine is selected for you.
- **Hints in plain text.** Pin a line with `[mm:ss]` or nudge it with `<<+0.5>>` without leaving the lyrics box.
- **Lyric videos, devocalized.** Upload an MP4 with burned-in lyrics and get the same video back with the voice removed and the instrumental in its place. Zero editing required.
- **Never lose work.** Refresh mid-draft and progress resumes; come back tomorrow and the editor restores itself.
- **Never waste GPU.** Live per-stage progress with timings, plus a stop button that kills the cloud job the second you change your mind.

## How it works

```
Your song + lyrics
       │
       ▼
┌──────────────┐   separate + transcribe + align   ┌─────────────┐
│ Editor (you) │ ────────────────────────────────▶ │ Modal (GPU) │
│  drag words  │ ◀──────────────────────────────── │  word times │
└──────────────┘        render request             └─────────────┘
       │                                                │
       ▼                                                ▼
  KaraokeGen.mp4 ◀── per-word fill burned in ──── ffmpeg (CPU)
```

1. **Draft** — the cloud separates the vocals, listens for where each line is sung, aligns your lyrics to those positions, then times every single word. Your lyrics are the script; the AI only decides the timing.
2. **Edit** — play any word, drag it into place, fix any text. Seconds per line, not minutes.
3. **Render** — your timing is burned into a 1280×720, 30fps MP4 over a clean instrumental bed.

## Powering the cloud

The exe is the only way to run the app — there is no `pip install` / `uvicorn` browser mode. The cloud backend deploys itself from inside the app. Manual deploy is only for developers:

```powershell
pip install modal
modal setup                                          # authenticate

cd karaokegen
git clone https://github.com/jhuang448/LyricsAlignment-Multilingual   # vendored aligner (gitignored)

modal volume create karaokengen-models                 # weight cache (auto-created on deploy)
modal volume create karaokengen-files                  # job stems + outputs (auto-created on deploy)
modal run modal_app.py::download_models                # one-time: pre-cache weights (~$0.50)
modal deploy modal_app.py                              # deploy the API
```

Paste the printed URL and the key you set for `MODAL_API_KEY` into the app's gear-icon Settings and hit Save (they land in `%APPDATA%\KaraokeGen\config.json`).

> **Windows note:** prefix Modal commands with `$env:PYTHONUTF8=1; $env:PYTHONIOENCODING='utf-8'` to avoid console encoding errors.

**Cost** — Modal bills per second, only while a job runs: roughly **$0.03–0.10 per draft** and **$0.01 per render**. About $30 covers 300–500 songs with cached models.

<details>
<summary><b>Project structure</b></summary>

```
karaokegen/
├── KaraokeGen/              # shared core (pure Python, no GPU imports)
│   ├── config.py            # Settings — single source of truth (KVC_* env overrides)
│   ├── models.py            # API schemas (AlignmentResult, job requests/results)
│   ├── lyrics.py            # hint parsing, transcript-to-lines, verification
│   ├── render.py            # ASS subtitle generation + ffmpeg burn
│   ├── audio.py             # onset detection + ffmpeg wrappers
│   ├── align.py             # GPU: separation, ASR, line alignment + polish
│   ├── ensemble.py          # dual-aligner arbitration + English word median
│   ├── word_ctc.py          # whole-song + windowed CTC word timing
│   └── tagalog_g2p.py       # Tagalog → IPA for the aligner
├── modal_app.py             # Modal images, volumes, draft/render jobs, API
├── local_app/
│   ├── server.py            # FastAPI: serves the UI, proxies to Modal
│   ├── video.py             # local ffmpeg: audio extract / instrumental remux
│   ├── exe_main.py          # desktop entry point (uvicorn + native window)
│   └── templates/ static/   # editor UI (HTML/JS/CSS)
├── build_exe.ps1            # one-command KaraokeGen.exe build
└── .env.example
```

</details>

<details>
<summary><b>Advanced configuration</b></summary>

All settings live in `KaraokeGen/config.py` with `KVC_*` environment overrides.
The ones you'll most likely touch:

| Setting | Default | What it does |
|---------|---------|--------------|
| `genre` | `"hiphop"` | `"hiphop"` = MMS-FA word engine, `"ballad"` = MMS-1B engine |
| `start_s` (per job) | `0.0` | Lead-in skip for live takes — see below |
| `timing_offset_s` | `0.0` | Global shift applied to all timings |
| `video_w / video_h / video_fps` | `1280 / 720 / 30` | Output resolution and framerate |
| `refine_timings` | `True` | Alignment refine pass (`KVC_REFINE_TIMINGS=0` skips it) |

Dependency pins that matter: `audio-separator[gpu]==0.44.5`, `transformers==4.49.0`, `numpy>=2,<2.5` — see `requirements-modal.txt`.

**Live recordings (spoken intro).** A live take opens with talk that is not lyrics. Deleting those lines from the lyrics box does *not* remove the audio: the aligner still hears several seconds of speech with real vocal energy, and every anchoring pass (stable-ts, LA line starts, onset/env arbitration, CTC windows) can pull the first lyric lines onto it. Set **Song starts at** in step 1 (`0:45`, or `45`) and the backend copies every stem it analyzes with that much cut off the front, then shifts the timings back onto the full-song clock — so nothing can latch onto the intro, the transcript no longer contains it, and the rendered MP4 still includes it. Alternatively call the API with `"start_s": 45.0` in the `/draft` or `/transcribe` payload.

</details>

<details>
<summary><b>API reference</b></summary>

All Modal endpoints require `Authorization: Bearer <MODAL_API_KEY>`.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/draft` | Submit a draft job (separate + transcribe + align) |
| POST | `/video-draft` | Separation-only job (lyric-video mode) |
| GET | `/jobs/{call_id}` | Poll job status |
| GET | `/progress/{draft_job_id}` | Pipeline stage + percentage |
| GET | `/draft-result/{draft_job_id}` | Persisted result (powers resume) |
| POST | `/cancel/{call_id}` | Kill a running GPU job |
| POST | `/render` · `/render-word` | Submit a render job (word fill) |
| GET | `/files/{file_id}` | Download the rendered MP4 |
| GET | `/instrumental/{id}` · `/vocals-raw/{id}` · `/second-voice/{id}` | Stream stems |

The local server mirrors these under `/api/*`, adding upload handling, video remuxing, and static file serving.

</details>

## Known limitations

- Dense Taglish rap with heavy drift and sparse-song outros may need manual word dragging — that's what the editor is for.
- Parenthetical ad-libs sometimes get interpolated timing; drag to fix.
- The alignment library (stable-ts) is archived upstream; it works today and will be replaced if it ever breaks.

## Credits

- [audio-separator](https://github.com/nomadkaraoke/python-audio-separator) and the UVR community (separation models)
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) via [stable-ts](https://github.com/jianfch/stable-ts)
- [LyricsAlignment-Multilingual](https://github.com/jhuang448/LyricsAlignment-Multilingual)
- [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR) · [wavesurfer.js](https://wavesurfer-js.org/) · [Modal](https://modal.com) · FFmpeg

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

Use only with audio you have the rights to process. Provided as-is for personal / educational use.
