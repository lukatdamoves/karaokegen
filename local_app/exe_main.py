"""KaraokeGen desktop launcher (PyInstaller exe entry point).

Runs the existing FastAPI editor (local_app.server:app) on 127.0.0.1 and
opens it in a native pywebview window. Falls back to the default browser
when pywebview is not installed (dev machines without the exe extras).

No pipeline logic lives here — this is only process/window plumbing so the
whole product ships as a single KaraokeGen.exe.
"""
from __future__ import annotations

import os
import shutil
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path


HOST = "127.0.0.1"
PORT = 8765
APP_TITLE = "KaraokeGen"


def _exe_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _appdata_dir() -> Path:
    for k in ("LOCALAPPDATA", "APPDATA"):
        v = os.environ.get(k)
        if v:
            return Path(v) / "KaraokeGen"
    return Path.home() / ".karaokegen"


def _ensure_appdata() -> Path:
    p = _appdata_dir()
    p.mkdir(parents=True, exist_ok=True)
    (p / "cache").mkdir(exist_ok=True)
    return p


def _ensure_ffmpeg_on_path() -> str | None:
    """Bundled ffmpeg (extracted to AppData) > beside-exe > system PATH."""
    # 1) bundled inside the PyInstaller _MEIPASS
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        cand = Path(sys._MEIPASS) / "ffmpeg.exe"  # type: ignore[attr-defined]
        if cand.exists():
            # copy to AppData so Defender only scans once and future runs
            # don't need to unpack the whole bundle for ffmpeg
            dst = _ensure_appdata() / "ffmpeg.exe"
            try:
                if not dst.exists() or dst.stat().st_size != cand.stat().st_size:
                    shutil.copy2(cand, dst)
            except Exception:
                dst = cand
            os.environ["PATH"] = str(dst.parent) + os.pathsep + os.environ.get("PATH", "")
            return str(dst)
    # 2) beside the exe (dev / manual placement)
    exe_dir = _exe_dir()
    for cand in (exe_dir / "ffmpeg.exe", exe_dir / "bin" / "ffmpeg.exe",
                 _appdata_dir() / "ffmpeg.exe"):
        if cand.exists():
            os.environ["PATH"] = str(cand.parent) + os.pathsep + os.environ.get("PATH", "")
            return str(cand)
    return shutil.which("ffmpeg")


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((HOST, port)) != 0


def _wait_for_server(port: int, timeout_s: float = 60.0) -> bool:
    # Generous: OneFile cold start unpacks ~40 MB + Defender scans it, which
    # can take well over 20 s on a first launch. False-timeout here used to
    # kill the app before the server finished booting.
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex((HOST, port)) == 0:
                return True
        time.sleep(0.25)
    return False


def _boot_log(msg: str) -> None:
    """Append one line to the AppData boot log — the only evidence when the
    windowed exe dies silently. Never raises."""
    try:
        p = _appdata_dir() / "app.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.stat().st_size > 200 * 1024:
            p.write_text("", encoding="utf-8")
        import datetime
        with open(p, "a", encoding="utf-8") as f:
            f.write(datetime.datetime.now().strftime("%H:%M:%S") + " " + msg + "\n")
    except Exception:
        pass


def _fatal(msg: str) -> None:
    _boot_log("FATAL: " + msg)
    print(msg, file=sys.stderr)
    try:
        import tkinter.messagebox as mb
        import tkinter as tk
        r = tk.Tk()
        r.withdraw()
        mb.showerror(APP_TITLE, msg)
        r.destroy()
    except Exception:
        pass


