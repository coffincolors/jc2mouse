from __future__ import annotations

import asyncio
import statistics
import sys
import time

from dbus_next import Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType
from evdev import UInput, ecodes as e

BLUEZ = "org.bluez"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
PROP_IFACE = "org.freedesktop.DBus.Properties"
DEVICE_IFACE = "org.bluez.Device1"
GATT_CHRC_IFACE = "org.bluez.GattCharacteristic1"

# ---- Joy-Con 2 known GATT UUIDs (from your discovery logs) ----
DEFAULT_NOTIFY_UUID = "ab7de9be-89fe-49ad-828f-118f09df7fd2"
DEFAULT_CTRL_UUID = "649d4ac9-8eb7-4e6c-af44-1ea54fe5f005"

# ---- Optical decode (your proven logic) ----
OPT_OFFSET = 0x0F
OPT_LEN = 5
X_LO_IDX, X_HI_IDX = 1, 2
Y_LO_IDX, Y_HI_IDX = 3, 4

# Optical tuning
SENS_X = 1.0
SENS_Y = 1.0
DEADZONE = 2
MAX_STEP = 200
INVERT_X = False
INVERT_Y = False

# ---- Motion smoothing / resample ----
# (These are the "perfect" settings you ended up with)
MOTION_HZ = 120.0
MOTION_MAX_PER_TICK = 60  # 60*120 = 7200/sec throughput
MOTION_IDLE_CUTOFF_S = 0.060  # if no motion for 60ms, start braking
MOTION_IDLE_BRAKE = 0.35  # keep 35% of backlog each tick when idle
MOTION_IDLE_ZERO = 1.0  # if backlog smaller than this, zero it

# ---- Stick location (empirically: bytes 13-15 = 3 bytes packed for X/Y 12-bit) ----
STICK_BASE_IDX = 13  # data[13], data[14], data[15]

# Stick calibration + scroll tuning
STICK_CAL_SAMPLES = 25
STICK_DEADZONE_12 = 70
STICK_RECENTER_RADIUS = 25
STICK_RECENTER_ALPHA = 0.02

SCROLL_MAX_LINES_PER_SEC = 20.0
SCROLL_CURVE_POWER = 1.6
SCROLL_MAX_STEP = 3


# ---- Button bitfields (current best map) ----
BTN4 = 4
BTN5 = 5

# byte[4]
BTN_Y = 0x01
BTN_X = 0x02
BTN_B = 0x04
BTN_A = 0x08
BTN_SR = 0x10
BTN_SL = 0x20
BTN_L = 0x40
BTN_ZL = 0x80

# byte[5]
BTN_R3 = 0x04
BTN_HOME = 0x10
BTN_C = 0x40


def _unwrap(v):
    return getattr(v, "value", v)


def btn_pressed(data: bytes, byte_idx: int, mask: int) -> bool:
    return len(data) > byte_idx and (data[byte_idx] & mask) != 0


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def u16_from_opt(opt: bytes, lo_idx: int, hi_idx: int) -> int:
    return opt[lo_idx] | (opt[hi_idx] << 8)


def delta_u16(curr: int, prev: int) -> int:
    d = (curr - prev) & 0xFFFF
    return d - 0x10000 if d > 0x7FFF else d


def decode_stick_12(b0: int, b1: int, b2: int) -> tuple[int, int]:
    """
    Standard Nintendo-style 3-byte stick packing:
      x = b0 | ((b1 & 0x0F) << 8)
      y = (b1 >> 4) | (b2 << 4)
    Both are 12-bit (0..4095).
    """
    x = b0 | ((b1 & 0x0F) << 8)
    y = (b1 >> 4) | (b2 << 4)
    return x, y


