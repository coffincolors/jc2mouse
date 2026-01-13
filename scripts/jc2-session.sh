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
  echo "[jc2] Stopping stock bluetooth.service..."
  systemctl stop bluetooth.service || true

  echo "[jc2] Starting jc2-bluetooth.service..."
  if systemctl start jc2-bluetooth.service; then
    sleep 0.3
    if systemctl is-active --quiet jc2-bluetooth.service; then
      echo "[jc2] Session mode active."
      return 0
    fi
  fi

  echo "[jc2] Failed to start jc2-bluetooth.service; rolling back..." >&2
  systemctl stop jc2-bluetooth.service || true
  systemctl start bluetooth.service || true
  return 1
}

stop_session() {
  echo "[jc2] Stopping jc2-bluetooth.service..."
  systemctl stop jc2-bluetooth.service || true

  echo "[jc2] Restoring stock bluetooth.service..."
  systemctl start bluetooth.service || true

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
