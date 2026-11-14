#!/usr/bin/env python3
"""clipfinder - find action moments in gameplay recordings from audio and motion signals.

No AI model, no network access, no accounts. It only needs:
  * Python 3.9+
  * numpy            (pip install numpy)
  * ffmpeg + ffprobe on PATH

Workflow
  1. python clipfinder.py analyze  my_stream.mp4 --preset slowmo
       -> my_stream_clipfinder/report.html   (candidates with thumbnails, filterable)
  2. open report.html, tick the clips you like, copy the command it prints
  3. python clipfinder.py render  my_stream_clipfinder --run <id> --ids 3 7
       -> my_stream_clipfinder/clips/*.mp4   (1080x1920, ready for Shorts)

How it decides (all heuristics - tune them on your own footage):
  loud_peak         loudness jumps well above the recent baseline
  motion_burst      on-screen motion jumps well above the recent baseline
  quiet_then_burst  a calm stretch is followed by a sudden loud/busy moment
  slow_motion       shortly after an intense moment, motion drops sharply (but does not
                    freeze) while the audio gets lower/muffled - slow-mo / kill-cam /
                    replay candidates. This is the least reliable detector.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, fields

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

__version__ = "1.3.1"

BIN = 0.5              # seconds per analysis bin
SR = 16000             # audio sample rate used for analysis
SPEECH_LO, SPEECH_HI = 300.0, 3400.0   # speech band in Hz
FF_IN = ["-protocol_whitelist", "file,pipe"]   # local files only: never follow URLs or playlists inside a media file
VID_FPS = 4            # frames per second sampled for motion
VID_W, VID_H = 64, 36  # tiny grayscale frames are enough for motion

DETECTORS = {
    "loud_peak": (
        "ശബ്ദം ഉയരുന്നു",
        "പെട്ടെന്ന് ശബ്ദം കൂടുന്ന ഭാഗം (സ്ഫോടനം, ഹിറ്റ്, നിലവിളി)",
        "sudden jump in loudness (explosions, hits, shouting)",
    ),
    "motion_burst": (
        "ചലനം കൂടുന്നു",
        "സ്ക്രീനിൽ പെട്ടെന്ന് ചലനം കൂടുന്ന ഭാഗം",
        "sudden jump in on-screen motion",
    ),
    "quiet_then_burst": (
        "ശാന്തം → പൊട്ടിത്തെറി",
        "ശാന്തമായ ഭാഗത്തിന് ശേഷം പെട്ടെന്ന് ശബ്ദം/ചലനം (ക്ലച്ച്, ജമ്പ് സ്കെയർ)",
        "calm stretch followed by a sudden loud or busy moment (clutch, jump scare)",
    ),
    "voice_excitement": (
        "ശബ്ദം/ആർപ്പ് (സംസാര ബാൻഡ്)",
        "സംസാര ബാൻഡിൽ (300-3400 Hz) ശബ്ദം പെട്ടെന്ന് കൂടുന്നു (ഉച്ചത്തിലുള്ള സംസാരം, ആർപ്പ്). സംഗീതവും ഇതിൽ പെടാം",
        "speech-band (300-3400 Hz) energy jumps while the rest of the sound does not (shouting, cheering; music can also trigger it)",
    ),
    "slow_motion": (
        "സ്ലോ-മോഷൻ സാധ്യത",
        "തീവ്രമായ നിമിഷത്തിന് ശേഷം ചലനം കുറയുകയും ശബ്ദം മങ്ങുകയും ചെയ്യുന്നു (സ്ലോ-മോഷൻ/റീപ്ലേ)",
        "after an intense moment motion drops and audio gets muffled (slow-mo / replay; heuristic)",
    ),
}

DEFAULT_WEIGHTS = {"loud_peak": 1.0, "motion_burst": 1.0, "quiet_then_burst": 1.2, "slow_motion": 1.5,
                   "voice_excitement": 1.0}

PRESETS = {
    "all": {},
    "action": {"use": ["loud_peak", "motion_burst"]},
    "slowmo": {"require": ["slow_motion"]},
    "clutch": {"require": ["quiet_then_burst"]},
    "voice": {"require": ["voice_excitement"]},
}


@dataclass
class Params:
    use: list = field(default_factory=lambda: list(DETECTORS))
    require: list = field(default_factory=list)
    weights: dict = field(default_factory=dict)
    top: int = 20
    min_score: float = 0.25      # a detector must reach this to create an event
    tag_min: float = 0.25        # a detector must reach this inside a window to be tagged
    z_min: float = 2.0           # how far above the baseline (robust z) counts as unusual
    baseline_s: float = 120.0    # length of the rolling baseline window
    merge_gap: float = 8.0       # events closer than this form one candidate
    pre: float = 8.0             # seconds kept before the first event
    post: float = 12.0           # seconds kept after the last event
    min_len: float = 15.0
    max_len: float = 59.0
    skip_start: float = 0.0
    skip_end: float = 0.0
    keep_static: bool = False    # keep windows that are mostly a still picture (menus)
    static_thr: float = 0.2      # motion below this counts as "still picture"
    flat_thr: float = 2.0        # picture detail below this counts as a flat screen (loading, end card)
    slowmo_trigger: bool = True  # slow_motion must follow an intense moment
    slowmo_ratio: float = 0.5    # motion must fall to <= this fraction of the recent peak
    slowmo_min_s: float = 2.0


# --------------------------------------------------------------------------- helpers

def log(msg: str, quiet: bool = False) -> None:
    if not quiet:
        print(msg, file=sys.stderr, flush=True)


class ClipError(SystemExit):
    """A problem to show to the user. A SystemExit (exit code 2) so the command line stops cleanly,
    but it carries the message so another front end (the web app) can show it."""

    def __init__(self, message: str):
        super().__init__(2)
        self.message = message


class Cancelled(Exception):
    pass


CANCEL = threading.Event()     # set by the web app to stop a running job


QUIET_ERRORS = False           # the web app shows errors itself instead of printing them


def die(msg: str) -> "None":
    if not QUIET_ERRORS:
        print(f"ERROR: {msg}", file=sys.stderr)
    raise ClipError(msg)


def safe_name(text: str, maxlen: int = 60) -> str:
    """A file-name-safe version of text that keeps letters of every language (including Malayalam marks)."""
    out = []
    for ch in str(text):
        if ch.isalnum() or ch in "-_" or unicodedata.category(ch).startswith("M"):
            out.append(ch)
        elif ch.isspace() or ch in ".,":
            out.append("_")
    name = "".join(out).strip("_.-")
    while "__" in name:
        name = name.replace("__", "_")
    name = name[:maxlen].strip("_.-")
    if name.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                                      *(f"LPT{i}" for i in range(1, 10))}:
        name += "_"
    return name


def fmt_time(t: float) -> str:
    t = int(round(max(0.0, t)))
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}"


def atomic_write(path: str, mode: str, writer, **kw) -> None:
    """Write to a temporary file, then rename - a crash or a second run never leaves a half-written file."""
    tmp = f"{path}.tmp{os.getpid()}"
    try:
        with open(tmp, mode, **kw) as fh:
            writer(fh)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def make_dir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        die(f"cannot create the folder {path}: {exc.strerror or exc}")


def shell_safe(path: str) -> bool:
    """True if the path can sit inside double quotes in cmd, PowerShell and bash without being interpreted."""
    return not any(ch in path for ch in '"$`%!') and not any(ord(c) < 32 or c == "\x7f" for c in path)


def tool(name: str) -> str:
    """Full path of a helper program found on PATH (so Windows never picks up a look-alike from the current folder)."""
    return shutil.which(name) or name


def need_tools() -> None:
    for name in ("ffmpeg", "ffprobe"):
        if shutil.which(name) is None:
            die(f"'{name}' was not found on PATH. Install ffmpeg first (see README).")


ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}


def parse_youtube_url(url: str) -> str:
    """Return the URL if it is a plain https YouTube link, otherwise stop with an error."""
    m = re.fullmatch(r"https://([A-Za-z0-9.-]+)(?::443)?(/[^\s]*)?", url.strip())
    if not m or m.group(1).lower() not in ALLOWED_HOSTS:
        die("only https links from youtube.com / youtu.be are accepted")
    return url.strip()


FULL_FORMAT = ["-f", "bv*[height<=1080]+ba/b[height<=1080]/b"]
LOW_FORMAT = ["-S", "res:360", "-f", "bv*+ba/b"]       # about 360p video + the best sound: a fraction of the size


def ytdlp_time(t: float) -> str:
    """Seconds -> H:MM:SS.cc, a form yt-dlp accepts for --download-sections."""
    t = max(0.0, float(t))
    h, rest = divmod(t, 3600)
    m, sec = divmod(rest, 60)
    return f"{int(h)}:{int(m):02d}:{sec:05.2f}"


DOWNLOAD_EXT = ("mp4", "mkv", "webm", "mov", "m4v", "flv", "avi", "3gp")


def _find_download(out_dir: str, stem: str):
    """A finished download is exactly <stem>.<video extension>. Half-finished pieces that yt-dlp leaves behind
    (<stem>.f137.mp4 = video without sound, *.part, *.ytdl) are not finished downloads and are never used."""
    pattern = re.compile(re.escape(stem) + r"\.(?:" + "|".join(DOWNLOAD_EXT) + ")", re.IGNORECASE)
    for name in sorted(os.listdir(out_dir)):
        if pattern.fullmatch(name):
            f = os.path.join(out_dir, name)
            if os.path.isfile(f) and os.path.getsize(f) > 0:
                return f
    return None


def _run_ytdlp(args: list, out_dir: str, stem: str, url: str) -> str:
    """Run yt-dlp (list arguments, no shell) to fetch url into out_dir/<stem>.<ext>. Stoppable with CANCEL."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", stem):
        die("internal error: bad file stem")
    cmd = [tool("yt-dlp"), "--ignore-config", "--no-playlist", "--no-progress", *args, "--merge-output-format", "mp4",
           "-o", os.path.join(out_dir, stem + ".%(ext)s").replace("%", "%%").replace("%%(ext)s", "%(ext)s"),
           "--", url]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", stdin=subprocess.DEVNULL)
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if CANCEL.is_set():
                proc.kill()
                proc.communicate()
                raise Cancelled()
    have = _find_download(out_dir, stem)
    if proc.returncode != 0 or not have:
        tail = (stderr or stdout or "").strip().splitlines()[-3:]
        die("yt-dlp could not download the video:\n  " + "\n  ".join(tail))
    return have


