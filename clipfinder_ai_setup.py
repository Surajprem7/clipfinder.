"""clipfinder: download the AI helper (llama.cpp + one vision model) into <data>/ai. Run by setup_windows.bat.

  python clipfinder_ai_setup.py --data <data folder>

What it downloads, over HTTPS only, from two fixed places:
  * llama.cpp's newest Windows build from GitHub (ggml-org/llama.cpp, the "win-vulkan-x64" zip: it uses the
    graphics card when it can and the processor otherwise), about 40 MB;
  * Google's Gemma 3 4B model in the GGUF form made by the llama.cpp team (huggingface.co/ggml-org), about 3.3 GB.
Every file is checked against the SHA-256 that the site publishes before it is used. A half-finished download is
continued next time. Run it again later to get a newer llama.cpp; the model is only downloaded once.
Nothing is installed into Windows: delete the <data>/ai folder to remove it all."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

GITHUB_API_LIST = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=15"
GITHUB_DOWNLOAD = "https://github.com/ggml-org/llama.cpp/releases/download/"
ASSET_END = "-bin-win-vulkan-x64.zip"
HF_REPO = "ggml-org/gemma-3-4b-it-GGUF"
HF_TREE = f"https://huggingface.co/api/models/{HF_REPO}/tree/main"
HF_FILE = f"https://huggingface.co/{HF_REPO}/resolve/main/"
MODEL, MMPROJ = "gemma-3-4b-it-Q4_K_M.gguf", "mmproj-model-f16.gguf"
NEED_FREE = 4_500_000_000
UA = {"User-Agent": "clipfinder-setup"}


class SetupError(Exception):
    pass


def say(msg: str) -> None:
    print(msg, flush=True)


def get_json(url: str):
    req = urllib.request.Request(url, headers={**UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            if not r.geturl().startswith("https://"):
                raise SetupError("refused a non-HTTPS answer")
            return json.loads(r.read(5_000_000).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SetupError(f"could not read {url}: {exc}") from None


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(4 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download(url: str, target: str, size: int, sha: str) -> None:
    """Download url to target (continuing a .part file), then check size and SHA-256. sha may be '' (no check)."""
    if os.path.isfile(target) and os.path.getsize(target) == size and (not sha or sha256_of(target) == sha):
        say(f"  already here: {os.path.basename(target)}")
        return
    part = target + ".part"
    have = os.path.getsize(part) if os.path.isfile(part) else 0
    if size and have > size:
        os.remove(part)
        have = 0
    headers = dict(UA)
    if have:
        headers["Range"] = f"bytes={have}-"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            if not r.geturl().startswith("https://"):
                raise SetupError("refused a non-HTTPS download")
            if have and r.status != 206:          # the server would not continue: start again
                have = 0
            mode = "ab" if have else "wb"
            done, last, t0 = have, 0.0, time.monotonic()
            with open(part, mode) as out:
                while True:
                    block = r.read(1 << 20)
                    if not block:
                        break
                    out.write(block)
                    done += len(block)
                    now = time.monotonic()
                    if now - last > 2:
                        speed = (done - have) / max(1e-3, now - t0) / 1e6
                        if size:
                            say(f"  {os.path.basename(target)}: {done * 100 // size}%  ({done / 1e9:.2f} of {size / 1e9:.2f} GB, {speed:.1f} MB/s)")
                        else:
                            say(f"  {os.path.basename(target)}: {done / 1e6:.0f} MB ({speed:.1f} MB/s)")
                        last = now
    except (urllib.error.URLError, OSError) as exc:
        raise SetupError(f"download stopped ({exc}). Run the setup again to continue where it stopped.") from None
    if size and os.path.getsize(part) != size:
        raise SetupError(f"{os.path.basename(target)} is incomplete. Run the setup again to continue.")
    if sha:
        say(f"  checking {os.path.basename(target)} ...")
        if sha256_of(part) != sha:
            os.remove(part)
            raise SetupError(f"{os.path.basename(target)} did not match its published checksum and was deleted. Run the setup again.")
    os.replace(part, target)


def safe_extract(zpath: str, dest: str) -> None:
    root = os.path.realpath(dest)
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/") or ":" in name:
                raise SetupError(f"refused a strange path in the zip: {name!r}")
            target = os.path.realpath(os.path.join(root, name))
            if target != root and not target.startswith(root + os.sep):
                raise SetupError(f"refused a strange path in the zip: {name!r}")
        z.extractall(root)


def find_server(folder: str) -> str:
    for dirpath, _dirs, files in os.walk(folder):
        for f in files:
            if f.lower() in ("llama-server.exe", "llama-server"):
                return os.path.join(dirpath, f)
    raise SetupError("llama-server was not found in the download")


def good_tag(tag) -> bool:
    """llama.cpp's release names: 'b7123' (build numbers) and, since late 2026, 'v1.0.0'."""
    return isinstance(tag, str) and re.fullmatch(r"b\d{1,7}|v\d{1,4}(\.\d{1,4}){0,3}", tag) is not None


