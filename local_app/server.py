"""Local FastAPI app: serves the editor UI + proxies to the Modal API.

Lightweight: no torch, no GPU. Just fastapi + httpx. The browser holds the
alignment in memory and edits LINE timings via wavesurfer.js regions
(line-level only — no per-word editing).

Run:  uvicorn local_app.server:app --port 8765
Binds to 127.0.0.1 only (personal use - not exposed to LAN).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

log = logging.getLogger(__name__)


def _bundle_base_dir() -> Path:
    """Resolve local_app dir both from source and inside a PyInstaller bundle.

    PyInstaller extracts --add-data files under sys._MEIPASS, so
    Path(__file__).parent is wrong when frozen. The exe build ships
    templates/static as local_app/templates + local_app/static.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "local_app"  # type: ignore[attr-defined]
    return Path(__file__).parent


def _exe_dir() -> Path:
    """Dir holding the .exe when frozen, else the repo root (dev)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _appdata_dir() -> Path:
    """User-writable config home. LOCALAPPDATA on Windows, XDG fallback elsewhere."""
    for key in ("LOCALAPPDATA", "APPDATA"):
        v = os.environ.get(key)
        if v:
            return Path(v) / "KaraokeGen"
    return Path.home() / ".karaokegen"


def _config_path() -> Path:
    return _appdata_dir() / "config.json"


def _load_config_file() -> dict:
    try:
        p = _config_path()
        if p.exists():
            # utf-8-sig: tolerates BOM if the user hand-edited with Notepad
            return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        pass
    return {}


def _save_config_file(url: str, key: str) -> None:
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Restrict permissions on POSIX: this file holds the API key.
    try:
        if os.name != "nt":
            os.chmod(p.parent, 0o700)
    except Exception:
        pass
    data = json.dumps(
        {"modal_api_url": url.strip(), "modal_api_key": key.strip()},
        indent=2)
    # Windows: replace can fail if the target is locked (e.g., concurrent
    # POST /api/config). Retry once after unlink.
    tmp: Path | None = None
    for attempt in range(2):
        try:
            tmp = p.with_suffix(f".tmp.{os.getpid()}")
            tmp.write_text(data, encoding="utf-8")
            # ensure data hits disk before replace (power-loss safety)
            try:
                with open(tmp, "r+b") as f:
                    f.flush()
                    os.fsync(f.fileno())
            except Exception:
                pass
            tmp.replace(p)
            try:
                if os.name != "nt":
                    os.chmod(p, 0o600)
            except Exception:
                pass
            return
        except PermissionError:
            # Windows file lock (concurrent save) — unlink target once and retry.
            if attempt == 0:
                try:
                    if p.exists():
                        p.unlink()
                except Exception:
                    pass
                continue
            raise
        finally:
            try:
                if tmp is not None and tmp.exists():
                    # after a successful replace tmp no longer exists (renamed)
                    tmp.unlink(missing_ok=True)
            except Exception:
                pass


def _resolve_modal_config() -> tuple[str, str]:
    """Priority: AppData config.json > env/.env (dev only).

    When frozen (exe), ONLY AppData counts — the repo's .env must not make
    the wizard appear configured on a fresh PC. That's why root and dist
    behaved differently: root sat next to the repo's .env, dist didn't.
    """
    cfg = _load_config_file()
    url = (cfg.get("modal_api_url") or "").strip()
    key = (cfg.get("modal_api_key") or "").strip()
    if url or key:
        return url, key
    if getattr(sys, "frozen", False):
        return "", ""
    return (os.environ.get("MODAL_API_URL", "").strip(),
            os.environ.get("MODAL_API_KEY", "").strip())


# .env is dev-only. Frozen exe uses ONLY AppData config.json — never the
# repo's .env and never process env, so a fresh PC with the mirrored exe at
# the repo root does not appear "configured" from stale env. There is no
# .env/env escape hatch in the frozen exe by design (single source of truth).
if not getattr(sys, "frozen", False):
    load_dotenv(_exe_dir() / ".env")
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASE_DIR = _bundle_base_dir()
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))

MODAL_URL, MODAL_KEY = _resolve_modal_config()
MODAL_URL = MODAL_URL.rstrip("/")

# 100 MB upload cap (base64 inflates ~33%, so ~75 MB raw audio max)
MAX_AUDIO_BYTES = 100 * 1024 * 1024
# lyric-video uploads can be large MP4s; only the extracted audio travels to
# Modal, so the video itself is bounded generously.
MAX_VIDEO_BYTES = 300 * 1024 * 1024

app = FastAPI(title="KaraokeGen Editor")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.middleware("http")
async def no_cache_ui(request: Request, call_next):
    """The editor evolved fast and stale cached editor.js caused edits to be
    silently ignored (old word-level JS vs new line-level render). Never let
    the browser cache the UI assets."""
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-store"
    return resp

# Shared httpx client (reuses TLS connections across poll requests)
_http_client: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=300, write=300, pool=10))
    return _http_client


def _require_modal_url() -> str:
    """Modal base URL or a clear 412 (not a bare 500).

    Without this, a missing URL becomes `client.get("/vocals-raw/...")` —
    a relative URL that raises inside httpx and surfaces as HTTP 500 with
    no hint to open Settings."""
    url = _effective_modal_url()
    if not url:
        raise HTTPException(412, "Modal backend not configured — open Settings (gear icon) and save your Modal URL and key")
    if not _configured():
        raise HTTPException(412, "Modal API key not set — open Settings (gear icon) and save your key")
    return url


_PROXY_LOG = _appdata_dir() / "proxy.log"


def _proxy_log(msg: str) -> None:
    """Append one line to the AppData proxy log (rotated at 200 KB).

    The windowed exe has no console, so upstream Modal failures would
    otherwise leave zero evidence. Never raises."""
    try:
        _PROXY_LOG.parent.mkdir(parents=True, exist_ok=True)
        if _PROXY_LOG.exists() and _PROXY_LOG.stat().st_size > 200 * 1024:
            _PROXY_LOG.write_text("", encoding="utf-8")
        import datetime
        # Single-line log: upstream bodies can contain newlines/CR (log forgery).
        clean = str(msg).replace("\r", " ").replace("\n", " ")[:2000]
        with open(_PROXY_LOG, "a", encoding="utf-8") as f:
            f.write(datetime.datetime.now().strftime("%H:%M:%S") + " " + clean + "\n")
    except Exception:
        pass


def _modal_exc_to_http(exc: Exception) -> HTTPException:
    """Map httpx errors from the Modal API to proper local status codes.

    Without this every Modal 404/401/413 became a local 500, hiding the real
    cause from the editor (which keys 401 → 're-save key' messaging).
    5xx/timeout/connect failures are also appended to proxy.log so a
    'stem unavailable HTTP 500' can actually be diagnosed afterwards.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            body = exc.response.text[:1000]
        except Exception:
            body = "modal request failed"
        try:
            path = str(exc.request.url).split("modal.run", 1)[-1]
        except Exception:
            path = "?"
        if exc.response.status_code >= 500:
            _proxy_log("upstream %s -> %s: %s" % (path, exc.response.status_code, body[:200]))
        return HTTPException(exc.response.status_code, body)
    if isinstance(exc, httpx.TimeoutException):
        _proxy_log("timeout: %s" % (exc,))
        return HTTPException(504, "Modal API timed out — retry in a moment")
    if isinstance(exc, httpx.RequestError):
        _proxy_log("connect fail: %s" % (exc,))
        return HTTPException(502, f"Cannot reach Modal API: {exc}")
    return HTTPException(500, str(exc)[:500])


