#!/usr/bin/env python3
"""
Every Camera — Unified camera control for Canon, SPTT, Infra, and Sentry cameras.

Usage:
    python main.py                         # Auto-detect: GUI if display, else error
    python main.py --type cannon           # Console mode, Canon DSLR
    python main.py --type sptt             # Console mode, CSDU-429 scientific camera
    python main.py --type infra            # Console mode, SW1300 SWIR camera
    python main.py --type sentry           # Console mode, Princeton/imagerd_rt camera
    python main.py --gui                   # GUI mode (all camera types)
    python main.py --gui --type cannon     # GUI mode, Canon only
    python main.py --config path.json      # Use custom config file

Monitor is a separate program: python monitor_app.py

Camera drivers live in the cameras/ package:
    cameras/cannon_driver.py  — Canon DSLR via gphoto2
    cameras/sptt_driver.py    — CSDU-429 via USB (FITS)
    cameras/infra_driver.py   — Tanho SW1300 SWIR (TIFF/PNG/FITS)
    cameras/sentry_driver.py  — Princeton CCD via imagerd_rt daemon (FITS)
"""
import argparse
import sys
import os

# Ensure the project root is in the path so all imports resolve correctly
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import can_use_gui


def main():
    parser = argparse.ArgumentParser(
        description="Every Camera — Unified Camera Controller",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Camera types:
  cannon    Canon DSLR cameras via gphoto2 (schedule-based capture, JPEG)
  sptt      CSDU-429 scientific camera via USB (captures at :00 and :30, FITS)
  infra     SW1300 SWIR camera (Tanho THCAMSW1300, schedule-based, TIFF/PNG/FITS)
  sentry    Princeton Instruments CCD via imagerd_rt daemon (FITS, autonomous schedule)

In console mode (--type), the program runs headless.
With a display available and no --type flag, GUI mode starts automatically.
Monitor is a separate program: python monitor_app.py
        """,
    )
    parser.add_argument("--type", choices=["cannon", "sptt", "infra", "sentry"],
                        help="Camera type (required for console mode)")
    parser.add_argument("--gui", action="store_true",
                        help="Force GUI mode")
    parser.add_argument("--config", default=None,
                        help="Path to config.json (default: config.json next to script)")
    parser.add_argument("--preview", action="store_true",
                        help="Preview mode: continuously update preview_{cam}.png/fits "
                             "at max FPS (console mode only)")

    args = parser.parse_args()

    # Determine mode
    if args.type and not args.gui:
        # Explicit console mode
        if args.type == "cannon":
            from cameras.cannon_driver import run_console_cannon
            run_console_cannon(args.config, preview=args.preview)
        elif args.type == "sptt":
            from cameras.sptt_driver import run_console_sptt
            run_console_sptt(args.config, preview=args.preview)
        elif args.type == "infra":
            from cameras.infra_driver import run_console_infra
            run_console_infra(args.config, preview=args.preview)
        elif args.type == "sentry":
            from cameras.sentry_driver import run_console_sentry
            run_console_sentry(args.config, preview=args.preview)
    elif args.gui or (can_use_gui() and not args.type):
        # GUI mode
        from gui_app import run_gui
        run_gui(args)
    else:
        # No display, no --type
        print("Error: No display available. Use --type <cannon|sptt|infra|sentry> for console mode.")
        print("       Or use --gui to force GUI mode (requires DISPLAY).")
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
