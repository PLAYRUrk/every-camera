#!/usr/bin/env python3
"""
Every Camera — Unified camera control for Canon (gphoto2) and SPTT (CSDU-429).

Usage:
    python main.py                         # Auto-detect: GUI if display, else error
    python main.py --type cannon           # Console mode, Canon camera
    python main.py --type sptt             # Console mode, SPTT camera
    python main.py --gui                   # GUI mode (all camera types)
    python main.py --gui --type cannon     # GUI mode, Canon only
    python main.py --gui --type sptt       # GUI mode, SPTT only
    python main.py --config path.json      # Use custom config file

Monitor is a separate program: python monitor_app.py
"""
import argparse
import sys
import os

# Ensure the script directory is in the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import can_use_gui


def main():
    parser = argparse.ArgumentParser(
        description="Every Camera — Unified Camera Controller",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Camera types:
  cannon    Canon DSLR cameras via gphoto2 (schedule-based capture)
  sptt      CSDU-429 scientific camera via USB (captures at :00 and :30, FITS output)
  infra     SW1300 SWIR camera (Tanho THCAMSW1300, schedule-based capture, TIFF/PNG)

In console mode (--type), the program runs headless.
With a display available and no --type flag, GUI mode starts automatically.
Monitor is a separate program: python monitor_app.py
        """,
    )
    parser.add_argument("--type", choices=["cannon", "sptt", "infra"],
                        help="Camera type (required for console mode)")
    parser.add_argument("--gui", action="store_true",
                        help="Force GUI mode")
    parser.add_argument("--config", default=None,
                        help="Path to config.json (default: config.json next to script)")

    args = parser.parse_args()

    # Determine mode
    if args.type and not args.gui:
        # Explicit console mode
        if args.type == "cannon":
            from cannon_driver import run_console_cannon
            run_console_cannon(args.config)
        elif args.type == "sptt":
            from sptt_driver import run_console_sptt
            run_console_sptt(args.config)
        elif args.type == "infra":
            from infra_driver import run_console_infra
            run_console_infra(args.config)
    elif args.gui or (can_use_gui() and not args.type):
        # GUI mode
        from gui_app import run_gui
        run_gui(args)
    else:
        # No display, no --type
        print("Error: No display available. Use --type <cannon|sptt|infra> for console mode.")
        print("       Or use --gui to force GUI mode (requires DISPLAY).")
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