def _modal_headers() -> dict:
    # Re-resolve every call so a POST /api/config takes effect without restart
    _, key = _resolve_modal_config()
    k = key or MODAL_KEY
    h = {"Content-Type": "application/json"}
    if k:
        h["Authorization"] = "Bearer " + k
    return h


def _effective_modal_url() -> str:
    url, _ = _resolve_modal_config()
    return (url or MODAL_URL).rstrip("/")


def _configured() -> bool:
    url, key = _resolve_modal_config()
    url = url or MODAL_URL
    key = key or MODAL_KEY
    return bool(url and key)


def _build_stamp() -> str:
    try:
        p = BASE_DIR / "static" / "build.json"
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8-sig"))
            return "build %s (%s)" % (d.get("built", "?"), d.get("commit", "?"))
    except Exception:
        pass
    return "dev build"


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return TEMPLATES.TemplateResponse(
        request=request,
        name="editor.html",
        context={
            "modal_url": _effective_modal_url() or "(not configured — open Settings)",
            "configured": _configured(),
            "build_stamp": _build_stamp(),
        },
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(str(BASE_DIR / "static" / "favicon.ico"), media_type="image/x-icon")


# ---------------------------------------------------------------------------
# draft: upload audio (or lyric video) + optional lyrics -> Modal -> poll
# ---------------------------------------------------------------------------
VIDEO_CACHE_DIR = _appdata_dir() / "video_cache"
VIDEO_CACHE_TTL_S = 6 * 3600  # keep the uploaded lyric video 6h for the remux step


def _video_hash(audio_bytes: bytes) -> str:
    """Must match Modal's _audio_hash so the video is keyed by the same stem."""
    return hashlib.sha256(audio_bytes).hexdigest()[:12]


def _prune_video_cache():
    try:
        cache_v = VIDEO_CACHE_DIR / "v"
        if not cache_v.exists():
            return
        now = time.time()
        for p in cache_v.glob("*"):
            try:
                if now - p.stat().st_mtime > VIDEO_CACHE_TTL_S:
                    if p.is_dir():
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        p.unlink(missing_ok=True)
            except Exception:
                continue
    except Exception:
        pass


from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(_: FastAPI):
    _prune_video_cache()
    yield


app.router.lifespan_context = _lifespan


def _clamp_start_s(value) -> float:
    """Seconds of lead-in to skip before ASR/alignment (0 = align from 0:00).
    Untrusted client input, so keep it inside a range the backend can honour."""
    try:
        return max(0.0, min(float(value or 0.0), 3600.0))
    except (TypeError, ValueError):
        return 0.0


_CALL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_]{3,127}$")
_HEX12_RE = re.compile(r"^[0-9a-f]{12}$")


def _check_call_id(call_id: str) -> None:
    if not _CALL_ID_RE.match(call_id or ""):
        raise HTTPException(400, "invalid call id")


def _check_job_id(job_id: str) -> None:
    if not _HEX12_RE.match(job_id or ""):
        raise HTTPException(400, "invalid job id")


# Local guard mirroring the Modal render cap (20 MB alignment JSON).
MAX_RENDER_BODY_BYTES = 25 * 1024 * 1024


