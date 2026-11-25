"""Tests for the local AI helper and the "newer version?" check. Ollama and GitHub are played by small stand-in
servers on this computer.  Run:  python3 -m unittest discover -s tests"""
import ast
import base64
import http.server
import json
import os
import re
import sys
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipfinder as cf  # noqa: E402
import clipfinder_ai as ai  # noqa: E402
import clipfinder_update as upd  # noqa: E402
import clipfinder_web as w  # noqa: E402
from test_web import Base, read  # noqa: E402


class Fake(http.server.BaseHTTPRequestHandler):
    box = None

    def log_message(self, *a):
        pass

    def _reply(self, status, obj):
        data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        b = self.box
        if self.path == "/api/tags":
            return self._reply(200, {"models": [{"name": n} for n in b["models"]] + [{"name": "bad name!"}, {"x": 1}, 7]})
        if self.path == "/version.json":
            return self._reply(b["version_status"], b["version_body"])
        self._reply(404, {})

    def do_POST(self):
        b = self.box
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode())
        b["chat"] = body
        if b["mode"] == "error":
            return self._reply(500, b"<html>boom</html>")
        if b["mode"] == "garbage":
            return self._reply(200, {"message": {"content": "I think the title should be..."}})
        answer = b.get("answer") or {"title": "Epic <b>boss</b> fight\nwin", "description": "The player beats the boss.\x07 <script>",
                                     "hashtags": ["Astro Bot", "#boss fight!", "ps5", 5, "x" * 80]}
        self._reply(200, {"message": {"role": "assistant", "content": json.dumps(answer)}})


def start_fake():
    box = {"models": ["gemma3:4b", "llava:latest"], "mode": "ok", "chat": None, "answer": None,
           "version_status": 200, "version_body": {"version": "9.9.9"}}
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), type("H", (Fake,), {"box": box}))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, box


