"""One-time auto-setup helper: deploys the KaraokeGen backend into
the CALLER's own Modal account, using their Modal API token.

Why this exists: a Modal token (MODAL_TOKEN_ID / MODAL_TOKEN_SECRET)
only authenticates the Modal client — deployment happens by running
`modal deploy` somewhere with that token. Phones can't run the
Modal client, so this tiny helper does it server-side:

  POST /setup/start {token_id, token_secret} -> {job_id}
  GET  /setup/status/{job_id} -> {state, url?, api_key?, error?}

The deploy runs in a spawned function with the token ONLY in the
subprocess environment. The token is never logged, never stored,
and never used for anything except that one deploy. The backend's
API key is a fresh random key generated per setup (never the
caller's Modal token) — it is written to a .env next to
modal_app.py before deploying, so the backend's
modal.Secret.from_dotenv() picks it up, and it is returned to the
caller once, in the done job entry. Job state (state + deployed
URL + API key only) lives in a Modal Dict. Advanced users who
don't want to trust this helper can deploy modal_app.py
themselves and paste their URL + key into the app instead — same
result.

Abuse guards (this helper runs in the OWNER's account, so a
stranger can bill the owner by spamming it):
  * the token is verified with `modal token info` BEFORE any
    deploy is spawned — junk tokens are rejected in seconds with
    no deploy billed;
  * POST /setup/start is rate-limited per IP (3/hour), globally
    (50/day), and capped at 2 concurrent deploys;
  * GET /setup/status is rate-limited per IP (30/min) so a job id
    cannot be hammered to enumerate deployed URLs;
  * run_deploy is capped at 30 minutes (a cold first build with
    the AI models takes 5-10 min; a timeout is retried and
    resumes from cached image layers).

Deploy this helper once with:
  modal deploy server/setup_app.py::setup_app
It then lives at https://<workspace>--karaokegen-setup.<region>.modal.run
"""

from __future__ import annotations

import hashlib
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path

import modal
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

setup_app = modal.App("karaokegen-setup")


# Module level on purpose: with `from __future__ import annotations`,
# FastAPI resolves endpoint annotations from module globals — a
# request model nested inside api() is misread as a query param.
class StartRequest(BaseModel):
    token_id: str
    token_secret: str


jobs = modal.Dict.from_name("karaokegen-setup-jobs", create_if_missing=True)

# Rate-limit counters live in their own Dict so they never mix with
# job entries (and _lookup_job never sees them). A separate Dict also
# means the counters survive api() container restarts and are shared
# across containers — in-memory counters would be per-container and
# trivially bypassed by hitting a fresh container.
ratelimit = modal.Dict.from_name(
    "karaokegen-setup-ratelimit", create_if_missing=True
)

# Abuse-guard limits. Legit use is ~1 start per user ever (plus a
# retry or two), and the phone polls status every 4s (15/min) —
# these caps leave wide headroom for real users while a flood hits
# a wall in seconds.
STARTS_PER_IP_PER_HOUR = 3
STARTS_GLOBAL_PER_DAY = 50
MAX_CONCURRENT_DEPLOYS = 2
STATUS_PER_IP_PER_MIN = 30
# `modal token info` is one read-only control-plane call; on Modal
# infra it answers in seconds. 45s bounds a hung/slow check — a
# timeout fails CLOSED (no spawn), the caller retries.
VALIDATE_TIMEOUT_SEC = 45

# The KaraokeGen backend sources, baked in so the deploy target
# (/root/karaokegen/modal_app.py) and its package exist locally for
# the Modal client to serialize. LyricsAlignment-Multilingual is
# NOT baked in (it is large) — run_deploy fetches it on first use,
# the same way the desktop app does.
_REPO_ROOT = Path(__file__).parent.parent
_image = (
    modal.Image.debian_slim(python_version="3.12")
    # python-dotenv is required: `modal deploy` imports modal_app.py
    # locally in this container, and the backend's
    # modal.Secret.from_dotenv() needs it to read the .env we write
    # (without it the deploy dies immediately with "Need the `dotenv`
    # package installed"). Same lesson as the ReVanced helper, which
    # needed httpx for the same reason.
    .pip_install("modal", "fastapi", "anyio", "httpx", "python-dotenv")
    .add_local_file(
        _REPO_ROOT / "modal_app.py",
        "/root/karaokegen/modal_app.py",
    )
    .add_local_dir(
        _REPO_ROOT / "KaraokeGen",
        "/root/karaokegen/KaraokeGen",
    )
)

