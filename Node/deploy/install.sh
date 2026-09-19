#!/usr/bin/env bash
# Install the Echinus node on a Raspberry Pi Zero 2W (Pi OS Bookworm).
#
#   git clone <repo> ~/echinus && bash ~/echinus/Node/deploy/install.sh
#
# Run it again any time to pick up new code — it's safe to repeat.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
CONFIG="$REPO_DIR/Node/node.toml"

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

# ── uv ───────────────────────────────────────────────────────────────────────
if ! command -v uv &>/dev/null; then
    echo "installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

cd "$REPO_DIR"
echo "syncing dependencies (slow on first run)..."
# --system-site-packages so the apt-installed picamera2 is importable; it can't
# be installed from PyPI. Install it first if it's missing:
#   sudo apt install -y python3-picamera2
uv venv --system-site-packages
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
sudo systemctl enable --now echinus-node.service

echo
echo "=== done ==="
echo "status: sudo systemctl status echinus-node"
echo "logs:   sudo journalctl -fu echinus-node"
