#!/usr/bin/env python3
"""Firmware loader for CSDU cameras: 429/423 and 285C (Cypress FX2 + Xilinx FPGA).

Loads FX2 8051 firmware via vendor request 0xA0, then the Xilinx FPGA bitstream via
0xF1.

Usage:
    sudo python -m cameras.sptt_load_firmware

Firmware files (in the program's firmware/ directory):
    csdu285.spt        - vendor script for the CSDU-285C (CSDU285.SPT)
    c2-429.spt         - vendor script for the CSDU-429/423 (optional)
    fx2_firmware.bin   - 6936 bytes, CSDU-429 FX2 8051 code (from USB capture)
    fpga_bitstream.bin - 54908 bytes, CSDU-429 Xilinx FPGA bitstream

Requires: pyusb, libusb
"""

import sys
import os
import time
import usb.core
import usb.util
import usb.backend.libusb1

from collections import namedtuple
from dataclasses import dataclass

from utils import APP_DIR

from .sptt_spt import extract_firmware

VID = 0xDCDC
PID_RAW = 0xF429        # FX2 before firmware load
PID_CONFIGURED = 0xE429  # FX2 after firmware load

# FX2 firmware memory layout (from USB capture):
# Block 1: 4064 bytes at address 0x0000
# Block 2: 2872 bytes at address 0x0FE0
FX2_BLOCKS = [
    (0x0000, 4064),  # internal RAM + external
    (0x0FE0, 2872),  # continuation
]

FPGA_CHUNK_SIZE = 64


@dataclass(frozen=True)
class SpttModel:
    """One camera of the CSDU family.

    The command set, the status block and the 12-bit packing are shared: the vendor
    drives all of them through the one stt_cam.dll.
    """
    name: str
    pid_raw: int
    pid_configured: int
    # The vendor's firmware script, looked for in the firmware directory.
    spt_name: str
    # The sensor reads out two fields, even lines first; decode_frame has to
    # weave them back together at full resolution.
    interlaced: bool
    # The sensor has a Bayer colour filter, so a full-resolution frame is a
    # mosaic rather than a grey image.
    color: bool
    # Full-sensor frame size per binning code. Only for a log line when the
    # firmware reports something else; the firmware's figure is what is used.
    nominal_sizes: dict
    # Frames are taken one at a time by software start, as the vendor's demo does,
    # rather than free-running.
    single_shot: bool = False
    # The firmware obeys the binning command. Where it does not, the status block's
    # size fields hold the sensor's size and the frame is the ROI.
    has_binning: bool = True


# CSDU-423 is the same camera to this driver: the vendor installs it with the
# CSDU429 driver, the same USB IDs and the same C2-429.spt.
CSDU429 = SpttModel(
    name="CSDU-429", pid_raw=PID_RAW, pid_configured=PID_CONFIGURED,
    spt_name="c2-429.spt", interlaced=True, color=False,
    nominal_sizes={0: (744, 576), 1: (372, 288), 3: (186, 144)})

# ICX285AQ: progressive scan, RGB Bayer. IDs and script from the vendor's
# csdu285.inf / CSDU285.SPT. The firmware reads out 1392x1032 of the chip's
# 1040 lines — what the camera reported on site, and the ROI every vendor
# config and the vendor's own TIFFs use.
CSDU285C = SpttModel(
    name="CSDU-285C", pid_raw=0xD285, pid_configured=0xC285,
    spt_name="csdu285.spt", interlaced=False, color=True,
    nominal_sizes={0: (1392, 1032)}, single_shot=True, has_binning=False)

MODELS = (CSDU429, CSDU285C)


UDEV_RULES = "systemd/99-every-camera-sptt.rules"


def explain_usb_error(exc):
    """``exc`` as a message an operator can act on.

    Finding the camera needs no rights, so a run without them fails at the first
    transfer with a bare ``[Errno 13] Access denied``.
    """
    if getattr(exc, "errno", None) == 13 or "Access denied" in str(exc):
        return (f"No permission to use the camera's USB device ({exc}). "
                f"Install the udev rules so it works without root: "
                f"sudo cp {UDEV_RULES} /etc/udev/rules.d/ && "
                "sudo udevadm control --reload-rules && sudo udevadm trigger, "
                "then replug the camera.")
    return str(exc)


# ``port`` is the physical USB port, e.g. "1-2.3", as in /sys/bus/usb/devices. The
# firmware gives two cameras of one model no serial number to tell them apart, and the
# port survives the re-enumeration after firmware load, which a bus address does not.
CameraLocation = namedtuple("CameraLocation", "model port configured")


