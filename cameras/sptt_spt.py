"""
Reader for the vendor's Cypress ``.spt`` scripts (C2-429.spt, CSDU285.SPT).

The Windows driver brings a camera up by replaying one of these against the
bare FX2: each record is one vendor request. Reading the firmware out of the
script, rather than out of a USB capture, is what lets every model the vendor
ships a script for be loaded the same way.

Record layout (little-endian), as found in both scripts::

    0   "CSPT"
    4   u32  record length, header included
    8   u16  header length (0x20)
    15  u8   record kind: 0x44 = vendor OUT request, 0x65 = driver-side wait
    16  u8   bRequest   (0xA0 FX2 RAM write, 0xF0 FPGA init, 0xF1 FPGA data)
    18  u16  wValue     (RAM address for 0xA0)
    20  u16  wIndex
    hdr ...  payload

Checked against the blobs in ``firmware/``: extracting C2-429.spt gives
``fx2_firmware.bin`` and ``fpga_bitstream.bin`` byte for byte.
"""
import struct
from collections import namedtuple

MAGIC = b"CSPT"
KIND_VENDOR = 0x44
REQ_FX2_RAM = 0xA0
REQ_FPGA_DATA = 0xF1
# FX2 CPU control register: the script writes it to hold and release the 8051,
# which the loader does itself, so it is not part of the firmware image.
CPUCS = 0xE600

Record = namedtuple("Record", "kind request value index data")


def parse_spt(data):
    """Every record of a script, in order."""
    records = []
    pos = 0
    while pos < len(data):
        if data[pos:pos + 4] != MAGIC:
            raise ValueError(f"Not an SPT record at offset {pos}")
        length, header = struct.unpack_from("<IH", data, pos + 4)
        if length < header or pos + length > len(data):
            raise ValueError(f"Truncated SPT record at offset {pos}")
        kind, request = data[pos + 15], data[pos + 16]
        value, index = struct.unpack_from("<HH", data, pos + 18)
        records.append(Record(kind, request, value, index,
                              bytes(data[pos + header:pos + length])))
        pos += length
    return records


def extract_firmware(data):
    """The FX2 image and FPGA bitstream a script loads.

    Returns ``(fx2_blocks, fpga)``: ``fx2_blocks`` is a list of
    ``(address, bytes)`` in load order, ``fpga`` the bitstream as one block.
    """
    fx2_blocks = []
    fpga = []
    for rec in parse_spt(data):
        if rec.kind != KIND_VENDOR:
            continue
        if rec.request == REQ_FX2_RAM and rec.value != CPUCS:
            fx2_blocks.append((rec.value, rec.data))
        elif rec.request == REQ_FPGA_DATA:
            fpga.append(rec.data)
    if not fx2_blocks or not fpga:
        raise ValueError("SPT script holds no FX2 firmware or no FPGA bitstream")
    return fx2_blocks, b"".join(fpga)
