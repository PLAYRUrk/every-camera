# Compatibility shim — real code lives in cameras/cannon_driver.py
from cameras.cannon_driver import *  # noqa: F401, F403
from cameras.cannon_driver import (   # noqa: F401
    run_console_cannon, run_preview_cannon,
    release_camera_usb, detect_model, apply_camcfg,
    install_gphoto_log_filter,
    get_adjustable_params, get_camera_settings_info,
    capture_image, CannonWorkerConsole,
)
