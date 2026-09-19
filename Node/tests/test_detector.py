import numpy as np
import pytest

from echinus_node.detector import MotionDetector


def blank(det):
    return np.zeros((det.height, det.width), dtype=np.uint8)


def spot(det, cx, cy, size=12):
    frame = blank(det)
    frame[cy - size // 2:cy + size // 2, cx - size // 2:cx + size // 2] = 255
    return frame


def test_first_frame_is_background_only():
    det = MotionDetector(width=64, height=48)
    assert det.update(blank(det)) is None


def test_frame_size_overrides_the_configured_size():
    """A replayed clip or a camera that ignored the requested size still works."""
    det = MotionDetector(width=640, height=480)
    det.update(np.zeros((48, 64), dtype=np.uint8))

    assert (det.width, det.height) == (64, 48)


def test_bright_blob_is_detected_and_roughly_centred():
    det = MotionDetector(width=64, height=48, min_active_pixels=10)
    det.update(blank(det))

    result = det.update(spot(det, 32, 24))

    assert result is not None
    cx, cy = det.last_centroid
    assert abs(cx - 31.5) < 2 and abs(cy - 23.5) < 2
    az, el = result
    assert abs(az) < 2 and abs(el) < 2  # centre of frame -> on the lens axis


def test_persistent_clutter_is_suppressed():
    """A blob that fires in the same place every frame eventually stops counting."""
    det = MotionDetector(width=64, height=48, min_active_pixels=10, activity_alpha=0.1)
    det.update(blank(det))

    frame = spot(det, 32, 24)
    detections = [det.update(frame) is not None for _ in range(40)]

    assert detections[0] is True    # first sighting counts
    assert detections[-1] is False  # after 40 identical frames it's clutter


def test_clutter_suppression_can_be_turned_off():
    det = MotionDetector(width=64, height=48, min_active_pixels=10, suppress_clutter=False)
    det.update(blank(det))

    frame = spot(det, 32, 24)
    # Without suppression the blob keeps triggering — until the running average
    # absorbs it, which is the other, deliberate way motion stops counting.
    assert det.update(frame) is not None
    assert det.update(frame) is not None


def test_pixel_to_azel_signs():
    det = MotionDetector(width=640, height=480, fov_h_deg=60.0)

    assert det.pixel_to_azel(320, 240) == pytest.approx((0.0, 0.0))
    az_right, _ = det.pixel_to_azel(640, 240)
    _, el_up = det.pixel_to_azel(320, 0)

    assert az_right == pytest.approx(30.0)  # right edge is half the horizontal FOV
    assert el_up > 0                        # top of the frame is above the axis
