"""clipfinder: is there a newer version? Reads one tiny public file (version.json) from the project's GitHub page.
Nothing is downloaded or installed here: the page only shows a note, the user downloads the new version themself.
Nothing about the user or their videos is sent; GitHub sees an ordinary anonymous request for that file."""
from __future__ import annotations

import http.client
import json
import re
import ssl
import threading
import time
from urllib.parse import urlsplit

VERSION_URL = "https://raw.githubusercontent.com/Surajprem7/clipfinder./main/version.json"
PROJECT_PAGE = "https://github.com/Surajprem7/clipfinder."
VERSION_RE = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}")
KEEP_SECONDS = 6 * 3600
ALLOW_PLAIN_HTTP = False           # tests only

_lock = threading.Lock()
_cache = {"at": 0.0, "latest": None}


def as_tuple(version: str) -> tuple:
    return tuple(int(x) for x in version.split("."))


def fetch_latest(timeout: float = 6.0):
    """The newest version number published, or None when it cannot be read."""
    u = urlsplit(VERSION_URL)
    try:
        if u.scheme == "https":
            conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout, context=ssl.create_default_context())
        elif u.scheme == "http" and u.hostname == "127.0.0.1" and ALLOW_PLAIN_HTTP:
            conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
        else:
            return None
        try:
            conn.request("GET", u.path, headers={"User-Agent": "clipfinder-update-check", "Accept": "application/json"})
            resp = conn.getresponse()
            data = resp.read(2001)
            if resp.status != 200 or len(data) > 2000:
                return None
        finally:
            conn.close()
        version = json.loads(data.decode("utf-8")).get("version")
    except (OSError, http.client.HTTPException, ValueError, AttributeError, RecursionError):
        return None
    return version if isinstance(version, str) and VERSION_RE.fullmatch(version) else None


def check(current: str) -> dict:
    """{"current": "1.5.0", "latest": "1.6.0" or None, "newer": True/False}; asks GitHub at most every 6 hours."""
    with _lock:
        if time.time() - _cache["at"] > KEEP_SECONDS:
            _cache.update(at=time.time(), latest=fetch_latest())
        latest = _cache["latest"]
    newer = bool(latest) and as_tuple(latest) > as_tuple(current)
    return {"current": current, "latest": latest, "newer": newer}
