#!/usr/bin/env bash
# Put Waveshare's sx126x.py where `import sx126x` will find it.
#
#   bash shared/fetch-sx126x.sh          # from the repo root
#
# The driver isn't on PyPI and Waveshare doesn't publish it as a file you can
# curl directly — it only ships inside the HAT's demo zip, so we download that
# and pull the one module out of it.
#
# Both install scripts call this. Safe to repeat; it skips the download if the
# driver is already in place.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$REPO_DIR/.venv/bin/python"
ZIP_URL="https://files.waveshare.com/upload/1/18/SX126X_LoRa_HAT_CODE.zip"

if [ ! -x "$PYTHON" ]; then
    echo "no venv at $REPO_DIR/.venv — run uv sync first" >&2
    exit 1
fi

# purelib, not site.getsitepackages()[0]: the node's venv is created with
# --system-site-packages, which makes getsitepackages() return the system
# directory as well — and dropping the driver there hides it from the venv.
SITE_PACKAGES="$("$PYTHON" -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")"
TARGET="$SITE_PACKAGES/sx126x.py"

if [ -f "$TARGET" ]; then
    echo "sx126x driver already at $TARGET"
    exit 0
fi

echo "fetching Waveshare sx126x driver..."
TMP_ZIP="$(mktemp -t sx126x.XXXXXX.zip)"
trap 'rm -f "$TMP_ZIP"' EXIT

if ! curl -LSf -o "$TMP_ZIP" "$ZIP_URL"; then
    echo >&2
    echo "  download failed: $ZIP_URL" >&2
    echo "  get SX126X_LoRa_HAT_CODE.zip from the Waveshare wiki by hand, then copy" >&2
    echo "  raspberrypi/python/sx126x.py to $TARGET" >&2
    exit 1
fi

# The zip's internal layout has changed between releases, so find the module
# rather than assuming a path.
"$PYTHON" - "$TMP_ZIP" "$TARGET" <<'PY'
import shutil
import sys
import zipfile

zip_path, target = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(zip_path) as z:
    members = [n for n in z.namelist() if n == "sx126x.py" or n.endswith("/sx126x.py")]
    if not members:
        sys.exit(f"no sx126x.py inside {zip_path} — Waveshare changed the zip")
    with z.open(members[0]) as src, open(target, "wb") as dst:
        shutil.copyfileobj(src, dst)
    print(f"  {members[0]} -> {target}")
PY

# It imports RPi.GPIO and pyserial at module scope, so a missing dependency
# looks exactly like a missing driver. Say which it is now, not at runtime.
if ! "$PYTHON" -c "import sx126x" 2>/dev/null; then
    echo >&2
    echo "  installed, but 'import sx126x' still fails. Its own dependencies are" >&2
    echo "  apt packages, not PyPI ones:" >&2
    echo "      sudo apt install -y python3-rpi.gpio python3-serial" >&2
    exit 1
fi

echo "  import sx126x — ok"
