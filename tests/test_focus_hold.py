"""Taking a camera away from its schedule to focus it.

Focus used to be squeezed into whatever gap a schedule left, differently on
every camera: the PIXIS refused a live frame that would not fit before the next
slot, the others skipped their capture seconds. An operator connecting mid-night
got an unpredictable trickle of frames and no idea what was happening to the
measurements.

Now a focus session can *hold* the camera — stop it starting captures — and
focus_app asks the operator before it does that on a camera that is measuring.
These tests pin down the state machine that makes it safe: who can hold, what
renews a hold without meaning to, and what releases it when nobody says so.
"""
import sys
import time

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from camera_service import CameraService                    # noqa: E402


def service(**kwargs):
    return CameraService("asi", "test-cam", "/tmp/out", **kwargs)


# ---------------------------------------------------------------------------
# Holding and releasing
# ---------------------------------------------------------------------------
def test_a_plain_focus_session_leaves_the_schedule_alone():
    svc = service()
    svc.request_focus(60, hold=False)
    assert svc.focus_active()
    assert not svc.focus_hold()


def test_asking_for_a_hold_stops_the_schedule():
    svc = service()
    state = svc.request_focus(60, hold=True)
    assert state["hold"] is True
    assert state["hold_supported"] is True
    assert svc.focus_hold()


def test_renewing_without_saying_does_not_cancel_the_hold():
    """The MJPEG stream renews the session on every frame it sends.

    It has no opinion about holding, and if a renewal without the flag were
    read as "no hold" the camera would resume measuring under a live view that
    still believed it had stopped it.
    """
    svc = service()
    svc.request_focus(60, hold=True)
    svc.request_focus(60)                    # what /api/live.mjpg does
    assert svc.focus_hold()


def test_a_hold_can_be_released_without_ending_the_session():
    svc = service()
    svc.request_focus(60, hold=True)
    svc.request_focus(60, hold=False)
    assert svc.focus_active()
    assert not svc.focus_hold()


def test_stopping_focus_releases_the_hold():
    svc = service()
    svc.request_focus(60, hold=True)
    state = svc.stop_focus()
    assert state["hold"] is False
    assert not svc.focus_hold()
    assert not svc.focus_active()


def test_an_expired_session_releases_the_camera_by_itself():
    """The safety net: a focus tool that crashes must not stop a night's work."""
    svc = service()
    svc.request_focus(1, hold=True)
    assert svc.focus_hold()
    # The deadline is monotonic, so move the clock rather than waiting on it.
    svc._focus_deadline = time.monotonic() - 0.1
    assert not svc.focus_hold()
    assert not svc.focus_active()


# ---------------------------------------------------------------------------
# Cameras that cannot be held
# ---------------------------------------------------------------------------
def test_a_camera_whose_schedule_is_not_ours_refuses_the_hold():
    """The sentry: imagerd_rt owns the programme, so it cannot be paused here."""
    svc = CameraService("sentry", "sentry-1", "/tmp/out", supports_hold=False)
    state = svc.request_focus(60, hold=True)
    assert state["hold"] is False
    assert state["hold_supported"] is False
    assert not svc.focus_hold()
    # Streaming still works; only the pause is refused.
    assert svc.focus_active()


def test_a_camera_without_focus_cannot_be_held_either():
    svc = CameraService("sentry", "sentry-1", "/tmp/out", supports_focus=False)
    state = svc.request_focus(60, hold=True)
    assert state["focus_active"] is False
    assert state["hold_supported"] is False
    assert not svc.focus_hold()


# ---------------------------------------------------------------------------
# What the observer is told
# ---------------------------------------------------------------------------
def test_a_camera_says_whether_it_is_measuring():
    svc = service()
    assert svc.info()["schedule_active"] is None      # nothing said yet
    svc.set_schedule_active(True)
    assert svc.info()["schedule_active"] is True
    assert svc.status()["schedule_active"] is True
    svc.set_schedule_active(False)
    assert svc.info()["schedule_active"] is False


def test_the_reply_says_whether_anything_was_interrupted():
    svc = service()
    svc.set_schedule_active(True)
    assert svc.request_focus(60, hold=True)["was_measuring"] is True
    svc.stop_focus()
    svc.set_schedule_active(False)
    assert svc.request_focus(60, hold=True)["was_measuring"] is False


def test_a_hold_is_only_reported_as_effective_once_the_worker_stops():
    """Asking is not the same as it having happened.

    The worker confirms from its own loop, so the operator is told the
    measurements paused only when they actually did.
    """
    svc = service()
    svc.request_focus(60, hold=True)
    assert svc.status()["focus_hold"] is True
    assert svc.status()["hold_effective"] is False
    svc.note_hold_effective(True)
    assert svc.status()["hold_effective"] is True
    svc.stop_focus()
    assert svc.status()["hold_effective"] is False


def test_status_reports_no_hold_once_the_session_is_over():
    svc = service()
    svc.request_focus(60, hold=True)
    svc.note_hold_effective(True)
    svc._focus_deadline = time.monotonic() - 0.1
    snap = svc.status()
    assert snap["focus_active"] is False
    assert snap["focus_hold"] is False
    assert snap["hold_effective"] is False