@app.post("/api/draft")
async def draft(
    audio: UploadFile | None = File(None),
    video: UploadFile | None = File(None),
    lyrics: str = Form(""),
    language: str = Form("tl"),
    genre: str = Form("hiphop"),
    start_s: float = Form(0.0),
):
    if not _configured():
        raise HTTPException(412, "Modal backend not configured — open Settings (gear icon) and save your Modal URL and key")

    is_video = video is not None
    if is_video:
        from local_app import video as video_utils

        video_bytes = await video.read(MAX_VIDEO_BYTES + 1)
        if len(video_bytes) > MAX_VIDEO_BYTES:
            raise HTTPException(
                413, "video too large (max %d MB)" % (MAX_VIDEO_BYTES // 1024 // 1024))
        tmp = Path(tempfile.mkdtemp(prefix="kvg_"))
        try:
            vpath = tmp / "upload.mp4"
            await asyncio.to_thread(vpath.write_bytes, video_bytes)
            wav_path = tmp / "audio.wav"
            # ffmpeg is CPU-bound + blocks the event loop; run off-thread
            await asyncio.to_thread(video_utils.extract_audio, vpath, wav_path)
            audio_bytes = await asyncio.to_thread(wav_path.read_bytes)
        except RuntimeError as exc:
            # _ffmpeg prefixes media failures with "ffmpeg failed:" (bad file,
            # no audio stream → user error 422); missing binary → 500.
            if str(exc).startswith("ffmpeg failed:"):
                raise HTTPException(422, f"Could not extract audio: {exc}")
            raise HTTPException(500, str(exc))
        finally:
            await asyncio.to_thread(shutil.rmtree, tmp, ignore_errors=True)

        draft_job_id = _video_hash(audio_bytes)
        cache = VIDEO_CACHE_DIR / "v" / draft_job_id
        await asyncio.to_thread(cache.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread((cache / "original.mp4").write_bytes, video_bytes)
    else:
        if audio is None:
            raise HTTPException(400, "provide either an audio or a video file")
        audio_bytes = await audio.read(MAX_AUDIO_BYTES + 1)
        if len(audio_bytes) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "audio too large (max %d MB)" % (MAX_AUDIO_BYTES // 1024 // 1024))
        draft_job_id = _video_hash(audio_bytes)

    audio_b64 = base64.b64encode(audio_bytes).decode("ascii")

    payload = {
        "audio_b64": audio_b64,
        "language": language,
        "lyrics": lyrics,
        "genre": genre,
        # Lead-in skip: seconds of spoken intro the aligner must not hear.
        "start_s": _clamp_start_s(start_s),
    }
    client = _client()
    try:
        r = await client.post(
            _effective_modal_url() + ("/video-draft" if is_video else "/draft"),
            json=payload, headers=_modal_headers())
        r.raise_for_status()
    except httpx.TimeoutException:
        raise HTTPException(504, "Modal API timed out (cold start or large upload). Retry in a moment.")
    except httpx.HTTPStatusError as exc:
        try:
            body = exc.response.text[:1000]
        except Exception:
            body = "draft failed"
        raise HTTPException(exc.response.status_code, body)
    except httpx.RequestError as exc:
        raise _modal_exc_to_http(exc)
    result = r.json()
    result["mode"] = "video" if is_video else "audio"
    return result


@app.post("/api/transcribe")
async def transcribe(
    audio: UploadFile | None = File(None),
    language: str = Form("tl"),
    genre: str = Form("hiphop"),
    start_s: float = Form(0.0),
):
    """No-lyrics flow, phase 1 (audio only): separate + transcribe, NO
    alignment. Returns {"job_id", "draft_job_id"}; the transcript arrives
    via the job poll. The follow-up /api/draft with edited lyrics reuses
    the cached stems and aligns. Requires a redeployed backend (Modal
    /transcribe); old backends 404 with a redeploy hint."""
    if not _configured():
        raise HTTPException(412, "Modal backend not configured — open Settings (gear icon) and save your Modal URL and key")
    if audio is None:
        raise HTTPException(400, "provide an audio file")
    audio_bytes = await audio.read(MAX_AUDIO_BYTES + 1)
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "audio too large (max %d MB)" % (MAX_AUDIO_BYTES // 1024 // 1024))
    draft_job_id = _video_hash(audio_bytes)
    audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
    payload = {
        "audio_b64": audio_b64,
        "language": language,
        "lyrics": "",
        "genre": genre,
        # Lead-in skip: the transcript must not contain the spoken intro.
        "start_s": _clamp_start_s(start_s),
    }
    client = _client()
    try:
        r = await client.post(
            _effective_modal_url() + "/transcribe",
            json=payload, headers=_modal_headers())
        r.raise_for_status()
    except httpx.TimeoutException:
        raise HTTPException(504, "Modal API timed out (cold start or large upload). Retry in a moment.")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(
                404, "transcribe endpoint not found on the backend — "
                     "redeploy via Settings (gear icon → Save token & Deploy backend)")
        try:
            body = exc.response.text[:1000]
        except Exception:
            body = "transcribe failed"
        raise HTTPException(exc.response.status_code, body)
    except httpx.RequestError as exc:
        raise _modal_exc_to_http(exc)
    result = r.json()
    result["mode"] = "transcript"
    return result


@app.get("/api/video-karaoke/{draft_job_id}")
async def video_karaoke(draft_job_id: str):
    """Lyric-video mode: remux the instrumental (from Modal) onto the user's
    ORIGINAL video, preserving their burned-in lyrics. Streams the MP4 back."""
    if not all(c in "0123456789abcdef" for c in draft_job_id) or len(draft_job_id) != 12:
        raise HTTPException(400, "invalid draft_job_id")

    original = VIDEO_CACHE_DIR / "v" / draft_job_id / "original.mp4"
    if not original.exists():
        raise HTTPException(404, "original video no longer cached — re-upload it")

    if not _configured():
        raise HTTPException(500, "MODAL_API_URL not set in environment")

    # fetch the instrumental stem Modal saved for this audio
    client = _client()
    try:
        r = await client.get(
            _effective_modal_url() + "/instrumental/" + draft_job_id, headers=_modal_headers())
        r.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(404, "instrumental not ready on Modal")
        raise _modal_exc_to_http(exc)
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        raise _modal_exc_to_http(exc)
    instrumental = r.content

    from local_app import video as video_utils

    out_dir = Path(tempfile.mkdtemp(prefix="kvg_remux_"))
    try:
        inst_path = out_dir / "instrumental.wav"
        await asyncio.to_thread(inst_path.write_bytes, instrumental)
        out_path = out_dir / "karaoke.mp4"
        try:
            await asyncio.to_thread(video_utils.remux_karaoke, original, inst_path, out_path)
        except RuntimeError as exc:
            if str(exc).startswith("ffmpeg failed:"):
                raise HTTPException(422, f"Could not remux video: {exc}")
            raise HTTPException(500, str(exc))

        def iterfile():
            try:
                with open(out_path, "rb") as f:
                    while chunk := f.read(1024 * 1024):
                        yield chunk
            finally:
                shutil.rmtree(out_dir, ignore_errors=True)

        return StreamingResponse(
            iterfile(), media_type="video/mp4",
            headers={"Content-Disposition": "attachment; filename=karaoke_%s.mp4" % original.stem})
    except Exception:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise


@app.get("/api/vocals-raw/{draft_job_id}")
async def proxy_vocals_raw(draft_job_id: str):
    if not all(c in "0123456789abcdef" for c in draft_job_id) or len(draft_job_id) != 12:
        raise HTTPException(400, "invalid draft_job_id")
    _require_modal_url()
    client = _client()
    try:
        r = await client.get(_effective_modal_url() + "/vocals-raw/" + draft_job_id, headers=_modal_headers())
        r.raise_for_status()
    except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError) as exc:
        raise _modal_exc_to_http(exc)
    return StreamingResponse(
        r.aiter_bytes(1024 * 1024), media_type="audio/wav",
        headers={"Content-Disposition": "attachment; filename=vocals_raw.wav"})


@app.get("/api/instrumental/{draft_job_id}")
async def proxy_instrumental(draft_job_id: str):
    if not all(c in "0123456789abcdef" for c in draft_job_id) or len(draft_job_id) != 12:
        raise HTTPException(400, "invalid draft_job_id")
    _require_modal_url()
    client = _client()
    try:
        r = await client.get(_effective_modal_url() + "/instrumental/" + draft_job_id, headers=_modal_headers())
        r.raise_for_status()
    except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError) as exc:
        raise _modal_exc_to_http(exc)
    return StreamingResponse(
        r.aiter_bytes(1024 * 1024), media_type="audio/wav",
        headers={"Content-Disposition": "attachment; filename=instrumental.wav"})


@app.get("/api/second-voice/{draft_job_id}")
async def proxy_second_voice(draft_job_id: str):
    if not all(c in "0123456789abcdef" for c in draft_job_id) or len(draft_job_id) != 12:
        raise HTTPException(400, "invalid draft_job_id")
    _require_modal_url()
    client = _client()
    try:
        r = await client.get(_effective_modal_url() + "/second-voice/" + draft_job_id, headers=_modal_headers())
        r.raise_for_status()
    except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError) as exc:
        raise _modal_exc_to_http(exc)
    return StreamingResponse(
        r.aiter_bytes(1024 * 1024), media_type="audio/wav",
        headers={"Content-Disposition": "attachment; filename=second_voice.wav"})


@app.get("/api/backing-vocals/{draft_job_id}")
async def proxy_backing_vocals(draft_job_id: str):
    if not all(c in "0123456789abcdef" for c in draft_job_id) or len(draft_job_id) != 12:
        raise HTTPException(400, "invalid draft_job_id")
    _require_modal_url()
    client = _client()
    try:
        r = await client.get(_effective_modal_url() + "/backing-vocals/" + draft_job_id, headers=_modal_headers())
        r.raise_for_status()
    except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError) as exc:
        raise _modal_exc_to_http(exc)
    return StreamingResponse(
        r.aiter_bytes(1024 * 1024), media_type="audio/wav",
        headers={"Content-Disposition": "attachment; filename=backing_vocals.wav"})


@app.get("/api/jobs/{call_id}")
async def poll_job(call_id: str):
    _check_call_id(call_id)
    if not _configured():
        raise HTTPException(500, "MODAL_API_URL not set")
    client = _client()
    try:
        r = await client.get(_effective_modal_url() + "/jobs/" + call_id, headers=_modal_headers())
        r.raise_for_status()
    except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError) as exc:
        raise _modal_exc_to_http(exc)
    return r.json()


