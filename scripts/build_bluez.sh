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

  # Replace the exact 4-line sec_level selection block:
  #   if (dev->le_state.paired)
  #       sec_level = BT_IO_SEC_MEDIUM;
  #   else
  #       sec_level = BT_IO_SEC_LOW;
  #
  # with:
  #   if (dev->le_state.paired)
  #       sec_level = BT_IO_SEC_MEDIUM;
  #   else
  #       sec_level = BT_IO_SEC_MEDIUM;
  #
  # This assumes BlueZ 5.72 uses that structure (it does, per your grep).

  perl -0777 -i -pe '
    s/if\s*\(\s*dev->le_state\.paired\s*\)\s*\n\s*sec_level\s*=\s*BT_IO_SEC_MEDIUM\s*;\s*\n\s*else\s*\n\s*sec_level\s*=\s*BT_IO_SEC_LOW\s*;/
      "if (dev->le_state.paired)\n\tsec_level = BT_IO_SEC_MEDIUM;\nelse\n\tsec_level = BT_IO_SEC_MEDIUM;"/se
  ' "$f"

  # Validate we actually changed the unpaired branch
  python3 - "$f" <<'PY'
import re, pathlib, sys
f = pathlib.Path(sys.argv[1])
t = f.read_text()

# Find the specific paired/unpaired assignment nearby.
m = re.search(
    r'if\s*\(\s*dev->le_state\.paired\s*\)\s*\n\s*sec_level\s*=\s*BT_IO_SEC_MEDIUM\s*;\s*\n\s*else\s*\n\s*sec_level\s*=\s*(BT_IO_SEC_\w+)\s*;',
    t
)

if not m:
    print("[bluez] ERROR: could not find expected sec_level if/else block to patch")
    sys.exit(2)

print("[bluez] unpaired sec_level =", m.group(1))
if m.group(1) != "BT_IO_SEC_MEDIUM":
    print("[bluez] ERROR: patch did not set unpaired sec_level to BT_IO_SEC_MEDIUM")
    sys.exit(3)
PY
}

apply_patch_ignore_secondary_timeout() {
  local f="${SRC_DIR}/bluez-${BLUEZ_VER}/src/shared/gatt-client.c"

  if [[ ! -f "$f" ]]; then
    echo "ERROR: expected file not found: $f" >&2
    exit 1
  fi

  echo "[bluez] Patching gatt-client to ignore Secondary discovery timeout (att_ecode == 0x00)..."

  # Insert after the "Secondary service discovery failed" debug print:
  #   if (att_ecode == 0x00)
  #       goto next;
  #
  # so BlueZ continues instead of failing init on timeout.

  perl -0777 -i -pe '
    s/(Secondary service discovery failed\.\s*"\s*ATT ECODE: 0x%02x",\s*att_ecode\);\s*)/
      $1 . "if (att_ecode == 0x00)\n\t\tgoto next;\n"/se
  ' "$f"

  # Quick sanity check
  if ! grep -n "att_ecode == 0x00" "$f" >/dev/null; then
    echo "[bluez] ERROR: secondary timeout ignore patch did not apply" >&2
    exit 4
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
  apply_patch_ignore_secondary_timeout
  build_and_install

  echo
  echo "[done] BlueZ ${BLUEZ_VER} built+installed to ${PREFIX}"
  echo "Next:"
  echo "  sudo scripts/setup.sh"
  echo "  sudo jc2-session start"
  echo "  systemctl status jc2-bluetooth.service --no-pager"
}

main "$@"
