"""Decides which log lines deserve a letter; hangs off ``console_ui.add_sink``.

The sink runs on the logging thread, usually the one taking the frame, so letters
only go into the spool and ``sentinel.py`` sends them.
Each rule sends once, then counts repeats for ``cooldown_minutes``: a fault repeating
all night costs one letter, not a thousand and the daily allowance.
A rule must not log: it would re-enter the sink.
"""
import faulthandler
import json
import os
import re
import socket
import sys
import threading
import time
import traceback

from datetime import datetime as dt
from pathlib import Path

import console_ui
import mailer

_TICK_SECONDS = 30.0

# Both keyed by the status file stem the sentinel tracks: "{pid}" for a console
# worker, "{pid}_{camera}" for one GUI tab.
_MARKER_HOME = Path.home() / ".every_camera"
EXIT_DIR = str(_MARKER_HOME / "exits")
CRASH_DIR = str(_MARKER_HOME / "crashes")


class Rule:
    """Immediate rules get a letter on first match; the rest wait for the digest."""

    def __init__(self, kind, pattern, headline, immediate=True, levels=("ERROR",)):
        self.kind = kind
        self.pattern = re.compile(pattern, re.IGNORECASE)
        self.headline = headline
        self.immediate = immediate
        self.levels = levels

    def matches(self, message, level):
        return level in self.levels and self.pattern.search(message) is not None


# First match wins, so the specific rules come first.
RULES = (
    Rule("capture-stopped",
         r"consecutive capture failures",
         "камера остановила съёмку"),
    Rule("filter-controller-silent",
         # The controller says "no answer" and "written off" about itself; the failed
         # rebuild is reported by the wheel. Same fault, same letter.
         r"Filter (controller|wheel).*(no answer|written off|still says nothing)",
         "контроллер фильтров не отвечает"),
    Rule("filter-wheel-lost",
         r"Filter wheel would not reach position",
         "колесо фильтров не встало в нужное положение"),
    Rule("exposure-not-honoured",
         r"ExposureNotHonoured|exposure was not honoured",
         "камера не выдержала заданную экспозицию"),
    Rule("camera-unavailable",
         r"Failed to (connect|open) camera",
         "камера не открылась"),
    Rule("cooling-timeout",
         r"(Cooling|Warm-up).*timed out",
         "сенсор не вышел на температуру"),
    Rule("closing-darks-failed",
         r"Closing dark frames failed",
         "закрывающие тёмные кадры не сняты"),
    Rule("measurement-loop-failed",
         r"Measurement loop failed",
         "измерительный цикл прерван ошибкой"),
    # Catch-all: worth knowing by morning, not worth a letter at three at night.
    Rule("error", r".", "ошибка", immediate=False),
)


def _log_tail(path, lines):
    """Reads from the end: the file can reach 2 MB and only its last page is quoted."""
    if not path or not os.path.exists(path):
        return "(лог недоступен)"
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            block = min(size, max(4096, lines * 200))
            handle.seek(size - block)
            text = handle.read().decode("utf-8", errors="replace")
    except OSError as exc:
        return f"(лог не прочитан: {exc})"
    return "\n".join(text.splitlines()[-lines:])


def _host_address():
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(0.3)
        probe.connect(("8.8.8.8", 80))
        address = probe.getsockname()[0]
        probe.close()
        return address
    except OSError:
        return "?"


