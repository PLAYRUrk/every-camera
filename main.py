#!/usr/bin/env python3
"""
Every Camera — Unified camera control for Canon, SPTT, Infra, Sentry, ASI and Japan.

Usage:
    python main.py                         # Auto-detect: GUI if display, else error
    python main.py --type cannon           # Console mode, Canon DSLR
    python main.py --type sptt             # Console mode, CSDU-429 scientific camera
    python main.py --type infra            # Console mode, SW1300 SWIR camera
    python main.py --type sentry           # Console mode, Princeton/imagerd_rt camera
    python main.py --type asi              # Console mode, ASI all-sky imager (PICAM)
    python main.py --type japan            # Console mode, Japan all-sky imager (DCAM)
    python main.py --type asi --verbose    # Plain log lines instead of the dashboard
    python main.py --type asi --probe      # Print what the camera accepts, then exit
    python main.py --gui                   # GUI mode (all camera types)
    python main.py --gui --type cannon     # GUI mode, Canon only
    python main.py --config path.json      # Use custom config file

Console mode draws one live status screen that is redrawn in place (console_ui.py)
and writes a full log to ~/.every_camera/logs/. Use --verbose, or redirect the
output, to get plain timestamped lines instead.

Monitor is a separate program: python monitor_app.py

Camera drivers live in the cameras/ package:
    cameras/cannon_driver.py  — Canon DSLR via gphoto2
    cameras/sptt_driver.py    — CSDU-429 via USB (FITS)
    cameras/infra_driver.py   — Tanho SW1300 SWIR (TIFF/PNG/FITS)
    cameras/sentry_driver.py  — Princeton CCD via imagerd_rt daemon (FITS)
    cameras/asi_driver.py     — ASI all-sky imager: Princeton PIXIS via PICAM (FITS)
    cameras/japan_driver.py   — Japan all-sky imager: Hamamatsu via DCAM-API (FITS)

What asi and japan share — the filter wheel, the schedule arithmetic, the solar
altitude and the FITS core — lives in cameras/common/.
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
  asi       ASI all-sky imager: Princeton PIXIS via PICAM + filter wheel (FITS)
  japan     Japan all-sky imager: Hamamatsu via DCAM-API + filter wheel (FITS)

In console mode (--type), the program runs headless.
With a display available and no --type flag, GUI mode starts automatically.
Monitor is a separate program: python monitor_app.py

A camera whose schedule file is missing or empty starts in setup mode: it comes
up, serves live frames to focus_app.py, and archives nothing. --setup asks for
that mode explicitly, even when a schedule is configured.
        """,
    )
    parser.add_argument("--type",
                        choices=["cannon", "sptt", "infra", "sentry", "asi",
                                 "japan"],
                        help="Camera type (required for console mode)")
    parser.add_argument("--gui", action="store_true",
                        help="Force GUI mode")
    parser.add_argument("--config", default=None,
                        help="Path to config.json (default: config.json next to script)")
    parser.add_argument("--preview", action="store_true",
                        help="Preview mode: continuously update preview_{cam}.png/fits "
                             "at max FPS (console mode only)")
    parser.add_argument("--setup", action="store_true",
                        help="Setup mode: bring the camera up for focusing only "
                             "— no scheduled captures, nothing archived. Entered "
                             "automatically when no schedule is configured")
    parser.add_argument("--probe", action="store_true",
                        help="ASI and Japan only: open the camera, print what it "
                             "accepts for every parameter the driver sets, and "
                             "exit. Nothing is written to the camera (on Japan "
                             "this also traces the filter wheel, which moves it)")
    parser.add_argument("--verbose", action="store_true",
                        help="Console mode: plain timestamped log lines instead of "
                             "the live dashboard (also the default when the output "
                             "is not a terminal)")

    args = parser.parse_args()

    if args.probe and args.type not in ("asi", "japan"):
        print("Error: --probe is only available for --type asi and --type japan.")
        sys.exit(1)

    # Determine mode
    if args.type and not args.gui:
        # Explicit console mode
        if args.type == "cannon":
            from cameras.cannon_driver import run_console_cannon
            run_console_cannon(args.config, preview=args.preview,
                               verbose=args.verbose,
                               setup_mode=args.setup)
        elif args.type == "sptt":
            from cameras.sptt_driver import run_console_sptt
            run_console_sptt(args.config, preview=args.preview,
                             verbose=args.verbose,
                             setup_mode=args.setup)
        elif args.type == "infra":
            from cameras.infra_driver import run_console_infra
            run_console_infra(args.config, preview=args.preview,
                              verbose=args.verbose,
                              setup_mode=args.setup)
        elif args.type == "sentry":
            from cameras.sentry_driver import run_console_sentry
            run_console_sentry(args.config, preview=args.preview,
                               verbose=args.verbose,
                               setup_mode=args.setup)
        elif args.type == "asi":
            if args.probe:
                from cameras.asi_driver import run_probe_asi
                run_probe_asi(args.config)
                return
            from cameras.asi_driver import run_console_asi
            run_console_asi(args.config, preview=args.preview,
                            verbose=args.verbose,
                            setup_mode=args.setup)
        elif args.type == "japan":
            if args.probe:
                from cameras.japan_driver import run_probe_japan
                run_probe_japan(args.config)
                return
            from cameras.japan_driver import run_console_japan
            run_console_japan(args.config, preview=args.preview,
                              verbose=args.verbose,
                              setup_mode=args.setup)
    elif args.gui or (can_use_gui() and not args.type):
        # GUI mode
        from gui_app import run_gui
        run_gui(args)
    else:
        # No display, no --type
        print("Error: No display available. "
              "Use --type <cannon|sptt|infra|sentry|asi|japan> for console mode.")
        print("       Or use --gui to force GUI mode (requires DISPLAY).")
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
