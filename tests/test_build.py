"""Build/install integration tests; no display or user configuration is touched.

Set XCURSOR_TEST_LIBRARY to a libXcursor shared library to enable native-loader
pixel comparisons when the system loader cannot find libXcursor automatically.
"""

import ctypes
import ctypes.util
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

from tools.cursorlib import load_cursor
from tools.xcursor import read_headers


ROOT = Path(__file__).resolve().parent.parent
SIZES = [24, 32, 48, 64, 96]


class NativeImage(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "version", "size", "width", "height", "xhot", "yhot", "delay"
    )] + [("pixels", ctypes.POINTER(ctypes.c_uint32))]


class NativeImages(ctypes.Structure):
    _fields_ = [
        ("nimage", ctypes.c_int),
        ("images", ctypes.POINTER(ctypes.POINTER(NativeImage))),
        ("name", ctypes.c_char_p),
    ]


def native_library():
    name = os.environ.get("XCURSOR_TEST_LIBRARY") or ctypes.util.find_library("Xcursor")
    try:
        library = ctypes.CDLL(name or "libXcursor.so.1")
    except OSError:
        return None
    library.XcursorFilenameLoadImages.argtypes = [ctypes.c_char_p, ctypes.c_int]
    library.XcursorFilenameLoadImages.restype = ctypes.POINTER(NativeImages)
    library.XcursorImagesDestroy.argtypes = [ctypes.POINTER(NativeImages)]
    library.XcursorImagesDestroy.restype = None
    return library


NATIVE = native_library()


def snapshot(folder):
    result = {}
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder).as_posix()
        if path.is_symlink():
            result[relative] = ("symlink", os.readlink(path))
        elif path.is_file():
            result[relative] = ("file", hashlib.sha256(path.read_bytes()).hexdigest())
    return result


class BuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="kaltsit-integration-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.project = Path(cls.temporary.name) / "project with spaces"
        cls.project.mkdir()
        for name in ("tools", "assets/windows"):
            shutil.copytree(ROOT / name, cls.project / name, ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("cursors.json", "install.sh"):
            shutil.copyfile(ROOT / name, cls.project / name)
        cls.manifest_text = (cls.project / "cursors.json").read_text()
        cls.manifest = json.loads(cls.manifest_text)
        cls.theme = cls.project / "theme/Kaltsit"
        result = cls.build_command(cls.theme)
        if result.returncode:
            raise AssertionError(result.stderr)
        cls.report = json.loads((cls.theme / "build-report.json").read_text())

    @classmethod
    def build_command(cls, output, sizes=SIZES):
        return subprocess.run(
            [sys.executable, str(cls.project / "tools/build_theme.py"),
             "--output", str(output), "--sizes", *map(str, sizes)],
            text=True, capture_output=True, check=False,
        )

    def test_all_sources_aliases_metadata_and_hashes(self):
        self.assertEqual(len(self.manifest), 17)
        self.assertEqual(self.report["sizes"], SIZES)
        self.assertEqual(set(self.report["cursors"]), set(self.manifest))
        expected_names = set(self.manifest)
        for name, spec in self.manifest.items():
            source = self.project / "assets/windows" / spec["source"]
            cursor = self.theme / "cursors" / name
            report = self.report["cursors"][name]
            self.assertEqual(report["source_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
            frames = load_cursor(source, 32)
            self.assertEqual(report["frames_per_size"], len(frames))
            self.assertEqual(report["duration_ms"], sum(frame.delay_ms for frame in frames))
            headers = read_headers(cursor)
            self.assertEqual([header["size"] for header in headers],
                             [size for size in SIZES for _ in frames])
            self.assertEqual(len(report["images"]), len(headers))
            for recorded, header in zip(report["images"], headers):
                self.assertEqual(recorded, {key: header[key] for key in recorded})
            for alias in spec["aliases"]:
                expected_names.add(alias)
                path = self.theme / "cursors" / alias
                self.assertTrue(path.is_symlink())
                self.assertEqual(os.readlink(path), name)
                self.assertEqual(path.resolve(), cursor.resolve())
                self.assertTrue(path.is_file(), f"broken alias: {alias}")
        self.assertEqual({path.name for path in (self.theme / "cursors").iterdir()}, expected_names)

    @unittest.skipIf(NATIVE is None, "libXcursor is unavailable; set XCURSOR_TEST_LIBRARY")
    def test_libxcursor_matches_every_source_frame_at_every_size(self):
        for name, spec in self.manifest.items():
            for size in SIZES:
                with self.subTest(cursor=name, size=size):
                    expected = load_cursor(self.project / "assets/windows" / spec["source"], size)
                    loaded = NATIVE.XcursorFilenameLoadImages(os.fsencode(self.theme / "cursors" / name), size)
                    self.assertTrue(loaded, "libXcursor rejected the generated cursor")
                    try:
                        self.assertEqual(loaded.contents.nimage, len(expected))
                        for index, frame in enumerate(expected):
                            actual = loaded.contents.images[index].contents
                            self.assertEqual((actual.size, actual.width, actual.height),
                                             (size, frame.image.width, frame.image.height))
                            self.assertEqual((actual.xhot, actual.yhot), frame.hotspot)
                            self.assertEqual(actual.delay, frame.delay_ms)
                            # Pack native uint32 values explicitly as little-endian;
                            # this comparison also works on a big-endian test host.
                            count = actual.width * actual.height
                            pixels = struct.pack(f"<{count}I", *actual.pixels[:count])
                            self.assertEqual(pixels, frame.image.convert("RGBa").tobytes("raw", "BGRa"))
                    finally:
                        NATIVE.XcursorImagesDestroy(loaded)

    def test_build_is_byte_deterministic_and_replaces_managed_output(self):
        original = snapshot(self.theme)
        output = Path(self.temporary.name) / "second build"
        result = self.build_command(output, list(reversed(SIZES)) + [32])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(snapshot(output), original)
        (output / "stale-file").write_text("remove during rebuild")
        result = self.build_command(output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(snapshot(output), original)

    def test_build_refuses_unmanaged_directories_and_symlink_destinations(self):
        with tempfile.TemporaryDirectory(dir=self.temporary.name) as folder:
            unmanaged = Path(folder) / "unmanaged"
            unmanaged.mkdir()
            sentinel = unmanaged / "keep"
            sentinel.write_text("untouched")
            result = self.build_command(unmanaged)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(sentinel.read_text(), "untouched")
            link = Path(folder) / "symlink"
            link.symlink_to(unmanaged, target_is_directory=True)
            result = self.build_command(link)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(link.is_symlink())
            self.assertEqual(sentinel.read_text(), "untouched")

    def test_invalid_manifest_paths_and_duplicate_aliases_are_rejected(self):
        mutations = [
            lambda m: m["left_ptr"].update(source="../Normal.ani"),
            lambda m: m["left_ptr"].update(aliases=["../escape"]),
            lambda m: m["left_ptr"].update(aliases=["/tmp/escape"]),
            lambda m: m["left_ptr"].update(aliases=["watch"]),
        ]
        path = self.project / "cursors.json"
        output = Path(self.temporary.name) / "invalid-output"
        try:
            for mutate in mutations:
                manifest = json.loads(self.manifest_text)
                mutate(manifest)
                path.write_text(json.dumps(manifest))
                result = self.build_command(output)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(output.exists())
        finally:
            path.write_text(self.manifest_text)

    def test_install_reinstall_backup_and_uninstall_under_temporary_prefix(self):
        with tempfile.TemporaryDirectory(dir=self.temporary.name) as folder:
            prefix = Path(folder) / "icons with spaces"
            prefix.mkdir()
            sentinel = prefix / "unrelated-theme"
            sentinel.write_text("leave me alone")
            command = ["sh", str(self.project / "install.sh"), "--prefix", str(prefix)]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(snapshot(prefix / "Kaltsit"), snapshot(self.theme))
            (prefix / "Kaltsit" / "custom-marker").write_text("preserve in backup")
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            backups = list(prefix.glob("Kaltsit.backup.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual((backups[0] / "Kaltsit/custom-marker").read_text(), "preserve in backup")
            self.assertEqual(snapshot(prefix / "Kaltsit"), snapshot(self.theme))
            result = subprocess.run(command + ["--uninstall"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((prefix / "Kaltsit").exists())
            self.assertEqual(sentinel.read_text(), "leave me alone")
            self.assertTrue(backups[0].is_dir())
            self.assertEqual(list(prefix.glob(".kaltsit-install.*")), [])

    def test_installer_refuses_symlinks_and_unmanaged_uninstall(self):
        with tempfile.TemporaryDirectory(dir=self.temporary.name) as folder:
            prefix = Path(folder) / "icons"
            prefix.mkdir()
            target = Path(folder) / "outside"
            target.mkdir()
            (target / "keep").write_text("untouched")
            link = prefix / "Kaltsit"
            link.symlink_to(target, target_is_directory=True)
            command = ["sh", str(self.project / "install.sh"), "--prefix", str(prefix)]
            for args in ([], ["--uninstall"]):
                result = subprocess.run(command + args, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(link.is_symlink())
                self.assertEqual((target / "keep").read_text(), "untouched")
            link.unlink()
            link.mkdir()
            (link / "keep").write_text("unmanaged")
            result = subprocess.run(command + ["--uninstall"], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((link / "keep").read_text(), "unmanaged")


if __name__ == "__main__":
    unittest.main()
