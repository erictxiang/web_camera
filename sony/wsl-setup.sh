#!/usr/bin/env bash
# One-time WSL environment for the Sony camrig instance.
#
# The a6000 backend must run inside WSL2: gphoto2 reaches PTP cameras through
# libusb and usbfs, which need no kernel driver, so a usbipd-attached body is
# enough. The Osmo backend cannot run here at all (no uvcvideo), which is why
# the two cameras are two processes. See ../sony/README.md.
#
# Run as the normal user, not root:  bash sony/wsl-setup.sh
set -euo pipefail

VENV="${CAMRIG_VENV:-$HOME/camrig-venv}"

if ! dpkg -s libgphoto2-dev python3-venv python3-dev build-essential >/dev/null 2>&1; then
  echo "installing apt packages (sudo)"
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
    gphoto2 libgphoto2-dev python3-venv python3-pip python3-dev build-essential
fi

if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$(dirname "$0")/requirements.txt"

"$VENV/bin/python" - <<'PY'
import cv2, gphoto2 as gp, numpy, fastapi
print("python-gphoto2", gp.__version__, "libgphoto2", gp.gp_library_version(gp.GP_VERSION_SHORT)[0])
print("cv2", cv2.__version__, "numpy", numpy.__version__, "fastapi", fastapi.__version__)
PY
echo "venv ready at $VENV"