@app.get("/api/progress/{draft_job_id}")
async def get_progress(draft_job_id: str):
    _check_job_id(draft_job_id)
    if not _configured():
        raise HTTPException(500, "MODAL_API_URL not set")
    client = _client()
    try:
        r = await client.get(_effective_modal_url() + "/progress/" + draft_job_id, headers=_modal_headers())
        r.raise_for_status()
    except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError) as exc:
        raise _modal_exc_to_http(exc)
    return r.json()


@app.get("/api/render-progress/{draft_job_id}")
async def get_render_progress(draft_job_id: str):
    _check_job_id(draft_job_id)
    if not _configured():
        raise HTTPException(500, "MODAL_API_URL not set")
    client = _client()
    try:
        r = await client.get(_effective_modal_url() + "/render-progress/" + draft_job_id, headers=_modal_headers())
        r.raise_for_status()
    except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError) as exc:
        raise _modal_exc_to_http(exc)
    return r.json()


@app.get("/api/draft-result/{draft_job_id}")
async def get_draft_result(draft_job_id: str):
    _check_job_id(draft_job_id)
    if not _configured():
        raise HTTPException(500, "MODAL_API_URL not set")
    client = _client()
    try:
        r = await client.get(_effective_modal_url() + "/draft-result/" + draft_job_id, headers=_modal_headers())
        r.raise_for_status()
    except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError) as exc:
        raise _modal_exc_to_http(exc)
    return r.json()


