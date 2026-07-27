"""The measurement console: rendering, plain mode, log routing, stdout capture.

The dashboard runs on its own thread while a camera is exposing, so the property
that matters most is that nothing it does can raise into — or block — the worker.
"""
import io
import sys
import threading
import time

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import console_ui  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_singleton():
    """No test may leave a dashboard or sink installed for the next one."""
    yield
    console_ui.set_dashboard(None)
    for sink in list(console_ui._sinks):
        console_ui.remove_sink(sink)


def make_dashboard(tmp_path, plain=False, **kwargs):
    dash = console_ui.Dashboard("TEST", "cam_1", plain=plain,
                                log_file=str(tmp_path / "test.log"),
                                interval=0.05, **kwargs)
    dash._out = io.StringIO()          # capture instead of writing to a terminal
    return dash


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_draw_survives_an_empty_state(tmp_path):
    dash = make_dashboard(tmp_path)
    dash._draw()                        # must not raise on a state with no fields
    assert "TEST" in dash._out.getvalue()


def test_draw_shows_the_fields_a_driver_publishes(tmp_path):
    dash = make_dashboard(tmp_path)
    dash.update(status="running", frames=12, darks=3, errors=0,
                last_file="20260727T210000_3.fits", output_dir="/data/asi",
                node_name="Tory-1", server_url="http://10.0.0.1:8765")
    dash.set_section("camera", [("Filter:", "3")])
    dash._draw()
    text = dash._out.getvalue()
    for expected in ("running", "12 light / 3 dark", "20260727T210000_3.fits",
                     "/data/asi", "Tory-1", "http://10.0.0.1:8765", "Filter:"):
        assert expected in text


def test_capture_progress_is_shown_while_an_exposure_runs(tmp_path):
    dash = make_dashboard(tmp_path)
    dash.capture_begin("LIGHT filter=3", 25.0)
    dash._draw()
    assert "LIGHT filter=3" in dash._out.getvalue()
    dash.capture_end()
    dash._out.truncate(0)
    dash._out.seek(0)
    dash._draw()
    assert "LIGHT filter=3" not in dash._out.getvalue()


def test_long_values_are_truncated_not_wrapped(tmp_path):
    dash = make_dashboard(tmp_path)
    dash.update(output_dir="/" + "x" * 500)
    dash._draw()
    for line in dash._out.getvalue().splitlines():
        assert len(line) <= console_ui.MAX_WIDTH + 2


# ---------------------------------------------------------------------------
# The render thread
# ---------------------------------------------------------------------------
def test_the_render_thread_starts_and_stops(tmp_path):
    dash = make_dashboard(tmp_path)
    dash.start()
    try:
        assert dash._thread is not None and dash._thread.is_alive()
        time.sleep(0.15)
        assert dash._out.getvalue()          # it really did draw
    finally:
        dash.stop()
    assert dash._thread is None
    assert console_ui.get_dashboard() is None


def test_a_broken_state_value_cannot_kill_the_render_thread(tmp_path):
    class Exploding:
        def __str__(self):
            raise RuntimeError("boom")

    dash = make_dashboard(tmp_path)
    dash.start()
    try:
        dash.update(status=Exploding())
        time.sleep(0.15)
        assert dash._thread.is_alive()
    finally:
        dash.stop()


def test_stop_restores_stdout(tmp_path):
    original = sys.stdout
    dash = make_dashboard(tmp_path)
    dash.start()
    assert sys.stdout is not original
    dash.stop()
    assert sys.stdout is original


# ---------------------------------------------------------------------------
# Plain mode
# ---------------------------------------------------------------------------
def test_plain_mode_prints_lines_and_draws_nothing(tmp_path):
    dash = make_dashboard(tmp_path, plain=True)
    dash.start()
    try:
        dash.log("hello", "WARN")
        text = dash._out.getvalue()
        assert "[WARN] hello" in text
        assert console_ui.CLEAR_SCREEN not in text
    finally:
        dash.stop()


def test_plain_mode_leaves_stdout_alone(tmp_path):
    original = sys.stdout
    dash = make_dashboard(tmp_path, plain=True)
    dash.start()
    try:
        assert sys.stdout is original
    finally:
        dash.stop()


def test_plain_mode_reports_status_changes_once(tmp_path):
    dash = make_dashboard(tmp_path, plain=True)
    dash.start()
    try:
        dash.update(status="running")
        dash.update(status="running")
        dash.update(status="stopped")
        lines = [ln for ln in dash._out.getvalue().splitlines() if "status:" in ln]
        assert len(lines) == 2
    finally:
        dash.stop()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def test_log_lines_reach_the_panel_and_the_file(tmp_path):
    dash = make_dashboard(tmp_path)
    dash.log("archived a frame")
    assert any("archived a frame" in line[2] for line in dash._log)
    dash.stop()
    assert "archived a frame" in (tmp_path / "test.log").read_text()


