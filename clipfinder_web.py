#!/usr/bin/env python3
"""clipfinder web - the point-and-click front end for clipfinder.py.

Run it, and a page opens in your browser. Everything happens on your own computer:
the server listens on 127.0.0.1 only, nothing is sent anywhere, and there is no account.

    python clipfinder_web.py

Safety measures (each one is covered by a test in tests/test_web.py):
  * listens on 127.0.0.1 only; a random access token is needed (sent once in the link, then kept in a cookie)
  * the Host header must be 127.0.0.1 / localhost with our port (blocks DNS-rebinding)
  * browsers that send Sec-Fetch-Site must say same-origin (blocks other localhost pages)
  * every change (POST / PUT) also needs the right Origin and a custom X-CF header (blocks cross-site forms)
  * no CORS headers are ever sent; a strict Content-Security-Policy is set on the page
  * every value from the page is checked (ids are 12 hex digits, numbers are finite and in range, paths must be
    real video files); files are served only from fixed places (the page's own 3 files, the chosen video,
    the clips folder), never from a path the page supplies
  * ffmpeg / yt-dlp are started with argument lists, never through a shell
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import hmac
import http.cookies
import importlib.util
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import clipfinder as cf

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".m4v", ".ts", ".avi", ".flv"}
MIME = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska",
        ".mov": "video/quicktime", ".ts": "video/mp2t", ".avi": "video/x-msvideo", ".flv": "video/x-flv"}
MAX_JSON = 64 * 1024
MAX_UPLOAD = 60 * 1024 ** 3
FROZEN = bool(getattr(sys, "frozen", False))                 # True inside the packaged program (PyInstaller)
BASE_DIR = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8")}
ID_RE = re.compile(r"[0-9a-f]{12}")
COOKIE = "cf_session"
CSP = ("default-src 'none'; img-src 'self' data:; media-src 'self'; connect-src 'self'; script-src 'self'; "
       "style-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
PRESET_IDS = list(cf.PRESETS)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(message or code)
        self.status, self.code, self.message = status, code, message or code


def explain(msg: str) -> str:
    """A short machine-readable reason, so the page can show a friendly Malayalam message."""
    m = msg.lower()
    if "was not found on path" in m:
        return "no_ytdlp" if "yt-dlp" in m else "no_ffmpeg"
    if "only https links" in m:
        return "bad_link"
    if "shorter than 10 seconds" in m:
        return "too_short"
    if "ffprobe could not read" in m or "no video stream" in m or "does not say how long" in m \
            or "could not decode any video frames" in m:
        return "unreadable"
    if "yt-dlp could not download" in m:
        return "download_failed"
    if "source video is missing" in m or "video file not found" in m:
        return "source_missing"
    if "report is from run" in m:
        return "stale"
    return "other"


# --------------------------------------------------------------------------- storage

class Store:
    def __init__(self, root: str):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.videos = os.path.join(self.root, "videos")
        self.analyses = os.path.join(self.root, "analyses")
        cf.make_dir(self.videos)
        cf.make_dir(self.analyses)
        cutoff = time.time() - 86400                  # leftovers of an interrupted upload
        for name in os.listdir(self.videos):
            f = os.path.join(self.videos, name)
            if name.startswith(".upload-") and name.endswith(".part") and os.path.getmtime(f) < cutoff:
                try:
                    os.remove(f)
                except OSError:
                    pass

    @staticmethod
    def make_id(kind: str, source: str) -> str:
        return hashlib.sha1(f"{kind}|{source}".encode("utf-8", "replace"), usedforsecurity=False).hexdigest()[:12]

    def adir(self, aid: str) -> str:
        if not isinstance(aid, str) or not ID_RE.fullmatch(aid):
            raise ApiError(404, "not_found")
        return os.path.join(self.analyses, aid)

    def read_meta(self, aid: str) -> dict:
        path = os.path.join(self.adir(aid), "meta.json")
        try:
            with open(path, encoding="utf-8") as fh:
                meta = json.load(fh)
            if not isinstance(meta, dict) or not isinstance(meta.get("name"), str):
                raise ValueError
            return meta
        except (OSError, ValueError):
            raise ApiError(404, "not_found")

    def write_meta(self, aid: str, meta: dict) -> None:
        cf.atomic_write(os.path.join(self.adir(aid), "meta.json"), "w",
                        lambda fh: json.dump(meta, fh, ensure_ascii=False, indent=2), encoding="utf-8")

    def clips_dir(self, aid: str) -> str:
        return os.path.join(self.adir(aid), "clips")

    def made(self, aid: str) -> list:
        d = self.clips_dir(aid)
        out = []
        if os.path.isdir(d):
            for name in os.listdir(d):
                f = os.path.join(d, name)
                if name.endswith(".mp4") and os.path.isfile(f):
                    st = os.stat(f)
                    out.append({"file": name, "size": st.st_size, "mtime": st.st_mtime})
        out.sort(key=lambda x: -x["mtime"])
        return out

    def load(self, aid: str) -> dict:
        try:
            return cf.load_analysis(self.adir(aid))
        except cf.ClipError:
            raise ApiError(404, "damaged", "the saved analysis is damaged - analyse the video again")


def peak_time(c: dict) -> float:
    ev = c.get("events") or []
    if ev:
        return float(max(ev, key=lambda e: e.get("score", 0)).get("t", (c["start"] + c["end"]) / 2))
    return (c["start"] + c["end"]) / 2


def level_of(score: float) -> str:
    return "high" if score >= 0.6 else "mid" if score >= 0.4 else "low"


def make_params(preset: str, sensitivity: int) -> cf.Params:
    if preset not in cf.PRESETS:
        raise ApiError(400, "bad_request", "unknown preset")
    p = cf.Params()
    cf.apply_preset(p, preset)
    p.z_min = round(1.0 + sensitivity * 0.02, 3)           # 0 -> 1.0 (more clips) ... 100 -> 3.0 (stricter)
    p.min_score = round(0.1 + 0.3 * sensitivity / 100, 3)  # 50 -> the engine defaults (2.0 / 0.25)
    p.tag_min = min(0.25, p.min_score)
    return p


# --------------------------------------------------------------------------- background job (one at a time)

class FileLock:
    """A lock that lives in a file, so that the web page and the Claude connector (two separate programs sharing one data
    folder) never run two jobs at once. The system frees it by itself when a program ends, so it can never stay stuck."""

    def __init__(self, path: str):
        self.path, self.fh = path, None

    def acquire(self) -> bool:
        """True = we hold it. False = another program holds it. If locking itself is impossible, work is not blocked."""
        try:
            fh = open(self.path, "a+b")
        except OSError:
            return True
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            return exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK)
        self.fh = fh
        return True

    def release(self) -> None:
        fh, self.fh = self.fh, None
        if fh:
            try:
                fh.close()                                  # closing frees the lock on every system
            except OSError:
                pass


class Jobs:
    def __init__(self, lockfile: str | None = None):
        self.lock = threading.Lock()
        self.cur = None
        self.counter = 0
        self.flock = FileLock(lockfile) if lockfile else None

    def start(self, kind: str, aid: str, fn) -> dict:
        with self.lock:
            if self.cur and self.cur["state"] == "running":
                raise ApiError(409, "busy", "another job is running")
            if self.flock and not self.flock.acquire():
                raise ApiError(409, "busy", "another ClipFinder program is running a job")
            self.counter += 1
            cf.CANCEL.clear()
            job = {"id": self.counter, "kind": kind, "analysis": aid, "state": "running", "stage": "start",
                   "frac": None, "t0": time.time(), "stage_t0": time.time(), "message": "", "code": "",
                   "result": None}
            self.cur = job
        try:
            threading.Thread(target=self._run, args=(job, fn), daemon=True).start()
        except BaseException:
            if self.flock:
                self.flock.release()
            raise
        return job

    def _update(self, job, stage, frac):
        if stage != job["stage"]:
            job["stage"], job["stage_t0"] = stage, time.time()
        job["frac"] = frac

    def _run(self, job, fn):
        try:
            job["result"] = fn(lambda stage, frac=None: self._update(job, stage, frac))
            job["state"] = "done"
        except cf.Cancelled:
            job["state"] = "cancelled"
        except cf.ClipError as exc:
            job["state"], job["message"], job["code"] = "error", exc.message, explain(exc.message)
        except ApiError as exc:
            job["state"], job["message"], job["code"] = "error", exc.message, exc.code
        except Exception:                                   # a bug: log it for the developer, tell the user little
            traceback.print_exc(file=sys.stderr)
            job["state"], job["message"], job["code"] = "error", "unexpected error", "internal"
        finally:
            job["t1"] = time.time()
            if self.flock:
                self.flock.release()

    def view(self):
        job = self.cur
        if not job:
            return None
        now = job.get("t1", time.time())
        v = {k: job[k] for k in ("id", "kind", "analysis", "state", "stage", "frac", "message", "code")}
        v["elapsed"] = round(now - job["t0"], 1)
        frac = job["frac"]
        v["eta"] = None
        if job["state"] == "running" and job["stage"] == "read" and isinstance(frac, float) and frac > 0.03:
            v["eta"] = round((time.time() - job["stage_t0"]) * (1 - frac) / frac)
        if job["state"] == "done":
            v["result"] = job["result"]
        return v

    def cancel(self) -> bool:
        job = self.cur
        if job and job["state"] == "running":
            cf.CANCEL.set()
            return True
        return False


# --------------------------------------------------------------------------- things the server can do for the page

def pick_file_here() -> int:
    """The file dialog itself (run in a separate process by default_picker, because dialogs need their own main thread).
    Prints the chosen path; exit code 3 means tkinter is not available."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return 3
    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    path = filedialog.askopenfilename(title="Video", filetypes=[
        ("Video", "*.mp4 *.mkv *.mov *.webm *.m4v *.ts *.avi *.flv"), ("All", "*.*")])
    root.destroy()
    sys.stdout.buffer.write((path or "").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


def default_picker():
    """Open the operating system's file dialog (needs tkinter). Returns a path, '' if cancelled, None if unavailable."""
    if FROZEN:
        cmd = [sys.executable, "--pick-file"]
    else:
        cmd = [sys.executable, "-E", "-s", os.path.abspath(__file__), "--pick-file"]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=600, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    return res.stdout.decode("utf-8", "replace").strip()


def default_open_folder(path: str) -> bool:
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)                               # type: ignore[attr-defined]
        else:
            tool = "open" if sys.platform == "darwin" else "xdg-open"
            if shutil.which(tool) is None:
                return False
            subprocess.Popen([tool, path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def add_bundled_tools() -> list:
    """ffmpeg, ffprobe and yt-dlp that travel with the program (a 'tools' folder next to it) are used before any others."""
    dirs = []
    if FROZEN:
        dirs.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "tools"))
    dirs.append(os.path.join(BASE_DIR, "tools"))
    found = [d for d in dirs if os.path.isdir(d)]
    if found:
        os.environ["PATH"] = os.pathsep.join(found + [os.environ.get("PATH", "")])
    if os.name == "nt":
        os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"    # Windows must not run look-alike programs from the current folder
    return found