@app.post("/api/cancel/{call_id}")
async def cancel_job(call_id: str):
    _check_call_id(call_id)
    if not _configured():
        raise HTTPException(500, "MODAL_API_URL not set")
    client = _client()
    try:
        r = await client.post(_effective_modal_url() + "/cancel/" + call_id, headers=_modal_headers())
        r.raise_for_status()
    except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError) as exc:
        raise _modal_exc_to_http(exc)
    return r.json()


# ---------------------------------------------------------------------------
# render: send edited alignment + lyrics -> Modal -> poll -> stream MP4
# ---------------------------------------------------------------------------
@app.post("/api/render")
async def render(request: Request):
    if not _configured():
        raise HTTPException(500, "MODAL_API_URL not set")
    try:
        raw = await request.body()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    if len(raw) > MAX_RENDER_BODY_BYTES:
        raise HTTPException(413, "render payload too large")
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    client = _client()
    try:
        r = await client.post(_effective_modal_url() + "/render", json=body, headers=_modal_headers())
        r.raise_for_status()
    except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError) as exc:
        raise _modal_exc_to_http(exc)
    return r.json()


@app.post("/api/render-word")
async def render_word(request: Request):
    """Word-level karaoke render (strict per-word {\\k} ASS). Same payload as
    /api/render; forces word mode server-side."""
    if not _configured():
        raise HTTPException(500, "MODAL_API_URL not set")
    try:
        raw = await request.body()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    if len(raw) > MAX_RENDER_BODY_BYTES:
        raise HTTPException(413, "render payload too large")
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "invalid JSON body")
    body["word_level"] = True
    body["highlight"] = "word"
    client = _client()
    try:
        r = await client.post(_effective_modal_url() + "/render-word", json=body, headers=_modal_headers())
        r.raise_for_status()
    except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError) as exc:
        raise _modal_exc_to_http(exc)
    return r.json()


@app.get("/api/preview-ass/{draft_job_id}/{mode}")
async def preview_ass(draft_job_id: str, mode: str):
    """Generate ASS locally from the persisted draft result (fetched from the
    Modal volume once) for instant A/B of line vs word highlighting."""
    if mode not in ("line", "word"):
        raise HTTPException(400, "mode must be line or word")
    if not all(c in "0123456789abcdef" for c in draft_job_id) or len(draft_job_id) != 12:
        raise HTTPException(400, "invalid draft_job_id")
    _require_modal_url()
    client = _client()
    try:
        r = await client.get(_effective_modal_url() + "/draft-result/" + draft_job_id,
                             headers=_modal_headers())
        r.raise_for_status()
    except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError) as exc:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
            raise HTTPException(404, "draft result not found (run a draft first)")
        raise _modal_exc_to_http(exc)
    try:
        data = r.json()
        from KaraokeGen.models import AlignmentResult
        from KaraokeGen.render import result_to_ass, result_to_ass_word
        from fastapi.responses import Response
        from KaraokeGen.config import Settings
        result = AlignmentResult.model_validate(data["alignment"])
        s = Settings()
        ass = result_to_ass_word(result, s) if mode == "word" else result_to_ass(result, s)
        return Response(content=ass, media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{draft_job_id}_{mode}.ass"'})
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, f"ASS generation failed: {exc}")


@app.get("/api/file/{file_id}")
async def stream_file(file_id: str):
    """Proxy-stream the finished MP4 from Modal to the browser.

    Uses a dedicated streaming client (not the shared poll client) and
    keeps it open for the duration of the response."""
    if not all(c in "0123456789abcdef" for c in file_id) or len(file_id) != 12:
        raise HTTPException(400, "invalid file_id")
    if not _configured():
        raise HTTPException(500, "MODAL_API_URL not set")

    stream_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10, read=None, write=30, pool=10))
    try:
        req = stream_client.build_request("GET", _effective_modal_url() + "/files/" + file_id, headers=_modal_headers())
        resp = await stream_client.send(req, stream=True)
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        await stream_client.aclose()
        raise _modal_exc_to_http(exc)
    if resp.status_code != 200:
        body = await resp.aread()
        await resp.aclose()
        await stream_client.aclose()
        try:
            detail = body.decode("utf-8", errors="replace")[:1000]
        except Exception:
            detail = "file not found"
        raise HTTPException(resp.status_code, detail)

    async def gen():
        try:
            async for chunk in resp.aiter_bytes(1024 * 1024):
                yield chunk
        finally:
            await resp.aclose()
            await stream_client.aclose()

    fname = "%s.mp4" % file_id
    return StreamingResponse(
        gen(), media_type="video/mp4",
        headers={"Content-Disposition": 'attachment; filename="%s"' % fname},
    )


# ---------------------------------------------------------------------------
# config: AppData-persisted Modal URL/key + connectivity test
# ---------------------------------------------------------------------------
@app.get("/api/config")
async def get_config():
    url, key = _resolve_modal_config()
    url = url or MODAL_URL
    key = key or MODAL_KEY
    return {
        "modal_api_url": url,
        "modal_api_key_set": bool(key),
        "modal_api_key_preview": (key[:4] + "..." + key[-4:] if len(key) >= 8 else ("..." if key else "")),
        "configured": _configured(),
        "config_path": str(_config_path()),
    }


def _is_local_url(url: str) -> bool:
    """True for loopback dev URLs (http allowed); everything else must be https."""
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        return host in ("localhost", "127.0.0.1", "::1")
    except Exception:
        return False


