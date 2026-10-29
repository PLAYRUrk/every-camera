#!/usr/bin/env python3
"""
Firmware loader for CSDU cameras — 429/423 and 285C (Cypress FX2 + Xilinx FPGA).

Loads, for whichever model is on the bus:
1. FX2 8051 firmware via vendor request 0xA0
2. Xilinx FPGA bitstream via vendor request 0xF1

Usage:
    sudo python -m cameras.sptt_load_firmware

Firmware files (in the program's firmware/ directory):
    csdu285.spt        - vendor script for the CSDU-285C (CSDU285.SPT)
    c2-429.spt         - vendor script for the CSDU-429/423 (optional)
    fx2_firmware.bin   - 6936 bytes, CSDU-429 FX2 8051 code (from USB capture)
    fpga_bitstream.bin - 54908 bytes, CSDU-429 Xilinx FPGA bitstream

Requires: pyusb, libusb
    pip install pyusb
"""

import sys
import os
import time
import usb.core
import usb.util
import usb.backend.libusb1

from dataclasses import dataclass

from utils import APP_DIR

from .sptt_spt import extract_firmware

# Camera USB IDs
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
    """One camera of the CSDU family, as far as this program has to tell them apart.

    Everything else — the command set, the status block, the 12-bit packing —
    is shared: the vendor drives all of them through the one stt_cam.dll.
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


# CSDU-423 is the same camera to this driver: the vendor installs it with the
# CSDU429 driver, the same USB IDs and the same C2-429.spt.
CSDU429 = SpttModel(
    name="CSDU-429", pid_raw=PID_RAW, pid_configured=PID_CONFIGURED,
    spt_name="c2-429.spt", interlaced=True, color=False,
    nominal_sizes={0: (744, 576), 1: (372, 288), 3: (186, 144)})

# ICX285AQ: progressive scan, RGB Bayer, 1392x1040. IDs and script from the
# vendor's csdu285.inf / CSDU285.SPT.
CSDU285C = SpttModel(
    name="CSDU-285C", pid_raw=0xD285, pid_configured=0xC285,
    spt_name="csdu285.spt", interlaced=False, color=True,
    nominal_sizes={0: (1392, 1040), 1: (696, 520), 3: (348, 260)})

MODELS = (CSDU429, CSDU285C)


def detect_model(backend):
    """The model of the first SPTT camera on the bus, configured or not, or None."""
    for model in MODELS:
        for pid in (model.pid_configured, model.pid_raw):
            dev = usb.core.find(idVendor=VID, idProduct=pid, backend=backend)
            if dev:
                usb.util.dispose_resources(dev)
                return model
    return None


def find_libusb_backend():
    """Try to find libusb backend."""
    import platform
    system = platform.system()

    if system == "Linux":
        import ctypes.util
        path = ctypes.util.find_library("usb-1.0")
        if path:
            return usb.backend.libusb1.get_backend(find_library=lambda x: path)
        for p in ["/usr/lib/x86_64-linux-gnu/libusb-1.0.so",
                  "/usr/lib/libusb-1.0.so",
                  "/usr/lib/aarch64-linux-gnu/libusb-1.0.so"]:
            try:
                return usb.backend.libusb1.get_backend(find_library=lambda x, path=p: path)
            except Exception:
                continue

    return usb.backend.libusb1.get_backend()


def firmware_path(firmware_dir=None):
    """Where the two firmware blobs live.

    ``firmware_dir`` is the ``sptt.firmware_dir`` config key; empty means the
    ``firmware/`` directory shipped with the program. That default is anchored
    to :data:`utils.APP_DIR` — the program root — and not to this file, which
    lives one level down in ``cameras/``. Deriving it from ``__file__`` is what
    made the loader look for ``cameras/firmware/``, a directory that has never
    existed, so no firmware could be found and the camera never came up.
    """
    if firmware_dir:
        return (firmware_dir if os.path.isabs(firmware_dir)
                else os.path.join(APP_DIR, firmware_dir))
    return os.path.join(APP_DIR, "firmware")


def load_firmware_files(firmware_dir=None, model=CSDU429):
    """Read the FX2 firmware and the FPGA bitstream for ``model``.

    Returns ``(fx2_blocks, fpga_data)``, ``fx2_blocks`` being ``(address,
    bytes)`` pairs. The vendor's ``.spt`` script is used when it is in the
    directory; the CSDU-429 also still loads from the two blobs it has always
    shipped with.
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

    # Raised, not sys.exit: this is called from the GUI's connect button and
    # from the console worker, and both need to report it rather than take the
    # whole program down. SystemExit would not even be caught by their
    # ``except Exception`` handlers.
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
    """Detach kernel driver on Linux if attached."""
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

    # Step 1: Assert reset
    print("  CPUCS = 0x01 (assert reset)")
    dev.ctrl_transfer(0x40, 0xA0, 0xE600, 0x0000, b'\x01')

    # Step 2: Write firmware blocks
    for addr, block in fx2_blocks:
        print(f"  Write {len(block)} bytes to 0x{addr:04X}")
        CHUNK = 4096
        for off in range(0, len(block), CHUNK):
            chunk = block[off:off + CHUNK]
            dev.ctrl_transfer(0x40, 0xA0, addr + off, 0x0000, chunk, timeout=5000)

    # Step 3: Verify before releasing reset (CPU halted, RAM readable)
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
        print("ERROR: Firmware verification failed! Aborting.")
        sys.exit(1)

    # Step 4: Release reset — 8051 starts executing
    print("  CPUCS = 0x00 (release reset)")
    dev.ctrl_transfer(0x40, 0xA0, 0xE600, 0x0000, b'\x00')

    print("FX2 firmware loaded. Device will re-enumerate.")


