"""Tests for the YouTube upload. Google is played by a small stand-in server on this computer:
nothing here talks to the real Google.  Run:  python3 -m unittest discover -s tests"""
import ast
import http.server
import json
import os
import re
import sys
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipfinder_youtube as yt  # noqa: E402
from test_web import Base, read  # noqa: E402

CLIENT = {"installed": {"client_id": "1234567890-abcdefg.apps.googleusercontent.com", "client_secret": "GOCSPX-test-secret",
                        "auth_uri": "x", "token_uri": "y"}}


class FakeGoogle(http.server.BaseHTTPRequestHandler):
    """Token endpoint + resumable upload, with switches to misbehave."""
    box = None      # shared state, set by start_fake()

    def log_message(self, *a):
        pass

    def _reply(self, status, obj=None, headers=None, raw=None):
        data = raw if raw is not None else json.dumps(obj if obj is not None else {}).encode()
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        b = self.box
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        path = urlsplit(self.path).path
        if path == "/token":
            form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            b["token_calls"].append(form)
            if b["mode"] == "invalid_grant":
                return self._reply(400, {"error": "invalid_grant", "error_description": "<b>secret detail</b>"})
            if form.get("grant_type") == "authorization_code":
                if form.get("code") != "good-code" or not form.get("code_verifier"):
                    return self._reply(400, {"error": "invalid_request"})
                return self._reply(200, {"access_token": "access-1234567890", "refresh_token": "refresh-ABCDEFGHIJ", "expires_in": 3600})
            if form.get("refresh_token") != "refresh-ABCDEFGHIJ":
                return self._reply(400, {"error": "invalid_grant"})
            return self._reply(200, {"access_token": "access-1234567890", "expires_in": 3600})
        if path == "/upload":
            if self.headers.get("Authorization") != "Bearer access-1234567890":
                return self._reply(401, {"error": {"errors": [{"reason": "authError"}]}})
            if b["mode"] == "quota":
                return self._reply(403, {"error": {"errors": [{"reason": "quotaExceeded"}], "message": "<script>x</script>"}})
            b["meta"] = json.loads(body.decode("utf-8"))
            b["size"] = int(self.headers["X-Upload-Content-Length"])
            b["got"] = b""
            where = "http://evil.example/session" if b["mode"] == "evil_location" else f"http://127.0.0.1:{b['port']}/session/abc"
            return self._reply(200, {}, {"Location": where})
        self._reply(404)

    def do_PUT(self):
        b = self.box
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", self.headers.get("Content-Range", ""))
        if not m or int(m.group(1)) != len(b["got"]) or int(m.group(3)) != b["size"]:
            return self._reply(400)
        b["got"] += body
        b["puts"] += 1
        if b["mode"] == "fail_mid" and b["puts"] == 2:
            return self._reply(500, raw=b"<html>boom</html>")
        if len(b["got"]) < b["size"]:
            return self._reply(308, raw=b"", headers={"Range": f"bytes=0-{len(b['got']) - 1}"})
        if b["mode"] == "bad_id":
            return self._reply(200, {"id": "../../etc"})
        self._reply(200, {"id": "vid_ABC-123"})


def start_fake():
    box = {"mode": "ok", "token_calls": [], "puts": 0, "got": b"", "meta": None, "size": 0}
    handler = type("H", (FakeGoogle,), {"box": box})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    box["port"] = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, box


