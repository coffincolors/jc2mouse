#!/usr/bin/env bash
set -euo pipefail

BLUEZ_VER="${BLUEZ_VER:-5.72}"
PREFIX="${PREFIX:-/opt/jc2mouse/bluez}"

# Toggles
#   JC2_PATCH_GATT_SECONDARY_TIMEOUT=1  -> in discover_secondary_cb(), treat att_ecode == 0x00 like "goto next"
#   JC2_PATCH_ATT_SECONDARY_TIMEOUT=1   -> in timeout_cb(), convert 0x2801 secondary-service timeout into ATT Error Response / Attr Not Found
#   JC2_TRACE_TIMEOUT_OPCODE10=1        -> in timeout_cb(), print start/end/group_type for timed-out Read By Group Type requests
#   JC2_SKIP_SECONDARY_DISCOVERY=1      -> skip bt_gatt_discover_secondary_services() entirely (best yes/no experiment)
#   JC2_FORCE_UNPAIRED_MEDIUM=1         -> force unpaired LE sec_level to MEDIUM instead of LOW
JC2_PATCH_GATT_SECONDARY_TIMEOUT="${JC2_PATCH_GATT_SECONDARY_TIMEOUT:-1}"
JC2_PATCH_ATT_SECONDARY_TIMEOUT="${JC2_PATCH_ATT_SECONDARY_TIMEOUT:-1}"
JC2_TRACE_TIMEOUT_OPCODE10="${JC2_TRACE_TIMEOUT_OPCODE10:-1}"
JC2_SKIP_SECONDARY_DISCOVERY="${JC2_SKIP_SECONDARY_DISCOVERY:-1}"
JC2_FORCE_UNPAIRED_MEDIUM="${JC2_FORCE_UNPAIRED_MEDIUM:-0}"

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
  command -v gcc >/dev/null || { echo "Missing gcc" >&2; exit 1; }
  command -v python3 >/dev/null || { echo "Missing python3" >&2; exit 1; }
  command -v tar >/dev/null || { echo "Missing tar" >&2; exit 1; }
  command -v pkg-config >/dev/null || { echo "Missing pkg-config" >&2; exit 1; }
  pkg-config --exists libicalvcal || { echo "Missing libicalvcal (install Arch package: libical)" >&2; exit 1; }
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

apply_patch_device_connect_le() {
  local f="${SRC_DIR}/bluez-${BLUEZ_VER}/src/device.c"
  [[ -f "$f" ]] || { echo "ERROR: missing $f" >&2; exit 1; }

  local unpaired="BT_IO_SEC_LOW"
  if [[ "${JC2_FORCE_UNPAIRED_MEDIUM}" == "1" ]]; then
    unpaired="BT_IO_SEC_MEDIUM"
  fi

  echo "[bluez] Patching device_connect_le(): paired=BT_IO_SEC_MEDIUM unpaired=${unpaired}"

  python3 - "$f" "$unpaired" <<'PY'
import pathlib, re, sys

f = pathlib.Path(sys.argv[1])
unpaired = sys.argv[2]
t = f.read_text()

pat = re.compile(
    r'if\s*\(\s*dev->le_state\.paired\s*\)\s*'
    r'\n\s*sec_level\s*=\s*BT_IO_SEC_\w+\s*;'
    r'\s*\n\s*else\s*'
    r'\n\s*sec_level\s*=\s*BT_IO_SEC_\w+\s*;',
    re.M
)

rep = (
    'if (dev->le_state.paired)\n'
    '\t\tsec_level = BT_IO_SEC_MEDIUM;\n'
    '\telse\n'
    f'\t\tsec_level = {unpaired};'
)

new_t, n = pat.subn(rep, t, count=1)
if n != 1:
    print("[bluez] ERROR: could not patch device_connect_le sec_level block")
    sys.exit(2)

f.write_text(new_t)
print(f"[bluez] device.c patched: paired=BT_IO_SEC_MEDIUM unpaired={unpaired}")
PY
}

