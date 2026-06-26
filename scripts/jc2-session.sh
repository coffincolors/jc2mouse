#!/usr/bin/env bash
set -euo pipefail

need_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "ERROR: must run as root (use sudo)" >&2
    exit 1
  fi
}

status() {
  systemctl is-active --quiet bluetooth.service && echo "bluetooth.service: active" || echo "bluetooth.service: inactive"
  systemctl is-active --quiet jc2-bluetooth.service && echo "jc2-bluetooth.service: active" || echo "jc2-bluetooth.service: inactive"
}

start_session() {
  echo "[jc2] Stopping stock Bluetooth units..."
  systemctl stop bluetooth.target bluetooth.service || true
  systemctl mask --runtime bluetooth.service || true

  echo "[jc2] Starting jc2-bluetooth.service..."
  if systemctl start jc2-bluetooth.service; then
    for _ in {1..30}; do
      if systemctl is-active --quiet jc2-bluetooth.service; then
        echo "[jc2] Session mode active."
        return 0
      fi
      sleep 0.2
    done
  fi

  echo "[jc2] Failed to start jc2-bluetooth.service; rolling back..." >&2
  systemctl status jc2-bluetooth.service --no-pager -l >&2 || true
  journalctl -u jc2-bluetooth.service -n 80 --no-pager >&2 || true
  systemctl stop jc2-bluetooth.service || true
  systemctl unmask bluetooth.service || true
  systemctl start bluetooth.target || systemctl start bluetooth.service || true
  return 1
}

stop_session() {
  echo "[jc2] Stopping jc2-bluetooth.service..."
  systemctl stop jc2-bluetooth.service || true

  echo "[jc2] Restoring stock Bluetooth units..."
  systemctl unmask bluetooth.service || true
  systemctl start bluetooth.target || systemctl start bluetooth.service || true

  echo "[jc2] Session mode ended."
}

cmd="${1:-}"
need_root

case "$cmd" in
  status) status ;;
  start)  start_session ;;
  stop)   stop_session ;;
  *)
    echo "Usage: $0 {status|start|stop}" >&2
    exit 2
    ;;
esac
