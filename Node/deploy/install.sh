#!/usr/bin/env bash
# Install the Echinus node on a Raspberry Pi Zero 2W (Pi OS Bookworm).
#
#   git clone <repo> ~/echinus && bash ~/echinus/Node/deploy/install.sh
#
# Run it again any time to pick up new code — it's safe to repeat.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
CONFIG="$REPO_DIR/Node/node.toml"
# The camera overlay for this node's sensor. The IMX219 (Arducam B0036) is
# what the build uses; override for a different module.
CAMERA_OVERLAY="${CAMERA_OVERLAY:-imx219}"

echo "=== Echinus node install ==="
echo "repo:   $REPO_DIR"
echo "config: $CONFIG"

# ── config ───────────────────────────────────────────────────────────────────
if [ ! -f "$CONFIG" ]; then
    cp "$REPO_DIR/Node/node.toml.example" "$CONFIG"
    echo
    echo "Created $CONFIG — set a unique [node] id in it, then run this again."
    exit 1
fi

# ── boot config ──────────────────────────────────────────────────────────────
# UART for the LoRa HAT, and the camera overlay the sensor needs. Both take
# effect at boot, so if anything changed we install the service but don't
# start it yet.
REBOOT_NEEDED=0
set +e
bash "$REPO_DIR/shared/configure-boot.sh" --camera "$CAMERA_OVERLAY"
boot_status=$?
set -e
case $boot_status in
    0)  ;;
    10) REBOOT_NEEDED=1 ;;
    *)  echo "boot config failed" >&2; exit $boot_status ;;
esac

# ── apt packages ─────────────────────────────────────────────────────────────
# These can't come from PyPI on Pi OS: picamera2 and RPi.GPIO are apt-only, and
# sx126x.py needs pyserial visible to the same interpreter. That's what the
# --system-site-packages venv below is for.
APT_PACKAGES="python3-picamera2 python3-rpi.gpio python3-serial"
MISSING=""
for pkg in $APT_PACKAGES; do
    dpkg -s "$pkg" &>/dev/null || MISSING="$MISSING $pkg"
done
if [ -n "$MISSING" ]; then
    echo "installing apt packages:$MISSING"
    sudo apt-get update
    sudo apt-get install -y $MISSING
fi

# ── uv ───────────────────────────────────────────────────────────────────────
if ! command -v uv &>/dev/null; then
    echo "installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

cd "$REPO_DIR"
echo "syncing dependencies (slow on first run)..."
# --system-site-packages so the apt-installed picamera2 and RPi.GPIO are importable.
# --python pins the venv to the interpreter those apt packages were built for;
# uv otherwise downloads its own CPython, and the shared dist-packages tree then
# belongs to a different version and the imports fail anyway.
uv venv --system-site-packages --python /usr/bin/python3
uv sync --package echinus-node --active

# ── Waveshare LoRa driver ────────────────────────────────────────────────────
# sx126x.py isn't on PyPI; it comes out of Waveshare's demo zip. Without it the
# node still detects motion but can't transmit, so this is a hard failure.
bash "$REPO_DIR/shared/fetch-sx126x.sh"

# ── systemd ──────────────────────────────────────────────────────────────────
echo "installing systemd service..."
sed -e "s|%REPO_DIR%|$REPO_DIR|g" -e "s|%USER%|$USER|g" \
    "$REPO_DIR/Node/deploy/echinus-node.service" \
    | sudo tee /etc/systemd/system/echinus-node.service > /dev/null

sudo systemctl daemon-reload
if [ "$REBOOT_NEEDED" = 1 ]; then
    # Starting now would just crash-loop until the hardware config is live.
    sudo systemctl enable echinus-node.service
else
    sudo systemctl enable --now echinus-node.service
fi

echo
echo "=== done ==="
if [ "$REBOOT_NEEDED" = 1 ]; then
    echo
    echo "REBOOT REQUIRED — the boot config changed."
    echo "  sudo reboot     (a full power cycle if the camera was just enabled)"
    echo
fi
echo "status: sudo systemctl status echinus-node"
echo "logs:   sudo journalctl -fu echinus-node"
