#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: must run as root (sudo)" >&2
  exit 1
fi

# Install systemd unit
install -Dm644 systemd/jc2-bluetooth.service /etc/systemd/system/jc2-bluetooth.service

# Install helper script
install -Dm755 scripts/jc2-session.sh /usr/local/sbin/jc2-session

systemctl unmask bluetooth.service >/dev/null 2>&1 || true
systemctl daemon-reload

echo "[setup] Installed:"
echo "  /etc/systemd/system/jc2-bluetooth.service"
echo "  /usr/local/sbin/jc2-session"
echo
echo "[setup] Recommended next step:"
echo "  Try jc2mouse with your normal stock Bluetooth stack first."
echo "  Example:"
echo "    sudo -E .venv/bin/jc2mouse run --auto --session off"
echo
echo "[setup] Optional fallback:"
echo "  If stock Bluetooth is unreliable on your system, build/install patched bluetoothd:"
echo "    sudo scripts/build_bluez.sh"
echo "    sudo jc2-session start"