apply_patch_gatt_secondary_timeout() {
  local f="${SRC_DIR}/bluez-${BLUEZ_VER}/src/shared/gatt-client.c"
  [[ -f "$f" ]] || { echo "ERROR: missing $f" >&2; exit 1; }

  if [[ "${JC2_PATCH_GATT_SECONDARY_TIMEOUT}" != "1" ]]; then
    echo "[bluez] Skipping gatt-client secondary timeout quirk."
    return 0
  fi

  echo "[bluez] Patching discover_secondary_cb(): att_ecode == 0x00 -> goto next"

  python3 - "$f" <<'PY'
import pathlib, re, sys

f = pathlib.Path(sys.argv[1])
t = f.read_text()

if 'att_ecode == 0x00' in t or 'case 0x00:' in t:
    print("[bluez] gatt-client secondary timeout quirk already present.")
    sys.exit(0)

func_pat = re.compile(
    r'(static void discover_secondary_cb\s*\(.*?\)\s*\{)(.*?)(^\})',
    re.M | re.S
)
m = func_pat.search(t)
if not m:
    print("[bluez] ERROR: could not find discover_secondary_cb()")
    sys.exit(2)

head, body, tail = m.group(1), m.group(2), m.group(3)

switch_pat = re.compile(
    r'switch\s*\(\s*att_ecode\s*\)\s*\{(.*?)\n(\s*default\s*:.*?\n\s*\})',
    re.M | re.S
)
sm = switch_pat.search(body)
if sm:
    switch_block = sm.group(0)
    if 'case 0x00:' not in switch_block:
        default_anchor = re.search(r'\n(\s*default\s*:)', switch_block)
        if not default_anchor:
            print("[bluez] ERROR: found switch(att_ecode) but not default")
            sys.exit(3)
        indent = default_anchor.group(1).split('default')[0]
        patched = (
            switch_block[:default_anchor.start()]
            + f'\n{indent}case 0x00:\n{indent}\tgoto next;'
            + switch_block[default_anchor.start():]
        )
        body = body[:sm.start()] + patched + body[sm.end():]
        t = t[:m.start()] + head + body + tail + t[m.end():]
        f.write_text(t)
        print("[bluez] gatt-client secondary timeout quirk applied via switch(att_ecode).")
        sys.exit(0)

anchor = 'Secondary service discovery failed. ATT ECODE: 0x%02x'
idx = body.find(anchor)
if idx == -1:
    print("[bluez] ERROR: could not find secondary discovery failure anchor")
    sys.exit(4)

segment_start = body.rfind('\n', 0, idx)
segment_end = body.find('goto done;', idx)
if segment_end == -1:
    print("[bluez] ERROR: could not find goto done after secondary failure log")
    sys.exit(5)
segment_end = body.find('\n', segment_end)
if segment_end == -1:
    segment_end = len(body)

segment = body[segment_start:segment_end]

dbg_pat = re.compile(
    r'(DBG\(client->att,\s*"Secondary service discovery failed\. ATT ECODE: 0x%02x",\s*\n\s*att_ecode\);)',
    re.M
)
patched_segment, n = dbg_pat.subn(r'\1\n\n\tif (att_ecode == 0x00)\n\t\tgoto next;', segment, count=1)
if n != 1:
    print("[bluez] ERROR: could not patch secondary discovery failure block")
    sys.exit(6)

body = body[:segment_start] + patched_segment + body[segment_end:]
t = t[:m.start()] + head + body + tail + t[m.end():]
f.write_text(t)
print("[bluez] gatt-client secondary timeout quirk applied via failure block.")
PY
}