def usb_port(dev):
    ports = getattr(dev, "port_numbers", None)
    if ports:
        return f"{dev.bus}-" + ".".join(str(p) for p in ports)
    # A backend that cannot say: the bus address is unique for now, though it
    # changes on replug, so it can be shown but is no use in a config.
    return f"{dev.bus}:{dev.address}"


def find_device(backend, pid, port=None):
    match = None if port is None else (lambda dev: usb_port(dev) == port)
    return usb.core.find(idVendor=VID, idProduct=pid, backend=backend,
                         custom_match=match)


def list_cameras(backend):
    found = []
    for model in MODELS:
        for pid, configured in ((model.pid_configured, True),
                                (model.pid_raw, False)):
            devices = usb.core.find(find_all=True, idVendor=VID, idProduct=pid,
                                    backend=backend) or []
            for dev in devices:
                found.append(CameraLocation(model, usb_port(dev), configured))
                usb.util.dispose_resources(dev)
    return sorted(found, key=lambda cam: cam.port)


def describe_cameras(cameras):
    return "; ".join(f"{c.model.name} on USB port {c.port}"
                     f"{'' if c.configured else ' (no firmware yet)'}"
                     for c in cameras) or "none"


def select_camera(backend, port=None):
    """The camera this run is to drive, or LookupError saying why there is none.

    With several cameras ``port`` is required: taking "the first" would let two runs
    pick the same camera, or swap their archives after a reboot.
    """
    cameras = list_cameras(backend)
    if port:
        chosen = [c for c in cameras if c.port == port]
        if not chosen:
            raise LookupError(f"No SPTT camera on USB port {port}. "
                              f"Connected: {describe_cameras(cameras)}.")
        return chosen[0]
    if not cameras:
        raise LookupError("No SPTT camera found (looked for "
                          + ", ".join(m.name for m in MODELS) + ").")
    if len(cameras) > 1:
        raise LookupError(
            f"{len(cameras)} SPTT cameras connected: {describe_cameras(cameras)}. "
            "Set sptt.usb_port in this run's config to the one it should drive "
            "(python main.py --type sptt --probe lists them).")
    return cameras[0]


def find_libusb_backend():
    import platform
    system = platform.system()

    if system == "Linux":
        import ctypes.util
        path = ctypes.util.find_library("usb-1.0")
        candidates = ([path] if path else []) + [
            "/usr/lib/x86_64-linux-gnu/libusb-1.0.so",
            "/usr/lib64/libusb-1.0.so.0",
            "/usr/lib/libusb-1.0.so",
            "/usr/lib/aarch64-linux-gnu/libusb-1.0.so"]
        for p in candidates:
            # get_backend answers None, not an exception, for a library it cannot load.
            try:
                backend = usb.backend.libusb1.get_backend(
                    find_library=lambda x, path=p: path)
            except Exception:
                backend = None
            if backend is not None:
                return backend

    return usb.backend.libusb1.get_backend()


def firmware_path(firmware_dir=None):
    """Where the firmware files live: ``sptt.firmware_dir``, or ``firmware/`` in the
    program root.

    Anchored to :data:`utils.APP_DIR`, not to this file, which lives one level down in
    ``cameras/``.
    """
    if firmware_dir:
        return (firmware_dir if os.path.isabs(firmware_dir)
                else os.path.join(APP_DIR, firmware_dir))
    return os.path.join(APP_DIR, "firmware")


def load_firmware_files(firmware_dir=None, model=CSDU429):
    """Returns ``(fx2_blocks, fpga_data)``, ``fx2_blocks`` being ``(address, bytes)``
    pairs.

    The vendor's ``.spt`` script is used when present; the CSDU-429 can also load from
    its two blobs.
    """
    directory = firmware_path(firmware_dir)
    spt_path = os.path.join(directory, model.spt_name)

    if os.path.exists(spt_path):
        with open(spt_path, 'rb') as f:
            fx2_blocks, fpga_data = extract_firmware(f.read())
        print(f"  Firmware script: {spt_path}")
    elif model is CSDU429:
        fx2_blocks, fpga_data = _load_429_blobs(directory)
    else:
        raise FileNotFoundError(
            f"{model.name} firmware not found: {spt_path}. Copy the vendor's "
            f"{model.spt_name} there, or set sptt.firmware_dir in config.json "
            "if it lives elsewhere.")

    print(f"  FX2 firmware:    {sum(len(b) for _, b in fx2_blocks)} bytes")
    print(f"  FPGA bitstream:  {len(fpga_data)} bytes")
    return fx2_blocks, fpga_data


