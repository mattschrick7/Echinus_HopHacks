#!/usr/bin/env bash
# Boot-time test capture for the detector: blink the Pi's onboard LED while
# trim_sample.py records, so you have a visual "it's working" signal with no
# wifi/monitor once the Pi is out in the field. Meant to be run via the
# echinus-test-capture systemd service in this directory.
#
# Usage: blink_and_capture.sh [output_path]
#   DURATION=300 blink_and_capture.sh   # override the default 120s capture
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"   # workspace root (.venv lives here)
OUTPUT="${1:-$SCRIPT_DIR/samples/boot_capture_$(date +%Y%m%d_%H%M%S).mp4}"
DURATION="${DURATION:-120}"

source "$SCRIPT_DIR/blink.sh"
trap stop_blink EXIT
start_blink

cd "$REPO_DIR"
"$REPO_DIR/.venv/bin/python" Node/testing/capture_sample.py "$OUTPUT" --duration "$DURATION"