def wait_for_configured_device(backend, timeout=10.0, model=CSDU429):
    """Wait for device to re-enumerate after FX2 firmware load."""
    print("Waiting for FX2 firmware to initialize...")
    time.sleep(2)

    print("Searching for device", end='', flush=True)
    start = time.time()
    while time.time() - start < timeout:
        # After FX2 firmware loads, device may keep same PID or change
        for pid in [model.pid_configured, model.pid_raw]:
            dev = usb.core.find(idVendor=VID, idProduct=pid, backend=backend)
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

    # Step 1: Init FPGA programming mode
    print("  Sending F0 init command...")
    dev.ctrl_transfer(0x40, 0xF0, 0x0000, 0x0000, None, timeout=5000)

    # Step 2: Send bitstream in 64-byte chunks
    # Note: The FPGA may finish configuration and reset the device before
    # all bytes are sent. This is normal — the trailing bytes are typically
    # bitstream padding. We treat a disconnect in the last 10% as success.
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
    print("CSDU Camera Firmware Loader")
    print("===========================")

    backend = find_libusb_backend()
    model = detect_model(backend)
    if model is None:
        print("ERROR: No camera found!")
        for m in MODELS:
            print(f"  {m.name}: raw {VID:04x}:{m.pid_raw:04x}, "
                  f"configured {VID:04x}:{m.pid_configured:04x}")
        print("  Make sure the camera is connected via USB.")
        sys.exit(1)
    print(f"Camera model: {model.name}")

    # Step 1: Check if device is already configured
    dev = usb.core.find(idVendor=VID, idProduct=model.pid_configured, backend=backend)
    if dev:
        print(f"Camera already configured (PID=0x{model.pid_configured:04x}).")
        print("Firmware is already loaded. Ready to use.")
        return

    # Load firmware files
    print("Loading firmware files...")
    fx2_blocks, fpga_data = load_firmware_files(model=model)
    print()

    # Step 2: Find raw (unconfigured) FX2 device
    dev_raw = usb.core.find(idVendor=VID, idProduct=model.pid_raw, backend=backend)
    print(f"Found raw FX2 device: {VID:04x}:{model.pid_raw:04x}")
    detach_kernel_driver(dev_raw)

    # Step 3: Load FX2 firmware
    load_fx2_firmware(dev_raw, fx2_blocks)
    print()

    # Step 4: USB bus reset and re-acquire device
    print("Sending USB bus reset...")
    try:
        dev_raw.reset()
    except usb.core.USBError as e:
        print(f"  USB reset returned: {e} (expected)")

    usb.util.dispose_resources(dev_raw)
    del dev_raw

    dev = wait_for_configured_device(backend, model=model)
    if not dev:
        print("ERROR: Device not found after firmware load!")
        sys.exit(1)

    detach_kernel_driver(dev)
    dev.set_configuration()

    # Step 5: Load FPGA bitstream
    print()
    load_fpga_bitstream(dev, fpga_data)

    # Cleanup — device may have disconnected during FPGA load
    try:
        usb.util.dispose_resources(dev)
    except Exception:
        pass
    del dev

    # Wait for device to re-enumerate after FPGA configuration
    print()
    dev_final = wait_for_configured_device(backend, timeout=10.0, model=model)
    if dev_final:
        print("Camera initialization complete!")
        print(f"Device ready at {VID:04x}:{model.pid_configured:04x}")
    else:
        print("WARNING: Device not found after initialization.")
        print("  Check: sudo dmesg | tail -30")


if __name__ == '__main__':
    main()
