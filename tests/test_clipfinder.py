"""Tests run against a synthetic video with known ground truth (see sample_video.py).

Run:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import clipfinder as cf  # noqa: E402
from sample_video import make_sample  # noqa: E402


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def ffprobe(path):
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
        capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


def events_of(data, detector):
    return [(e["t"], e["score"]) for c in data["candidates"] for e in c["events"] if e["detector"] == detector]


class WithSample(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.video = make_sample(os.path.join(cls.tmp.name, "sample.mp4"))
        cls.out = os.path.join(cls.tmp.name, "out")
        cls.data = cf.analyze(cls.video, cls.out, cf.Params(), quiet=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class TestDetection(WithSample):
    def test_loud_burst_found(self):
        self.assertTrue(any(28 <= t <= 32 for t, _ in events_of(self.data, "loud_peak")))

    def test_motion_burst_found(self):
        self.assertTrue(any(28 <= t <= 32 for t, _ in events_of(self.data, "motion_burst")))

    def test_slow_motion_found_after_the_burst(self):
        slow = events_of(self.data, "slow_motion")
        self.assertTrue(any(31 <= t <= 36 for t, _ in slow), slow)

    def test_quiet_then_burst_found(self):
        self.assertTrue(any(79 <= t <= 83 for t, _ in events_of(self.data, "quiet_then_burst")))

    def test_first_burst_is_not_a_quiet_then_burst(self):
        self.assertFalse(any(26 <= t <= 33 for t, _ in events_of(self.data, "quiet_then_burst")))

    def test_sustained_music_only_counts_once(self):
        loud = [t for t, _ in events_of(self.data, "loud_peak") if 48 <= t <= 72]
        self.assertTrue(all(t < 54 for t in loud), loud)   # the onset, not the next 20 seconds

    def test_menu_is_not_reported(self):
        for d in cf.DETECTORS:
            self.assertFalse(any(88 <= t <= 110 for t, _ in events_of(self.data, d)), d)

    def test_best_candidate_combines_conditions(self):
        best = self.data["candidates"][0]
        self.assertTrue({"loud_peak", "motion_burst", "slow_motion"} <= set(best["tags"]), best["tags"])
        self.assertLess(best["start"], 30)
        self.assertGreater(best["end"], 33)

    def test_candidates_are_sorted_non_overlapping_and_within_limits(self):
        c = self.data["candidates"]
        self.assertEqual([x["id"] for x in c], list(range(1, len(c) + 1)))
        self.assertEqual([x["score"] for x in c], sorted((x["score"] for x in c), reverse=True))
        for x in c:
            self.assertGreaterEqual(x["start"], 0)
            self.assertLessEqual(x["end"], self.data["duration"] + 0.01)
            self.assertLessEqual(x["duration"], 59.01)
        for i, a in enumerate(c):
            for b in c[i + 1:]:
                overlap = min(a["end"], b["end"]) - max(a["start"], b["start"])
                self.assertLessEqual(overlap, 0.3 * min(a["duration"], b["duration"]) + 1e-6)


class TestVoice(WithSample):
    def test_shout_is_found(self):
        self.assertTrue(any(111 <= t <= 116 for t, _ in events_of(self.data, "voice_excitement")))

    def test_explosion_is_not_voice(self):
        self.assertFalse(any(27 <= t <= 36 for t, _ in events_of(self.data, "voice_excitement")))

    def test_no_voice_events_without_audio(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        v = make_sample(os.path.join(tmp.name, "mute.mp4"), with_audio=False)
        data = cf.analyze(v, os.path.join(tmp.name, "o"), cf.Params(), thumbs=False, quiet=True)
        self.assertEqual(events_of(data, "voice_excitement"), [])

    def test_require_voice(self):
        p = cf.Params(require=["voice_excitement"])
        data = cf.analyze(self.video, self.out, p, thumbs=False, quiet=True)
        self.assertTrue(data["candidates"])
        self.assertTrue(all("voice_excitement" in c["tags"] for c in data["candidates"]))


class TestEvaluate(WithSample):
    def _labels(self, text):
        path = os.path.join(self.tmp.name, "labels.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_time_formats(self):
        self.assertEqual(cf.parse_time("95"), 95)
        self.assertEqual(cf.parse_time("1:35"), 95)
        self.assertEqual(cf.parse_time("1:02:03"), 3723)
        for bad in ("", "a", "1:2:3:4", "-5", "nan", "1:xx"):
            with self.assertRaises(ValueError, msg=bad):
                cf.parse_time(bad)

    def test_read_labels_with_comments_and_ranges(self):
        labels = cf.read_labels(self._labels("# my clips\n0:30  # explosion\n\n1:20-1:22\n"))
        self.assertEqual(labels, [(30.0, 30.0), (80.0, 82.0)])

    def test_bad_labels_file_exits(self):
        for text in ("", "# only a comment\n", "banana\n", "2:00-1:00\n"):
            with self.assertRaises(SystemExit, msg=text):
                cf.read_labels(self._labels(text))
        with self.assertRaises(SystemExit):
            cf.read_labels(os.path.join(self.tmp.name, "missing.txt"))

    def test_evaluation_numbers(self):
        # 0:31 and 1:21 are real moments in the sample; 1:35-1:37 is the quiet gap between two candidates
        labels = [(31.0, 31.0), (81.0, 81.0), (95.0, 97.0)]
        res = cf.evaluate_candidates(self.data, labels, tolerance=1.0)
        self.assertEqual((res["labels"], res["found"]), (3, 2))
        self.assertAlmostEqual(res["recall"], 2 / 3)
        self.assertEqual([m["label"] for m in res["missed"]], [(95.0, 97.0)])
        self.assertLessEqual(res["good_candidates"], res["candidates"])
        self.assertIn("slow_motion", res["by_tag"])

    def test_tolerance(self):
        res0 = cf.evaluate_candidates(self.data, [(18.0, 18.0)], tolerance=0.0)
        res5 = cf.evaluate_candidates(self.data, [(18.0, 18.0)], tolerance=5.0)
        self.assertEqual(res0["found"], 0)
        self.assertEqual(res5["found"], 1)

    def test_cli(self):
        path = self._labels("0:31\n1:21\n")
        r = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "clipfinder.py"), "evaluate", self.out, path],
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("recall 100%", r.stdout)
        late = self._labels("5:00\n")
        r = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "clipfinder.py"), "evaluate", self.out, late],
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 2)
        self.assertIn("after the end", r.stderr)


class TestConditions(WithSample):
    def test_require_slow_motion(self):
        p = cf.Params()
        cf.apply_preset(p, "slowmo")
        data = cf.analyze(self.video, self.out, p, thumbs=False, quiet=True)
        self.assertTrue(data["candidates"])
        for c in data["candidates"]:
            self.assertIn("slow_motion", c["tags"])

    def test_action_preset_does_not_create_slowmo_events(self):
        p = cf.Params()
        cf.apply_preset(p, "action")
        data = cf.analyze(self.video, self.out, p, thumbs=False, quiet=True)
        self.assertEqual(events_of(data, "slow_motion"), [])
        self.assertEqual(events_of(data, "quiet_then_burst"), [])

    def test_require_clutch(self):
        p = cf.Params()
        cf.apply_preset(p, "clutch")
        data = cf.analyze(self.video, self.out, p, thumbs=False, quiet=True)
        self.assertTrue(data["candidates"])
        self.assertTrue(all("quiet_then_burst" in c["tags"] for c in data["candidates"]))
        self.assertTrue(any(70 <= c["start"] + 10 <= 90 for c in data["candidates"]))

    def test_stricter_threshold_returns_fewer(self):
        loose = cf.analyze(self.video, self.out, cf.Params(min_score=0.2), thumbs=False, quiet=True)
        strict = cf.analyze(self.video, self.out, cf.Params(min_score=0.95), thumbs=False, quiet=True)
        self.assertLessEqual(len(strict["candidates"]), len(loose["candidates"]))

    def test_top_limits_count(self):
        data = cf.analyze(self.video, self.out, cf.Params(top=1), thumbs=False, quiet=True)
        self.assertEqual(len(data["candidates"]), 1)

    def test_skip_start_and_end(self):
        data = cf.analyze(self.video, self.out, cf.Params(skip_start=60, skip_end=10), thumbs=False, quiet=True)
        for c in data["candidates"]:
            self.assertGreaterEqual(c["start"], 60)
            self.assertLessEqual(c["end"], 110.01)

    def test_cached_signals_are_reused(self):
        with mock.patch.object(cf, "extract_signals", side_effect=AssertionError("decoded again")):
            cf.analyze(self.video, self.out, cf.Params(top=3), thumbs=False, quiet=True)

    def test_changed_file_invalidates_cache(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        v = make_sample(os.path.join(tmp.name, "a.mp4"), seed=1)
        out = os.path.join(tmp.name, "o")
        cf.analyze(v, out, cf.Params(), thumbs=False, quiet=True)
        make_sample(v, seed=2)  # overwrite with a different file
        with mock.patch.object(cf, "extract_signals", wraps=cf.extract_signals) as spy:
            cf.analyze(v, out, cf.Params(), thumbs=False, quiet=True)
            self.assertEqual(spy.call_count, 1)

    def test_custom_preset_file(self):
        path = os.path.join(self.tmp.name, "mine.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"name": "mine", "use": ["loud_peak"], "weights": {"loud_peak": 2}, "top": 2}, fh)
        p = cf.Params()
        cf.apply_preset(p, path)
        self.assertEqual((p.use, p.top, p.weights), (["loud_peak"], 2, {"loud_peak": 2}))

    def test_bad_preset_and_condition_names_exit(self):
        with self.assertRaises(SystemExit):
            cf.apply_preset(cf.Params(), "does-not-exist")
        bad = os.path.join(self.tmp.name, "bad.json")
        with open(bad, "w") as fh:
            json.dump({"nonsense": 1}, fh)
        with self.assertRaises(SystemExit):
            cf.apply_preset(cf.Params(), bad)
        with self.assertRaises(SystemExit):
            cf.normalize_params(cf.Params(use=["loud_peak", "teleport"]))


class TestRender(WithSample):
    def _check(self, path, expect_audio=True):
        info = ffprobe(path)
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        self.assertEqual((v["width"], v["height"]), (1080, 1920))
        self.assertEqual(v["pix_fmt"], "yuv420p")
        has_a = any(s["codec_type"] == "audio" for s in info["streams"])
        self.assertEqual(has_a, expect_audio)
        return float(info["format"]["duration"])

    def test_render_blur(self):
        data = cf.analyze(self.video, self.out, cf.Params(), thumbs=False, quiet=True)
        made = cf.render(self.out, [1], run=data["run_id"], quiet=True)
        self.assertEqual(len(made), 1)
        dur = self._check(made[0])
        self.assertAlmostEqual(dur, data["candidates"][0]["duration"], delta=1.0)

    def test_render_crop(self):
        data = cf.analyze(self.video, self.out, cf.Params(), thumbs=False, quiet=True)
        made = cf.render(self.out, [1, 2], run=data["run_id"], layout="crop", crop_x=0.3, quiet=True)
        self.assertEqual(len(made), 2)
        for m in made:
            self._check(m)

    def test_stale_report_is_rejected(self):
        cf.analyze(self.video, self.out, cf.Params(), thumbs=False, quiet=True)
        with self.assertRaises(SystemExit):
            cf.render(self.out, [1], run="deadbeef", quiet=True)

    def test_unknown_id_is_rejected(self):
        data = cf.analyze(self.video, self.out, cf.Params(), thumbs=False, quiet=True)
        with self.assertRaises(SystemExit):
            cf.render(self.out, [999], run=data["run_id"], quiet=True)

    def test_padding_extends_clip(self):
        data = cf.analyze(self.video, self.out, cf.Params(), thumbs=False, quiet=True)
        c = data["candidates"][0]
        made = cf.render(self.out, [1], run=data["run_id"], pad_pre=2, pad_post=3, quiet=True)
        dur = self._check(made[0])
        self.assertAlmostEqual(dur, c["duration"] + 2 + 3, delta=1.0)


class TestFlatScreen(unittest.TestCase):
    """A loading / end-card screen (smooth gradient, no picture detail) must not become a clip."""

    def signals(self, flat_from, flat_to):
        n = 240
        rng = np.random.default_rng(3)
        L = -40 + rng.normal(0, 0.5, n)
        M = 3 + rng.normal(0, 0.2, n)
        D = np.full(n, 12.0)
        L[flat_from:flat_to], M[flat_from:flat_to], D[flat_from:flat_to] = -22, 9, 0.8   # loud + moving + flat
        return {"L": L, "C": np.full(n, 1500.0), "V": np.full(n, -50.0), "R": np.full(n, -6.0),
                "M": M, "D": D, "has_audio": np.array(True)}

    def candidates(self, sig, **kw):
        p = cf.normalize_params(cf.Params(**kw))
        curves, static, _ = cf.compute_curves(sig, p)
        return cf.build_candidates(curves, static, p, 120.0)

    def test_flat_screen_is_dropped(self):
        self.assertEqual(self.candidates(self.signals(100, 140)), [])

    def test_flat_screen_kept_when_asked(self):
        c = self.candidates(self.signals(100, 140), keep_static=True)
        self.assertTrue(c)
        self.assertIn("menu_like", c[0]["flags"])

    def test_detailed_picture_is_kept(self):
        sig = self.signals(100, 140)
        sig["D"][:] = 12.0
        self.assertTrue(self.candidates(sig))

    def test_bad_flat_setting_refused(self):
        with self.assertRaises(SystemExit):
            cf.normalize_params(cf.Params(flat_thr=-1))


class TestSceneRules(unittest.TestCase):
    """v1.6: menu/puzzle screens, scene changes, black breaks inside a clip, still edges, and filling a short list."""

    def sig(self, n=1200, seed=4):
        rng = np.random.default_rng(seed)
        return {"L": -30 + rng.normal(0, 0.8, n), "C": np.full(n, 1500.0), "V": np.full(n, -50.0),
                "R": np.full(n, -6.0), "M": 8 + rng.normal(0, 0.5, n), "D": np.full(n, 12.0),
                "S": np.full(n, 33.0), "has_audio": np.array(True)}

    def cands(self, sig, **kw):
        p = cf.normalize_params(cf.Params(**kw))
        curves, static, dbg = cf.compute_curves(sig, p)
        return cf.build_candidates(curves, static, p, len(sig["M"]) * cf.BIN, dbg.get("flat"))

    def test_puzzle_screen_with_moving_pieces_is_not_a_clip(self):
        s = self.sig()
        s["M"][:400] = 0.3 + np.random.default_rng(1).normal(0, 0.05, 400)   # quiet screen ...
        s["S"][:400] = 3.0                                                   # ... that hardly changes in 10 s
        s["M"][200:206] = 6.0                                                # a piece moves: big jump for it
        self.assertFalse([c for c in self.cands(s) if c["start"] < 200 * cf.BIN])
        s["S"][:400] = 33.0                                                  # same jump in real play is kept
        self.assertTrue([c for c in self.cands(s) if 90 <= c["start"] <= 103])

    def test_scene_change_after_a_menu_is_not_action(self):
        s = self.sig()
        s["M"][:400], s["S"][:400] = 0.1, 2.0                                # menu ...
        s["M"][400:404] = 40.0                                               # ... then the game comes back
        self.assertFalse([c for c in self.cands(s) if c["start"] < 215 <= c["end"]])

    def test_black_break_cuts_the_clip(self):
        s = self.sig()
        s["M"][600:604] = 25.0                                               # action at 300 s
        s["D"][612:620] = 0.5                                                # black from 306 to 310 s
        c = [c for c in self.cands(s) if c["start"] <= 300 <= c["end"]]
        self.assertTrue(c)
        self.assertLessEqual(c[0]["end"], 306.5)                             # (+ one bin of smoothing)
        self.assertGreaterEqual(c[0]["duration"], 15.0)                      # grown on the side away from black

    def test_cut_at_breaks_helper(self):
        br = np.zeros(200, bool); br[100:106] = True
        self.assertEqual(cf.cut_at_breaks(40.0, 60.0, 45.0, br, 0, 100, 15), (35.0, 50.0))
        self.assertIsNone(cf.cut_at_breaks(40.0, 60.0, 51.0, br, 0, 100, 15))
        self.assertEqual(cf.cut_at_breaks(40.0, 60.0, 45.0, np.zeros(200, bool), 0, 100, 15), (40.0, 60.0))

    def test_still_start_is_trimmed(self):
        st = np.zeros(200, bool); st[40:60] = True                          # still from 20 to 30 s
        self.assertEqual(cf.trim_still_edges(22.0, 42.0, 31.0, st, 0, 100, 15), (30.0, 45.0))
        self.assertEqual(cf.trim_still_edges(22.0, 42.0, 25.0, st, 0, 100, 15), (22.0, 42.0))  # moment is still

    def test_busy_stream_still_gives_a_choice(self):
        s = self.sig(n=4800)
        rng = np.random.default_rng(9)
        for k in range(10):                                                  # ten small bumps in busy play
            i = 300 + k * 420
            s["M"][i:i + 3] += 3.5 + rng.normal(0, 0.2)
        few = cf.find_candidates(s, cf.normalize_params(cf.Params()), 2400.0)
        more = cf.find_candidates(s, cf.normalize_params(cf.Params(min_count=8)), 2400.0)
        self.assertGreater(len(more), len(few))
        self.assertLessEqual(len(more), 8)
        self.assertEqual([c["id"] for c in more], list(range(1, len(more) + 1)))
        with self.assertRaises(SystemExit):
            cf.normalize_params(cf.Params(min_count=-1))

    def test_old_analyses_are_recognised(self):
        for v, old in (("1.5.0", True), ("1.6.0", False), ("2.0", False), ("x", True), (None, True), ("0.9.1", True)):
            self.assertEqual(cf.detection_is_old(v), old, v)

    def test_old_signal_files_without_S_still_work(self):
        s = self.sig()
        del s["S"]
        cf.compute_curves(s, cf.normalize_params(cf.Params()))


class TestFollowCrop(unittest.TestCase):
    """Layout 'follow': the crop moves sideways to keep the action in the picture."""

    def frames(self, xs, w=96, h=54):
        out = np.full((len(xs), h, w), 30.0, dtype=np.float32)
        for i, x in enumerate(xs):
            if x is not None:
                out[i, 20:30, int(x * (w - 8)):int(x * (w - 8)) + 8] = 250.0
        return out

    def test_centres_follow_a_moving_thing(self):
        xs = list(np.linspace(0.1, 0.9, 40))
        c = cf.follow_centres(self.frames(xs))
        self.assertTrue(np.isnan(c[0]))
        got = c[1:]
        self.assertTrue(np.all(np.isfinite(got)))
        pulled = 0.5 + (np.array(xs[1:]) - 0.5) * (1 - cf.FOLLOW_CENTRE_PULL)   # trusted half way, towards the middle
        self.assertLess(np.abs(got - pulled).max(), 0.12)
        self.assertTrue(np.all(np.diff(got[::5]) > 0))                          # still moves the right way

    def test_zoom_view_is_a_square_and_follow_is_9_16(self):
        self.assertAlmostEqual(cf.view_share("zoom", 1920, 1080), 0.5625)
        self.assertAlmostEqual(cf.view_share("follow", 1920, 1080), 0.31640625)
        self.assertEqual(cf.view_share("zoom", 1080, 1920), 1.0)

    def test_zoom_filter_puts_a_square_on_a_blurred_background(self):
        f = cf.build_filter("zoom", 0.5, 1920, 1080, [(0.0, 0.2), (5.0, 0.8)])
        self.assertIn("crop=w='trunc(ih/2)*2'", f)
        self.assertIn("scale=1080:1080", f)
        self.assertIn("boxblur", f)
        self.assertIn("overlay=0:(H-h)/2", f)
        # a portrait source has no room to move: it falls back to the blurred layout
        self.assertNotIn("1080:1080", cf.build_filter("zoom", 0.5, 1080, 1920, [(0.0, 0.5)]))

    def test_still_picture_and_whole_screen_change_give_no_centre(self):
        still = cf.follow_centres(self.frames([0.5] * 10))
        self.assertTrue(np.all(np.isnan(still)))
        flash = np.stack([np.full((54, 96), v, dtype=np.float32) for v in (10, 240, 10, 240)])
        self.assertTrue(np.all(np.isnan(cf.follow_centres(flash))))

    def test_path_is_calm_and_stays_inside(self):
        rng = np.random.default_rng(1)
        centres = np.clip(0.5 + rng.normal(0, 0.3, 160), 0, 1)            # jumpy input, 20 s at 8 fps
        centres[40:60] = np.nan
        view = 0.316
        path = cf.follow_path(centres, 20.0, view)
        t = np.array([p[0] for p in path]); v = np.array([p[1] for p in path])
        self.assertEqual(t[0], 0.0)
        self.assertTrue(np.all((v >= 0) & (v <= 1)))
        self.assertTrue(np.all(np.diff(t) > 0))
        speed = np.abs(np.diff(v)) / np.diff(t) * (1 - view)             # share of the picture width per second
        self.assertLessEqual(speed.max(), 0.25 + 1e-3)

    def test_path_without_information_stays_in_the_middle(self):
        self.assertEqual(cf.follow_path(np.full(50, np.nan), 6.0, 0.3), [(0.0, 0.5)])
        self.assertEqual(cf.follow_path(np.zeros(0), 6.0, 0.3), [(0.0, 0.5)])

    def test_long_clip_keeps_the_command_short(self):
        path = cf.follow_path(np.linspace(0, 1, 180 * 8), 180.0, 0.316)
        self.assertLessEqual(len(path), cf.FOLLOW_MAX_KEYS + 2)
        self.assertLess(len(cf.path_expression(path)), 8000)

    def test_expression_matches_the_path(self):
        path = [(0.0, 0.2), (1.0, 0.6), (2.0, 0.6), (3.0, 0.1)]
        expr = cf.path_expression(path)
        self.assertRegex(expr, r"^clip\([0-9.+\-*t,clip()]+,0,1\)$")           # nothing but numbers, t and clip()
        def value(t):
            clip = lambda x, lo, hi: max(lo, min(hi, x))
            return eval(expr, {"__builtins__": {}}, {"clip": clip, "t": t})
        for t, want in ((0, 0.2), (0.5, 0.4), (1, 0.6), (1.5, 0.6), (2.5, 0.35), (3, 0.1), (9, 0.1)):
            self.assertAlmostEqual(value(t), want, places=3)

    def test_filter_falls_back_when_there_is_no_room_or_no_path(self):
        self.assertIn("boxblur", cf.build_filter("follow", 0.5, 1080, 1920, [(0.0, 0.5)]))   # already upright
        self.assertIn("boxblur", cf.build_filter("follow", 0.5, 1920, 1080, None))
        self.assertIn("clip(", cf.build_filter("follow", 0.5, 1920, 1080, [(0.0, 0.5), (1.0, 0.7)]))

    def test_rendered_clip_keeps_a_moving_thing_in_view(self):
        tmp = tempfile.mkdtemp()
        try:
            src, out = os.path.join(tmp, "src.mp4"), os.path.join(tmp, "out.mp4")
            subprocess.run([cf.tool("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x202830:s=640x360:r=30:d=12",
                            "-f", "lavfi", "-i", "color=c=white:s=40x40:r=30:d=12",
                            "-filter_complex", "[0][1]overlay=x='20+t/12*560':y=160[v]", "-map", "[v]",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", src], check=True)
            def box_positions(layout, rows=slice(0, 96)):
                cf.render_clip(src, 0.0, 12.0, out, layout, 0.5, 640, 360)
                self.assertEqual((ffprobe(out)["streams"][0]["width"], ffprobe(out)["streams"][0]["height"]), (1080, 1920))
                raw = subprocess.run([cf.tool("ffmpeg"), "-v", "error", "-i", out, "-vf", "fps=1,scale=54:96,format=gray",
                                      "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
                frames = np.frombuffer(raw, np.uint8).reshape(-1, 96, 54)
                return [bool((f[rows].max(axis=0) > 200).any()) for f in frames[1:11]]
            self.assertTrue(all(box_positions("follow")[1:9]))     # always in the picture
            self.assertTrue(all(box_positions("zoom", slice(22, 74))))   # the sharp square keeps it, edges too
            self.assertFalse(all(box_positions("crop")))           # a fixed crop loses it
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestEdgeCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_video_without_audio(self):
        v = make_sample(os.path.join(self.tmp.name, "mute.mp4"), with_audio=False)
        data = cf.analyze(v, os.path.join(self.tmp.name, "o"), cf.Params(), thumbs=False, quiet=True)
        self.assertFalse(data["has_audio"])
        self.assertTrue(any(28 <= t <= 32 for t, _ in events_of(data, "motion_burst")))
        self.assertEqual(events_of(data, "loud_peak"), [])
        out = cf.render(os.path.join(self.tmp.name, "o"), [1], run=data["run_id"], quiet=True)
        info = ffprobe(out[0])
        self.assertFalse(any(s["codec_type"] == "audio" for s in info["streams"]))

    def test_missing_file_exits(self):
        with self.assertRaises(SystemExit):
            cf.analyze(os.path.join(self.tmp.name, "nope.mp4"), None, cf.Params(), quiet=True)

    def test_not_a_video_exits(self):
        bad = os.path.join(self.tmp.name, "x.mp4")
        with open(bad, "wb") as fh:
            fh.write(b"this is not a video")
        with self.assertRaises(SystemExit):
            cf.analyze(bad, os.path.join(self.tmp.name, "o"), cf.Params(), quiet=True)

    def test_very_short_video_exits_cleanly(self):
        short = os.path.join(self.tmp.name, "s.mp4")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10:duration=4",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", short], check=True)
        with self.assertRaises(SystemExit):
            cf.analyze(short, os.path.join(self.tmp.name, "o"), cf.Params(), quiet=True)

    def test_path_with_spaces_and_unicode(self):
        folder = os.path.join(self.tmp.name, "എന്റെ ഗെയിം ക്ലിപ്പ്")
        os.makedirs(folder)
        v = make_sample(os.path.join(folder, "stream 1.mp4"))
        data = cf.analyze(v, None, cf.Params(top=2), thumbs=False, quiet=True)
        self.assertTrue(os.path.isfile(os.path.join(data["out"], "report.html")))
        made = cf.render(data["out"], [1], run=data["run_id"], quiet=True)
        self.assertTrue(os.path.isfile(made[0]))

    def test_quiet_footage_gives_few_or_no_candidates(self):
        v = os.path.join(self.tmp.name, "calm.mp4")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:size=160x90:rate=10:duration=60",
                        "-f", "lavfi", "-i", "anoisesrc=a=0.01:d=60:r=16000", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-shortest", v], check=True)
        data = cf.analyze(v, os.path.join(self.tmp.name, "o"), cf.Params(), thumbs=False, quiet=True)
        self.assertEqual(data["candidates"], [])


class TestReport(WithSample):
    def test_report_is_self_contained_and_escaped(self):
        data = json.loads(json.dumps(self.data))
        data["video"] = "/x/<img src=x onerror=alert(1)>.mp4"
        data["run_id"] = "ab12cd34"
        page = cf.build_html(data, {1: ["AAAA"]}, '/tmp/out"</script><b>')
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;.mp4", page)
        self.assertNotIn('"</script><b>', page)              # JSON string cannot close the script tag
        self.assertIn("Content-Security-Policy", page)
        self.assertIn("default-src 'none'", page)
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        self.assertNotIn("<link", page)
        self.assertNotIn("src=\"http", page)

    def test_report_lists_every_candidate_with_thumbnails(self):
        page = _read(os.path.join(self.out, "report.html"))
        self.assertEqual(page.count('class="card"'), len(self.data["candidates"]))
        self.assertIn("data:image/jpeg;base64,", page)

    def test_empty_report(self):
        data = json.loads(json.dumps(self.data))
        data["candidates"] = []
        page = cf.build_html(data, {}, "/tmp/o")
        self.assertIn("ഒന്നും കിട്ടിയില്ല", page)


class TestLink(unittest.TestCase):
    """The real download can't be tested offline; these check validation and the plumbing with a fake yt-dlp."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _fake_ytdlp(self, sample, fail=False):
        bindir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bindir, exist_ok=True)
        script = os.path.join(bindir, "yt-dlp")
        with open(script, "w") as fh:
            fh.write("#!%s\nimport shutil, sys\n" % sys.executable)
            if fail:
                fh.write("sys.stderr.write('ERROR: Video unavailable\\n'); sys.exit(1)\n")
            else:
                fh.write("a = sys.argv\nassert '--' in a and a[a.index('--') + 1].startswith('https://')\n"
                         "o = a[a.index('-o') + 1].replace('%%(ext)s', 'mp4')\nshutil.copy(%r, o)\n" % sample)
        os.chmod(script, 0o755)
        return mock.patch.dict(os.environ, {"PATH": bindir + os.pathsep + os.environ["PATH"]})

    def test_url_validation(self):
        for ok in ("https://www.youtube.com/watch?v=abc", "https://youtu.be/abc", "https://m.youtube.com/live/abc"):
            self.assertEqual(cf.parse_youtube_url(ok), ok)
        for bad in ("http://youtube.com/watch?v=a", "https://evil.com/watch?v=a", "https://youtube.com.evil.com/x",
                    "https://evil.com/?u=https://youtube.com/", "file:///etc/passwd", "--exec=calc https://youtu.be/a",
                    "https://youtube.com/a b"):
            with self.assertRaises(SystemExit, msg=bad):
                cf.parse_youtube_url(bad)

    def test_missing_ytdlp_gives_clear_error(self):
        with mock.patch.dict(os.environ, {"PATH": os.path.dirname(sys.executable) if False else "/nonexistent"}):
            with self.assertRaises(SystemExit):
                cf.download_video("https://youtu.be/abc", self.tmp.name, quiet=True)

    def test_link_to_report_with_fake_downloader(self):
        sample = make_sample(os.path.join(self.tmp.name, "orig.mp4"))
        out = os.path.join(self.tmp.name, "res")
        with self._fake_ytdlp(sample):
            r = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "clipfinder.py"), "analyze",
                                "https://youtu.be/abc123", "--out", out, "--quiet", "--no-thumbs"],
                               capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(out, "source.mp4")))
        data = json.loads(_read(os.path.join(out, "candidates.json")))
        self.assertTrue(data["candidates"])
        made = cf.render(out, [1], run=data["run_id"], quiet=True)
        self.assertTrue(os.path.isfile(made[0]))

    def test_ytdlp_time_format(self):
        self.assertEqual(cf.ytdlp_time(0), "0:00:00.00")
        self.assertEqual(cf.ytdlp_time(83.5), "0:01:23.50")
        self.assertEqual(cf.ytdlp_time(3725.25), "1:02:05.25")
        self.assertEqual(cf.ytdlp_time(-5), "0:00:00.00")

    def test_section_download_refuses_bad_ranges(self):
        sample = make_sample(os.path.join(self.tmp.name, "orig.mp4"))
        with self._fake_ytdlp(sample):
            for a, b in ((5, 5), (10, 5), (float("nan"), 5), (-1, 5), (0, 1e9), (0, float("inf"))):
                with self.assertRaises(SystemExit, msg=(a, b)):
                    cf.download_section("https://youtu.be/abc", a, b, self.tmp.name, "sec", quiet=True)
            for stem in ("../x", "a b", "a%b", ""):
                with self.assertRaises(SystemExit, msg=stem):
                    cf.download_section("https://youtu.be/abc", 1, 9, self.tmp.name, stem, quiet=True)
            with self.assertRaises(SystemExit):
                cf.download_section("https://evil.example/x", 1, 9, self.tmp.name, "sec", quiet=True)

    def test_download_failure_is_reported(self):
        with self._fake_ytdlp("x", fail=True):
            with self.assertRaises(SystemExit):
                cf.download_video("https://youtu.be/abc", os.path.join(self.tmp.name, "o"), quiet=True)


