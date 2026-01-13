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

apply_patch_att_timeout_quirk() {
  local f="${SRC_DIR}/bluez-${BLUEZ_VER}/src/shared/att.c"

  if [[ ! -f "$f" ]]; then
    echo "ERROR: expected file not found: $f" >&2
    exit 1
  fi

  echo "[bluez] Patching att.c timeout_cb() to avoid disconnect on secondary service discovery timeout..."

  # 1) Ensure wakeup_chan_writer is forward-declared before timeout_cb
  # (prevents implicit declaration -> static redeclaration error)
  if ! awk '
      /static bool timeout_cb/ { seen_timeout=1 }
      /static void wakeup_chan_writer/ && !seen_timeout { found=1 }
      END { exit(found ? 0 : 1) }
    ' "$f"
  then
    perl -0777 -i -pe '
      s/(static bool timeout_cb\s*\(void \*user_data\)\s*\n\{)/static void wakeup_chan_writer(void *data, void *user_data);\n\n$1/s
    ' "$f"
  fi

  # 2) Insert quirk block after "op->timeout_id = 0;" and before "disc_att_send_op(op);"
  # Do it with awk to avoid perl escape issues.
  local tmp
  tmp="$(mktemp)"

  awk '
    BEGIN { inserted=0 }
    {
      print $0

      if (!inserted && $0 ~ /op->timeout_id[[:space:]]*=[[:space:]]*0;[[:space:]]*$/) {
        print ""
        print "\t/*"
        print "\t * Joy-Con 2 quirk:"
        print "\t * Secondary Service discovery (Read By Group Type, group type 0x2801)"
        print "\t * may never respond. Do not terminate the ATT bearer; synthesize an"
        print "\t * Error Response and continue."
        print "\t */"
        print "\tif (op->opcode == BT_ATT_OP_READ_BY_GRP_TYPE_REQ && op->callback && op->pdu && op->len >= 6) {"
        print "\t\tuint16_t group_type = get_le16(op->pdu + 4);"
        print "\t\tif (group_type == 0x2801) {"
        print "\t\t\tuint8_t err_pdu[4];"
        print "\t\t\tuint16_t handle = get_le16(op->pdu);"
        print ""
        print "\t\t\terr_pdu[0] = op->opcode; /* request opcode */"
        print "\t\t\tput_le16(handle, &err_pdu[1]);"
        print "\t\t\terr_pdu[3] = BT_ATT_ERROR_UNSUPPORTED_GROUP_TYPE;"
        print ""
        print "\t\t\top->callback(BT_ATT_OP_ERROR_RSP, err_pdu, sizeof(err_pdu), op->user_data);"
        print "\t\t\tdestroy_att_send_op(op);"
        print "\t\t\twakeup_chan_writer(chan, NULL);"
        print "\t\t\treturn false;"
        print "\t\t}"
        print "\t}"
        print ""

        inserted=1
      }
    }
    END {
      if (!inserted) exit 42
    }
  ' "$f" > "$tmp" || {
    rc=$?
    rm -f "$tmp"
    if [ "$rc" -eq 42 ]; then
      echo "[bluez] ERROR: could not find anchor line \"op->timeout_id = 0;\" in att.c" >&2
      exit 6
    fi
    echo "[bluez] ERROR: awk insert failed" >&2
    exit 7
  }

  mv "$tmp" "$f"

  # 3) Verify the patch landed
  if ! grep -n "Joy-Con 2 quirk" "$f" >/dev/null; then
    echo "[bluez] ERROR: att.c timeout quirk patch did not apply" >&2
    exit 5
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
  #apply_patch_ignore_secondary_timeout
  apply_patch_att_timeout_quirk
  build_and_install

  echo
  echo "[done] BlueZ ${BLUEZ_VER} built+installed to ${PREFIX}"
  echo "Next:"
  echo "  sudo scripts/setup.sh"
  echo "  sudo jc2-session start"
  echo "  systemctl status jc2-bluetooth.service --no-pager"
}

main "$@"
