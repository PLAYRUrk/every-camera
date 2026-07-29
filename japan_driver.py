# Compatibility shim — real code lives in cameras/japan_driver.py
from cameras.japan_driver import *  # noqa: F401, F403
from cameras.japan_driver import (   # noqa: F401
    run_console_japan, run_preview_japan, run_probe_japan,
    JapanCamera, JapanWorkerConsole,
)
