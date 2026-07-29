"""The vendored DCAM wrappers: importable without the SDK, patched where it counts.

Hamamatsu's ``dcamapi4.py`` loads ``libdcamapi.so`` and binds fifty-odd ctypes
prototypes *at import time*. every-camera must import ``cameras.japan_driver`` on
any machine — ``main.py`` dispatches through it, every test in this directory
exercises the driver against simulators, and a station running only the simulator
has no SDK at all. That is only true as long as nothing imports the vendor modules
at module scope, so the first test here is the one that keeps the whole suite
runnable.

The last test guards the other half: the vendor file carries exactly one local
change, and a future SDK bump that copies the file in and forgets to re-apply it
would restore a hard-coded path. That failure would otherwise surface on the
station, at night.
"""
import subprocess
import sys

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cameras.japan import dcamsdk                          # noqa: E402

VENDOR = ROOT / "cameras" / "japan" / "dcamsdk" / "vendor"


def run_python(code):
    """Run a snippet in a fresh interpreter rooted at the repo."""
    return subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                          capture_output=True, text=True, timeout=120)


# ---------------------------------------------------------------------------
# Importable with no SDK installed
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("module", [
    "cameras.japan.dcamsdk",
    "cameras.japan.camera",
    "cameras.japan.devices",
    "cameras.japan_driver",
    "japan_driver",
])
def test_the_driver_imports_without_the_sdk(module):
    """A fresh interpreter, so a module another test already imported cannot help."""
    result = run_python(f"import {module}")
    assert result.returncode == 0, result.stderr


def test_importing_the_driver_does_not_load_the_library():
    """Not just "it worked" — the vendor modules must not be in sys.modules.

    On a machine that *does* have the SDK, a stray module-scope import would still
    succeed here and the guard above would pass while the real property — nothing
    touches the hardware until a camera is opened — had quietly been lost.
    """
    result = run_python(
        "import sys, cameras.japan_driver;"
        "print([m for m in ('dcam', 'dcamapi4') if m in sys.modules])")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_the_simulator_backend_needs_nothing_from_the_sdk():
    result = run_python(
        "from cameras.japan import config, devices;"
        "c = config.from_dict({'camera': {'backend': 'sim'},"
        " 'filter_wheel': {'port': 'sim'}});"
        "cam = devices.make_camera(c);"
        "print(type(cam).__name__)")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "SimCamera"


# ---------------------------------------------------------------------------
# A missing runtime says what it looked for
# ---------------------------------------------------------------------------
def test_a_missing_runtime_names_every_path_it_tried(monkeypatch):
    monkeypatch.setenv("DCAM_LIB", "/nonexistent/libdcamapi.so")
    monkeypatch.setattr(dcamsdk, "_sdk", None)
    with pytest.raises(OSError) as excinfo:
        dcamsdk.load()
    message = str(excinfo.value)
    assert "DCAM_LIB" in message, "the override is not mentioned"
    assert "/nonexistent/libdcamapi.so" in message, "the tried path is not shown"
    assert "/usr/local/lib/libdcamapi.so" in message, "the default is not shown"


def test_a_failed_load_leaves_no_half_imported_module(monkeypatch):
    """Otherwise the second attempt fails differently from the first."""
    monkeypatch.setenv("DCAM_LIB", "/nonexistent/libdcamapi.so")
    monkeypatch.setattr(dcamsdk, "_sdk", None)
    for _ in range(2):
        with pytest.raises(OSError):
            dcamsdk.load()
    assert [m for m in ("dcam", "dcamapi4") if m in sys.modules] == []


def test_the_vendor_directory_is_not_left_on_the_path(monkeypatch):
    """``dcam`` is a very ordinary name to leak into every later import."""
    monkeypatch.setenv("DCAM_LIB", "/nonexistent/libdcamapi.so")
    monkeypatch.setattr(dcamsdk, "_sdk", None)
    before = list(sys.path)
    with pytest.raises(OSError):
        dcamsdk.load()
    assert sys.path == before


# ---------------------------------------------------------------------------
# The vendor files themselves
# ---------------------------------------------------------------------------
def test_both_vendor_wrappers_are_present():
    assert (VENDOR / "dcamapi4.py").exists()
    assert (VENDOR / "dcam.py").exists()


def test_the_library_path_hunk_is_still_applied():
    """The one local change. A verbatim re-drop of the SDK loses it silently."""
    source = (VENDOR / "dcamapi4.py").read_text()
    assert "DCAM_LIB" in source, \
        "the DCAM_LIB override is gone — was the vendor file re-copied?"
    assert "cdll.LoadLibrary('/usr/local/lib/libdcamapi.so')" not in source, \
        "the hard-coded library path is back"
    assert "every-camera:" in source, "the marker comment around the hunk is gone"


def test_the_hunk_keeps_its_names_out_of_the_star_import():
    """``dcam.py`` does ``from dcamapi4 import *``.

    A helper spelled without the leading double underscore would be exported into
    it, and from there into anything doing the same — which is how a name like
    ``os`` ends up shadowed three modules away.
    """
    source = (VENDOR / "dcamapi4.py").read_text()
    hunk = source.split("every-camera:", 1)[1].split("# --- end ---", 1)[0]
    assigned = set()
    for line in hunk.splitlines():
        stripped = line.strip()
        if stripped.startswith(("#", "import ", "from ")) or "=" not in stripped:
            continue
        name = stripped.split("=", 1)[0].strip()
        if name.isidentifier():
            assigned.add(name)
    public = {name for name in assigned if not name.startswith("__")}
    assert public == set(), f"module-level names visible to 'import *': {public}"


def test_the_vendor_wrapper_is_otherwise_untouched():
    """Everything outside the hunk should still be Hamamatsu's own file."""
    source = (VENDOR / "dcamapi4.py").read_text()
    assert "Copyright (C) 2021-2025 Hamamatsu Photonics K.K." in source
    assert (VENDOR / "dcam.py").read_text().count("from dcamapi4 import *") == 1