apply_patch_skip_secondary_discovery() {
  local f="${SRC_DIR}/bluez-${BLUEZ_VER}/src/shared/gatt-client.c"
  [[ -f "$f" ]] || { echo "ERROR: missing $f" >&2; exit 1; }

  if [[ "${JC2_SKIP_SECONDARY_DISCOVERY}" != "1" ]]; then
    echo "[bluez] Secondary discovery skip disabled."
    return 0
  fi

  echo "[bluez] Patching discover_primary_cb(): bypass secondary discovery, continue with included services"

  python3 - "$f" <<'PY'
import pathlib, re, sys

f = pathlib.Path(sys.argv[1])
t = f.read_text()

marker = 'JC2TRACE bypassing secondary service discovery; continuing with included services'
if marker in t:
    print("[bluez] secondary discovery bypass already present.")
    sys.exit(0)

m = re.search(
    r'(static void discover_primary_cb\s*\(.*?\)\s*\{)(.*?)(^\})',
    t,
    re.M | re.S,
)
if not m:
    print("[bluez] ERROR: could not find discover_primary_cb()")
    sys.exit(2)

head, body, tail = m.group(1), m.group(2), m.group(3)

# Ensure discover_primary_cb has a local handle_range declaration
decl_anchor = 'struct bt_gatt_iter iter;'
decl_needed = 'struct handle_range *range;'
if decl_needed not in body:
    if decl_anchor not in body:
        print("[bluez] ERROR: could not find iter declaration anchor in discover_primary_cb()")
        sys.exit(3)
    body = body.replace(
        decl_anchor,
        decl_anchor + '\n\tstruct handle_range *range;',
        1
    )

old = r'''secondary:
	/*
	 * Version 4.2 [Vol 1, Part A] page 101:
	 * A secondary service is a service that provides auxiliary
	 * functionality of a device and is referenced from at least one
	 * primary service on the device.
	 */
	if (queue_isempty(op->pending_svcs))
		goto done;

	/* Discover secondary services */
	client->discovery_req = bt_gatt_discover_secondary_services(client->att,
						NULL, op->start, op->end,
						discover_secondary_cb,
						discovery_op_ref(op),
						discovery_op_unref);
	if (client->discovery_req)
		return;

	DBG(client, "Failed to start secondary service discovery");

	discovery_op_unref(op);
	success = false;'''

new = r'''secondary:
	/*
	 * JC2 experiment:
	 * Bypass secondary service discovery entirely, but continue into the
	 * normal included-service discovery stage so characteristics can still
	 * be discovered and exported.
	 */
	if (queue_isempty(op->pending_svcs))
		goto done;

	if (op->svc_first > 0x0001)
		remove_discov_range(op, 1, op->svc_first - 1);
	if (op->svc_last < 0xffff)
		remove_discov_range(op, op->svc_last + 1, 0xffff);

	range = queue_peek_head(op->discov_ranges);
	if (!range) {
		DBG(client, "JC2TRACE no discovery range after bypassing secondary discovery");
		goto done;
	}

	DBG(client, "JC2TRACE bypassing secondary service discovery; continuing with included services");

	client->discovery_req = bt_gatt_discover_included_services(client->att,
						range->start,
						range->end,
						discover_incl_cb,
						discovery_op_ref(op),
						discovery_op_unref);
	if (client->discovery_req)
		return;

	DBG(client, "Failed to start included services discovery");

	discovery_op_unref(op);
	success = false;'''

if old not in body:
    print("[bluez] ERROR: could not find expected secondary block in discover_primary_cb()")
    sys.exit(4)

body = body.replace(old, new, 1)

new_t = t[:m.start()] + head + body + tail + t[m.end():]
f.write_text(new_t)
print("[bluez] discover_primary_cb() patched: secondary bypass now continues into included services.")
PY
}

