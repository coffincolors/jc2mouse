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

systemctl daemon-reload

echo "[setup] Installed:"
echo "  /etc/systemd/system/jc2-bluetooth.service"
echo "  /usr/local/sbin/jc2-session"
echo
echo "[setup] Next:"
echo "  1) Build/install patched bluetoothd to /opt/jc2mouse/bluez (next step)"
echo "  2) sudo jc2-session start"
