#!/usr/bin/env bash
# Install the Echinus hub on a Raspberry Pi 4 (Pi OS Bookworm).
#
#   git clone <repo> ~/echinus && bash ~/echinus/Hub/deploy/install.sh
#
# Run it again any time to pick up new code — it's safe to repeat.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
CONFIG="$REPO_DIR/Hub/hub.toml"

echo "=== Echinus hub install ==="
echo "repo:   $REPO_DIR"
echo "config: $CONFIG"

# ── config ───────────────────────────────────────────────────────────────────
if [ ! -f "$CONFIG" ]; then
    cp "$REPO_DIR/Hub/hub.toml.example" "$CONFIG"
    echo
    echo "Created $CONFIG — set [server] url to your Server, then run this again."
    exit 1
fi

# ── uv ───────────────────────────────────────────────────────────────────────
if ! command -v uv &>/dev/null; then
    echo "installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

cd "$REPO_DIR"
echo "syncing dependencies..."
uv sync --package echinus-hub

# ── Waveshare LoRa driver ────────────────────────────────────────────────────
# sx126x.py isn't on PyPI; it comes out of Waveshare's demo zip. Without it the
# hub has nothing to listen to, so this is a hard failure.
bash "$REPO_DIR/shared/fetch-sx126x.sh"

# ── systemd ──────────────────────────────────────────────────────────────────
echo "installing systemd service..."
sed -e "s|%REPO_DIR%|$REPO_DIR|g" -e "s|%USER%|$USER|g" \
    "$REPO_DIR/Hub/deploy/echinus-hub.service" \
    | sudo tee /etc/systemd/system/echinus-hub.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now echinus-hub.service

echo
echo "=== done ==="
echo "status: sudo systemctl status echinus-hub"
echo "logs:   sudo journalctl -fu echinus-hub"