def _check_modal_url(url: str) -> None:
    if not (url.startswith("https://") or url.startswith("http://")):
        raise HTTPException(400, "modal_api_url must start with https://")
    if url.startswith("http://") and not _is_local_url(url):
        raise HTTPException(400, "modal_api_url must use https:// (http is only allowed for localhost)")


@app.post("/api/config")
async def set_config(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "invalid JSON body")
    url = (body.get("modal_api_url") or "").strip().rstrip("/")
    key = (body.get("modal_api_key") or "").strip()
    if not url:
        raise HTTPException(400, "modal_api_url is required")
    if not key:
        raise HTTPException(400, "modal_api_key is required")
    if len(url) > 256 or len(key) > 256:
        raise HTTPException(400, "modal_api_url/key too long")
    _check_modal_url(url)
    _save_config_file(url, key)
    # update live globals so the same process uses the new values
    global MODAL_URL, MODAL_KEY
    MODAL_URL, MODAL_KEY = url, key
    os.environ["MODAL_API_URL"] = url
    os.environ["MODAL_API_KEY"] = key
    return {"ok": True, "configured": True}


@app.post("/api/config/test")
async def test_config(request: Request):
    body: dict = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            parsed = await request.json()
            body = parsed if isinstance(parsed, dict) else {}
        except Exception:
            body = {}
    url = (body.get("modal_api_url") or "").strip().rstrip("/")
    key = (body.get("modal_api_key") or "").strip()
    if not url or not key:
        # fall back to saved config
        url, key = _resolve_modal_config()
        url = url or MODAL_URL
        key = key or MODAL_KEY
    if not url or not key:
        raise HTTPException(400, "modal_api_url and modal_api_key are required")
    # SSRF guard: this endpoint fetches a user-supplied URL server-side, so
    # only allow the Modal API host (or loopback for dev). Without this, a
    # malicious page reaching the loopback server could make it probe the LAN.
    try:
        from urllib.parse import urlparse
        _host = (urlparse(url).hostname or "").lower()
    except Exception:
        raise HTTPException(400, "invalid modal_api_url")
    if not (_host.endswith(".modal.run") or _host in ("localhost", "127.0.0.1", "::1")):
        raise HTTPException(400, "modal_api_url must be a *.modal.run URL")
    _check_modal_url(url)
    # Hit a cheap Modal endpoint that requires auth: /jobs/invalid returns
    # 401 on bad key, 404 on good key (no such job). Both prove connectivity.
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=15, write=10, pool=10)) as c:
            r = await c.get(url + "/jobs/__kvg_test__",
                            headers={"Authorization": "Bearer " + key})
        if r.status_code == 401:
            return {"ok": False, "reason": "invalid key (401)"}
        if r.status_code in (404, 422):
            return {"ok": True}
        if r.status_code == 200:
            return {"ok": True}
        return {"ok": False, "reason": f"unexpected {r.status_code}: {r.text[:200]}"}
    except httpx.TimeoutException:
        return {"ok": False, "reason": "timeout — check the URL and your network"}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:300]}


# ---------------------------------------------------------------------------
# auto Modal setup: one-click login + deploy — no typing
# ---------------------------------------------------------------------------
_MODAL_DEPLOY_LOG = _appdata_dir() / "deploy.log"
_MODAL_DEPLOY_STATE: dict = {"running": False, "done": False, "ok": False, "error": ""}


def _modal_token_path() -> Path:
    # modal stores token at ~/.modal.toml (Windows: %USERPROFILE%\.modal.toml)
    return Path.home() / ".modal.toml"


def _modal_is_logged_in() -> bool:
    p = _modal_token_path()
    if not p.exists():
        return False
    try:
        txt = p.read_text(encoding="utf-8", errors="ignore")
        return "token_id" in txt and "token_secret" in txt
    except Exception:
        return False


def _modal_app_path() -> Path | None:
    # repo root when dev, _MEIPASS when frozen
    for base in [Path(__file__).resolve().parent.parent,
                 Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "frozen", False) else None,
                 _appdata_dir()]:
        if base and (base / "modal_app.py").exists():
            return base / "modal_app.py"
    # also check exe dir
    cand = _exe_dir() / "modal_app.py"
    if cand.exists():
        return cand
    return None


def _lyrics_align_path() -> Path | None:
    for base in [Path(__file__).resolve().parent.parent,
                 Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "frozen", False) else None,
                 _appdata_dir(), _exe_dir()]:
        if base and (base / "LyricsAlignment-Multilingual").exists():
            return base / "LyricsAlignment-Multilingual"
    return None


