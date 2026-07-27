"""
The measurement console: one live status screen, shared by every camera.

Console mode used to be a scrolling wall of ``print()`` calls, so the state of a
run had to be reconstructed by reading backwards. This module replaces it with a
single block that is redrawn in place — the model used by the ASI all-sky imager
program — generalised so that all drivers show the same screen.

Three properties matter, and the design follows from them:

* **It must not freeze during an exposure.** The renderer lives on its own
  daemon thread and reads a shared state dict, so the clock and the
  capture-elapsed counter keep ticking while the worker thread is blocked inside
  a 25-second ``capture()``. The renderer never touches a camera handle; the
  worker pushes values in with :meth:`Dashboard.update` at points where reading
  them is safe.
* **It must not be corrupted by stray output.** While a dashboard is drawing,
  ``sys.stdout``/``sys.stderr`` are wrapped: anything a library prints (gphoto2,
  ctypes wrappers, paho) lands in the log panel instead of on top of the screen.
  Driver code calls :func:`log` rather than ``print``.
* **It must degrade.** Under systemd, ``nohup`` or a pipe there is no terminal to
  redraw, so the dashboard switches to plain timestamped lines instead of filling
  the journal with escape sequences. ``--verbose`` forces the same mode. The full
  log always goes to a file either way.

Typical use in a driver::

    dash = console_ui.start_dashboard("ASI", instance_name, verbose=args.verbose)
    try:
        dash.update(status="running", output_dir=out)
        dash.set_section("camera", [("Exposure:", "25 s")])
        dash.capture_begin("LIGHT filter=3", 25.0)
        ...
    finally:
        dash.stop()

Modules that are not drivers just call ``console_ui.log(...)``; with no dashboard
running it prints the line, so the GUI and the standalone tools are unaffected.
"""
import contextlib
import os
import shutil
import sys
import threading
import time

from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CLEAR_SCREEN = "\033[H\033[J"          # cursor home + erase to end of screen
MIN_WIDTH = 64
MAX_WIDTH = 120
LABEL_WIDTH = 26
DEFAULT_INTERVAL = 0.3                  # seconds between redraws
DEFAULT_LOG_LINES = 8
LOG_RING = 200                          # lines kept in memory
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_DIR = str(Path.home() / ".every_camera" / "logs")

LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")


def _enable_ansi():
    """Enable ANSI/VT escape sequences on Windows consoles (no-op elsewhere)."""
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass


def _term_width():
    try:
        width = shutil.get_terminal_size((80, 24)).columns
    except Exception:
        width = 80
    return max(MIN_WIDTH, min(width - 1, MAX_WIDTH))


def _fmt_size_mb(mb):
    try:
        mb = float(mb)
    except (TypeError, ValueError):
        return None
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB free"
    return f"{mb:.0f} MB free"


# ---------------------------------------------------------------------------
# Log file
# ---------------------------------------------------------------------------
class _LogFile:
    """Tiny size-rotating log writer (one backup). Never raises."""

    def __init__(self, path, max_bytes=LOG_MAX_BYTES):
        self.path = path
        self.max_bytes = max_bytes
        self._fh = None
        self._lock = threading.Lock()
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8")
        except OSError:
            self._fh = None

    def write(self, line):
        if self._fh is None:
            return
        with self._lock:
            try:
                self._fh.write(line + "\n")
                self._fh.flush()
                if self._fh.tell() > self.max_bytes:
                    self._rotate()
            except (OSError, ValueError):
                pass

    def _rotate(self):
        try:
            self._fh.close()
        except Exception:
            pass
        try:
            os.replace(self.path, self.path + ".1")
        except OSError:
            pass
        try:
            self._fh = open(self.path, "a", encoding="utf-8")
        except OSError:
            self._fh = None

    def close(self):
        with self._lock:
            try:
                if self._fh:
                    self._fh.close()
            except Exception:
                pass
            self._fh = None