def test_the_log_panel_is_bounded(tmp_path):
    dash = make_dashboard(tmp_path)
    for i in range(console_ui.LOG_RING + 50):
        dash.log(f"line {i}")
    assert len(dash._log) <= console_ui.LOG_RING


def test_module_log_prints_when_no_dashboard_is_running(capsys):
    console_ui.log("plain message", "INFO")
    assert "plain message" in capsys.readouterr().out


def test_module_helpers_are_safe_without_a_dashboard():
    # The GUI and the standalone tools call these with no console running.
    console_ui.update(status="running")
    console_ui.set_section("camera", [("a", "b")])
    console_ui.capture_begin("x", 1.0)
    console_ui.capture_end()
    console_ui.note("hi")


def test_sinks_receive_every_line(tmp_path):
    seen = []
    console_ui.add_sink(lambda msg, level: seen.append((msg, level)))
    console_ui.log("without dashboard", "WARN")
    dash = make_dashboard(tmp_path)
    dash.log("with dashboard", "ERROR")
    dash.stop()
    assert ("without dashboard", "WARN") in seen
    assert ("with dashboard", "ERROR") in seen


def test_a_broken_sink_cannot_break_logging(tmp_path):
    def explode(msg, level):
        raise RuntimeError("boom")

    console_ui.add_sink(explode)
    dash = make_dashboard(tmp_path)
    dash.log("still logged")
    assert any("still logged" in line[2] for line in dash._log)
    dash.stop()


# ---------------------------------------------------------------------------
# stdout capture
# ---------------------------------------------------------------------------
def test_stray_prints_go_to_the_log_instead_of_the_screen(tmp_path):
    dash = make_dashboard(tmp_path)
    dash.start()
    try:
        print("chatty C library")
        sys.stdout.flush()
        assert any("chatty C library" in line[2] for line in dash._log)
    finally:
        dash.stop()


def test_partial_writes_are_buffered_until_a_newline(tmp_path):
    dash = make_dashboard(tmp_path)
    capture = console_ui._StreamCapture(dash)
    capture.write("half ")
    assert dash._log == []
    capture.write("a line\n")
    assert dash._log[-1][2] == "half a line"


def test_a_progress_bar_collapses_into_one_updating_line(tmp_path):
    """A \\r-driven progress bar must not flood the panel (SPTT firmware load)."""
    dash = make_dashboard(tmp_path)
    capture = console_ui._StreamCapture(dash)
    for pct in range(0, 101, 10):
        capture.write(f"\r  FPGA: {pct}/100 bytes ({pct}%)")
    capture.flush()
    assert len(dash._log) == 1
    assert "100%" in dash._log[0][2]


# ---------------------------------------------------------------------------
# Log file
# ---------------------------------------------------------------------------
def test_the_log_file_rotates_at_its_size_limit(tmp_path):
    path = tmp_path / "rot.log"
    logfile = console_ui._LogFile(str(path), max_bytes=200)
    for i in range(100):
        logfile.write(f"line {i} " + "x" * 40)
    logfile.close()
    assert path.exists()
    assert (tmp_path / "rot.log.1").exists()
    assert path.stat().st_size <= 400


def test_an_unwritable_log_file_is_not_fatal(tmp_path):
    logfile = console_ui._LogFile(str(tmp_path / "nope" / "deep" / "x.log"))
    logfile.write("still fine")          # must not raise
    logfile.close()


def test_default_log_path_is_sanitised():
    path = console_ui.default_log_path("asi", "ASI/../1")
    assert "/.." not in Path(path).name
    assert path.endswith(".log")


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
def test_updates_from_many_threads_do_not_corrupt_the_screen(tmp_path):
    dash = make_dashboard(tmp_path)
    dash.start()
    stop = threading.Event()

    def writer(n):
        while not stop.is_set():
            dash.update(status=f"worker {n}", frames=n)
            dash.log(f"from {n}")

    threads = [threading.Thread(target=writer, args=(i,), daemon=True)
               for i in range(4)]
    for th in threads:
        th.start()
    time.sleep(0.3)
    stop.set()
    for th in threads:
        th.join(timeout=1)
    try:
        assert dash._thread.is_alive()
    finally:
        dash.stop()