apply_patch_att_timeout_cb() {
  local f="${SRC_DIR}/bluez-${BLUEZ_VER}/src/shared/att.c"
  [[ -f "$f" ]] || { echo "ERROR: missing $f" >&2; exit 1; }

  echo "[bluez] Rewriting att.c timeout_cb()..."

  python3 - "$f" "${JC2_TRACE_TIMEOUT_OPCODE10}" "${JC2_PATCH_ATT_SECONDARY_TIMEOUT}" <<'PY'
import pathlib, re, sys

f = pathlib.Path(sys.argv[1])
trace_timeout = sys.argv[2] == "1"
patch_secondary = sys.argv[3] == "1"
t = f.read_text()

m_timeout = re.search(r'^static bool timeout_cb\(void \*user_data\)\n\{', t, re.M)
if not m_timeout:
    print("[bluez] ERROR: could not find timeout_cb()")
    sys.exit(2)

decl = "static void wakeup_chan_writer(void *data, void *user_data);"
before = t[:m_timeout.start()]
if decl not in before:
    t = before + decl + "\n\n" + t[m_timeout.start():]

trace_block = r'''
	if (op->opcode == BT_ATT_OP_READ_BY_GRP_TYPE_REQ) {
		char hexbuf[256];
		const uint8_t *pdu = op->pdu;
		size_t i, pos = 0;

		memset(hexbuf, 0, sizeof(hexbuf));

		if (pdu) {
			for (i = 0; i < op->len && pos + 4 < sizeof(hexbuf); i++)
				pos += snprintf(hexbuf + pos, sizeof(hexbuf) - pos,
						"%02x ", pdu[i]);
		}

		DBG(att, "(chan %p) Operation timed out: opcode=0x%02x len=%u raw=[%s]",
		    chan, op->opcode, op->len, hexbuf);

		if (pdu && op->len >= 6) {
			uint16_t start0 = get_le16(pdu + 0);
			uint16_t end0 = get_le16(pdu + 2);
			uint16_t type0 = get_le16(pdu + 4);

			DBG(att,
			    "(chan %p) opcode=0x%02x decode@+0 start=0x%04x end=0x%04x group_type=0x%04x",
			    chan, op->opcode, start0, end0, type0);
		}

		if (pdu && op->len >= 7) {
			uint16_t start1 = get_le16(pdu + 1);
			uint16_t end1 = get_le16(pdu + 3);
			uint16_t type1 = get_le16(pdu + 5);

			DBG(att,
			    "(chan %p) opcode=0x%02x decode@+1 start=0x%04x end=0x%04x group_type=0x%04x",
			    chan, op->opcode, start1, end1, type1);
		}
	} else {
		DBG(att, "(chan %p) Operation timed out: 0x%02x", chan, op->opcode);
	}
'''

secondary_block = ""
if patch_secondary:
    secondary_block = r'''
	/*
	 * JC2 quirk:
	 * Only apply the synthetic Attribute Not Found response if the timed-out
	 * packet really decodes as a Secondary Service discovery request.
	 */
	if (op->opcode == BT_ATT_OP_READ_BY_GRP_TYPE_REQ &&
	    op->callback &&
	    op->pdu) {
		const uint8_t *pdu = op->pdu;
		const uint8_t *base = NULL;
		uint16_t start_handle = 0;
		uint16_t group_type = 0;

		if (op->len >= 7 && get_le16(pdu + 5) == 0x2801) {
			base = pdu + 1;
			start_handle = get_le16(base + 0);
			group_type = get_le16(base + 4);
		} else if (op->len >= 6 && get_le16(pdu + 4) == 0x2801) {
			base = pdu + 0;
			start_handle = get_le16(base + 0);
			group_type = get_le16(base + 4);
		}

		if (base && group_type == 0x2801) {
			uint8_t err_pdu[4];

			DBG(att, "(chan %p) quirk: converting secondary service timeout into Attribute Not Found", chan);

			err_pdu[0] = op->opcode;
			put_le16(start_handle, &err_pdu[1]);
			err_pdu[3] = BT_ATT_ERROR_ATTRIBUTE_NOT_FOUND;

			op->callback(BT_ATT_OP_ERROR_RSP, err_pdu, sizeof(err_pdu),
				     op->user_data);
			destroy_att_send_op(op);
			wakeup_chan_writer(chan, NULL);
			return false;
		}
	}
'''

replacement = f'''static bool timeout_cb(void *user_data)
{{
	struct timeout_data *timeout = user_data;
	struct bt_att_chan *chan = timeout->chan;
	struct bt_att *att = chan->att;
	struct att_send_op *op = NULL;

	if (chan->pending_req && chan->pending_req->id == timeout->id) {{
		op = chan->pending_req;
		chan->pending_req = NULL;
	}} else if (chan->pending_ind && chan->pending_ind->id == timeout->id) {{
		op = chan->pending_ind;
		chan->pending_ind = NULL;
	}}

	if (!op)
		return false;
{trace_block}
	op->timeout_id = 0;
{secondary_block}
	if (att->timeout_callback)
		att->timeout_callback(op->id, op->opcode, att->timeout_data);

	disc_att_send_op(op);

	io_shutdown(chan->io);

	return false;
}}'''

pat = re.compile(
    r'^static bool timeout_cb\(void \*user_data\)\n\{.*?^\}',
    re.M | re.S
)

new_t, n = pat.subn(replacement, t, count=1)
if n != 1:
    print("[bluez] ERROR: could not replace timeout_cb()")
    sys.exit(3)

f.write_text(new_t)
print(f"[bluez] att.c timeout_cb() rewritten. trace_timeout={trace_timeout} patch_secondary={patch_secondary}")
PY
}