# ---------------------------------------------------------------------------
# stdout capture
# ---------------------------------------------------------------------------
class _StreamCapture:
    """File-like object that turns writes into dashboard log lines.

    Installed over ``sys.stdout``/``sys.stderr`` while a dashboard is drawing, so
    that a chatty C library cannot scribble over the status block. Partial writes
    are buffered until a newline arrives.
    """

    def __init__(self, dashboard, level="INFO"):
        self._dash = dashboard
        self._level = level
        self._buf = ""
        self._lock = threading.Lock()

    def write(self, text):
        if not text:
            return 0
        with self._lock:
            # A carriage return ends a line as surely as a newline does: progress
            # bars (``print(f"\r 50%", end="")``) would otherwise buffer their
            # whole run into one enormous line.
            self._buf += text.replace("\r\n", "\n").replace("\r", "\n")
            lines = self._buf.split("\n")
            self._buf = lines.pop()
        for line in lines:
            line = line.rstrip()
            if line:
                self._dash.log(line, self._level, _from_stream=True)
        return len(text)

    def flush(self):
        with self._lock:
            # rstrip only: leading spaces are part of the line, and stripping
            # them would break the progress-line matching in Dashboard.log.
            pending, self._buf = self._buf.rstrip(), ""
        if pending.strip():
            self._dash.log(pending, self._level, _from_stream=True)

    def isatty(self):
        return False

    def fileno(self):
        raise OSError("console_ui capture stream has no descriptor")


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
class Dashboard:
    """Live status screen for one measurement run.

    The renderer thread only ever *reads* the state dict under a lock; the worker
    thread writes into it. Nothing here talks to hardware, so a hung camera call
    cannot stop the screen from updating (and a slow terminal cannot stall a
    capture).
    """

    def __init__(self, title, instance="", *, interval=DEFAULT_INTERVAL,
                 log_lines=DEFAULT_LOG_LINES, log_file=None, plain=None,
                 footer="Ctrl+C — stop"):
        self.title = title
        self.instance = instance
        self._interval = float(interval)
        self._log_lines = int(log_lines)
        self._footer = footer

        if plain is None:
            plain = not _stdout_is_tty()
        self.plain = bool(plain)

        self._state = {"status": "starting"}
        self._sections = {}
        self._section_order = []
        self._log = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread = None
        self._out = sys.__stdout__
        self._saved_stdout = None
        self._saved_stderr = None
        self._logfile = _LogFile(log_file) if log_file else None
        self._last_status = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self):
        """Begin drawing (or, in plain mode, just start logging)."""
        set_dashboard(self)
        if self.plain:
            self._emit_plain(f"{self.title} — {self.instance}" if self.instance
                             else self.title, "INFO")
            return self
        _enable_ansi()
        self._saved_stdout, self._saved_stderr = sys.stdout, sys.stderr
        sys.stdout = _StreamCapture(self, "INFO")
        sys.stderr = _StreamCapture(self, "WARN")
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="everycam-console")
        self._thread.start()
        return self

    def stop(self):
        """Stop drawing, restore stdout and leave the cursor on a clean line."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._saved_stdout is not None:
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except Exception:
                pass
            sys.stdout, sys.stderr = self._saved_stdout, self._saved_stderr
            self._saved_stdout = self._saved_stderr = None
        if not self.plain:
            try:
                self._out.write("\n")
                self._out.flush()
            except Exception:
                pass
        if self._logfile:
            self._logfile.close()
        if get_dashboard() is self:
            set_dashboard(None)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False

    @contextlib.contextmanager
    def suspended(self):
        """Hand the terminal back for the duration of the block.

        Anything that talks to the operator — the setup wizard's prompts, a
        password question — needs a plain terminal: with the screen being
        redrawn three times a second, and stdout captured into the log panel,
        the questions would never be seen. Drawing resumes on exit.
        """
        if self.plain or self._thread is None:
            yield
            return
        self._paused.set()
        saved_out, saved_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = self._saved_stdout, self._saved_stderr
        try:
            self._out.write(CLEAR_SCREEN)
            self._out.flush()
        except Exception:
            pass
        try:
            yield
        finally:
            sys.stdout, sys.stderr = saved_out, saved_err
            self._paused.clear()

    # ------------------------------------------------------------------
    # Worker-side writes (cheap, non-blocking)
    # ------------------------------------------------------------------
    def update(self, **fields):
        """Merge fields into the displayed state.

        Recognised keys: ``status``, ``detail``, ``phase``, ``schedule``,
        ``next_at``, ``frames``, ``darks``, ``errors``, ``last_file``,
        ``output_dir``, ``disk_free_mb``, ``server_url``, ``node_name``,
        ``focus``, ``mqtt``, ``note``. Unknown keys are stored and ignored by
        the renderer, so a driver can stage values without breaking the layout.
        """
        with self._lock:
            self._state.update(fields)
            status = self._state.get("status")
        if self.plain and status is not None and status != self._last_status:
            self._last_status = status
            detail = fields.get("detail") or ""
            self._emit_plain(f"status: {status}{(' — ' + detail) if detail else ''}",
                             "INFO")

    def set_section(self, name, rows):
        """Replace a named block of ``(label, value)`` rows below the core ones."""
        with self._lock:
            if name not in self._sections:
                self._section_order.append(name)
            self._sections[name] = list(rows or [])

    def set_footer(self, text):
        with self._lock:
            self._footer = text or ""

    def note(self, text):
        """Show a one-line warning in the status block (empty string clears it)."""
        self.update(note=text)

    def capture_begin(self, label, exposure=0.0):
        """Mark the start of an exposure so the screen can show it elapsing."""
        self.update(capturing_since=time.monotonic(), capturing_label=label,
                    capturing_exposure=float(exposure or 0.0))

    def capture_end(self):
        self.update(capturing_since=None)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    def log(self, message, level="INFO", _from_stream=False):
        """Record a line: log panel + log file (plain mode prints it)."""
        level = (level or "INFO").upper()
        stamp = datetime.now().strftime("%H:%M:%S")
        text = str(message).rstrip()
        if not text:
            return
        _emit_to_sinks(text, level)
        if self._logfile:
            self._logfile.write(f"{datetime.now().isoformat(timespec='seconds')} "
                                f"[{level}] {text}")
        if self.plain:
            self._emit_plain(text, level, stamp=stamp, to_file=False)
            return
        with self._lock:
            # Successive progress updates ("FPGA: 40% ... 41% ...") replace each
            # other instead of flooding the panel and pushing real messages out.
            if (_from_stream and self._log
                    and self._log[-1][1] == level
                    and len(text) > 8 and text[:8] == self._log[-1][2][:8]):
                self._log[-1] = (stamp, level, text)
            else:
                self._log.append((stamp, level, text))
            if len(self._log) > LOG_RING:
                del self._log[:-LOG_RING]

    def _emit_plain(self, text, level="INFO", stamp=None, to_file=True):
        stamp = stamp or datetime.now().strftime("%H:%M:%S")
        line = f"{stamp} [{level}] {text}"
        if to_file and self._logfile:
            self._logfile.write(f"{datetime.now().isoformat(timespec='seconds')} "
                                f"[{level}] {text}")
        try:
            self._out.write(line + "\n")
            self._out.flush()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Renderer
    # ------------------------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            if self._paused.is_set():
                self._stop.wait(self._interval)
                continue
            try:
                self._draw()
            except Exception:
                pass            # a broken screen must never stop a measurement
            self._stop.wait(self._interval)

    def _snapshot(self):
        with self._lock:
            state = dict(self._state)
            sections = [(name, list(self._sections.get(name, [])))
                        for name in self._section_order]
            logs = self._log[-self._log_lines:] if self._log_lines else []
            footer = self._footer
        return state, sections, logs, footer

    def _core_rows(self, state):
        now = datetime.now()
        rows = [("Time:", now.strftime("%H:%M:%S"))]

        status = state.get("status", "-")
        detail = state.get("detail")
        rows.append(("Status:", f"{status}  ({detail})" if detail else str(status)))

        if state.get("phase"):
            rows.append(("Phase:", str(state["phase"])))
        if state.get("schedule"):
            rows.append(("Schedule:", str(state["schedule"])))

        next_at = state.get("next_at")
        if next_at is not None:
            if isinstance(next_at, datetime):
                remaining = max(0.0, (next_at - now).total_seconds())
                rows.append(("Next frame:",
                             f"{next_at.strftime('%H:%M:%S')}  (in {remaining:.1f} s)"))
            else:
                rows.append(("Next frame:", str(next_at)))

        since = state.get("capturing_since")
        if since is not None:
            elapsed = time.monotonic() - since
            exposure = state.get("capturing_exposure") or 0.0
            label = state.get("capturing_label", "")
            budget = f" / {exposure:.0f} s" if exposure else ""
            rows.append(("Capturing:", f"{label}  {elapsed:4.1f} s{budget}".strip()))

        frames = state.get("frames")
        if frames is not None:
            value = str(frames)
            if state.get("darks") is not None:
                value = f"{frames} light / {state['darks']} dark"
            errors = state.get("errors")
            if errors:
                value += f"        errors {errors}"
            rows.append(("Frames captured:", value))

        if state.get("last_file"):
            rows.append(("Last file:", str(state["last_file"])))

        out_dir = state.get("output_dir")
        if out_dir:
            free = _fmt_size_mb(state.get("disk_free_mb"))
            rows.append(("Output dir:", f"{out_dir}  ({free})" if free else str(out_dir)))
        return rows

    def _network_rows(self, state):
        rows = []
        if state.get("server_url"):
            value = str(state["server_url"])
            if state.get("node_name"):
                value += f"   name: {state['node_name']}"
            rows.append(("LAN server:", value))
        if state.get("focus"):
            rows.append(("Live view / focus:", str(state["focus"])))
        if state.get("mqtt"):
            rows.append(("MQTT:", str(state["mqtt"])))
        return rows

    def _draw(self):
        state, sections, logs, footer = self._snapshot()
        width = _term_width()
        value_width = max(16, width - LABEL_WIDTH - 4)

        def emit(label, value):
            text = str(value)
            if len(text) > value_width:
                text = "…" + text[-(value_width - 1):]
            out.append(f"  {label:<{LABEL_WIDTH}}{text}")

        header = f" {self.title}"
        if self.instance:
            header += f"  ({self.instance})"
        if state.get("node_name"):
            header = f" {self.title} @ {state['node_name']}"
            if self.instance:
                header += f"  ({self.instance})"

        out = [CLEAR_SCREEN, "=" * width, header, "=" * width]
        for label, value in self._core_rows(state):
            emit(label, value)

        for name, rows in sections:
            if not rows:
                continue
            out.append(f"  ---- {name} " + "-" * max(0, width - 10 - len(name)))
            for label, value in rows:
                emit(label, value)

        net_rows = self._network_rows(state)
        if net_rows:
            out.append("  ---- network " + "-" * max(0, width - 17))
            for label, value in net_rows:
                emit(label, value)

        if state.get("note"):
            emit("! note:", state["note"])

        if logs:
            out.append("-" * width)
            for stamp, level, text in logs:
                line = f"  {stamp} [{level:<5}] {text}"
                out.append(line[:width] if len(line) > width else line)

        out.append("=" * width)
        if footer:
            out.append(f"  {footer}")

        try:
            self._out.write("\n".join(out) + "\n")
            self._out.flush()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_active = None
_active_lock = threading.Lock()
# Extra destinations for log lines — the GUI registers one so a driver's
# messages reach its log pane without the driver knowing a GUI exists.
_sinks = []


def add_sink(callback):
    """Also deliver every log line to ``callback(message, level)``."""
    if callback not in _sinks:
        _sinks.append(callback)


def remove_sink(callback):
    try:
        _sinks.remove(callback)
    except ValueError:
        pass


def _emit_to_sinks(message, level):
    for callback in list(_sinks):
        try:
            callback(message, level)
        except Exception:
            pass        # a broken sink must never break a measurement


def _stdout_is_tty():
    try:
        return sys.__stdout__ is not None and sys.__stdout__.isatty()
    except Exception:
        return False


def set_dashboard(dashboard):
    global _active
    with _active_lock:
        _active = dashboard


def get_dashboard():
    return _active


def default_log_path(camera_type, instance_name=""):
    """Path of the rotating log file for one console run."""
    stem = f"{camera_type}_{instance_name}" if instance_name else str(camera_type)
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in stem)
    return os.path.join(LOG_DIR, f"{safe}.log")


def start_dashboard(camera_type, instance_name="", *, verbose=False,
                    title=None, footer="Ctrl+C — stop", log_file=None):
    """Create, register and start the dashboard for a console run.

    ``verbose`` (or a non-TTY stdout) selects plain line logging instead of the
    redrawn screen — the log file is written in both cases.
    """
    dash = Dashboard(
        title or f"EVERY CAMERA — {str(camera_type).upper()}",
        instance_name,
        plain=True if verbose else None,
        footer=footer,
        log_file=log_file or default_log_path(camera_type, instance_name),
    )
    return dash.start()


@contextlib.contextmanager
def suspended():
    """Give an interactive prompt the terminal; a no-op with no dashboard."""
    dash = _active
    if dash is None:
        yield
        return
    with dash.suspended():
        yield


def log(message, level="INFO"):
    """Log a line through the active dashboard, or print it if none is running."""
    dash = _active
    if dash is not None:
        dash.log(message, level)
        return
    _emit_to_sinks(message, level)
    prefix = f"[{level.upper()}] " if level else ""
    print(f"{prefix}{message}", flush=True)


def info(message):
    log(message, "INFO")


def warn(message):
    log(message, "WARN")


def error(message):
    log(message, "ERROR")


def debug(message):
    log(message, "DEBUG")


def update(**fields):
    """Update the active dashboard, if there is one."""
    dash = _active
    if dash is not None:
        dash.update(**fields)


def set_section(name, rows):
    dash = _active
    if dash is not None:
        dash.set_section(name, rows)


def note(text):
    dash = _active
    if dash is not None:
        dash.note(text)


def capture_begin(label, exposure=0.0):
    dash = _active
    if dash is not None:
        dash.capture_begin(label, exposure)


def capture_end():
    dash = _active
    if dash is not None:
        dash.capture_end()
