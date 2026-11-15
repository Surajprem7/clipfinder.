"""clipfinder: upload a finished clip to the user's own YouTube channel.

This is the only file of clipfinder that talks to Google. It uses nothing but the Python standard library.

How it works
  1. The user creates an "OAuth client" of type "Desktop app" in their own Google Cloud project and puts the
     downloaded JSON file into the data folder as youtube_client.json.
  2. "Connect": the browser goes to Google's sign-in page; Google sends the user back to this program on
     127.0.0.1 with a one-time code; the code is exchanged for a refresh token (PKCE protects the exchange).
  3. The refresh token is kept in the data folder (.youtube-token, readable by this user only on Linux/macOS).
     It allows uploading videos to that channel and nothing else (scope youtube.upload).
  4. "Upload": the clip is sent in pieces (resumable upload) with its title, description and visibility.

Nothing from Google's replies is shown to the page as-is: errors become short fixed codes, details go to stderr.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import secrets
import ssl
import sys
import threading
import time
from urllib.parse import urlencode, urlsplit

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPE = "https://www.googleapis.com/auth/youtube.upload"
CLIENT_FILE = "youtube_client.json"
TOKEN_FILE = ".youtube-token"
CHUNK = 4 * 1024 * 1024            # upload piece size (a multiple of 256 KiB, as Google requires)
MAX_REPLY = 1024 * 1024            # never read more than this from a reply
PENDING_SECONDS = 600              # a started sign-in is valid this long
PRIVACY = ("private", "unlisted", "public")
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{6,20}")
ALLOW_PLAIN_HTTP = False           # tests only: lets the URLs above point to a local stand-in server


class YouTubeError(Exception):
    """code: not_set_up | not_connected | reconnect | auth_failed | quota | upload_failed | cancelled | bad_request"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        if detail:
            print(f"youtube: {code}: {detail[:300]}", file=sys.stderr)


def _request(method: str, url: str, headers: dict, body: bytes = b"", timeout: float = 60.0):
    """One HTTPS request. Returns (status, headers dict with lower-case names, body bytes)."""
    u = urlsplit(url)
    local = u.hostname in ("127.0.0.1", "localhost")
    if u.scheme == "https":
        conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout,
                                           context=ssl.create_default_context())   # checks the certificate and the name
    elif u.scheme == "http" and local and ALLOW_PLAIN_HTTP:
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    else:
        raise YouTubeError("upload_failed", "refusing a non-https address")
    try:
        conn.request(method, (u.path or "/") + ("?" + u.query if u.query else ""), body=body,
                     headers={**headers, "Content-Length": str(len(body))})
        resp = conn.getresponse()
        data = resp.read(MAX_REPLY + 1)
        if len(data) > MAX_REPLY:
            raise YouTubeError("upload_failed", "reply too large")
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
    except (OSError, http.client.HTTPException) as exc:
        raise YouTubeError("upload_failed", f"network: {type(exc).__name__}") from None
    finally:
        conn.close()


def _json(data: bytes) -> dict:
    try:
        obj = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return {}
    return obj if isinstance(obj, dict) else {}


def clean_text(text, limit: int) -> str:
    """Title / description as YouTube accepts them: no control characters, no < or >, limited length."""
    if not isinstance(text, str):
        return ""
    text = "".join(ch for ch in text if ch == "\n" or (ch >= " " and ch != "\x7f"))
    return text.replace("<", "").replace(">", "").strip()[:limit]


def load_client(root: str):
    """(client_id, client_secret) from youtube_client.json in the data folder, or None."""
    try:
        with open(os.path.join(root, CLIENT_FILE), encoding="utf-8") as fh:
            raw = fh.read(20000)
        box = json.loads(raw).get("installed")
        cid, secret = box["client_id"], box["client_secret"]
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        return None
    if not (isinstance(cid, str) and isinstance(secret, str) and 10 < len(cid) < 200 and 5 < len(secret) < 200
            and cid.endswith(".apps.googleusercontent.com") and re.fullmatch(r"[\x21-\x7e]+", cid + secret)):
        return None
    return cid, secret


