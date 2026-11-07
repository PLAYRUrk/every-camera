#!/usr/bin/env python3
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import can_use_gui

CAMERA_TYPES = ("cannon", "sptt", "infra", "sentry", "asi", "japan")


def camera_type(value):
    """A camera type, optionally with a ``-label``.

    The systemd unit passes its instance name as the type, so every-camera@sptt-north
    arrives as "sptt-north". The label only tells services and their configs apart; the
    camera itself is picked by sptt.usb_port in that config.
    """
    base = value.split("-", 1)[0]
    if base not in CAMERA_TYPES:
        raise argparse.ArgumentTypeError(
            f"unknown camera type {value!r} (choose from {', '.join(CAMERA_TYPES)})")
    return base


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
    parser.add_argument("--type", type=camera_type, metavar="TYPE",
                        help="Camera type (required for console mode): "
                             + ", ".join(CAMERA_TYPES) + ". A label after a "
                             "dash (sptt-north) names one of several cameras "
                             "of a type — it is ignored here, and lets "
                             "every-camera@sptt-north be its own service")
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
                        help="ASI and Japan: open the camera, print what it "
                             "accepts for every parameter the driver sets, and "
                             "exit. Nothing is written to the camera (on Japan "
                             "this also traces the filter wheel, which moves it). "
                             "SPTT: list the connected cameras and their USB "
                             "ports, for sptt.usb_port")
    parser.add_argument("--verbose", action="store_true",
                        help="Console mode: plain timestamped log lines instead of "
                             "the live dashboard (also the default when the output "
                             "is not a terminal)")

    args = parser.parse_args()

    if args.probe and args.type not in ("asi", "japan", "sptt"):
        print("Error: --probe is only available for --type asi, japan and sptt.")
        sys.exit(1)

    if args.type and not args.gui:
        if args.type == "cannon":
            from cameras.cannon_driver import run_console_cannon
            run_console_cannon(args.config, preview=args.preview,
                               verbose=args.verbose,
                               setup_mode=args.setup)
        elif args.type == "sptt":
            if args.probe:
                from cameras.sptt_driver import run_probe_sptt
                run_probe_sptt(args.config)
                return
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
        from gui_app import run_gui
        run_gui(args)
    else:
        print("Error: No display available. "
              "Use --type <cannon|sptt|infra|sentry|asi|japan> for console mode.")
        print("       Or use --gui to force GUI mode (requires DISPLAY).")
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