class TestFinishedDownloads(unittest.TestCase):
    """Pieces left behind by an interrupted yt-dlp must never be mistaken for a finished video."""

    def test_only_the_exact_finished_file_counts(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("lowres.f137.mp4", "lowres.f140.m4a", "lowres.mp4.part", "lowres.mp4.ytdl", "lowres.mp4.tmp",
                         "lowres.txt", "lowres2.mp4", "xlowres.mp4"):
                with open(os.path.join(d, name), "wb") as fh:
                    fh.write(b"x" * 10)
            self.assertIsNone(cf._find_download(d, "lowres"))              # nothing finished yet
            with open(os.path.join(d, "lowres.mp4"), "wb") as fh:
                fh.write(b"y" * 10)
            self.assertEqual(os.path.basename(cf._find_download(d, "lowres")), "lowres.mp4")
            os.remove(os.path.join(d, "lowres.mp4"))
            with open(os.path.join(d, "lowres.webm"), "wb") as fh:          # a single-file format keeps its own extension
                fh.write(b"y" * 10)
            self.assertEqual(os.path.basename(cf._find_download(d, "lowres")), "lowres.webm")

    def test_empty_files_and_folders_do_not_count(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "source.mp4"), "wb").close()
            os.mkdir(os.path.join(d, "source.mkv"))
            self.assertIsNone(cf._find_download(d, "source"))

    def test_section_names_do_not_get_mixed_up(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "sec_100_2000.mp4"), "wb") as fh:
                fh.write(b"z" * 10)
            self.assertIsNone(cf._find_download(d, "sec_100_200"))
            self.assertIsNotNone(cf._find_download(d, "sec_100_2000"))


