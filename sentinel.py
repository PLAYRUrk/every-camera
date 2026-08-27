#!/usr/bin/env python3
"""The watchdog that outlives the camera process.

Everything else in the alert system runs inside the program that is measuring,
which means none of it can report the one failure that matters most: the
program not being there any more. A segfault in a ctypes driver, an OOM kill, a
machine that wedged — in every one of those the worker has no chance to say
anything, and the night ends silently. So this runs beside it, as its own
service, doing four things and nothing else:

1. **Sends the spool.** It is the only thing in the program that opens a socket
   to an SMTP server. A worker queues a letter by writing a file — microseconds,
   no network, cannot fail in a way that stalls a frame — and this picks it up.
   That is what makes the promise: a letter queued a second before the process
   died still goes out, because a crash cannot un-write a file.

2. **Notices a worker that died.** A status file whose PID is gone, with no
   clean-exit marker beside it, is a process that did not get to shut down.

3. **Notices a worker that stopped working.** Alive, but its status has not
   moved in longer than it should have.

4. **Watches the disk**, independently of any worker, because a full disk is
   exactly the condition in which a worker stops being able to say so.

5. **Sends a status summary**, if ``alerts.status_mail_minutes`` asks for one.
   This is the part that replaces what a broker's retained status topic did for
   somebody outside the network: they cannot reach in, so the station reports
   out. One letter, every camera on the machine, in a table — readable in any
   mail client, no broker and no inbound port involved.

It is deliberately small and stdlib-only: no numpy, no PyQt, no camera driver.
It has to be the thing that is still running when everything else is not.

Usage:
    python sentinel.py                  # run until stopped
    python sentinel.py --once           # one pass, for testing
    python sentinel.py --interval 30
"""
import argparse
import json
import os
import socket
import sys
import time

from datetime import datetime as dt
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import alerts                                                    # noqa: E402
import console_ui                                                # noqa: E402
import mailer                                                    # noqa: E402

from utils import (                                              # noqa: E402
    HOME_STATUS_DIR, get_node_name, load_config, pid_alive,
)

STATE_FILE = str(Path.home() / ".every_camera" / "sentinel.json")
# Where the services keep their configs (systemd/install.sh). The sentinel
# is one per machine while the cameras are one per type, so it has no config
# of its own to be pointed at — it reads theirs. That is deliberate: the
# addresses to notify are a fact about the station, and asking for them
# twice is asking for two answers.
SERVICE_CONFIG_DIR = "/etc/every-camera"
DEFAULT_INTERVAL = 15.0
# A worker publishes its status at least every STATUS_MIN_INTERVAL (5 s) and
# the monitor calls it stale at 30. This is deliberately far looser than
# either: a tile going orange is a glance, a letter is somebody's evening.
HANG_SECONDS = 600.0


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _boot_id():
    """Something that changes when the machine reboots, or None if it cannot tell.

    Without this, the first pass after a reboot would find every PID it knew
    about gone and report each one as a crash. A reboot is one event, not six
    dead cameras, and the letters about it would be six lies.
    """
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        pass
    try:
        return str(int(time.time() - time.monotonic()))
    except Exception:
        return None


def find_config(explicit=None):
    """The configuration this machine's alerts are described by.

    Tried in order: what was asked for, the checkout's own config.json (the
    normal case when the program is run by hand), then each service config
    in /etc/every-camera. The first one that names somebody to notify wins;
    if none does, the last one read is still returned, so that the sentinel
    starts and says what is missing rather than refusing to run.
    """
    candidates = []
    if explicit:
        candidates.append(explicit)
    candidates.append(None)          # load_config's own default
    try:
        candidates += [os.path.join(SERVICE_CONFIG_DIR, name)
                       for name in sorted(os.listdir(SERVICE_CONFIG_DIR))
                       if name.endswith(".json")]
    except OSError:
        pass

    last = {}
    for candidate in candidates:
        try:
            cfg = load_config(candidate)
        except Exception:
            continue
        last = cfg
        if [a for a in ((cfg.get("alerts") or {}).get("to") or []) if a]:
            return cfg
    return last