def _load_429_blobs(directory):
    fx2_path = os.path.join(directory, "fx2_firmware.bin")
    fpga_path = os.path.join(directory, "fpga_bitstream.bin")

    # Raised, not sys.exit: the GUI's connect button and the console worker must
    # report it, and their ``except Exception`` handlers would not catch SystemExit.
    missing = [p for p in (fx2_path, fpga_path) if not os.path.exists(p)]
    if missing:
        raise FileNotFoundError(
            "SPTT firmware not found: " + ", ".join(missing) +
            ". Set sptt.firmware_dir in config.json if the files live elsewhere.")

    with open(fx2_path, 'rb') as f:
        fx2_data = f.read()
    with open(fpga_path, 'rb') as f:
        fpga_data = f.read()

    expected_fx2 = sum(size for _, size in FX2_BLOCKS)
    if len(fx2_data) != expected_fx2:
        print(f"WARNING: FX2 firmware size {len(fx2_data)} != expected {expected_fx2}")

    print(f"  Firmware directory: {directory}")
    blocks = []
    offset = 0
    for addr, size in FX2_BLOCKS:
        blocks.append((addr, fx2_data[offset:offset + size]))
        offset += size
    return blocks, fpga_data


def detach_kernel_driver(dev):
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
            print("  Detached kernel driver")
    except (usb.core.USBError, NotImplementedError):
        pass


def load_fx2_firmware(dev, fx2_blocks):
    """
    Load FX2 8051 firmware using vendor request 0xA0.

    ``fx2_blocks`` is the ``(address, bytes)`` list load_firmware_files gives.

    Protocol (from USB capture, frames 339-345; CSDU-429 block sizes):
    1. Write 0x01 to CPUCS (0xE600) — assert reset
    2. Write 4064 bytes to 0x0000
    3. Write 2872 bytes to 0x0FE0
    4. Write 0x00 to CPUCS (0xE600) — release reset
    """
    print("Loading FX2 firmware...")

    print("  CPUCS = 0x01 (assert reset)")
    dev.ctrl_transfer(0x40, 0xA0, 0xE600, 0x0000, b'\x01')

    for addr, block in fx2_blocks:
        print(f"  Write {len(block)} bytes to 0x{addr:04X}")
        CHUNK = 4096
        for off in range(0, len(block), CHUNK):
            chunk = block[off:off + CHUNK]
            dev.ctrl_transfer(0x40, 0xA0, addr + off, 0x0000, chunk, timeout=5000)

    # Verify before releasing reset: the CPU is halted and RAM is readable.
    print("  Verifying firmware in RAM...")
    verify_ok = True
    for addr, expected in fx2_blocks:
        size = len(expected)
        readback = b''
        for off in range(0, size, 4096):
            rlen = min(4096, size - off)
            rb = dev.ctrl_transfer(0xC0, 0xA0, addr + off, 0x0000, rlen, timeout=5000)
            readback += bytes(rb)
        if readback != expected:
            print(f"  VERIFY FAILED at 0x{addr:04X}!")
            for j in range(min(len(expected), len(readback))):
                if expected[j] != readback[j]:
                    print(f"    First diff at offset {j}: wrote 0x{expected[j]:02x}, read 0x{readback[j]:02x}")
                    break
            verify_ok = False
        else:
            print(f"  Verified {size} bytes at 0x{addr:04X} OK")

    if not verify_ok:
        # Raised, not sys.exit: see _load_429_blobs.
        raise RuntimeError("FX2 firmware verification failed")

    # Release reset: the 8051 starts executing.
    print("  CPUCS = 0x00 (release reset)")
    dev.ctrl_transfer(0x40, 0xA0, 0xE600, 0x0000, b'\x00')

    print("FX2 firmware loaded. Device will re-enumerate.")


def wait_for_configured_device(backend, timeout=10.0, model=CSDU429, port=None):
    """``port`` keeps the wait on the camera being loaded, with several on the machine."""
    print("Waiting for FX2 firmware to initialize...")
    time.sleep(2)

    print("Searching for device", end='', flush=True)
    start = time.time()
    while time.time() - start < timeout:
        # After FX2 firmware loads, device may keep same PID or change
        for pid in [model.pid_configured, model.pid_raw]:
            dev = find_device(backend, pid, port)
            if dev:
                print(f" found {VID:04x}:{pid:04x} ({time.time() - start:.1f}s)")
                return dev
        time.sleep(0.5)
        print(".", end='', flush=True)

    print(" TIMEOUT!")
    return None


