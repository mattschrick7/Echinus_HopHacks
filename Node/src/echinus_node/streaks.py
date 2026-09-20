"""
Deciding what is worth the airtime.

The detector fires on anything that moves, every frame. The radio can carry
roughly one packet a second. Left alone, those two facts meet in the worst
possible way: the node transmits far more than the channel holds, the packets
collide, and *which* observations survive is decided at random by the
collisions rather than by anything about the targets.

So the node decides instead. It keeps the last few frames' blobs, joins them
into streaks, fits a straight line to each, and transmits only the streaks that
fit well.

Why a line is the right test
----------------------------
Clutter that survives the detector's own activity map — a branch in wind, a
flag, glint on water — *oscillates*. It returns to where it was. A drone
crossing the frame does not: over half a second it traces very nearly a
straight line in (az, el). So the residual of a straight-line fit separates the
two far more sharply than "did it move far enough", which passes fast clutter
and rejects slow targets.

Why the line is also the message
--------------------------------
The fit's slopes are angular rates, and they cost four bytes. Sending them lets
the Server work out where the target was at any instant between transmissions,
so one packet a second carries what used to take fifteen. The fit is both the
filter and the compression.

What this deliberately isn't
----------------------------
Not a Kalman filter, and not a copy of the Server's tracker. The Server tracks
in three dimensions across nodes and asks "where is it, and is it the same
drone as before". This asks one much smaller question — "is this a real moving
thing, or clutter" — about one camera's own view, and a ring buffer and a least
squares fit answer it with no state worth persisting.

Known limitation: a target that hovers has no line to fit and will be rejected.
The detector already cannot see one (see its module docstring on the clutter
map), so this is consistent rather than a new gap, but it is worth knowing.
"""
from __future__ import annotations

from collections import deque
from typing import NamedTuple

import numpy as np

from echinus_link.packets import MAX_TARGETS, Target

# Target ids are one byte on the wire and 0 reads as "unset", so they cycle
# through 1..255. A node would have to hold 255 streaks at once for a reused id
# to collide with a live one.
_MAX_TARGET_ID = 255


class Fit(NamedTuple):
    """A straight line through one streak, referenced to its newest sample."""

    t0_ms: int        # the instant az0/el0 describe
    az0: float        # degrees off the lens axis at t0
    el0: float
    az_rate: float    # degrees per second
    el_rate: float
    rms_deg: float    # RMS angular distance from the fitted line


class Streak:
    """One candidate object, as a short history of where it has been."""

    __slots__ = ("id", "points", "misses", "passes", "target_id", "_fit", "_dirty")

    def __init__(self, streak_id: int, max_frames: int) -> None:
        self.id = streak_id
        self.points: deque[tuple[int, float, float]] = deque(maxlen=max_frames)
        self.misses = 0
        self.passes = 0  # consecutive frames whose fit has been good enough
        self.target_id: int | None = None
        self._fit: Fit | None = None
        self._dirty = True

    def add(self, t_ms: int, az: float, el: float) -> None:
        self.points.append((t_ms, az, el))
        self.misses = 0
        self._dirty = True

    @property
    def last(self) -> tuple[int, float, float]:
        return self.points[-1]

    def fit(self) -> Fit | None:
        """Least squares line through the streak, or None if it can't be fitted.

        Cached, because association asks every streak where it expects to be
        and there is no point refitting an untouched one.
        """
        if not self._dirty:
            return self._fit
        self._dirty = False
        self._fit = _fit(self.points)
        return self._fit

    def predict(self, t_ms: int) -> tuple[float, float]:
        """Where this streak should be at t_ms, from its fit if it has one."""
        fit = self.fit()
        if fit is None:
            _, az, el = self.last
            return az, el
        dt = (t_ms - fit.t0_ms) / 1000.0
        return fit.az0 + fit.az_rate * dt, fit.el0 + fit.el_rate * dt


