"""clipfinder: optional AI helper that runs on this computer only.

A vision model looks at one small picture sheet made from a finished clip and suggests a title, a description and
hashtags. Three ways to run that model, all on this computer (127.0.0.1) and nowhere else:

* the app's own copy of llama.cpp ("local/..."): setup_windows.bat can download the llama.cpp program and one
  model into the data folder (see clipfinder_ai_setup.py). The app starts that program itself the first time the AI
  is asked for something, and stops it when the app closes. Nothing else needs to be installed or opened.
* LM Studio ("lmstudio/..."), if the user runs it with its local server on (port 1234).
* Ollama (model names as Ollama shows them), if the user runs it (port 11434).

What comes back from the model is ordinary untrusted text: it is cut to size, cleaned, and only ever placed into
the title / description boxes, where the user can change it before using it."""
from __future__ import annotations

import base64
import http.client
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time

HOST = "127.0.0.1"                       # every engine is on this computer; never anything else
PORT = 11434                             # Ollama
LMSTUDIO_PORT = 1234                     # LM Studio's local server
LOCAL_MODEL = "local/gemma-3-4b"         # how the app's own model is shown and saved
LMSTUDIO_PREFIX = "lmstudio/"
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}")
MAX_REPLY = 2 * 1024 * 1024
LANG_NAMES = {"en": "English", "ml": "Malayalam"}
START_TIMEOUT = 300.0                    # loading a 3 GB model from a slow disk can take a while

SCHEMA = {"type": "object",
          "properties": {"title": {"type": "string"}, "description": {"type": "string"},
                         "hashtags": {"type": "array", "items": {"type": "string"}}},
          "required": ["title", "description", "hashtags"]}