class AlertEngine:
    def __init__(self, cfg, node_name, instance_name, camera_type,
                 log_path=None, status_provider=None, spool=None):
        cfg = cfg or {}
        self.node_name = node_name or socket.gethostname()
        self.instance_name = instance_name or "?"
        self.camera_type = camera_type or "?"
        self.log_path = log_path
        self.status_provider = status_provider
        self.spool = spool if spool is not None else mailer.Spool()
        self.recipients = [a for a in (cfg.get("to") or []) if a]
        self.cooldown = float(cfg.get("cooldown_minutes", 60)) * 60.0
        self.digest_every = float(cfg.get("digest_minutes", 30)) * 60.0
        self.log_tail_lines = int(cfg.get("log_tail_lines", 50))
        self.disk_free_min_mb = int(cfg.get("disk_free_min_mb", 0) or 0)

        self._lock = threading.Lock()
        self._sent_at = {}          # kind -> when its last letter went out
        self._suppressed = {}       # kind -> repeats folded into the digest
        self._digest = []           # (time, level, message) awaiting a letter
        self._digest_due = time.monotonic() + self.digest_every
        self._disk_warned = False
        # Set while handling a rule, so anything logged from here is not handled again.
        self._busy = threading.local()
        self._stop = threading.Event()
        self._thread = None
        self.started_at = time.time()

    def start(self):
        console_ui.add_sink(self._on_line)
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="everycam-alerts")
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        console_ui.remove_sink(self._on_line)
        self.flush_digest()

    def _run(self):
        while not self._stop.wait(_TICK_SECONDS):
            try:
                self.tick()
            except Exception:
                # Must not take the process down, and must not log: that re-enters the
                # sink.
                pass

    def tick(self):
        if time.monotonic() >= self._digest_due:
            self.flush_digest()
        self._check_disk()

    def _on_line(self, message, level):
        if getattr(self._busy, "flag", False):
            return
        self._busy.flag = True
        try:
            self._classify(message, level)
        except Exception:
            pass
        finally:
            self._busy.flag = False

    def _classify(self, message, level):
        for rule in RULES:
            if not rule.matches(message, level):
                continue
            if rule.immediate:
                self._raise(rule, message)
            else:
                with self._lock:
                    self._digest.append((dt.now(), level, message))
            return

    def _raise(self, rule, message):
        now = time.monotonic()
        with self._lock:
            last = self._sent_at.get(rule.kind)
            if last is not None and now - last < self.cooldown:
                self._suppressed[rule.kind] = self._suppressed.get(rule.kind, 0) + 1
                return
            self._sent_at[rule.kind] = now
        self.queue(f"{self.instance_name} на {self.node_name} — {rule.headline}",
                   self._compose(rule.headline, message), kind=rule.kind)

    def queue(self, subject, body, kind=""):
        return self.spool.put(f"[every-camera] {subject}", body,
                              to=self.recipients, kind=kind) is not None

    def _header(self):
        uptime = time.time() - self.started_at
        rows = [
            ("Узел", self.node_name),
            ("Экземпляр", self.instance_name),
            ("Камера", self.camera_type),
            ("Машина", f"{socket.gethostname()} ({_host_address()})"),
            ("Процесс", str(os.getpid())),
            ("Время", dt.now().isoformat(timespec="seconds")),
            ("Работает", f"{uptime / 3600:.1f} ч"),
        ]
        return "\n".join(f"{name:<12} {value}" for name, value in rows)

    def _status_block(self):
        if self.status_provider is None:
            return ""
        try:
            status = self.status_provider() or {}
        except Exception:
            return ""
        interesting = ("status", "shots_taken", "errors", "last_shot",
                       "ccd_temp", "setpoint", "locked", "disk_free_mb",
                       "filter", "exposure_s", "setup_mode")
        rows = [f"{key:<14} {status[key]}"
                for key in interesting if status.get(key) is not None]
        return "\n".join(rows)

    def _compose(self, headline, message, extra=""):
        parts = [
            f"{self.instance_name} на {self.node_name}: {headline}.",
            "",
            self._header(),
            "",
            "Сообщение:",
            message,
        ]
        if extra:
            parts += ["", extra]
        status = self._status_block()
        if status:
            parts += ["", "Состояние:", status]
        parts += ["", f"Последние {self.log_tail_lines} строк лога:",
                  _log_tail(self.log_path, self.log_tail_lines)]
        return "\n".join(parts)

    def flush_digest(self):
        with self._lock:
            entries = self._digest
            suppressed = self._suppressed
            self._digest = []
            self._suppressed = {}
            self._digest_due = time.monotonic() + self.digest_every
        if not entries and not suppressed:
            return False

        lines = [f"{when.strftime('%H:%M:%S')} [{level}] {text}"
                 for when, level, text in entries]
        if suppressed:
            lines.append("")
            lines.append("Повторы, о которых письма не отправлялись:")
            lines += [f"  {kind}: ещё {count} раз(а)"
                      for kind, count in sorted(suppressed.items())]
        count = len(entries)
        return self.queue(
            f"{self.instance_name} на {self.node_name} — сводка ошибок ({count})",
            self._compose("накопившиеся ошибки", "\n".join(lines)),
            kind="digest")

    def _check_disk(self):
        if self.disk_free_min_mb <= 0 or self.status_provider is None:
            return
        try:
            free = (self.status_provider() or {}).get("disk_free_mb")
        except Exception:
            return
        if free is None:
            return
        if free >= self.disk_free_min_mb:
            # One letter per drop below the threshold; rearmed once there is room again.
            self._disk_warned = False
            return
        if self._disk_warned:
            return
        self._disk_warned = True
        self.queue(
            f"{self.instance_name} на {self.node_name} — заканчивается место",
            self._compose("на диске заканчивается место",
                          f"Свободно {free} МБ, порог {self.disk_free_min_mb} МБ."),
            kind="disk-low")


