"""clipfinder: optional AI helper that runs on this computer only.

It talks to Ollama (https://ollama.com), a free program the user installs themself, at 127.0.0.1:11434 - an address
that is this computer and nothing else. A vision model in Ollama looks at one small picture sheet made from a
finished clip and suggests a title, a description and hashtags. No picture or text leaves the computer here.

What comes back from the model is ordinary untrusted text: it is cut to size, cleaned, and only ever placed into
the title / description boxes, where the user can change it before using it."""
from __future__ import annotations

import base64
import http.client
import json
import re
import sys

HOST, PORT = "127.0.0.1", 11434          # Ollama on this computer; never anything else
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}")
MAX_REPLY = 2 * 1024 * 1024
LANG_NAMES = {"en": "English", "ml": "Malayalam"}

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


def _call(method: str, path: str, body=None, timeout: float = 5.0) -> dict:
    conn = http.client.HTTPConnection(HOST, PORT, timeout=timeout)
    try:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        conn.request(method, path, body=data, headers={"Content-Type": "application/json"} if data else {})
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


def models() -> list:
    """Names of the models installed in Ollama (empty list when there are none)."""
    found = _call("GET", "/api/tags").get("models")
    names = [m.get("name") for m in found if isinstance(m, dict)] if isinstance(found, list) else []
    return sorted({n for n in names if isinstance(n, str) and MODEL_RE.fullmatch(n)})[:100]


def status() -> dict:
    try:
        return {"running": True, "models": models()}
    except AiError:
        return {"running": False, "models": []}


def clean(text, limit: int) -> str:
    if not isinstance(text, str):
        return ""
    text = "".join(ch for ch in text if ch == "\n" or (ch >= " " and ch != "\x7f"))
    return text.replace("<", "").replace(">", "").strip()[:limit]


def hashtag(word) -> str:
    word = re.sub(r"[^\w]", "", word if isinstance(word, str) else "", flags=re.UNICODE)[:30]
    return "#" + word if word else ""


def describe(model: str, sheet_jpeg: bytes, game: str, hint: str, lang: str = "en") -> dict:
    """Ask the model for a title and description of the clip shown in sheet_jpeg (a few frames side by side).
    Returns {"title": ..., "description": ...} ready for the boxes on the page."""
    if not isinstance(model, str) or not MODEL_RE.fullmatch(model):
        raise AiError("no_model")
    game, hint = clean(game, 60), clean(hint, 100)
    language = LANG_NAMES.get(lang, "English")
    prompt = (
        "The picture shows a few frames, left to right in time order, from one short gameplay clip recorded on a PS5. "
        + (f"The game is: {game}. " if game else "")
        + (f"The streamer's working title is: {hint}. " if hint else "")
        + f"Write for a YouTube Short, in {language}: a catchy title of at most 60 characters, a description of two "
          "or three short sentences that says what happens in the clip, and 3 to 5 hashtags (single words, no '#'). "
          "Describe only what can be seen; do not invent player names, scores or events. Reply as JSON.")
    reply = _call("POST", "/api/chat", {
        "model": model, "stream": False, "format": SCHEMA, "options": {"temperature": 0.4, "num_predict": 400},
        "messages": [{"role": "user", "content": prompt, "images": [base64.b64encode(sheet_jpeg).decode("ascii")]}]},
        timeout=300.0)
    message = reply.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    try:
        out = json.loads(content) if isinstance(content, str) else None
    except (ValueError, RecursionError):
        out = None
    if not isinstance(out, dict):
        raise AiError("failed", "the model did not answer in the expected form")
    title, text = clean(out.get("title"), 100).replace("\n", " "), clean(out.get("description"), 1500)
    tags = [t for t in (hashtag(x) for x in (out.get("hashtags") if isinstance(out.get("hashtags"), list) else [])[:8]) if t]
    tags = list(dict.fromkeys(["#Shorts"] + tags + ([hashtag(game)] if hashtag(game) else []) + ["#PS5", "#Gaming"]))[:10]
    if not title or not text:
        raise AiError("failed", "empty title or description")
    return {"title": title, "description": text + "\n\n" + " ".join(tags)}