def _fit(points) -> Fit | None:
    """Fit az and el against time, referenced to the most recent sample.

    Referencing to the newest sample rather than the oldest means the
    intercepts *are* the current bearing, which is what gets transmitted — no
    extrapolation needed at the point where accuracy matters most.
    """
    if len(points) < 2:
        return None

    t = np.fromiter((p[0] for p in points), dtype=np.float64, count=len(points))
    az = np.fromiter((p[1] for p in points), dtype=np.float64, count=len(points))
    el = np.fromiter((p[2] for p in points), dtype=np.float64, count=len(points))

    t0 = t[-1]
    dt = (t - t0) / 1000.0
    if dt.max() == dt.min():
        return None  # every sample at one instant: no line to fit

    az_rate, az0 = np.polyfit(dt, az, 1)
    el_rate, el0 = np.polyfit(dt, el, 1)

    # Residual as an angular distance, so one number covers both axes.
    residual = np.hypot(az - (az_rate * dt + az0), el - (el_rate * dt + el0))
    rms = float(np.sqrt(float((residual ** 2).mean())))
    if not np.isfinite(rms):
        return None

    return Fit(int(t0), float(az0), float(el0), float(az_rate), float(el_rate), rms)


class StreakBuffer:
    """Blobs in, confirmed targets out.

    Every knob here is a `[tracking]` key in node.toml. Three of them decide
    what reaches the air, and the defaults were picked by replaying a clip of
    a crossing target next to a branch swinging in wind:

      `min_points` (8 of max_frames=10) and `confirm_frames` (4) are the
      clutter rejection. Together they mean a streak is judged over most of a
      0.7s window, four times in a row, before it counts. That is what a slow
      oscillation cannot pass — see _score().

      `min_rate_dps` defaults to **off**, deliberately. A 2 deg/s floor also
      cleans up the test clip, but angular rate falls with range: a 15 m/s
      drone at 2 km subtends only 0.4 deg/s, so a floor that tidies the bench
      would blind the deployment to exactly the targets it exists to see. The
      time window does the same job without that cost.

    The rest shape association and are rarely worth touching.
    """

    def __init__(
        self,
        max_frames: int = 10,
        gate_deg: float = 2.0,
        max_rate_dps: float = 60.0,
        min_points: int = 8,
        max_residual_deg: float = 0.5,
        min_rate_dps: float = 0.0,
        confirm_frames: int = 4,
        coast_frames: int = 3,
        max_streaks: int = 12,
    ) -> None:
        self.max_frames = max_frames
        self.gate_deg = gate_deg
        self.max_rate_dps = max_rate_dps
        self.min_points = min_points
        self.max_residual_deg = max_residual_deg
        self.min_rate_dps = min_rate_dps
        self.confirm_frames = confirm_frames
        self.coast_frames = coast_frames
        self.max_streaks = max_streaks

        self._streaks: list[Streak] = []
        self._next_streak_id = 1
        self._next_target_id = 1

    # ── association ──────────────────────────────────────────────────────────

    def add(self, t_ms: int, candidates: list[tuple[float, float, float]]) -> None:
        """Fold one frame's blobs into the streaks.

        Pairs are taken cheapest first with each blob and each streak used at
        most once — the same rule the Server applies to rays, and for the same
        reason: with two objects in view, a greedy per-streak choice will
        happily give both streaks the same blob and lose one of the targets.
        """
        pairs = []
        for s, streak in enumerate(self._streaks):
            p_az, p_el = streak.predict(t_ms)
            gate = self._gate(streak, t_ms)
            for c, (az, el, _mass) in enumerate(candidates):
                distance = float(np.hypot(az - p_az, el - p_el))
                if distance <= gate:
                    pairs.append((distance, s, c))
        pairs.sort()

        claimed_streaks: set[int] = set()
        claimed_blobs: set[int] = set()
        for _distance, s, c in pairs:
            if s in claimed_streaks or c in claimed_blobs:
                continue
            claimed_streaks.add(s)
            claimed_blobs.add(c)
            az, el, _mass = candidates[c]
            self._streaks[s].add(t_ms, az, el)

        for s, streak in enumerate(self._streaks):
            if s not in claimed_streaks:
                streak.misses += 1

        for c, (az, el, _mass) in enumerate(candidates):
            if c not in claimed_blobs:
                self._start(t_ms, az, el)

        self._prune()
        self._score()

    def _score(self) -> None:
        """Count how many frames in a row each streak's fit has held up.

        This is the difference between "fits a line right now" and "is a
        target". A slow oscillation is *locally* straight: a branch swinging
        with a seven-frame period looks linear across any five of them, and
        will pass the residual gate the moment its window first fills. It stops
        passing as the window widens and the curve shows — so requiring the fit
        to hold for several consecutive frames rejects it, while a real target,
        whose fit only gets better with more points, sails through.

        Called once per frame from add(), so confirmed() stays a pure read and
        can be called as often as a caller likes without inflating the count.
        """
        for streak in self._streaks:
            if self._fits(streak):
                streak.passes += 1
                if streak.passes >= self.confirm_frames and streak.target_id is None:
                    streak.target_id = self._claim_target_id()
            else:
                streak.passes = 0

    def _fits(self, streak: Streak) -> bool:
        fit = streak.fit()
        if fit is None or len(streak.points) < self.min_points:
            return False
        if fit.rms_deg > self.max_residual_deg:
            return False
        if self.min_rate_dps and np.hypot(fit.az_rate, fit.el_rate) < self.min_rate_dps:
            return False
        return True

    def _gate(self, streak: Streak, t_ms: int) -> float:
        """How far from its prediction a streak will accept a blob.

        Grows with the gap since the streak was last seen, because a coasting
        prediction gets less trustworthy the longer it coasts.
        """
        dt = abs(t_ms - streak.last[0]) / 1000.0
        return self.gate_deg + self.max_rate_dps * dt

    def _start(self, t_ms: int, az: float, el: float) -> None:
        streak = Streak(self._next_streak_id, self.max_frames)
        self._next_streak_id += 1
        streak.add(t_ms, az, el)
        self._streaks.append(streak)

    def _prune(self) -> None:
        """Drop streaks that have gone quiet, and cap how many we carry.

        The cap matters on a windy day: without it, clutter that never confirms
        still accumulates streaks frame after frame and the association loop
        grows quadratically for no benefit.
        """
        self._streaks = [s for s in self._streaks if s.misses <= self.coast_frames]
        if len(self._streaks) > self.max_streaks:
            # Keep the ones with the most evidence behind them.
            self._streaks.sort(key=lambda s: len(s.points), reverse=True)
            self._streaks = self._streaks[: self.max_streaks]

    # ── output ───────────────────────────────────────────────────────────────

    def confirmed(self, limit: int = MAX_TARGETS) -> list[Target]:
        """The streaks worth transmitting, best fit first.

        A streak earns a target id the first time it passes, and keeps it for
        as long as it lives — that id is what lets the Hub and Server see one
        continuing target instead of a series of unrelated bearings.
        """
        scored = []
        for streak in self._streaks:
            if streak.passes < self.confirm_frames or streak.target_id is None:
                continue
            fit = streak.fit()
            if fit is None:
                continue
            # More evidence is better, a tighter fit is better.
            quality = len(streak.points) / (1.0 + fit.rms_deg)
            scored.append((quality, streak, fit))

        scored.sort(key=lambda row: row[0], reverse=True)
        return [
            Target(
                target_id=streak.target_id,
                az_deg=fit.az0,
                el_deg=fit.el0,
                az_rate_dps=fit.az_rate,
                el_rate_dps=fit.el_rate,
                coasting=streak.misses > 0,
            )
            for _quality, streak, fit in scored[:limit]
        ]

    def _claim_target_id(self) -> int:
        live = {s.target_id for s in self._streaks if s.target_id is not None}
        for _ in range(_MAX_TARGET_ID):
            target_id = self._next_target_id
            self._next_target_id = self._next_target_id % _MAX_TARGET_ID + 1
            if target_id not in live:
                return target_id
        return self._next_target_id  # every id live at once; reuse is the least bad

    @property
    def streak_count(self) -> int:
        return len(self._streaks)
