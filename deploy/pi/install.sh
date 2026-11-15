#!/usr/bin/env bash
# Install or update the Vigil edge service on Raspberry Pi OS (Bookworm, 64-bit).
# Run from a checkout of the repository:   sudo deploy/pi/install.sh [--start]
# Idempotent: re-run after `git pull` to update the code. Existing config and
# env files in /etc/vigil are never overwritten.
set -euo pipefail

START=0
[[ "${1:-}" == "--start" ]] && START=1
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PREFIX=/opt/vigil
DATA=/var/lib/vigil
ETC=/etc/vigil

echo "==> system packages (camera stack, OpenCV, NumPy, ffmpeg from apt)"
apt-get update -qq
apt-get install -y -qq python3-picamera2 python3-opencv python3-numpy python3-venv \
    python3-pip ffmpeg sqlite3 >/dev/null

echo "==> service user and directories"
id vigil >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin vigil
usermod -a -G video,render vigil
install -d -o vigil -g vigil -m 0750 "$DATA"
install -d -o root -g vigil -m 0750 "$ETC"
install -d -m 0755 "$PREFIX"

echo "==> python environment (uses apt's picamera2/OpenCV/NumPy; see ADR-014)"
if [[ ! -x "$PREFIX/venv/bin/python" ]]; then
    python3 -m venv --system-site-packages "$PREFIX/venv"
fi
# --no-deps: never let pip replace apt's NumPy/OpenCV (Picamera2 is built against them).
"$PREFIX/venv/bin/pip" install --quiet --no-deps --upgrade "$REPO/services/edge"
"$PREFIX/venv/bin/python" - <<'PY'
import cv2, numpy, picamera2, vigil_edge
print(f"    vigil-edge {vigil_edge.__version__}, OpenCV {cv2.__version__}, NumPy {numpy.__version__}")
PY

echo "==> configuration (kept if present)"
[[ -f "$ETC/vigil.toml" ]] || install -o root -g vigil -m 0640 "$REPO/config/vigil.example.toml" "$ETC/vigil.toml"
[[ -f "$ETC/vigil.env" ]] || install -o root -g vigil -m 0640 "$REPO/deploy/pi/vigil.env.example" "$ETC/vigil.env"

echo "==> journald limits and systemd unit"
install -D -m 0644 "$REPO/deploy/pi/journald-vigil.conf" /etc/systemd/journald.conf.d/vigil.conf
install -m 0644 "$REPO/deploy/pi/vigil-edge.service" /etc/systemd/system/vigil-edge.service
systemctl restart systemd-journald
systemctl daemon-reload
systemctl enable vigil-edge.service >/dev/null

echo "==> camera check"
if command -v rpicam-hello >/dev/null; then
    rpicam-hello --list-cameras || echo "    WARNING: no camera detected (check the ribbon cable)"
fi

if [[ $START -eq 1 ]]; then
    systemctl restart vigil-edge.service
    echo "==> started; follow logs with: journalctl -u vigil-edge -f"
else
    echo "==> installed and enabled (starts on boot). Start now: sudo systemctl start vigil-edge"
fi
