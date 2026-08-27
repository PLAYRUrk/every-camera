"""The watchdog: telling a crash from a shutdown, and a hang from either.

This is the part of the alert system that reports the failure nothing else can
— the program not being there any more — so what it has to get right is not
noticing a death but *classifying* it. A worker that was stopped on purpose
must produce no letter at all: an alert system that mails somebody every time
an operator presses Ctrl+C is one whose mail gets filtered away within a week,
and then the real crash goes unread too.

Nothing here sends anything: letters are inspected in the spool.
"""
import json
import sys
import time

from datetime import datetime as dt, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import alerts                                                    # noqa: E402
import mailer                                                    # noqa: E402
import sentinel as sentinel_mod                                  # noqa: E402

from sentinel import Sentinel                                    # noqa: E402

PID = 424242


@pytest.fixture
def yard(tmp_path, monkeypatch):
    """A status directory, a spool and marker directories, all disposable."""
    status_dir = tmp_path / "status"
    status_dir.mkdir()
    monkeypatch.setattr(alerts, "EXIT_DIR", str(tmp_path / "exits"))
    monkeypatch.setattr(alerts, "CRASH_DIR", str(tmp_path / "crashes"))

    alive = {"value": True}
    monkeypatch.setattr(sentinel_mod, "pid_alive", lambda pid: alive["value"])

    def build(**cfg):
        settings = {"to": ["team@example.org"]}
        settings.update(cfg)
        return Sentinel({"alerts": settings, "node_name": "TORY"},
                        status_dir=str(status_dir),
                        state_file=str(tmp_path / "sentinel.json"),
                        spool=mailer.Spool(str(tmp_path / "outbox")))

    build.status_dir = status_dir
    build.alive = alive
    build.tmp = tmp_path
    return build


def write_status(yard, *, pid=PID, age_seconds=0, name=None, **fields):
    record = {
        "instance_name": "ASI_ASI",
        "camera_type": "asi",
        "status": "running",
        "shots_taken": 41,
        "last_update": (dt.now() - timedelta(seconds=age_seconds)).isoformat(),
    }
    record.update(fields)
    path = yard.status_dir / (name or f"{pid}.json")
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def subjects(watchdog):
    return [letter["subject"] for letter in watchdog.spool.pending()]


# -- a worker that died ------------------------------------------------------
def test_a_worker_that_vanished_without_shutting_down_is_reported(yard):
    watchdog = yard()
    write_status(yard)
    watchdog.watch_workers()
    assert subjects(watchdog) == []          # first sight is not news

    yard.alive["value"] = False
    watchdog.watch_workers()
    assert len(subjects(watchdog)) == 1
    assert "завершился аварийно" in subjects(watchdog)[0]


def test_the_status_file_left_behind_is_enough_on_its_own(yard):
    # The file is still there and the process is not — the case that shows up
    # hours later as "Removed 1 stale status file(s)" and nowhere else.
    watchdog = yard()
    path = write_status(yard)
    watchdog.watch_workers()
    yard.alive["value"] = False
    watchdog.watch_workers()
    assert len(subjects(watchdog)) == 1
    assert path.exists()                     # the sentinel does not tidy up


def test_a_worker_that_shut_down_properly_is_not_reported(yard):
    watchdog = yard()
    status_path = write_status(yard)
    watchdog.watch_workers()

    alerts.note_clean_exit(status_path, "SIGTERM")
    yard.alive["value"] = False
    status_path.unlink()
    watchdog.watch_workers()
    assert subjects(watchdog) == []


def test_a_crash_report_is_quoted_in_the_letter(yard):
    watchdog = yard()
    status_path = write_status(yard)
    watchdog.watch_workers()

    alerts.note_crash(status_path, reason="PicamError: capture failed",
                      detail="Traceback (most recent call last):\n  ...")
    yard.alive["value"] = False
    watchdog.watch_workers()
    body = watchdog.spool.pending()[0]["body"]
    assert "PicamError: capture failed" in body
    assert "Traceback" in body


def test_a_death_with_nothing_said_about_it_still_explains_itself(yard):
    watchdog = yard()
    write_status(yard)
    watchdog.watch_workers()
    yard.alive["value"] = False
    watchdog.watch_workers()
    body = watchdog.spool.pending()[0]["body"]
    # No report means SIGKILL, OOM or a segfault in a driver — say so, rather
    # than leave whoever reads it at three in the morning guessing.
    assert "segfault" in body or "SIGKILL" in body


def test_a_death_is_reported_once_not_on_every_pass(yard):
    watchdog = yard()
    write_status(yard)
    watchdog.watch_workers()
    yard.alive["value"] = False
    for _ in range(5):
        watchdog.watch_workers()
    assert len(subjects(watchdog)) == 1


def test_two_cameras_in_one_process_are_two_workers(yard):
    watchdog = yard()
    write_status(yard, name=f"{PID}_asi.json")
    write_status(yard, name=f"{PID}_japan.json", camera_type="japan",
                 instance_name="JAPAN_1")
    watchdog.watch_workers()
    yard.alive["value"] = False
    watchdog.watch_workers()
    assert len(subjects(watchdog)) == 2


# -- a worker that stopped working -------------------------------------------
def test_a_process_that_is_alive_but_not_publishing_is_reported(yard):
    watchdog = yard()
    write_status(yard, age_seconds=sentinel_mod.HANG_SECONDS + 60)
    watchdog.watch_workers()
    assert len(subjects(watchdog)) == 1
    assert "не работает" in subjects(watchdog)[0]