class YouTube:
    def __init__(self, root: str):
        self.root = root
        self.lock = threading.Lock()
        self.pending = {}                 # state -> (code verifier, redirect address, valid until)

    # ---- sign-in
    def token_path(self) -> str:
        return os.path.join(self.root, TOKEN_FILE)

    def refresh_token(self):
        try:
            with open(self.token_path(), encoding="utf-8") as fh:
                tok = fh.read(4000).strip()
        except OSError:
            return None
        return tok if re.fullmatch(r"[\x21-\x7e]{10,2000}", tok) else None

    def status(self) -> dict:
        return {"client": load_client(self.root) is not None, "connected": self.refresh_token() is not None}

    def begin(self, port: int) -> str:
        """Start a sign-in: returns the Google address the browser must open."""
        client = load_client(self.root)
        if not client:
            raise YouTubeError("not_set_up")
        state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        redirect = f"http://127.0.0.1:{int(port)}/oauth/callback"
        now = time.time()
        with self.lock:
            self.pending = {k: v for k, v in self.pending.items() if v[2] > now}
            if len(self.pending) >= 5:
                self.pending.clear()
            self.pending[state] = (verifier, redirect, now + PENDING_SECONDS)
        return AUTH_URL + "?" + urlencode({
            "client_id": client[0], "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
            "access_type": "offline", "prompt": "consent", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256"})

    def finish(self, state, code) -> None:
        """Google sent the user back: swap the one-time code for a refresh token and keep it."""
        with self.lock:
            entry = self.pending.pop(state, None) if isinstance(state, str) else None
        if not entry or entry[2] < time.time() or not isinstance(code, str) or not 1 <= len(code) <= 2000:
            raise YouTubeError("auth_failed", "unknown or expired sign-in")
        client = load_client(self.root)
        if not client:
            raise YouTubeError("not_set_up")
        status, _, data = _request("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"}, urlencode({
            "code": code, "client_id": client[0], "client_secret": client[1], "redirect_uri": entry[1],
            "grant_type": "authorization_code", "code_verifier": entry[0]}).encode())
        tok = _json(data).get("refresh_token")
        if status != 200 or not isinstance(tok, str) or not re.fullmatch(r"[\x21-\x7e]{10,2000}", tok):
            raise YouTubeError("auth_failed", f"token exchange: HTTP {status} {_json(data).get('error', '')}")
        path = self.token_path()
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(tok)
        os.replace(tmp, path)

    def disconnect(self) -> None:
        """Forget the sign-in on this computer. (To also cancel it at Google: myaccount.google.com/permissions.)"""
        try:
            os.remove(self.token_path())
        except FileNotFoundError:
            pass

    def access_token(self) -> str:
        client, refresh = load_client(self.root), self.refresh_token()
        if not client:
            raise YouTubeError("not_set_up")
        if not refresh:
            raise YouTubeError("not_connected")
        status, _, data = _request("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"}, urlencode({
            "client_id": client[0], "client_secret": client[1], "refresh_token": refresh,
            "grant_type": "refresh_token"}).encode())
        obj = _json(data)
        tok = obj.get("access_token")
        if status == 200 and isinstance(tok, str) and re.fullmatch(r"[\x21-\x7e]{10,4000}", tok):
            return tok
        if obj.get("error") == "invalid_grant":                # the sign-in expired or was removed at Google
            raise YouTubeError("reconnect", "invalid_grant")
        raise YouTubeError("auth_failed", f"refresh: HTTP {status} {obj.get('error', '')}")

    # ---- upload
    @staticmethod
    def _api_error(status: int, data: bytes) -> YouTubeError:
        err = _json(data).get("error")
        reason = ""
        if isinstance(err, dict):
            errors = err.get("errors")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                reason = str(errors[0].get("reason", ""))
        detail = f"HTTP {status} {reason}"
        if status == 401:
            return YouTubeError("reconnect", detail)
        if status == 403 and ("quota" in reason.lower() or "ratelimit" in reason.lower() or "uploadlimit" in reason.lower()):
            return YouTubeError("quota", detail)
        return YouTubeError("upload_failed", detail)

    def upload(self, path: str, title: str, description: str, privacy: str, progress=None, cancelled=None) -> str:
        """Send one video file. Returns the new video's id. progress(fraction) is called while it uploads."""
        title, description = clean_text(title, 100), clean_text(description, 4500)
        if privacy not in PRIVACY or not title:
            raise YouTubeError("bad_request")
        try:
            size = os.path.getsize(path)
        except OSError:
            raise YouTubeError("upload_failed", "file missing") from None
        if not 1000 <= size <= 2 * 1024 ** 3:
            raise YouTubeError("upload_failed", "file size out of range")
        token = self.access_token()
        meta = {"snippet": {"title": title, "description": description, "categoryId": "20"},
                "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
        status, headers, data = _request(
            "POST", UPLOAD_URL + "?uploadType=resumable&part=snippet,status",
            {"Authorization": "Bearer " + token, "Content-Type": "application/json; charset=UTF-8",
             "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)},
            json.dumps(meta).encode("utf-8"))
        session = headers.get("location", "")
        if status != 200 or not session:
            raise self._api_error(status, data)
        want, got = urlsplit(UPLOAD_URL), urlsplit(session)
        if (got.scheme, got.hostname, got.port) != (want.scheme, want.hostname, want.port):
            raise YouTubeError("upload_failed", "unexpected upload address")
        sent = 0
        with open(path, "rb") as fh:
            while sent < size:
                if cancelled and cancelled():
                    raise YouTubeError("cancelled")
                piece = fh.read(min(CHUNK, size - sent))
                if not piece:
                    raise YouTubeError("upload_failed", "file changed while uploading")
                end = sent + len(piece) - 1
                status, headers, data = _request(
                    "PUT", session, {"Content-Type": "video/mp4", "Content-Range": f"bytes {sent}-{end}/{size}"},
                    piece, timeout=300.0)
                if status in (200, 201):
                    vid = _json(data).get("id")
                    if not isinstance(vid, str) or not VIDEO_ID_RE.fullmatch(vid):
                        raise YouTubeError("upload_failed", "no video id in the reply")
                    if progress:
                        progress(1.0)
                    return vid
                if status != 308:
                    raise self._api_error(status, data)
                m = re.fullmatch(r"bytes=0-(\d+)", headers.get("range", ""))
                sent = int(m.group(1)) + 1 if m else end + 1      # Google says how much it has
                if sent > size:
                    raise YouTubeError("upload_failed", "bad range in the reply")
                fh.seek(sent)
                if progress:
                    progress(sent / size)
        raise YouTubeError("upload_failed", "upload ended without a reply")