async def _run_deploy_bg():
    _MODAL_DEPLOY_STATE.update(running=True, done=False, ok=False, error="")
    _MODAL_DEPLOY_LOG.parent.mkdir(parents=True, exist_ok=True)
    try:
        app_path = _modal_app_path()
        if not app_path:
            raise RuntimeError("modal_app.py not found in bundle — rebuild the exe with build_exe.ps1")
        # ensure LyricsAlignment present
        if not _lyrics_align_path():
            # try to fetch it as zip (no git needed)
            _MODAL_DEPLOY_LOG.write_text("Fetching LyricsAlignment-Multilingual...\n", encoding="utf-8")
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=60) as c:
                    r = await c.get("https://github.com/jhuang448/LyricsAlignment-Multilingual/archive/refs/heads/main.zip")
                    r.raise_for_status()
                    fd, tmp_name = tempfile.mkstemp(suffix=".zip")
                    ztmp = Path(tmp_name)
                    try:
                        os.close(fd)
                        ztmp.write_bytes(r.content)
                        import zipfile
                        dst = _appdata_dir() / "LyricsAlignment-Multilingual"
                        with zipfile.ZipFile(ztmp) as zf:
                            # ZipSlip guard: refuse members escaping _appdata_dir().
                            _base = _appdata_dir().resolve()
                            for _m in zf.namelist():
                                _target = (_base / _m).resolve()
                                if _base not in _target.parents and _target != _base:
                                    raise RuntimeError(f"unsafe zip entry: {_m[:100]}")
                            zf.extractall(_appdata_dir())
                        # zip contains folder with suffix -main
                        for p in _appdata_dir().glob("LyricsAlignment-Multilingual*"):
                            if p.is_dir() and p.name != "LyricsAlignment-Multilingual":
                                if dst.exists():
                                    shutil.rmtree(dst, ignore_errors=True)
                                p.rename(dst)
                                break
                    finally:
                        try:
                            ztmp.unlink(missing_ok=True)
                        except Exception:
                            pass
            except Exception as exc:
                raise RuntimeError(f"Could not fetch LyricsAlignment-Multilingual: {exc}")

        workdir = app_path.parent
        modal_bin = shutil.which("modal")
        use_sdk = False
        if modal_bin:
            cmd = [modal_bin, "deploy", str(app_path)]
        elif getattr(sys, "frozen", False):
            # Pure browser copy-paste, no external `modal` binary needed:
            # deploy via the bundled `modal` SDK directly.
            use_sdk = True
            cmd = None  # type: ignore
        else:
            cmd = [sys.executable, "-m", "modal", "deploy", str(app_path)]
        if not use_sdk:
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(workdir), env=env,  # type: ignore
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            out = ""
            assert proc.stdout
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                txt = line.decode("utf-8", errors="replace")
                out += txt
                _MODAL_DEPLOY_LOG.write_text(out, encoding="utf-8")
            await proc.wait()
            if proc.returncode != 0:
                raise RuntimeError(f"modal deploy failed ({proc.returncode}):\n{out[-4000:]}")
            m = re.search(r"https://[^\s\"']+--karaokengen-api\.modal\.run", out)
            if not m:
                m = re.search(r"https://[^\s\"']+\.modal\.run", out)
            if not m:
                raise RuntimeError(f"Deploy succeeded but URL not found in output:\n{out[-2000:]}")
            url = m.group(0).rstrip("/")
        else:
            # SDK deploy — no external `modal` binary, pure bundled package.
            _MODAL_DEPLOY_LOG.write_text("Deploying via bundled SDK (no CLI)...\n", encoding="utf-8")
            try:
                import modal  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "modal SDK not bundled — rebuild the exe with `pip install modal` "
                    "and `build_exe.ps1` (requirements-exe.txt must include modal)"
                ) from exc
            import importlib.util as _ilu
            # make workdir importable for `import KaraokeGen` inside modal_app.py
            if str(workdir) not in sys.path:
                sys.path.insert(0, str(workdir))
            spec = _ilu.spec_from_file_location("modal_app", str(app_path))
            if spec is None or spec.loader is None:
                raise RuntimeError(f"Could not load {app_path}")
            mod = _ilu.module_from_spec(spec)
            sys.modules["modal_app"] = mod
            spec.loader.exec_module(mod)  # type: ignore
            # `app.deploy` is blocking — run in a thread so the event loop stays alive
            await asyncio.to_thread(mod.app.deploy)
            # Derive the web URL from the workspace that owns the token we just saved
            tok_txt = _modal_token_path().read_text(encoding="utf-8", errors="ignore")
            m_id = re.search(r'token_id\s*=\s*"([^"]+)"', tok_txt)
            m_sec = re.search(r'token_secret\s*=\s*"([^"]+)"', tok_txt)
            if not m_id or not m_sec:
                raise RuntimeError("Could not read token from ~/.modal.toml after save")
            try:
                from modal.config import _lookup_workspace, config as _modal_config
                server_url = _modal_config.get("server_url")
                ws = await _lookup_workspace(server_url, m_id.group(1), m_sec.group(1))
                workspace = getattr(ws, "username", None) or getattr(ws, "name", None) or str(ws)
            except Exception as exc:
                raise RuntimeError(f"Could not resolve Modal workspace: {exc}") from exc
            url = f"https://{workspace}--karaokengen-api.modal.run"
            out = f"Deployed via SDK\nURL: {url}\n"
            _MODAL_DEPLOY_LOG.write_text(out, encoding="utf-8")
        # The deployed API checks Authorization: Bearer <MODAL_API_KEY> where
        # MODAL_API_KEY is the Modal Secret (modal.Secret.from_dotenv()). We must
        # keep the local key in sync with that Secret. If the user already had a
        # key (AppData), keep it; otherwise derive from the token_secret or
        # generate one and attempt to push it as a Modal Secret so next deploys
        # match. Random-without-push is the old bug (401 loop).
        cur_url, cur_key = _resolve_modal_config()
        if not cur_key:
            # Prefer the token_secret itself (already known to Modal side) as the
            # API key — least surprise, no extra secret needed if the server has
            # no MODAL_API_KEY set (empty api_key bypasses auth). Fall back to
            # random only if token unreadable.
            try:
                _tok = _modal_token_path().read_text(encoding="utf-8", errors="ignore")
                _msec = re.search(r'token_secret\s*=\s*"([^"]+)"', _tok)
                if _msec:
                    cur_key = _msec.group(1)
            except Exception:
                pass
            if not cur_key:
                import secrets as _sec
                cur_key = _sec.token_hex(16)
                _MODAL_DEPLOY_LOG.write_text(
                    (_MODAL_DEPLOY_LOG.read_text(encoding="utf-8", errors="ignore") if _MODAL_DEPLOY_LOG.exists() else "")
                    + f"\nWARNING: generated random API key {cur_key[:8]}... — if the Modal app has MODAL_API_KEY set to a different value, set it to this key (`modal secret create MODAL_API_KEY {cur_key}`) or auth will 401.\n",
                    encoding="utf-8")
        _save_config_file(url, cur_key)
        global MODAL_URL, MODAL_KEY
        MODAL_URL, MODAL_KEY = url, cur_key
        os.environ["MODAL_API_URL"] = url
        os.environ["MODAL_API_KEY"] = cur_key
        _MODAL_DEPLOY_STATE.update(ok=True, error="")
    except Exception as exc:
        _MODAL_DEPLOY_STATE.update(ok=False, error=str(exc)[:2000])
        _MODAL_DEPLOY_LOG.write_text(
            (_MODAL_DEPLOY_LOG.read_text(encoding="utf-8", errors="ignore") if _MODAL_DEPLOY_LOG.exists() else "")
            + f"\nERROR: {exc}\n", encoding="utf-8")
    finally:
        _MODAL_DEPLOY_STATE.update(running=False, done=True)


