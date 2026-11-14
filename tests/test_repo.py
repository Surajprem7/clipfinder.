"""Checks on the files around the program: what may go to GitHub, and what the Windows build does."""
from __future__ import annotations

import importlib.util
import os
import re
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
VIDEO_EXT = (".mp4", ".mkv", ".mov", ".webm", ".m4v", ".ts", ".avi", ".flv", ".wav", ".mp3")


def load(name, *parts):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, *parts))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class TestWhatGoesToGitHub(unittest.TestCase):
    def test_gitignore_blocks_every_video_and_audio_type(self):
        lines = {ln.strip() for ln in read(".gitignore").splitlines()}
        for ext in VIDEO_EXT:
            self.assertIn("*" + ext, lines, ext)
        for folder in ("tools/", "dist/", "build/", "installer_out/", "videos/", "analyses/"):
            self.assertIn(folder, lines, folder)

    def test_the_project_folder_holds_no_videos_and_no_big_files(self):
        for dirpath, dirs, files in os.walk(ROOT):
            # (the build's own output and downloaded helper programs are never committed: .gitignore blocks them)
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "tools", "tools_dl", "dist", "build", "installer_out")]
            for name in files:
                path = os.path.join(dirpath, name)
                self.assertFalse(name.lower().endswith(VIDEO_EXT), path)
                self.assertLess(os.path.getsize(path), 2 * 1024 * 1024, path)


class TestWindowsBuild(unittest.TestCase):
    def test_workflow_is_manual_read_only_and_checks_what_it_downloads(self):
        wf = read(".github", "workflows", "build-windows.yml")
        self.assertIn("workflow_dispatch:", wf)
        for trigger in ("pull_request", "pull_request_target", "push:", "schedule:"):
            self.assertNotIn(trigger, wf, trigger)          # it only runs when the owner presses the button
        self.assertRegex(wf, r"permissions:\s*\n\s+contents: read\n")
        self.assertNotIn("secrets.", wf)
        self.assertNotIn("write-all", wf)
        self.assertIn("Get-FileHash", wf)                    # downloads are compared with the published checksums
        self.assertIn("retention-days: 1", wf)               # the free storage never fills up
        self.assertNotRegex(wf, r"(?i)iex|Invoke-Expression|\|\s*sh\b|\|\s*bash\b")
        urls = set(re.findall(r"https://[^\s\"']+", wf))
        hosts = {u.split("/")[2] for u in urls}
        self.assertEqual(hosts, {"github.com"})

    def test_installer_is_per_user_and_keeps_the_videos_on_uninstall(self):
        iss = read("packaging", "clipfinder.iss")
        self.assertIn("PrivilegesRequired=lowest", iss)
        self.assertNotIn("[UninstallDelete]", iss)           # videos and clips are never deleted by the uninstaller
        self.assertIn("..\\dist\\ClipFinder", iss)

    def test_packaging_recipe_takes_the_web_page_along(self):
        spec = read("packaging", "clipfinder.spec")
        self.assertIn('"web"', spec)
        self.assertIn("clipfinder_web.py", spec)
        self.assertIn("upx=False", spec)

    def test_licence_note_names_every_bundled_program(self):
        text = read("packaging", "THIRD_PARTY.txt")
        for name in ("ffmpeg", "yt-dlp", "numpy", "PyInstaller"):
            self.assertIn(name, text)

    def test_windows_scripts_are_plain_ascii_with_windows_line_ends(self):
        for name in ("start_clipfinder.bat", "setup_windows.bat"):
            with open(os.path.join(ROOT, name), "rb") as fh:
                raw = fh.read()
            raw.decode("ascii")
            self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"), name)