class TestRenderSource(WithSample):
    def test_render_can_cut_from_another_file(self):
        items = [{"id": 1, "start": 30.0, "end": 38.0, "layout": "blur", "crop_x": 0.5, "title": "a"},
                 {"id": 2, "start": 80.0, "end": 88.0, "layout": "crop", "crop_x": 0.3, "title": "b"}]
        seen = []

        def fetch(cid, start, dur, n, total):
            seen.append((cid, start, dur, n, total))
            return (self.video, start, self.data["width"], self.data["height"]) if cid == 1 else None

        made = cf.render_items(self.out, items, quiet=True, fetch=fetch)
        self.assertEqual(seen, [(1, 30.0, 8.0, 0, 2), (2, 80.0, 8.0, 1, 2)])
        for m in made:
            v = next(s for s in ffprobe(m["path"])["streams"] if s["codec_type"] == "video")
            self.assertEqual((v["width"], v["height"]), (1080, 1920))

    def test_bad_items_are_refused(self):
        good = {"id": 1, "start": 30.0, "end": 38.0}
        for bad in ({"id": 99}, {"id": True}, {"end": 30.5}, {"start": float("nan")},
                    {"layout": "square"}, {"crop_x": 2}, {"start": "x"}):
            with self.assertRaises(SystemExit, msg=bad):
                cf.render_items(self.out, [{**good, **bad}], quiet=True)
        with self.assertRaises(SystemExit):
            cf.render_items(self.out, [], quiet=True)