class WithFake(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake, cls.box = start_fake()
        port = cls.fake.server_address[1]
        cls.saved = (ai.PORT, upd.VERSION_URL, upd.ALLOW_PLAIN_HTTP)
        ai.PORT = port
        upd.VERSION_URL = f"http://127.0.0.1:{port}/version.json"
        upd.ALLOW_PLAIN_HTTP = True

    @classmethod
    def tearDownClass(cls):
        ai.PORT, upd.VERSION_URL, upd.ALLOW_PLAIN_HTTP = cls.saved
        cls.fake.shutdown()
        cls.fake.server_close()
        super().tearDownClass()

    def setUp(self):
        self.box.update(models=["gemma3:4b", "llava:latest"], mode="ok", chat=None, answer=None,
                        version_status=200, version_body={"version": "9.9.9"})
        upd._cache.update(at=0.0, latest=None)


class TestAiModule(WithFake):
    def test_lists_only_well_formed_model_names(self):
        self.assertEqual(ai.status(), {"running": True, "models": ["gemma3:4b", "llava:latest"], "local": False, "local_on": False})

    def test_not_running_is_reported_not_raised(self):
        port, ai.PORT = ai.PORT, 1
        try:
            self.assertEqual(ai.status(), {"running": False, "models": [], "local": False, "local_on": False})
            with self.assertRaises(ai.AiError) as cm:
                ai.describe("gemma3:4b", b"\xff\xd8x", "", "")
            self.assertEqual(cm.exception.code, "not_running")
        finally:
            ai.PORT = port

    def test_description_is_cleaned_and_gets_hashtags(self):
        out = ai.describe("gemma3:4b", b"\xff\xd8picture", "Astro Bot", "my title", "en")
        self.assertEqual(out["title"], "Epic bboss/b fight win")
        self.assertEqual(out["description"], "The player beats the boss. script\n\n#Shorts #AstroBot #bossfight #ps5 #" + "x" * 30 + " #PS5 #Gaming")
        sent = self.box["chat"]
        self.assertEqual(sent["model"], "gemma3:4b")
        self.assertIs(sent["stream"], False)
        self.assertEqual(base64.b64decode(sent["messages"][0]["images"][0]), b"\xff\xd8picture")
        self.assertIn("Astro Bot", sent["messages"][0]["content"])
        self.assertIn("English", sent["messages"][0]["content"])
        self.assertIn("Malayalam", ai.describe("gemma3:4b", b"\xff\xd8", "", "", "ml") and self.box["chat"]["messages"][0]["content"])

    def test_bad_answers_and_bad_model_names_fail_cleanly(self):
        for mode in ("error", "garbage"):
            self.box["mode"] = mode
            with self.assertRaises(ai.AiError) as cm:
                ai.describe("gemma3:4b", b"\xff\xd8", "", "")
            self.assertEqual(cm.exception.code, "failed")
        self.box["mode"] = "ok"
        for answer in ({"title": "", "description": "x", "hashtags": []}, {"title": "x"}, [1], "text", {"title": 5, "description": 6}):
            self.box["answer"] = answer
            with self.assertRaises(ai.AiError):
                ai.describe("gemma3:4b", b"\xff\xd8", "", "")
        for bad in ("", "a b", "../x", "x" * 90, None, 5, "m;rm"):
            with self.assertRaises(ai.AiError) as cm:
                ai.describe(bad, b"\xff\xd8", "", "")
            self.assertEqual(cm.exception.code, "no_model")

    def test_only_this_computer_and_only_standard_library(self):
        src = read(os.path.join(ROOT, "clipfinder_ai.py"))
        self.assertIn('HOST = "127.0.0.1"', src)
        self.assertEqual(src.count("HTTPConnection("), 1)                                    # one place connects ...
        self.assertIn("http.client.HTTPConnection(HOST,", src)                               # ... always to HOST
        self.assertEqual(re.findall(r"https?://[^\s\"')]+", src), [])
        self.assertNotIn("HTTPSConnection", src)
        self.assertIn('"--host", HOST', src)                                                 # own server: this PC only
        self.assertIn('"--api-key", self.key', src)
        mods = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                mods |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module.split(".")[0])
        self.assertLessEqual(mods, {"__future__", "base64", "http", "json", "os", "re", "secrets", "socket",
                                    "subprocess", "sys", "threading", "time", "ctypes"})


class TestUpdateModule(WithFake):
    def test_newer_same_and_older(self):
        self.assertEqual(upd.check("1.5.0"), {"current": "1.5.0", "latest": "9.9.9", "newer": True})
        self.assertEqual(upd.check("9.9.9")["newer"], False)                 # answered from the 6 hour memory
        self.assertEqual(upd.check("10.0.0")["newer"], False)

    def test_unreadable_answers_mean_no_note(self):
        for status, body in ((404, {}), (200, {"version": "1.x"}), (200, {"version": "<script>"}), (200, {"version": 5}),
                             (200, [1]), (200, b"not json"), (200, {"version": "1.2.3.4"}), (200, b"x" * 5000)):
            upd._cache.update(at=0.0, latest=None)
            self.box.update(version_status=status, version_body=body)
            self.assertEqual(upd.check("1.5.0"), {"current": "1.5.0", "latest": None, "newer": False}, body)

    def test_real_address_is_the_projects_public_file_over_https(self):
        src = read(os.path.join(ROOT, "clipfinder_update.py"))
        self.assertEqual(sorted(set(re.findall(r"https?://[^\s\"')]+", src))),
                         ["https://github.com/Surajprem7/clipfinder.",
                          "https://raw.githubusercontent.com/Surajprem7/clipfinder./main/version.json"])
        self.assertIn("ALLOW_PLAIN_HTTP = False", src)
        for banned in ("subprocess", "open(", "exec(", "eval(", "CERT_NONE"):
            self.assertNotIn(banned, src)

    def test_version_file_matches_the_program(self):
        self.assertEqual(json.loads(read(os.path.join(ROOT, "version.json"))), {"version": cf.__version__})