def latest_release(asset_end: str) -> tuple:
    """(tag, asset dict) of the newest llama.cpp release that has the Windows file. The project's "latest" release is
    not always one of its program builds (in Nov 2026 it was 'v1.0.0' without Windows files), so the newest few
    releases are looked at. GitHub allows 60 such questions an hour per internet address (an office shares one)."""
    rels = get_json(GITHUB_API_LIST)
    if not isinstance(rels, list):
        msg = str(rels.get("message", ""))[:100] if isinstance(rels, dict) else ""
        raise SetupError(f"GitHub did not list the llama.cpp releases ({msg or 'unexpected answer'}). Try again in an hour.")
    for rel in rels:
        if not isinstance(rel, dict) or rel.get("draft"):    # the build-numbered ones are marked "pre-release"
            continue
        tag, assets = rel.get("tag_name"), rel.get("assets")
        if not good_tag(tag) or not isinstance(assets, list):
            continue
        asset = next((a for a in assets if isinstance(a, dict) and str(a.get("name", "")).endswith(asset_end)), None)
        if asset:
            return tag, asset
    seen = "; ".join(f"{r.get('tag_name')}: " + ", ".join(str(x.get("name")) for x in (r.get("assets") or [])
                                                    if isinstance(x, dict) and "win" in str(x.get("name")))[:200]
                     for r in rels[:4] if isinstance(r, dict))
    raise SetupError(f"none of the newest llama.cpp releases has a '{asset_end}' file (seen: {seen[:900]})")


def get_llama(ai: str, asset_end: str) -> str:
    say("[AI 1/2] llama.cpp (the program that runs the model)")
    tag, asset = latest_release(asset_end)
    url = asset.get("browser_download_url", "")
    if not isinstance(url, str) or not url.startswith(GITHUB_DOWNLOAD) or not str(asset.get("name", "")).endswith(asset_end) \
            or "/" in str(asset.get("name")) or "\\" in str(asset.get("name")):
        raise SetupError("unexpected download address for llama.cpp")
    digest = asset.get("digest") or ""
    sha = digest.split(":", 1)[1] if isinstance(digest, str) and digest.startswith("sha256:") else ""
    dest = os.path.join(ai, f"llama-{tag}")
    if os.path.isdir(dest):
        try:
            server = find_server(dest)
            say(f"  already here: llama.cpp {tag}")
            return server
        except SetupError:
            shutil.rmtree(dest, ignore_errors=True)
    zpath = os.path.join(ai, asset["name"])
    say(f"  llama.cpp {tag}" + ("" if sha else " (GitHub gave no checksum for it)"))
    download(url, zpath, int(asset.get("size") or 0), sha)
    tmp = dest + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    safe_extract(zpath, tmp)
    os.replace(tmp, dest)
    os.remove(zpath)
    for old in os.listdir(ai):                          # keep only the newest llama.cpp
        p = os.path.join(ai, old)
        if old.startswith("llama-") and os.path.isdir(p) and p != dest:
            shutil.rmtree(p, ignore_errors=True)
    return find_server(dest)


def get_model(ai: str) -> tuple:
    say("[AI 2/2] the model (Gemma 3 4B, can look at pictures; about 3.3 GB, only once)")
    tree = get_json(HF_TREE)
    if not isinstance(tree, list):
        raise SetupError("Hugging Face did not list the model files as expected")
    info = {e.get("path"): e for e in tree if isinstance(e, dict)}
    models = os.path.join(ai, "models")
    os.makedirs(models, exist_ok=True)
    out = []
    for name in (MODEL, MMPROJ):
        e = info.get(name)
        lfs = e.get("lfs") if isinstance(e, dict) else None
        sha = lfs.get("oid") if isinstance(lfs, dict) else None
        size = lfs.get("size") if isinstance(lfs, dict) else None
        if not isinstance(sha, str) or len(sha) != 64 or not isinstance(size, int):
            raise SetupError(f"no checksum published for {name}")
        download(HF_FILE + name, os.path.join(models, name), size, sha)
        out.append(os.path.join(models, name))
    return tuple(out)


def check_devices(server: str) -> None:
    try:
        res = subprocess.run([server, "--list-devices"], capture_output=True, text=True, timeout=60,
                             cwd=os.path.dirname(server), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        lines = [l.strip() for l in (res.stdout + res.stderr).splitlines() if l.strip()]
        gpus = [l for l in lines if ":" in l and ("Vulkan" in l or "CUDA" in l)]
        say("  graphics card found: " + gpus[0] if gpus else "  no usable graphics card found: the processor will be used (slower)")
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetupError(f"llama.cpp does not start on this computer: {exc}") from None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="clipfinder_ai_setup")
    ap.add_argument("--data", required=True, help="the app's data folder; the AI goes into <data>/ai")
    ap.add_argument("--asset", default=ASSET_END, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    ai = os.path.join(os.path.abspath(a.data), "ai")
    os.makedirs(ai, exist_ok=True)
    try:
        if shutil.disk_usage(ai).free < NEED_FREE and not os.path.isfile(os.path.join(ai, "models", MODEL)):
            raise SetupError("not enough free space: about 4.5 GB is needed on this drive")
        server = get_llama(ai, a.asset)
        model, mmproj = get_model(ai)
        check_devices(server)
        rel = lambda p: os.path.relpath(p, ai)      # noqa: E731
        with open(os.path.join(ai, "ready.json"), "w", encoding="utf-8") as fh:
            json.dump({"server": rel(server), "model": rel(model), "mmproj": rel(mmproj)}, fh, indent=2)
    except SetupError as exc:
        say(f"\nAI setup did not finish: {exc}")
        return 1
    say("\nAI helper is ready. In the app: Settings -> AI helper -> model 'local/gemma-3-4b'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
