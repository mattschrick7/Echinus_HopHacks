#!/usr/bin/env bash
# Set the boot-time hardware config the HAT and camera need.
#
#   bash shared/configure-boot.sh --camera imx219      # node
#   bash shared/configure-boot.sh                      # hub (UART only)
#
# Two things the Pi won't do on its own:
#
#   UART    — the SX1262 HAT talks over GPIO 14/15, but the serial port is off
#             by default and a login console holds it when it isn't. Without
#             this you get "No such file or directory: /dev/ttyS0".
#   camera  — Bookworm's autodetect doesn't reliably find the IMX219 on a
#             Zero 2W, so the overlay is named explicitly.
#
# Everything here is idempotent: settings live in one marked block that gets
# rewritten each run, and conflicting earlier lines are commented out rather
# than deleted. Exits 10 if a reboot is needed to apply the changes.
set -euo pipefail

CAMERA=""
DISABLE_BT=0

while [ $# -gt 0 ]; do
    case "$1" in
        --camera)     CAMERA="${2:?--camera needs an overlay name}"; shift 2 ;;
        --disable-bt) DISABLE_BT=1; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

if [ -f /boot/firmware/config.txt ]; then
    BOOT=/boot/firmware          # Bookworm and later
elif [ -f /boot/config.txt ]; then
    BOOT=/boot                   # Bullseye and earlier
else
    echo "no config.txt found — skipping boot config (not a Raspberry Pi?)"
    exit 0
fi

echo "boot config: $BOOT/config.txt"

SETTINGS="enable_uart=1"
[ -n "$CAMERA" ] && SETTINGS="$SETTINGS camera_auto_detect=0 dtoverlay=$CAMERA"
[ "$DISABLE_BT" = 1 ] && SETTINGS="$SETTINGS dtoverlay=disable-bt"

CHANGED=0

# The python steps below exit 0 when nothing needed changing, 10 when they
# changed something, and anything else on a real failure — which must not be
# mistaken for "no change needed".
note_status() {
    case "$1" in
        0)  ;;
        10) CHANGED=10 ;;
        *)  echo "boot config step failed (exit $1)" >&2; exit "$1" ;;
    esac
}

# ── config.txt ───────────────────────────────────────────────────────────────
set +e
sudo python3 - "$BOOT/config.txt" $SETTINGS <<'PY'
import shutil
import sys

MARK_START = "# === echinus (managed — edits here are overwritten) ==="
MARK_END = "# === end echinus ==="

path, settings = sys.argv[1], sys.argv[2:]
original = open(path, encoding="utf-8").read()
lines = original.splitlines()

# Drop any previous managed block; it gets rebuilt from scratch below.
if MARK_START in lines:
    start = lines.index(MARK_START)
    end = lines.index(MARK_END) if MARK_END in lines else len(lines) - 1
    lines = lines[:start] + lines[end + 1:]

# Comment out earlier assignments to the keys we own. config.txt is read
# top-to-bottom and conditional [sections] complicate "last one wins", so the
# only safe thing is to leave exactly one assignment in the file.
owned_keys = {s.split("=", 1)[0] for s in settings if not s.startswith("dtoverlay=")}
owned_overlays = {s for s in settings if s.startswith("dtoverlay=")}
for i, line in enumerate(lines):
    bare = line.strip()
    if not bare or bare.startswith("#"):
        continue
    key = bare.split("=", 1)[0]
    if key in owned_keys or bare in owned_overlays:
        lines[i] = f"#{line}    # echinus: superseded below"

while lines and not lines[-1].strip():
    lines.pop()

# [all] so the block applies regardless of which conditional section the file
# happened to end in.
lines += ["", MARK_START, "[all]"] + list(settings) + [MARK_END, ""]
updated = "\n".join(lines)

if updated == original:
    print("  config.txt already correct")
    sys.exit(0)

shutil.copy2(path, path + ".echinus-bak")
with open(path, "w", encoding="utf-8") as f:
    f.write(updated)
print(f"  updated config.txt (backup at {path}.echinus-bak):")
for s in settings:
    print(f"    {s}")
sys.exit(10)
PY
status=$?
set -e
note_status "$status"

# ── cmdline.txt ──────────────────────────────────────────────────────────────
# A serial console on the same UART holds the port open, which turns the
# missing-device error into a busy/garbled one. Strip it.
set +e
sudo python3 - "$BOOT/cmdline.txt" <<'PY'
import shutil
import sys

path = sys.argv[1]
original = open(path, encoding="utf-8").read()
# One long line of space-separated tokens — keep it that way.
tokens = original.split()
kept = [t for t in tokens if not (t.startswith("console=serial") or t.startswith("console=ttyAMA"))]

if kept == tokens:
    print("  cmdline.txt already free of a serial console")
    sys.exit(0)

shutil.copy2(path, path + ".echinus-bak")
with open(path, "w", encoding="utf-8") as f:
    f.write(" ".join(kept) + "\n")
print(f"  removed serial console from cmdline.txt (backup at {path}.echinus-bak)")
sys.exit(10)
PY
status=$?
set -e
note_status "$status"

# The getty can be running even with cmdline.txt clean.
for unit in serial-getty@ttyS0.service serial-getty@ttyAMA0.service; do
    if systemctl is-enabled "$unit" &>/dev/null || systemctl is-active "$unit" &>/dev/null; then
        echo "  disabling $unit"
        sudo systemctl disable --now "$unit" &>/dev/null || true
    fi
done

# ── groups ───────────────────────────────────────────────────────────────────
# The service runs as this user, not root: it needs the serial port (dialout),
# the GPIO pins the HAT's M0/M1 lines use (gpio) and the camera (video).
for grp in dialout gpio video; do
    if getent group "$grp" &>/dev/null && ! id -nG "$USER" | grep -qw "$grp"; then
        echo "  adding $USER to $grp"
        sudo usermod -aG "$grp" "$USER"
        CHANGED=10
    fi
done

if [ "$CHANGED" = 10 ]; then
    echo "  reboot required for these to take effect"
    exit 10
fi

echo "  boot config already correct"