class TestThroughTheApp(WithFake, Base):
    def made_clip(self):
        aid = self.analyze()
        a = self.jget("/api/analysis/" + aid)[1]
        c = a["clips"][0]
        self.jpost("/api/render", {"id": aid, "run": a["run"], "items": [
            {"id": c["id"], "start": c["start"], "end": c["start"] + 6, "layout": "blur", "crop_x": 0.5, "title": "ai"}]})
        job = self.wait_job()
        self.assertEqual(job["state"], "done", job)
        return aid, job["result"]["files"][0]["file"]

    def test_update_is_off_unless_the_real_program_turned_it_on(self):
        self.assertEqual(self.jget("/api/update")[1], {"current": cf.__version__, "latest": None, "newer": False})
        self.app.update_check = True
        try:
            self.assertEqual(self.jget("/api/update")[1], {"current": cf.__version__, "latest": "9.9.9", "newer": True})
        finally:
            self.app.update_check = False

    def test_model_choice_is_saved_and_must_be_an_installed_model(self):
        self.jpost("/api/ai/model", {"model": ""})
        self.assertEqual(self.jget("/api/ai")[1], {"running": True, "models": ["gemma3:4b", "llava:latest"], "model": "",
                                                   "local": False, "local_on": False})
        for bad in ("not-installed", "a b", 5, None, ["gemma3:4b"]):
            st, d = self.jpost("/api/ai/model", {"model": bad})
            self.assertEqual((st, d["error"]), (400, "ai_no_model"), bad)
        st, d = self.jpost("/api/ai/model", {"model": "gemma3:4b"})
        self.assertEqual((st, d["model"]), (200, "gemma3:4b"))
        self.assertEqual(w.load_prefs(self.store.root)["ai_model"], "gemma3:4b")
        self.assertEqual(self.jpost("/api/lang", {"lang": "en"})[0], 200)       # the two settings do not wipe each other
        self.assertEqual(w.load_prefs(self.store.root), {"lang": "en", "ai_model": "gemma3:4b"})
        self.jpost("/api/lang", {"lang": "ml"})
        st, _, body = self.req("POST", "/api/ai/model", {"model": "llava:latest"}, headers={"X-Forwarded-For": "100.1.2.3"})
        self.assertEqual((st, json.loads(body)["error"]), (403, "local_only"))
        self.box["models"] = ["llava:latest"]                                  # the saved model was removed from Ollama
        self.assertEqual(self.jget("/api/ai")[1]["model"], "")

    def test_describe_sends_a_picture_sheet_of_the_clip_and_returns_clean_text(self):
        aid, name = self.made_clip()
        self.jpost("/api/ai/model", {"model": ""})
        st, d = self.jpost("/api/ai/describe", {"id": aid, "file": name, "game": "Astro Bot", "title": "t"})
        self.assertEqual((st, d["error"]), (400, "ai_no_model"))
        self.jpost("/api/ai/model", {"model": "gemma3:4b"})
        st, d = self.jpost("/api/ai/describe", {"id": aid, "file": name, "game": "Astro Bot", "title": "t", "lang": "en"})
        self.assertEqual(st, 200, d)
        self.assertEqual(d["title"], "Epic bboss/b fight win")
        self.assertIn("#Shorts", d["description"])
        jpg = base64.b64decode(self.box["chat"]["messages"][0]["images"][0])
        self.assertEqual(jpg[:3], b"\xff\xd8\xff")
        self.assertLess(len(jpg), 1_500_000)
        self.box["mode"] = "error"
        st, d = self.jpost("/api/ai/describe", {"id": aid, "file": name})
        self.assertEqual((st, d["error"]), (400, "ai_failed"))
        self.assertNotIn("boom", json.dumps(d))
        self.box["mode"] = "ok"
        for bad in ({"file": "../" + name}, {"file": "missing.mp4"}, {"file": 5}, {"game": "x" * 61}, {"title": 7},
                    {"lang": "fr"}, {"id": "nope"}):
            self.box["chat"] = None
            st, d = self.jpost("/api/ai/describe", {"id": aid, "file": name, **bad})
            self.assertIn(st, (400, 404), bad)
            self.assertIsNone(self.box["chat"], bad)

    def test_every_ai_error_has_a_sentence_in_both_languages(self):
        js = read(os.path.join(ROOT, "web", "app.js"))
        for code in set(w.AI_CODES.values()):
            self.assertEqual(len(re.findall(r"^\s{8}%s: \"" % code, js, re.M)), 2, code)


