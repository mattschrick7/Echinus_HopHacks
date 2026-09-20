"""
Motion detection — the node's one real job.

Frames go in; (az, el) offsets from the lens axis come out whenever something
moves. These offsets are *camera-relative*: the node has no idea where it is
or which way it points, and doesn't need to. The Server holds each node's
position and orientation and converts these offsets into world bearings.

How it works
------------
1. Keep an exponential running average of the scene. Any pixel that differs
   from the average by more than `brightness_threshold` counts as "in motion".
2. Suppress clutter. Chronically-active regions (waving branches, a flag, a
   busy sidewalk) fire the *same* pixels frame after frame, whereas a real
   target sweeps *through* pixels, firing any one of them only briefly. A
   per-pixel occupancy average (`_activity`) tracks how often each pixel is in
   motion, and pixels that fire persistently get weighted down. This is a soft
   weighting, not a hard mask — a strong transient target can still stand out
   inside a moderately cluttered region, while something that *hovers* in place
   will, by design, eventually fade into the clutter map.
3. Take the highest-weight connected blob and return its weighted centroid,
   converted to degrees off the lens axis through a pinhole camera model.
"""
from __future__ import annotations

import numpy as np
from scipy.ndimage import label


class MotionDetector:
    def __init__(
        self,
        width: int = 640,
        height: int = 480,
        fov_h_deg: float = 62.2,
        alpha: float = 0.05,
        brightness_threshold: float = 30.0,
        min_active_pixels: int = 50,
        suppress_clutter: bool = True,
        activity_alpha: float = 0.02,
        activity_ceiling: float = 0.35,
        max_candidates: int = 8,
    ) -> None:
        self.width = width
        self.height = height
        self.fov_h_deg = fov_h_deg
        self.alpha = alpha
        self.brightness_threshold = brightness_threshold
        self.min_active_pixels = min_active_pixels
        self.suppress_clutter = suppress_clutter
        self.activity_alpha = activity_alpha
        self.activity_ceiling = activity_ceiling
        self.max_candidates = max_candidates

        self._average: np.ndarray | None = None
        self._activity: np.ndarray | None = None
        self._xs: np.ndarray | None = None  # ravelled pixel coordinates, cached
        self._ys: np.ndarray | None = None  # so centroids cost three bincounts
        self.last_centroid: tuple[float, float] | None = None
        self.last_centroids: list[tuple[float, float]] = []
        """Every candidate's centroid this frame, strongest first — for the preview."""

    def update(self, frame: np.ndarray) -> tuple[float, float] | None:
        """Feed one uint8 grayscale frame; get back the single strongest blob.

        Returns (az_deg, el_deg) relative to the lens axis when something moved,
        or None otherwise. The first frame always returns None — it becomes the
        initial background.

        Kept as a thin wrapper over candidates() because plenty of callers only
        ever wanted the best blob, and the streak tracker is the only one that
        needs the rest.
        """
        found = self.candidates(frame)
        return (found[0][0], found[0][1]) if found else None

    def candidates(self, frame: np.ndarray) -> list[tuple[float, float, float]]:
        """Every blob worth considering this frame, as (az_deg, el_deg, mass).

        Strongest first, at most `max_candidates`. The streak tracker needs all
        of them: association across frames only works if a target that is
        briefly the *second* brightest thing in view still gets offered up,
        and taking only the winner is how a track loses its object to a passing
        car and never gets it back.
        """
        f = frame.astype(np.float32)
        self.last_centroids = []

        if self._average is None:
            # Trust the frame over the config: a camera may not give the size
            # that was asked for, and a replayed video certainly won't. The
            # angle maths below depends on these being the real dimensions.
            self.height, self.width = frame.shape[:2]
            self._average = f.copy()
            self._activity = np.zeros_like(f)
            ys, xs = np.indices((self.height, self.width))
            self._xs, self._ys = xs.ravel().astype(np.float32), ys.ravel().astype(np.float32)
            return []

        mask = np.abs(f - self._average) > self.brightness_threshold

        # Occupancy average: persistent clutter climbs toward 1.0, a target
        # merely passing through stays near 0.
        self._activity += self.activity_alpha * (mask.astype(np.float32) - self._activity)
        self._average += self.alpha * (f - self._average)

        if self.suppress_clutter and self.activity_ceiling > 0.0:
            # occupancy 0 -> weight 1 (keep); at the ceiling -> weight 0 (drop)
            weight = np.clip(1.0 - self._activity / self.activity_ceiling, 0.0, 1.0)
        else:
            weight = np.ones_like(f)

        # Drop fully-suppressed pixels before labelling so a wall of clutter
        # can't bridge into — and drag the centroid off — a real target's blob.
        active = mask & (weight > 0.0)

        # Gate on weighted mass, not raw pixel count, so a frame of nothing but
        # chronic clutter can't trip a detection.
        if float(weight[active].sum()) < self.min_active_pixels:
            return []

        labelled, blob_count = label(active)
        if blob_count == 0:
            return []

        # Score blobs by summed weight: a big cluttered blob loses to a smaller
        # but salient one. Centroids come from the same pass — three bincounts
        # over the frame rather than one np.where per blob, which matters when
        # we now want several of them and the host is a Pi Zero.
        flat = labelled.ravel()
        weighted = (weight * active).ravel()
        mass = np.bincount(flat, weights=weighted)
        sum_x = np.bincount(flat, weights=weighted * self._xs)
        sum_y = np.bincount(flat, weights=weighted * self._ys)
        mass[0] = 0.0  # label 0 is the background

        order = np.argsort(mass)[::-1][: self.max_candidates]
        found = []
        for index in order:
            m = float(mass[index])
            if m < self.min_active_pixels:
                break  # sorted, so everything after this is smaller too
            cx, cy = float(sum_x[index] / m), float(sum_y[index] / m)
            self.last_centroids.append((cx, cy))
            az, el = self.pixel_to_azel(cx, cy)
            found.append((az, el, m))

        self.last_centroid = self.last_centroids[0] if self.last_centroids else None
        return found

    def pixel_to_azel(self, px: float, py: float) -> tuple[float, float]:
        """Pixel coordinates -> degrees off the lens axis (right +az, up +el).

        Pinhole model: one focal length in pixels, derived from the horizontal
        field of view, used for both axes (square pixels).
        """
        focal_px = (self.width / 2.0) / np.tan(np.radians(self.fov_h_deg) / 2.0)
        az = float(np.degrees(np.arctan2(px - self.width / 2.0, focal_px)))
        el = float(np.degrees(np.arctan2(self.height / 2.0 - py, focal_px)))
        return az, el