_URL_RE = re.compile(
    r"https://[a-z0-9-]+--karaokegen-api[a-z0-9.-]*\.modal\.run"
)

# Fallback: the deploy URL is deterministic —
# https://<workspace>--karaokegen-api.us-east.modal.run — and the
# "View Deployment" line prints the workspace on every successful
# deploy, even when the web URL itself is not re-printed (an
# update of an already-deployed app). Parse the workspace out of
# it so a successful deploy is never marked failed just because
# the URL line was missing from the output.
_VIEW_DEPLOY_RE = re.compile(
    r"modal\.com/apps/([a-z0-9-]+)/main/deployed/karaokengen"
)

# Job entries expire 2h after their last update (a max-length
# deploy runs ~30 min; 2h leaves time to poll the result). Expired
# entries are deleted lazily on status read — the Dict is never
# enumerated, so nothing else needs a cleanup pass.
JOB_TTL_SEC = 7200

# Modal's CLI output is rich-formatted with ANSI colour codes, which can
# land in the middle of a URL and break a plain regex. Strip them from
# every deploy line before matching.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes so regexes see the plain text."""
    return _ANSI_RE.sub("", text)


# Modal tokens look like ak-<id> / as-<secret>. Scrub by
# PATTERN, not by exact value: an error string or a wrapped CLI
# line may carry a token we never saw verbatim.
_TOKEN_RE = re.compile(r"\b(?:ak|as)-[A-Za-z0-9_-]+")


def _scrub(text: str) -> str:
    """Replace any Modal-token-shaped substring with ***."""
    return _TOKEN_RE.sub("***", text)


def _job_set(job_id: str, entry: dict) -> None:
    """Store a job entry, stamped so the TTL can expire it.

    The token hash (used for dedup) sticks: a progress update that
    doesn't carry it keeps the one already stored, so a repeat
    start can still find this running job.
    """
    entry = dict(entry)
    entry["ts"] = time.time()
    try:
        old = jobs.get(job_id)
    except Exception:
        old = None
    if isinstance(old, dict) and old.get("token_hash") \
            and not entry.get("token_hash"):
        entry["token_hash"] = old["token_hash"]
    jobs[job_id] = entry


def _lookup_job(job_id: str):
    """Return the job entry, or None if missing/expired.

    Expired entries are deleted so a stale job id stops resolving.
    The returned dict is a copy without the internal timestamp and
    without the token hash (which is only used server-side for
    dedup and must never be served to a caller).
    """
    entry = jobs.get(job_id)
    if entry is None:
        return None
    ts = entry.get("ts")
    if ts is not None and time.time() - ts > JOB_TTL_SEC:
        try:
            jobs.pop(job_id, None)
        except Exception:
            pass
        return None
    entry = dict(entry)
    entry.pop("ts", None)
    entry.pop("token_hash", None)
    return entry


def _token_hash(token_id: str) -> str:
    """One-way hash of a token id, for dedup only. The token itself
    is never stored; the hash lets us tell that two setup starts
    are for the same Modal account without keeping the token."""
    return hashlib.sha256(token_id.encode("utf-8")).hexdigest()[:16]


def _running_job_for_token(token_hash: str):
    """The job id of a still-running deploy for this token, if any.

    One running deploy per Modal account: a second start for the
    same token returns the existing job instead of spawning a
    duplicate deploy (double-tap, retry, or two phones). The job
    entry is scanned, not a separate index, so a stale 'running'
    entry ages out via its timestamp and can't wedge this forever.
    """
    try:
        items = list(jobs.items())
    except Exception:
        return None
    now = time.time()
    for job_id, entry in items:
        if not isinstance(entry, dict):
            continue
        if entry.get("state") != "running":
            continue
        if entry.get("token_hash") != token_hash:
            continue
        ts = entry.get("ts")
        if ts is not None and now - ts > JOB_TTL_SEC:
            continue
        return job_id
    return None