@app.get("/api/modal/status")
async def modal_status():
    url, key = _resolve_modal_config()
    url = url or MODAL_URL
    key = key or MODAL_KEY
    return {
        "logged_in": _modal_is_logged_in(),
        "configured": _configured(),
        "modal_api_url": url,
        "modal_api_key_preview": (key[:4] + "..." + key[-4:] if len(key) >= 8 else ("..." if key else "")),
        "config_path": str(_config_path()),
        "modal_app_found": _modal_app_path() is not None,
        "lyrics_align_found": _lyrics_align_path() is not None,
        "deploy": dict(_MODAL_DEPLOY_STATE),
        "deploy_log_tail": (_MODAL_DEPLOY_LOG.read_text(encoding="utf-8", errors="ignore")[-4000:] if _MODAL_DEPLOY_LOG.exists() else ""),
        "modal_token_path": str(_modal_token_path()),
    }


@app.post("/api/modal/token")
async def modal_token(request: Request):
    """Save a pasted ak-/as- token (browser copy-paste, no CLI)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "invalid JSON body")
    tid = (body.get("token_id") or "").strip()
    tsec = (body.get("token_secret") or "").strip()
    if not tid or not tsec:
        raise HTTPException(400, "token_id and token_secret are required")
    # Strict charset: the values are interpolated into a TOML file, so quotes,
    # newlines, or escapes must be rejected (TOML injection).
    if not re.fullmatch(r"ak-[A-Za-z0-9\-_]{8,128}", tid):
        raise HTTPException(400, "Token ID should look like ak-... (letters, numbers, -/_ only)")
    if not re.fullmatch(r"as-[A-Za-z0-9\-_]{8,128}", tsec):
        raise HTTPException(400, "Token secret should look like as-... (letters, numbers, -/_ only)")
    try:
        p = _modal_token_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        # minimal .modal.toml — modal SDK reads this
        p.write_text(f"[default]\ntoken_id = \"{tid}\"\ntoken_secret = \"{tsec}\"\n", encoding="utf-8")
        try:
            if os.name != "nt":
                os.chmod(p, 0o600)
        except Exception:
            pass
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/modal/login")
async def modal_login():
    # Kept for backwards compat — now just opens the dashboard
    try:
        webbrowser.open("https://modal.com/")
        return {"ok": True, "browser_opened": True}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/modal/deploy")
async def modal_deploy():
    if not _modal_is_logged_in():
        raise HTTPException(412, "Not logged into Modal — hit Login first")
    if _MODAL_DEPLOY_STATE.get("running"):
        return {"ok": True, "already_running": True}
    _MODAL_DEPLOY_STATE.update(running=True, done=False, ok=False, error="")
    _MODAL_DEPLOY_LOG.write_text("Starting deploy...\n", encoding="utf-8")
    asyncio.create_task(_run_deploy_bg())
    return {"ok": True, "started": True}


@app.post("/api/config/clear")
async def clear_config():
    """Forget the saved Modal URL/key (gear → Disconnect)."""
    if _MODAL_DEPLOY_STATE.get("running"):
        raise HTTPException(409, "Deploy in progress — wait for it to finish before clearing")
    try:
        p = _config_path()
        if p.exists():
            p.unlink()
    except Exception as exc:
        raise HTTPException(500, str(exc))
    global MODAL_URL, MODAL_KEY
    MODAL_URL, MODAL_KEY = "", ""
    os.environ.pop("MODAL_API_URL", None)
    os.environ.pop("MODAL_API_KEY", None)
    return {"ok": True}


@app.post("/api/modal/disconnect")
async def modal_disconnect():
    """Switch account: clear AppData config + ~/.modal.toml token + deploy state.

    Refused while a deploy is running — otherwise the background deploy would
    re-save config at completion, resurrecting the account just cleared.
    """
    if _MODAL_DEPLOY_STATE.get("running"):
        raise HTTPException(409, "Deploy in progress — wait for it to finish before disconnecting")
    try:
        p = _config_path()
        if p.exists():
            p.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        t = _modal_token_path()
        if t.exists():
            t.unlink(missing_ok=True)
    except Exception:
        pass
    global MODAL_URL, MODAL_KEY
    MODAL_URL, MODAL_KEY = "", ""
    os.environ.pop("MODAL_API_URL", None)
    os.environ.pop("MODAL_API_KEY", None)
    _MODAL_DEPLOY_STATE.update(running=False, done=False, ok=False, error="")
    return {"ok": True}


@app.get("/api/modal/deploy/log")
async def modal_deploy_log():
    return {
        "state": dict(_MODAL_DEPLOY_STATE),
        "log": (_MODAL_DEPLOY_LOG.read_text(encoding="utf-8", errors="ignore") if _MODAL_DEPLOY_LOG.exists() else ""),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("local_app.server:app", host="127.0.0.1", port=8765, reload=True)