# ---------------------------------------------------------------------------
# Status files
# ---------------------------------------------------------------------------
def scan_status_dir(status_dir):
    """Every live status file, keyed by PID string.

    Console workers write ``{pid}.json``; the GUI, which can run several cameras
    in one process, writes ``{pid}_{camera}.json``. Both are keyed here by the
    file's stem, so two cameras in one process stay two entries.
    """
    found = {}
    try:
        names = os.listdir(status_dir)
    except OSError:
        return found
    for name in sorted(names):
        stem, ext = os.path.splitext(name)
        if ext != ".json":
            continue
        pid_part = stem.split("_", 1)[0]
        if not pid_part.isdigit():
            continue
        record = _read_json(os.path.join(status_dir, name))
        if record is None:
            continue
        found[stem] = {"pid": int(pid_part), "status": record}
    return found


def _age_of(record):
    """Seconds since the worker last published, or None if it never said."""
    stamp = record.get("last_update")
    if not stamp:
        return None
    try:
        return (dt.now() - dt.fromisoformat(stamp)).total_seconds()
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# The watchdog
# ---------------------------------------------------------------------------
class Sentinel:
    def __init__(self, cfg=None, status_dir=None, state_file=STATE_FILE,
                 spool=None, hang_seconds=HANG_SECONDS):
        cfg = cfg or {}
        self.cfg = cfg
        self.alerts_cfg = cfg.get("alerts") or {}
        self.status_dir = status_dir or cfg.get("status_dir") or HOME_STATUS_DIR
        self.state_file = state_file
        self.spool = spool if spool is not None else mailer.Spool()
        self.hang_seconds = hang_seconds
        self.node_name = get_node_name(cfg)
        self.recipients = [a for a in (self.alerts_cfg.get("to") or []) if a]
        self.quota = mailer.Quota(limit=self.alerts_cfg.get("max_per_day", 50))
        self.log_tail_lines = int(self.alerts_cfg.get("log_tail_lines", 50))
        self.disk_free_min_mb = int(self.alerts_cfg.get("disk_free_min_mb", 0) or 0)
        self._disk_warned = False
        self.status_mail_seconds = (
            float(self.alerts_cfg.get("status_mail_minutes", 0) or 0) * 60.0)
        # First one goes out a full interval from start, not at start: a
        # station rebooting in a loop would otherwise mail on every boot.
        self._next_status_mail = time.monotonic() + self.status_mail_seconds
        self._state = _read_json(self.state_file) or {}
        self._known = dict(self._state.get("known") or {})
        self._reconcile_boot()

    # -- state ---------------------------------------------------------------
    def _reconcile_boot(self):
        boot = _boot_id()
        if boot and self._state.get("boot_id") not in (None, boot):
            # The machine restarted. Everything it knew about is gone for that
            # reason and no other, so forget it rather than mourn it.
            self._known = {}
        self._state["boot_id"] = boot

    def _save_state(self):
        self._state["known"] = self._known
        try:
            mailer._write_json_atomic(self.state_file, self._state)
        except OSError:
            pass

    # -- letters -------------------------------------------------------------
    def queue(self, subject, body, kind=""):
        return self.spool.put(f"[every-camera] {subject}", body,
                              to=self.recipients, kind=kind) is not None

    def _letter(self, headline, entry, detail=""):
        status = entry.get("status") or {}
        rows = [
            ("Узел", self.node_name),
            ("Экземпляр", entry.get("instance") or "?"),
            ("Камера", entry.get("camera") or "?"),
            ("Машина", socket.gethostname()),
            ("Процесс", str(entry.get("pid") or "?")),
            ("Время", dt.now().isoformat(timespec="seconds")),
        ]
        if status.get("last_update"):
            rows.append(("Последний статус", str(status["last_update"])))
        parts = [headline, "", "\n".join(f"{k:<18} {v}" for k, v in rows)]
        if detail:
            parts += ["", detail]
        if status:
            interesting = ("status", "shots_taken", "errors", "last_shot",
                           "ccd_temp", "setpoint", "disk_free_mb", "output_dir")
            body = [f"{key:<14} {status[key]}"
                    for key in interesting if status.get(key) is not None]
            if body:
                parts += ["", "Последнее известное состояние:", "\n".join(body)]
        log_path = console_ui.default_log_path(entry.get("camera") or "",
                                               entry.get("instance") or "")
        parts += ["", f"Последние {self.log_tail_lines} строк лога:",
                  alerts._log_tail(log_path, self.log_tail_lines)]
        return "\n".join(parts)

    # -- the four jobs -------------------------------------------------------
    def send_spool(self):
        account = mailer.resolve_account(self.alerts_cfg)
        if account is None or not self.recipients:
            return 0, 0, 0
        if self.quota.remaining() <= 0:
            if self.spool.count() and self.quota.take_announcement():
                console_ui.warn(
                    f"Alerts: daily cap of {self.quota.limit} letters reached; "
                    f"the rest stay queued. Everything is still in the log.")
            return 0, 0, 0
        return self.spool.send_all(account, self.recipients, quota=self.quota)

    def watch_workers(self):
        """Compare what is running now with what was running last time."""
        current = scan_status_dir(self.status_dir)

        for key, entry in list(self._known.items()):
            if key in current and pid_alive(current[key]["pid"]):
                continue
            if key in current and not pid_alive(current[key]["pid"]):
                # The file is still there and the process is not: it never
                # reached its own shutdown. This is the case the log shows as
                # "Removed 1 stale status file(s)" at the next start, hours
                # later, with nobody told in between.
                entry["status"] = current[key]["status"]
            self._report_gone(key, entry)
            self._known.pop(key, None)

        for key, found in current.items():
            if not pid_alive(found["pid"]):
                # Its file outlives it — nothing removes a status file except
                # the worker itself, and this one did not get to. Registering it
                # again would mean reporting the same death on every pass until
                # somebody restarted the camera.
                continue
            status = found["status"]
            entry = self._known.setdefault(key, {})
            entry.update({
                "pid": found["pid"],
                "instance": status.get("instance_name") or entry.get("instance"),
                "camera": status.get("camera_type") or entry.get("camera"),
                "status": status,
            })
            self._check_hang(key, entry, status)

        self._save_state()

    def _report_gone(self, key, entry):
        marker = alerts.take_marker(alerts.EXIT_DIR, key)
        if marker is not None:
            # It said goodbye. Nothing to report.
            return
        instance = entry.get("instance") or key
        detail = alerts.take_crash(key)
        self.queue(
            f"{instance} на {self.node_name} — процесс завершился аварийно",
            self._letter(
                f"{instance} на {self.node_name}: процесс исчез, не пройдя "
                f"штатное завершение.", entry, detail or
                "Отчёта о падении нет — процесс не успел его записать: "
                "SIGKILL, нехватка памяти, segfault в драйвере камеры или "
                "выключение машины."),
            kind="worker-died")

    def _check_hang(self, key, entry, status):
        age = _age_of(status)
        if age is None or age < self.hang_seconds:
            if entry.pop("hung", None):
                instance = entry.get("instance") or key
                self.queue(f"{instance} на {self.node_name} — снова публикует статус",
                           self._letter(f"{instance} на {self.node_name}: "
                                        f"статус снова обновляется.", entry),
                           kind="worker-alive-again")
            return
        if entry.get("hung"):
            return
        entry["hung"] = True
        instance = entry.get("instance") or key
        self.queue(
            f"{instance} на {self.node_name} — процесс жив, но не работает",
            self._letter(
                f"{instance} на {self.node_name}: процесс существует, но статус "
                f"не обновлялся {age / 60:.0f} мин.", entry,
                "Процесс не мёртв — он застрял. Чаще всего это ожидание "
                "ответа от камеры или от контроллера в вызове без таймаута."),
            kind="worker-hung")

    def check_disk(self):
        if self.disk_free_min_mb <= 0:
            return
        free = None
        for entry in self._known.values():
            value = (entry.get("status") or {}).get("disk_free_mb")
            if value is not None and (free is None or value < free):
                free = value
        if free is None:
            return
        if free >= self.disk_free_min_mb:
            self._disk_warned = False
            return
        if self._disk_warned:
            return
        self._disk_warned = True
        self.queue(
            f"{self.node_name} — заканчивается место на диске",
            f"На узле {self.node_name} ({socket.gethostname()}) свободно "
            f"{free} МБ при пороге {self.disk_free_min_mb} МБ.",
            kind="disk-low")

    def status_summary(self):
        """Every camera on this machine, as one table for a letter."""
        lines = []
        for key in sorted(self._known):
            entry = self._known[key]
            status = entry.get("status") or {}
            age = _age_of(status)
            lines.append("  ".join((
                f"{str(entry.get('instance') or key):<16}",
                f"{str(entry.get('camera') or '?'):<7}",
                f"{str(status.get('status') or '?'):<8}",
                f"кадров {str(status.get('shots_taken') or 0):>6}",
                f"ошибок {str(status.get('errors') or 0):>4}",
                f"диск {status.get('disk_free_mb') or '?'} МБ",
                ("обновлён только что" if age is None or age < 60
                 else f"без обновлений {age / 60:.0f} мин"),
            )))
        return "\n".join(lines)

    def send_status_mail(self, force=False):
        """Queue the periodic summary when it is due. Returns True if it did."""
        if self.status_mail_seconds <= 0 and not force:
            return False
        if not force and time.monotonic() < self._next_status_mail:
            return False
        self._next_status_mail = time.monotonic() + self.status_mail_seconds
        if not self._known:
            return False
        body = (f"Узел {self.node_name} ({socket.gethostname()}), "
                f"{dt.now().isoformat(timespec='seconds')}\n\n"
                + self.status_summary())
        return self.queue(
            f"{self.node_name} — состояние камер ({len(self._known)})",
            body, kind="status-summary")

    def tick(self):
        self.watch_workers()
        self.check_disk()
        self.send_status_mail()
        return self.send_spool()

    def run(self, interval=DEFAULT_INTERVAL):
        console_ui.log(f"Sentinel watching {self.status_dir} every {interval:.0f} s")
        account = mailer.resolve_account(self.alerts_cfg)
        if not self.recipients:
            console_ui.warn("Sentinel: no recipients configured (alerts.to in "
                            "config.json) — letters will queue and stay queued")
        console_ui.log(f"Sentinel mail: {mailer.account_summary(account)}")
        while True:
            try:
                self.tick()
            except Exception as exc:
                console_ui.warn(f"Sentinel pass failed: {exc}")
            time.sleep(interval)


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Watch the camera workers on this machine and send alert mail")
    parser.add_argument("--config", default=None, help="Path to config.json")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help="Seconds between passes (default 15)")
    parser.add_argument("--once", action="store_true",
                        help="Do a single pass and exit")
    parser.add_argument("--status-mail", action="store_true",
                        help="Queue a status summary now, whatever the schedule")
    args = parser.parse_args()

    try:
        cfg = find_config(args.config)
    except Exception:
        cfg = {}

    sentinel = Sentinel(cfg)
    if args.status_mail:
        sentinel.watch_workers()
        sentinel.send_status_mail(force=True)
    if args.once or args.status_mail:
        sent, failed, dropped = sentinel.tick()
        print(f"sent {sent}, failed {failed}, dropped {dropped}, "
              f"queued {sentinel.spool.count()}")
        return
    sentinel.run(interval=max(1.0, args.interval))


if __name__ == "__main__":
    main()