def _ytdlp_ready(url: str, out_dir: str) -> str:
    url = parse_youtube_url(url)
    if shutil.which("yt-dlp") is None:
        die("'yt-dlp' was not found on PATH. Install it (pip install yt-dlp) or download the video yourself.")
    make_dir(out_dir)
    return url


def download_video(url: str, out_dir: str, quiet: bool = False, low: bool = False) -> str:
    """Download your own YouTube video with yt-dlp (must be installed separately). Returns the file path.
    low=True fetches a small ~360p copy: enough to find the moments, a fraction of the download."""
    url = _ytdlp_ready(url, out_dir)
    stem = "lowres" if low else "source"
    have = _find_download(out_dir, stem)
    if have:
        log(f"Using the video already downloaded: {have}", quiet)
        return have
    log("Downloading the video with yt-dlp (this can take a while)...", quiet)
    return _run_ytdlp(LOW_FORMAT if low else FULL_FORMAT, out_dir, stem, url)


def download_section(url: str, start: float, end: float, out_dir: str, stem: str, quiet: bool = False) -> str:
    """Download only start..end (seconds) of a video in up to 1080p. Cuts are forced to be frame-accurate."""
    url = _ytdlp_ready(url, out_dir)
    if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= 86400):
        die("bad time range for the section download")
    have = _find_download(out_dir, stem)
    if have:
        return have
    log(f"Downloading {fmt_time(start)} - {fmt_time(end)} with yt-dlp ...", quiet)
    return _run_ytdlp([*FULL_FORMAT, "--download-sections", f"*{ytdlp_time(start)}-{ytdlp_time(end)}",
                       "--force-keyframes-at-cuts"], out_dir, stem, url)


