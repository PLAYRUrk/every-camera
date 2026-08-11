"""Where the SPTT firmware is looked for, and what happens when it is missing.

The CSDU-429 arrives on the bus as a bare Cypress FX2 and only becomes a camera
once the 8051 firmware and the FPGA bitstream have been pushed into it. The
loader resolved its default directory from ``__file__``; when the module moved
down into ``cameras/`` the default moved with it, to ``cameras/firmware/`` — a
directory that has never existed. The blobs sit in ``firmware/`` at the program
root, so nothing could be found, and the loader answered by calling
``sys.exit(1)`` from inside a library function. The vendor's own copy in
SPTT-CAM kept working because it still sits next to its own firmware folder,
which is exactly why the old program could talk to the camera and this one
could not.

``sptt.firmware_dir`` has been in the config, the setup form and the README the
whole time; nothing ever passed it to the loader.
"""
import os
import sys

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cameras.sptt_load_firmware import (      # noqa: E402
    firmware_path, load_firmware_files,
)
from utils import APP_DIR                     # noqa: E402


def test_the_default_directory_is_the_program_root_not_the_package():
    """The regression: cameras/firmware/ is not where the blobs live."""
    assert firmware_path() == os.path.join(APP_DIR, "firmware")
    assert not firmware_path().endswith(os.path.join("cameras", "firmware"))


def test_the_shipped_firmware_is_where_the_loader_looks():
    directory = Path(firmware_path())
    assert (directory / "fx2_firmware.bin").is_file()
    assert (directory / "fpga_bitstream.bin").is_file()


def test_the_shipped_firmware_actually_loads():
    fx2, fpga = load_firmware_files()
    assert len(fx2) == 6936
    assert len(fpga) == 54908


def test_a_configured_directory_is_used(tmp_path):
    """sptt.firmware_dir was collected by the setup form and then ignored."""
    assert firmware_path(str(tmp_path)) == str(tmp_path)


def test_a_relative_directory_hangs_off_the_program_root(tmp_path):
    assert firmware_path("fw") == os.path.join(APP_DIR, "fw")


def test_missing_firmware_is_raised_not_exited(tmp_path):
    """sys.exit would take the GUI down and dodge its except Exception."""
    with pytest.raises(FileNotFoundError) as excinfo:
        load_firmware_files(str(tmp_path))
    message = str(excinfo.value)
    assert "fx2_firmware.bin" in message
    assert "firmware_dir" in message          # tells the operator the way out


def test_a_half_populated_directory_is_still_an_error(tmp_path):
    (tmp_path / "fx2_firmware.bin").write_bytes(b"\x00" * 6936)
    with pytest.raises(FileNotFoundError) as excinfo:
        load_firmware_files(str(tmp_path))
    assert "fpga_bitstream.bin" in str(excinfo.value)


def test_the_config_key_reaches_the_loader():
    """ensure_firmware_loaded has to accept it, or the key is decoration."""
    import inspect

    from cameras.sptt_driver import ensure_firmware_loaded

    assert "firmware_dir" in inspect.signature(ensure_firmware_loaded).parameters
