#!/usr/bin/env python3
"""The watchdog that outlives the camera process.

The rest of the alert system runs inside the measuring program, so it cannot report
that program being gone. This runs beside it as its own service and:

- sends the spool, as the only SMTP client in the program;
- notices a worker that died: a status file with a dead PID and no clean-exit marker;
- notices a worker that hung: alive, but its status has stopped moving;
- warns about the disk, from the free space workers last reported;
- mails a status summary if ``alerts.status_mail_minutes`` asks, so somebody who cannot
  reach in still hears from the station;
- mails the workers' tunnel addresses when they change and every
  ``server.tunnel.mail_minutes``.

No numpy, PyQt or camera driver: it must be what is still running when nothing else is.
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


def _service_config_dir():
    """One sentinel per machine, one config per camera, so it reads theirs: the
    addresses to notify are a fact about the station.

    Windows has no installer writing this directory. It looks under ``%PROGRAMDATA%``,
    and finding nothing is normal: :func:`find_config` falls back to the checkout's
    config.json.
    """
    if os.name == "nt":
        base = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
        return os.path.join(base, "every-camera")
    return "/etc/every-camera"


SERVICE_CONFIG_DIR = _service_config_dir()
DEFAULT_INTERVAL = 15.0
# Workers publish at least every STATUS_MIN_INTERVAL (5 s) and the monitor calls one
# stale at 30. Far looser here: a tile going orange is a glance, a letter is somebody's
# evening.
HANG_SECONDS = 600.0
# Cameras started together get their addresses seconds apart: one letter, not six.
TUNNEL_SETTLE_SECONDS = 30.0


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _boot_id():
    """Changes when the machine reboots; None if it cannot tell.

    Without it a reboot reads as every known PID crashing: one event, six letters. Off
    Linux it is the wall-clock boot moment, which NTP drifts by a second or two.
    Compared for equality, that can claim a reboot that never happened: one spurious
    letter, against one letter per camera after every real reboot.
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


