#!/usr/bin/env bash
set -euo pipefail

BLUEZ_VER="${BLUEZ_VER:-5.72}"
PREFIX="${PREFIX:-/opt/jc2mouse/bluez}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="${ROOT_DIR}/bluez-src"
BLD_DIR="${ROOT_DIR}/bluez-build"
TARBALL="bluez-${BLUEZ_VER}.tar.xz"
URL="https://www.kernel.org/pub/linux/bluetooth/${TARBALL}"

need_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "ERROR: run as root (sudo)" >&2
    exit 1
  fi
}

ensure_deps() {
  command -v curl >/dev/null || { echo "Missing curl" >&2; exit 1; }
  command -v make >/dev/null || { echo "Missing make" >&2; exit 1; }
  command -v gcc  >/dev/null || { echo "Missing gcc" >&2; exit 1; }
}

fetch() {
  mkdir -p "${SRC_DIR}"
  cd "${SRC_DIR}"

  if [[ ! -f "${TARBALL}" ]]; then
    echo "[bluez] Downloading ${URL}"
    curl -L --fail -o "${TARBALL}" "${URL}"
  else
    echo "[bluez] Using cached ${SRC_DIR}/${TARBALL}"
  fi

  rm -rf "bluez-${BLUEZ_VER}"
  tar -xf "${TARBALL}"
}

apply_patch_force_medium() {
  local f="${SRC_DIR}/bluez-${BLUEZ_VER}/src/device.c"

  if [[ ! -f "$f" ]]; then
    echo "ERROR: expected file not found: $f" >&2
    exit 1
  fi

  echo "[bluez] Patching device_connect_le() to force BT_IO_SEC_MEDIUM for unpaired LE..."

  # We patch the specific if/else that sets sec_level based on paired.
  # This is a targeted text substitution: change BT_IO_SEC_LOW -> BT_IO_SEC_MEDIUM
  # but only in the "else" branch directly under "if (dev->le_state.paired)" context.
  #
  # This avoids brittle line-number patches.
  perl -0777 -i -pe '
    s/(if\s*\(\s*dev->le_state\.paired\s*\)\s*\{\s*sec_level\s*=\s*BT_IO_SEC_MEDIUM;\s*\}\s*else\s*\{\s*sec_level\s*=\s*)BT_IO_SEC_LOW(\s*;\s*\})/${1}BT_IO_SEC_MEDIUM$2/s
  ' "$f"

  # Sanity check: confirm LOW no longer appears in that function region
  if grep -n "BT_IO_SEC_LOW" "$f" >/dev/null; then
    echo "[warn] BT_IO_SEC_LOW still present somewhere in device.c (may be unrelated)."
  fi
}

build_and_install() {
  rm -rf "${BLD_DIR}"
  mkdir -p "${BLD_DIR}"
  cd "${SRC_DIR}/bluez-${BLUEZ_VER}"

  echo "[bluez] Configuring..."
  ./configure --prefix="${PREFIX}" \
    --sysconfdir=/etc \
    --localstatedir=/var \
    --enable-experimental \
    --disable-systemd

  echo "[bluez] Building..."
  make -j"$(nproc)"

  echo "[bluez] Installing to ${PREFIX}..."
  rm -rf "${PREFIX}"
  make install

  echo "[bluez] Installed bluetoothd at:"
  echo "  ${PREFIX}/sbin/bluetoothd"
}

main() {
  need_root
  ensure_deps
  fetch
  apply_patch_force_medium
  build_and_install

  echo
  echo "[done] BlueZ ${BLUEZ_VER} built+installed to ${PREFIX}"
  echo "Next:"
  echo "  sudo scripts/setup.sh"
  echo "  sudo jc2-session start"
  echo "  systemctl status jc2-bluetooth.service --no-pager"
}

main "$@"
