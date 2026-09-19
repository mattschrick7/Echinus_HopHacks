from __future__ import annotations
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np


class PreviewServer:
    """MJPEG HTTP server that renders motion centroids as fading dots, composited over
    the live camera/video frame when available (falls back to a dark canvas)."""

    def __init__(self, width: int, height: int, port: int = 8080, dot_lifetime: float = 4.0) -> None:
        self._width = width
        self._height = height
        self._dot_lifetime = dot_lifetime
        self._lock = threading.Lock()
        self._dots: list[tuple[float, float, float]] = []  # (cx, cy, monotonic_time)
        self._latest_label = ""
        self._frame: np.ndarray | None = None

        server_ref = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/":
                    self._stream()
                elif self.path == "/snapshot":
                    self._snapshot()
                else:
                    self.send_error(404)

            def _stream(self):
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                while True:
                    data = _encode_jpeg(server_ref._render())
                    if data is None:
                        break
                    try:
                        self.wfile.write(
                            b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + data + b"\r\n"
                        )
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
                    time.sleep(1 / 15)

            def _snapshot(self):
                data = _encode_jpeg(server_ref._render())
                if data is None:
                    self.send_error(500)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("", port), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def add_dot(self, cx: float, cy: float, label: str = "") -> None:
        with self._lock:
            self._dots.append((cx, cy, time.monotonic()))
            self._latest_label = label

    def update_frame(self, frame_bgr: np.ndarray | None) -> None:
        """Feed the latest camera/video frame to use as the preview background."""
        if frame_bgr is None:
            return
        with self._lock:
            self._frame = frame_bgr.copy()

    def _render(self) -> np.ndarray:
        import cv2  # deferred so import error is raised only when preview is actually used

        now = time.monotonic()

        with self._lock:
            frame = self._frame
            self._dots = [(x, y, t) for x, y, t in self._dots if now - t < self._dot_lifetime]
            dots = list(self._dots)
            label = self._latest_label

        if frame is not None:
            img = frame.copy()
            if img.shape[1::-1] != (self._width, self._height):
                img = cv2.resize(img, (self._width, self._height))
        else:
            img = np.full((self._height, self._width, 3), 20, dtype=np.uint8)
            # Subtle centre crosshair for orientation while waiting for the first frame
            mx, my = self._width // 2, self._height // 2
            cv2.line(img, (mx - 18, my), (mx + 18, my), (55, 55, 55), 1)
            cv2.line(img, (mx, my - 18), (mx, my + 18), (55, 55, 55), 1)

        for x, y, t in dots:
            alpha = max(0.0, 1.0 - (now - t) / self._dot_lifetime)
            fill = int(200 * alpha)
            cv2.circle(img, (int(x), int(y)), 8, (0, fill, 0), -1)
            cv2.circle(img, (int(x), int(y)), 8, (0, 255, 0), 1)

        if label:
            cv2.putText(img, label, (8, self._height - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (180, 180, 180), 1)

        return img

    def stop(self) -> None:
        self._server.shutdown()


def _encode_jpeg(img: np.ndarray) -> bytes | None:
    try:
        import cv2
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return buf.tobytes() if ok else None
    except Exception:
        return None