def _client_ip(request: Request) -> str:
    """Best-effort client IP for rate limiting.

    Modal's edge proxy APPENDS the real peer IP to X-Forwarded-For,
    so the RIGHTMOST entry is the one the client cannot forge: a
    client can prepend arbitrary entries, but cannot control what
    the proxy appends after them. (The leftmost entry is the
    client-claimed IP and is trivially spoofed, so it is NOT
    trusted.) If the proxy ever replaced the header instead of
    appending, there is a single entry and it is the real IP either
    way. Falls back to the direct peer.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
    client = getattr(request, "client", None)
    return client.host if client is not None else "unknown"


def _rate_allow(key: str, limit: int, window_sec: int) -> bool:
    """Fixed-window counter in the rate-limit Dict.

    True = allowed (and counted); False = limit hit (not counted).
    The window resets when it expires. Counts are approximate under
    concurrent requests (read-modify-write is not atomic) — a burst
    may admit one or two over the cap; the global cap + concurrency
    cap backstop that. A Dict error fails OPEN (the Dict being down
    breaks jobs too, so blocking all setup on it would only add a
    second failure mode).
    """
    now = time.time()
    try:
        entry = ratelimit.get(key)
    except Exception:
        return True
    if not isinstance(entry, dict) \
            or now - entry.get("window_start", 0) >= window_sec:
        entry = {"count": 1, "window_start": now}
    else:
        if entry.get("count", 0) >= limit:
            return False
        entry = {
            "count": entry.get("count", 0) + 1,
            "window_start": entry["window_start"],
        }
    try:
        ratelimit[key] = entry
    except Exception:
        pass
    return True


def _running_deploys() -> int:
    """Jobs whose state is still 'running' and not TTL-expired.

    Scanned from the jobs Dict (self-healing: a stale 'running'
    entry ages out via its timestamp, so a crashed deploy cannot
    wedge the cap forever). Missing timestamps count as running
    (conservative — a job with no timestamp is treated as live).
    """
    count = 0
    try:
        items = list(jobs.items())
    except Exception as exc:  # noqa: BLE001 - fail open, logged
        print(f"setup: could not list jobs for concurrency cap: {exc}",
              flush=True)
        return 0
    now = time.time()
    for _job_id, entry in items:
        if not isinstance(entry, dict):
            continue
        if entry.get("state") != "running":
            continue
        ts = entry.get("ts")
        if ts is not None and now - ts > JOB_TTL_SEC:
            continue
        count += 1
    return count


_AUTH_HINTS = (
    "unauthorized", "unauthenticated", "invalid token",
    "not authorized", "authentication", "permission denied",
    "invalid_token",
)


def _validate_token(token_id: str, token_secret: str):
    """Check the caller's Modal token with `modal token info`.

    Returns (True, None) if Modal accepts the token, else
    (False, plain error message). One read-only control-plane call
    — no compute is billed and nothing is deployed. Runs the Modal
    CLI in a subprocess with the caller's token only in env, the
    same isolation run_deploy uses (MODAL_IS_REMOTE removed, fresh
    config path). A non-zero exit or a timeout fails CLOSED: the
    caller retries, and no deploy is ever spawned on an unverified
    token. The CLI's stderr is classified, never returned — it must
    never reach the caller.
    """
    env = dict(os.environ)
    env["MODAL_TOKEN_ID"] = token_id
    env["MODAL_TOKEN_SECRET"] = token_secret
    env.pop("MODAL_IS_REMOTE", None)
    config_path = f"/tmp/modal-config-validate-{uuid.uuid4().hex[:12]}.toml"
    try:
        os.remove(config_path)
    except OSError:
        pass
    env["MODAL_CONFIG_PATH"] = config_path
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "modal", "token", "info"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=VALIDATE_TIMEOUT_SEC, env=env,
        )
    except subprocess.TimeoutExpired:
        return False, (
            "Could not verify this token — the check timed out. "
            "Try again."
        )
    except Exception:
        return False, "Could not verify this token — try again."
    finally:
        try:
            os.remove(config_path)
        except OSError:
            pass
    if proc.returncode == 0:
        return True, None
    low = _scrub(proc.stderr or "").lower()
    if any(hint in low for hint in _AUTH_HINTS):
        return False, (
            "This Modal token was rejected. Check the token and "
            "try again."
        )
    return False, "Could not verify this token — try again in a moment."


_LYRICS_ALIGN_URL = (
    "https://github.com/jhuang448/LyricsAlignment-Multilingual"
    "/archive/refs/heads/main.zip"
)


def _ensure_lyrics_align(deploy_dir: Path) -> None:
    """Fetch LyricsAlignment-Multilingual if it is not already there.

    modal_app.py bakes this directory into the backend image, so it
    must exist before `modal deploy` runs. Same zip the desktop app
    fetches. Extracted with a ZipSlip guard.
    """
    target = deploy_dir / "LyricsAlignment-Multilingual"
    if target.is_dir():
        return
    zip_path = deploy_dir / "lyrics-align.zip"
    req = urllib.request.Request(
        _LYRICS_ALIGN_URL, headers={"User-Agent": "karaokegen-setup/1.0"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        zip_path.write_bytes(resp.read())
    base = deploy_dir.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            dest = (base / member).resolve()
            if base not in dest.parents and dest != base:
                raise RuntimeError("unsafe zip entry")
        zf.extractall(deploy_dir)
    zip_path.unlink(missing_ok=True)
    # The zip contains a folder with a -main suffix; rename it.
    for cand in deploy_dir.glob("LyricsAlignment-Multilingual*"):
        if cand.is_dir() and cand.name != "LyricsAlignment-Multilingual":
            cand.rename(target)
            break
    if not target.is_dir():
        raise RuntimeError("LyricsAlignment-Multilingual not found after extract")


@setup_app.function(image=_image, timeout=1800)
def run_deploy(job_id: str, token_id: str, token_secret: str,
               token_hash: str) -> None:
    """Run `modal deploy` with the caller's token; record result.

    The token lives only in this call's arguments + the subprocess
    environment. Output is scrubbed of the token before recording.
    A fresh random API key is generated for this backend and written
    to a .env next to modal_app.py, so the backend's
    modal.Secret.from_dotenv() picks it up — the key is never the
    caller's Modal token. The token hash is stored in the job entry
    (never served to callers) so a repeat start for the same account
    can find this job instead of spawning a duplicate deploy.
    """
    api_key = secrets.token_hex(16)
    deploy_dir = Path("/root/karaokegen")
    _job_set(job_id, {
        "state": "running", "stage": "Starting deploy", "progress": 3,
        "token_hash": token_hash,
    })
    try:
        env = dict(os.environ)
        env["MODAL_TOKEN_ID"] = token_id
        env["MODAL_TOKEN_SECRET"] = token_secret
        # CRITICAL: inside Modal containers the client IGNORES
        # token credentials entirely (_is_remote: "credential
        # settings are ignored inside containers") and deploys
        # as the WORKER's workspace — a fake token happily
        # deployed into the helper owner's account. Removing
        # MODAL_IS_REMOTE makes the CLI authenticate with the
        # caller's token exactly like a laptop would. Isolate
        # the config path so no ambient profile interferes.
        env.pop("MODAL_IS_REMOTE", None)
        config_path = f"/tmp/modal-config-{job_id}.toml"
        try:
            os.remove(config_path)
        except OSError:
            pass
        env["MODAL_CONFIG_PATH"] = config_path
        # The backend's API key: a fresh random key in a .env next
        # to modal_app.py, read by modal.Secret.from_dotenv() at
        # deploy time. Never the caller's Modal token.
        (deploy_dir / ".env").write_text(
            f"MODAL_API_KEY={api_key}\n", encoding="utf-8"
        )
        # The backend image needs LyricsAlignment present.
        _job_set(job_id, {
            "state": "running",
            "stage": "Fetching word-alignment tools",
            "progress": 5,
        })
        _ensure_lyrics_align(deploy_dir)
        # Stream CLI output line-by-line so the phone shows REAL
        # deploy stages (image build is the long pole), not a
        # blind spinner. Output is token-scrubbed before any use.
        proc = subprocess.Popen(
            [sys.executable, "-m", "modal", "deploy",
             str(deploy_dir / "modal_app.py")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env, cwd=str(deploy_dir),
        )
        stage = "Deploying to your Modal account"
        progress = 8
        url_found = None
        workspace_found = None
        tail: list = []
        # 30-min function timeout (timeout=1800 above): the internal
        # deadline leaves 150s for reaping the child + cleanup.
        deadline = time.monotonic() + 1650
        assert proc.stdout is not None
        # Read stdout on a pump thread so a quiet subprocess
        # cannot block the deadline check forever (readline blocks
        # until a line arrives; the old loop only checked the
        # deadline after each line).
        line_queue: queue.Queue = queue.Queue()

        def _pump_stdout():
            try:
                for raw_line in proc.stdout:
                    line_queue.put(raw_line)
            except Exception:
                pass
            finally:
                line_queue.put(None)

        pump = threading.Thread(target=_pump_stdout, daemon=True)
        pump.start()
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    proc.kill()
                    raise subprocess.TimeoutExpired(proc.args, 1650)
                try:
                    raw_line = line_queue.get(timeout=min(remaining, 5))
                except queue.Empty:
                    continue
                if raw_line is None:
                    break
                # Strip ANSI colour codes first (they can split a URL
                # in the middle and break the regex), then scrub tokens.
                line = _strip_ansi(_scrub(raw_line))
                tail.append(line.rstrip())
                tail = tail[-5:]
                low = line.lower()
                if "building image" in low or "building" in low:
                    stage = "Building server image (AI models included)"
                    progress = max(progress, 25)
                elif "pip install" in low or "run python" in low:
                    stage = "Installing AI models into the image"
                    progress = max(progress, 40)
                elif "pushing" in low or "uploading" in low:
                    stage = "Uploading server image"
                    progress = max(progress, 60)
                elif "creating" in low or "created" in low:
                    stage = "Creating your server app"
                    progress = max(progress, 80)
                match = _URL_RE.search(line)
                if match:
                    url_found = match.group(0)
                    stage = "Server address created"
                    progress = max(progress, 92)
                # The workspace, from the "View Deployment" line —
                # used to build the URL if the URL line itself
                # never appears (an update of an existing app).
                m_ws = _VIEW_DEPLOY_RE.search(line)
                if m_ws:
                    workspace_found = m_ws.group(1)
                _job_set(job_id, {
                    "state": "running", "stage": stage,
                    "progress": progress,
                })
        finally:
            # Always reap the child — wait() after kill(), never
            # a zombie if the loop exits early. The token-bearing
            # config file is removed even on failure.
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            try:
                os.remove(config_path)
            except OSError:
                pass
            try:
                (deploy_dir / ".env").unlink(missing_ok=True)
            except OSError:
                pass
        returncode = proc.returncode
        # If the deploy succeeded but the URL line never appeared
        # (an update of an already-deployed app), build the URL
        # from the workspace in the "View Deployment" line. The
        # URL shape is fixed, so this is exact, not a guess.
        if returncode == 0 and not url_found and workspace_found:
            url_found = (
                f"https://{workspace_found}"
                "--karaokegen-api.us-east.modal.run"
            )
        if returncode == 0 and url_found:
            _job_set(job_id, {
                "state": "done", "stage": "Deployed",
                "progress": 100, "url": url_found,
                "api_key": api_key,
            })
            print(f"setup {job_id}: deployed {url_found}", flush=True)
        else:
            # The poller gets a generic failure only — the CLI
            # tail (paths, internals) stays server-side, in the log.
            _job_set(job_id, {
                "state": "error", "stage": "Deploy failed",
                "progress": progress,
                "error": "deploy failed",
            })
            print(
                f"setup {job_id}: deploy failed rc={returncode} "
                f"tail={' | '.join(tail)}",
                flush=True,
            )
    except subprocess.TimeoutExpired:
        _job_set(job_id, {"state": "error",
                          "error": "deploy timed out after ~30 minutes — "
                                   "try again (image layers already built "
                                   "are cached, so a retry is usually "
                                   "fast)"})
    except Exception as exc:  # noqa: BLE001 - reported to the caller
        # Scrub the exception text too — a Modal client error
        # may echo the token, and this record is served to anyone
        # holding the job id.
        _job_set(job_id, {"state": "error",
                          "error": _scrub(
                              f"{type(exc).__name__}: {exc}"
                          )[:400]})


@setup_app.function(image=_image)
@modal.asgi_app()
def api():
    from fastapi import FastAPI

    web = FastAPI()

    @web.get("/health")
    def health():
        return {"status": "ok", "service": "karaokegen-setup"}

    @web.post("/setup/start")
    def start(request: Request, req: StartRequest):
        # Guards run first, cheapest first: a flood hits 429s without
        # ever reaching the token check or a deploy spawn. Each limit
        # has its own HTTP code + plain message so the phone (and a
        # live smoke check) can tell them apart.
        ip = _client_ip(request)
        if not _rate_allow(
                f"start:ip:{ip}", STARTS_PER_IP_PER_HOUR, 3600):
            return JSONResponse(
                status_code=429,
                content={"state": "error",
                         "error": "Too many setup attempts from this "
                                  "address — try again in an hour."})
        if not _rate_allow(
                "start:global", STARTS_GLOBAL_PER_DAY, 86400):
            return JSONResponse(
                status_code=429,
                content={"state": "error",
                         "error": "The setup helper has reached its "
                                  "daily limit — try again tomorrow."})
        if _running_deploys() >= MAX_CONCURRENT_DEPLOYS:
            return JSONResponse(
                status_code=429,
                content={"state": "error",
                         "error": "The setup helper is already running "
                                  "2 deploys — try again in a few "
                                  "minutes."})
        token_id = req.token_id.strip()
        token_secret = req.token_secret.strip()
        if not token_id.startswith("ak-") \
                or not token_secret.startswith("as-"):
            return JSONResponse(
                status_code=400,
                content={"state": "error",
                         "error": "That does not look like a Modal API "
                                  "token (Token ID starts ak-, Secret "
                                  "starts as-)."})
        # Token pre-validation: a junk token is rejected here, in
        # seconds, with no deploy billed. Nothing below this line
        # runs on an unverified token.
        valid, err = _validate_token(token_id, token_secret)
        if not valid:
            return JSONResponse(
                status_code=400,
                content={"state": "error", "error": err})
        # Dedup: one running deploy per Modal account. A repeat start
        # for the same token (double-tap, retry, second phone) gets
        # the existing job back instead of spawning a duplicate
        # deploy that would bill twice and race the first one.
        token_hash = _token_hash(token_id)
        existing = _running_job_for_token(token_hash)
        if existing is not None:
            return {"job_id": existing, "state": "running",
                    "resumed": True}
        job_id = uuid.uuid4().hex[:16]
        _job_set(job_id, {
            "state": "running", "stage": "Queued", "progress": 1,
            "token_hash": token_hash,
        })
        run_deploy.spawn(job_id, token_id, token_secret, token_hash)
        return {"job_id": job_id, "state": "running"}

    @web.get("/setup/status/{job_id}")
    def status(request: Request, job_id: str):
        ip = _client_ip(request)
        if not _rate_allow(
                f"status:ip:{ip}", STATUS_PER_IP_PER_MIN, 60):
            # 429 with state "running" on purpose: the phone polls
            # this endpoint every 4s, and its poller fails the whole
            # setup on any other state. A rate-limited read leaks no
            # URL and no job state — the poller just retries in 4s.
            return JSONResponse(
                status_code=429,
                content={"state": "running",
                         "stage": "Slow down — retrying...",
                         "progress": -1})
        entry = _lookup_job(job_id)
        if entry is None:
            return {"state": "unknown"}
        return entry

    return web