# A dead PID tells the sentinel that a worker died, not why. The crash marker says
# why; the clean-exit marker is what tells an orderly stop from a crash.
def marker_key(status_path=None):
    """Status file stem, so two cameras in one GUI process are two workers."""
    if status_path:
        return os.path.splitext(os.path.basename(str(status_path)))[0]
    return str(os.getpid())


def _write_marker(directory, key, payload):
    try:
        mailer._write_json_atomic(os.path.join(directory, f"{key}.json"), payload)
        return True
    except OSError:
        return False


def take_marker(directory, key):
    path = os.path.join(directory, f"{key}.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    try:
        os.remove(path)
    except OSError:
        pass
    return data if isinstance(data, dict) else {}


def note_clean_exit(status_path=None, reason=""):
    """Without this an orderly stop reaches the sentinel as a vanished process: a crash.
    """
    key = marker_key(status_path)
    _write_marker(EXIT_DIR, key, {
        "pid": os.getpid(),
        "reason": reason or "stopped normally",
        "at": dt.now().isoformat(timespec="seconds"),
    })


def note_crash(status_path=None, reason="", detail=""):
    key = marker_key(status_path)
    _write_marker(CRASH_DIR, key, {
        "pid": os.getpid(),
        "reason": reason,
        "traceback": detail,
        "at": dt.now().isoformat(timespec="seconds"),
    })


def take_crash(key):
    """Joins the exception hook's JSON report and the ``faulthandler`` dump.

    The dump is the only trace a PICAM or DCAM segfault leaves.
    """
    parts = []
    report = take_marker(CRASH_DIR, key) or {}
    if report.get("reason"):
        parts.append(str(report["reason"]))
    if report.get("traceback"):
        parts.append(str(report["traceback"]))
    fault_path = os.path.join(CRASH_DIR, f"{key}.fault")
    try:
        text = open(fault_path, encoding="utf-8", errors="replace").read().strip()
    except OSError:
        text = ""
    if text:
        parts.append("Аварийный дамп интерпретатора:\n" + text)
    try:
        os.remove(fault_path)
    except OSError:
        pass
    return "\n\n".join(parts)


def install_crash_handler(status_path=None):
    """Installs an exception hook and, for segfaults in ctypes drivers,
    ``faulthandler``.
    """
    key = marker_key(status_path)
    installed = False

    previous = sys.excepthook

    def hook(exc_type, exc, tb):
        try:
            note_crash(status_path,
                       reason=f"{exc_type.__name__}: {exc}",
                       detail="".join(traceback.format_exception(exc_type, exc, tb)))
        except Exception:
            pass
        previous(exc_type, exc, tb)

    sys.excepthook = hook
    installed = True

    try:
        os.makedirs(CRASH_DIR, exist_ok=True)
        # Kept open for the life of the process: by then no file can be opened any more.
        handle = open(os.path.join(CRASH_DIR, f"{key}.fault"), "w",
                      encoding="utf-8")
        faulthandler.enable(file=handle)
        globals().setdefault("_fault_files", []).append(handle)
    except (OSError, RuntimeError, ValueError):
        pass

    return installed


_engine = None


def start(cfg, node_name, instance_name, camera_type, log_path=None,
          status_provider=None, status_path=None):
    """Returns the engine, or None if disabled.

    No recipients means a station not set up yet: one log line, not a failure to
    measure.
    """
    global _engine
    cfg = cfg or {}
    if not cfg.get("enabled", True):
        return None
    recipients = [a for a in (cfg.get("to") or []) if a]
    if not recipients:
        console_ui.warn("Alerts: no recipients configured (alerts.to in "
                        "config.json) — nothing will be sent")
        return None
    account = mailer.resolve_account(cfg)
    if account is None:
        console_ui.warn(f"Alerts: {mailer.account_summary(None)} — letters will "
                        f"be queued but cannot be sent")
    else:
        console_ui.log(f"Alerts: {mailer.account_summary(account)} "
                       f"→ {', '.join(recipients)}")
    if status_provider is None and status_path:
        # The status file already holds what a letter quotes, exactly as the monitor
        # shows it, and saves threading a callback through six drivers.
        status_provider = lambda: _read_status(status_path)      # noqa: E731

    stop()
    _engine = AlertEngine(cfg, node_name, instance_name, camera_type,
                          log_path=log_path,
                          status_provider=status_provider).start()
    return _engine


def _read_status(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def stop():
    global _engine
    if _engine is not None:
        try:
            _engine.stop()
        except Exception:
            pass
        _engine = None


def engine():
    return _engine
