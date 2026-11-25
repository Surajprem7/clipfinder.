"""Tests for clipfinder_web.py: the local server, its safety rules, and the page in a real browser.

Run:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import ast
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import clipfinder as cf  # noqa: E402
import clipfinder_web as w  # noqa: E402
from sample_video import make_sample  # noqa: E402

TOKEN = "test-token-123"


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def probe_json(path):
    res = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", path],
                         capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


class Base(unittest.TestCase):
    """One server for the whole class, one analysed sample video."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.video = make_sample(os.path.join(cls.tmp.name, "sample.mp4"))
        cf.QUIET_ERRORS = True
        cls.store = w.Store(os.path.join(cls.tmp.name, "data"))
        cls.srv, cls.app = w.make_server(cls.store, 0, token=TOKEN)
        cls.port = cls.app.port
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()
        cf.QUIET_ERRORS = False

    # ---- helpers
    def req(self, method, path, body=None, headers=None, cookie=True, write_headers=True, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        h = {"Host": f"127.0.0.1:{self.port}"}
        if cookie:
            h["Cookie"] = f"{w.COOKIE}={TOKEN}"
        if method != "GET" and write_headers:
            h.update({"Origin": f"http://127.0.0.1:{self.port}", "X-CF": "1", "Content-Type": "application/json"})
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        payload = r.read()
        conn.close()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, payload

    def jget(self, path, **kw):
        st, h, d = self.req("GET", path, **kw)
        return st, json.loads(d.decode("utf-8")) if d[:1] in (b"{", b"[") else d

    def jpost(self, path, body=None, **kw):
        st, h, d = self.req("POST", path, body if body is not None else {}, **kw)
        return st, json.loads(d.decode("utf-8"))

    def wait_job(self, timeout=120):
        end = time.time() + timeout
        while time.time() < end:
            job = self.jget("/api/job")[1]["job"]
            if job and job["state"] != "running":
                return job
            time.sleep(0.2)
        self.fail("job did not finish")

    def analyze(self, **extra):
        st, d = self.jpost("/api/analyze", {"kind": "file", "path": self.video, **extra})
        self.assertEqual(st, 200, d)
        if "existing" in d:
            return d["existing"]
        job = self.wait_job()
        self.assertEqual(job["state"], "done", job)
        return job["result"]["analysis"]


class TestAccess(Base):
    def test_listens_on_loopback_only(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")
        # and the listening socket really refuses another interface name
        self.assertNotIn(self.srv.server_address[0], ("0.0.0.0", "", "::"))

    def test_no_session_is_refused(self):
        self.assertEqual(self.req("GET", "/api/state", cookie=False)[0], 403)
        self.assertEqual(self.req("GET", "/", cookie=False)[0], 403)
        self.assertEqual(self.req("GET", "/app.js", cookie=False)[0], 403)
        self.assertEqual(self.req("GET", "/api/state", cookie=False, headers={"Cookie": f"{w.COOKIE}=wrong"})[0], 403)
        self.assertEqual(self.req("GET", "/?t=wrong", cookie=False)[0], 403)

    def test_login_link_sets_a_strict_cookie(self):
        st, h, _ = self.req("GET", f"/?t={TOKEN}", cookie=False)
        self.assertEqual(st, 302)
        self.assertEqual(h["location"], "/")
        cookie = h["set-cookie"]
        self.assertIn(f"{w.COOKIE}={TOKEN}", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn(TOKEN, h["location"])

    def test_host_header_must_be_ours(self):
        for host in ("evil.example", "127.0.0.1", f"127.0.0.1:{self.port + 1}", f"evil.example:{self.port}", ""):
            self.assertEqual(self.req("GET", "/api/state", headers={"Host": host})[0], 403, host)
        self.assertEqual(self.req("GET", "/api/state", headers={"Host": f"localhost:{self.port}"})[0], 200)

    def test_other_localhost_pages_are_refused_by_fetch_metadata(self):
        for site in ("cross-site", "same-site"):
            self.assertEqual(self.req("GET", "/api/state", headers={"Sec-Fetch-Site": site})[0], 403, site)
        for site in ("same-origin", "none"):
            self.assertEqual(self.req("GET", "/api/state", headers={"Sec-Fetch-Site": site})[0], 200, site)

    def test_changes_need_origin_and_custom_header_and_json(self):
        body = {"id": "0" * 12}
        ok_origin = f"http://127.0.0.1:{self.port}"
        self.assertEqual(self.req("POST", "/api/cancel", {}, write_headers=False)[0], 403)
        self.assertEqual(self.req("POST", "/api/cancel", {}, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.req("POST", "/api/cancel", {}, headers={"Origin": f"http://127.0.0.1:{self.port + 1}"})[0], 403)
        self.assertEqual(self.req("POST", "/api/cancel", {}, headers={"X-CF": "0"})[0], 403)
        self.assertEqual(self.req("POST", "/api/cancel", {}, write_headers=False, headers={"Origin": ok_origin})[0], 403)
        self.assertEqual(self.req("POST", "/api/cancel", {}, headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.req("PUT", "/api/upload?name=a.mp4", raw=b"x", write_headers=False)[0], 403)
        self.assertEqual(self.req("POST", "/api/cancel", body)[0], 200)

    def test_chunked_bodies_are_refused(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        s.sendall((f"POST /api/cancel HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nCookie: {w.COOKIE}={TOKEN}\r\n"
                   f"Origin: http://127.0.0.1:{self.port}\r\nX-CF: 1\r\nContent-Type: application/json\r\n"
                   "Transfer-Encoding: chunked\r\nContent-Length: 2\r\n\r\n2\r\n{}\r\n0\r\n\r\n").encode())
        reply = s.recv(4096)
        s.close()
        self.assertIn(b" 400 ", reply.split(b"\r\n")[0])

    def test_never_sends_cors_headers_and_refuses_preflight(self):
        st, h, _ = self.req("GET", "/api/state")
        self.assertFalse([k for k in h if k.startswith("access-control-")])
        st, h, _ = self.req("OPTIONS", "/api/state", write_headers=False)
        self.assertIn(st, (405, 501))
        self.assertFalse([k for k in h if k.startswith("access-control-")])

    def test_page_has_strict_policy_headers(self):
        st, h, body = self.req("GET", "/")
        self.assertEqual(st, 200)
        csp = h["content-security-policy"]
        for part in ("default-src 'none'", "script-src 'self'", "style-src 'self'", "frame-ancestors 'none'",
                     "base-uri 'none'", "form-action 'none'", "connect-src 'self'"):
            self.assertIn(part, csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotIn("unsafe-eval", csp)
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertEqual(h["referrer-policy"], "no-referrer")
        self.assertEqual(h["x-frame-options"], "DENY")
        self.assertEqual(h["cache-control"], "no-store")
        self.assertIn(b"clipfinder", body)

    def test_only_the_three_page_files_are_served(self):
        self.assertEqual(self.req("GET", "/app.js")[0], 200)
        self.assertEqual(self.req("GET", "/app.css")[0], 200)
        for bad in ("/clipfinder.py", "/../clipfinder.py", "/%2e%2e/clipfinder.py", "/web/app.js", "/index.html",
                    "/web/../clipfinder_web.py", "/%2e%2e%2fclipfinder.py", "//etc/passwd", "/app.js/../../etc/passwd"):
            self.assertEqual(self.req("GET", bad)[0], 404, bad)


class TestInputChecks(Base):
    def test_bad_json_and_sizes(self):
        self.assertEqual(self.req("POST", "/api/analyze", raw=b"{not json")[0], 400)
        self.assertEqual(self.req("POST", "/api/analyze", raw=b"[1,2]")[0], 400)
        self.assertEqual(self.req("POST", "/api/analyze", raw=b"\xff\xfe")[0], 400)
        self.assertEqual(self.req("POST", "/api/analyze", raw=b"{" + b" " * 70000 + b"}")[0], 413)
        st, h, d = self.req("POST", "/api/analyze", write_headers=True, headers={"Content-Length": "abc"}, raw=b"{}")
        self.assertIn(st, (400, 411))

    def test_ids_are_twelve_hex_digits_only(self):
        for bad in ("../../etc", "ABCDEF123456", "abc", "0" * 11, "0" * 13, "%2e%2e", "0" * 12 + "/x"):
            st, _, _ = self.req("GET", f"/api/analysis/{bad}")
            self.assertEqual(st, 404, bad)
            self.assertEqual(self.req("GET", f"/media/{bad}")[0], 404, bad)
            self.assertEqual(self.req("GET", f"/thumb/{bad}/1.jpg")[0], 404, bad)
            self.assertEqual(self.req("GET", f"/clip/{bad}/a.mp4")[0], 404, bad)
        self.assertEqual(self.req("GET", f"/api/analysis/{'0' * 12}")[0], 404)

    def test_analyze_rejects_bad_sources(self):
        cases = [
            ({"kind": "file", "path": os.path.join(self.tmp.name, "missing.mp4")}, "file_missing"),
            ({"kind": "file", "path": ""}, "bad_path"),
            ({"kind": "file", "path": 5}, "bad_path"),
            ({"kind": "file", "path": "a\x00b.mp4"}, "bad_path"),
            ({"kind": "file", "path": "x" * 5000}, "bad_path"),
            ({"kind": "link", "url": "https://evil.example/watch?v=abcdefghijk"}, "bad_link"),
            ({"kind": "link", "url": "http://www.youtube.com/watch?v=abcdefghijk"}, "bad_link"),
            ({"kind": "link", "url": "file:///etc/passwd"}, "bad_link"),
            ({"kind": "link", "url": "https://www.youtube.com.evil.example/watch?v=abcdefghijk"}, "bad_link"),
            ({"kind": "link", "url": 7}, "bad_link"),
            ({"kind": "other"}, "bad_request"),
            ({}, "bad_request"),
            ({"kind": "file", "path": self.video, "preset": "nope"}, "bad_request"),
            ({"kind": "file", "path": self.video, "sensitivity": 101}, "bad_request"),
            ({"kind": "file", "path": self.video, "sensitivity": -1}, "bad_request"),
            ({"kind": "file", "path": self.video, "sensitivity": True}, "bad_request"),
            ({"kind": "file", "path": self.video, "sensitivity": "50"}, "bad_request"),
            ({"kind": "file", "path": self.video, "sensitivity": 50.5}, "bad_request"),
        ]
        for body, code in cases:
            st, d = self.jpost("/api/analyze", body)
            self.assertEqual(st, 400, (body, d))
            self.assertEqual(d["error"], code, body)

    def test_only_video_extensions_are_analysed(self):
        txt = os.path.join(self.tmp.name, "notes.txt")
        with open(txt, "w") as fh:
            fh.write("hello")
        st, d = self.jpost("/api/analyze", {"kind": "file", "path": txt})
        self.assertEqual((st, d["error"]), (400, "bad_type"))
        st, d = self.jpost("/api/analyze", {"kind": "file", "path": self.tmp.name})
        self.assertEqual((st, d["error"]), (400, "file_missing"))

    def test_nothing_was_started_by_rejected_requests(self):
        before = self.app.jobs.counter
        self.jpost("/api/analyze", {"kind": "file", "path": "/nonexistent.mp4"})
        self.jpost("/api/analyze", {"kind": "link", "url": "https://evil.example/x"})
        self.jpost("/api/render", {"id": "0" * 12, "run": "x", "items": []})
        self.assertEqual(self.app.jobs.counter, before)


class TestWorkflow(Base):
    def test_full_flow_analyse_review_render(self):
        aid = self.analyze()
        st, a = self.jget(f"/api/analysis/{aid}")
        self.assertEqual(st, 200)
        self.assertEqual(a["name"], "sample")
        self.assertTrue(a["source_ok"])
        self.assertGreaterEqual(len(a["clips"]), 2)
        for c in a["clips"]:
            self.assertGreater(c["end"], c["start"])
            self.assertIn(c["level"], ("high", "mid", "low"))
            self.assertTrue(set(c["tags"]) <= set(cf.DETECTORS))
        # analysing the same file again opens the saved result instead of redoing the work
        st, d = self.jpost("/api/analyze", {"kind": "file", "path": self.video})
        self.assertEqual(d, {"existing": aid})
        # it shows in the library and the state
        st, s = self.jget("/api/state")
        self.assertIn(aid, [x["id"] for x in s["library"]])
        self.assertEqual(s["data_dir"], self.store.root)
        # pictures
        st, h, jpg = self.req("GET", f"/thumb/{aid}/{a['clips'][0]['id']}.jpg")
        self.assertEqual((st, h["content-type"]), (200, "image/jpeg"))
        self.assertTrue(jpg.startswith(b"\xff\xd8"))
        self.assertEqual(self.req("GET", f"/thumb/{aid}/9999.jpg")[0], 404)
        self.assertEqual(self.req("GET", f"/thumb/{aid}/x.jpg")[0], 404)
        # render two clips, one trimmed, one cropped
        self.assertFalse(a["old"])                       # made by this version: no need to look again
        self.assertEqual(max(c["pct"] for c in a["clips"]), 100)   # the best moment of the stream fills the bar
        self.assertEqual(w.level_of(0.2, 1, 9), "high")         # busy stream: its best third still counts high
        self.assertEqual(w.level_of(0.2, 5, 9), "mid")
        self.assertEqual(w.level_of(0.2, 9, 9), "low")
        self.assertEqual(w.level_of(0.7, 9, 9), "high")         # a strong moment is high wherever it is
        c1, c2 = a["clips"][0], a["clips"][1]
        items = [
            {"id": c1["id"], "start": c1["start"] + 2, "end": c1["start"] + 12, "layout": "follow", "crop_x": 0.5,
             "title": "ആദ്യത്തെ ക്ലിപ്പ്"},
            {"id": c2["id"], "start": c2["start"], "end": c2["start"] + 8, "layout": "crop", "crop_x": 0.2, "title": "../../evil"},
        ]
        st, d = self.jpost("/api/render", {"id": aid, "run": a["run"], "items": items})
        self.assertEqual(st, 200, d)
        job = self.wait_job()
        self.assertEqual(job["state"], "done", job)
        files = job["result"]["files"]
        self.assertEqual(len(files), 2)
        clips_dir = os.path.join(self.store.adir(aid), "clips")
        for f in files:
            path = os.path.join(clips_dir, f["file"])
            self.assertEqual(os.path.dirname(os.path.realpath(path)), os.path.realpath(clips_dir))
            streams = probe_json(path)["streams"]
            v = next(s for s in streams if s["codec_type"] == "video")
            self.assertEqual((v["width"], v["height"]), (1080, 1920))
            self.assertTrue(any(s["codec_type"] == "audio" for s in streams))
            st, h, body = self.req("GET", f"/clip/{aid}/" + __import__("urllib.parse").parse.quote(f["file"]))
            self.assertEqual((st, h["content-type"]), (200, "video/mp4"))
            self.assertEqual(len(body), f["size"])
        self.assertNotIn("..", " ".join(f["file"] for f in files))
        self.assertGreaterEqual(len(self.jget(f"/api/analysis/{aid}")[1]["made"]), len(files))

    def test_clip_route_cannot_leave_the_clips_folder(self):
        aid = self.analyze()
        os.makedirs(os.path.join(self.store.adir(aid), "clips"), exist_ok=True)
        for bad in ("..%2fmeta.json", "..%2fclips%2fx.mp4", "%2e%2e%2fmeta.json", "meta.json", ".hidden.mp4",
                    "a%5cb.mp4", "..%5c..%5cmeta.json", "x.mp4%00.txt", "nothere.mp4"):
            self.assertEqual(self.req("GET", f"/clip/{aid}/{bad}")[0], 404, bad)

    def test_media_supports_ranges(self):
        aid = self.analyze()
        size = os.path.getsize(self.video)
        st, h, body = self.req("GET", f"/media/{aid}", headers={"Range": "bytes=0-99"})
        self.assertEqual((st, len(body), h["content-range"]), (206, 100, f"bytes 0-99/{size}"))
        self.assertEqual(h["accept-ranges"], "bytes")
        st, h, body = self.req("GET", f"/media/{aid}", headers={"Range": "bytes=-50"})
        self.assertEqual((st, len(body)), (206, 50))
        st, h, body = self.req("GET", f"/media/{aid}", headers={"Range": f"bytes={size - 10}-"})
        self.assertEqual((st, len(body)), (206, 10))
        st, h, body = self.req("GET", f"/media/{aid}", headers={"Range": f"bytes=0-{size * 5}"})
        self.assertEqual((st, len(body)), (206, size))
        self.assertEqual(self.req("GET", f"/media/{aid}", headers={"Range": f"bytes={size}-"})[0], 416)
        self.assertEqual(self.req("GET", f"/media/{aid}", headers={"Range": "bytes=9-3"})[0], 416)
        self.assertEqual(self.req("GET", f"/media/{aid}", headers={"Range": "bytes=-0"})[0], 416)
        st, h, body = self.req("GET", f"/media/{aid}")
        self.assertEqual((st, len(body)), (200, size))
        self.assertEqual(h["content-type"], "video/mp4")

    def test_presets_and_sensitivity_change_the_list_and_are_remembered(self):
        aid = self.analyze()
        before = self.jget(f"/api/analysis/{aid}")[1]
        st, d = self.jpost("/api/redetect", {"id": aid, "preset": "slowmo", "sensitivity": 30})
        self.assertEqual(st, 200)
        self.assertEqual(self.wait_job()["state"], "done")
        after = self.jget(f"/api/analysis/{aid}")[1]
        self.assertEqual((after["preset"], after["sensitivity"]), ("slowmo", 30))
        self.assertNotEqual(after["run"], before["run"])
        self.assertTrue(all("slow_motion" in c["tags"] for c in after["clips"]))
        self.jpost("/api/redetect", {"id": aid, "preset": "all", "sensitivity": 50})
        self.wait_job()
        st, d = self.jpost("/api/redetect", {"id": aid, "preset": "zzz", "sensitivity": 50})
        self.assertEqual((st, d["error"]), (400, "bad_request"))
        st, d = self.jpost("/api/redetect", {"id": "f" * 12, "preset": "all", "sensitivity": 50})
        self.assertEqual(st, 404)

    def test_sensitivity_slider_midpoint_equals_engine_defaults(self):
        p = w.make_params("all", 50)
        d = cf.Params()
        self.assertEqual((p.z_min, p.min_score, p.tag_min), (d.z_min, d.min_score, d.tag_min))
        lo, hi = w.make_params("all", 0), w.make_params("all", 100)
        self.assertLess(lo.z_min, hi.z_min)
        self.assertLess(lo.min_score, hi.min_score)
        cf.normalize_params(lo)
        cf.normalize_params(hi)

    def test_render_refuses_old_lists_and_bad_values(self):
        aid = self.analyze()
        a = self.jget(f"/api/analysis/{aid}")[1]
        c = a["clips"][0]
        good = {"id": c["id"], "start": c["start"], "end": c["start"] + 5, "layout": "blur", "crop_x": 0.5, "title": "t"}
        st, d = self.jpost("/api/render", {"id": aid, "run": "deadbeef", "items": [good]})
        self.assertEqual((st, d["error"]), (409, "stale"))
        bad_items = [
            [], "x", [5], [{**good, "id": True}], [{**good, "id": "1"}], [{**good, "layout": "cam"}],
            [{**good, "start": "0"}], [{**good, "end": None}], [{**good, "crop_x": True}], [{**good, "title": 5}],
            [good] * 41,
        ]
        for items in bad_items:
            st, d = self.jpost("/api/render", {"id": aid, "run": a["run"], "items": items})
            self.assertEqual(st, 400, items)
        # NaN / Infinity are not valid JSON but Python's parser accepts them: they must still be refused
        raw = json.dumps({"id": aid, "run": a["run"], "items": [good]}).replace(str(c["start"] + 5), "NaN", 1).encode()
        st, h, _ = self.req("POST", "/api/render", raw=raw)
        self.assertEqual(st, 400)
        # values the engine refuses show up as a job error, not a crash
        st, d = self.jpost("/api/render", {"id": aid, "run": a["run"], "items": [{**good, "end": good["start"] + 0.2}]})
        self.assertEqual(st, 200)
        job = self.wait_job()
        self.assertEqual(job["state"], "error")
        st, d = self.jpost("/api/render", {"id": aid, "run": a["run"], "items": [{**good, "id": 999}]})
        job = self.wait_job() if st == 200 else {"state": "error"}
        self.assertEqual(job["state"], "error")

    def test_one_job_at_a_time_and_cancel(self):
        aid = self.analyze()
        started = threading.Event()

        def slow(progress):
            started.set()
            for _ in range(200):
                if cf.CANCEL.is_set():
                    raise cf.Cancelled()
                progress("read", 0.5)
                time.sleep(0.05)

        self.app.jobs.start("analyze", aid, slow)
        self.assertTrue(started.wait(5))
        st, d = self.jpost("/api/redetect", {"id": aid, "preset": "all", "sensitivity": 50})
        self.assertEqual((st, d["error"]), (409, "busy"))
        st, d = self.jpost("/api/cancel")
        self.assertEqual((st, d), (200, {"cancelled": True}))
        self.assertEqual(self.wait_job()["state"], "cancelled")
        self.assertEqual(self.jpost("/api/cancel")[1], {"cancelled": False})

    def test_unexpected_errors_do_not_leak_details(self):
        def boom(progress):
            raise RuntimeError("secret internal detail /home/x")

        import io
        old = sys.stderr
        sys.stderr = io.StringIO()
        try:
            self.app.jobs.start("analyze", "a" * 12, boom)
            job = self.wait_job()
        finally:
            sys.stderr = old
        self.assertEqual((job["state"], job["code"]), ("error", "internal"))
        self.assertNotIn("secret", json.dumps(job))

    def test_picker_and_open_folder_hooks(self):
        aid = self.analyze()
        try:
            self.app.picker = lambda: None
            self.assertEqual(self.jpost("/api/pick")[1], {"unavailable": True})
            self.app.picker = lambda: ""
            self.assertEqual(self.jpost("/api/pick")[1], {"path": ""})
            self.app.picker = lambda: self.video
            self.assertEqual(self.jpost("/api/pick")[1], {"path": os.path.realpath(self.video)})
            self.app.picker = lambda: "/etc/passwd"
            self.assertEqual(self.jpost("/api/pick")[0], 400)
            seen = []
            self.app.open_folder = lambda p: seen.append(p) or True
            st, d = self.jpost("/api/open-folder", {"id": aid})
            self.assertEqual((st, d["opened"]), (200, True))
            self.assertEqual(seen[-1], os.path.join(self.store.adir(aid), "clips"))
            self.jpost("/api/open-folder", {})
            self.assertEqual(seen[-1], self.store.videos)
            self.assertEqual(self.jpost("/api/open-folder", {"id": "../../etc"})[0], 404)
            self.assertEqual(len(seen), 2)
        finally:
            self.app.picker = w.default_picker
            self.app.open_folder = w.default_open_folder


class TestUpload(Base):
    def put(self, name, data, **kw):
        return self.req("PUT", f"/api/upload?name={name}", raw=data, headers={"Content-Type": "application/octet-stream"}, **kw)

    def leftovers(self):
        return [n for n in os.listdir(self.store.videos) if n.endswith(".part")]

    def test_upload_is_saved_with_a_safe_name(self):
        data = read_bytes(self.video)
        st, h, body = self.put("my%20stream.mp4", data)
        self.assertEqual(st, 200)
        path = json.loads(body)["path"]
        self.assertEqual(os.path.dirname(path), os.path.realpath(self.store.videos))
        self.assertEqual(read_bytes(path), data)
        # same name again does not overwrite
        st, h, body = self.put("my%20stream.mp4", data)
        self.assertNotEqual(json.loads(body)["path"], path)
        self.assertEqual(self.leftovers(), [])

    def test_upload_names_cannot_escape(self):
        for name in ("..%2f..%2fevil.mp4", "%2e%2e%5c%2e%2e%5cevil.mp4", "a%00b.mp4", "..%2F..%2F..%2Fetc%2Fcron.d%2Fx.mp4"):
            st, h, body = self.put(name, b"abc")
            if st == 200:
                p = json.loads(body)["path"]
                self.assertEqual(os.path.dirname(p), os.path.realpath(self.store.videos), name)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "evil.mp4")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "data", "evil.mp4")))

    def test_upload_rejects_bad_types_and_sizes(self):
        self.assertEqual(self.put("a.exe", b"abc")[0], 400)
        self.assertEqual(self.put("a", b"abc")[0], 400)
        self.assertEqual(self.put("a.mp4", b"")[0], 413)
        self.assertEqual(self.put("a.mp4", b"x", cookie=False)[0], 403)

    def test_cut_off_upload_leaves_nothing_behind(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        s.sendall((f"PUT /api/upload?name=cut.mp4 HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                   f"Cookie: {w.COOKIE}={TOKEN}\r\nOrigin: http://127.0.0.1:{self.port}\r\nX-CF: 1\r\n"
                   "Content-Type: application/octet-stream\r\nContent-Length: 1000\r\n\r\n").encode() + b"x" * 10)
        s.shutdown(socket.SHUT_WR)
        reply = s.recv(4096)
        s.close()
        self.assertIn(b"400", reply.split(b"\r\n")[0])
        self.assertEqual(self.leftovers(), [])
        self.assertFalse(os.path.exists(os.path.join(self.store.videos, "cut.mp4")))


FAKE_YTDLP = r"""#!%(py)s
import json, os, shutil, subprocess, sys
a = sys.argv
with open(%(log)r, "a") as fh:
    fh.write(json.dumps(a[1:]) + "\n")
assert "--" in a and a[a.index("--") + 1].startswith("https://")
out = a[a.index("-o") + 1].replace("%%(ext)s", "mp4")
mode = os.environ.get("FAKE_SECTIONS", "ok")


def secs(t):
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


if "--download-sections" in a:
    if mode == "fail":
        sys.stderr.write("ERROR: sections are not available\n")
        sys.exit(1)
    start, end = [secs(x) for x in a[a.index("--download-sections") + 1].lstrip("*").split("-")]
    if mode == "short":
        end -= 10
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-to", str(end), "-i", %(full)r, "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", out], check=True)
elif "-S" in a:
    shutil.copy(%(low)r, out)
else:
    if os.environ.get("FAKE_FULL") == "fail":
        sys.stderr.write("ERROR: unable to download video data\\n")
        sys.exit(1)
    shutil.copy(%(full)r, out)
"""


class TestFastLink(Base):
    """Links: a small copy is downloaded for the analysis, and only the chosen parts in full quality afterwards.
    The real YouTube cannot be reached from the tests, so a fake yt-dlp stands in for it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bin = os.path.join(cls.tmp.name, "bin")
        os.makedirs(cls.bin)
        cls.log = os.path.join(cls.tmp.name, "ytdlp_calls.log")
        cls.low = os.path.join(cls.tmp.name, "low.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", cls.video, "-vf", "scale=160:90", "-c:v", "libx264",
                        "-preset", "ultrafast", "-c:a", "aac", cls.low], check=True)
        script = os.path.join(cls.bin, "yt-dlp")
        with open(script, "w") as fh:
            fh.write(FAKE_YTDLP % {"py": sys.executable, "log": cls.log, "full": cls.video, "low": cls.low})
        os.chmod(script, 0o755)

    def setUp(self):
        from unittest import mock
        open(self.log, "w").close()
        p = mock.patch.dict(os.environ, {"PATH": self.bin + os.pathsep + os.environ["PATH"], "FAKE_SECTIONS": "ok"})
        p.start()
        self.addCleanup(p.stop)

    def calls(self):
        return [json.loads(line) for line in read(self.log).splitlines() if line]

    def start(self, vid):
        url = f"https://www.youtube.com/watch?v={vid}"
        st, d = self.jpost("/api/analyze", {"kind": "link", "url": url})
        self.assertEqual(st, 200, d)
        job = self.wait_job()
        self.assertEqual(job["state"], "done", job)
        aid = job["result"]["analysis"]
        return aid, self.jget(f"/api/analysis/{aid}")[1]

    def render(self, aid, a, n=2):
        items = []
        for c in a["clips"][:n]:
            items.append({"id": c["id"], "start": c["start"] + 1, "end": c["start"] + 9, "layout": "blur",
                          "crop_x": 0.5, "title": f"clip {c['id']}"})
        st, d = self.jpost("/api/render", {"id": aid, "run": a["run"], "items": items})
        self.assertEqual(st, 200, d)
        return items, self.wait_job()

    def test_analysis_downloads_only_the_small_copy(self):
        aid, a = self.start("aaaaaaaaaa1")
        self.assertTrue(a["low_res"])
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][calls[0].index("-S") + 1], "res:360")
        self.assertNotIn("--download-sections", calls[0])
        self.assertEqual(calls[0][-2:-1], ["--"])
        folder = self.store.adir(aid)
        self.assertTrue(os.path.isfile(os.path.join(folder, "lowres.mp4")))
        self.assertFalse(os.path.exists(os.path.join(folder, "source.mp4")))
        self.assertGreaterEqual(len(a["clips"]), 2)
        # opening the same link again does not download again
        st, d = self.jpost("/api/analyze", {"kind": "link", "url": "https://www.youtube.com/watch?v=aaaaaaaaaa1"})
        self.assertEqual(d, {"existing": aid})
        self.assertEqual(len(self.calls()), 1)

    def test_clips_come_from_downloaded_parts_in_full_quality(self):
        aid, a = self.start("aaaaaaaaaa2")
        items, job = self.render(aid, a)
        self.assertEqual(job["state"], "done", job)
        sections = [c for c in self.calls() if "--download-sections" in c]
        self.assertEqual(len(sections), 2)
        for c, it in zip(sections, items):
            self.assertIn("--force-keyframes-at-cuts", c)
            m = re.fullmatch(r"\*(\d+):(\d\d):(\d\d\.\d\d)-(\d+):(\d\d):(\d\d\.\d\d)", c[c.index("--download-sections") + 1])
            self.assertIsNotNone(m, c)
            s0 = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
            e0 = int(m[4]) * 3600 + int(m[5]) * 60 + float(m[6])
            self.assertAlmostEqual(s0, it["start"] - 3, delta=0.02)        # 3 s of padding each side
            self.assertAlmostEqual(e0, it["end"] + 3, delta=0.02)
            self.assertEqual(c[-2:-1], ["--"])
        self.assertEqual([c for c in self.calls() if "--download-sections" not in c and "-S" not in c], [])   # never the whole video
        folder = self.store.adir(aid)
        self.assertFalse(os.path.exists(os.path.join(folder, "source.mp4")))
        self.assertEqual(len(os.listdir(os.path.join(folder, "sections"))), 2)
        for f, it in zip(job["result"]["files"], items):
            path = os.path.join(folder, "clips", f["file"])
            streams = probe_json(path)["streams"]
            v = next(x for x in streams if x["codec_type"] == "video")
            self.assertEqual((v["width"], v["height"]), (1080, 1920))
            dur = float(next(x for x in streams if x["codec_type"] == "video").get("duration") or 0)
            self.assertAlmostEqual(dur, it["end"] - it["start"], delta=0.6)       # trimmed exactly to what was asked

    def test_same_parts_are_not_downloaded_twice(self):
        aid, a = self.start("aaaaaaaaaa3")
        self.render(aid, a, 1)
        n = len(self.calls())
        items, job = self.render(aid, a, 1)
        self.assertEqual(job["state"], "done")
        self.assertEqual(len(self.calls()), n)

    def test_if_parts_cannot_be_downloaded_the_whole_video_is(self):
        os.environ["FAKE_SECTIONS"] = "fail"
        aid, a = self.start("aaaaaaaaaa4")
        items, job = self.render(aid, a)
        self.assertEqual(job["state"], "done", job)
        full = [c for c in self.calls() if "--download-sections" not in c and "-S" not in c]
        self.assertEqual(len(full), 1)                                         # once, not once per clip
        self.assertTrue(os.path.isfile(os.path.join(self.store.adir(aid), "source.mp4")))
        self.assertEqual(len(job["result"]["files"]), 2)

    def test_when_nothing_can_be_downloaded_the_job_fails_clearly_and_no_clip_appears(self):
        os.environ["FAKE_SECTIONS"] = "fail"
        os.environ["FAKE_FULL"] = "fail"
        self.addCleanup(os.environ.pop, "FAKE_FULL", None)
        aid, a = self.start("aaaaaaaaaa7")
        items, job = self.render(aid, a, 1)
        self.assertEqual((job["state"], job["code"]), ("error", "download_failed"), job)
        clips = os.path.join(self.store.adir(aid), "clips")
        self.assertEqual([f for f in (os.listdir(clips) if os.path.isdir(clips) else []) if f.endswith(".mp4")], [])

    def test_a_cancelled_render_does_not_start_the_fallback_download(self):
        slow = os.path.join(self.tmp.name, "slowbin2")
        os.makedirs(slow, exist_ok=True)
        aid, a = self.start("aaaaaaaaaa8")
        with open(os.path.join(slow, "yt-dlp"), "w") as fh:
            fh.write("#!%s\nimport time\ntime.sleep(60)\n" % sys.executable)
        os.chmod(os.path.join(slow, "yt-dlp"), 0o755)
        from unittest import mock
        with mock.patch.dict(os.environ, {"PATH": slow + os.pathsep + os.environ["PATH"]}):
            items = [{"id": c["id"], "start": c["start"] + 1, "end": c["start"] + 9, "layout": "blur", "crop_x": 0.5,
                      "title": "x"} for c in a["clips"][:1]]
            st, d = self.jpost("/api/render", {"id": aid, "run": a["run"], "items": items})
            self.assertEqual(st, 200, d)
            time.sleep(1.0)
            self.assertEqual(self.jpost("/api/cancel")[1], {"cancelled": True})
            job = self.wait_job(30)
        self.assertEqual(job["state"], "cancelled", job)

    def test_a_part_with_the_wrong_length_is_not_trusted(self):
        os.environ["FAKE_SECTIONS"] = "short"
        aid, a = self.start("aaaaaaaaaa5")
        items, job = self.render(aid, a, 1)
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(len([c for c in self.calls() if "--download-sections" not in c and "-S" not in c]), 1)

    def test_missing_ytdlp_is_reported_before_anything_starts(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            st, d = self.jpost("/api/analyze", {"kind": "link", "url": "https://youtu.be/abcdefghijk"})
        self.assertEqual((st, d["error"]), (400, "no_ytdlp"))

    def test_cancel_stops_the_download(self):
        slow = os.path.join(self.tmp.name, "slowbin")
        os.makedirs(slow, exist_ok=True)
        with open(os.path.join(slow, "yt-dlp"), "w") as fh:
            fh.write("#!%s\nimport time\ntime.sleep(60)\n" % sys.executable)
        os.chmod(os.path.join(slow, "yt-dlp"), 0o755)
        from unittest import mock
        with mock.patch.dict(os.environ, {"PATH": slow + os.pathsep + os.environ["PATH"]}):
            st, d = self.jpost("/api/analyze", {"kind": "link", "url": "https://www.youtube.com/watch?v=aaaaaaaaaa6"})
            self.assertEqual(st, 200)
            time.sleep(1.0)
            self.assertEqual(self.jpost("/api/cancel")[1], {"cancelled": True})
            t0 = time.time()
            self.assertEqual(self.wait_job(20)["state"], "cancelled")
            self.assertLess(time.time() - t0, 10)


class TestConnectorPanel(Base):
    def test_the_command_for_claude_is_fixed_and_absolute(self):
        st, d = self.jget("/api/connector")
        self.assertEqual(st, 200)
        self.assertTrue(os.path.isabs(d["command"]))
        self.assertEqual(d["args"][0], os.path.join(ROOT, "clipfinder_web.py"))
        self.assertEqual(d["args"][1:], ["--mcp", "--data", self.srv.app.store.root])
        self.assertFalse(d["frozen"])

    def test_it_needs_a_session_and_the_test_needs_the_write_checks(self):
        self.assertEqual(self.req("GET", "/api/connector", cookie=False)[0], 403)
        self.assertEqual(self.req("POST", "/api/connector-test", {}, cookie=False)[0], 403)
        self.assertEqual(self.req("POST", "/api/connector-test", {}, write_headers=False)[0], 403)
        self.assertEqual(self.req("POST", "/api/connector-test", {}, headers={"Origin": "http://evil.example"})[0], 403)

    def test_the_self_test_really_starts_the_connector(self):
        st, d = self.jpost("/api/connector-test")
        self.assertEqual(st, 200)
        self.assertEqual(d, {"ok": True, "tools": 8})

    def test_the_self_test_reports_a_broken_connector(self):
        app = self.srv.app
        orig = app.connector_info
        app.connector_info = lambda: {"command": sys.executable, "args": ["-c", "print('not protocol')"], "frozen": False}
        try:
            self.assertEqual(self.jpost("/api/connector-test")[1], {"ok": False, "tools": 0})
            app.connector_info = lambda: {"command": os.path.join(ROOT, "no-such-program"), "args": [], "frozen": False}
            self.assertEqual(self.jpost("/api/connector-test")[1], {"ok": False, "tools": 0})
        finally:
            app.connector_info = orig


class TestRemote(unittest.TestCase):
    HOST = "pc.tail1234.ts.net"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = w.Store(os.path.join(cls.tmp.name, "data"))
        cls.srv, cls.app = w.make_server(cls.store, 0, remote_host=cls.HOST)
        cls.port = cls.app.port
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def req(self, method, path, host=None, body=None, origin=None, extra=None, cookie=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Host": host or self.HOST}
        if cookie:
            h["Cookie"] = f"{w.COOKIE}={self.app.token}"
        if method == "POST":
            h.update({"Origin": origin or f"https://{self.HOST}", "X-CF": "1", "Content-Type": "application/json"})
        h.update(extra or {})
        conn.request(method, path, body=json.dumps(body or {}).encode() if method == "POST" else None, headers=h)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, data

    def local(self):
        return f"127.0.0.1:{self.port}"

    def test_names_must_be_tailscale_names(self):
        for ok in ("pc.tail1234.ts.net", "https://PC.Tail1234.ts.net/", "a-b.c-d.ts.net"):
            self.assertTrue(w.clean_remote_host(ok).endswith(".ts.net"))
        for bad in ("evil.com", "ts.net", ".ts.net", "a.ts.net.evil.com", "pc.tail1234.ts.net:8765", "127.0.0.1", "", 5, None,
                    "pc .ts.net", "pc\n.ts.net", "-a.ts.net", "a..ts.net", "pc.tail1234.ts.net/x", "x" * 300 + ".ts.net"):
            with self.assertRaises(w.ApiError, msg=repr(bad)):
                w.clean_remote_host(bad)

    def test_the_secret_stays_the_same_between_runs_and_is_private(self):
        t1 = w.remote_token(self.store.root)
        self.assertEqual(t1, w.remote_token(self.store.root))
        self.assertEqual(self.app.token, t1)
        self.assertRegex(t1, r"^[A-Za-z0-9_-]{43}$")
        if os.name != "nt":
            self.assertEqual(os.stat(os.path.join(self.store.root, ".remote-token")).st_mode & 0o077, 0)
        other, app2 = w.make_server(w.Store(os.path.join(self.tmp.name, "d2")), 0)
        other.server_close()
        self.assertNotEqual(app2.token, t1)                         # without remote access the secret is new every run

    def test_the_tailscale_name_is_accepted_and_others_are_not(self):
        st, h, d = self.req("GET", "/api/state")
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(d)["remote"])
        self.assertEqual(self.req("GET", "/api/state", host="other.tail1234.ts.net")[0], 403)
        self.assertEqual(self.req("GET", "/api/state", host=f"{self.HOST}:{self.port}")[0], 403)
        self.assertEqual(self.req("GET", "/api/state", host="evil.example")[0], 403)
        st, h, d = self.req("GET", "/api/state", host=self.local())
        self.assertFalse(json.loads(d)["remote"])

    def test_it_still_needs_the_secret(self):
        self.assertEqual(self.req("GET", "/api/state", cookie=False)[0], 403)
        st, h, d = self.req("GET", "/api/state", cookie=False, extra={"Cookie": f"{w.COOKIE}=wrong"})
        self.assertEqual(st, 403)

    def test_writes_need_the_https_origin_of_the_tailscale_name(self):
        self.assertEqual(self.req("POST", "/api/cancel")[0], 200)
        self.assertEqual(self.req("POST", "/api/cancel", origin=f"http://{self.HOST}")[0], 403)
        self.assertEqual(self.req("POST", "/api/cancel", origin="https://evil.example")[0], 403)
        self.assertEqual(self.req("POST", "/api/cancel", origin="https://other.tail1234.ts.net")[0], 403)

    def test_the_cookie_is_secure_only_on_the_https_name(self):
        st, h, d = self.req("GET", f"/?t={self.app.token}", cookie=False)
        self.assertEqual(st, 302)
        self.assertIn("Secure", h["set-cookie"])
        self.assertIn("HttpOnly", h["set-cookie"])
        st, h, d = self.req("GET", f"/?t={self.app.token}", host=self.local(), cookie=False)
        self.assertNotIn("Secure", h["set-cookie"])

    def test_controls_that_act_on_the_computer_are_closed_to_remote_requests(self):
        for path in ("/api/pick", "/api/open-folder", "/api/quit", "/api/update-ytdlp", "/api/connector-test", "/api/remote"):
            st, h, d = self.req("POST", path)
            self.assertEqual((st, json.loads(d)["error"]), (403, "local_only"), path)
        for path in ("/api/connector", "/api/remote"):
            self.assertEqual(self.req("GET", path)[0], 403, path)
        # the same request made straight from this computer is allowed
        self.assertEqual(self.req("GET", "/api/connector", host=self.local())[0], 200)

    def test_a_request_that_carries_proxy_headers_counts_as_remote_even_with_a_local_host(self):
        for hdr in ("X-Forwarded-For", "X-Forwarded-Host", "Forwarded", "Tailscale-User-Login"):
            st, h, d = self.req("GET", "/api/connector", host=self.local(), extra={hdr: "100.64.0.2"})
            self.assertEqual(st, 403, hdr)

    def test_saving_the_name_from_the_local_page(self):
        st, h, d = self.req("POST", "/api/remote", host=self.local(), origin=f"http://{self.local()}",
                            body={"host": "NEW.tail9.ts.net"})
        self.assertEqual(st, 200)
        info = json.loads(d)
        self.assertEqual(info["host"], "new.tail9.ts.net")
        self.assertEqual(info["active"], self.HOST)
        self.assertEqual(info["link"], f"https://{self.HOST}/?t={self.app.token}")
        self.assertEqual(w.load_remote_host(self.store.root), "new.tail9.ts.net")
        for bad in ("evil.com", 5, ["a.ts.net"]):
            st, h, d = self.req("POST", "/api/remote", host=self.local(), origin=f"http://{self.local()}", body={"host": bad})
            self.assertEqual((st, json.loads(d)["error"]), (400, "bad_host_name"))
        st, h, d = self.req("POST", "/api/remote", host=self.local(), origin=f"http://{self.local()}", body={"host": ""})
        self.assertIsNone(json.loads(d)["host"])
        self.assertIsNone(w.load_remote_host(self.store.root))

    def test_a_damaged_settings_file_means_remote_access_off(self):
        path = os.path.join(self.store.root, "remote.json")
        for content in ("{", "[]", '{"host": "evil.com"}', '{"host": 5}', ""):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            self.assertIsNone(w.load_remote_host(self.store.root), content)
        os.remove(path)

    def test_without_remote_access_a_tailscale_name_is_refused(self):
        srv, app = w.make_server(w.Store(os.path.join(self.tmp.name, "d3")), 0, token=TOKEN)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=30)
        conn.request("GET", "/api/state", headers={"Host": self.HOST, "Cookie": f"{w.COOKIE}={TOKEN}"})
        r = conn.getresponse()
        r.read()
        conn.close()
        self.assertEqual(r.status, 403)


class TestQuit(unittest.TestCase):
    def test_quit_stops_the_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = w.Store(os.path.join(tmp, "d"))
            srv, app = w.make_server(store, 0, token=TOKEN)
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=10)
            conn.request("POST", "/api/quit", body=b"{}", headers={
                "Host": f"127.0.0.1:{app.port}", "Cookie": f"{w.COOKIE}={TOKEN}", "X-CF": "1",
                "Origin": f"http://127.0.0.1:{app.port}", "Content-Type": "application/json"})
            self.assertEqual(conn.getresponse().status, 200)
            conn.close()
            t.join(10)
            self.assertFalse(t.is_alive())
            srv.server_close()

    def test_port_in_use_moves_to_the_next(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = w.Store(os.path.join(tmp, "d"))
            one, a1 = w.make_server(store, 0)
            two, a2 = w.make_server(store, a1.port)
            try:
                self.assertNotEqual(a1.port, a2.port)
                self.assertNotEqual(a1.token, a2.token)
                self.assertGreaterEqual(len(a1.token), 32)
            finally:
                one.server_close()
                two.server_close()


FAKE_UPDATABLE = r"""#!%(py)s
import sys
state = %(state)r
if sys.argv[1:] == ["--version"]:
    sys.stdout.write(open(state).read())
    sys.exit(0)
if sys.argv[1:] == ["-U"]:
    mode = %(mode)r
    if mode == "newer":
        open(state, "w").write("2026.11.01\n")
    sys.exit(0 if mode != "fail" else 1)
sys.exit(2)
"""


class TestYtdlpUpdate(Base):
    """The settings button that updates yt-dlp (it breaks every few weeks when YouTube changes)."""

    def fake(self, mode):
        folder = os.path.join(self.tmp.name, "upd-" + mode)
        os.makedirs(folder, exist_ok=True)
        state = os.path.join(folder, "version.txt")
        with open(state, "w") as fh:
            fh.write("2026.10.01\n")
        script = os.path.join(folder, "yt-dlp")
        with open(script, "w") as fh:
            fh.write(FAKE_UPDATABLE % {"py": sys.executable, "state": state, "mode": mode})
        os.chmod(script, 0o755)
        return folder

    def run_update(self, mode):
        from unittest import mock
        with mock.patch.dict(os.environ, {"PATH": self.fake(mode) + os.pathsep + os.environ["PATH"]}):
            return self.jpost("/api/update-ytdlp")

    def test_newer_version_is_reported(self):
        st, d = self.run_update("newer")
        self.assertEqual((st, d), (200, {"ok": True, "before": "2026.10.01", "after": "2026.11.01", "changed": True}))

    def test_already_up_to_date_is_said_honestly(self):
        st, d = self.run_update("same")
        self.assertEqual((st, d), (200, {"ok": True, "before": "2026.10.01", "after": "2026.10.01", "changed": False}))

    def test_a_failed_update_does_not_claim_success_or_change(self):
        from unittest import mock
        real = subprocess.run
        calls = []

        def fake_run(cmd, *a, **k):
            calls.append(list(cmd))
            if cmd[0] == sys.executable:                      # the pip fallback also fails
                return subprocess.CompletedProcess(cmd, 1, b"", b"")
            return real(cmd, *a, **k)
        with mock.patch.object(w.subprocess, "run", fake_run):
            st, d = self.run_update("fail")
        self.assertEqual((st, d), (200, {"ok": False, "before": "2026.10.01", "after": "2026.10.01", "changed": False}))
        self.assertIn([sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"], calls)

    def test_pip_installed_program_is_updated_with_pip(self):
        from unittest import mock
        real = subprocess.run
        folder = self.fake("fail")                       # its own -U refuses, like a pip-installed yt-dlp does

        def fake_run(cmd, *a, **k):
            if cmd[0] == sys.executable:
                with open(os.path.join(folder, "version.txt"), "w") as fh:
                    fh.write("2026.12.05\n")
                return subprocess.CompletedProcess(cmd, 0, b"", b"")
            return real(cmd, *a, **k)
        with mock.patch.dict(os.environ, {"PATH": folder + os.pathsep + os.environ["PATH"]}), \
                mock.patch.object(w.subprocess, "run", fake_run):
            st, d = self.jpost("/api/update-ytdlp")
        self.assertEqual((st, d), (200, {"ok": True, "before": "2026.10.01", "after": "2026.12.05", "changed": True}))

    def test_a_hanging_update_is_stopped_and_not_called_success(self):
        from unittest import mock
        real = subprocess.run

        def fake_run(cmd, *a, **k):
            if cmd[-1] == "-U":
                raise subprocess.TimeoutExpired(cmd, k.get("timeout", 0))
            if cmd[0] == sys.executable:
                raise OSError("pip is not there")
            return real(cmd, *a, **k)
        with mock.patch.object(w.subprocess, "run", fake_run):
            st, d = self.run_update("same")
        self.assertEqual((st, d["ok"], d["changed"]), (200, False, False))

    def test_pip_is_not_used_by_the_packaged_program(self):
        from unittest import mock
        calls = []

        def fake_run(cmd, *a, **k):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 1 if cmd[-1] == "-U" else 0, b"2026.10.01\n", b"")
        with mock.patch.object(w.shutil, "which", lambda n: "/opt/cf/yt-dlp"), \
                mock.patch.object(w.subprocess, "run", fake_run), mock.patch.object(w, "FROZEN", True):
            res = w.default_update_ytdlp()
        self.assertEqual([c[0] for c in calls], ["/opt/cf/yt-dlp"] * 3)       # --version, -U, --version: no pip
        self.assertEqual((res["ok"], res["changed"]), (False, False))

    def test_commands_are_fixed_ones_without_anything_from_the_page(self):
        from unittest import mock
        calls = []

        def fake_run(cmd, *a, **k):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, b"2026.10.01\n", b"")
        with mock.patch.object(w.shutil, "which", lambda n: "/opt/cf/yt-dlp"), mock.patch.object(w.subprocess, "run", fake_run):
            st, d = self.jpost("/api/update-ytdlp", {"cmd": "calc.exe", "args": ["--exec", "x"]})
        self.assertEqual(st, 200)
        self.assertEqual(calls, [["/opt/cf/yt-dlp", "--version"], ["/opt/cf/yt-dlp", "-U"], ["/opt/cf/yt-dlp", "--version"]])
        self.assertTrue(d["ok"])

    def test_missing_program_is_reported(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            st, d = self.jpost("/api/update-ytdlp")
        self.assertEqual((st, d["error"]), (400, "no_ytdlp"))

    def test_refused_while_a_job_runs(self):
        old = self.app.jobs.cur
        self.app.jobs.cur = {"state": "running"}
        try:
            st, d = self.jpost("/api/update-ytdlp")
        finally:
            self.app.jobs.cur = old
        self.assertEqual((st, d["error"]), (409, "busy"))

    def test_same_protection_as_every_other_action(self):
        from unittest import mock
        called = []
        with mock.patch.object(self.app, "updater", lambda: called.append(1) or {}):
            self.assertEqual(self.req("POST", "/api/update-ytdlp", {}, cookie=False)[0], 403)
            self.assertEqual(self.req("POST", "/api/update-ytdlp", {}, write_headers=False)[0], 403)
            self.assertEqual(self.req("POST", "/api/update-ytdlp", {}, headers={"Origin": "http://evil.example"})[0], 403)
            self.assertEqual(self.req("GET", "/api/update-ytdlp")[0], 404)
        self.assertEqual(called, [])


class TestAppWindow(unittest.TestCase):
    """The page opens in its own small window (Edge / Chrome --app) and falls back to the normal browser."""
    URL = "http://127.0.0.1:8765/?t=abc"

    def test_window_is_used_when_a_browser_is_found(self):
        from unittest import mock
        with mock.patch.object(w, "find_app_browser", lambda: "/opt/edge/msedge"), \
                mock.patch.object(w.subprocess, "Popen") as popen, mock.patch.object(w.webbrowser, "open") as op:
            self.assertEqual(w.open_ui(self.URL), "window")
        cmd = popen.call_args[0][0]
        self.assertEqual(cmd[:2], ["/opt/edge/msedge", "--app=" + self.URL])
        self.assertTrue(all(isinstance(c, str) for c in cmd))
        op.assert_not_called()

    def test_normal_tab_when_asked_or_when_nothing_is_found_or_launch_fails(self):
        from unittest import mock
        with mock.patch.object(w, "find_app_browser", lambda: "/opt/edge/msedge"), \
                mock.patch.object(w.subprocess, "Popen") as popen, mock.patch.object(w.webbrowser, "open") as op:
            self.assertEqual(w.open_ui(self.URL, window=False), "browser")
            popen.assert_not_called()
            op.assert_called_once_with(self.URL)
        with mock.patch.object(w, "find_app_browser", lambda: None), \
                mock.patch.object(w.subprocess, "Popen") as popen, mock.patch.object(w.webbrowser, "open") as op:
            self.assertEqual(w.open_ui(self.URL), "browser")
            popen.assert_not_called()
            op.assert_called_once_with(self.URL)
        with mock.patch.object(w, "find_app_browser", lambda: "/opt/edge/msedge"), \
                mock.patch.object(w.subprocess, "Popen", side_effect=OSError("nope")), \
                mock.patch.object(w.webbrowser, "open") as op:
            self.assertEqual(w.open_ui(self.URL), "browser")
            op.assert_called_once_with(self.URL)

    def test_finds_a_chrome_family_browser_on_linux_path(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            exe = os.path.join(d, "chromium")
            with open(exe, "w") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(exe, 0o755)
            with mock.patch.object(w.sys, "platform", "linux"), mock.patch.dict(os.environ, {"PATH": d}):
                self.assertEqual(w.find_app_browser(), exe)
            with mock.patch.object(w.sys, "platform", "linux"), mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
                self.assertIsNone(w.find_app_browser())

    def test_windows_prefers_edge_then_chrome(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            edge = os.path.join(d, "Microsoft", "Edge", "Application", "msedge.exe")
            chrome = os.path.join(d, "Google", "Chrome", "Application", "chrome.exe")
            for f in (edge, chrome):
                os.makedirs(os.path.dirname(f))
                with open(f, "w") as fh:
                    fh.write("x")
            env = {"ProgramFiles": d, "ProgramFiles(x86)": os.path.join(d, "none"), "LocalAppData": os.path.join(d, "none")}
            with mock.patch.object(w.os, "name", "nt"), mock.patch.dict(os.environ, env):
                self.assertEqual(w.find_app_browser(), edge)
                os.remove(edge)
                self.assertEqual(w.find_app_browser(), chrome)

    def test_tab_flag_is_passed_on(self):
        from unittest import mock

        class FakeServer:
            def serve_forever(self):
                raise KeyboardInterrupt

            def server_close(self):
                pass

        class FakeTimer:
            def __init__(self, delay, fn):
                self.fn = fn

            def start(self):
                self.fn()
        seen = []
        for argv, expect in ((["--data", "x"], True), (["--data", "x", "--tab"], False)):
            with tempfile.TemporaryDirectory() as d, \
                    mock.patch.object(w, "make_server", lambda *a, **k: (FakeServer(), mock.Mock(port=1, token="t"))), \
                    mock.patch.object(w, "open_ui", lambda url, window=True: seen.append(window)), \
                    mock.patch.object(w.threading, "Timer", FakeTimer), mock.patch("builtins.print"):
                argv = list(argv)
                argv[1] = os.path.join(d, "data")
                self.assertEqual(w.main(argv), 0)
            self.assertEqual(seen[-1], expect)
        # --no-browser opens nothing at all
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(w, "make_server", lambda *a, **k: (FakeServer(), mock.Mock(port=1, token="t"))), \
                mock.patch.object(w, "open_ui", lambda url, window=True: seen.append("opened")), mock.patch("builtins.print"):
            self.assertEqual(w.main(["--data", os.path.join(d, "data"), "--no-browser"]), 0)
        self.assertNotIn("opened", seen)


class TestPackaged(unittest.TestCase):
    """Things that only matter in the packaged program (one .exe with its own tools folder)."""

    def test_file_dialog_runs_in_its_own_process_with_fixed_arguments(self):
        from unittest import mock
        seen = []

        def fake_run(cmd, **k):
            seen.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "D:\\Videos\\stream.mp4".encode("utf-8"), b"")
        with mock.patch.object(w.subprocess, "run", fake_run):
            with mock.patch.object(w, "FROZEN", False):
                self.assertEqual(w.default_picker(), "D:\\Videos\\stream.mp4")
            with mock.patch.object(w, "FROZEN", True):
                w.default_picker()
        self.assertEqual(seen[0], [sys.executable, "-E", "-s", os.path.join(ROOT, "clipfinder_web.py"), "--pick-file"])
        self.assertEqual(seen[1], [sys.executable, "--pick-file"])

    def test_file_dialog_unavailable_gives_none(self):
        from unittest import mock
        with mock.patch.object(w.subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 3, b"", b"")):
            self.assertIsNone(w.default_picker())
        with mock.patch.object(w.subprocess, "run", side_effect=OSError("no")):
            self.assertIsNone(w.default_picker())

    def test_pick_file_flag_only_shows_the_dialog(self):
        import io
        from unittest import mock
        with tempfile.TemporaryDirectory() as d, mock.patch.object(w, "pick_file_here", lambda: 7):
            self.assertEqual(w.main(["--pick-file", "--data", os.path.join(d, "data")]), 7)
            self.assertFalse(os.path.exists(os.path.join(d, "data")))          # no folders, no server
        # the dialog function with a stand-in for tkinter
        calls = {}

        class Root:
            def withdraw(self): calls["withdraw"] = True
            def attributes(self, *a): calls["top"] = a
            def destroy(self): calls["destroy"] = True

        fake = mock.Mock()
        fake.Tk = Root
        fake.filedialog.askopenfilename = lambda **k: "C:\\Clips\\\u0d15\u0d4d\u0d32\u0d3f\u0d2a\u0d4d.mp4"
        out = mock.Mock(buffer=io.BytesIO())
        with mock.patch.dict(sys.modules, {"tkinter": fake, "tkinter.filedialog": fake.filedialog}), \
                mock.patch.object(w.sys, "stdout", out):
            self.assertEqual(w.pick_file_here(), 0)
        self.assertEqual(out.buffer.getvalue().decode("utf-8"), "C:\\Clips\\\u0d15\u0d4d\u0d32\u0d3f\u0d2a\u0d4d.mp4")
        self.assertTrue(calls["withdraw"] and calls["destroy"])
        with mock.patch.dict(sys.modules, {"tkinter": None}):
            self.assertEqual(w.pick_file_here(), 3)

    def test_tools_folder_comes_first_on_the_search_path(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            inner = os.path.join(d, "bundle")
            exe_dir = os.path.join(d, "app")
            for f in (os.path.join(inner, "tools"), os.path.join(exe_dir, "tools")):
                os.makedirs(f)
            with mock.patch.dict(os.environ, {"PATH": "/usr/bin"}), mock.patch.object(w, "BASE_DIR", inner), \
                    mock.patch.object(w, "FROZEN", True), mock.patch.object(w.sys, "executable", os.path.join(exe_dir, "ClipFinder.exe")):
                found = w.add_bundled_tools()
                self.assertEqual(found, [os.path.join(exe_dir, "tools"), os.path.join(inner, "tools")])
                self.assertEqual(os.environ["PATH"].split(os.pathsep)[:3], found + ["/usr/bin"])
            with mock.patch.dict(os.environ, {"PATH": "/usr/bin"}), mock.patch.object(w, "BASE_DIR", os.path.join(d, "none")), \
                    mock.patch.object(w, "FROZEN", False):
                self.assertEqual(w.add_bundled_tools(), [])
                self.assertEqual(os.environ["PATH"], "/usr/bin")

    def test_windows_is_told_not_to_run_programs_from_the_current_folder(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin"}), mock.patch.object(w.os, "name", "nt"):
            w.add_bundled_tools()
            self.assertEqual(os.environ["NoDefaultCurrentDirectoryInExePath"], "1")
            os.environ.pop("NoDefaultCurrentDirectoryInExePath", None)

    def test_helper_programs_are_started_by_full_path(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            exe = os.path.join(d, "ffmpeg")
            with open(exe, "w") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(exe, 0o755)
            with mock.patch.dict(os.environ, {"PATH": d}):
                self.assertEqual(cf.tool("ffmpeg"), exe)
            with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
                self.assertEqual(cf.tool("ffmpeg"), "ffmpeg")


class TestStaticRules(unittest.TestCase):
    """Rules about the code and page files themselves."""
    ALLOWED = {"argparse", "errno", "hashlib", "hmac", "http", "importlib", "json", "math", "os", "re", "secrets", "shutil",
               "subprocess", "sys", "threading", "time", "traceback", "webbrowser", "urllib", "clipfinder", "__future__",
               "tkinter", "clipfinder_mcp", "clipfinder_youtube", "clipfinder_ai", "clipfinder_update", "fcntl", "msvcrt"}                # tkinter: only the file dialog, in its own process
    FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__"}

    def tree(self):
        return ast.parse(read(os.path.join(ROOT, "clipfinder_web.py")))

    def test_imports_are_the_expected_few(self):
        mods = set()
        for node in ast.walk(self.tree()):
            if isinstance(node, ast.Import):
                mods |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module.split(".")[0])
        self.assertLessEqual(mods, self.ALLOWED, mods - self.ALLOWED)
        src = read(os.path.join(ROOT, "clipfinder_web.py"))
        for banned in ("urllib.request", "http.client", "socket", "requests", "smtplib", "ftplib", "pickle", "xmlrpc"):
            self.assertNotIn("import " + banned, src)

    def test_no_shell_no_dynamic_code_and_only_loopback(self):
        for node in ast.walk(self.tree()):
            if isinstance(node, ast.keyword) and node.arg == "shell":
                self.fail("shell=... is used")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in self.FORBIDDEN_CALLS:
                self.fail("dynamic code: " + node.func.id)
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in ("system", "popen"):
                self.fail("os.system/popen used")
        src = read(os.path.join(ROOT, "clipfinder_web.py"))
        self.assertNotIn("0.0.0.0", src)
        self.assertEqual(re.findall(r'Server\(\("([^"]*)"', src), ["127.0.0.1"])
        self.assertNotIn("Access-Control", src)

    def test_page_files_make_no_outside_requests_and_use_no_inline_code(self):
        web = os.path.join(ROOT, "web")
        html_, js, css = (read(os.path.join(web, n)) for n in ("index.html", "app.js", "app.css"))
        for text in (html_, js, css):
            self.assertIsNone(re.search(r"(src|href|action)\s*=\s*[\"']\s*(https?:)?//", text.replace(
                'href="https://www.youtube.com/upload"', "").replace('href="https://github.com/Surajprem7/clipfinder."', "")),
                "outside address in a page file")
            self.assertNotIn("@import", text)
            self.assertIsNone(re.search(r"url\(\s*[\"']?(https?:)?//", css))
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", html_), "inline script")
        self.assertIsNone(re.search(r"\sstyle\s*=", html_), "inline style attribute")
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", html_), "inline event handler")
        for banned in ("eval(", "new Function", "document.write", "insertAdjacentHTML", "outerHTML", "setAttribute(\"style\"",
                       "localStorage", "sessionStorage", "XMLHttpRequest(\"http", "fetch(\"http", "WebSocket", "sendBeacon"):
            self.assertNotIn(banned, js, banned)
        # innerHTML is never used (server text goes in with textContent only)
        self.assertNotIn("innerHTML", js)
        outside = {u for u in re.findall(r"https?://[^\s\"'`)]+", js)
                   if not u.startswith(("http://www.w3.org/2000/svg", "https://www.youtube.com", "https://youtu.be",
                                        "https://studio.youtube.com/video/", "https://accounts.google.com/"))}
        self.assertEqual(outside, set())
        self.assertEqual(sorted(set(re.findall(r"https?://[^\s\"'<>]+", html_))),
                         ["http://www.w3.org/2000/svg", "https://github.com/Surajprem7/clipfinder.", "https://www.youtube.com/upload",
                          "https://www.youtube.com/watch?v=…"])
        self.assertIn('rel="noopener noreferrer"', html_)

    def test_every_element_the_script_looks_up_exists(self):
        html_, js = read(os.path.join(ROOT, "web", "index.html")), read(os.path.join(ROOT, "web", "app.js"))
        ids = set(re.findall(r'\bid="([^"]+)"', html_))
        wanted = set(re.findall(r'\$\("([^"]+)"\)', js))
        dynamic = {"s-" + s for s in ("home", "progress", "review", "make", "done")}
        self.assertEqual(sorted(wanted - ids - dynamic), [])

    def test_error_codes_the_server_can_send_have_a_message_in_the_page(self):
        js = read(os.path.join(ROOT, "web", "app.js"))
        tree, codes = self.tree(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "ApiError" and len(node.args) >= 2 \
                    and isinstance(node.args[1], ast.Constant):
                codes.add(node.args[1].value)
            if isinstance(node, ast.FunctionDef) and node.name == "explain":
                codes |= {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                          and re.fullmatch(r"[a-z_]+", n.value) and n.value != "other"}
        quiet = {"other", "bad_request", "bad_json", "length_required", "uri_too_long", "no_picture", "bad_type", "bad_lang"}
        self.assertGreater(len(codes), 15)
        blocks = re.findall(r"^\s{6}err: \{\n(.*?)^\s{6}\},", js, re.M | re.S)
        self.assertEqual(len(blocks), 2)                      # one per language
        for block in blocks:
            known = set(re.findall(r"^\s{8}([a-z_]+):\s*\"", block, re.M))
            self.assertEqual(sorted(codes - known - quiet), [])


class TestLanguageSetting(Base):
    def test_language_is_malayalam_until_changed_and_then_remembered(self):
        self.assertEqual(self.jget("/api/state")[1]["lang"], "ml")
        code, _, body = self.req("POST", "/api/lang", {"lang": "en"})
        self.assertEqual((code, json.loads(body)), (200, {"lang": "en"}))
        self.assertEqual(self.jget("/api/state")[1]["lang"], "en")
        self.assertEqual(w.load_lang(self.store.root), "en")
        self.req("POST", "/api/lang", {"lang": "ml"})
        self.assertEqual(w.load_lang(self.store.root), "ml")

    def test_only_known_languages_are_accepted(self):
        for bad in ("fr", "", None, 5, ["en"], "../x", "en\n"):
            code, _, body = self.req("POST", "/api/lang", {"lang": bad})
            self.assertEqual((code, json.loads(body)["error"]), (400, "bad_lang"), bad)
        self.assertEqual(w.load_lang(self.store.root), "ml")

    def test_a_damaged_prefs_file_falls_back_to_malayalam(self):
        for content in ("{", "[]", '{"lang": "xx"}', '{"lang": 1}', "\x00"):
            with open(os.path.join(self.store.root, "prefs.json"), "w", encoding="utf-8") as fh:
                fh.write(content)
            self.assertEqual(w.load_lang(self.store.root), "ml")
        os.remove(os.path.join(self.store.root, "prefs.json"))


class TestLanguages(unittest.TestCase):
    """Malayalam and English: every text the page shows must exist in both."""

    @classmethod
    def setUpClass(cls):
        cls.html = read(os.path.join(ROOT, "web", "index.html"))
        cls.js = read(os.path.join(ROOT, "web", "app.js"))
        ml = cls.js.index("    ml: {"); en = cls.js.index("    en: {"); end = cls.js.index("  const LANGS")
        cls.ml, cls.en = cls.js[ml:en], cls.js[en:end]
        page = cls.en.index("      page: {")
        cls.en_page, cls.en_main = cls.en[page:], cls.en[:page]

    @staticmethod
    def keys(block, indent):
        lines = [l for l in block.split("\n") if re.match(r" {%d}[a-z]" % indent, l)]
        return {k for l in lines for k in re.findall(r"(?:^ {%d}|, )([a-z_0-9]+): " % indent, l)}

    def test_both_languages_have_the_same_texts(self):
        self.assertEqual(self.keys(self.ml, 6), self.keys(self.en, 6))
        self.assertGreater(len(self.keys(self.ml, 6)), 80)

    def test_every_text_the_script_asks_for_exists(self):
        used = set(re.findall(r'\btr\("([a-z_0-9]+)"', self.js))
        used |= set(re.findall(r'"(step_[a-z]+|t_[a-z]+)"', self.js))
        self.assertEqual(sorted(used - self.keys(self.ml, 6)), [])
        self.assertEqual(sorted(used - self.keys(self.en_main, 6)), [])

    def test_every_marked_part_of_the_page_has_english(self):
        marked = set(re.findall(r'data-i(?:-aria)?="([a-z_0-9]+)"', self.html))
        self.assertGreater(len(marked), 60)
        self.assertEqual(sorted(marked - self.keys(self.en_page, 8)), [])

    def test_no_malayalam_text_is_left_unmarked_in_the_page(self):
        body = self.html[self.html.index("<body>"):]
        marked_spans = []
        for m in re.finditer(r'<(\w+)[^>]*\sdata-i="[a-z_0-9]+"[^>]*>', body):
            marked_spans.append((m.end(), body.index("</%s>" % m.group(1), m.end())))
        for m in re.finditer(r"[\u0d00-\u0d7f]+", body):
            inside = any(a <= m.start() < b for a, b in marked_spans) or m.group(0) == "മലയാളം"   # the language button
            in_attr = body.rfind("<", 0, m.start()) > body.rfind(">", 0, m.start())   # inside a tag (aria-label)
            self.assertTrue(inside or in_attr, body[max(0, m.start() - 60):m.start() + 20])
        for m in re.finditer(r'aria-label="[^"]*[\u0d00-\u0d7f][^"]*"', body):
            tag = body[body.rfind("<", 0, m.start()):m.end()]
            self.assertTrue("data-i-aria=" in tag or 'id="lang-' in tag or "Language" in tag, tag)

    def test_script_has_no_malayalam_outside_the_text_table(self):
        rest = self.js[:self.js.index("  const TEXT = {")] + self.js[self.js.index("  const LANGS"):]
        self.assertIsNone(re.search(r"[\u0d00-\u0d7f]", rest))

    def test_english_text_has_no_malayalam(self):
        self.assertIsNone(re.search(r"[\u0d00-\u0d7f]", self.en))


def _browser_available():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


@unittest.skipUnless(_browser_available(), "playwright + chromium not available")
class TestInBrowser(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # headless Chromium cannot play H.264, so give it a WebM it can play
        cls.webm = os.path.join(cls.tmp.name, "stream demo.webm")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", cls.video, "-c:v", "libvpx", "-b:v", "600k", "-c:a", "libvorbis",
                        cls.webm], check=True)

    @staticmethod
    def poll(pg, expr, timeout=15):
        # (the page's policy forbids eval, so Playwright's wait_for_function cannot be used: poll instead)
        end = time.time() + timeout
        while time.time() < end:
            if pg.evaluate(expr):
                return
            time.sleep(0.2)
        raise AssertionError("timed out waiting for: " + expr)

    def test_connector_and_remote_panels_work_in_a_real_browser(self):
        from playwright.sync_api import sync_playwright
        logs = []
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_context(viewport={"width": 390, "height": 800}).new_page()
            pg.on("console", lambda m: logs.append((m.type, m.text)) if m.type in ("error", "warning") else None)
            pg.on("pageerror", lambda e: logs.append(("pageerror", str(e))))
            pg.goto(f"http://127.0.0.1:{self.port}/?t={TOKEN}")
            pg.wait_for_selector("#s-home:not([hidden])")
            pg.click("#open-settings")
            pg.wait_for_selector("#settings[open]")
            pg.click("#connect summary")
            self.poll(pg, "document.getElementById('cn-json').value.length > 20")
            cfg = json.loads(pg.input_value("#cn-json"))["mcpServers"]["clipfinder"]
            self.assertEqual(cfg["command"], os.path.abspath(sys.executable))
            self.assertEqual(cfg["args"][1:3], ["--mcp", "--data"])
            self.assertNotIn("--images", cfg["args"])
            pg.check("#cn-images")
            self.assertEqual(json.loads(pg.input_value("#cn-json"))["mcpServers"]["clipfinder"]["args"][-1], "--images")
            cmd = pg.input_value("#cn-cmd")
            self.assertTrue(cmd.startswith("claude mcp add clipfinder -- "))
            self.assertTrue(cmd.endswith("--images"))
            pg.click("#cn-test")
            self.poll(pg, "document.getElementById('cn-msg').textContent.indexOf('8') >= 0", timeout=60)
            # the Tailscale part
            pg.click("#remote summary")
            self.poll(pg, "document.getElementById('rm-cmd').value.indexOf('tailscale serve') === 0")
            pg.fill("#rm-host", "evil.example.com")
            pg.click("#rm-save")
            self.poll(pg, "document.getElementById('rm-msg').textContent.indexOf('Tailscale') >= 0")
            self.assertIsNone(w.load_remote_host(self.store.root))
            pg.fill("#rm-host", "pc.tail1234.ts.net")
            pg.click("#rm-save")
            self.poll(pg, "document.getElementById('rm-msg').textContent.indexOf('അടച്ച്') >= 0")
            self.assertEqual(w.load_remote_host(self.store.root), "pc.tail1234.ts.net")
            w.save_remote_host(self.store.root, None)
            self.assertEqual(pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), True)
            b.close()
        self.assertEqual([l for l in logs if "status of 400" not in l[1]], [])      # (the 400 is the wrong name we sent on purpose)

    def test_language_switch_changes_every_screen_text_and_is_remembered(self):
        from playwright.sync_api import sync_playwright
        logs = []
        ml_re = re.compile(r"[\u0d00-\u0d7f]+")
        with sync_playwright() as p:
            b = p.chromium.launch()
            ctx = b.new_context(viewport={"width": 1100, "height": 800})
            pg = ctx.new_page()
            pg.on("console", lambda m: logs.append((m.type, m.text)) if m.type in ("error", "warning") else None)
            pg.on("pageerror", lambda e: logs.append(("pageerror", str(e))))
            pg.goto(f"http://127.0.0.1:{self.port}/?t={TOKEN}")
            pg.wait_for_selector("#s-home:not([hidden])")
            self.assertEqual(pg.evaluate("document.documentElement.lang"), "ml")
            ml_h1 = pg.inner_text("#s-home h1")
            self.assertTrue(ml_re.search(ml_h1))
            pg.click("#lang-en")
            self.poll(pg, "document.documentElement.lang === 'en'")
            self.assertEqual(pg.inner_text("#s-home h1"), "Which stream do you want clips from?")
            self.assertEqual(pg.get_attribute("#lang-en", "aria-pressed"), "true")
            # nothing Malayalam is left on the home screen except the language button and file names
            visible = pg.inner_text("main") + pg.inner_text("header")
            left = [w for w in ml_re.findall(visible) if w != "മലയാളം"]
            self.assertEqual(left, [], visible)
            pg.click("#open-settings")
            pg.wait_for_selector("#settings[open]")
            self.assertEqual(pg.inner_text("#settings-title"), "Settings")
            self.assertIsNone(ml_re.search(pg.inner_text("#kv")))
            self.assertEqual(pg.get_attribute("#open-settings", "aria-label"), "Settings")
            pg.click("#settings-close")
            # remembered after a reload
            pg.reload()
            pg.wait_for_selector("#s-home:not([hidden])")
            self.poll(pg, "document.documentElement.lang === 'en'")
            self.assertEqual(pg.inner_text("#lib-empty, #s-home h2").strip() != "", True)
            # and back to Malayalam, exactly as before
            pg.click("#lang-ml")
            self.poll(pg, "document.documentElement.lang === 'ml'")
            self.assertEqual(pg.inner_text("#s-home h1"), ml_h1)
            self.assertEqual(pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), True)
            b.close()
        self.assertEqual(logs, [])

    def test_a_remote_visitor_does_not_get_the_controls_for_this_computer(self):
        from playwright.sync_api import sync_playwright
        logs = []
        with sync_playwright() as p:
            b = p.chromium.launch()
            ctx = b.new_context(viewport={"width": 1000, "height": 800}, extra_http_headers={"X-Forwarded-For": "100.64.0.2"})
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: logs.append(("pageerror", str(e))))
            pg.goto(f"http://127.0.0.1:{self.port}/?t={TOKEN}")
            pg.wait_for_selector("#s-home:not([hidden])")
            self.poll(pg, "document.body.classList.contains('remote')")
            for sel in ("#pick", "#connect", "#remote"):
                self.assertFalse(pg.locator(sel).first.is_visible(), sel)
            pg.click("#open-settings")
            pg.wait_for_selector("#settings[open]")
            for sel in ("#st-folder", "#st-update", "#st-quit", "#connect", "#remote"):
                self.assertFalse(pg.locator(sel).is_visible(), sel)
            b.close()
        self.assertEqual(logs, [])

    def test_whole_journey_with_no_console_errors(self):
        from playwright.sync_api import sync_playwright
        logs = []
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_context(viewport={"width": 1280, "height": 800}).new_page()
            pg.on("console", lambda m: logs.append((m.type, m.text)) if m.type in ("error", "warning") else None)
            pg.on("pageerror", lambda e: logs.append(("pageerror", str(e))))
            pg.goto(f"http://127.0.0.1:{self.port}/?t={TOKEN}")
            pg.wait_for_selector("#s-home:not([hidden])")
            self.assertNotIn("t=", pg.url)
            # home: upload through the browser's own file box, as a drag-and-drop would
            pg.set_input_files("#file-input", self.webm)
            pg.wait_for_selector("#s-review:not([hidden])", timeout=90000)
            pg.wait_for_selector(".clip")
            self.assertEqual(pg.locator(".clip.sel").count(), 3)
            # choosing and un-choosing
            pg.locator(".clip .pick").nth(0).click(position={"x": 100, "y": 15})
            self.assertEqual(pg.locator(".clip.sel").count(), 2)
            self.assertTrue(pg.locator("#rv-go").is_enabled())
            pg.locator(".clip .pick").nth(0).click(position={"x": 100, "y": 15})
            # the clip can be watched
            pg.locator(".clip .play").first.click()
            pg.wait_for_selector(".thumb video")
            self.poll(pg, "document.querySelector('.thumb video').readyState >= 2")
            pg.locator(".thumb .x").click()
            # a condition changes the list
            all_info = pg.inner_text("#top-info")
            pg.get_by_role("button", name="സ്ലോ-മോഷൻ").click()
            pg.wait_for_selector('#presets button[aria-pressed="true"]:text-is("സ്ലോ-മോഷൻ")', timeout=20000)
            pg.get_by_role("button", name="എല്ലാം", exact=True).click()
            pg.wait_for_selector('#presets button[aria-pressed="true"]:text-is("എല്ലാം")', timeout=20000)
            self.assertEqual(pg.inner_text("#top-info"), all_info)
            # the review screen in English, then back
            sel_before = pg.locator(".clip.sel").count()
            pg.click("#lang-en")
            self.poll(pg, "document.documentElement.lang === 'en'")
            pg.wait_for_selector('#presets button[aria-pressed="true"]:text-is("All")')
            self.assertIn("clips found", pg.inner_text("#top-info"))
            self.assertIn("selected", pg.inner_text("#rv-count"))
            self.assertTrue(pg.inner_text(".clip .head").startswith("Clip "))
            self.assertEqual(pg.locator(".clip.sel").count(), sel_before)          # the choice is kept
            self.assertIsNone(re.search(r"[\u0d00-\u0d7f]", pg.inner_text("#s-review")))
            pg.click("#lang-ml")
            pg.wait_for_selector('#presets button[aria-pressed="true"]:text-is("എല്ലാം")')
            # select none / all, then back to the three that were chosen
            chosen_ids = pg.evaluate("[...document.querySelectorAll('.clip.sel')].map(c => c.dataset.id)")
            pg.click("#rv-none")
            self.assertEqual(pg.locator(".clip.sel").count(), 0)
            self.assertTrue(pg.is_disabled("#rv-go"))
            pg.click("#rv-all")
            self.assertEqual(pg.locator(".clip.sel").count(), pg.locator(".clip").count())
            pg.click("#rv-none")
            for cid in chosen_ids:
                pg.locator(f'.clip[data-id="{cid}"] .pick').dispatch_event("click")
            self.assertEqual(pg.locator(".clip.sel").count(), 3)
            self.assertRegex(pg.inner_text(".clip .time"), r"\d+ സെക്കൻഡ്")           # the length is on the card
            # make
            pg.click("#rv-go")
            pg.wait_for_selector("#s-make:not([hidden])")
            before = pg.inner_text("#trim-text")
            pg.locator("#trim-a").focus()
            for _ in range(4):
                pg.keyboard.press("ArrowRight")
            self.assertNotEqual(pg.inner_text("#trim-text"), before)
            # zoom + follow is first and chosen by default (recommended)
            self.assertEqual(pg.get_attribute("button.layout >> nth=0", "aria-pressed"), "true")
            self.assertEqual(pg.get_attribute("#phone", "class"), "phone zoom")
            self.assertTrue(pg.is_visible("#follow-note"))
            pg.locator("button.layout").nth(1).click()                             # blurred background
            self.assertEqual(pg.get_attribute("#phone", "class"), "phone")
            self.assertFalse(pg.is_visible("#follow-note"))
            pg.locator("button.layout").nth(2).click()                             # fixed crop
            self.assertEqual(pg.get_attribute("#phone", "class"), "phone crop")
            self.assertTrue(pg.locator("button.layout").nth(4).is_disabled())      # the coming-soon layout
            pg.locator("button.layout").nth(3).click()                             # follow the action
            self.assertTrue(pg.is_visible("#follow-note"))
            self.assertFalse(pg.is_visible("#crop-row"))
            self.assertEqual(pg.get_attribute("#phone", "class"), "phone crop")
            pg.locator("button.layout").nth(2).click()
            self.assertFalse(pg.is_visible("#follow-note"))
            # one press gives every clip this layout
            self.assertTrue(pg.is_visible("#all-same"))
            pg.click("#all-same")
            self.assertNotEqual(pg.inner_text("#all-same-msg"), "")
            pg.locator("#mk-tabs button").nth(1).click()
            self.assertEqual(pg.get_attribute("button.layout >> nth=2", "aria-pressed"), "true")
            pg.locator("#mk-tabs button").nth(0).click()
            self.assertFalse(pg.is_visible("#trim-long"))                        # the clip is under a minute
            pg.fill("#title", "ടെസ്റ്റ് ക്ലിപ്പ്")
            self.assertEqual(pg.inner_text("#ph-title"), "ടെസ്റ്റ് ക്ലിപ്പ്")
            self.assertEqual(pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), True)
            pg.click("#mk-go")
            pg.wait_for_selector("#s-progress:not([hidden])")
            pg.wait_for_selector("#s-done:not([hidden])", timeout=120000)
            aid = pg.url.split("#/done/")[1]
            self.assertEqual(pg.locator("#dn-list li").count(), 3)
            self.assertIn("ടെസ്റ്റ് ക്ലിപ്പ്", pg.inner_text("#dn-list"))
            self.assertEqual(pg.locator("#dn-list li").first.locator("button").count(), 3)   # details + copy title + watch
            self.assertEqual(pg.get_attribute("#dn-studio", "href"), "https://www.youtube.com/upload")
            # a made clip plays in the dialog
            pg.locator("#dn-list li").first.locator("button").nth(2).click()
            pg.wait_for_selector("#player[open]")
            pg.click("#player-close")
            pg.click("#dn-home")
            pg.wait_for_selector("#s-home:not([hidden])")
            self.assertIn("stream_demo", pg.inner_text("#lib"))
            # settings panel opens and closes with the keyboard
            pg.click("#open-settings")
            pg.wait_for_selector("#settings[open]")
            pg.keyboard.press("Escape")
            self.assertEqual(pg.locator("#settings[open]").count(), 0)
            b.close()
        self.assertEqual(logs, [])
        clips = os.listdir(os.path.join(self.store.analyses, aid, "clips"))
        self.assertEqual(len(clips), 3)
        for name in clips:
            v = next(s for s in probe_json(os.path.join(self.store.analyses, aid, "clips", name))["streams"]
                     if s["codec_type"] == "video")
            self.assertEqual((v["width"], v["height"]), (1080, 1920))

    def test_pick_button_falls_back_to_the_browser_file_box_when_there_is_no_system_dialog(self):
        from playwright.sync_api import sync_playwright
        old = self.app.picker
        self.app.picker = lambda: None                        # as on a Python without tkinter
        try:
            with sync_playwright() as p:
                b = p.chromium.launch()
                pg = b.new_context().new_page()
                pg.goto(f"http://127.0.0.1:{self.port}/?t={TOKEN}")
                pg.wait_for_selector("#s-home:not([hidden])")
                with pg.expect_file_chooser(timeout=10000) as chooser:
                    pg.click("#pick")
                chooser.value.set_files(self.webm)
                pg.wait_for_selector("#s-review:not([hidden])", timeout=90000)
                b.close()
        finally:
            self.app.picker = old

    def test_phone_width_has_no_sideways_scroll_on_any_screen(self):
        from playwright.sync_api import sync_playwright
        aid = self.analyze()
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_context(viewport={"width": 390, "height": 844}).new_page()
            pg.goto(f"http://127.0.0.1:{self.port}/?t={TOKEN}")
            pg.wait_for_selector("#s-home:not([hidden])")
            for lang in ("ml", "en"):
                pg.click("#lang-" + lang)
                self.poll(pg, f"document.documentElement.lang === '{lang}'")
                for route, sel in (("#/", "#s-home:not([hidden])"), (f"#/review/{aid}", "#s-review:not([hidden])"),
                                   (f"#/make/{aid}", "#s-make:not([hidden])")):
                    pg.evaluate(f"location.hash = '{route}'")
                    pg.wait_for_selector(sel)
                    time.sleep(0.5)
                    self.assertTrue(pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (lang, route))
            pg.click("#lang-ml")
            time.sleep(0.3)
            b.close()

    def test_wrong_link_shows_the_help_page_not_the_app(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_context().new_page()
            resp = pg.goto(f"http://127.0.0.1:{self.port}/?t=nope")
            self.assertEqual(resp.status, 403)
            self.assertIn("അനുമതി", pg.inner_text("body"))
            b.close()


if __name__ == "__main__":
    unittest.main()