class AiError(Exception):
    """code: not_running | no_model | failed"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        if detail:
            print(f"ai: {code}: {detail[:300]}", file=sys.stderr)


def _call(method: str, path: str, body=None, timeout: float = 5.0, port: int | None = None, key: str = "") -> dict:
    conn = http.client.HTTPConnection(HOST, PORT if port is None else port, timeout=timeout)
    try:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        if key:
            headers["Authorization"] = "Bearer " + key
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read(MAX_REPLY + 1)
        if resp.status != 200 or len(raw) > MAX_REPLY:
            raise AiError("failed", f"{path}: HTTP {resp.status}")
        obj = json.loads(raw.decode("utf-8"))
    except (ConnectionError, TimeoutError) as exc:
        raise AiError("not_running" if isinstance(exc, ConnectionError) else "failed", type(exc).__name__) from None
    except (OSError, http.client.HTTPException, ValueError, RecursionError) as exc:
        raise AiError("failed", type(exc).__name__) from None
    finally:
        conn.close()
    if not isinstance(obj, dict):
        raise AiError("failed", "reply is not an object")
    return obj


# --------------------------------------------------------------------------- the app's own llama.cpp

class LocalEngine:
    """The llama.cpp server that clipfinder_ai_setup.py put into <data>/ai. Started on first use, stopped on exit.
    It listens on 127.0.0.1 only and needs a key that only this app knows, so a web page cannot use it."""

    def __init__(self):
        self.folder = ""
        self.proc = None
        self.port = 0                             # a free port is picked each time the program starts
        self.job = None
        self.key = secrets.token_urlsafe(24)
        self.lock = threading.Lock()

    def configure(self, data_dir: str) -> None:
        self.folder = os.path.join(os.path.abspath(data_dir), "ai") if data_dir else ""

    def files(self) -> dict | None:
        """Paths of the server program, the model and its picture part, if the setup finished; else None."""
        if not self.folder:
            return None
        try:
            with open(os.path.join(self.folder, "ready.json"), encoding="utf-8") as fh:
                info = json.load(fh)
        except (OSError, ValueError, RecursionError):
            return None
        root = os.path.realpath(self.folder)
        out = {}
        for key in ("server", "model", "mmproj"):
            rel = info.get(key) if isinstance(info, dict) else None
            if not isinstance(rel, str) or not rel or os.path.isabs(rel):
                return None
            path = os.path.realpath(os.path.join(root, rel))
            if not path.startswith(root + os.sep) or not os.path.isfile(path):
                return None
            out[key] = path
        return out

    def installed(self) -> bool:
        return self.files() is not None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def ready(self) -> bool:
        if not self.alive():
            return False
        try:
            return _call("GET", "/health", port=self.port, timeout=2.0).get("status") == "ok"
        except AiError:
            return False

    def start(self, timeout: float = START_TIMEOUT) -> None:
        with self.lock:
            if self.ready():
                return
            f = self.files()
            if not f:
                raise AiError("no_model", "the AI files are not set up")
            self.stop()
            self.port = free_port()
            cmd = [f["server"], "-m", f["model"], "--mmproj", f["mmproj"], "--host", HOST, "--port", str(self.port),
                   "--api-key", self.key, "-c", "8192", "-np", "1", "--no-webui"]
            log = open(os.path.join(self.folder, "server_log.txt"), "wb")
            try:
                self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                             cwd=os.path.dirname(f["server"]),
                                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError as exc:
                raise AiError("failed", f"could not start the AI program: {exc}") from None
            finally:
                log.close()
            self.job = kill_with_us(self.proc)
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if self.proc.poll() is not None:
                    raise AiError("failed", "the AI program stopped while loading (see ai/server_log.txt)")
                if self.ready():
                    return
                time.sleep(1.0)
            self.stop()
            raise AiError("failed", "the AI program did not get ready in time")

    def stop(self) -> None:
        p, self.proc = self.proc, None
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def kill_with_us(proc):
    """Windows: put the AI program in a job that Windows closes when this app ends in any way (even when its
    window is closed with X), so a 3 GB program is never left running on its own. Elsewhere: nothing to do."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.OpenProcess.restype = wintypes.HANDLE

        class LIMIT(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

        class EXT(ctypes.Structure):
            _fields_ = [("Basic", LIMIT), ("Io", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = k32.CreateJobObjectW(None, None)
        info = EXT()
        info.Basic.LimitFlags = 0x2000                       # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not job or not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return None
        handle = k32.OpenProcess(0x0001 | 0x0100, False, proc.pid)   # PROCESS_TERMINATE | PROCESS_SET_QUOTA
        if not handle or not k32.AssignProcessToJobObject(job, handle):
            return None
        k32.CloseHandle(handle)
        return job                                             # kept open for as long as the app runs
    except (OSError, AttributeError, ValueError):
        return None


LOCAL = LocalEngine()


def configure(data_dir: str) -> None:
    LOCAL.configure(data_dir)


def shutdown() -> None:
    LOCAL.stop()


# --------------------------------------------------------------------------- what is available

def ollama_models() -> list:
    found = _call("GET", "/api/tags").get("models")
    names = [m.get("name") for m in found if isinstance(m, dict)] if isinstance(found, list) else []
    return sorted({n for n in names if isinstance(n, str) and MODEL_RE.fullmatch(n)})[:100]


def lmstudio_models() -> list:
    found = _call("GET", "/v1/models", port=LMSTUDIO_PORT, timeout=2.0).get("data")
    ids = [m.get("id") for m in found if isinstance(m, dict)] if isinstance(found, list) else []
    return sorted({LMSTUDIO_PREFIX + i for i in ids if isinstance(i, str) and MODEL_RE.fullmatch(LMSTUDIO_PREFIX + i)})[:50]


def models() -> list:
    """Every model that can be used now: the app's own one first, then LM Studio's and Ollama's."""
    out = [LOCAL_MODEL] if LOCAL.installed() else []
    for fn in (lmstudio_models, ollama_models):
        try:
            out += fn()
        except AiError:
            pass
    return out


def status() -> dict:
    found = models()
    return {"running": bool(found), "models": found, "local": LOCAL.installed(), "local_on": LOCAL.alive()}


# --------------------------------------------------------------------------- asking

def clean(text, limit: int) -> str:
    if not isinstance(text, str):
        return ""
    text = "".join(ch for ch in text if ch == "\n" or (ch >= " " and ch != "\x7f"))
    return text.replace("<", "").replace(">", "").strip()[:limit]


def hashtag(word) -> str:
    word = re.sub(r"[^\w]", "", word if isinstance(word, str) else "", flags=re.UNICODE)[:30]
    return "#" + word if word else ""


def _openai_chat(port: int, model_id: str, prompt: str, jpeg: bytes, key: str = "") -> str:
    """OpenAI-style chat with one picture (llama.cpp's server and LM Studio both speak this)."""
    reply = _call("POST", "/v1/chat/completions", {
        "model": model_id, "temperature": 0.4, "max_tokens": 400, "stream": False,
        "response_format": {"type": "json_schema", "json_schema": {"name": "clip", "strict": True, "schema": SCHEMA}},
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")}}]}]},
        timeout=600.0, port=port, key=key)
    choices = reply.get("choices")
    first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = first.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, str) else ""


def _ollama_chat(model: str, prompt: str, jpeg: bytes) -> str:
    reply = _call("POST", "/api/chat", {
        "model": model, "stream": False, "format": SCHEMA, "options": {"temperature": 0.4, "num_predict": 400},
        "messages": [{"role": "user", "content": prompt, "images": [base64.b64encode(jpeg).decode("ascii")]}]},
        timeout=300.0)
    message = reply.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, str) else ""


def describe(model: str, sheet_jpeg: bytes, game: str, hint: str, lang: str = "en") -> dict:
    """Ask the model for a title and description of the clip shown in sheet_jpeg (a few frames side by side).
    Returns {"title": ..., "description": ..., "seconds": ...} ready for the boxes on the page."""
    if not isinstance(model, str) or not MODEL_RE.fullmatch(model):
        raise AiError("no_model")
    game, hint = clean(game, 60), clean(hint, 100)
    language = LANG_NAMES.get(lang, "English")
    script = " Use Malayalam script (മലയാളം), not English words in Latin letters." if lang == "ml" else ""
    prompt = (
        "The picture shows a few frames, left to right in time order, from one short gameplay clip recorded on a PS5. "
        + (f"The game is: {game}. " if game else "")
        + (f"The streamer's working title is: {hint}. " if hint else "")
        + "Write for a YouTube Short: a catchy title of at most 60 characters, a description of two or three short "
          "sentences that says what happens in the clip, and 3 to 5 hashtags (single English words, no '#'). "
          f"Write the title and the description in {language}.{script} "
          "Describe only what can be seen in the frames. Do not guess the names of characters, villains or places "
          "that are not clearly shown (say 'the hero' or 'enemies' instead), and do not invent scores or story. "
          "Reply as JSON.")
    t0 = time.monotonic()
    if model == LOCAL_MODEL:
        LOCAL.start()
        content = _openai_chat(LOCAL.port, "local", prompt, sheet_jpeg, LOCAL.key)
    elif model.startswith(LMSTUDIO_PREFIX):
        content = _openai_chat(LMSTUDIO_PORT, model[len(LMSTUDIO_PREFIX):], prompt, sheet_jpeg)
    else:
        content = _ollama_chat(model, prompt, sheet_jpeg)
    try:
        out = json.loads(content) if content else None
    except (ValueError, RecursionError):
        out = None
    if not isinstance(out, dict):
        raise AiError("failed", "the model did not answer in the expected form")
    title, text = clean(out.get("title"), 100).replace("\n", " "), clean(out.get("description"), 1500)
    tags = [t for t in (hashtag(x) for x in (out.get("hashtags") if isinstance(out.get("hashtags"), list) else [])[:8]) if t]
    tags = list(dict.fromkeys(["#Shorts"] + tags + ([hashtag(game)] if hashtag(game) else []) + ["#PS5", "#Gaming"]))[:10]
    if not title or not text:
        raise AiError("failed", "empty title or description")
    return {"title": title, "description": text + "\n\n" + " ".join(tags), "seconds": round(time.monotonic() - t0)}