class JC2OpticalMouse:
    def __init__(
        self,
        mac: str,
        notify_uuid: str | None = None,
        ctrl_uuid: str | None = None,
        verbose: bool = False,
    ):
        self.mac = mac.upper()
        self.notify_uuid = (notify_uuid or DEFAULT_NOTIFY_UUID).lower()
        self.ctrl_uuid = (ctrl_uuid or DEFAULT_CTRL_UUID).lower()
        self.verbose = verbose

        # telemetry for one-line status
        self._notif_count = 0
        self._last_notif_ts = 0.0
        self._last_opt_ts = 0.0
        self._last_opt: bytes | None = None
        self._last_raw_b4 = 0
        self._last_raw_b5 = 0

        # stick telemetry (raw + decoded)
        self._last_stick_raw = (0, 0, 0)
        self._last_stick_x12 = 0
        self._last_stick_y12 = 0

        # stick calibration state
        self._stick_center_x12: int | None = None
        self._stick_center_y12: int | None = None
        self._stick_cal_x: list[int] = []
        self._stick_cal_y: list[int] = []

        # scroll accumulator + timing
        self._wheel_accum = 0.0
        self._prev_notif_ts: float | None = None

        self.bus: MessageBus | None = None
        self.objects = None

        self.dev_path: str | None = None
        self.notify_path: str | None = None
        self.ctrl_path: str | None = None

        # optical state
        self.prev_x16: int | None = None
        self.prev_y16: int | None = None

        # motion resampler state
        self._last_motion_ts = 0.0
        self._dx_accum = 0.0
        self._dy_accum = 0.0
        self._pump_task: asyncio.Task | None = None

        # for status/debug correlation (minimal)
        self._last_opt_dx = 0
        self._last_opt_dy = 0
        self._last_emit_ix = 0
        self._last_emit_iy = 0

        # button edge tracking
        self._prev_left = False
        self._prev_right = False
        self._prev_middle = False

        # cached GATT interfaces for restart logic (avoid re-registering callbacks)
        self._notify_props = None
        self._notify_ch = None
        self._ctrl_ch = None
        self._handler_installed = False

        self.ui = UInput(
            {
                e.EV_REL: [
                    e.REL_X,
                    e.REL_Y,
                    e.REL_WHEEL,
                    getattr(e, "REL_WHEEL_HI_RES", e.REL_WHEEL),
                ],
                e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE],
            },
            name="jc2mouse (BlueZ D-Bus)",
        )

    async def _get_managed_objects(self):
        intro = await self.bus.introspect(BLUEZ, "/")
        om_obj = self.bus.get_proxy_object(BLUEZ, "/", intro)
        om = om_obj.get_interface(OM_IFACE)
        return await om.call_get_managed_objects()

    def _find_device_path(self):
        suffix = "dev_" + self.mac.replace(":", "_")
        for path, ifaces in self.objects.items():
            if path.endswith(suffix) and DEVICE_IFACE in ifaces:
                return path
        raise RuntimeError(
            f"Device {self.mac} not found in BlueZ object tree. "
            f"(If needed: connect once with bluetoothctl in this session.)"
        )

    def _pick_characteristics(self):
        notify_path = None
        ctrl_path = None

        for path, ifaces in self.objects.items():
            ch = ifaces.get(GATT_CHRC_IFACE)
            if not ch:
                continue
            if self.dev_path and not path.startswith(self.dev_path + "/"):
                continue

            uuid = str(_unwrap(ch.get("UUID", "")) or "").lower()
            if uuid == self.notify_uuid:
                notify_path = path
            if uuid == self.ctrl_uuid:
                ctrl_path = path

        if not notify_path:
            raise RuntimeError(f"Notify characteristic UUID not found yet: {self.notify_uuid}")
        if not ctrl_path:
            raise RuntimeError(f"Control characteristic UUID not found yet: {self.ctrl_uuid}")

        self.notify_path = notify_path
        self.ctrl_path = ctrl_path

    async def _refresh_objects_until_gatt(self, timeout_s: float = 60.0) -> bool:
        deadline = time.time() + timeout_s
        last_msg = 0.0

        while time.time() < deadline:
            self.objects = await self._get_managed_objects()

            found_notify = False
            found_ctrl = False
            total = 0

            for path, ifaces in self.objects.items():
                ch = ifaces.get(GATT_CHRC_IFACE)
                if not ch:
                    continue
                if self.dev_path and not path.startswith(self.dev_path + "/"):
                    continue

                total += 1
                uuid = str(_unwrap(ch.get("UUID", "")) or "").lower()
                if uuid == self.notify_uuid:
                    found_notify = True
                if uuid == self.ctrl_uuid:
                    found_ctrl = True

            now = time.time()
            if now - last_msg > 0.5:
                print(
                    f"[jc2] GATT chrc={total} have_notify={found_notify} have_ctrl={found_ctrl}",
                    file=sys.stderr,
                )
                last_msg = now

            if found_notify and found_ctrl:
                return True

            await asyncio.sleep(0.25)

        return False

    async def connect(self):
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self.objects = await self._get_managed_objects()
        self.dev_path = self._find_device_path()

        dev_intro = await self.bus.introspect(BLUEZ, self.dev_path)
        dev_obj = self.bus.get_proxy_object(BLUEZ, self.dev_path, dev_intro)
        dev = dev_obj.get_interface(DEVICE_IFACE)
        props = dev_obj.get_interface(PROP_IFACE)

        try:
            await props.call_set(DEVICE_IFACE, "Trusted", Variant("b", True))
        except Exception:
            pass

        print("[jc2] Connecting...", file=sys.stderr)
        await dev.call_connect()
        print("[jc2] Connected. Waiting for GATT discovery...", file=sys.stderr)

        ok = await self._refresh_objects_until_gatt(timeout_s=60.0)
        if not ok:
            raise RuntimeError("Connected, but notify/control characteristics never appeared.")

        self._pick_characteristics()
        print(f"[jc2] notify_path={self.notify_path}", file=sys.stderr)
        print(f"[jc2] ctrl_path={self.ctrl_path}", file=sys.stderr)

    async def start(self):
        """
        One-time setup: attach PropertiesChanged handler once, cache notify/control interfaces,
        then ensure notify is active + send optical init.
        """
        ch_intro = await self.bus.introspect(BLUEZ, self.notify_path)
        ch_obj = self.bus.get_proxy_object(BLUEZ, self.notify_path, ch_intro)
        self._notify_ch = ch_obj.get_interface(GATT_CHRC_IFACE)
        self._notify_props = ch_obj.get_interface(PROP_IFACE)

        ctrl_intro = await self.bus.introspect(BLUEZ, self.ctrl_path)
        ctrl_obj = self.bus.get_proxy_object(BLUEZ, self.ctrl_path, ctrl_intro)
        self._ctrl_ch = ctrl_obj.get_interface(GATT_CHRC_IFACE)

        if not self._handler_installed:

            def on_props_changed(_iface, changed, _invalidated):
                if "Value" in changed:
                    data = bytes(_unwrap(changed["Value"]))
                    self.handle_notification(data)

            self._notify_props.on_properties_changed(on_props_changed)
            self._handler_installed = True

        await self.ensure_notify_and_init()

    async def ensure_notify_and_init(self):
        if self._notify_ch is None or self._ctrl_ch is None:
            raise RuntimeError("Driver not started yet (missing cached GATT interfaces).")

        print("[jc2] StartNotify()", file=sys.stderr)
        await self._notify_ch.call_start_notify()

        async def write_cmd(hexstr: str):
            b = bytes.fromhex(hexstr)
            opts = {"type": Variant("s", "command")}
            await self._ctrl_ch.call_write_value(b, opts)

        print("[jc2] Sending optical init (FF)...", file=sys.stderr)
        await write_cmd("0c91010200040000ff000000")
        await write_cmd("0c91010400040000ff000000")
        print("[jc2] Init sent. Moving cursor should work if notifications are correct.", file=sys.stderr)

    def _update_stick(self, data: bytes, now: float) -> bool:
        if len(data) <= STICK_BASE_IDX + 2:
            self._prev_notif_ts = now if self._prev_notif_ts is None else self._prev_notif_ts
            return False

        b0 = data[STICK_BASE_IDX]
        b1 = data[STICK_BASE_IDX + 1]
        b2 = data[STICK_BASE_IDX + 2]

        self._last_stick_raw = (b0, b1, b2)
        x12, y12 = decode_stick_12(b0, b1, b2)
        self._last_stick_x12 = x12
        self._last_stick_y12 = y12

        # dt for rate-based scrolling
        if self._prev_notif_ts is None:
            dt = 0.0
        else:
            dt = now - self._prev_notif_ts
        self._prev_notif_ts = now

        # Calibration: collect samples then set median center
        if self._stick_center_x12 is None or self._stick_center_y12 is None:
            self._stick_cal_x.append(x12)
            self._stick_cal_y.append(y12)
            if len(self._stick_cal_x) >= STICK_CAL_SAMPLES:
                self._stick_center_x12 = int(statistics.median(self._stick_cal_x))
                self._stick_center_y12 = int(statistics.median(self._stick_cal_y))
            return False

        # Optional gentle recenter when near neutral
        dx0 = x12 - self._stick_center_x12
        dy0 = y12 - self._stick_center_y12
        if abs(dx0) <= STICK_RECENTER_RADIUS and abs(dy0) <= STICK_RECENTER_RADIUS:
            cx = self._stick_center_x12 * (1.0 - STICK_RECENTER_ALPHA) + x12 * STICK_RECENTER_ALPHA
            cy = self._stick_center_y12 * (1.0 - STICK_RECENTER_ALPHA) + y12 * STICK_RECENTER_ALPHA
            self._stick_center_x12 = int(cx)
            self._stick_center_y12 = int(cy)

        if dt <= 0.0:
            return False

        dy = y12 - self._stick_center_y12
        if abs(dy) <= STICK_DEADZONE_12:
            return False

        mag = min(abs(dy), 2048)
        norm = clamp((mag - STICK_DEADZONE_12) / max(1.0, (2048 - STICK_DEADZONE_12)), 0.0, 1.0)
        speed_lines_per_sec = (norm ** SCROLL_CURVE_POWER) * SCROLL_MAX_LINES_PER_SEC

        direction = 1.0 if dy > 0 else -1.0
        self._wheel_accum += direction * speed_lines_per_sec * dt

        wrote = False
        hires_code = getattr(e, "REL_WHEEL_HI_RES", None)
        if hires_code is not None:
            hires_units = int(self._wheel_accum * 120.0)
            hires_units = int(clamp(hires_units, -SCROLL_MAX_STEP * 120, SCROLL_MAX_STEP * 120))
            if hires_units != 0:
                self.ui.write(e.EV_REL, hires_code, hires_units)
                self._wheel_accum -= hires_units / 120.0
                wrote = True

        step = int(clamp(self._wheel_accum, -SCROLL_MAX_STEP, SCROLL_MAX_STEP))
        if step != 0:
            self.ui.write(e.EV_REL, e.REL_WHEEL, step)
            self._wheel_accum -= step
            wrote = True

        return wrote

    def handle_notification(self, data: bytes):
        now = time.time()
        self._notif_count += 1
        self._last_notif_ts = now

        # record button bytes (useful for status)
        if len(data) > 4:
            self._last_raw_b4 = data[4]
        if len(data) > 5:
            self._last_raw_b5 = data[5]

        did_any = False

        # scroll can happen even if optical slice is missing
        if self._update_stick(data, now):
            did_any = True

        # buttons (must work even if optical slice is missing)
        left = btn_pressed(data, BTN4, BTN_L)
        right = btn_pressed(data, BTN4, BTN_ZL)
        middle = btn_pressed(data, BTN5, BTN_R3)

        if left != self._prev_left:
            self.ui.write(e.EV_KEY, e.BTN_LEFT, 1 if left else 0)
            self._prev_left = left
            did_any = True

        if right != self._prev_right:
            self.ui.write(e.EV_KEY, e.BTN_RIGHT, 1 if right else 0)
            self._prev_right = right
            did_any = True

        if middle != self._prev_middle:
            self.ui.write(e.EV_KEY, e.BTN_MIDDLE, 1 if middle else 0)
            self._prev_middle = middle
            did_any = True

        # must have optical bytes to accumulate motion
        if len(data) < OPT_OFFSET + OPT_LEN:
            if did_any:
                self.ui.syn()
            return

        opt = data[OPT_OFFSET : OPT_OFFSET + OPT_LEN]
        self._last_opt = opt
        self._last_opt_ts = now

        x16 = u16_from_opt(opt, X_LO_IDX, X_HI_IDX)
        y16 = u16_from_opt(opt, Y_LO_IDX, Y_HI_IDX)

        if self.prev_x16 is None:
            self.prev_x16, self.prev_y16 = x16, y16
            dx = 0
            dy = 0
        else:
            dx = delta_u16(x16, self.prev_x16)
            dy = delta_u16(y16, self.prev_y16)
            self.prev_x16, self.prev_y16 = x16, y16

        if INVERT_X:
            dx = -dx
        if INVERT_Y:
            dy = -dy

        if abs(dx) <= DEADZONE:
            dx = 0
        if abs(dy) <= DEADZONE:
            dy = 0

        self._last_opt_dx = dx
        self._last_opt_dy = dy

        if dx != 0 or dy != 0:
            self._last_motion_ts = now

            mdx = float(clamp(dx * SENS_X, -MAX_STEP, MAX_STEP))
            mdy = float(clamp(dy * SENS_Y, -MAX_STEP, MAX_STEP))

            if mdx != 0.0 or mdy != 0.0:
                self._dx_accum += mdx
                self._dy_accum += mdy

        # only sync button/scroll writes here; motion is synced by the pump
        if did_any:
            self.ui.syn()

    async def start_motion_pump(self):
        if self._pump_task is not None:
            return

        async def _pump():
            period = 1.0 / MOTION_HZ

            # Drain behavior (this is the part that made it feel PERFECT)
            DRAIN_FRACTION = 0.25
            MIN_PER_TICK = 1.0
            MAX_PER_TICK = float(MOTION_MAX_PER_TICK)

            while True:
                await asyncio.sleep(period)
                now = time.time()

                ax = float(self._dx_accum)
                ay = float(self._dy_accum)

                if abs(ax) < 0.1 and abs(ay) < 0.1:
                    continue

                # Idle braking so it STOPS NOW
                if (now - self._last_motion_ts) > MOTION_IDLE_CUTOFF_S:
                    self._dx_accum *= MOTION_IDLE_BRAKE
                    self._dy_accum *= MOTION_IDLE_BRAKE

                    if abs(self._dx_accum) < MOTION_IDLE_ZERO:
                        self._dx_accum = 0.0
                    if abs(self._dy_accum) < MOTION_IDLE_ZERO:
                        self._dy_accum = 0.0

                    if abs(self._dx_accum) < 0.1 and abs(self._dy_accum) < 0.1:
                        continue

                    ax = float(self._dx_accum)
                    ay = float(self._dy_accum)

                # Spread backlog across multiple ticks (smooth)
                mag = (ax * ax + ay * ay) ** 0.5

                per_tick = mag * DRAIN_FRACTION
                per_tick = clamp(per_tick, MIN_PER_TICK, MAX_PER_TICK)

                if mag > 0.0:
                    out_dx = ax * (per_tick / mag)
                    out_dy = ay * (per_tick / mag)
                else:
                    out_dx = 0.0
                    out_dy = 0.0

                ix = int(round(out_dx))
                iy = int(round(out_dy))

                self._last_emit_ix = ix
                self._last_emit_iy = iy

                if ix == 0 and iy == 0:
                    continue

                self.ui.write(e.EV_REL, e.REL_X, ix)
                self.ui.write(e.EV_REL, e.REL_Y, iy)
                self.ui.syn()

                # subtract exactly what we emitted (integer residue handling)
                self._dx_accum -= ix
                self._dy_accum -= iy

        self._pump_task = asyncio.create_task(_pump())