class TestNothingElseHasToBeInstalled(unittest.TestCase):
    """The installer must carry Python, numpy, the Visual C++ runtime, the file dialog, ffmpeg, ffprobe and yt-dlp."""

    @classmethod
    def setUpClass(cls):
        cls.cb = load("check_bundle", "packaging", "check_bundle.py")

    def test_the_list_of_required_parts_covers_everything(self):
        text = " ".join(p for patterns in self.cb.REQUIRED.values() for p in patterns).lower()
        for part in ("clipfinder.exe", "python3", "vcruntime140.dll", "_multiarray_umath", "_tkinter", "tcl86t.dll", "tk86t.dll", "init.tcl", "web/index.html",
                     "tools/ffmpeg.exe", "tools/ffprobe.exe", "tools/yt-dlp.exe"):
            self.assertIn(part, text, part)

    def test_each_imported_dll_is_judged_correctly(self):
        c = self.cb.classify
        bundled = {"python312.dll", "vcruntime140.dll", "tcl86t.dll"}
        in_system = lambda n: n in {"kernel32.dll", "vcruntime140_1.dll", "msvcp140.dll", "libwinpthread-1.dll"}   # noqa: E731
        self.assertEqual(c("python312.dll", bundled, in_system), "bundled")
        self.assertEqual(c("PYTHON312.DLL", bundled, in_system), "bundled")
        self.assertEqual(c("kernel32.dll", bundled, in_system), "system")
        self.assertEqual(c("api-ms-win-crt-runtime-l1-1-0.dll", bundled, in_system), "system")
        self.assertEqual(c("ext-ms-win-foo-l1-1-0.dll", bundled, lambda n: False), "system")
        # the Visual C++ runtime does not count as "comes with Windows", even if this computer happens to have it
        self.assertEqual(c("vcruntime140_1.dll", bundled, in_system), "missing")
        self.assertEqual(c("msvcp140.dll", bundled, in_system), "missing")
        # other third-party DLLs are not accepted just because they sit in a system folder on one computer
        self.assertEqual(c("libgcc_s_seh-1.dll", bundled, in_system), "missing")
        self.assertEqual(c("somethingelse.dll", bundled, lambda n: False), "missing")

    def test_missing_parts_are_reported(self):
        with tempfile.TemporaryDirectory() as d:
            problems = self.cb.missing_files(d)
            self.assertEqual(len(problems), sum(len(v) for v in self.cb.REQUIRED.values()))
            os.makedirs(os.path.join(d, "tools"))
            for name in ("ffmpeg.exe", "ffprobe.exe", "yt-dlp.exe"):
                open(os.path.join(d, "tools", name), "w").close()
            after = self.cb.missing_files(d)
            self.assertFalse([p for p in after if "tools/" in p])
            self.assertTrue([p for p in after if "ClipFinder.exe" in p])

    def test_the_test_run_of_the_program_gets_a_bare_environment(self):
        smoke = load("smoke_exe", "packaging", "smoke_exe.py")
        env = {"PATH": "C:\\tools;C:\\Python312", "PYTHONHOME": "C:\\Python312", "PYTHONPATH": "x", "SystemRoot": "C:\\Windows",
               "TEMP": "C:\\Temp", "GITHUB_TOKEN": "secret", "FFMPEG_DIR": "C:\\tools"}
        with mock.patch.dict(os.environ, env, clear=True):
            clean = smoke.clean_environment()
        self.assertEqual(clean["PATH"], os.pathsep.join([os.path.join("C:\\Windows", "System32"), "C:\\Windows"]))
        self.assertNotIn("tools", clean["PATH"].lower())
        self.assertNotIn("python", clean["PATH"].lower())
        self.assertFalse([k for k in clean if k.upper().startswith("PYTHON")])
        self.assertNotIn("GITHUB_TOKEN", clean)
        self.assertEqual(clean["SystemRoot"], "C:\\Windows")
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin", "HOME": "/home/x"}, clear=True):
            self.assertEqual(smoke.clean_environment()["PATH"], "/nonexistent")

    def test_workflow_checks_the_build_and_the_installed_copy(self):
        wf = read(".github", "workflows", "build-windows.yml")
        self.assertEqual(wf.count("packaging/check_bundle.py"), 2)            # on the build and on the installed copy
        self.assertEqual(len(re.findall(r"smoke_exe\.py [^\n]*--clean-env", wf)), 2)       # the build and the installed copy
        self.assertIn("/VERYSILENT", wf)                                      # the real installer is run
        self.assertIn("unins000.exe", wf)                                     # and the uninstaller
        self.assertIn("pefile", wf)
        spec = read("packaging", "clipfinder.spec")
        self.assertIn("vcruntime140.dll", spec)
        self.assertIn("vcruntime140_1.dll", spec)

    def test_nothing_needs_dotnet_java_or_a_separate_python(self):
        # the whole project is Python + HTML; the only other programs are ffmpeg, ffprobe and yt-dlp
        for name in ("clipfinder.py", "clipfinder_web.py"):
            src = read(name)
            for banned in ("clr.", "pythonnet", "System.Windows", "dotnet", "java ", "node "):
                self.assertNotIn(banned, src, f"{name}: {banned}")
        iss = read("packaging", "clipfinder.iss")
        self.assertNotIn("dotnet", iss.lower())
        self.assertNotIn("netfx", iss.lower())


if __name__ == "__main__":
    unittest.main()


class TestRevisions(unittest.TestCase):
    """Every revision is written down in CHANGELOG.md, newest first, and the program says which one it is."""

    def test_the_newest_changelog_entry_is_the_program_version(self):
        version = re.search(r'^__version__ = "([0-9.]+)"', read("clipfinder.py"), re.M).group(1)
        found = re.findall(r"^## v([0-9.]+) \(\d{4}-\d{2}-\d{2}\)", read("CHANGELOG.md"), re.M)
        self.assertTrue(found)
        self.assertEqual(found[0], version)

    def test_changelog_versions_only_go_up(self):
        found = re.findall(r"^## v([0-9.]+) ", read("CHANGELOG.md"), re.M)
        nums = [tuple(int(x) for x in v.split(".")) for v in found]
        self.assertEqual(nums, sorted(nums, reverse=True))
        self.assertEqual(len(nums), len(set(nums)))

    def test_the_installer_gets_the_same_version(self):
        wf = read(".github", "workflows", "build-windows.yml")
        self.assertIn("clipfinder.__version__", wf)
        self.assertIn('"/DAppVersion=$ver"', wf)
