"""Which log lines become letters, and — mostly — which ones do not.

The failure this guards against is not "no alert arrives". It is the opposite:
the night of 26 August 2026 logged the same filter-wheel error every forty
seconds from dusk until breakfast, and an alert system that mailed each one
would have sent about a thousand letters, spent the shared daily allowance
before midnight, and taught everyone to filter the address away. So most of
what is pinned down here is silence: cooldowns, digests, and the guarantee that
a rule which fails cannot take the measurement with it.

No engine started here is left attached — see the autouse fixture.
"""
import sys
import time

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import alerts                                                    # noqa: E402
import console_ui                                                # noqa: E402
import mailer                                                    # noqa: E402


@pytest.fixture(autouse=True)
def _clean_singleton():
    """No test may leave a sink or a module-level engine behind."""
    yield
    alerts.stop()
    console_ui.set_dashboard(None)
    for sink in list(console_ui._sinks):
        console_ui.remove_sink(sink)


@pytest.fixture
def engine(tmp_path):
    """An engine writing to a spool of its own, with the clock left alone."""

    def build(**cfg):
        settings = {"to": ["team@example.org"], "cooldown_minutes": 60,
                    "digest_minutes": 30, "log_tail_lines": 5}
        settings.update(cfg)
        eng = alerts.AlertEngine(
            settings, "TORY", "ASI_ASI", "asi",
            log_path=str(tmp_path / "asi.log"),
            spool=mailer.Spool(str(tmp_path / "outbox")))
        return eng

    return build


def subjects(eng):
    return [letter["subject"] for letter in eng.spool.pending()]


def bodies(eng):
    return [letter["body"] for letter in eng.spool.pending()]


# -- the faults that must wake somebody --------------------------------------
def test_a_camera_that_stopped_itself_is_reported_at_once(engine):
    eng = engine()
    eng._on_line("5 consecutive capture failures — stopping.", "ERROR")
    assert len(subjects(eng)) == 1
    assert "остановила съёмку" in subjects(eng)[0]


@pytest.mark.parametrize("line", [
    "Filter controller on /dev/ttyUSB0: no answer. The port opened, so ...",
    "Filter controller written off — nothing has answered on /dev/ttyUSB0",
    "Filter wheel: the port reopened but the controller still says nothing",
])
def test_every_way_the_controller_goes_quiet_is_reported(engine, line):
    eng = engine()
    eng._on_line(line, "ERROR")
    assert len(subjects(eng)) == 1


def test_the_letter_names_the_machine_and_quotes_the_line(engine):
    eng = engine()
    eng._on_line("Failed to open camera: no device", "ERROR")
    body = bodies(eng)[0]
    assert "TORY" in body and "ASI_ASI" in body
    assert "Failed to open camera: no device" in body


def test_the_letter_carries_the_current_status(engine):
    eng = engine()
    eng.status_provider = lambda: {"status": "error", "shots_taken": 41,
                                   "errors": 7, "disk_free_mb": 900}
    eng._on_line("Measurement loop failed: boom", "ERROR")
    body = bodies(eng)[0]
    assert "shots_taken" in body and "41" in body


def test_the_letter_carries_the_tail_of_the_log(engine, tmp_path):
    (tmp_path / "asi.log").write_text(
        "\n".join(f"line {i}" for i in range(100)), encoding="utf-8")
    eng = engine()
    eng._on_line("Measurement loop failed: boom", "ERROR")
    body = bodies(eng)[0]
    assert "line 99" in body
    assert "line 50" not in body        # only the last few, not the whole night


# -- and the silence ---------------------------------------------------------
def test_the_same_fault_every_forty_seconds_sends_one_letter(engine):
    eng = engine()
    for _ in range(200):
        eng._on_line("Filter wheel would not reach position 2 — it will not "
                     "say where it is", "ERROR")
    assert len(subjects(eng)) == 1


def test_the_repeats_are_counted_and_reported_in_the_digest(engine):
    eng = engine()
    for _ in range(50):
        eng._on_line("Filter wheel would not reach position 2", "ERROR")
    eng.flush_digest()
    digest = [b for s, b in zip(subjects(eng), bodies(eng)) if "сводка" in s]
    assert len(digest) == 1
    assert "ещё 49" in digest[0]


def test_a_fault_is_reported_again_once_the_cooldown_has_passed(engine):
    eng = engine(cooldown_minutes=0)
    eng._on_line("Filter wheel would not reach position 2", "ERROR")
    eng._on_line("Filter wheel would not reach position 2", "ERROR")
    assert len(subjects(eng)) == 2


def test_ordinary_errors_wait_for_the_digest_rather_than_ringing(engine):
    eng = engine()
    eng._on_line("write failed: disk error", "ERROR")
    eng._on_line("frame name collision", "ERROR")
    assert subjects(eng) == []

    eng.flush_digest()
    assert len(subjects(eng)) == 1
    assert "сводка ошибок (2)" in subjects(eng)[0]


def test_a_digest_with_nothing_in_it_is_not_sent(engine):
    eng = engine()
    assert eng.flush_digest() is False
    assert subjects(eng) == []


def test_warnings_and_notes_are_not_alerts(engine):
    eng = engine()
    eng._on_line("Filter wheel did not confirm position 2 — retrying", "WARN")
    eng._on_line("Saved 20260826_124600_TORY_ASI0.fits  mean 688 ADU", "INFO")
    eng.flush_digest()
    assert subjects(eng) == []


# -- the disk ----------------------------------------------------------------
def test_a_filling_disk_is_reported_once_not_every_tick(engine):
    eng = engine(disk_free_min_mb=1000)
    eng.status_provider = lambda: {"disk_free_mb": 500}
    for _ in range(10):
        eng._check_disk()
    assert len(subjects(eng)) == 1
    assert "место" in subjects(eng)[0]


def test_a_disk_that_recovers_can_be_reported_again(engine):
    eng = engine(disk_free_min_mb=1000)
    free = {"disk_free_mb": 500}
    eng.status_provider = lambda: free
    eng._check_disk()
    free["disk_free_mb"] = 5000
    eng._check_disk()
    free["disk_free_mb"] = 500
    eng._check_disk()
    assert len(subjects(eng)) == 2


# -- it cannot hurt the measurement ------------------------------------------
def test_a_rule_that_throws_does_not_reach_the_caller(engine, monkeypatch):
    eng = engine()
    monkeypatch.setattr(eng, "_classify",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    eng._on_line("anything at all", "ERROR")     # must simply return


def test_logging_from_inside_a_rule_cannot_recurse(engine, monkeypatch):
    eng = engine()
    seen = []

    def noisy(message, level):
        seen.append(message)
        # Exactly the mistake the guard exists for: a rule that logs.
        console_ui.error("and now I log about it")

    monkeypatch.setattr(eng, "_classify", noisy)
    console_ui.add_sink(eng._on_line)
    console_ui.error("the original line")
    assert len(seen) == 1


def test_an_unwritable_spool_is_not_fatal(engine, monkeypatch):
    eng = engine()
    monkeypatch.setattr(mailer, "_write_json_atomic",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
    eng._on_line("5 consecutive capture failures — stopping.", "ERROR")


# -- starting up -------------------------------------------------------------
def test_a_station_with_no_recipients_starts_nothing_and_says_so(tmp_path, capsys):
    assert alerts.start({"enabled": True, "to": []}, "TORY", "ASI", "asi") is None
    assert "no recipients" in capsys.readouterr().out


def test_alerts_can_be_turned_off_outright(tmp_path):
    assert alerts.start({"enabled": False, "to": ["a@example.org"]},
                        "TORY", "ASI", "asi") is None