def probe(path: str) -> dict:
    if not os.path.isfile(path):
        die(f"video file not found: {path}")
    res = subprocess.run(
        [tool("ffprobe"), "-v", "error", *FF_IN, "-print_format", "json", "-show_format", "-show_streams", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
    )
    if res.returncode != 0:
        die(f"ffprobe could not read the file: {res.stderr.strip()[:300]}")
    try:
        info = json.loads(res.stdout)
    except ValueError:
        die("ffprobe gave an unreadable answer for this file")
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        die("the file has no video stream")
    duration = 0.0
    for raw in (info.get("format", {}).get("duration"), video.get("duration")):
        try:
            duration = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration > 0:
            break
        duration = 0.0
    if duration <= 0:
        die("the file does not say how long it is (a raw or damaged stream?). Convert it first: "
            "ffmpeg -i input -c copy output.mp4")
    return {
        "duration": duration,
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
    }


def clip01(x):
    return np.clip(x, 0.0, 1.0)


def smooth(x: np.ndarray, k: int = 3) -> np.ndarray:
    if len(x) < k:
        return x.astype(float).copy()
    pad = k // 2
    xp = np.pad(x.astype(float), (pad, pad), mode="edge")
    return np.convolve(xp, np.ones(k) / k, mode="valid")


def _odd(k: int) -> int:
    k = int(k)
    return k if k % 2 == 1 else k + 1


def rolling_median(x: np.ndarray, w: int, chunk: int = 4000) -> np.ndarray:
    n = len(x)
    if n < 3:
        return x.astype(float).copy()
    w = _odd(w)
    if w > n:
        w = n if n % 2 == 1 else n - 1
    pad = w // 2
    xp = np.pad(x.astype(float), (pad, pad), mode="reflect")
    out = np.empty(n)
    for s in range(0, n, chunk):
        e = min(n, s + chunk)
        out[s:e] = np.median(sliding_window_view(xp[s:e + w - 1], w), axis=1)
    return out


def robust_z(x: np.ndarray, win: int, min_scale: float, max_scale: float):
    """z-score against a rolling median / MAD baseline (robust to long loud sections).

    The scale is clamped: a floor stops tiny wobbles in a very steady signal from looking
    huge, and a cap stops a very mixed stretch (menus + fights + music) from hiding real peaks.
    """
    med = rolling_median(x, win)
    mad = rolling_median(np.abs(x - med), win) * 1.4826
    z = (x - med) / np.clip(mad, min_scale, max_scale)
    return np.clip(z, -10.0, 12.0), med


def _prev_windows(x: np.ndarray, k: int) -> np.ndarray:
    """row t holds x[t-k .. t-1] (edge-padded at the start)."""
    xp = np.concatenate([np.full(k, x[0], dtype=float), x.astype(float)])
    return sliding_window_view(xp, k)[: len(x)]


def prev_median(x, k):
    return np.median(_prev_windows(x, k), axis=1)


def prev_mean(x, k):
    return _prev_windows(x, k).mean(axis=1)


def prev_max(x, k):
    return _prev_windows(x, k).max(axis=1)


def trailing_max(x: np.ndarray, k: int) -> np.ndarray:
    """max of x[t-k .. t] for every t."""
    xp = np.concatenate([np.zeros(k), x.astype(float)])
    return sliding_window_view(xp, k + 1).max(axis=1)


def runs(mask: np.ndarray):
    out, i, n = [], 0, len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            out.append((i, j))
            i = j
        else:
            i += 1
    return out


# --------------------------------------------------------------------------- signal extraction

def _audio_features(path: str):
    hop = int(SR * BIN)
    cmd = [tool("ffmpeg"), "-nostdin", "-v", "error", *FF_IN, "-i", path, "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    win = np.hanning(hop)
    freqs = np.fft.rfftfreq(hop, 1.0 / SR)
    band = (freqs >= SPEECH_LO) & (freqs <= SPEECH_HI)
    loud, cent, vband, vfrac = [], [], [], []
    try:
        while True:
            if CANCEL.is_set():
                proc.kill()
                raise Cancelled()
            buf = proc.stdout.read(hop * 2)
            if len(buf) < hop * 2:
                break
            x = np.frombuffer(buf, dtype=np.int16).astype(np.float64) / 32768.0
            rms = math.sqrt(float(np.mean(x * x)))
            loud.append(20.0 * math.log10(max(rms, 3e-5)))   # floor about -90 dB
            mag = np.abs(np.fft.rfft(x * win))
            total = float(mag.sum())
            cent.append(float((freqs * mag).sum() / total) if total > 1e-6 else 0.0)
            pw = mag * mag
            bp, tp = float(pw[band].sum()) + 1e-9, float(pw.sum()) + 1e-9
            vband.append(10.0 * math.log10(bp))              # energy in the speech band (dB, relative)
            vfrac.append(10.0 * math.log10(bp / tp))         # how much of all sound is in that band (dB)
    finally:
        proc.stdout.close()
        proc.wait()
    return np.array(loud), np.array(cent), np.array(vband), np.array(vfrac)


def _video_motion(path: str, expected: float = 0.0, progress=None):
    cmd = [
        tool("ffmpeg"), "-nostdin", "-v", "error", *FF_IN, "-i", path, "-an",
        "-vf", f"fps={VID_FPS},scale={VID_W}:{VID_H}:flags=area,format=gray",
        "-f", "rawvideo", "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    size = VID_W * VID_H
    prev, diffs, det = None, [], []
    try:
        while True:
            if CANCEL.is_set():
                proc.kill()
                raise Cancelled()
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            if progress and expected > 0 and len(diffs) % 80 == 0:
                progress("read", min(0.99, len(diffs) / expected))
            f = np.frombuffer(buf, dtype=np.uint8).astype(np.float32)
            diffs.append(0.0 if prev is None else float(np.abs(f - prev).mean()))
            g = f.reshape(VID_H, VID_W)
            # detail = average step between neighbouring pixels: a smooth gradient has almost none
            det.append(float(np.abs(np.diff(g, axis=1)).mean() + np.abs(np.diff(g, axis=0)).mean()))
            prev = f
    finally:
        proc.stdout.close()
        proc.wait()
    d, e = np.array(diffs), np.array(det)
    if len(d) > 1:
        d[0] = d[1]
    per_bin = int(round(VID_FPS * BIN))
    nb = len(d) // per_bin
    if not nb:
        return np.zeros(0), np.zeros(0)
    return (d[: nb * per_bin].reshape(nb, per_bin).mean(axis=1),
            e[: nb * per_bin].reshape(nb, per_bin).mean(axis=1))


def _fit(x: np.ndarray, n: int, fill: float) -> np.ndarray:
    if len(x) >= n:
        return x[:n].astype(float)
    pad = x[-1] if len(x) else fill
    return np.concatenate([x.astype(float), np.full(n - len(x), pad)])


def source_key(path: str) -> str:
    st = os.stat(path)
    return f"{os.path.abspath(path)}|{st.st_size}|{st.st_mtime_ns}|sig3"


def extract_signals(path: str, info: dict, quiet: bool = False, progress=None) -> dict:
    t0 = time.time()
    log("Reading audio and video (this is the slow part; it is only done once per file)...", quiet)
    with ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(_audio_features, path) if info["has_audio"] else None
        fv = pool.submit(_video_motion, path, info["duration"] * VID_FPS, progress)
        M, D = fv.result()
        L, C, V, R = fa.result() if fa else (np.zeros(0),) * 4
    n = int(info["duration"] / BIN) if info["duration"] > 0 else max(len(M), len(L))
    n = max(n, 1)
    if len(M) == 0:
        die("could not decode any video frames from the file")
    sig = {
        "L": _fit(L, n, -90.0) if info["has_audio"] else np.full(n, -90.0),
        "C": _fit(C, n, 0.0) if info["has_audio"] else np.zeros(n),
        "V": _fit(V, n, -90.0) if info["has_audio"] else np.full(n, -90.0),
        "R": _fit(R, n, -10.0) if info["has_audio"] else np.full(n, -10.0),
        "M": _fit(M, n, 0.0),
        "D": _fit(D, n, 50.0),
        "has_audio": np.array(bool(info["has_audio"])),
    }
    log(f"  decoded in {time.time() - t0:.0f}s", quiet)
    return sig


def load_or_extract(path: str, info: dict, out: str, reuse: bool, quiet: bool, progress=None) -> dict:
    cache = os.path.join(out, "signals.npz")
    key = source_key(path)
    if reuse and os.path.isfile(cache):
        try:
            with np.load(cache, allow_pickle=False) as z:
                if str(z["key"]) == key:
                    log("Using cached signals (change conditions freely - this is instant).", quiet)
                    return {k: z[k] for k in ("L", "C", "V", "R", "M", "D", "has_audio")}
        except Exception:
            pass
    sig = extract_signals(path, info, quiet, progress)
    atomic_write(cache, "wb", lambda fh: np.savez_compressed(fh, key=np.array(key), **sig))
    return sig


# --------------------------------------------------------------------------- detectors

def compute_curves(sig: dict, p: Params):
    """Return ({detector: score curve 0..1 per bin}, static mask, debug arrays)."""
    L, M, C = (np.asarray(sig[k], float) for k in ("L", "M", "C"))
    V, R = np.asarray(sig["V"], float), np.asarray(sig["R"], float)
    has_audio = bool(sig["has_audio"])
    n = len(M)
    curves = {d: np.zeros(n) for d in DETECTORS}
    static = np.zeros(n, dtype=bool)
    if n < 20:
        return curves, static, {}
    win = max(21, int(p.baseline_s / BIN))
    Ls, Ms, Cs = smooth(L), smooth(M), smooth(C)
    # "static" = a still picture (menu) OR a flat screen with no picture detail (loading / end card)
    flat = smooth(np.asarray(sig["D"], float)) < p.flat_thr
    static = (Ms < p.static_thr) | flat
    k5, k6, k8 = int(5 / BIN), int(6 / BIN), int(8 / BIN)

    # motion and pitch are compared in the log domain (they change by factors, not amounts)
    zM, _ = robust_z(np.log1p(Ms), win, 0.15, 1.0)
    motion = clip01((zM - p.z_min) / 4.0) * clip01((zM - prev_median(zM, k5)) / 3.0)
    if has_audio:
        zL, _ = robust_z(Ls, win, 1.5, 6.0)
        zC, _ = robust_z(np.log(Cs + 50.0), win, 0.1, 1.0)
        loud = clip01((zL - p.z_min) / 4.0) * clip01((zL - prev_median(zL, k5)) / 3.0)
    else:
        zL, zC, loud = np.zeros(n), np.zeros(n), np.zeros(n)
    voice = np.zeros(n)
    if has_audio:
        # speech-band energy jumps AND a larger share of the sound is in that band than usual
        # (an explosion or gunfire is broadband, so its share does not change)
        zV, _ = robust_z(smooth(V), win, 1.5, 6.0)
        zR, _ = robust_z(smooth(R), win, 1.0, 4.0)
        voice = (clip01((zV - p.z_min) / 4.0) * clip01((zV - prev_median(zV, k5)) / 3.0)
                 * clip01(zR / 2.0))

    # calm stretch -> sudden burst. "Calm" needs BOTH sound and picture below their baseline.
    calm_m = -prev_mean(zM, k6)
    if has_audio:
        calm = np.minimum(-prev_mean(zL, k6), calm_m)
        burst_z = np.maximum(zL, zM)
    else:
        calm, burst_z = calm_m, zM
    qtb = clip01((calm - 0.3) / 1.0) * clip01((burst_z - p.z_min) / 4.0)

    # slow motion: intense moment, then motion falls (but does not freeze) and audio is muffled
    trig = trailing_max(np.maximum(loud, motion), k8)
    ratio = Ms / (prev_max(Ms, k8) + 0.3)
    drop = (ratio <= p.slowmo_ratio) & (Ms >= 0.12)
    if has_audio:
        muffled = (Cs / (prev_median(Cs, k8) + 1.0) <= 0.75) | (zC <= -2.0)
        a_factor = 1.0
    else:
        muffled, a_factor = np.ones(n, dtype=bool), 0.7
    cond = drop & muffled
    if p.slowmo_trigger:
        cond &= trig >= 0.25
    slow = np.zeros(n)
    for a, b in runs(cond):
        secs = (b - a) * BIN
        if secs < p.slowmo_min_s:
            continue
        depth = float(clip01(1.0 - float(ratio[a:b].mean()) / max(p.slowmo_ratio, 1e-6)))
        slow[a] = float(clip01(secs / 6.0)) * (0.5 + 0.5 * depth) * a_factor   # one event per run

    curves.update(loud_peak=loud, motion_burst=motion, quiet_then_burst=qtb, slow_motion=slow,
                  voice_excitement=voice)
    return curves, static, {"zL": zL, "zM": zM, "zC": zC, "Ms": Ms, "flat": flat}


def pick_events(curve: np.ndarray, thr: float, sep_bins: int):
    chosen = []
    for i in np.argsort(-curve, kind="stable"):
        if curve[i] < thr:
            break
        if all(abs(int(i) - j) >= sep_bins for j in chosen):
            chosen.append(int(i))
    return sorted(chosen)


def _num(name: str, v, lo: float, hi: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        die(f"setting '{name}' must be a number between {lo:g} and {hi:g} (got {v!r})")
    return float(v)


def normalize_params(p: Params) -> Params:
    for name, val in (("use", p.use), ("require", p.require)):
        if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
            die(f"setting '{name}' must be a list of condition names")
    if not isinstance(p.weights, dict):
        die("setting 'weights' must be a mapping like {\"slow_motion\": 2}")
    for name in ("keep_static", "slowmo_trigger"):
        if not isinstance(getattr(p, name), bool):
            die(f"setting '{name}' must be true or false")
    if isinstance(p.top, bool) or not isinstance(p.top, int) or not 1 <= p.top <= 500:
        die(f"setting 'top' must be a whole number between 1 and 500 (got {p.top!r})")
    for name, lo, hi in (("min_score", 0, 1), ("tag_min", 0, 1), ("z_min", 0, 12), ("baseline_s", 20, 3600),
                         ("merge_gap", 0, 300), ("pre", 0, 300), ("post", 0, 300), ("min_len", 1, 900),
                         ("max_len", 1, 900), ("skip_start", 0, 1e7), ("skip_end", 0, 1e7),
                         ("static_thr", 0, 10), ("flat_thr", 0, 50), ("slowmo_ratio", 0.05, 1), ("slowmo_min_s", 0, 120)):
        setattr(p, name, _num(name, getattr(p, name), lo, hi))
    for name, val in p.weights.items():
        _num(f"weights.{name}", val, 0, 100)
    for name in list(p.use) + list(p.require):
        if name not in DETECTORS:
            die(f"unknown condition '{name}'. Available: {', '.join(DETECTORS)}")
    for name in p.weights:
        if name not in DETECTORS:
            die(f"unknown condition '{name}' in weights")
    p.use = list(dict.fromkeys(list(p.use) + list(p.require)))
    if not p.use:
        die("no conditions selected")
    if all(float(p.weights.get(d, DEFAULT_WEIGHTS[d])) == 0 for d in p.use):
        die("all selected conditions have weight 0")
    p.max_len = max(5.0, p.max_len)
    p.min_len = min(max(3.0, p.min_len), p.max_len)
    return p


def build_candidates(curves: dict, static: np.ndarray, p: Params, duration: float):
    w = {d: float(p.weights.get(d, DEFAULT_WEIGHTS[d])) for d in DETECTORS}
    events = []
    for d in p.use:
        for i in pick_events(curves[d], p.min_score, int(3 / BIN)):
            events.append((i * BIN, d, float(curves[d][i])))
    events.sort()

    clusters = []
    for ev in events:
        if clusters and ev[0] - clusters[-1][-1][0] <= p.merge_gap:
            clusters[-1].append(ev)
        else:
            clusters.append([ev])

    lo, hi = p.skip_start, max(p.skip_start + 1, duration - p.skip_end)
    cands = []
    for cl in clusters:
        best = max(cl, key=lambda e: w[e[1]] * e[2])
        start = min(e[0] for e in cl) - p.pre
        end = max(e[0] for e in cl) + p.post
        if end - start > p.max_len:
            start = best[0] - p.pre
            end = start + p.max_len
        if end - start < p.min_len:
            end = start + p.min_len
        if end > hi:
            start -= end - hi
            end = hi
        if start < lo:
            end = min(hi, end + (lo - start))
            start = lo
        if end - start < 3:
            continue
        a = int(start / BIN)
        b = max(a + 1, int(math.ceil(end / BIN)))
        mx = {d: float(curves[d][a:b].max()) for d in DETECTORS}
        tags = [d for d in DETECTORS if mx[d] >= p.tag_min]
        if any(r not in tags for r in p.require):
            continue
        num = sum(w[d] * mx[d] for d in p.use)
        den = sum(w[d] for d in p.use)
        single = max(w[d] * mx[d] for d in p.use) / max(w[d] for d in p.use)
        score = 0.6 * single + 0.4 * (num / den)
        static_frac = float(static[a:b].mean())
        flags = []
        if static_frac > 0.5:
            if not p.keep_static:
                continue
            flags.append("menu_like")
        cands.append({
            "start": round(start, 2), "end": round(end, 2), "duration": round(end - start, 2),
            "score": round(float(score), 3), "tags": tags,
            "events": [{"t": round(t, 2), "detector": d, "score": round(s, 3)}
                       for t, d, s in cl if start <= t <= end],
            "flags": flags, "static_frac": round(static_frac, 2),
        })

    cands.sort(key=lambda c: -c["score"])
    kept = []
    for c in cands:
        clash = any(
            min(c["end"], k["end"]) - max(c["start"], k["start"]) > 0.3 * min(c["duration"], k["duration"])
            for k in kept
        )
        if not clash:
            kept.append(c)
        if len(kept) >= p.top:
            break
    for i, c in enumerate(kept, 1):
        c["id"] = i
    return kept


# --------------------------------------------------------------------------- thumbnails + report

def grab_frame(video: str, t: float, width: int = 320) -> bytes:
    cmd = [tool("ffmpeg"), "-nostdin", "-v", "error", *FF_IN, "-ss", f"{max(0.0, t):.2f}", "-i", video, "-frames:v", "1",
           "-vf", f"scale={width}:-2", "-q:v", "5", "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
    res = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL)
    return res.stdout if res.returncode == 0 else b""


REPORT_TEMPLATE = """<!doctype html>
<html lang="ml"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; base-uri 'none'; form-action 'none'; img-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>clipfinder റിപ്പോർട്ട്</title>
<style>
:root{--bg:#f6f6f4;--fg:#1b1b1b;--card:#fff;--line:#d9d9d4;--mut:#666;--acc:#0b6bcb;--bar:#e3e3de}
@media (prefers-color-scheme:dark){:root{--bg:#161616;--fg:#eee;--card:#202020;--line:#383838;--mut:#a0a0a0;--acc:#6db3ff;--bar:#333}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,"Noto Sans Malayalam",sans-serif}
main{max-width:980px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:8px 0 2px}.sub{color:var(--mut);margin:0 0 12px;word-break:break-all}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}
.chip{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:999px;padding:6px 12px;cursor:pointer;font:inherit}
.chip[aria-pressed=true]{background:var(--acc);color:#fff;border-color:var(--acc)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;margin:12px 0}
.head{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.head label{display:flex;gap:8px;align-items:center;font-weight:600;cursor:pointer}
.time{color:var(--mut)}.bar{height:6px;background:var(--bar);border-radius:3px;margin:8px 0;max-width:260px}
.bar i{display:block;height:6px;background:var(--acc);border-radius:3px}
.tags{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0}
.tag{font-size:13px;border:1px solid var(--line);border-radius:6px;padding:1px 8px}
.warn{color:#b45309;font-size:13px}
.thumbs{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}.thumbs img{width:100%;border-radius:6px;display:block}
.cmd{position:sticky;bottom:0;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px;margin-top:16px}
.cmd code{display:block;white-space:pre-wrap;word-break:break-all;user-select:all;margin-top:6px}
.note{color:var(--mut);font-size:13px}
</style></head><body><main>
<h1>clipfinder റിപ്പോർട്ട്</h1>
<p class="sub">__VIDEO__ · __DURATION__ · വിശകലനം: __WHEN__ · റൺ ID: __RUN__</p>
<p class="note">ഇത് ശബ്ദവും ചലനവും നോക്കി ഉണ്ടാക്കിയ സാധ്യതാ ലിസ്റ്റാണ്, "നല്ല ക്ലിപ്പ്" എന്ന ഉറപ്പല്ല. ക്ലിപ്പുകൾ കണ്ട് നിങ്ങൾ തന്നെ തിരഞ്ഞെടുക്കുക.</p>
<div class="chips" id="chips"><span class="note" style="align-self:center">കണ്ടിഷൻ ഫിൽട്ടർ (എല്ലാം ഉള്ളവ കാണിക്കും):</span>__CHIPS__</div>
<div id="list">__CARDS__</div>
__EMPTY__
<div class="cmd"><strong>തിരഞ്ഞെടുത്ത ക്ലിപ്പുകൾ ഉണ്ടാക്കാനുള്ള കമാൻഡ്</strong>
<code id="cmd">ഒന്നും തിരഞ്ഞെടുത്തിട്ടില്ല</code>
<span class="note">ഈ കമാൻഡ് കോപ്പി ചെയ്ത് ടെർമിനലിൽ ഒട്ടിക്കുക (കമാൻഡിൽ ക്ലിക്ക് ചെയ്താൽ മുഴുവൻ സെലക്ട് ആകും).</span></div>
</main>
<script>
const OUT=__OUT_JSON__, RUN=__RUN_JSON__;
const cards=[...document.querySelectorAll('.card')], chips=[...document.querySelectorAll('.chip')];
function refresh(){
  const on=chips.filter(c=>c.getAttribute('aria-pressed')==='true').map(c=>c.dataset.tag);
  cards.forEach(c=>{const t=(c.dataset.tags||'').split(' ');c.style.display=on.every(x=>t.includes(x))?'':'none';});
  const ids=cards.filter(c=>c.querySelector('input').checked).map(c=>c.dataset.id);
  document.getElementById('cmd').textContent=OUT===null
    ? 'ഫോൾഡറിന്റെ പേരിൽ പ്രത്യേക ചിഹ്നങ്ങൾ (" $ ` % !) ഉണ്ട്, സുരക്ഷയ്ക്കായി കമാൻഡ് കാണിക്കുന്നില്ല. ഫോൾഡറിന്റെ പേര് മാറ്റി വീണ്ടും വിശകലനം ചെയ്യുക.'
    : ids.length
    ? 'python clipfinder.py render "'+OUT+'" --run '+RUN+' --ids '+ids.join(' ')
    : 'ഒന്നും തിരഞ്ഞെടുത്തിട്ടില്ല';
}
chips.forEach(c=>c.addEventListener('click',()=>{c.setAttribute('aria-pressed',c.getAttribute('aria-pressed')==='true'?'false':'true');refresh();}));
cards.forEach(c=>c.querySelector('input').addEventListener('change',refresh));
refresh();
</script></body></html>
"""


def build_html(data: dict, thumbs: dict, out_dir: str) -> str:
    e = html.escape
    chips = "".join(
        f'<button class="chip" type="button" aria-pressed="false" data-tag="{d}" title="{e(DETECTORS[d][1])}">{e(DETECTORS[d][0])}</button>'
        for d in DETECTORS
    )
    cards = []
    for c in data["candidates"]:
        imgs = "".join(f'<img alt="" src="data:image/jpeg;base64,{b}">' for b in thumbs.get(c["id"], []))
        tags = "".join(f'<span class="tag" title="{e(DETECTORS[t][1])}">{e(DETECTORS[t][0])}</span>' for t in c["tags"])
        warn = '<div class="warn">ഇതിൽ ഏറിയ പങ്കും നിശ്ചലമായ സ്ക്രീൻ ആണ് (മെനു ആയിരിക്കാം)</div>' if c["flags"] else ""
        cards.append(
            f'<div class="card" data-id="{c["id"]}" data-tags="{" ".join(c["tags"])}">'
            f'<div class="head"><label><input type="checkbox"> #{c["id"]}</label>'
            f'<span class="time">{fmt_time(c["start"])} – {fmt_time(c["end"])} · {c["duration"]:.0f} സെ</span></div>'
            f'<div class="bar"><i style="width:{int(c["score"] * 100)}%"></i></div>'
            f'<div class="tags">{tags}</div>{warn}<div class="thumbs">{imgs}</div></div>'
        )
    empty = "" if cards else '<p class="note">ഈ കണ്ടിഷനുകൾക്ക് ഒന്നും കിട്ടിയില്ല. --min-score കുറച്ചോ മറ്റൊരു preset ഉപയോഗിച്ചോ നോക്കുക.</p>'
    safe_json = lambda s: json.dumps(s).replace("<", "\\u003c")  # noqa: E731
    return (REPORT_TEMPLATE
            .replace("__VIDEO__", e(os.path.basename(data["video"])))
            .replace("__DURATION__", fmt_time(data["duration"]))
            .replace("__WHEN__", e(data["created"]))
            .replace("__RUN__", e(data["run_id"]))
            .replace("__CHIPS__", chips)
            .replace("__CARDS__", "".join(cards))
            .replace("__EMPTY__", empty)
            .replace("__OUT_JSON__", safe_json(os.path.abspath(out_dir)) if shell_safe(os.path.abspath(out_dir)) else "null")
            .replace("__RUN_JSON__", safe_json(data["run_id"])))


# --------------------------------------------------------------------------- analyze / render

def analyze(video: str, out: str | None, p: Params, reuse: bool = True, thumbs: bool = True,
            quiet: bool = False, progress=None, write_report: bool = True) -> dict:
    """progress(stage, fraction) is called as the work goes on: stage 'read' (slow, once per file),
    'detect', 'thumbs'."""
    emit = progress or (lambda stage, frac: None)
    need_tools()
    p = normalize_params(p)
    video = os.path.abspath(video)      # always absolute: a name starting with '-' or 'xyz:' cannot be misread
    out = out or (os.path.splitext(video)[0] + "_clipfinder")
    make_dir(out)
    info = probe(video)
    if info["duration"] < 10:
        die("the video is shorter than 10 seconds")
    sig = load_or_extract(video, info, out, reuse, quiet, progress)
    emit("detect", 0.0)
    curves, static, _ = compute_curves(sig, p)
    cands = build_candidates(curves, static, p, info["duration"])
    run_id = hashlib.sha1(f"{time.time()}{video}{p}".encode("utf-8", "replace"), usedforsecurity=False).hexdigest()[:8]
    data = {
        "version": __version__, "run_id": run_id, "video": os.path.abspath(video),
        "created": time.strftime("%Y-%m-%d %H:%M"), "duration": info["duration"],
        "width": info["width"], "height": info["height"], "has_audio": info["has_audio"],
        "params": {f.name: getattr(p, f.name) for f in fields(p)},
        "candidates": cands,
    }
    thumb_map = {}
    if thumbs:
        for i, c in enumerate(cands):
            if CANCEL.is_set():
                raise Cancelled()
            emit("thumbs", i / max(1, len(cands)))
            frames = [grab_frame(video, c["start"] + (c["end"] - c["start"]) * f) for f in (0.15, 0.5, 0.85)]
            thumb_map[c["id"]] = [base64.b64encode(b).decode("ascii") for b in frames if b]
    atomic_write(os.path.join(out, "candidates.json"), "w",
                 lambda fh: json.dump(data, fh, ensure_ascii=False, indent=2), encoding="utf-8")
    if write_report:
        atomic_write(os.path.join(out, "report.html"), "w",
                     lambda fh: fh.write(build_html(data, thumb_map, out)), encoding="utf-8")
    data["out"] = out
    return data



# --------------------------------------------------------------------------- evaluation

def parse_time(text: str) -> float:
    """'95', '1:35' or '1:02:03' (optionally with decimals) -> seconds."""
    parts = text.strip().split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError(text)
    total = 0.0
    for part in parts:
        v = float(part)
        if v < 0 or math.isnan(v) or math.isinf(v):
            raise ValueError(text)
        total = total * 60.0 + v
    return total


def read_labels(path: str):
    """One label per line: a time ('1:35') or a range ('1:35-1:50'). '#' starts a comment."""
    if not os.path.isfile(path):
        die(f"labels file not found: {path}")
    labels = []
    with open(path, encoding="utf-8-sig") as fh:
        for no, line in enumerate(fh, 1):
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            try:
                if "-" in line:
                    a, b = (parse_time(x) for x in line.split("-", 1))
                    if b < a:
                        raise ValueError(line)
                else:
                    a = b = parse_time(line)
            except ValueError:
                die(f"{path}, line {no}: cannot read '{line}'. Use 1:35 or 1:35-1:50 (one per line)")
            labels.append((a, b))
    if not labels:
        die(f"no labels found in {path}")
    return labels


def evaluate_candidates(data: dict, labels: list, tolerance: float = 3.0) -> dict:
    """Compare candidates with the moments you marked as good. A label is found when a candidate
    window (widened by `tolerance` seconds) overlaps it."""
    cands = data["candidates"]

    def overlaps(c, lab):
        return c["start"] - tolerance <= lab[1] and lab[0] <= c["end"] + tolerance

    found, missed = [], []
    for lab in labels:
        hits = [c for c in cands if overlaps(c, lab)]
        (found if hits else missed).append({"label": lab, "ids": [c["id"] for c in hits],
                                            "tags": sorted({t for c in hits for t in c["tags"]})})
    good = [c["id"] for c in cands if any(overlaps(c, lab) for lab in labels)]
    by_tag = {}
    for d in DETECTORS:
        tagged = [c for c in cands if d in c["tags"]]
        if tagged:
            by_tag[d] = {"candidates": len(tagged), "with_label": sum(1 for c in tagged if c["id"] in good)}
    top = {}
    for n in (5, 10):
        sub = cands[:n]
        if sub:
            top[n] = sum(1 for c in sub if c["id"] in good) / len(sub)
    return {
        "labels": len(labels), "found": len(found), "recall": len(found) / len(labels),
        "candidates": len(cands), "good_candidates": len(good),
        "precision": (len(good) / len(cands)) if cands else 0.0,
        "precision_at": top, "by_tag": by_tag, "missed": missed, "found_detail": found,
        "duration": data.get("duration"),
    }


def print_evaluation(res: dict, tolerance: float) -> None:
    print(f"Marked moments : {res['labels']}")
    print(f"Found          : {res['found']}  (recall {res['recall']:.0%})")
    print(f"Candidates     : {res['candidates']}  ({res['good_candidates']} overlap a marked moment, "
          f"precision {res['precision']:.0%})")
    for n, v in res["precision_at"].items():
        print(f"Top {n:<2d} hit rate : {v:.0%}")
    print("(precision is a rough guide only: a candidate you did not mark can still be a good clip)")
    if res["by_tag"]:
        print("\nBy condition (candidates carrying the tag / how many overlap a marked moment):")
        for d, v in res["by_tag"].items():
            print(f"  {d:<18s} {v['candidates']:>3d} / {v['with_label']}")
    if res["found_detail"]:
        print("\nFound:")
        for f in res["found_detail"]:
            print(f"  {fmt_time(f['label'][0])}  <- clip #{', #'.join(map(str, f['ids']))}  [{', '.join(f['tags'])}]")
    if res["missed"]:
        print("\nMissed (try --z-min lower, --min-score lower, or a different preset):")
        for m in res["missed"]:
            print(f"  {fmt_time(m['label'][0])}")
    print(f"\n(a candidate window counts if it comes within {tolerance:g}s of the marked moment)")


LAYOUTS = ("blur", "crop", "follow")
FOLLOW_FPS = 8               # frames per second looked at to find where the action is
FOLLOW_W, FOLLOW_H = 96, 54  # tiny grayscale frames are enough for that
FOLLOW_MAX_KEYS = 120        # at most this many turning points in the crop path (keeps the ffmpeg command short)


def wide_enough(width: int, height: int) -> bool:
    """Is the picture wider than 9:16, so that a crop has room to move sideways?"""
    return width > 0 and height > 0 and width / height > 9 / 16 + 0.01


def follow_centres(frames: np.ndarray) -> np.ndarray:
    """Where the action is in each frame: 0 = left edge .. 1 = right edge, NaN = nothing to go by.
    frames: (n, h, w) grayscale. Movement shared by the whole picture (camera pans, cuts, flashes) is taken out
    first, so what is left is something moving against the background."""
    n, _, w = frames.shape
    out = np.full(n, np.nan)
    xs = (np.arange(w) + 0.5) / w
    for i in range(1, n):
        col = np.abs(frames[i] - frames[i - 1]).mean(axis=0)
        extra = np.clip(col - np.median(col), 0.0, None)
        if extra.mean() < 0.25 or col.mean() > 40.0:      # too quiet, or a hard cut / flash
            continue
        peak = extra >= 0.5 * extra.max()                 # the strongest part only, not faint movement elsewhere
        out[i] = float((xs[peak] * extra[peak]).sum() / extra[peak].sum())
    return out


def follow_path(centres: np.ndarray, dur: float, view: float) -> list:
    """Turn per-frame centres into a calm crop path: [(seconds, position 0..1), ...].
    view: width of the crop as a share of the picture width. Gaps keep the last known place, the path is smoothed
    over about 2 s and never moves faster than a quarter of the picture per second."""
    n = len(centres)
    if n == 0 or dur <= 0 or not 0.0 < view < 1.0:
        return [(0.0, 0.5)]
    c = np.array(centres, float)
    known = np.flatnonzero(np.isfinite(c))
    if len(known) == 0:
        return [(0.0, 0.5)]
    c[: known[0]] = c[known[0]]
    for i in range(known[0] + 1, n):
        if not np.isfinite(c[i]):
            c[i] = c[i - 1]
    step = dur / n
    win = max(1, int(round(2.0 / step)))
    if win > 1:
        pad = np.concatenate([np.full(win, c[0]), c, np.full(win, c[-1])])
        kernel = np.ones(win) / win
        c = np.convolve(np.convolve(pad, kernel, mode="same"), kernel, mode="same")[win:-win]
    pos = np.clip((c - view / 2) / (1 - view), 0.0, 1.0)
    key_step = max(0.5, dur / FOLLOW_MAX_KEYS)
    times = np.arange(0.0, dur + key_step / 2, key_step)
    vals = np.interp(times, (np.arange(n) + 0.5) * step, pos)
    limit = 0.25 / (1 - view) * key_step                 # a quarter of the picture width per second
    for i in range(1, len(vals)):
        vals[i] = vals[i - 1] + float(np.clip(vals[i] - vals[i - 1], -limit, limit))
    return [(round(float(t), 3), round(float(v), 4)) for t, v in zip(times, vals)]


def find_follow_path(video: str, start: float, dur: float, width: int, height: int) -> list:
    """Look at one clip (small and gray) and work out the crop path that keeps the action in view."""
    cmd = [tool("ffmpeg"), "-nostdin", "-v", "error", *FF_IN, "-ss", f"{start:.2f}", "-i", video, "-t", f"{dur:.2f}",
           "-an", "-vf", f"fps={FOLLOW_FPS},scale={FOLLOW_W}:{FOLLOW_H}:flags=area,format=gray", "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    size, frames = FOLLOW_W * FOLLOW_H, []
    try:
        while True:
            if CANCEL.is_set():
                proc.kill()
                raise Cancelled()
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            frames.append(np.frombuffer(buf, dtype=np.uint8).astype(np.float32).reshape(FOLLOW_H, FOLLOW_W))
    finally:
        proc.stdout.close()
        proc.wait()
    if len(frames) < 2:
        return [(0.0, 0.5)]
    view = (height * 9 / 16) / width
    return follow_path(follow_centres(np.stack(frames)), dur, view)


def path_expression(path: list) -> str:
    """The crop path as an ffmpeg expression of t (seconds): straight lines between the turning points."""
    terms = [f"{path[0][1]:.4f}"]
    for (t0, v0), (t1, v1) in zip(path, path[1:]):
        if t1 > t0 and abs(v1 - v0) >= 0.0001:
            terms.append(f"{(v1 - v0) / (t1 - t0):+.5f}*clip(t-{t0:.3f},0,{t1 - t0:.3f})")
    return "clip(" + "".join(terms) + ",0,1)"


def build_filter(layout: str, crop_x: float, width: int, height: int, path: list | None = None) -> str:
    if layout == "follow" and path and wide_enough(width, height):
        return (f"[0:v]crop=w='trunc(ih*9/16/2)*2':h=ih:x='(iw-out_w)*{path_expression(path)}':y=0,"
                f"scale=1080:1920,format=yuv420p[v]")
    if layout == "crop" and wide_enough(width, height):
        return (f"[0:v]crop=w='trunc(ih*9/16/2)*2':h=ih:x='(iw-out_w)*{crop_x:.3f}':y=0,"
                f"scale=1080:1920,format=yuv420p[v]")
    return ("[0:v]split=2[bg][fg];"
            "[bg]scale=135:240:force_original_aspect_ratio=increase,crop=135:240,boxblur=4:1,scale=1080:1920[b];"
            "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[f];"
            "[b][f]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]")


def load_analysis(out: str) -> dict:
    """Read candidates.json and refuse anything that looks damaged or edited by hand."""
    path = os.path.join(out, "candidates.json")
    if not os.path.isfile(path):
        die(f"no candidates.json in {out} - run 'analyze' first")

    def number(v) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        ok = (isinstance(data["video"], str) and isinstance(data["run_id"], str)
              and all(number(data[k]) for k in ("duration", "width", "height"))
              and isinstance(data["candidates"], list)
              and all(isinstance(c["id"], int) and number(c["start"]) and number(c["end"])
                      for c in data["candidates"]))
    except (ValueError, KeyError, TypeError, OSError):
        ok = False
    if not ok:
        die(f"{path} is damaged or was edited by hand - run 'analyze' again")
    return data


PROGRESS_KEYS = ("frame=", "fps=", "stream_", "bitrate=", "total_size=", "out_time", "dup_frames=",
                 "drop_frames=", "speed=", "progress=")


def render_clip(video: str, start: float, dur: float, target: str, layout: str, crop_x: float,
                width: int, height: int, progress=None) -> None:
    """Cut one clip into a 1080x1920 file. progress(fraction) is called while ffmpeg works.
    layout 'follow' first looks at the clip to find where the action is, then crops along that path."""
    path = find_follow_path(video, start, dur, width, height) if layout == "follow" and wide_enough(width, height) else None
    cmd = [tool("ffmpeg"), "-nostdin", "-y", "-v", "error", "-progress", "pipe:1", "-nostats", *FF_IN,
           "-ss", f"{start:.2f}", "-i", video, "-t", f"{dur:.2f}",
           "-filter_complex", build_filter(layout, crop_x, width, height, path),
           "-map", "[v]", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", target]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace")
    errors = []
    try:
        for line in proc.stdout:
            if CANCEL.is_set():
                proc.kill()
                raise Cancelled()
            line = line.strip()
            if line.startswith("out_time_us=") and progress:
                try:
                    progress(min(0.99, int(line.split("=", 1)[1]) / 1e6 / dur))
                except ValueError:
                    pass
            elif line and not line.startswith(PROGRESS_KEYS):
                errors.append(line)
                errors = errors[-12:]
    finally:
        proc.stdout.close()
        rc = proc.wait()
    if rc != 0:
        if os.path.exists(target):
            os.remove(target)
        die("ffmpeg failed: " + " | ".join(errors)[:400])


def render_items(out: str, items: list, run: str | None = None, quiet: bool = False, progress=None,
                 fetch=None) -> list:
    """Render clips. items: dicts with id, start, end (seconds), layout ('blur'|'crop'), crop_x (0..1), title.
    progress(done_index, total, fraction_of_current). Returns [{'id', 'file', 'path'}].
    fetch(clip_id, start, duration, index, total), if given, may return (file, start_in_file, width, height) to cut
    from a different file than the analysed one (e.g. a full-quality section), or None to use the analysed video."""
    need_tools()
    data = load_analysis(out)
    video = data["video"]
    if not os.path.isabs(video) or not os.path.isfile(video):
        die(f"the source video is missing or its path is not valid: {video}")
    if run and run != data["run_id"]:
        die(f"this report is from run {run}, but the folder now holds run {data['run_id']}. "
            "Open the newest report.html and copy its command again.")
    by_id = {c["id"]: c for c in data["candidates"]}
    if not items:
        die("no clips were chosen")
    clean = []
    for it in items:
        try:
            cid, start, end = it["id"], float(it["start"]), float(it["end"])
            layout, crop_x, title = it.get("layout", "blur"), float(it.get("crop_x", 0.5)), str(it.get("title", ""))
        except (KeyError, TypeError, ValueError):
            die("a clip request is incomplete or has wrong values")
        if isinstance(cid, bool) or not isinstance(cid, int) or cid not in by_id:
            die(f"no such clip id: {cid!r}. Valid ids: {sorted(by_id)}")
        if not all(math.isfinite(x) for x in (start, end, crop_x)):
            die("clip times must be real numbers")
        start, end = max(0.0, start), min(float(data["duration"]), end)
        if not (1.0 <= end - start <= 180.0):
            die("a clip must be between 1 and 180 seconds long")
        if layout not in LAYOUTS:
            die("layout must be 'blur', 'crop' or 'follow'")
        if not 0.0 <= crop_x <= 1.0:
            die("crop position must be between 0 and 1")
        clean.append((cid, start, end - start, layout, crop_x, title[:120]))
    clips_dir = os.path.join(out, "clips")
    make_dir(clips_dir)
    made = []
    for n, (cid, start, dur, layout, crop_x, title) in enumerate(clean):
        label = safe_name(title) or fmt_time(by_id[cid]["start"]).replace(":", "-")
        fname = f"clip_{cid:02d}_{label}.mp4"
        target = os.path.join(clips_dir, fname)
        log(f"Rendering clip #{cid} ({fmt_time(start)} - {fmt_time(start + dur)}) ...", quiet)
        src, start_in, width, height = video, start, data["width"], data["height"]
        if fetch:
            got = fetch(cid, start, dur, n, len(clean))
            if got:
                src, start_in, width, height = got
        render_clip(src, start_in, dur, target, layout, crop_x, width, height,
                    (lambda fr, n=n: progress(n, len(clean), fr)) if progress else None)
        made.append({"id": cid, "file": fname, "path": target})
        if progress:
            progress(n + 1, len(clean), 0.0)
    return made


def render(out: str, ids: list, run: str | None = None, layout: str = "blur", crop_x: float = 0.5,
           pad_pre: float = 0.0, pad_post: float = 0.0, max_len: float = 180.0, quiet: bool = False) -> list:
    if not (0.0 <= crop_x <= 1.0):
        die("--crop-x must be between 0 and 1")
    if min(pad_pre, pad_post) < 0 or not math.isfinite(pad_pre + pad_post):
        die("--pad-pre and --pad-post cannot be negative")
    if not math.isfinite(max_len) or max_len <= 0:
        die("--max-len must be a positive number")
    data = load_analysis(out)
    by_id = {c["id"]: c for c in data["candidates"]}
    missing = [i for i in ids if i not in by_id]
    if missing:
        die(f"no such clip id(s): {missing}. Valid ids: {sorted(by_id)}")
    items = []
    for cid in ids:
        c = by_id[cid]
        start = max(0.0, c["start"] - pad_pre)
        end = min(data["duration"], c["end"] + pad_post)
        dur = max(1.0, min(end - start, max_len))
        items.append({"id": cid, "start": start, "end": start + dur, "layout": layout, "crop_x": crop_x})
    return [m["path"] for m in render_items(out, items, run, quiet)]


# --------------------------------------------------------------------------- CLI

def _csv(s: str | None):
    return [x.strip() for x in s.split(",") if x.strip()] if s else None


def apply_preset(p: Params, spec: str) -> None:
    if spec in PRESETS:
        cfg = PRESETS[spec]
    elif os.path.isfile(spec):
        try:
            with open(spec, encoding="utf-8-sig") as fh:
                cfg = json.load(fh)
        except (ValueError, OSError) as exc:
            die(f"cannot read preset {spec}: {exc}")
        if not isinstance(cfg, dict):
            die(f"preset {spec} must be a JSON object like {{\"require\": [\"slow_motion\"]}}")
    else:
        die(f"unknown preset '{spec}'. Built-in: {', '.join(PRESETS)}; or give the path of a .json file")
    known = {f.name for f in fields(Params)}
    for k, v in cfg.items():
        if k == "name":
            continue
        if k not in known:
            die(f"unknown setting '{k}' in preset {spec}")
        setattr(p, k, v)


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="clipfinder", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="find candidate moments and write report.html")
    a.add_argument("video", help="video file, or a https YouTube link (needs yt-dlp)")
    a.add_argument("--out", help="output folder (default: <video>_clipfinder)")
    a.add_argument("--preset", help="all | action | slowmo | clutch | path/to/preset.json")
    a.add_argument("--use", help="comma list of conditions that can create candidates")
    a.add_argument("--require", help="comma list of conditions every candidate must contain")
    a.add_argument("--weights", help="e.g. slow_motion=2,loud_peak=0.5")
    a.add_argument("--top", type=int)
    a.add_argument("--min-score", type=float, dest="min_score")
    a.add_argument("--z-min", type=float, dest="z_min", help="higher = stricter (default 2.0)")
    a.add_argument("--pre", type=float)
    a.add_argument("--post", type=float)
    a.add_argument("--min-len", type=float, dest="min_len")
    a.add_argument("--max-len", type=float, dest="max_len")
    a.add_argument("--merge-gap", type=float, dest="merge_gap")
    a.add_argument("--skip-start", type=float, dest="skip_start", help="ignore the first N seconds")
    a.add_argument("--skip-end", type=float, dest="skip_end", help="ignore the last N seconds")
    a.add_argument("--keep-static", action="store_true", dest="keep_static")
    a.add_argument("--no-slowmo-trigger", action="store_false", dest="slowmo_trigger", default=None)
    a.add_argument("--fresh", action="store_true", help="ignore cached signals and decode again")
    a.add_argument("--no-thumbs", action="store_true")
    a.add_argument("--quiet", action="store_true")

    r = sub.add_parser("render", help="cut chosen candidates into 1080x1920 clips")
    r.add_argument("out", help="the analysis folder")
    r.add_argument("--ids", type=int, nargs="+", required=True)
    r.add_argument("--run", help="run id printed in report.html (protects against stale ids)")
    r.add_argument("--layout", choices=list(LAYOUTS), default="blur")
    r.add_argument("--crop-x", type=float, default=0.5, dest="crop_x", help="0=left .. 1=right (crop layout)")
    r.add_argument("--pad-pre", type=float, default=0.0, dest="pad_pre")
    r.add_argument("--pad-post", type=float, default=0.0, dest="pad_post")
    r.add_argument("--max-len", type=float, default=180.0, dest="max_len")
    r.add_argument("--quiet", action="store_true")

    e = sub.add_parser("evaluate", help="check an analysis against moments you marked as good")
    e.add_argument("out", help="the analysis folder")
    e.add_argument("labels", help="text file: one time (1:35) or range (1:35-1:50) per line")
    e.add_argument("--tolerance", type=float, default=3.0, help="seconds of slack (default 3)")

    sub.add_parser("conditions", help="list the available conditions")

    args = ap.parse_args(argv)

    if args.cmd == "conditions":
        for name, (_, _, en) in DETECTORS.items():
            print(f"{name:18s} {en}")
        print("\npresets: " + ", ".join(PRESETS))
        return 0

    if args.cmd == "evaluate":
        data = load_analysis(args.out)
        if not (math.isfinite(args.tolerance) and args.tolerance >= 0):
            die("--tolerance must be a number that is not negative")
        labels = read_labels(args.labels)
        late = [l for l in labels if data.get("duration") and l[0] > data["duration"]]
        if late:
            die(f"{len(late)} marked time(s) are after the end of the video ({fmt_time(data['duration'])}); check the labels file")
        print_evaluation(evaluate_candidates(data, labels, args.tolerance), args.tolerance)
        return 0

    if args.cmd == "render":
        made = render(args.out, args.ids, args.run, args.layout, args.crop_x, args.pad_pre,
                      args.pad_post, args.max_len, args.quiet)
        for m in made:
            print(m)
        return 0

    p = Params()
    if args.preset:
        apply_preset(p, args.preset)
    for name in ("top", "min_score", "z_min", "pre", "post", "min_len", "max_len", "merge_gap",
                 "skip_start", "skip_end"):
        v = getattr(args, name)
        if v is not None:
            setattr(p, name, v)
    if args.keep_static:
        p.keep_static = True
    if args.slowmo_trigger is False:
        p.slowmo_trigger = False
    if args.use:
        p.use = _csv(args.use)
    if args.require:
        p.require = _csv(args.require)
    if args.weights:
        try:
            p.weights = {k: float(v) for k, v in (kv.split("=") for kv in _csv(args.weights))}
        except ValueError:
            die("--weights must look like slow_motion=2,loud_peak=0.5")

    video, out = args.video, args.out
    if re.match(r"https?://", video, re.I):
        out = out or "youtube_" + hashlib.sha1(video.encode(), usedforsecurity=False).hexdigest()[:8] + "_clipfinder"
        video = download_video(video, out, args.quiet)
    data = analyze(video, out, p, reuse=not args.fresh, thumbs=not args.no_thumbs, quiet=args.quiet)
    print(f"\nFound {len(data['candidates'])} candidates (run {data['run_id']}).")
    for c in data["candidates"]:
        print(f"  #{c['id']:<3d} {fmt_time(c['start'])} - {fmt_time(c['end'])}  score {c['score']:.2f}  "
              f"{', '.join(c['tags'])}")
    print(f"\nOpen this file in your browser:\n  {os.path.join(os.path.abspath(data['out']), 'report.html')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
