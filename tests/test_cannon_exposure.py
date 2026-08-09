"""The exposure the Canon is told to use, and where its settings file lives.

Every other camera takes its exposure from ``config.json``; the Canon used to
take it from a generated, gitignored ``camcfg_<Model>.ini`` — and ``apply_camcfg``
writes that file into the camera inside ``except Exception: pass``, so a value
the camera does not offer produced no error at all and the night was shot at
whatever exposure happened to be set. These tests pin the two halves of the fix:
a value from ``config.json`` reaches the camera, and a value the camera rejects
is refused loudly instead of being dropped on the floor.

``cameras.cannon_driver`` imports gphoto2-cffi at module level and patches its
internals, so the binding is stubbed here — none of this needs a real camera.
"""
import os
import sys
import types

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# gphoto2-cffi stubs, installed before the driver is imported
# ---------------------------------------------------------------------------
def _install_gphoto_stub():
    if "gphoto2cffi" in sys.modules:
        return
    root = types.ModuleType("gphoto2cffi")
    backend = types.ModuleType("gphoto2cffi.backend")
    util = types.ModuleType("gphoto2cffi.util")
    main = types.ModuleType("gphoto2cffi.gphoto2")

    backend.ffi = types.SimpleNamespace(string=lambda *a, **k: b"")
    backend.lib = types.SimpleNamespace()
    # The driver replaces both of these with its own decoder.
    util.get_ctype = lambda *a, **k: None
    util.get_string = lambda *a, **k: ""
    main.get_string = lambda *a, **k: ""

    root.backend, root.util, root.gphoto2 = backend, util, main
    sys.modules.update({
        "gphoto2cffi": root,
        "gphoto2cffi.backend": backend,
        "gphoto2cffi.util": util,
        "gphoto2cffi.gphoto2": main,
    })


_install_gphoto_stub()

from cameras import cannon_driver                              # noqa: E402


# ---------------------------------------------------------------------------
# A camera config tree shaped like gphoto2's
# ---------------------------------------------------------------------------
SHUTTER_CHOICES = ["30", "10.3", "1", "1/125", "1/1000", "1/4000"]


class FakeWidget:
    def __init__(self, value, choices=()):
        self.value = value
        self._choices = list(choices)
        self.sets = []

    def _read_choices(self):
        return list(self._choices)

    def set(self, value):
        self.sets.append(value)
        self.value = value


def make_config(shutter="1", mode="Manual", section="capturesettings"):
    """A 6D-shaped tree: the exposure is not in the first section scanned."""
    return {
        "status": {"model": FakeWidget("Canon EOS 6D")},
        "imgsettings": {"iso": FakeWidget("3200", ["100", "3200"])},
        section: {
            "shutterspeed": FakeWidget(shutter, SHUTTER_CHOICES),
            "autoexposuremode": FakeWidget(mode, ["P", "Manual", "Bulb"]),
        },
    }


def shutter_of(config):
    for section in config.values():
        if "shutterspeed" in section:
            return section["shutterspeed"]
    raise AssertionError("no shutterspeed in this config")


# ---------------------------------------------------------------------------
# Finding a setting
# ---------------------------------------------------------------------------
def test_a_setting_is_found_whatever_section_holds_it():
    """A 300D keeps everything in `settings`, a 6D in `capturesettings`."""
    for section in ("capturesettings", "settings"):
        config = make_config(section=section)
        assert cannon_driver.find_widget(config, "shutterspeed") is not None


def test_the_sections_gphoto_reserves_are_not_searched():
    """`status` holds read-only values; nothing there is a setting to write."""
    assert cannon_driver.find_widget(make_config(), "model") is None


# ---------------------------------------------------------------------------
# Applying the exposure
# ---------------------------------------------------------------------------
def test_a_supported_exposure_reaches_the_camera():
    config = make_config(shutter="1")
    assert cannon_driver.apply_shutterspeed(config, "1/125") is True
    assert shutter_of(config).sets == ["1/125"]


def test_an_unsupported_exposure_leaves_the_camera_alone(capsys):
    """The regression: this used to vanish into `except Exception: pass`."""
    config = make_config(shutter="1")
    assert cannon_driver.apply_shutterspeed(config, "1/126") is False
    assert shutter_of(config).sets == []
    assert shutter_of(config).value == "1"
    # The operator cannot read the choices anywhere else: the .ini is generated
    # and gitignored, so the error has to carry them.
    out = capsys.readouterr().out
    assert "1/126" in out and "1/4000" in out


def test_an_empty_value_means_leave_the_exposure_as_it_is():
    config = make_config(shutter="1")
    for value in ("", "   ", None):
        assert cannon_driver.apply_shutterspeed(config, value) is False
    assert shutter_of(config).sets == []


def test_surrounding_whitespace_is_not_a_typo():
    config = make_config()
    assert cannon_driver.apply_shutterspeed(config, " 1/125 ") is True
    assert shutter_of(config).sets == ["1/125"]


def test_a_camera_without_a_shutterspeed_setting_is_survivable():
    config = {"imgsettings": {"iso": FakeWidget("100", ["100", "200"])}}
    assert cannon_driver.apply_shutterspeed(config, "1/125") is False


def test_a_non_manual_mode_is_warned_about(capsys):
    """In P/Av/Tv the camera picks its own exposure and ignores what we wrote."""
    config = make_config(mode="P")
    assert cannon_driver.apply_shutterspeed(config, "1/125") is True
    assert shutter_of(config).sets == ["1/125"]
    assert "Manual" in capsys.readouterr().out

    config = make_config(mode="Manual")
    cannon_driver.apply_shutterspeed(config, "1/125")
    assert "not Manual" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Where the settings file lives
# ---------------------------------------------------------------------------
def test_the_settings_file_is_named_after_the_model_by_default():
    path = cannon_driver.camcfg_path("Canon EOS 6D")
    assert os.path.basename(path) == "camcfg_Canon_EOS_6D.ini"
    assert os.path.dirname(path) == cannon_driver.APP_DIR


def test_an_explicit_settings_file_wins(tmp_path):
    """`cannon.camcfg_file` was in the config, the GUI and the README, and did
    nothing at all — the path was always rebuilt from the model name."""
    explicit = tmp_path / "my.ini"
    assert cannon_driver.camcfg_path("Canon EOS 6D", str(explicit)) == str(explicit)


def test_a_relative_settings_file_is_resolved_next_to_the_app():
    path = cannon_driver.camcfg_path("Canon EOS 6D", "cfg/canon.ini")
    assert path == os.path.join(cannon_driver.APP_DIR, "cfg/canon.ini")


# ---------------------------------------------------------------------------
# config.json carries the key
# ---------------------------------------------------------------------------
def test_an_existing_config_gains_the_key_without_a_migration(tmp_path):
    """`load_config` merges over the defaults, so nobody has to edit by hand."""
    import json
    import utils

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"cannon": {"output_dir": "/data"}}))
    cfg = utils.load_config(str(path))
    assert cfg["cannon"]["shutterspeed"] == ""
    assert cfg["cannon"]["output_dir"] == "/data"