class TestAudit(unittest.TestCase):
    """Security and robustness checks added after the code audit."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _sample(self, name="a.mp4"):
        return make_sample(os.path.join(self.tmp.name, name))

    def test_file_name_starting_with_dash(self):
        self._sample("-weird.mp4")
        old = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, old)
        data = cf.analyze("-weird.mp4", "out", cf.Params(top=2), thumbs=False, quiet=True)
        self.assertTrue(data["candidates"])

    def test_file_name_that_looks_like_a_protocol(self):
        self._sample("concat:x|y.mp4")
        old = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, old)
        data = cf.analyze("concat:x|y.mp4", "out", cf.Params(top=2), thumbs=False, quiet=True)
        self.assertTrue(data["candidates"])

    def test_playlist_file_cannot_pull_in_other_files(self):
        secret = os.path.join(self.tmp.name, "secret.txt")
        with open(secret, "w") as fh:
            fh.write("top secret")
        pl = os.path.join(self.tmp.name, "evil.m3u8")
        with open(pl, "w") as fh:
            fh.write("#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\n%s\n#EXT-X-ENDLIST\n" % secret)
        with self.assertRaises(SystemExit):
            cf.probe(pl)

    def test_report_hides_command_for_unsafe_folder_names(self):
        base = {"video": "/x/a.mp4", "duration": 100.0, "created": "now", "run_id": "ab12cd34", "candidates": []}
        page = cf.build_html(base, {}, '/tmp/a"; echo pwned; "')
        self.assertIn("const OUT=null", page)
        ok = cf.build_html(base, {}, "/tmp/it's fine (1)")
        self.assertNotIn("const OUT=null", ok)
        for bad in ('a"b', "a$b", "a`b", "a%b", "a!b", "a\nb"):
            self.assertFalse(cf.shell_safe(bad), bad)
        for good in ("/home/me/My Game's clips (2)", "C:\\Users\\me\\എന്റെ ഗെയിം"):
            self.assertTrue(cf.shell_safe(good), good)

    def test_report_csp_blocks_base_and_forms(self):
        page = cf.build_html({"video": "a.mp4", "duration": 100.0, "created": "now", "run_id": "ab12cd34",
                              "candidates": []}, {}, "/tmp/x")
        self.assertIn("base-uri 'none'", page)
        self.assertIn("form-action 'none'", page)

    def test_odd_numbers_are_rejected_with_a_message(self):
        v = self._sample()
        for kw in ({"top": 0}, {"top": -3}, {"top": "x"}, {"z_min": float("nan")}, {"min_score": 2},
                   {"pre": -1}, {"max_len": 0}, {"baseline_s": 1}, {"weights": {"loud_peak": -1}},
                   {"weights": {"loud_peak": 0, "motion_burst": 0, "quiet_then_burst": 0, "slow_motion": 0,
                                "voice_excitement": 0}},
                   {"use": "loud_peak"}, {"keep_static": "yes"}):
            with self.assertRaises(SystemExit, msg=str(kw)):
                cf.analyze(v, os.path.join(self.tmp.name, "o"), cf.Params(**kw), thumbs=False, quiet=True)

    def test_bad_preset_files(self):
        for text in ("{not json", "[1, 2]", ""):
            path = os.path.join(self.tmp.name, "p.json")
            with open(path, "w") as fh:
                fh.write(text)
            with self.assertRaises(SystemExit, msg=text):
                cf.apply_preset(cf.Params(), path)

    def test_render_rejects_bad_options(self):
        v = self._sample()
        out = os.path.join(self.tmp.name, "o")
        data = cf.analyze(v, out, cf.Params(top=1), thumbs=False, quiet=True)
        for kw in ({"crop_x": 2}, {"crop_x": -0.1}, {"pad_pre": -1}, {"pad_post": float("nan")}, {"max_len": 0}):
            with self.assertRaises(SystemExit, msg=str(kw)):
                cf.render(out, [1], run=data["run_id"], quiet=True, **kw)

    def test_hand_edited_candidates_file_is_not_trusted(self):
        v = self._sample()
        out = os.path.join(self.tmp.name, "o")
        data = cf.analyze(v, out, cf.Params(top=1), thumbs=False, quiet=True)
        path = os.path.join(out, "candidates.json")
        for mutate in (lambda d: d.update(video="http://example.com/x.mp4"),
                       lambda d: d.update(video="relative.mp4"),
                       lambda d: d["candidates"][0].update(start="soon"),
                       lambda d: d.pop("duration")):
            d = json.loads(_read(path))
            mutate(d)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(d, fh)
            with self.assertRaises(SystemExit):
                cf.render(out, [1], quiet=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        with open(path, "w") as fh:
            fh.write("{broken")
        with self.assertRaises(SystemExit):
            cf.render(out, [1], quiet=True)

    def test_unknown_duration_gives_clear_error(self):
        raw = os.path.join(self.tmp.name, "raw.h264")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10:duration=12",
                        "-c:v", "libx264", "-f", "h264", raw], check=True)
        with self.assertRaises(SystemExit):
            cf.analyze(raw, os.path.join(self.tmp.name, "o"), cf.Params(), quiet=True)

    def test_no_temp_files_left_behind(self):
        v = self._sample()
        out = os.path.join(self.tmp.name, "o")
        cf.analyze(v, out, cf.Params(top=2), thumbs=False, quiet=True)
        self.assertEqual([n for n in os.listdir(out) if ".tmp" in n], [])

    def test_unwritable_output_location_gives_message(self):
        v = self._sample()
        blocker = os.path.join(self.tmp.name, "file.txt")
        with open(blocker, "w") as fh:
            fh.write("x")
        with self.assertRaises(SystemExit):
            cf.analyze(v, os.path.join(blocker, "sub"), cf.Params(), thumbs=False, quiet=True)

    def test_huge_skip_does_not_crash(self):
        v = self._sample()
        data = cf.analyze(v, os.path.join(self.tmp.name, "o"), cf.Params(skip_start=5000, skip_end=5000),
                          thumbs=False, quiet=True)
        self.assertEqual(data["candidates"], [])

    def test_output_folder_with_percent_sign_for_downloads(self):
        sample = self._sample("orig.mp4")
        bindir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bindir)
        script = os.path.join(bindir, "yt-dlp")
        with open(script, "w") as fh:
            fh.write("#!%s\nimport shutil, sys\na = sys.argv\n"
                     "o = a[a.index('-o') + 1].replace('%%(ext)s', 'webm').replace('%%%%', '%%')\n"
                     "shutil.copy(%r, o)\n" % (sys.executable, sample))
        os.chmod(script, 0o755)
        out = os.path.join(self.tmp.name, "100%_run")
        with mock.patch.dict(os.environ, {"PATH": bindir + os.pathsep + os.environ["PATH"]}):
            got = cf.download_video("https://youtu.be/abc", out, quiet=True)
            again = cf.download_video("https://youtu.be/abc", out, quiet=True)
        self.assertTrue(got.endswith("source.webm") and os.path.isfile(got))   # not only .mp4
        self.assertEqual(got, again)


class TestPrivacy(unittest.TestCase):
    ALLOWED = {"argparse", "base64", "hashlib", "html", "json", "math", "os", "shutil", "subprocess", "sys",
               "time", "re", "concurrent", "dataclasses", "numpy", "__future__", "threading", "unicodedata"}

    def test_only_expected_imports_and_no_network_modules(self):
        tree = ast.parse(_read(os.path.join(ROOT, "clipfinder.py")))
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module.split(".")[0])
        self.assertLessEqual(mods, self.ALLOWED, mods - self.ALLOWED)

    def test_ffmpeg_is_only_ever_called_with_a_list(self):
        tree = ast.parse(_read(os.path.join(ROOT, "clipfinder.py")))
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "shell":
                self.fail("shell=... is used")
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in ("system", "popen"):
                self.fail("os.system/popen is used")
            if isinstance(node, ast.Name) and node.id in ("eval", "exec"):
                self.fail("eval/exec is used")


class TestCLI(unittest.TestCase):
    def test_end_to_end_via_command_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            v = make_sample(os.path.join(tmp, "game.mp4"))
            out = os.path.join(tmp, "result")
            run = lambda *a: subprocess.run([sys.executable, "-I", os.path.join(ROOT, "clipfinder.py"), *a],  # noqa: E731
                                            capture_output=True, text=True, encoding="utf-8")
            r = run("conditions")
            self.assertEqual(r.returncode, 0)
            self.assertIn("slow_motion", r.stdout)
            r = run("analyze", v, "--out", out, "--preset", "slowmo", "--quiet")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("slow_motion", r.stdout)
            self.assertTrue(os.path.isfile(os.path.join(out, "report.html")))
            run_id = json.loads(_read(os.path.join(out, "candidates.json")))["run_id"]
            r = run("render", out, "--run", run_id, "--ids", "1", "--quiet")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(os.path.isfile(r.stdout.strip().splitlines()[-1]))
            r = run("render", out, "--run", "wrong", "--ids", "1")
            self.assertEqual(r.returncode, 2)
            self.assertIn("ERROR", r.stderr)
            r = run("analyze", v, "--out", out, "--use", "teleport")
            self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
