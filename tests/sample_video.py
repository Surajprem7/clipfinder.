"""Builds a synthetic test video with known ground truth (no real game footage needed).

Timeline (seconds)            picture                       sound
   0 -  30  ambient           drifting stripes (1.5 px/frame)  quiet noise
  30 -  32  BURST             random noise frames            very loud white noise
  32 -  40  SLOW-MO           stripes at 0.3 px/frame        low, muffled rumble
  40 -  50  ambient
  50 -  70  MUSIC             ambient picture                steady loud 440 Hz tone (sustained, not an event)
  70 -  80  calm              still picture                  near silence
  80 -  82  BURST (after calm)  random noise frames          very loud white noise
  82 -  90  ambient
  90 - 110  MENU              still picture                  very quiet
 110 - 120  ambient
"""
from __future__ import annotations

import os
import subprocess
import wave

import numpy as np

W, H, FPS, DUR, SR = 320, 180, 10, 120, 16000

SEGMENTS = [
    (0, 30, "amb"), (30, 32, "burst"), (32, 40, "slow"), (40, 50, "amb"), (50, 70, "amb"),
    (70, 80, "still"), (80, 82, "burst"), (82, 90, "amb"), (90, 110, "still"), (110, 120, "amb"),
]


def _audio(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = DUR * SR
    t = np.arange(n) / SR
    a = rng.uniform(-1, 1, n) * 0.017                       # ambient bed (~ -40 dB)

    def seg(s, e):
        return slice(int(s * SR), int(e * SR))

    a[seg(30, 32)] = rng.uniform(-1, 1, 2 * SR) * 0.5       # explosion
    noise = rng.uniform(-1, 1, 8 * SR)
    rumble = np.convolve(noise, np.ones(60) / 60, mode="same")
    a[seg(32, 40)] = rumble / rumble.std() * 0.06           # muffled slow-mo
    a[seg(50, 70)] += 0.3 * np.sin(2 * np.pi * 440 * t[seg(50, 70)]) + 0.1 * np.sin(2 * np.pi * 880 * t[seg(50, 70)])
    a[seg(70, 80)] = rng.uniform(-1, 1, 10 * SR) * 0.0001   # near silence
    a[seg(80, 82)] = rng.uniform(-1, 1, 2 * SR) * 0.5
    a[seg(90, 110)] = rng.uniform(-1, 1, 20 * SR) * 0.004   # menu hiss
    ts = t[seg(112, 115)]
    voice = sum(np.sin(2 * np.pi * 300 * k * ts) / k for k in range(1, 10))   # 300..2700 Hz harmonics
    a[seg(112, 115)] += 0.1 * voice
    return np.clip(a, -1, 1)


def make_sample(path: str, seed: int = 0, with_audio: bool = True) -> str:
    rng = np.random.default_rng(seed)
    x = np.arange(W)
    base = (110 + 80 * np.sin(2 * np.pi * x / 80.0)).astype(np.float32)
    grad = np.linspace(0.7, 1.0, H, dtype=np.float32)[:, None]
    wav_path = path + ".wav"
    if with_audio:
        pcm = (_audio(seed) * 32767).astype(np.int16)
        with wave.open(wav_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm.tobytes())
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(FPS), "-i", "-"]
    if with_audio:
        cmd += ["-i", wav_path]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "24", "-pix_fmt", "yuv420p"]
    if with_audio:
        cmd += ["-c:a", "aac", "-b:a", "96k", "-shortest"]
    cmd += [path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    pos = 0.0
    for f in range(DUR * FPS):
        t = f / FPS
        kind = next(k for s, e, k in SEGMENTS if s <= t < e)
        speed = {"amb": 1.5, "burst": 8.0, "slow": 0.3, "still": 0.0}[kind]
        pos += speed
        gray = np.roll(base, int(round(pos)))[None, :] * grad
        if kind == "burst":
            gray = gray + rng.uniform(-50, 50)          # flashes
        frame = np.repeat(np.clip(gray, 0, 255).astype(np.uint8)[:, :, None], 3, axis=2)
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg failed while building the sample video")
    if with_audio and os.path.exists(wav_path):
        os.remove(wav_path)
    return path


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "sample.mp4"
    make_sample(out)
    print(out)