apply_patch_device_trace() {
  local f="${SRC_DIR}/bluez-${BLUEZ_VER}/src/device.c"
  [[ -f "$f" ]] || { echo "ERROR: missing $f" >&2; exit 1; }

  echo "[bluez] Instrumenting device.c trace points..."

  python3 - "$f" <<'PY'
import pathlib, sys

f = pathlib.Path(sys.argv[1])
t = f.read_text()

def find_function(text, name):
    candidates = [
        f"static void {name}(",
        f"static DBusMessage *{name}(",
        f"void {name}(",
        f"bool {name}(",
        f"static bool {name}(",
    ]
    start = -1
    for c in candidates:
        start = text.find(c)
        if start != -1:
            break
    if start == -1:
        raise RuntimeError(f"could not find function for {name}")

    brace = text.find("{", start)
    if brace == -1:
        raise RuntimeError(f"could not find opening brace for {name}")

    depth = 0
    i = brace
    while i < len(text):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return start, i + 1, text[start:i + 1]
        i += 1

    raise RuntimeError(f"could not find closing brace for {name}")

def replace_function(name, mutator):
    global t
    s, e, func = find_function(t, name)
    new_func = mutator(func)
    if new_func == func:
        raise RuntimeError(f"no changes made while patching {name}")
    t = t[:s] + new_func + t[e:]

def patch_browse_request_cancel(func):
    if 'JC2TRACE browse_request_cancel type=' in func:
        return func
    anchor = 'DBG("");'
    repl = '''DBG("JC2TRACE browse_request_cancel type=%u msg=%p device=%s",
			req->type, req->msg, device->path);'''
    if anchor not in func:
        raise RuntimeError("browse_request_cancel DBG anchor missing")
    return func.replace(anchor, repl, 1)

def patch_device_request_disconnect(func):
    if 'JC2TRACE device_request_disconnect sender=' not in func:
        anchor = '{\n'
        repl = '''{
\tDBG("JC2TRACE device_request_disconnect sender=%s path=%s iface=%s member=%s type=%d serial=%u browse=%p bonding=%p connected=%d att_io=%p disconn_timer=%u",
\t\tmsg ? dbus_message_get_sender(msg) : "(null)",
\t\tmsg ? dbus_message_get_path(msg) : "(null)",
\t\tmsg ? dbus_message_get_interface(msg) : "(null)",
\t\tmsg ? dbus_message_get_member(msg) : "(null)",
\t\tmsg ? dbus_message_get_type(msg) : -1,
\t\tmsg ? dbus_message_get_serial(msg) : 0,
\t\tdevice->browse, device->bonding, btd_device_is_connected(device),
\t\tdevice->att_io, device->disconn_timer);

'''
        idx = func.find(anchor)
        if idx == -1:
            raise RuntimeError("device_request_disconnect function start anchor missing")
        func = func.replace(anchor, repl, 1)

    old = '''if (device->browse)
\t\tbrowse_request_cancel(device->browse);'''
    new = '''if (device->browse) {
\t\tDBG("JC2TRACE browse_request_cancel caller=device_request_disconnect");
\t\tbrowse_request_cancel(device->browse);
\t}'''
    if 'JC2TRACE browse_request_cancel caller=device_request_disconnect' not in func:
        if old not in func:
            raise RuntimeError("device_request_disconnect browse anchor missing")
        func = func.replace(old, new, 1)

    return func

def patch_dev_disconnect(func):
    if 'JC2TRACE dev_disconnect sender=' in func:
        return func
    old = '\tdevice_request_disconnect(device, msg);'
    new = '''\tDBG("JC2TRACE dev_disconnect sender=%s path=%s iface=%s member=%s type=%d serial=%u",
\t\tmsg ? dbus_message_get_sender(msg) : "(null)",
\t\tmsg ? dbus_message_get_path(msg) : "(null)",
\t\tmsg ? dbus_message_get_interface(msg) : "(null)",
\t\tmsg ? dbus_message_get_member(msg) : "(null)",
\t\tmsg ? dbus_message_get_type(msg) : -1,
\t\tmsg ? dbus_message_get_serial(msg) : 0);

\tdevice_request_disconnect(device, msg);'''
    if old not in func:
        raise RuntimeError("dev_disconnect call anchor missing")
    return func.replace(old, new, 1)

def patch_att_disconnected_cb(func):
    if 'JC2TRACE att_disconnected_cb err=%d browse=%p auto_connect=%d connected=%d' in func:
        return func
    old = 'DBG("");'
    new = '''DBG("");
\tDBG("JC2TRACE att_disconnected_cb err=%d browse=%p auto_connect=%d connected=%d",
\t\terr, device->browse, device_get_auto_connect(device),
\t\tbtd_device_is_connected(device));'''
    if old not in func:
        raise RuntimeError("att_disconnected_cb DBG anchor missing")
    return func.replace(old, new, 1)

try:
    replace_function("browse_request_cancel", patch_browse_request_cancel)
    replace_function("device_request_disconnect", patch_device_request_disconnect)
    replace_function("dev_disconnect", patch_dev_disconnect)
    replace_function("att_disconnected_cb", patch_att_disconnected_cb)
except RuntimeError as e:
    print(f"[bluez] ERROR: {e}")
    sys.exit(10)

f.write_text(t)
print("[bluez] device.c trace instrumentation applied.")
PY
}