def find_config(explicit=None, config_dir=None):
    """The first config naming somebody to notify wins: the explicit path, the
    checkout's config.json, then each service config.

    If none does, the last one read is returned, so the sentinel starts and says what is
    missing rather than refusing to run.
    """
    candidates = []
    if explicit:
        candidates.append(explicit)
    candidates.append(None)          # load_config's own default
    try:
        directory = config_dir or SERVICE_CONFIG_DIR
        candidates += [os.path.join(directory, name)
                       for name in sorted(os.listdir(directory))
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


def scan_status_dir(status_dir):
    """Keyed by file stem: console workers write ``{pid}.json``, the GUI, which can run
    several cameras in one process, ``{pid}_{camera}.json``.
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
    stamp = record.get("last_update")
    if not stamp:
        return None
    try:
        # Epoch seconds: a naive local stamp compared with dt.now() jumps an hour
        # at a daylight-saving change.
        return time.time() - dt.fromisoformat(stamp).timestamp()
    except (ValueError, TypeError):
        return None


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
        # A full interval from start, not at start: a station rebooting in a loop would
        # otherwise mail on every boot.
        self._next_status_mail = time.monotonic() + self.status_mail_seconds
        self.tunnel_cfg = (cfg.get("server") or {}).get("tunnel") or {}
        self.tunnel_recipients = ([a for a in (self.tunnel_cfg.get("to") or []) if a]
                                  or self.recipients)
        self.tunnel_mail_seconds = (
            float(self.tunnel_cfg.get("mail_minutes", 0) or 0) * 60.0)
        self._tunnel_seen = None
        self._tunnel_seen_at = 0.0
        # As for the status mail: a restarted sentinel resends only what changed.
        self._next_tunnel_mail = time.monotonic() + self.tunnel_mail_seconds
        self._state = _read_json(self.state_file) or {}
        self._known = dict(self._state.get("known") or {})
        self._reconcile_boot()

    def _reconcile_boot(self):
        boot = _boot_id()
        if boot and self._state.get("boot_id") not in (None, boot):
            # The machine restarted: everything it knew about is gone for that reason
            # alone.
            self._known = {}
        self._state["boot_id"] = boot

    def _save_state(self):
        self._state["known"] = self._known
        try:
            mailer._write_json_atomic(self.state_file, self._state)
        except OSError:
            pass

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
        current = scan_status_dir(self.status_dir)

        for key, entry in list(self._known.items()):
            if key in current and pid_alive(current[key]["pid"]):
                continue
            if key in current and not pid_alive(current[key]["pid"]):
                # The file is there and the process is not: it never reached its own
                # shutdown.
                entry["status"] = current[key]["status"]
            self._report_gone(key, entry)
            self._known.pop(key, None)

        for key, found in current.items():
            if not pid_alive(found["pid"]):
                # Only the worker removes its status file, and this one did not get to.
                # Registering it again would report the same death on every pass.
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
            if status.get("tunnel_url"):
                lines.append(f"{'':<16}  {status['tunnel_url']}")
        return "\n".join(lines)

    def tunnels(self):
        found = {}
        for key, entry in self._known.items():
            status = entry.get("status") or {}
            if status.get("tunnel_url"):
                found[key] = {"instance": entry.get("instance") or key,
                              "camera": entry.get("camera") or "?",
                              "url": status["tunnel_url"],
                              "token": status.get("tunnel_token") or ""}
        return found

    def send_tunnel_mail(self, force=False, now=None):
        now = time.monotonic() if now is None else now
        current = self.tunnels()
        urls = sorted(t["url"] for t in current.values())
        if urls != self._tunnel_seen:
            self._tunnel_seen = urls
            self._tunnel_seen_at = now
        if not current:
            return False
        # A camera that stopped, or whose tunnel is restarting, brings nobody news.
        new = not set(urls) <= set(self._state.get("tunnel_mailed") or [])
        due = (self.tunnel_mail_seconds > 0 and now >= self._next_tunnel_mail)
        settled = now - self._tunnel_seen_at >= TUNNEL_SETTLE_SECONDS
        if not force and not (settled and (new or due)):
            return False
        queued = self.spool.put(
            f"[every-camera] {self.node_name} — адреса доступа извне",
            self._tunnel_letter(current),
            to=self.tunnel_recipients, kind="tunnel") is not None
        if queued:
            if self.tunnel_mail_seconds > 0:
                self._next_tunnel_mail = now + self.tunnel_mail_seconds
            self._state["tunnel_mailed"] = urls
            self._save_state()
        return queued

    def _tunnel_letter(self, current):
        parts = [f"Узел {self.node_name} ({socket.gethostname()}), "
                 f"{dt.now().isoformat(timespec='seconds')}",
                 "",
                 "Адреса меняются при каждом перезапуске камеры; новое письмо придёт "
                 "само.",
                 "Браузер и viewer_app.py — просмотр по адресу без токена. Изменение "
                 "параметров и фокус — focus_app.py, адрес с токеном вставить в поле "
                 "адреса камеры."]
        for key in sorted(current):
            item = current[key]
            parts += ["",
                      f"{item['instance']} ({item['camera']})",
                      f"  браузер:      {item['url']}/"]
            if item["token"]:
                parts += [f"  с токеном:    {item['url']}/?token={item['token']}",
                          f"  токен:        {item['token']}"]
        return "\n".join(parts)

    def send_status_mail(self, force=False):
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
        self.send_tunnel_mail()
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


def main():
    parser = argparse.ArgumentParser(
        description="Watch the camera workers on this machine and send alert mail")
    parser.add_argument("--config", default=None, help="Path to config.json")
    parser.add_argument("--config-dir", default=None,
                        help="Directory of the cameras' service configs "
                             f"(default {SERVICE_CONFIG_DIR})")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help="Seconds between passes (default 15)")
    parser.add_argument("--once", action="store_true",
                        help="Do a single pass and exit")
    parser.add_argument("--status-mail", action="store_true",
                        help="Queue a status summary now, whatever the schedule")
    parser.add_argument("--tunnel-mail", action="store_true",
                        help="Queue the tunnel addresses now, whatever the schedule")
    args = parser.parse_args()

    try:
        cfg = find_config(args.config, config_dir=args.config_dir)
    except Exception:
        cfg = {}

    sentinel = Sentinel(cfg)
    if args.status_mail:
        sentinel.watch_workers()
        sentinel.send_status_mail(force=True)
    if args.tunnel_mail:
        sentinel.watch_workers()
        if not sentinel.send_tunnel_mail(force=True):
            print("no camera on this machine reports a tunnel address")
    if args.once or args.status_mail or args.tunnel_mail:
        sent, failed, dropped = sentinel.tick()
        print(f"sent {sent}, failed {failed}, dropped {dropped}, "
              f"queued {sentinel.spool.count()}")
        return
    sentinel.run(interval=max(1.0, args.interval))


if __name__ == "__main__":
    main()
