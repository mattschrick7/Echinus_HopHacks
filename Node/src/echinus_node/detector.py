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

        self._average: np.ndarray | None = None
        self._activity: np.ndarray | None = None
        self.last_centroid: tuple[float, float] | None = None

    def update(self, frame: np.ndarray) -> tuple[float, float] | None:
        """Feed one uint8 grayscale frame.

        Returns (az_deg, el_deg) relative to the lens axis when something moved,
        or None otherwise. The first frame always returns None — it becomes the
        initial background.
        """
        f = frame.astype(np.float32)

        if self._average is None:
            # Trust the frame over the config: a camera may not give the size
            # that was asked for, and a replayed video certainly won't. The
            # angle maths below depends on these being the real dimensions.
            self.height, self.width = frame.shape[:2]
            self._average = f.copy()
            self._activity = np.zeros_like(f)
            return None

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
            return None

        labelled, blob_count = label(active)
        if blob_count == 0:
            return None

        # Score blobs by summed weight: a big cluttered blob loses to a smaller
        # but salient one.
        mass = np.bincount(labelled.ravel(), weights=(weight * active).ravel())
        mass[0] = 0.0  # label 0 is the background
        best = int(mass.argmax())
        if mass[best] < self.min_active_pixels:
            return None

        ys, xs = np.where(labelled == best)
        w = weight[ys, xs]
        cx = float((xs * w).sum() / w.sum())
        cy = float((ys * w).sum() / w.sum())
        self.last_centroid = (cx, cy)

        return self.pixel_to_azel(cx, cy)

    def pixel_to_azel(self, px: float, py: float) -> tuple[float, float]:
        """Pixel coordinates -> degrees off the lens axis (right +az, up +el).

        Pinhole model: one focal length in pixels, derived from the horizontal
        field of view, used for both axes (square pixels).
        """
        focal_px = (self.width / 2.0) / np.tan(np.radians(self.fov_h_deg) / 2.0)
        az = float(np.degrees(np.arctan2(px - self.width / 2.0, focal_px)))
        el = float(np.degrees(np.arctan2(self.height / 2.0 - py, focal_px)))
        return az, el