def main() -> int:
    # Windowed PyInstaller exe has no console — sys.stdout is None, which makes
    # uvicorn's default logging (isatty check) crash on boot. Patch before anything else.
    if getattr(sys, "frozen", False):
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")  # type: ignore[assignment]
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")  # type: ignore[assignment]
    _boot_log("=== launch frozen=%s meipass=%s ===" % (
        getattr(sys, "frozen", False),
        getattr(sys, "_MEIPASS", "-") if getattr(sys, "frozen", False) else "-"))
    try:
        _ensure_appdata()
        _boot_log("appdata ok: " + str(_appdata_dir()))
        ffmpeg = _ensure_ffmpeg_on_path()
        _boot_log("ffmpeg: " + str(ffmpeg))
        if not ffmpeg:
            _fatal("ffmpeg not found. The bundled ffmpeg failed to extract — "
                   "try placing ffmpeg.exe next to KaraokeGen.exe or on PATH.")
            return 1

        if not _port_free(PORT):
            # Another instance (or dev uvicorn) already owns the port — just open it.
            _boot_log("port busy, opening window only")
            _open_window(f"http://{HOST}:{PORT}")
            return 0

        import uvicorn

        # Direct import (not a "local_app.server:app" string): PyInstaller traces
        # this statically, so the whole FastAPI app is guaranteed in the bundle.
        from local_app.server import app as fastapi_app
        _boot_log("imports ok")

        config = uvicorn.Config(
            fastapi_app,
            host=HOST,
            port=PORT,
            log_level="warning",
            access_log=False,
            log_config=None,
            reload=False,
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True, name="kvg-uvicorn")
        thread.start()
        _boot_log("server thread started")

        if not _wait_for_server(PORT):
            server.should_exit = True
            _fatal("Server failed to start on port %d." % PORT)
            return 1
        _boot_log("server up, opening window")

        _open_window(f"http://{HOST}:{PORT}")
        _boot_log("window closed, exiting")
        server.should_exit = True
        return 0
    except BaseException as exc:
        import traceback
        _boot_log("EXCEPTION: %r\n%s" % (exc, traceback.format_exc()))
        _fatal("Startup failed: %r (see app.log in %s)" % (exc, _appdata_dir()))
        return 1


class _JsApi:
    """Exposed to the editor JS as window.pywebview.api.* (exe only).

    saveFile shows a native Save-As dialog and downloads the given local
    URL path (e.g. /api/file/<id>) to the chosen location. window.location
    navigations can't trigger a save prompt inside WebView2, so downloads
    must go through here or they silently fail.
    """

    def saveFile(self, url_path: str, filename: str):
        try:
            import webview  # type: ignore

            # Allowlist: only local API download paths. Without this, a
            # compromised page in the webview could make the host fetch an
            # arbitrary URL and write the response to disk.
            p = str(url_path or "")
            if p.startswith("http"):
                from urllib.parse import urlparse as _up
                try:
                    _u = _up(p)
                    if _u.hostname not in ("127.0.0.1", "localhost", "::1"):
                        return {"ok": False, "error": "URL not allowed"}
                    if _u.port not in (None, PORT):
                        return {"ok": False, "error": "URL not allowed"}
                    p = _u.path + (("?" + _u.query) if _u.query else "")
                except Exception:
                    return {"ok": False, "error": "bad URL"}
            if not p.startswith("/api/"):
                return {"ok": False, "error": "URL not allowed"}
            safe_name = "".join(
                c for c in (filename or "karaoke.mp4") if c.isalnum() or c in "._-") or "karaoke.mp4"
            wins = getattr(webview, "windows", None) or []
            win = wins[0] if wins else None
            if win is None:
                return {"ok": False, "error": "no window"}
            dest = win.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=safe_name,
                file_types=("Video (*.mp4)",),
            )
            if not dest:
                return {"ok": False, "cancelled": True}
            path = dest[0] if isinstance(dest, (list, tuple)) else dest
            full = f"http://{HOST}:{PORT}{p}"
            _boot_log("saveFile: %s -> %s" % (full, path))
            import urllib.request

            try:
                with urllib.request.urlopen(full, timeout=600) as resp, \
                        open(path, "wb") as out:
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
            except Exception as exc:
                return {"ok": False, "error": str(exc)[:500]}
            _boot_log("saveFile done: " + str(path))
            return {"ok": True, "path": str(path)}
        except Exception as exc:
            _boot_log("saveFile failed: %r" % (exc,))
            return {"ok": False, "error": str(exc)[:500]}


def _open_window(url: str) -> None:
    try:
        import webview  # type: ignore

        webview.create_window(APP_TITLE, url, width=1280, height=860,
                              js_api=_JsApi())
        webview.start()
    except Exception as _wv_exc:
        _boot_log(f"webview failed ({_wv_exc!r}) — falling back to browser")
        print(f"pywebview failed ({_wv_exc!r}) — opening in browser: {url}")
        webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