FAKE_LLAMA = r'''#!/usr/bin/env python3
# stands in for llama-server: same arguments, same /health and /v1/chat/completions, checks the key
import http.server, json, sys, time
a = sys.argv
arg = lambda k: a[a.index(k) + 1]
host, port, key = arg("--host"), int(arg("--port")), arg("--api-key")
assert arg("--mmproj").endswith(".gguf") and arg("-m").endswith(".gguf") and "--no-webui" in a
t0 = time.time()
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *x): pass
    def send(self, st, obj):
        d = json.dumps(obj).encode(); self.send_response(st); self.send_header("Content-Length", str(len(d))); self.end_headers(); self.wfile.write(d)
    def do_GET(self):
        if self.path == "/health":
            return self.send(503, {"error": "Loading model"}) if time.time() - t0 < 1.0 else self.send(200, {"status": "ok"})
        self.send(404, {})
    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer " + key:
            return self.send(401, {"error": "bad key"})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        part = body["messages"][0]["content"]
        ok = part[1]["image_url"]["url"].startswith("data:image/jpeg;base64,") and body["response_format"]["type"] == "json_schema"
        ans = {"title": "Local AI title", "description": "Seen by the local model." if ok else "bad request", "hashtags": ["Spider"]}
        self.send(200, {"choices": [{"message": {"role": "assistant", "content": json.dumps(ans)}}]})
http.server.HTTPServer((host, port), H).serve_forever()
'''