def ytdlp_version() -> str | None:
    """Version string of the yt-dlp that would be used, or None if it is missing / does not answer."""
    exe = shutil.which("yt-dlp")
    if not exe:
        return None
    try:
        res = subprocess.run([exe, "--version"], capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = res.stdout.decode("utf-8", "replace").strip().splitlines()
    if res.returncode != 0 or not lines or not re.fullmatch(r"[0-9A-Za-z._+-]{1,40}", lines[0].strip()):
        return None
    return lines[0].strip()


def default_update_ytdlp() -> dict:
    """Ask yt-dlp to update itself (works for the standalone program); a pip-installed one is updated with pip.
    Only fixed commands are run - nothing from the page ends up in them.
    "ok" is true only if one of the commands really finished with success."""
    exe = shutil.which("yt-dlp")
    if not exe:
        raise ApiError(400, "no_ytdlp")
    before = ytdlp_version()
    commands = [([exe, "-U"], 120)]
    if not FROZEN:
        commands.append(([sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"], 180))
    succeeded = False
    for cmd, limit in commands:
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=limit, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if res.returncode == 0:
            succeeded = True
            break
    after = ytdlp_version()
    return {"ok": succeeded and after is not None, "before": before, "after": after,
            "changed": after is not None and after != before}


def find_app_browser() -> str | None:
    """A Chrome-family browser that can open the page as a plain app window (no tabs, no address bar)."""
    cands = []
    if os.name == "nt":
        for var in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData"):
            base = os.environ.get(var)
            if base:
                cands.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
        for var in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
            base = os.environ.get(var)
            if base:
                cands.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
    elif sys.platform == "darwin":
        cands = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                 "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    else:
        cands = [shutil.which(n) for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                                           "microsoft-edge")]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def open_ui(url: str, window: bool = True) -> str:
    """Show the page. Preferably as its own small window; otherwise in the normal browser. Returns which one was used."""
    if window:
        exe = find_app_browser()
        if exe:
            try:
                subprocess.Popen([exe, f"--app={url}", "--window-size=1320,900"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return "window"
            except OSError:
                pass
    webbrowser.open(url)
    return "browser"


def check_video_path(raw) -> str:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 4096 or "\x00" in raw:
        raise ApiError(400, "bad_path")
    path = os.path.realpath(os.path.expanduser(raw.strip().strip('"').strip("'")))
    if not os.path.isfile(path):
        raise ApiError(400, "file_missing")
    if os.path.splitext(path)[1].lower() not in VIDEO_EXT:
        raise ApiError(400, "bad_type")
    return path


REMOTE_HOST_RE = re.compile(r"(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+ts\.net")
PROXY_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "Forwarded", "Tailscale-User-Login")


def clean_remote_host(raw) -> str:
    """The name Tailscale gave this computer (like pc.tail1234.ts.net). Only *.ts.net names are accepted."""
    if not isinstance(raw, str):
        raise ApiError(400, "bad_host_name")
    name = raw.strip().lower().removeprefix("https://").rstrip("/")
    if not REMOTE_HOST_RE.fullmatch(name):
        raise ApiError(400, "bad_host_name")
    return name


def load_remote_host(root: str) -> str | None:
    try:
        with open(os.path.join(root, "remote.json"), encoding="utf-8") as fh:
            return clean_remote_host(json.load(fh).get("host"))
    except (OSError, ValueError, AttributeError, ApiError):
        return None


def save_remote_host(root: str, host: str | None) -> None:
    path = os.path.join(root, "remote.json")
    if not host:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    cf.atomic_write(path, "w", lambda fh: json.dump({"host": host}, fh), encoding="utf-8")


LANGS = ("ml", "en")


def load_lang(root: str) -> str:
    """The page language the user chose (saved in prefs.json in the data folder). Malayalam until changed."""
    try:
        with open(os.path.join(root, "prefs.json"), encoding="utf-8") as fh:
            lang = json.load(fh).get("lang")
        return lang if lang in LANGS else "ml"
    except (OSError, ValueError, AttributeError):
        return "ml"


def save_lang(root: str, lang) -> str:
    if lang not in LANGS:
        raise ApiError(400, "bad_lang")
    cf.atomic_write(os.path.join(root, "prefs.json"), "w", lambda fh: json.dump({"lang": lang}, fh), encoding="utf-8")
    return lang


def remote_token(root: str) -> str:
    """A secret that stays the same between runs, so the link saved on the other device keeps working.
    (Used only when remote access is on. The file is readable by the owner only, where the system supports that.)"""
    path = os.path.join(root, ".remote-token")
    try:
        with open(path, encoding="utf-8") as fh:
            tok = fh.read().strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{43}", tok):
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(tok)
    return tok


YT_ID = re.compile(r"(?:v=|youtu\.be/|/live/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")


class App:
    def __init__(self, store: Store, port: int, token: str, verbose: bool = False):
        self.store, self.port, self.token, self.verbose = store, port, token, verbose
        self.jobs = Jobs(os.path.join(store.root, ".job.lock"))
        self.thumb_slots = threading.BoundedSemaphore(3)
        self.picker = default_picker
        self.open_folder = default_open_folder
        self.updater = default_update_ytdlp
        self.update_lock = threading.Lock()
        self.server = None
        self.locks = {}
        self.remote_host = None                                   # set by make_server when remote access is on

    # ---- views
    def tools(self) -> dict:
        return {"ffmpeg": shutil.which("ffmpeg") is not None, "ffprobe": shutil.which("ffprobe") is not None,
                "ytdlp": shutil.which("yt-dlp") is not None,
                "picker": importlib_has("tkinter")}

    def update_ytdlp(self) -> dict:
        if not self.update_lock.acquire(blocking=False):
            raise ApiError(409, "busy", "an update is already running")
        try:
            with self.jobs.lock:                                  # no new job may start while the program is replaced
                job = self.jobs.cur
                if job and job["state"] == "running":
                    raise ApiError(409, "busy", "another job is running")
                return self.updater()
        finally:
            self.update_lock.release()

    def need_space(self, gib: float = 2.0) -> None:
        """Refuse to start a job that writes files when the disk is nearly full."""
        try:
            free = shutil.disk_usage(self.store.root).free
        except OSError:
            return
        if free < gib * 1024 ** 3:
            raise ApiError(413, "no_space", "the disk is nearly full")

    def remote_info(self) -> dict:
        saved = load_remote_host(self.store.root)
        return {"host": saved, "active": self.remote_host, "port": self.port,
                "link": f"https://{self.remote_host}/?t={self.token}" if self.remote_host else None,
                "command": f"tailscale serve --bg {self.port}"}

    def set_remote(self, host) -> dict:
        save_remote_host(self.store.root, clean_remote_host(host) if host else None)
        return self.remote_info()

    def connector_info(self) -> dict:
        """The exact command Claude must run to start the connector (fixed by us; nothing from the page goes into it)."""
        args = ["--mcp", "--data", self.store.root]
        if not FROZEN:
            args.insert(0, os.path.abspath(__file__))
        return {"command": os.path.abspath(sys.executable), "args": args, "frozen": FROZEN}

    def test_connector(self) -> dict:
        """Start the connector exactly as Claude would, ask it for its tools, and stop it again."""
        info = self.connector_info()
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": "clipfinder-self-test", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        data = ("\n".join(json.dumps(m) for m in msgs) + "\n").encode()
        try:
            res = subprocess.run([info["command"], *info["args"]], input=data, capture_output=True, timeout=40)
        except (OSError, subprocess.TimeoutExpired):
            return {"ok": False, "tools": 0}
        replies = {}
        for line in res.stdout.splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                return {"ok": False, "tools": 0}                  # anything but protocol lines on stdout is a fault
            if isinstance(r, dict) and "id" in r:
                replies[r["id"]] = r
        tools = (replies.get(2, {}).get("result") or {}).get("tools")
        ok = bool(replies.get(1, {}).get("result")) and isinstance(tools, list) and len(tools) >= 7
        return {"ok": ok, "tools": len(tools) if isinstance(tools, list) else 0}

    def list_videos(self) -> list:
        out = []
        for name in os.listdir(self.store.videos):
            f = os.path.join(self.store.videos, name)
            if name.startswith(".") or not os.path.isfile(f) or os.path.splitext(name)[1].lower() not in VIDEO_EXT:
                continue
            real = os.path.realpath(f)
            aid = Store.make_id("file", real)
            st = os.stat(f)
            out.append({"name": name, "path": real, "size": st.st_size, "mtime": st.st_mtime,
                        "analysis": aid if os.path.isfile(os.path.join(self.store.analyses, aid, "meta.json")) else None})
        out.sort(key=lambda v: -v["mtime"])
        return out[:50]

    def library(self) -> list:
        items = []
        for aid in os.listdir(self.store.analyses):
            if not ID_RE.fullmatch(aid):
                continue
            try:
                meta = self.store.read_meta(aid)
                data = self.store.load(aid)
            except ApiError:
                continue
            items.append({"id": aid, "name": meta["name"], "clips": len(data["candidates"]),
                          "made": len(self.store.made(aid)), "duration": data["duration"],
                          "created": meta.get("created", ""), "mtime": os.path.getmtime(
                              os.path.join(self.store.analyses, aid, "meta.json"))})
        items.sort(key=lambda x: -x["mtime"])
        return items[:50]

    def state(self) -> dict:
        return {"version": cf.__version__, "data_dir": self.store.root, "video_dir": self.store.videos,
                "tools": self.tools(), "presets": PRESET_IDS, "videos": self.list_videos(),
                "library": self.library(), "job": self.jobs.view(),
                "detectors": {k: v[0] for k, v in cf.DETECTORS.items()}, "lang": load_lang(self.store.root)}

    def describe(self, aid: str) -> dict:
        meta = self.store.read_meta(aid)
        data = self.store.load(aid)
        video = data["video"]
        clips = []
        for c in data["candidates"]:
            clips.append({
                "id": c["id"], "start": c["start"], "end": c["end"], "duration": c["duration"],
                "peak": round(peak_time(c), 2), "score": c["score"], "pct": int(round(min(1.0, c["score"]) * 100)),
                "level": level_of(c["score"]), "tags": [t for t in c.get("tags", []) if t in cf.DETECTORS],
                "flags": [f for f in c.get("flags", []) if isinstance(f, str)],
            })
        return {"id": aid, "name": meta["name"], "kind": meta.get("kind", "file"), "run": data["run_id"],
                "duration": data["duration"], "width": data["width"], "height": data["height"],
                "preset": meta.get("preset", "all") if meta.get("preset") in cf.PRESETS else "all",
                "sensitivity": meta.get("sensitivity", 50) if isinstance(meta.get("sensitivity"), int) else 50,
                "source_ok": os.path.isfile(video) and os.path.splitext(video)[1].lower() in VIDEO_EXT,
                "ext": os.path.splitext(video)[1].lower(), "low_res": bool(meta.get("low_res")),
                "clips": clips, "made": self.store.made(aid)}

    # ---- jobs
    def start_analyze(self, body: dict) -> dict:
        kind = body.get("kind")
        preset, sens = self.read_choice(body)
        if kind == "file":
            video = check_video_path(body.get("path"))
            aid, name = Store.make_id("file", video), os.path.splitext(os.path.basename(video))[0][:80]
            source = video
        elif kind == "link":
            url = body.get("url")
            if not isinstance(url, str) or len(url) > 500:
                raise ApiError(400, "bad_link")
            try:
                url = cf.parse_youtube_url(url)
            except cf.ClipError:
                raise ApiError(400, "bad_link")
            m = YT_ID.search(url)
            aid, name = Store.make_id("link", url), f"YouTube {m.group(1)}" if m else "YouTube"
            source = url
            if not self.tools()["ytdlp"]:
                raise ApiError(400, "no_ytdlp")
        else:
            raise ApiError(400, "bad_request")
        out = self.store.adir(aid)
        if os.path.isfile(os.path.join(out, "meta.json")) and not body.get("again"):
            try:
                if os.path.isfile(self.store.load(aid)["video"]):
                    return {"existing": aid}
            except ApiError:
                pass

        self.need_space()

        def work(progress):
            cf.make_dir(out)
            video = source
            if kind == "link":
                progress("download", None)
                video = cf.download_video(source, out, quiet=True, low=True)      # small copy: enough to find moments
            data = cf.analyze(video, out, make_params(preset, sens), reuse=True, thumbs=False, quiet=True,
                              progress=progress, write_report=False)
            meta = {"name": name, "kind": kind, "source": source, "preset": preset, "sensitivity": sens,
                    "low_res": kind == "link", "created": time.strftime("%Y-%m-%d %H:%M")}
            self.store.write_meta(aid, meta)
            cands = data["candidates"][:12]
            for i, c in enumerate(cands):                    # first screenful of pictures, ready before the page opens
                if cf.CANCEL.is_set():
                    raise cf.Cancelled()
                progress("thumbs", i / max(1, len(cands)))
                try:
                    self.thumb(aid, c["id"])
                except ApiError:
                    pass
            return {"analysis": aid}

        return {"job": self.jobs.start("analyze", aid, work)["id"]}

    @staticmethod
    def read_choice(body: dict):
        preset = body.get("preset", "all")
        sens = body.get("sensitivity", 50)
        if preset not in cf.PRESETS or isinstance(sens, bool) or not isinstance(sens, int) or not 0 <= sens <= 100:
            raise ApiError(400, "bad_request")
        return preset, sens

    def start_redetect(self, body: dict) -> dict:
        aid = body.get("id")
        meta = self.store.read_meta(aid)
        data = self.store.load(aid)
        preset, sens = self.read_choice(body)
        out = self.store.adir(aid)

        def work(progress):
            cf.analyze(data["video"], out, make_params(preset, sens), reuse=True, thumbs=False, quiet=True,
                       progress=progress, write_report=False)
            meta.update(preset=preset, sensitivity=sens)
            self.store.write_meta(aid, meta)
            return {"analysis": aid}

        return {"job": self.jobs.start("redetect", aid, work)["id"]}

    def start_render(self, body: dict) -> dict:
        aid = body.get("id")
        self.store.read_meta(aid)
        data = self.store.load(aid)
        run = body.get("run")
        if not isinstance(run, str) or run != data["run_id"]:
            raise ApiError(409, "stale", "the list changed - open the clips again")
        raw = body.get("items")
        if not isinstance(raw, list) or not 1 <= len(raw) <= 40:
            raise ApiError(400, "bad_request")
        items = []
        for it in raw:
            if not isinstance(it, dict):
                raise ApiError(400, "bad_request")
            cid, start, end = it.get("id"), it.get("start"), it.get("end")
            layout, crop_x, title = it.get("layout", "blur"), it.get("crop_x", 0.5), it.get("title", "")
            if isinstance(cid, bool) or not isinstance(cid, int):
                raise ApiError(400, "bad_request")
            for v in (start, end, crop_x):
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                    raise ApiError(400, "bad_request")
            if layout not in cf.LAYOUTS or not isinstance(title, str):
                raise ApiError(400, "bad_request")
            items.append({"id": cid, "start": start, "end": end, "layout": layout, "crop_x": crop_x,
                          "title": title})
        out = self.store.adir(aid)
        meta = self.store.read_meta(aid)
        self.need_space()

        def work(progress):
            def cb(n, total, frac):
                progress("render", (n + frac) / total)
            fetch = self.section_fetcher(aid, meta, data, progress) if meta.get("low_res") else None
            made = cf.render_items(out, items, run=run, quiet=True, progress=cb, fetch=fetch)
            files = []
            for m in made:
                st = os.stat(m["path"])
                files.append({"id": m["id"], "file": m["file"], "size": st.st_size})
            return {"analysis": aid, "files": files}

        return {"job": self.jobs.start("render", aid, work)["id"]}

    def section_fetcher(self, aid: str, meta: dict, data: dict, progress):
        """For a clip from a link: download only that part of the video in full quality (a few seconds of padding
        on each side), check that the file has the length we asked for, and cut from it. If that fails, download
        the whole video once in full quality and cut from that."""
        url, out = meta["source"], self.store.adir(aid)
        folder = os.path.join(out, "sections")
        full = {}
        pad = 3.0

        def fetch(cid, start, dur, n, total):
            s, e = max(0.0, start - pad), min(float(data["duration"]), start + dur + pad)
            progress("fetch", n / total)
            try:
                cf.make_dir(folder)
                path = cf.download_section(url, s, e, folder, f"sec_{int(s * 100)}_{int(e * 100)}", quiet=True)
                info = cf.probe(path)
                if abs(info["duration"] - (e - s)) > 2.0:
                    cf.die("the downloaded part does not have the requested length")
                return path, start - s, info["width"], info["height"]
            except cf.ClipError:
                pass                                           # fall back to the whole video below
            if "path" not in full:
                progress("fetch_full", n / total)
                full["path"] = cf.download_video(url, out, quiet=True)
            info = cf.probe(full["path"])
            return full["path"], start, info["width"], info["height"]

        return fetch

    # ---- pictures
    def clip_frame(self, aid: str, clip_id: int, at: float | None = None) -> tuple:
        """One small JPEG from a clip that was already made: (jpeg bytes, second used, clip length).
        at: seconds into the clip (None = the middle). Only files directly inside this analysis's clips folder."""
        self.store.load(aid)                                           # the analysis must exist
        folder = os.path.realpath(os.path.join(self.store.adir(aid), "clips"))
        prefix = f"clip_{int(clip_id):02d}_"
        try:
            names = [n for n in os.listdir(folder) if n.startswith(prefix) and n.endswith(".mp4")]
        except OSError:
            names = []
        paths = [p for p in (os.path.join(folder, n) for n in names)
                 if os.path.isfile(p) and os.path.dirname(os.path.realpath(p)) == folder]
        if not paths:
            raise ApiError(404, "not_found")
        path = max(paths, key=os.path.getmtime)
        try:
            dur = float(cf.probe(path)["duration"])
        except (SystemExit, KeyError, TypeError, ValueError):
            raise ApiError(400, "unreadable") from None
        when = min(max(0.0, dur - 0.2), dur / 2 if at is None else float(at))
        cmd = [cf.tool("ffmpeg"), "-nostdin", "-v", "error", *cf.FF_IN, "-ss", f"{when:.2f}", "-i", path,
               "-frames:v", "1", "-vf", "scale=270:480", "-f", "mjpeg", "-q:v", "5", "-"]
        try:
            with self.thumb_slots:
                jpg = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired):
            jpg = b""
        if not jpg.startswith(b"\xff\xd8") or len(jpg) > 400_000:
            raise ApiError(400, "unreadable")
        return jpg, when, dur

    def thumb(self, aid: str, clip_id: int) -> bytes:
        data = self.store.load(aid)
        cand = next((c for c in data["candidates"] if c["id"] == clip_id), None)
        if cand is None:
            raise ApiError(404, "not_found")
        folder = os.path.join(self.store.adir(aid), "thumbs")
        path = os.path.join(folder, f"t{int(peak_time(cand) * 10)}.jpg")
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                return fh.read()
        if not os.path.isfile(data["video"]):
            raise ApiError(404, "source_missing")
        with self.thumb_slots:
            jpg = cf.grab_frame(data["video"], peak_time(cand), 480)
        if not jpg:
            raise ApiError(404, "no_picture")
        cf.make_dir(folder)
        cf.atomic_write(path, "wb", lambda fh: fh.write(jpg))
        return jpg


def importlib_has(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# --------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "clipfinder"
    sys_version = ""
    timeout = 60

    # quiet by default; never print the query string (it holds the access token)
    def log_message(self, fmt, *args):
        if self.server.app.verbose:
            sys.stderr.write("%s %s\n" % (self.command, urlsplit(self.path).path))

    @property
    def app(self) -> App:
        return self.server.app

    # ---- plumbing
    def _headers(self, ctype: str | None = None, length: int | None = None, cache: str = "no-store"):
        if ctype:
            self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None, html: bool = False):
        self.send_response(status)
        self._headers(ctype, len(body))
        if html:
            self.send_header("Content-Security-Policy", CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if status >= 400 and self.command in ("POST", "PUT"):
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _fail(self, status: int, code: str, message: str = ""):
        self._json({"error": code, "message": message or code}, status)

    def _local_hosts(self):
        p = self.app.port
        return {f"127.0.0.1:{p}", f"localhost:{p}"}

    def _allowed_hosts(self):
        extra = {self.app.remote_host} if self.app.remote_host else set()
        return self._local_hosts() | extra

    def _is_remote(self) -> bool:
        """Came through Tailscale (or any proxy), not straight from this computer."""
        if self.headers.get("Host", "") not in self._local_hosts():
            return True
        return any(self.headers.get(h) is not None for h in PROXY_HEADERS)

    def _local_only(self):
        if self._is_remote():
            raise ApiError(403, "local_only")

    def _check_common(self) -> bool:
        if self.headers.get("Host", "") not in self._allowed_hosts():
            self._fail(403, "bad_host")
            return False
        if self.headers.get("Transfer-Encoding") is not None:      # we only read bodies with a Content-Length
            self.close_connection = True
            self._fail(400, "no_chunked")
            return False
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):
            self._fail(403, "cross_site")
            return False
        return True

    def _has_session(self) -> bool:
        raw = self.headers.get("Cookie", "")
        if not raw:
            return False
        try:
            jar = http.cookies.SimpleCookie(raw)
        except http.cookies.CookieError:
            return False
        morsel = jar.get(COOKIE)
        return bool(morsel) and hmac.compare_digest(morsel.value.encode(), self.app.token.encode())

    def _check_write(self) -> bool:
        origins = {f"http://{h}" for h in self._local_hosts()}
        if self.app.remote_host:
            origins.add(f"https://{self.app.remote_host}")
        if self.headers.get("Origin", "") not in origins:
            self._fail(403, "bad_origin")
            return False
        if self.headers.get("X-CF") != "1":
            self._fail(403, "no_header")
            return False
        return True

    def _read_json(self) -> dict:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(415, "bad_type")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise ApiError(411, "length_required")
        if not 0 <= length <= MAX_JSON:
            raise ApiError(413, "too_large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise ApiError(400, "bad_json")
        if not isinstance(body, dict):
            raise ApiError(400, "bad_json")
        return body

    # ---- dispatch
    def _dispatch(self, method: str):
        try:
            if not self._check_common():
                return
            url = urlsplit(self.path)
            if len(url.query) > 2000:
                raise ApiError(414, "uri_too_long")
            path, query = url.path, (parse_qs(url.query, max_num_fields=10) if url.query else {})
            if method == "GET" and path == "/" and "t" in query:
                tok = query["t"][0]
                if hmac.compare_digest(tok.encode(), self.app.token.encode()):
                    self.send_response(302)
                    self.send_header("Location", "/")
                    secure = "; Secure" if self.headers.get("Host", "") == self.app.remote_host else ""
                    self.send_header("Set-Cookie", f"{COOKIE}={self.app.token}; Path=/; HttpOnly; SameSite=Strict{secure}")
                    self._headers("text/plain", 0)
                    self.end_headers()
                    return
            if not self._has_session():
                if method == "GET" and path == "/":
                    self._send(403, NO_ACCESS_PAGE.encode("utf-8"), "text/html; charset=utf-8", html=True)
                else:
                    self._fail(403, "no_session")
                return
            if method in ("POST", "PUT") and not self._check_write():
                return
            self._route(method, path, query)
        except ApiError as exc:
            self._fail(exc.status, exc.code, exc.message)
        except cf.ClipError as exc:
            self._fail(400, explain(exc.message), exc.message)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            self.close_connection = True
        except Exception:
            traceback.print_exc(file=sys.stderr)
            try:
                self._fail(500, "internal", "unexpected error")
            except OSError:
                self.close_connection = True

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    # ---- routes
    def _route(self, method: str, path: str, query: dict):
        app = self.app
        parts = [unquote(p) for p in path.split("/") if p]
        if method == "GET":
            if path in STATIC:
                name, ctype = STATIC[path]
                with open(os.path.join(WEB_DIR, name), "rb") as fh:
                    return self._send(200, fh.read(), ctype, html=(path == "/"))
            if path == "/api/state":
                st = app.state()
                st["remote"] = self._is_remote()
                return self._json(st)
            if path == "/api/remote":
                self._local_only()
                return self._json(app.remote_info())
            if path == "/api/job":
                return self._json({"job": app.jobs.view()})
            if path == "/api/connector":
                self._local_only()
                return self._json(app.connector_info())
            if len(parts) == 3 and parts[:2] == ["api", "analysis"]:
                return self._json(app.describe(parts[2]))
            if len(parts) == 3 and parts[0] == "thumb":
                m = re.fullmatch(r"(\d{1,4})\.jpg", parts[2])
                if not m:
                    raise ApiError(404, "not_found")
                return self._send(200, app.thumb(parts[1], int(m.group(1))), "image/jpeg",
                                  {"Cache-Control": "private, max-age=3600"})
            if len(parts) == 2 and parts[0] == "media":
                data = app.store.load(parts[1])
                video = data["video"]
                ext = os.path.splitext(video)[1].lower()
                if ext not in VIDEO_EXT or not os.path.isfile(video):
                    raise ApiError(404, "source_missing")
                return self._serve_file(video, MIME.get(ext, "application/octet-stream"))
            if len(parts) == 3 and parts[0] == "clip":
                if not re.fullmatch(r"[^/\\\x00]{1,200}\.mp4", parts[2]) or parts[2].startswith("."):
                    raise ApiError(404, "not_found")
                folder = os.path.realpath(app.store.clips_dir(parts[1]))
                target = os.path.realpath(os.path.join(folder, parts[2]))
                if os.path.dirname(target) != folder or not os.path.isfile(target):
                    raise ApiError(404, "not_found")
                return self._serve_file(target, "video/mp4")
            raise ApiError(404, "not_found")

        if method == "POST":
            if path in ("/api/pick", "/api/update-ytdlp", "/api/open-folder", "/api/quit", "/api/connector-test",
                        "/api/remote"):
                self._local_only()
            if path == "/api/pick":
                self._read_json()
                picked = app.picker()
                if picked is None:
                    return self._json({"unavailable": True})
                if picked == "":
                    return self._json({"path": ""})
                return self._json({"path": check_video_path(picked)})
            body = self._read_json()
            if path == "/api/analyze":
                return self._json(app.start_analyze(body))
            if path == "/api/redetect":
                return self._json(app.start_redetect(body))
            if path == "/api/render":
                return self._json(app.start_render(body))
            if path == "/api/update-ytdlp":
                return self._json(app.update_ytdlp())
            if path == "/api/connector-test":
                return self._json(app.test_connector())
            if path == "/api/remote":
                return self._json(app.set_remote(body.get("host")))
            if path == "/api/lang":
                return self._json({"lang": save_lang(app.store.root, body.get("lang"))})
            if path == "/api/cancel":
                return self._json({"cancelled": app.jobs.cancel()})
            if path == "/api/open-folder":
                aid = body.get("id")
                folder = app.store.clips_dir(aid) if aid else app.store.videos
                if aid:
                    app.store.read_meta(aid)
                    cf.make_dir(folder)
                return self._json({"opened": bool(app.open_folder(folder)), "path": folder})
            if path == "/api/quit":
                self._json({"bye": True})
                threading.Thread(target=app.server.shutdown, daemon=True).start()
                return
            raise ApiError(404, "not_found")

        if method == "PUT" and path == "/api/upload":
            return self._upload(query)
        raise ApiError(404, "not_found")

    def _upload(self, query: dict):
        name = (query.get("name") or [""])[0]
        ext = os.path.splitext(name)[1].lower()
        if ext not in VIDEO_EXT:
            raise ApiError(400, "bad_type")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise ApiError(411, "length_required")
        if not 1 <= length <= MAX_UPLOAD:
            raise ApiError(413, "too_large")
        store = self.app.store
        if shutil.disk_usage(store.videos).free < length + 2 * 1024 ** 3:
            raise ApiError(413, "no_space")
        base = cf.safe_name(os.path.splitext(name)[0]) or "video"
        target = os.path.join(store.videos, base + ext)
        n = 1
        while True:
            try:
                open(target, "xb").close()                    # reserve the name
                break
            except FileExistsError:
                n += 1
                target = os.path.join(store.videos, f"{base}-{n}{ext}")
        tmp = os.path.join(store.videos, f".upload-{secrets.token_hex(6)}.part")
        remaining = length
        try:
            with open(tmp, "wb") as fh:
                while remaining:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    fh.write(chunk)
                    remaining -= len(chunk)
            if remaining:
                raise ApiError(400, "incomplete")
            os.replace(tmp, target)
        except BaseException:
            for f in (tmp, target):
                if os.path.exists(f) and (f == tmp or os.path.getsize(f) == 0):
                    try:
                        os.remove(f)
                    except OSError:
                        pass
            raise
        self._json({"path": os.path.realpath(target)})

    def _serve_file(self, path: str, ctype: str):
        size = os.path.getsize(path)
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
            if m and (m.group(1) or m.group(2)):
                a, b = m.groups()
                if a == "":
                    n = int(b)
                    start, end = max(0, size - n), size - 1
                else:
                    start, end = int(a), min(int(b), size - 1) if b else size - 1
                if size == 0 or start >= size or start > end or (a == "" and int(b) == 0):
                    self.send_response(416)
                    self._headers("text/plain", 0)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                status = 206
        length = end - start + 1 if size else 0
        self.send_response(status)
        self._headers(ctype, length, cache="private, max-age=0")
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as fh:
            fh.seek(start)
            left = length
            while left > 0:
                chunk = fh.read(min(256 * 1024, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


NO_ACCESS_PAGE = """<!doctype html><html lang="ml"><head><meta charset="utf-8"><title>clipfinder</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family:system-ui,'Noto Sans Malayalam',sans-serif;background:#14161b;color:#eceef2;padding:40px;line-height:1.6">
<h1>ഈ പേജ് തുറക്കാൻ അനുമതി ഇല്ല</h1>
<p>clipfinder ആരംഭിച്ച ടെർമിനലിൽ (കറുത്ത ജാലകത്തിൽ) കാണിച്ചിരിക്കുന്ന മുഴുവൻ ലിങ്ക് (<code>http://127.0.0.1:…/?t=…</code>) ബ്രൗസറിൽ തുറക്കുക.</p>
<h2 lang="en">Not allowed to open this page</h2>
<p lang="en">Open the full link (<code>http://127.0.0.1:…/?t=…</code>) shown in the terminal (the black window) where clipfinder was started.</p>
</body></html>"""


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # a browser closing a video connection halfway is normal, not an error worth printing
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)

    allow_reuse_address = os.name != "nt"                    # on Windows this flag would let another program share the port


def make_server(store: Store, port: int = 8765, token: str | None = None, verbose: bool = False,
                remote_host: str | None = None):
    """Bind to 127.0.0.1 only; if the port is taken try the next ones. Returns (server, app)."""
    last = None
    for p in range(port, port + 20) if port else [0]:
        try:
            srv = Server(("127.0.0.1", p), Handler)
            break
        except OSError as exc:
            last = exc
    else:
        raise SystemExit(f"cannot open a local port ({last})")
    if remote_host:
        token = token or remote_token(store.root)
    app = App(store, srv.server_address[1], token or secrets.token_urlsafe(32), verbose)
    app.remote_host = remote_host
    app.server = srv
    srv.app = app
    return srv, app


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="clipfinder_web", description="clipfinder in your browser (local only)")
    ap.add_argument("--data", default=os.path.join("~", "ClipFinder"), help="folder for videos, analyses and clips")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    ap.add_argument("--tab", action="store_true", help="open in a normal browser tab instead of a small app window")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--remote-host", help="your Tailscale name (like pc.tail1234.ts.net) to allow opening the page from your other devices")
    ap.add_argument("--mcp", action="store_true", help="run as the Claude connector (started by Claude, not by hand)")
    ap.add_argument("--images", action="store_true", help="with --mcp: let Claude see one still picture per clip")
    ap.add_argument("--pick-file", action="store_true", help=argparse.SUPPRESS)      # used internally for the file dialog
    args = ap.parse_args(argv)
    if args.pick_file:
        return pick_file_here()
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass
    cf.QUIET_ERRORS = True
    add_bundled_tools()
    try:
        store = Store(args.data)
    except cf.ClipError as exc:
        print(f"ERROR: {exc.message}", file=sys.stderr)
        return 2
    if args.mcp:
        sys.modules.setdefault("clipfinder_web", sys.modules[__name__])   # when started as a script this IS that module
        import clipfinder_mcp
        return clipfinder_mcp.run(store, images=args.images)
    try:
        remote = clean_remote_host(args.remote_host) if args.remote_host else load_remote_host(store.root)
    except ApiError:
        print("ERROR: --remote-host must be a Tailscale name ending in .ts.net", file=sys.stderr)
        return 2
    srv, app = make_server(store, args.port, verbose=args.verbose, remote_host=remote)
    url = f"http://127.0.0.1:{app.port}/?t={app.token}"
    print("clipfinder is running on your computer only.", flush=True)
    print(f"Open this link in your browser (it opens by itself in a moment):\n\n    {url}\n", flush=True)
    print(f"Your videos folder: {store.videos}", flush=True)
    if remote:
        print(f"Remote access is ON for {remote}. Start sharing with:  tailscale serve --bg {app.port}", flush=True)
    print("Stop with Ctrl+C (or the 'Close' button in Settings).", flush=True)
    if not args.no_browser:
        threading.Timer(0.6, lambda: open_ui(url, window=not args.tab)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        cf.CANCEL.set()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
