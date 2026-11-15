#!/usr/bin/env bash
# Run the edge test suite on Debian Bookworm arm64 with apt's OpenCV/NumPy —
# the same Python stack as Raspberry Pi OS (ADR-014). Needs Docker; on Apple
# Silicon the arm64 image runs natively. Camera code is still tested via fakes.
set -euo pipefail
cd "$(dirname "$0")/.."
docker run --rm -v "$PWD:/src:ro" debian:bookworm-slim bash -c '
set -e
apt-get update -qq >/dev/null
apt-get install -y -qq python3-opencv python3-numpy python3-venv python3-pip \
    python3-pytest python3-jsonschema ffmpeg >/dev/null 2>&1
cp -r /src /work && cd /work
python3 -m venv --system-site-packages /venv
/venv/bin/pip install --quiet --no-deps ./services/edge
/venv/bin/python -c "import platform, sys, cv2, numpy; print(platform.machine(), \"python\", sys.version.split()[0], \"opencv\", cv2.__version__, \"numpy\", numpy.__version__)"
cd services/edge && /venv/bin/python -m pytest -q -p no:cacheprovider
'