class TestLocalEngine(WithFake):
    """The app's own llama.cpp: started on first use with a private key, on this PC only, stopped at the end."""

    def setUp(self):
        import tempfile
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        aid = os.path.join(self.tmp.name, "ai")
        os.makedirs(os.path.join(aid, "llama-b1"))
        os.makedirs(os.path.join(aid, "models"))
        server = os.path.join(aid, "llama-b1", "llama-server")
        with open(server, "w") as fh:
            fh.write(FAKE_LLAMA.replace("#!/usr/bin/env python3", "#!" + sys.executable))
        os.chmod(server, 0o755)
        for f in ("m.gguf", "p.gguf"):
            open(os.path.join(aid, "models", f), "wb").close()
        with open(os.path.join(aid, "ready.json"), "w") as fh:
            json.dump({"server": "llama-b1/llama-server", "model": "models/m.gguf", "mmproj": "models/p.gguf"}, fh)
        ai.configure(self.tmp.name)

    def tearDown(self):
        ai.shutdown()
        ai.configure("")
        self.tmp.cleanup()
        super().tearDown()

    def test_listed_first_started_on_use_and_stopped(self):
        st = ai.status()
        self.assertEqual(st["models"][0], ai.LOCAL_MODEL)
        self.assertTrue(st["local"])
        self.assertFalse(st["local_on"])                       # not started just by looking
        out = ai.describe(ai.LOCAL_MODEL, b"\xff\xd8pic", "Marvel's Spider-Man 2", "", "en")
        self.assertEqual(out["title"], "Local AI title")
        self.assertTrue(out["description"].startswith("Seen by the local model."))
        self.assertIsInstance(out["seconds"], int)
        self.assertTrue(ai.status()["local_on"])
        p = ai.LOCAL.proc
        ai.describe(ai.LOCAL_MODEL, b"\xff\xd8pic", "", "", "en")    # second time: the same program
        self.assertIs(ai.LOCAL.proc, p)
        ai.shutdown()
        self.assertIsNotNone(p.poll())
        self.assertFalse(ai.status()["local_on"])

    def test_without_the_key_the_server_refuses(self):
        ai.LOCAL.start()
        with self.assertRaises(ai.AiError):
            ai._call("POST", "/v1/chat/completions", {"x": 1}, port=ai.LOCAL.port)

    def test_files_outside_the_ai_folder_or_missing_are_refused(self):
        rj = os.path.join(self.tmp.name, "ai", "ready.json")
        for bad in ({"server": "../../bin/sh", "model": "models/m.gguf", "mmproj": "models/p.gguf"},
                    {"server": "/bin/sh", "model": "models/m.gguf", "mmproj": "models/p.gguf"},
                    {"server": "llama-b1/llama-server", "model": "models/none.gguf", "mmproj": "models/p.gguf"},
                    {"server": "llama-b1/llama-server"}, [1]):
            with open(rj, "w") as fh:
                json.dump(bad, fh)
            self.assertFalse(ai.LOCAL.installed(), bad)
            self.assertNotIn(ai.LOCAL_MODEL, ai.status()["models"])
            with self.assertRaises(ai.AiError) as cm:
                ai.describe(ai.LOCAL_MODEL, b"\xff\xd8", "", "")
            self.assertEqual(cm.exception.code, "no_model")

    def test_a_program_that_dies_is_reported(self):
        server = os.path.join(self.tmp.name, "ai", "llama-b1", "llama-server")
        with open(server, "w") as fh:
            fh.write("#!" + sys.executable + "\nraise SystemExit(3)\n")
        with self.assertRaises(ai.AiError) as cm:
            ai.describe(ai.LOCAL_MODEL, b"\xff\xd8", "", "")
        self.assertEqual(cm.exception.code, "failed")

    def test_lm_studio_models_are_offered_and_used(self):
        class LMS(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, obj):
                d = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(d)))
                self.end_headers()
                self.wfile.write(d)

            def do_GET(self):
                self.send({"data": [{"id": "google/gemma-3-4b"}, {"id": "bad id!"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                LMS.model = body["model"]
                self.send({"choices": [{"message": {"content": json.dumps({"title": "LM", "description": "d", "hashtags": []})}}]})

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), LMS)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        old, ai.LMSTUDIO_PORT = ai.LMSTUDIO_PORT, srv.server_address[1]
        try:
            self.assertEqual(ai.status()["models"][:2], [ai.LOCAL_MODEL, "lmstudio/google/gemma-3-4b"])
            self.assertEqual(ai.describe("lmstudio/google/gemma-3-4b", b"\xff\xd8", "", "")["title"], "LM")
            self.assertEqual(LMS.model, "google/gemma-3-4b")
        finally:
            ai.LMSTUDIO_PORT = old
            srv.shutdown()


class TestAiSetup(unittest.TestCase):
    def test_zip_with_a_path_outside_is_refused(self):
        import tempfile
        import zipfile
        import clipfinder_ai_setup as setup
        with tempfile.TemporaryDirectory() as d:
            for bad in ("../evil.txt", "/abs.txt", "a/../../x", "C:/x"):
                z = os.path.join(d, "x.zip")
                with zipfile.ZipFile(z, "w") as zf:
                    zf.writestr(bad, "x")
                with self.assertRaises(setup.SetupError):
                    setup.safe_extract(z, os.path.join(d, "out"))
            with zipfile.ZipFile(z, "w") as zf:
                zf.writestr("llama-server.exe", "x")
                zf.writestr("ggml-cpu.dll", "x")
            os.makedirs(os.path.join(d, "ok"))
            setup.safe_extract(z, os.path.join(d, "ok"))
            self.assertTrue(setup.find_server(os.path.join(d, "ok")).endswith("llama-server.exe"))

    def test_downloads_only_from_the_two_fixed_https_places_and_are_checked(self):
        src = read(os.path.join(ROOT, "clipfinder_ai_setup.py"))
        urls = set(re.findall(r"https?://[^\s\"'{}]+", src))
        self.assertTrue(all(u.startswith(("https://api.github.com/repos/ggml-org/llama.cpp/",
                                          "https://github.com/ggml-org/llama.cpp/", "https://huggingface.co/"))
                            for u in urls), urls)
        self.assertIn("sha256_of(part) != sha", src)
        self.assertIn('raise SetupError(f"no checksum published for {name}")', src)

    def test_setup_bat_offers_the_ai_and_keeps_windows_line_ends(self):
        with open(os.path.join(ROOT, "setup_windows.bat"), "rb") as fh:
            raw = fh.read()
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        self.assertIn(b'clipfinder_ai_setup.py" --data "%~dp0..\\data"', raw)
        self.assertIn(b"choice /C YN /T 20 /D Y", raw)


class TestInBrowser(WithFake, Base):
    @staticmethod
    def poll(pg, expr, timeout=20):
        import time
        end = time.time() + timeout
        while time.time() < end:
            if pg.evaluate(expr):
                return
            time.sleep(0.2)
        raise AssertionError("timed out waiting for: " + expr)

    def test_ai_fills_the_boxes_and_the_update_note_shows(self):
        from playwright.sync_api import sync_playwright
        self.app.update_check = True
        self.jpost("/api/ai/model", {"model": ""})
        aid = self.analyze()
        logs = []
        try:
            with sync_playwright() as p:
                b = p.chromium.launch()
                ctx = b.new_context(viewport={"width": 1100, "height": 900})
                ctx.grant_permissions(["clipboard-read", "clipboard-write"])
                pg = ctx.new_page()
                pg.on("console", lambda m: logs.append((m.type, m.text)) if m.type in ("error", "warning") else None)
                pg.on("pageerror", lambda e: logs.append(("pageerror", str(e))))
                pg.goto(f"http://127.0.0.1:{self.port}/?t=test-token-123")
                pg.wait_for_selector("#update-bar:not([hidden])")
                self.assertIn("9.9.9", pg.inner_text("#update-text"))
                self.assertEqual(pg.get_attribute("#update-bar a", "href"), "https://github.com/Surajprem7/clipfinder.")
                # choose the model in Settings
                pg.click("#open-settings")
                pg.click("#aibox summary")
                pg.wait_for_selector('#ai-model option[value="gemma3:4b"]', state="attached")
                pg.select_option("#ai-model", "gemma3:4b")
                self.poll(pg, "document.getElementById('ai-status').textContent.includes('gemma3:4b')")
                pg.click("#settings-close")
                pg.evaluate(f"location.hash = '#/review/{aid}'")
                pg.wait_for_selector("#s-review:not([hidden])")
                pg.click("#rv-none")
                pg.locator(".clip .pick").first.dispatch_event("click")
                pg.click("#rv-go")
                pg.wait_for_selector("#s-make:not([hidden])")
                pg.click("#mk-go")
                pg.wait_for_selector("#s-done:not([hidden])", timeout=120000)
                pg.locator("#dn-list li").first.locator("button").first.click()      # "Title and description" (YouTube not connected)
                pg.wait_for_selector("#ytdlg[open]")
                self.assertFalse(pg.is_visible("#yt-go"))
                self.assertFalse(pg.is_visible("#yt-privacy"))
                pg.fill("#yt-game", "Astro Bot")
                pg.click("#yt-ai")
                self.poll(pg, "document.getElementById('yt-title').value === 'Epic bboss/b fight win'", timeout=60)
                self.assertIn("#AstroBot", pg.input_value("#yt-desc"))
                pg.fill("#yt-game", "Other game")                                    # the AI's text is not overwritten
                self.assertIn("The player beats the boss.", pg.input_value("#yt-desc"))
                pg.click("#yt-copy")
                self.assertIn("The player beats the boss.", pg.evaluate("navigator.clipboard.readText()"))
                b.close()
        finally:
            self.app.update_check = False
        self.assertEqual(self.box["chat"]["model"], "gemma3:4b")
        self.assertEqual(logs, [])


if __name__ == "__main__":
    unittest.main()