verify_patches() {
  local device_f="${SRC_DIR}/bluez-${BLUEZ_VER}/src/device.c"
  local att_f="${SRC_DIR}/bluez-${BLUEZ_VER}/src/shared/att.c"
  local gatt_f="${SRC_DIR}/bluez-${BLUEZ_VER}/src/shared/gatt-client.c"

  echo "[bluez] Verifying patched source..."

  python3 - "$device_f" "${JC2_FORCE_UNPAIRED_MEDIUM}" <<'PY'
import pathlib, re, sys
f = pathlib.Path(sys.argv[1])
force_medium = sys.argv[2]
t = f.read_text()

m = re.search(
    r'if\s*\(\s*dev->le_state\.paired\s*\)\s*'
    r'\n\s*sec_level\s*=\s*(BT_IO_SEC_\w+)\s*;'
    r'\s*\n\s*else\s*'
    r'\n\s*sec_level\s*=\s*(BT_IO_SEC_\w+)\s*;',
    t
)
if not m:
    print("[bluez] ERROR: could not find sec_level block")
    sys.exit(10)

paired, unpaired = m.group(1), m.group(2)
expected = "BT_IO_SEC_MEDIUM" if force_medium == "1" else "BT_IO_SEC_LOW"
print(f"[bluez] device.c sec_level: paired={paired} unpaired={unpaired}")
if paired != "BT_IO_SEC_MEDIUM":
    print("[bluez] ERROR: paired sec_level wrong")
    sys.exit(11)
if unpaired != expected:
    print(f"[bluez] ERROR: unpaired sec_level wrong: {unpaired} != {expected}")
    sys.exit(12)
PY

  grep -qF 'JC2TRACE browse_request_cancel type=' "$device_f" || {
    echo "[bluez] ERROR: missing browse_request_cancel trace" >&2
    exit 20
  }

  grep -qF 'JC2TRACE browse_request_cancel caller=device_request_disconnect' "$device_f" || {
    echo "[bluez] ERROR: missing device_request_disconnect browse trace" >&2
    exit 21
  }

  grep -qF 'JC2TRACE dev_disconnect sender=' "$device_f" || {
    echo "[bluez] ERROR: missing dev_disconnect sender trace" >&2
    exit 22
  }

  grep -qF 'JC2TRACE device_request_disconnect sender=' "$device_f" || {
    echo "[bluez] ERROR: missing device_request_disconnect sender trace" >&2
    exit 23
  }

  grep -qF 'JC2TRACE att_disconnected_cb err=%d browse=%p auto_connect=%d connected=%d' "$device_f" || {
    echo "[bluez] ERROR: missing att_disconnected_cb trace" >&2
    exit 24
  }

  grep -qF 'static void wakeup_chan_writer(void *data, void *user_data);' "$att_f" || {
    echo "[bluez] ERROR: missing wakeup_chan_writer forward declaration" >&2
    exit 30
  }

  if [[ "${JC2_TRACE_TIMEOUT_OPCODE10}" == "1" ]]; then
    grep -qF 'group_type=0x%04x' "$att_f" || {
      echo "[bluez] ERROR: missing opcode 0x10 timeout field trace" >&2
      exit 31
    }
  fi

  if [[ "${JC2_PATCH_ATT_SECONDARY_TIMEOUT}" == "1" ]]; then
    grep -qF 'group_type == 0x2801' "$att_f" || {
      echo "[bluez] ERROR: missing 0x2801 timeout quirk" >&2
      exit 32
    }
    grep -qF 'BT_ATT_ERROR_ATTRIBUTE_NOT_FOUND' "$att_f" || {
      echo "[bluez] ERROR: missing Attribute Not Found quirk" >&2
      exit 33
    }
    grep -qF 'wakeup_chan_writer(chan, NULL)' "$att_f" || {
      echo "[bluez] ERROR: missing wakeup_chan_writer(chan, NULL)" >&2
      exit 34
    }
  fi

  if [[ "${JC2_PATCH_GATT_SECONDARY_TIMEOUT}" == "1" ]]; then
    grep -Eq 'case 0x00:|att_ecode == 0x00' "$gatt_f" || {
      echo "[bluez] ERROR: missing gatt-client secondary timeout quirk" >&2
      exit 40
    }
  fi

  if [[ "${JC2_SKIP_SECONDARY_DISCOVERY}" == "1" ]]; then
    grep -qF 'JC2TRACE bypassing secondary service discovery; continuing with included services' "$gatt_f" || {
      echo "[bluez] ERROR: missing secondary discovery bypass marker" >&2
      exit 41
    }
  fi

  echo "[bluez] Source verification passed."
}