def test_a_hang_is_reported_once_and_the_recovery_afterwards(yard):
    watchdog = yard()
    write_status(yard, age_seconds=sentinel_mod.HANG_SECONDS + 60)
    for _ in range(3):
        watchdog.watch_workers()
    assert len(subjects(watchdog)) == 1

    write_status(yard, age_seconds=0)
    watchdog.watch_workers()
    # Two letters queued in the same second have no defined order between them,
    # so ask whether the recovery is there rather than where it is.
    assert len(subjects(watchdog)) == 2
    assert any("снова публикует" in subject for subject in subjects(watchdog))


def test_a_worker_publishing_normally_is_never_mentioned(yard):
    watchdog = yard()
    for _ in range(5):
        write_status(yard, age_seconds=1)
        watchdog.watch_workers()
    assert subjects(watchdog) == []


# -- a machine that rebooted -------------------------------------------------
def test_a_reboot_is_one_event_not_a_crash_per_camera(yard, monkeypatch):
    watchdog = yard()
    write_status(yard, name=f"{PID}_asi.json")
    write_status(yard, name=f"{PID}_japan.json")
    watchdog.watch_workers()

    # Everything is gone, and the boot id has changed: the machine restarted,
    # which is one thing that happened and not two cameras that crashed.
    for path in yard.status_dir.iterdir():
        path.unlink()
    yard.alive["value"] = False
    monkeypatch.setattr(sentinel_mod, "_boot_id", lambda: "a-different-boot")

    after_reboot = yard()
    after_reboot.watch_workers()
    assert subjects(after_reboot) == []


def test_without_a_reboot_the_deaths_are_still_reported(yard, monkeypatch):
    monkeypatch.setattr(sentinel_mod, "_boot_id", lambda: "same-boot")
    watchdog = yard()
    write_status(yard)
    watchdog.watch_workers()

    yard.alive["value"] = False
    restarted = yard()                       # sentinel itself was restarted
    restarted.watch_workers()
    assert len(subjects(restarted)) == 1


# -- the disk ----------------------------------------------------------------
def test_a_filling_disk_is_reported_once(yard):
    watchdog = yard(disk_free_min_mb=1000)
    write_status(yard, disk_free_mb=200)
    watchdog.watch_workers()
    for _ in range(4):
        watchdog.check_disk()
    disk = [s for s in subjects(watchdog) if "место" in s]
    assert len(disk) == 1


# -- sending -----------------------------------------------------------------
def test_nothing_is_sent_when_there_is_no_account(yard, monkeypatch):
    monkeypatch.setattr(mailer, "resolve_account", lambda cfg=None: None)
    watchdog = yard()
    write_status(yard)
    watchdog.watch_workers()
    yard.alive["value"] = False
    watchdog.watch_workers()
    assert watchdog.send_spool() == (0, 0, 0)
    # And the letter stays queued rather than being thrown away: configure the
    # mailbox tomorrow and the crash report is still there.
    assert len(watchdog.spool.pending()) == 1


def test_the_letters_go_out_once_there_is_an_account(yard, monkeypatch):
    handed = []
    monkeypatch.setattr(mailer, "resolve_account",
                        lambda cfg=None: {"from": "a@b", "host": "h", "port": 1,
                                          "security": "ssl", "user": "u",
                                          "password": "p", "source": "test"})
    monkeypatch.setattr(mailer, "send_now",
                        lambda account, to, subject, body, timeout=None:
                        handed.append(subject))
    watchdog = yard()
    write_status(yard)
    watchdog.watch_workers()
    yard.alive["value"] = False
    watchdog.watch_workers()

    sent, failed, dropped = watchdog.send_spool()
    assert (sent, failed, dropped) == (1, 0, 0)
    assert len(handed) == 1


# -- reporting outward -------------------------------------------------------
def test_a_status_summary_lists_every_camera_on_the_machine(yard):
    """What replaces a retained status topic for somebody outside the network.

    They cannot reach in — only the station can reach out — so the station
    reports, in one letter that any mail client can read.
    """
    watchdog = yard(status_mail_minutes=30)
    write_status(yard, name=f"{PID}_asi.json", shots_taken=41, disk_free_mb=9000)
    write_status(yard, name=f"{PID}_japan.json", camera_type="japan",
                 instance_name="JAPAN_1", shots_taken=7, errors=2)
    watchdog.watch_workers()

    assert watchdog.send_status_mail(force=True) is True
    body = watchdog.spool.pending()[0]["body"]
    assert "ASI_ASI" in body and "JAPAN_1" in body
    assert "41" in body and "9000" in body


def test_the_summary_waits_for_its_interval(yard):
    watchdog = yard(status_mail_minutes=30)
    write_status(yard)
    watchdog.watch_workers()
    # Due in half an hour, not now: a station rebooting in a loop must not mail
    # on every boot.
    assert watchdog.send_status_mail() is False
    assert subjects(watchdog) == []


def test_no_summary_is_sent_when_none_was_asked_for(yard):
    watchdog = yard(status_mail_minutes=0)
    write_status(yard)
    watchdog.watch_workers()
    watchdog.tick()
    assert [s for s in subjects(watchdog) if "состояние" in s] == []


def test_a_machine_with_no_cameras_has_nothing_to_report(yard):
    watchdog = yard(status_mail_minutes=30)
    assert watchdog.send_status_mail(force=True) is False
