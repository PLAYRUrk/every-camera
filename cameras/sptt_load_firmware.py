#!/usr/bin/env python3
"""
Firmware loader for CSDU-429 camera (Cypress FX2 + Xilinx FPGA).

Loads clean firmware extracted from USB capture (no .spt file needed):
1. FX2 8051 firmware via vendor request 0xA0
2. Xilinx FPGA bitstream via vendor request 0xF1

Usage:
    sudo python load_firmware.py

Firmware files (in firmware/ directory relative to this script):
    fx2_firmware.bin   - 6936 bytes, FX2 8051 code
    fpga_bitstream.bin - 54908 bytes, Xilinx FPGA bitstream

Requires: pyusb, libusb
    pip install pyusb
"""

import sys
import os
import time
import usb.core
import usb.util
import usb.backend.libusb1

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


def load_firmware_files(script_dir=None):
    """Load firmware binary files from firmware/ directory."""
    if script_dir is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
    firmware_dir = os.path.join(script_dir, "firmware")

    fx2_path = os.path.join(firmware_dir, "fx2_firmware.bin")
    fpga_path = os.path.join(firmware_dir, "fpga_bitstream.bin")

    if not os.path.exists(fx2_path):
        print(f"ERROR: FX2 firmware not found: {fx2_path}")
        sys.exit(1)
    if not os.path.exists(fpga_path):
        print(f"ERROR: FPGA bitstream not found: {fpga_path}")
        sys.exit(1)

    with open(fx2_path, 'rb') as f:
        fx2_data = f.read()
    with open(fpga_path, 'rb') as f:
        fpga_data = f.read()

    expected_fx2 = sum(size for _, size in FX2_BLOCKS)
    if len(fx2_data) != expected_fx2:
        print(f"WARNING: FX2 firmware size {len(fx2_data)} != expected {expected_fx2}")

    print(f"  FX2 firmware:    {len(fx2_data)} bytes")
    print(f"  FPGA bitstream:  {len(fpga_data)} bytes")

    return fx2_data, fpga_data


def detach_kernel_driver(dev):
    """Detach kernel driver on Linux if attached."""
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
            print("  Detached kernel driver")
    except (usb.core.USBError, NotImplementedError):
        pass


def load_fx2_firmware(dev, fx2_data):
    """
    Load FX2 8051 firmware using vendor request 0xA0.

    Protocol (from USB capture, frames 339-345):
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
    offset = 0
    for addr, size in FX2_BLOCKS:
        block = fx2_data[offset:offset + size]
        print(f"  Write {len(block)} bytes to 0x{addr:04X}")
        CHUNK = 4096
        for off in range(0, len(block), CHUNK):
            chunk = block[off:off + CHUNK]
            dev.ctrl_transfer(0x40, 0xA0, addr + off, 0x0000, chunk, timeout=5000)
        offset += size

    # Step 3: Verify before releasing reset (CPU halted, RAM readable)
    print("  Verifying firmware in RAM...")
    offset = 0
    verify_ok = True
    for addr, size in FX2_BLOCKS:
        expected = fx2_data[offset:offset + size]
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
        offset += size

    if not verify_ok:
        print("ERROR: Firmware verification failed! Aborting.")
        sys.exit(1)

    # Step 4: Release reset — 8051 starts executing
    print("  CPUCS = 0x00 (release reset)")
    dev.ctrl_transfer(0x40, 0xA0, 0xE600, 0x0000, b'\x00')

    print("FX2 firmware loaded. Device will re-enumerate.")


def wait_for_configured_device(backend, timeout=10.0):
    """Wait for device to re-enumerate after FX2 firmware load."""
    print("Waiting for FX2 firmware to initialize...")
    time.sleep(2)

    print("Searching for device", end='', flush=True)
    start = time.time()
    while time.time() - start < timeout:
        # After FX2 firmware loads, device may keep same PID or change
        for pid in [PID_CONFIGURED, PID_RAW]:
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
    print("CSDU-429 Camera Firmware Loader")
    print("================================")

    # Load firmware files
    print("Loading firmware files...")
    fx2_data, fpga_data = load_firmware_files()
    print()

    backend = find_libusb_backend()

    # Step 1: Check if device is already configured
    dev = usb.core.find(idVendor=VID, idProduct=PID_CONFIGURED, backend=backend)
    if dev:
        print(f"Camera already configured (PID=0x{PID_CONFIGURED:04x}).")
        print("Firmware is already loaded. Ready to use.")
        return

    # Step 2: Find raw (unconfigured) FX2 device
    dev_raw = usb.core.find(idVendor=VID, idProduct=PID_RAW, backend=backend)
    if not dev_raw:
        print("ERROR: No camera found!")
        print(f"  Expected raw device:  {VID:04x}:{PID_RAW:04x}")
        print(f"  Expected configured:  {VID:04x}:{PID_CONFIGURED:04x}")
        print("  Make sure the camera is connected via USB.")
        sys.exit(1)

    print(f"Found raw FX2 device: {VID:04x}:{PID_RAW:04x}")
    detach_kernel_driver(dev_raw)

    # Step 3: Load FX2 firmware
    load_fx2_firmware(dev_raw, fx2_data)
    print()

    # Step 4: USB bus reset and re-acquire device
    print("Sending USB bus reset...")
    try:
        dev_raw.reset()
    except usb.core.USBError as e:
        print(f"  USB reset returned: {e} (expected)")

    usb.util.dispose_resources(dev_raw)
    del dev_raw

    dev = wait_for_configured_device(backend)
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
    dev_final = wait_for_configured_device(backend, timeout=10.0)
    if dev_final:
        print("Camera initialization complete!")
        print(f"Device ready at {VID:04x}:{PID_CONFIGURED:04x}")
    else:
        dev_raw2 = usb.core.find(idVendor=VID, idProduct=PID_RAW, backend=backend)
        if dev_raw2:
            print("Done. Device at PID=0xF429.")
        else:
            print("WARNING: Device not found after initialization.")
            print("  Check: sudo dmesg | tail -30")


if __name__ == '__main__':
    main()