show_patch_summary() {
  local device_f="${SRC_DIR}/bluez-${BLUEZ_VER}/src/device.c"
  local att_f="${SRC_DIR}/bluez-${BLUEZ_VER}/src/shared/att.c"
  local gatt_f="${SRC_DIR}/bluez-${BLUEZ_VER}/src/shared/gatt-client.c"

  echo
  echo "[bluez] Patch summary:"
  grep -n "sec_level =" "$device_f" | head -n 4 || true
  grep -n "JC2TRACE dev_disconnect sender=" "$device_f" || true
  grep -n "JC2TRACE device_request_disconnect sender=" "$device_f" || true
  grep -n "JC2TRACE browse_request_cancel" "$device_f" | head -n 6 || true
  grep -n "JC2TRACE att_disconnected_cb" "$device_f" || true
  grep -n "group_type=0x%04x" "$att_f" || true
  grep -n "group_type == 0x2801" "$att_f" || true
  grep -n "BT_ATT_ERROR_ATTRIBUTE_NOT_FOUND" "$att_f" || true
  grep -En "case 0x00:|att_ecode == 0x00" "$gatt_f" || true
  grep -n "JC2TRACE bypassing secondary service discovery; continuing with included services" "$gatt_f" || true
  echo
}

build_and_install() {
  rm -rf "${BLD_DIR}"
  mkdir -p "${BLD_DIR}"
  cd "${SRC_DIR}/bluez-${BLUEZ_VER}"

  local ical_libs
  ical_libs="$(pkg-config --libs libicalvcal)"

  echo "[bluez] Configuring..."
  ICAL_LIBS="${ical_libs}" ./configure \
    --prefix="${PREFIX}" \
    --sysconfdir=/etc \
    --localstatedir=/var \
    --enable-experimental \
    --disable-systemd

  echo "[bluez] Building..."
  make ICAL_LIBS="${ical_libs}" -j"$(nproc)"

  echo "[bluez] Installing to ${PREFIX}..."
  rm -rf "${PREFIX}"
  make install

  echo "[bluez] Installed bluetoothd at:"
  echo "  ${PREFIX}/libexec/bluetooth/bluetoothd"
}

main() {
  need_root
  ensure_deps
  fetch

  apply_patch_device_connect_le
  apply_patch_gatt_secondary_timeout
  apply_patch_skip_secondary_discovery
  apply_patch_att_timeout_cb
  apply_patch_device_trace
  verify_patches
  show_patch_summary
  build_and_install

  echo
  echo "[done] BlueZ ${BLUEZ_VER} built+installed to ${PREFIX}"
  echo "       GATT secondary timeout quirk: ${JC2_PATCH_GATT_SECONDARY_TIMEOUT}"
  echo "       ATT secondary timeout quirk:  ${JC2_PATCH_ATT_SECONDARY_TIMEOUT}"
  echo "       Trace timed-out opcode 0x10:  ${JC2_TRACE_TIMEOUT_OPCODE10}"
  echo "       Skip secondary discovery:     ${JC2_SKIP_SECONDARY_DISCOVERY}"
  echo "       Force unpaired MEDIUM:        ${JC2_FORCE_UNPAIRED_MEDIUM}"
}

main "$@"