async def run(mac: str, *, status: bool = True, status_hz: float = 5.0, verbose: bool = False):
    drv = JC2OpticalMouse(mac, verbose=verbose)
    await drv.connect()
    await drv.start()
    await drv.start_motion_pump()

    last_print = 0.0
    last_count = 0
    last_restart = 0.0
    period = 1.0 / max(0.5, float(status_hz))

    while True:
        await asyncio.sleep(0.2)
        now = time.time()

        # watchdog: if no notifications for 2s, try to re-StartNotify + re-init
        age = (now - drv._last_notif_ts) if drv._last_notif_ts else 999.0
        if age > 2.0 and (now - last_restart) > 3.0:
            last_restart = now
            sys.stderr.write("\n[jc2] WARNING: notifications stalled; re-sending StartNotify + init...\n")
            sys.stderr.flush()
            try:
                await drv.ensure_notify_and_init()
            except Exception as ex:
                sys.stderr.write(f"[jc2] restart failed: {ex}\n")
                sys.stderr.flush()

        if not status:
            continue

        if now - last_print >= period:
            last_print = now

            cnt = drv._notif_count
            rate = (cnt - last_count) / max(1e-6, (now - (last_print - period)))
            last_count = cnt

            opt_age = (now - drv._last_opt_ts) if drv._last_opt_ts else 999.0
            opt = drv._last_opt
            opt_s = "?? ?? ?? ?? ??" if opt is None else " ".join(f"{b:02x}" for b in opt)

            b4 = drv._last_raw_b4
            b5 = drv._last_raw_b5

            sr0, sr1, sr2 = drv._last_stick_raw
            cx = drv._stick_center_x12
            cy = drv._stick_center_y12

            # concise one-line status (won't scroll unless your terminal wraps due to width)
            sys.stderr.write(
                f"\r[jc2] notifs={cnt:6d} rate={rate:5.1f}/s age={age:4.1f}s "
                f"opt_age={opt_age:4.1f}s b4=0x{b4:02x} b5=0x{b5:02x} "
                f"stick={sr0:02x}{sr1:02x}{sr2:02x} "
                f"sx12={drv._last_stick_x12:4d} sy12={drv._last_stick_y12:4d} "
                f"c=({cx if cx is not None else -1},{cy if cy is not None else -1}) "
                f"opt=[{opt_s}]   "
            )
            sys.stderr.flush()
