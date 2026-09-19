"""
Capture a sample clip for the detector testing.

By default, records straight from the attached camera the same way the node
does — Pi camera if available, falling back exactly like
`echinus_node.camera.open_camera` does on a dev machine without one — then
trims the first and last N seconds off, so the camera shake while you set up
and walk away from the tripod doesn't end up in the test fixture.

Usage:
    python trim_sample.py samples/target1.mp4                  # 10 min from the node camera, ends trimmed
    python trim_sample.py samples/target1.mp4 --duration 120   # 2 min capture instead
    python trim_sample.py samples/target1.mp4 --input raw.mov  # trim an existing file instead of capturing
    python trim_sample.py samples/target1.mp4 --keep-raw       # also keep the untrimmed capture

Requires opencv-python-headless (a dev dependency of the workspace).
Run with the workspace venv so echinus_node resolves — e.g. from the repo
root: `uv run python Node/testing/capture_sample.py ...` (or
`.venv/bin/python`). On the node, capture additionally needs picamera2.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time

import cv2


def capture_from_node_camera(
    output_path: str,
    duration_s: float,
    width: int = 640,
    height: int = 480,
    warmup_frames: int = 20,
) -> None:
    """Record via the same Camera abstraction the node uses (Pi camera, or its
    dev-machine fallback), measuring the camera's real delivered frame rate
    first so the saved clip plays back at the same cadence the detector saw."""
    from echinus_node.camera import open_camera

    with open_camera(width, height) as cam:
        print(f"Measuring capture rate ({warmup_frames} frames)...")
        warmup_start = time.monotonic()
        frames = [cam.capture_gray() for _ in range(warmup_frames)]
        warmup_elapsed = time.monotonic() - warmup_start
        fps = warmup_frames / warmup_elapsed if warmup_elapsed > 0 else 15.0
        print(f"Measured ~{fps:.1f} fps -- recording for {duration_s:.0f}s -> {output_path}")
        print("Ctrl-C to stop early\n")

        writer = cv2.VideoWriter(output_path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
        if not writer.isOpened():
            raise RuntimeError(f"Could not open output video for writing: {output_path}")

        frame_count = 0
        try:
            for frame in frames:
                writer.write(cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR))
                frame_count += 1
            start = time.monotonic()
            while time.monotonic() - start < duration_s - warmup_elapsed:
                frame = cam.capture_gray()
                writer.write(cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR))
                frame_count += 1
        except KeyboardInterrupt:
            print("\nStopped early by user")
        finally:
            writer.release()

    print(f"Captured {frame_count} frames (~{frame_count / fps:.1f}s of footage) -> {output_path}")


def trim_video(input_path: str, output_path: str, start_trim_s: float, end_trim_s: float) -> None:
    cap = cv2.VideoCapture(input_path)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open input video: {input_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    start_frame = int(start_trim_s * fps)
    end_frame = total_frames - int(end_trim_s * fps)
    if end_frame <= start_frame:
        cap.release()
        raise ValueError(
            f"Nothing left after trimming: {total_frames} frames at {fps:.1f}fps, "
            f"trimming {start_trim_s}s from the start and {end_trim_s}s from the end"
        )

    writer = cv2.VideoWriter(output_path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened():
        cap.release()
        raise RuntimeError(f"Could not open output video for writing: {output_path}")

    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
    kept = 0
    for _ in range(end_frame - start_frame):
        ok, frame = cap.read()
        if not ok:
            break
        writer.write(frame)
        kept += 1

    cap.release()
    writer.release()
    print(f"Wrote {kept} frames ({kept / fps:.1f}s) -> {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture (from the node's camera) and trim a the detector --source test fixture"
    )
    parser.add_argument("output", help="Path to write the final trimmed video")
    parser.add_argument("--input", default=None,
                        help="Trim this existing video instead of capturing from the camera")
    parser.add_argument("--duration", type=float, default=600.0,
                        help="Seconds to capture before trimming (default 600 = 10 minutes)")
    parser.add_argument("--start-trim", type=float, default=10.0, help="Seconds to cut from the start (default 10)")
    parser.add_argument("--end-trim", type=float, default=10.0, help="Seconds to cut from the end (default 10)")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--keep-raw", action="store_true",
                        help="Also keep the untrimmed capture, saved alongside output as <output>.raw.mp4")
    args = parser.parse_args()

    raw_path = args.input
    made_temp = False
    if raw_path is None:
        if args.keep_raw:
            root, _ = os.path.splitext(args.output)
            raw_path = f"{root}.raw.mp4"
        else:
            fd, raw_path = tempfile.mkstemp(suffix=".mp4")
            os.close(fd)
            made_temp = True

    try:
        if args.input is None:
            capture_from_node_camera(raw_path, args.duration, args.width, args.height)
        trim_video(raw_path, args.output, args.start_trim, args.end_trim)
    except (RuntimeError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if made_temp:
            try:
                os.remove(raw_path)
            except OSError:
                pass


if __name__ == "__main__":
    main()