def load_fpga_bitstream(dev, fpga_data):
    """
    Load Xilinx FPGA bitstream via FX2 vendor requests.

    Protocol (from USB capture, frames 347-2063):
    1. 0xF0 OUT wValue=0, wIndex=0, wLength=0 — init FPGA programming
    2. 858x 0xF1 OUT wValue=0, wIndex=0, 64 bytes each — bitstream data
    """
    total = len(fpga_data)
    print(f"Loading FPGA bitstream ({total} bytes)...")

    print("  Sending F0 init command...")
    dev.ctrl_transfer(0x40, 0xF0, 0x0000, 0x0000, None, timeout=5000)

    # The FPGA may finish configuring and reset the device before all bytes are sent:
    # the trailing bytes are typically padding, so a disconnect in the last 10% is
    # success.
    loaded = 0
    for off in range(0, total, FPGA_CHUNK_SIZE):
        chunk = fpga_data[off:off + FPGA_CHUNK_SIZE]
        try:
            dev.ctrl_transfer(0x40, 0xF1, 0x0000, 0x0000, chunk, timeout=5000)
        except usb.core.USBError as e:
            loaded += len(chunk)
            pct = loaded * 100 // total
            if pct >= 90:
                print(f"\r  FPGA: device reset at {loaded}/{total} bytes ({pct}%) — FPGA configured")
                return
            else:
                raise RuntimeError(f"FPGA upload failed at {loaded}/{total} bytes ({pct}%): {e}") from e
        loaded += len(chunk)
        if loaded % 4096 < FPGA_CHUNK_SIZE:
            pct = loaded * 100 // total
            print(f"\r  FPGA: {loaded}/{total} bytes ({pct}%)", end='', flush=True)

    print(f"\r  FPGA: {loaded}/{total} bytes (100%)   ")
    print("FPGA bitstream loaded.")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="CSDU camera firmware loader")
    parser.add_argument("--list", action="store_true",
                        help="list the connected cameras and exit")
    parser.add_argument("--port", default=None,
                        help="USB port of the camera to load, e.g. 1-2.3 "
                             "(needed when several are connected)")
    args = parser.parse_args()

    print("CSDU Camera Firmware Loader")
    print("===========================")

    backend = find_libusb_backend()
    if args.list:
        for cam in list_cameras(backend):
            print(f"  {cam.port:<12} {cam.model.name:<10} "
                  f"{'configured' if cam.configured else 'no firmware'}")
        return
    try:
        cam = select_camera(backend, args.port)
    except LookupError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
    model, port = cam.model, cam.port
    print(f"Camera: {model.name} on USB port {port}")

    if cam.configured:
        print("Firmware is already loaded. Ready to use.")
        return

    print("Loading firmware files...")
    fx2_blocks, fpga_data = load_firmware_files(model=model)
    print()

    dev_raw = find_device(backend, model.pid_raw, port)
    print(f"Found raw FX2 device: {VID:04x}:{model.pid_raw:04x}")
    detach_kernel_driver(dev_raw)

    load_fx2_firmware(dev_raw, fx2_blocks)
    print()

    print("Sending USB bus reset...")
    try:
        dev_raw.reset()
    except usb.core.USBError as e:
        print(f"  USB reset returned: {e} (expected)")

    usb.util.dispose_resources(dev_raw)
    del dev_raw

    dev = wait_for_configured_device(backend, model=model, port=port)
    if not dev:
        print("ERROR: Device not found after firmware load!")
        sys.exit(1)

    detach_kernel_driver(dev)
    dev.set_configuration()

    print()
    load_fpga_bitstream(dev, fpga_data)

    # The device may have disconnected during the FPGA load.
    try:
        usb.util.dispose_resources(dev)
    except Exception:
        pass
    del dev

    # The device re-enumerates after FPGA configuration.
    print()
    dev_final = wait_for_configured_device(backend, timeout=10.0, model=model,
                                           port=port)
    if dev_final:
        print("Camera initialization complete!")
        print(f"Device ready at {VID:04x}:{model.pid_configured:04x}, port {port}")
    else:
        print("WARNING: Device not found after initialization.")
        print("  Check: sudo dmesg | tail -30")


if __name__ == '__main__':
    main()
