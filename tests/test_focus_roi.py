"""The focus square's geometry — the part that must be right for aiming to work.

``focus_app.crop_box`` turns a centre expressed as a fraction of the frame plus
a side in pixels into a square that stays wholly inside the frame. It is pure
arithmetic and needs neither Qt nor a camera, which is exactly why it lives
outside the widget.
"""
import sys

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("PyQt5", reason="focus_app is a Qt program")

from focus_app import crop_box, ROI_MIN                 # noqa: E402


def test_the_default_centre_is_the_middle_of_the_frame():
    assert crop_box((512, 512), 256) == (128, 128, 256)


def test_the_square_follows_the_centre_fraction():
    assert crop_box((512, 512), 128, (0.25, 0.75)) == (64, 384 - 64, 128)


def test_a_square_at_the_edge_is_pushed_inside_not_clipped():
    # Clipping would silently shrink the measured area and with it the
    # sharpness reading; the operator asked for this many pixels.
    x0, y0, side = crop_box((512, 512), 256, (0.0, 0.0))
    assert (x0, y0, side) == (0, 0, 256)
    x0, y0, side = crop_box((512, 512), 256, (1.0, 1.0))
    assert (x0, y0, side) == (256, 256, 256)


def test_a_square_larger_than_the_frame_shrinks_to_the_short_side():
    # 480 is the frame's short side; centred in a 640-wide frame that is x=80.
    assert crop_box((480, 640), 4096) == (80, 0, 480)


def test_a_non_square_frame_keeps_the_square_square():
    x0, y0, side = crop_box((480, 640), 200, (0.5, 0.5))
    assert side == 200
    assert (x0, y0) == (220, 140)
    assert 0 <= x0 <= 640 - side and 0 <= y0 <= 480 - side


def test_odd_sizes_are_honoured_exactly():
    # "Arbitrary size" means arbitrary, not rounded to a preset.
    assert crop_box((512, 512), 137)[2] == 137


def test_the_side_never_collapses_to_nothing():
    assert crop_box((512, 512), 0)[2] >= 1
    assert ROI_MIN >= 8


@pytest.mark.parametrize("fx,fy", [(-1.0, -1.0), (2.0, 2.0), (0.5, 3.0)])
def test_a_centre_outside_the_frame_still_yields_a_valid_box(fx, fy):
    x0, y0, side = crop_box((300, 400), 100, (fx, fy))
    assert 0 <= x0 <= 400 - side
    assert 0 <= y0 <= 300 - side