class WithFake(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake, cls.box = start_fake()
        cls.saved = (yt.TOKEN_URL, yt.UPLOAD_URL, yt.ALLOW_PLAIN_HTTP, yt.CHUNK)
        yt.TOKEN_URL = f"http://127.0.0.1:{cls.box['port']}/token"
        yt.UPLOAD_URL = f"http://127.0.0.1:{cls.box['port']}/upload"
        yt.ALLOW_PLAIN_HTTP = True
        yt.CHUNK = 256 * 1024

    @classmethod
    def tearDownClass(cls):
        yt.TOKEN_URL, yt.UPLOAD_URL, yt.ALLOW_PLAIN_HTTP, yt.CHUNK = cls.saved
        cls.fake.shutdown()
        cls.fake.server_close()
        super().tearDownClass()

    def reset(self, mode="ok"):
        self.box.update(mode=mode, token_calls=[], puts=0, got=b"", meta=None)


class TestModule(WithFake):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.y = yt.YouTube(self.root)
        self.reset()
        self.clip = os.path.join(self.root, "clip.mp4")
        with open(self.clip, "wb") as fh:
            fh.write(os.urandom(700_000))

    def tearDown(self):
        self.tmp.cleanup()

    def put_client(self, obj=CLIENT):
        with open(os.path.join(self.root, yt.CLIENT_FILE), "w", encoding="utf-8") as fh:
            json.dump(obj, fh)

    def connect(self):
        self.put_client()
        url = self.y.begin(8765)
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        self.y.finish(q["state"], "good-code")
        return q

    def test_only_a_proper_client_file_is_accepted(self):
        self.assertEqual(self.y.status(), {"client": False, "connected": False})
        for bad in ({}, [], {"web": CLIENT["installed"]}, {"installed": {"client_id": "x", "client_secret": "y"}},
                    {"installed": {"client_id": "a b.apps.googleusercontent.com", "client_secret": "GOCSPX-secret"}},
                    {"installed": {"client_id": 5, "client_secret": None}}):
            self.put_client(bad)
            self.assertIsNone(yt.load_client(self.root), bad)
        with self.assertRaises(yt.YouTubeError) as cm:
            self.y.begin(8765)
        self.assertEqual(cm.exception.code, "not_set_up")
        self.put_client()
        self.assertTrue(self.y.status()["client"])

    def test_sign_in_address_asks_only_for_upload_and_uses_pkce(self):
        self.put_client()
        url = self.y.begin(8765)
        self.assertTrue(url.startswith("https://accounts.google.com/o/oauth2/v2/auth?"))
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        self.assertEqual(q["scope"], "https://www.googleapis.com/auth/youtube.upload")
        self.assertEqual(q["redirect_uri"], "http://127.0.0.1:8765/oauth/callback")
        self.assertEqual(q["code_challenge_method"], "S256")
        self.assertGreaterEqual(len(q["state"]), 20)
        self.assertNotIn("GOCSPX", url)                              # the secret never goes into the browser

    def test_sign_in_stores_the_token_privately_and_state_is_single_use(self):
        q = self.connect()
        self.assertTrue(self.y.status()["connected"])
        form = self.box["token_calls"][0]
        self.assertEqual(form["redirect_uri"], "http://127.0.0.1:8765/oauth/callback")
        self.assertGreaterEqual(len(form["code_verifier"]), 43)
        if os.name == "posix":
            self.assertEqual(os.stat(self.y.token_path()).st_mode & 0o777, 0o600)
        for state, code in ((q["state"], "good-code"), ("nope", "good-code"), (None, "good-code"), ("", ""), (5, 5)):
            with self.assertRaises(yt.YouTubeError) as cm:
                self.y.finish(state, code)
            self.assertEqual(cm.exception.code, "auth_failed")

    def test_wrong_code_does_not_connect(self):
        self.put_client()
        q = {k: v[0] for k, v in parse_qs(urlsplit(self.y.begin(1)).query).items()}
        with self.assertRaises(yt.YouTubeError):
            self.y.finish(q["state"], "bad-code")
        self.assertFalse(self.y.status()["connected"])

    def test_upload_sends_the_whole_file_in_pieces_with_clean_text(self):
        self.connect()
        seen = []
        vid = self.y.upload(self.clip, "My <b>clip</b>\x00\x07", "line1\nline2 <x>", "private", progress=seen.append)
        self.assertEqual(vid, "vid_ABC-123")
        with open(self.clip, "rb") as fh:
            self.assertEqual(self.box["got"], fh.read())
        self.assertGreaterEqual(self.box["puts"], 3)
        self.assertEqual(self.box["meta"]["snippet"], {"title": "My bclip/b", "description": "line1\nline2 x", "categoryId": "20"})
        self.assertEqual(self.box["meta"]["status"], {"privacyStatus": "private", "selfDeclaredMadeForKids": False})
        self.assertEqual(seen[-1], 1.0)
        self.assertEqual(seen, sorted(seen))

    def test_errors_become_fixed_codes(self):
        self.connect()
        cases = (("quota", "quota"), ("fail_mid", "upload_failed"), ("bad_id", "upload_failed"),
                 ("evil_location", "upload_failed"), ("invalid_grant", "reconnect"))
        for mode, code in cases:
            self.reset(mode)
            with self.assertRaises(yt.YouTubeError) as cm:
                self.y.upload(self.clip, "t", "", "private")
            self.assertEqual(cm.exception.code, code, mode)
        self.reset()
        for args in ((self.clip, "", "", "private"), (self.clip, "t", "", "everyone"), (self.clip, "<>", "", "public")):
            with self.assertRaises(yt.YouTubeError) as cm:
                self.y.upload(*args)
            self.assertEqual(cm.exception.code, "bad_request")
        with self.assertRaises(yt.YouTubeError):
            self.y.upload(os.path.join(self.root, "missing.mp4"), "t", "", "private")

    def test_cancel_stops_between_pieces(self):
        self.connect()
        with self.assertRaises(yt.YouTubeError) as cm:
            self.y.upload(self.clip, "t", "", "private", cancelled=lambda: self.box["puts"] >= 1)
        self.assertEqual(cm.exception.code, "cancelled")
        self.assertLess(len(self.box["got"]), os.path.getsize(self.clip))

    def test_disconnect_forgets_the_token(self):
        self.connect()
        self.y.disconnect()
        self.y.disconnect()
        self.assertFalse(self.y.status()["connected"])
        with self.assertRaises(yt.YouTubeError) as cm:
            self.y.upload(self.clip, "t", "", "private")
        self.assertEqual(cm.exception.code, "not_connected")

    def test_plain_http_and_other_hosts_are_refused_outside_tests(self):
        yt.ALLOW_PLAIN_HTTP = False
        try:
            for url in (f"http://127.0.0.1:{self.box['port']}/token", "http://example.com/x", "ftp://example.com/x"):
                with self.assertRaises(yt.YouTubeError):
                    yt._request("POST", url, {})
        finally:
            yt.ALLOW_PLAIN_HTTP = True


class TestThroughTheApp(WithFake, Base):
    def setUp(self):
        self.reset()
        for name in (yt.CLIENT_FILE, yt.TOKEN_FILE):
            try:
                os.remove(os.path.join(self.store.root, name))
            except FileNotFoundError:
                pass

    def put_client(self):
        with open(os.path.join(self.store.root, yt.CLIENT_FILE), "w", encoding="utf-8") as fh:
            json.dump(CLIENT, fh)

    def connect(self):
        self.put_client()
        st, d = self.jpost("/api/youtube/connect", {})
        self.assertEqual(st, 200, d)
        q = {k: v[0] for k, v in parse_qs(urlsplit(d["url"]).query).items()}
        self.assertEqual(q["redirect_uri"], f"http://127.0.0.1:{self.port}/oauth/callback")
        return q

    def callback(self, state, code="good-code", headers=None, cookie=False):
        return self.req("GET", f"/oauth/callback?state={state}&code={code}", cookie=cookie, headers=headers)

    def made_clip(self):
        aid = self.analyze()
        a = self.jget("/api/analysis/" + aid)[1]
        c = a["clips"][0]
        st, d = self.jpost("/api/render", {"id": aid, "run": a["run"], "items": [
            {"id": c["id"], "start": c["start"], "end": c["start"] + 6, "layout": "blur", "crop_x": 0.5, "title": "up"}]})
        self.assertEqual(st, 200, d)
        job = self.wait_job()
        self.assertEqual(job["state"], "done", job)
        return aid, job["result"]["files"][0]["file"]

    def test_state_says_what_is_set_up(self):
        self.assertEqual(self.jget("/api/state")[1]["youtube"], {"client": False, "connected": False})
        st, d = self.jpost("/api/youtube/connect", {})
        self.assertEqual((st, d["error"]), (400, "yt_not_set_up"))
        self.put_client()
        self.assertEqual(self.jget("/api/state")[1]["youtube"], {"client": True, "connected": False})

    def test_callback_from_google_works_without_cookie_but_only_with_our_state(self):
        q = self.connect()
        # Google's redirect: no session cookie, marked cross-site by the browser
        st, _, body = self.callback("wrong-state", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(st, 400)
        self.assertFalse(self.jget("/api/state")[1]["youtube"]["connected"])
        st, hdr, body = self.callback(q["state"], headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(st, 200)
        self.assertIn("content-security-policy", hdr)
        self.assertNotIn(b"refresh-", body)
        self.assertNotIn(b"good-code", body)
        self.assertTrue(self.jget("/api/state")[1]["youtube"]["connected"])
        self.assertEqual(self.callback(q["state"])[0], 400)                    # the state works once
        tok = read(os.path.join(self.store.root, yt.TOKEN_FILE))
        self.assertNotIn(tok, json.dumps(self.jget("/api/state")[1]))          # the token never reaches the page

    def test_callback_and_connect_are_refused_for_remote_visitors_and_other_hosts(self):
        q = self.connect()
        self.assertEqual(self.callback(q["state"], headers={"X-Forwarded-For": "100.1.2.3"})[0], 403)
        self.assertEqual(self.callback(q["state"], headers={"Host": "evil.example"})[0], 403)
        self.assertFalse(self.jget("/api/state")[1]["youtube"]["connected"])
        for path in ("/api/youtube/connect", "/api/youtube/disconnect"):
            st, _, body = self.req("POST", path, {}, headers={"X-Forwarded-For": "100.1.2.3"})
            self.assertEqual((st, json.loads(body)["error"]), (403, "local_only"))
        # other cross-site requests are still refused: the exception is for this one address only
        self.assertEqual(self.req("GET", "/api/state", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.req("POST", "/oauth/callback?state=x&code=y", {}, headers={"Sec-Fetch-Site": "cross-site"})[0], 403)

    def test_upload_job_sends_the_clip_and_reports_the_video(self):
        aid, name = self.made_clip()
        st, d = self.jpost("/api/youtube/upload", {"id": aid, "file": name, "title": "t"})
        self.assertEqual((st, d["error"]), (400, "yt_not_set_up"))
        self.put_client()
        st, d = self.jpost("/api/youtube/upload", {"id": aid, "file": name, "title": "t"})
        self.assertEqual((st, d["error"]), (400, "yt_not_connected"))
        q = self.connect()
        self.assertEqual(self.callback(q["state"])[0], 200)
        st, d = self.jpost("/api/youtube/upload", {"id": aid, "file": name, "title": "എന്റെ ക്ലിപ്പ്", "description": "വിവരണം\n#Shorts",
                                                   "privacy": "unlisted"})
        self.assertEqual(st, 200, d)
        job = self.wait_job()
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(job["result"], {"analysis": aid, "file": name, "video": "vid_ABC-123", "privacy": "unlisted"})
        path = os.path.join(self.store.clips_dir(aid), name)
        with open(path, "rb") as fh:
            self.assertEqual(self.box["got"], fh.read())
        self.assertEqual(self.box["meta"]["snippet"]["title"], "എന്റെ ക്ലിപ്പ്")
        self.assertEqual(self.box["meta"]["status"]["privacyStatus"], "unlisted")
        # Google's own error text never reaches the page
        self.reset("quota")
        self.jpost("/api/youtube/upload", {"id": aid, "file": name, "title": "t"})
        job = self.wait_job()
        self.assertEqual((job["state"], job["code"]), ("error", "yt_quota"))
        self.assertNotIn("script", json.dumps(job))
        # disconnect
        st, d = self.jpost("/api/youtube/disconnect", {})
        self.assertEqual((st, d), (200, {"client": True, "connected": False}))

    def test_upload_takes_only_a_clip_of_this_analysis(self):
        aid, name = self.made_clip()
        q = self.connect()
        self.callback(q["state"])
        good = {"id": aid, "file": name, "title": "t"}
        for bad in ({"file": "../" + name}, {"file": "..\\x.mp4"}, {"file": "/etc/passwd"}, {"file": ".hidden.mp4"},
                    {"file": "missing.mp4"}, {"file": 5}, {"title": ""}, {"title": "x" * 101}, {"title": 7},
                    {"description": "x" * 4501}, {"description": 3}, {"privacy": "everyone"}, {"id": "nope"},
                    {"id": "0123456789ab"}):
            st, d = self.jpost("/api/youtube/upload", {**good, **bad})
            self.assertIn(st, (400, 404), bad)
        self.assertIsNone(self.box["meta"])                                    # nothing was sent anywhere


class TestInBrowser(WithFake, Base):
    def test_upload_from_the_done_screen(self):
        from playwright.sync_api import sync_playwright
        with open(os.path.join(self.store.root, yt.CLIENT_FILE), "w", encoding="utf-8") as fh:
            json.dump(CLIENT, fh)
        with open(os.path.join(self.store.root, yt.TOKEN_FILE), "w", encoding="utf-8") as fh:
            fh.write("refresh-ABCDEFGHIJ")
        self.reset()
        aid = self.analyze()
        logs = []
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_context(viewport={"width": 1100, "height": 900}).new_page()
            pg.on("console", lambda m: logs.append((m.type, m.text)) if m.type in ("error", "warning") else None)
            pg.on("pageerror", lambda e: logs.append(("pageerror", str(e))))
            pg.goto(f"http://127.0.0.1:{self.port}/?t=test-token-123")
            pg.wait_for_selector("#s-home:not([hidden])")
            # settings show that YouTube is connected
            pg.click("#open-settings")
            pg.wait_for_selector("#settings[open]")
            pg.click("#ytbox summary")
            self.assertTrue(pg.is_disabled("#yt-connect"))
            self.assertTrue(pg.is_visible("#yt-disconnect"))
            pg.click("#settings-close")
            pg.evaluate(f"location.hash = '#/review/{aid}'")
            pg.wait_for_selector("#s-review:not([hidden])")
            pg.click("#rv-none")
            pg.locator(".clip .pick").first.dispatch_event("click")
            pg.click("#rv-go")
            pg.wait_for_selector("#s-make:not([hidden])")
            pg.fill("#title", "Boss fight")
            pg.click("#mk-go")
            pg.wait_for_selector("#s-done:not([hidden])", timeout=120000)
            row = pg.locator("#dn-list li").first
            row.locator("button").first.click()                               # Upload to YouTube
            pg.wait_for_selector("#ytdlg[open]")
            self.assertEqual(pg.input_value("#yt-title"), "Boss fight")
            self.assertIn("#Shorts", pg.input_value("#yt-desc"))
            pg.fill("#yt-game", "Astro Bot")
            desc = pg.input_value("#yt-desc")
            self.assertIn("Astro Bot", desc)
            self.assertIn("#AstroBot", desc)
            pg.fill("#yt-desc", "my own words")                               # the user's text is left alone afterwards
            pg.fill("#yt-game", "Astro Bot 2")
            self.assertEqual(pg.input_value("#yt-desc"), "my own words")
            self.assertEqual(pg.input_value("#yt-privacy"), "private")
            pg.fill("#yt-title", "")
            pg.click("#yt-go")
            self.assertNotEqual(pg.inner_text("#yt-msg"), "")
            pg.fill("#yt-title", "Boss fight")
            pg.click("#yt-go")
            pg.wait_for_selector("#s-done:not([hidden]) a.done-link", timeout=60000)
            link = pg.locator("a.done-link")
            self.assertEqual(link.get_attribute("href"), "https://studio.youtube.com/video/vid_ABC-123/edit")
            self.assertEqual(link.get_attribute("rel"), "noopener noreferrer")
            b.close()
        self.assertEqual(self.box["meta"]["snippet"]["title"], "Boss fight")
        self.assertEqual(self.box["meta"]["snippet"]["description"], "my own words")
        self.assertEqual(self.box["meta"]["status"]["privacyStatus"], "private")
        self.assertEqual(logs, [])


class TestStaticRules(unittest.TestCase):
    def test_real_addresses_are_google_over_https_and_only_standard_library(self):
        src = read(os.path.join(ROOT, "clipfinder_youtube.py"))
        urls = sorted(set(re.findall(r"https://[^\s\"')]+", src)))
        self.assertEqual(re.findall(r"http://[^\s\"')]+", src), ["http://127.0.0.1:{int(port"])   # the way back to this program
        self.assertEqual(urls, ["https://accounts.google.com/o/oauth2/v2/auth", "https://oauth2.googleapis.com/token",
                                "https://www.googleapis.com/auth/youtube.upload",
                                "https://www.googleapis.com/upload/youtube/v3/videos"])
        self.assertIn("ALLOW_PLAIN_HTTP = False", src)
        mods = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                mods |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module.split(".")[0])
        self.assertLessEqual(mods, {"__future__", "base64", "hashlib", "http", "json", "os", "re", "secrets", "ssl", "sys",
                                    "threading", "time", "urllib"})
        for banned in ("subprocess", "eval(", "exec(", "shell", "verify_mode", "CERT_NONE", "_create_unverified_context"):
            self.assertNotIn(banned, src)

    def test_every_upload_error_has_a_sentence_in_both_languages(self):
        import clipfinder_web as w
        js = read(os.path.join(ROOT, "web", "app.js"))
        for code in set(w.YT_CODES.values()):
            self.assertEqual(len(re.findall(r"^\s{8}%s: \"" % code, js, re.M)), 2, code)

    def test_connector_has_no_upload_tool(self):
        mcp = read(os.path.join(ROOT, "clipfinder_mcp.py"))
        self.assertNotIn("clipfinder_youtube", mcp)
        self.assertNotIn("upload", " ".join(re.findall(r'\{"name": "([a-z_]+)"', mcp)))

if __name__ == "__main__":
    unittest.main()
