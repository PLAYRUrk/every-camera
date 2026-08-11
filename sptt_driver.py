# Compatibility shim — real code lives in cameras/sptt_driver.py
from cameras.sptt_driver import *  # noqa: F401, F403
from cameras.sptt_driver import (   # noqa: F401
    run_console_sptt, run_preview_sptt,
    SpttCamera, SpttWorkerConsole, ensure_firmware_loaded,
    save_fits, frame_metadata, ENCODING_12BPP, ENCODING_8BPP,
    SPTT_CAPTURE_SECONDS, make_command, _usb_write_retry, CMD_FIFO_INIT,
    ExposureNotHonoured, exposure_to_us, derive_period_us, KEEP,
)
