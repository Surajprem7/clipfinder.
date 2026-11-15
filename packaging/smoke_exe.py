"""Starts the packaged program, checks that it works end to end, and stops it.

    python packaging/smoke_exe.py <path to ClipFinder(.exe)> [--require-tools] [--clean-env] [--ffmpeg-dir DIR]

It makes a small test video, opens the program on a free port without opening a browser, analyses the video,
renders one clip, and asks the program to quit. Exit code 0 only if everything worked.
--clean-env     start the program with a bare environment: no Python, ffmpeg or yt-dlp on PATH and no PYTHON* variables,
                so it can only use what it carries itself (this is the "nothing else has to be installed" test)
--ffmpeg-dir    folder with ffmpeg, used only to make the test video (never given to the program)
"""
from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tests"))
from sample_video import make_sample  # noqa: E402


KEEP = {"systemroot", "systemdrive", "windir", "temp", "tmp", "tmpdir", "userprofile", "home", "homedrive", "homepath",
        "localappdata", "appdata", "programdata", "programfiles", "programfiles(x86)", "programw6432", "comspec",
        "pathext", "processor_architecture", "number_of_processors", "os", "lang", "username"}


def clean_environment() -> dict:
    """Only what any program needs to run; PATH has the Windows folders and nothing else."""
    env = {k: v for k, v in os.environ.items() if k.lower() in KEEP}
    root = os.environ.get("SystemRoot")
    env["PATH"] = os.pathsep.join([os.path.join(root, "System32"), root]) if root else "/nonexistent"
    return env


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    exe = os.path.abspath(sys.argv[1])
    need_tools = "--require-tools" in sys.argv
    tmp = tempfile.mkdtemp(prefix="cf_smoke_")
    saved_path = os.environ.get("PATH", "")
    if "--ffmpeg-dir" in sys.argv:
        os.environ["PATH"] = os.path.abspath(sys.argv[sys.argv.index("--ffmpeg-dir") + 1]) + os.pathsep + saved_path
    video = make_sample(os.path.join(tmp, "sample.mp4"))
    os.environ["PATH"] = saved_path
    env = clean_environment() if "--clean-env" in sys.argv else None
    if env is not None:
        print("starting the program with a bare environment, PATH =", env["PATH"])
    proc = subprocess.Popen([exe, "--data", os.path.join(tmp, "data"), "--port", "0", "--no-browser"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    ok = False
    port = token = None
    try:
        t0 = time.time()
        found = None
        while time.time() - t0 < 90:
            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                break
            found = re.search(r"http://127\.0\.0\.1:(\d+)/\?t=(\S+)", line)
            if found:
                break
        if not found:
            print("FAIL: the program did not print its address")
            return 1
        port, token = int(found[1]), found[2]
        print("started on port", port)

        def req(method, path, body=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=180)
            h = {"Host": f"127.0.0.1:{port}", "Cookie": f"cf_session={token}"}
            if method != "GET":
                h.update({"Origin": f"http://127.0.0.1:{port}", "X-CF": "1", "Content-Type": "application/json"})
            c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
            r = c.getresponse()
            data = r.read()
            c.close()
            return r.status, data

        def wait_job():
            end = time.time() + 300
            while time.time() < end:
                job = json.loads(req("GET", "/api/job")[1])["job"]
                if job and job["state"] != "running":
                    return job
                time.sleep(0.5)
            raise RuntimeError("job did not finish")

        st, page = req("GET", "/")
        assert st == 200 and b"app.js" in page, "page not served"
        assert req("GET", "/app.js")[0] == 200 and req("GET", "/app.css")[0] == 200, "page files missing"
        state = json.loads(req("GET", "/api/state")[1])
        print("tools:", state["tools"])
        if need_tools:
            assert state["tools"]["ffmpeg"] and state["tools"]["ffprobe"] and state["tools"]["ytdlp"], "bundled tools not found"
        # YouTube upload: the part is inside the program and can make a secure connection (ssl is bundled)
        assert state["youtube"] == {"client": False, "connected": False}, state.get("youtube")
        st, d = req("POST", "/api/youtube/connect", {})
        assert st == 400 and json.loads(d)["error"] == "yt_not_set_up", ("youtube", st, d)
        st, d = req("POST", "/api/analyze", {"kind": "file", "path": video})
        assert st == 200, d
        job = wait_job()
        assert job["state"] == "done", job
        aid = job["result"]["analysis"]
        a = json.loads(req("GET", f"/api/analysis/{aid}")[1])
        assert a["clips"], "no clips found in the test video"
        c = a["clips"][0]
        items = [{"id": c["id"], "start": c["start"], "end": c["start"] + 8, "layout": "follow", "crop_x": 0.5, "title": "smoke"}]
        st, d = req("POST", "/api/render", {"id": aid, "run": a["run"], "items": items})
        assert st == 200, d
        job = wait_job()
        assert job["state"] == "done" and job["result"]["files"], job
        print("analysed", len(a["clips"]), "clips and rendered", len(job["result"]["files"]))
        # the Claude connector: the same program started the way Claude starts it, over stdin / stdout
        st, d = req("POST", "/api/connector-test", {})
        assert st == 200 and json.loads(d) == {"ok": True, "tools": 8}, ("connector self-test", st, d)
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_clips", "arguments": {"analysis": aid}}}]
        res = subprocess.run([exe, "--mcp", "--data", os.path.join(tmp, "data")], env=env, timeout=120, capture_output=True,
                             input=("\n".join(json.dumps(m) for m in msgs) + "\n").encode())
        replies = {}
        for line in res.stdout.splitlines():
            r = json.loads(line)                                  # every line on stdout must be protocol
            replies[r["id"]] = r
        assert len(replies[2]["result"]["tools"]) == 8, replies.get(2)
        clips = json.loads(replies[3]["result"]["content"][0]["text"])["clips"]
        assert clips, replies.get(3)
        print("connector OK:", len(replies[2]["result"]["tools"]), "tools,", len(clips), "clips")
        if need_tools:
            st, d = req("POST", "/api/update-ytdlp", {})        # only checks the button is wired; the result depends on the network
            assert st == 200, d
        ok = True
    except Exception as exc:                                     # noqa: BLE001 - this is a test script: report and fail
        print("FAIL:", type(exc).__name__, exc)
    finally:
        try:
            if port:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                c.request("POST", "/api/quit", body=b"{}", headers={
                    "Host": f"127.0.0.1:{port}", "Cookie": f"cf_session={token}", "Origin": f"http://127.0.0.1:{port}",
                    "X-CF": "1", "Content-Type": "application/json"})
                c.getresponse().read()
                c.close()
        except Exception:                                        # noqa: BLE001
            pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            print("FAIL: the program did not stop when asked")
            ok = False
    print("OK" if ok else "NOT OK")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
