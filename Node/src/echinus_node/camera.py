"""
Grayscale frame source for the node.

On a Pi with a camera attached this is picamera2. Anywhere else (a laptop, a
recorded clip you want to replay through the detector) it falls back to
OpenCV. Both return the same thing — a uint8 HxW grayscale array — so nothing
downstream has to care which one it got.
"""
from __future__ import annotations

import os

import numpy as np


class EndOfStream(Exception):
    """A file source ran out of frames. Not an error — just the end of the clip."""


class Camera:
    """Base class. Subclasses implement capture_gray() and close()."""

    last_frame_bgr: np.ndarray | None = None
    """Most recent colour frame, kept for the preview server."""

    source_fps: float | None = None
    """Native frame rate of a file source, so replay can be paced to real time.
    None for live cameras, which pace themselves."""

    def capture_gray(self) -> np.ndarray:
        raise NotImplementedError

    def rewind(self) -> None:
        """Restart a file source. No-op for live cameras."""

    def close(self) -> None:
        raise NotImplementedError

    def __enter__(self) -> "Camera":
        return self

    def __exit__(self, *_) -> None:
        self.close()


class PiCamera(Camera):
    def __init__(self, width: int, height: int) -> None:
        from picamera2 import Picamera2

        self._cam = Picamera2()
        self._cam.configure(
            self._cam.create_preview_configuration(main={"format": "RGB888", "size": (width, height)})
        )
        self._cam.start()

    def capture_gray(self) -> np.ndarray:
        rgb = self._cam.capture_array()
        self.last_frame_bgr = rgb[:, :, ::-1]
        f = rgb.astype(np.float32)
        return (0.299 * f[:, :, 0] + 0.587 * f[:, :, 1] + 0.114 * f[:, :, 2]).astype(np.uint8)

    def close(self) -> None:
        self._cam.stop()
        self._cam.close()


class OpenCVCamera(Camera):
    def __init__(self, width: int, height: int, source: int | str = 0) -> None:
        import cv2

        self._cv2 = cv2
        self._cap = cv2.VideoCapture(source)
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        if not self._cap.isOpened():
            raise RuntimeError(f"could not open camera source {source!r}")

        # A file can hit EOF and be rewound; a live device can't.
        self._is_file = isinstance(source, str) and os.path.exists(source)
        if self._is_file:
            fps = self._cap.get(cv2.CAP_PROP_FPS)
            self.source_fps = fps if fps and fps > 0 else None

    def capture_gray(self) -> np.ndarray:
        ok, frame = self._cap.read()
        if not ok:
            if self._is_file:
                raise EndOfStream
            raise RuntimeError("frame capture failed")
        self.last_frame_bgr = frame
        return self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2GRAY)

    def rewind(self) -> None:
        self._cap.set(self._cv2.CAP_PROP_POS_FRAMES, 0)

    def close(self) -> None:
        self._cap.release()


def open_camera(width: int = 640, height: int = 480, source: int | str = 0) -> Camera:
    """Open the Pi camera, or OpenCV if picamera2 is unavailable.

    `source` other than 0 (a file path, a stream URL, another device index)
    always goes through OpenCV — picamera2 only talks to the ribbon camera.
    """
    if source == 0:
        try:
            return PiCamera(width, height)
        except ImportError:
            pass
    return OpenCVCamera(width, height, source)
