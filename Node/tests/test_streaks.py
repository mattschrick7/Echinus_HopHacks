"""What the node will and won't spend airtime on."""
import math

import pytest

from echinus_node.streaks import StreakBuffer

FRAME_MS = 66  # ~15 fps


def feed(buffer, frames, start_ms=1_000_000):
    """Play a list of per-frame blob lists through the buffer."""
    for i, candidates in enumerate(frames):
        buffer.add(start_ms + i * FRAME_MS, candidates)
    return buffer


def crossing(n=14, az0=-10.0, el0=2.0, az_rate=12.0, el_rate=1.0, jitter=0.0):
    """A target moving in a straight line, one blob per frame."""
    frames = []
    for i in range(n):
        t = i * FRAME_MS / 1000.0
        wobble = jitter * (1 if i % 2 else -1)
        frames.append([(az0 + az_rate * t + wobble, el0 + el_rate * t, 500.0)])
    return frames


def oscillating(n=16, centre=5.0, swing=3.0):
    """A branch in wind: it keeps returning to where it was."""
    return [
        [(centre + swing * math.sin(i * 1.4), 2.0, 500.0)]
        for i in range(n)
    ]


# ── what should be transmitted ───────────────────────────────────────────────


def test_a_straight_crossing_confirms():
    buffer = feed(StreakBuffer(), crossing())
    (target,) = buffer.confirmed()

    assert target.az_rate_dps == pytest.approx(12.0, abs=0.1)
    assert target.el_rate_dps == pytest.approx(1.0, abs=0.1)
    assert target.coasting is False
    # az0 is the bearing at the newest sample, not the oldest.
    last_t = 13 * FRAME_MS / 1000.0
    assert target.az_deg == pytest.approx(-10.0 + 12.0 * last_t, abs=0.1)


def test_a_confirmed_target_keeps_its_id_as_it_moves():
    buffer = StreakBuffer()
    feed(buffer, crossing(n=12))
    first = buffer.confirmed()[0].target_id

    feed(buffer, crossing(n=4, az0=-10.0 + 12.0 * 12 * FRAME_MS / 1000.0),
         start_ms=1_000_000 + 12 * FRAME_MS)
    assert buffer.confirmed()[0].target_id == first


def test_small_noise_still_confirms():
    # Real detections are not perfect; the residual gate must tolerate the
    # 0.1 deg-ish error the detector actually has.
    buffer = feed(StreakBuffer(), crossing(jitter=0.08))
    assert len(buffer.confirmed()) == 1


# ── what should not be ───────────────────────────────────────────────────────


def test_a_single_frame_blob_never_confirms():
    buffer = feed(StreakBuffer(), [[(1.0, 2.0, 500.0)]])
    assert buffer.confirmed() == []


def test_too_few_frames_never_confirms():
    buffer = feed(StreakBuffer(min_points=5), crossing(n=4))
    assert buffer.confirmed() == []


def test_oscillating_clutter_never_confirms():
    # The whole point of fitting a line: this moves plenty, but not in one
    # direction, so the residual stays high.
    buffer = feed(StreakBuffer(), oscillating(n=16))
    assert buffer.confirmed() == []


def test_a_slow_oscillation_never_confirms_even_though_it_looks_straight():
    # The case that got through before confirm_frames existed. A branch
    # swinging with a ~7-frame period is very nearly straight across any five
    # consecutive frames, so it passed the residual gate the moment its window
    # first filled — and put three bad packets on the air before the widening
    # window revealed the curve. Judged over 8 frames, four times running, it
    # cannot.
    slow = [[(5.0 + 2.4 * math.sin(i * 0.9), 2.0, 500.0)] for i in range(40)]
    assert feed(StreakBuffer(), slow).confirmed() == []


def test_a_stationary_blob_is_rejected_when_a_rate_floor_is_set():
    still = [[(5.0, 2.0, 500.0)] for _ in range(14)]
    assert feed(StreakBuffer(min_rate_dps=1.0), still).confirmed() == []


# ── continuity ───────────────────────────────────────────────────────────────


def test_a_brief_dropout_coasts_rather_than_restarting():
    frames = crossing(n=14)
    frames.insert(4, [])  # one frame where the detector saw nothing
    frames.insert(5, [])  # and another
    buffer = feed(StreakBuffer(coast_frames=3), frames)

    (target,) = buffer.confirmed()
    assert target.target_id is not None
    assert buffer.streak_count == 1  # not a second streak after the gap


def test_a_long_dropout_drops_the_streak():
    buffer = StreakBuffer(coast_frames=2)
    feed(buffer, crossing(n=12))
    feed(buffer, [[], [], [], []], start_ms=1_000_000 + 12 * FRAME_MS)

    assert buffer.streak_count == 0
    assert buffer.confirmed() == []


def test_two_crossing_targets_keep_separate_ids():
    # Moving toward each other: the case where a greedy per-streak match would
    # hand both streaks the same blob.
    frames = []
    for i in range(14):
        t = i * FRAME_MS / 1000.0
        frames.append([
            (-15.0 + 10.0 * t, 0.0, 500.0),
            (15.0 - 10.0 * t, 6.0, 500.0),
        ])
    targets = feed(StreakBuffer(), frames).confirmed()

    assert len(targets) == 2
    assert len({t.target_id for t in targets}) == 2
    assert {round(t.az_rate_dps) for t in targets} == {10, -10}


def test_clutter_cannot_accumulate_streaks_without_bound():
    # A windy day: lots of blobs, none of them forming a line.
    import random

    random.seed(0)
    buffer = StreakBuffer(max_streaks=12)
    for i in range(60):
        blobs = [(random.uniform(-30, 30), random.uniform(-20, 20), 500.0) for _ in range(6)]
        buffer.add(1_000_000 + i * FRAME_MS, blobs)

    assert buffer.streak_count <= 12


def test_confirmed_is_capped_and_best_first():
    frames = []
    for i in range(14):
        t = i * FRAME_MS / 1000.0
        frames.append([(-20.0 + 10.0 * t, 0.0, 500.0), (20.0 - 10.0 * t, 8.0, 500.0)])
    targets = feed(StreakBuffer(), frames).confirmed(limit=1)

    assert len(targets) == 1
