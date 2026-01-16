from __future__ import annotations

import asyncio
import statistics
import sys
import time

from dbus_next import Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType
from evdev import UInput, ecodes as e
try:
    from evdev import AbsInfo  # newer python-evdev
except Exception:
    from evdev.device import AbsInfo  # fallback


BLUEZ = "org.bluez"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
PROP_IFACE = "org.freedesktop.DBus.Properties"
DEVICE_IFACE = "org.bluez.Device1"
GATT_CHRC_IFACE = "org.bluez.GattCharacteristic1"

# ---- Joy-Con 2 known GATT UUIDs ----
DEFAULT_NOTIFY_UUID = "ab7de9be-89fe-49ad-828f-118f09df7fd2"
DEFAULT_CTRL_UUID = "649d4ac9-8eb7-4e6c-af44-1ea54fe5f005"

# ---- Optical decode ----
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
MOTION_HZ = 120.0
MOTION_MAX_PER_TICK = 60  # 60*120 = 7200/sec throughput
MOTION_IDLE_CUTOFF_S = 0.060  # if no motion for 60ms, start braking
MOTION_IDLE_BRAKE = 0.35  # keep 35% of backlog each tick when idle
MOTION_IDLE_ZERO = 1.0  # if backlog smaller than this, zero it

# ---- Stick location (bytes 13-15 = 3 bytes packed for X/Y 12-bit) ----
STICK_BASE_IDX = 13  # data[13], data[14], data[15]

# Stick calibration
STICK_CAL_SAMPLES = 25
STICK_DEADZONE_12 = 70
STICK_RECENTER_RADIUS = 25
STICK_RECENTER_ALPHA = 0.02

# Scroll tuning (mouse mode only)
SCROLL_MAX_LINES_PER_SEC = 20.0
SCROLL_CURVE_POWER = 1.6
SCROLL_MAX_STEP = 3

# ---- Button bitfields ----
BTN4 = 4
BTN5 = 5

# byte[4]
BTN_Y = 0x01
BTN_X = 0x02
BTN_B = 0x04
BTN_A = 0x08
BTN_SR = 0x10
BTN_SL = 0x20
BTN_L = 0x40   # (on Right JC2 this is effectively R)
BTN_ZL = 0x80  # (on Right JC2 this is effectively ZR)

# byte[5]
BTN_PLUS = 0x02
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


def _stderr(msg: str):
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


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

        # mode: "mouse" or "gamepad"
        self.mode = "mouse"
        self._prev_mode_btn = False

        # bringup / re-init serialization
        self._bringup_lock = asyncio.Lock()
        self._last_opt_warn_ts = 0.0

        # telemetry for one-line status
        self._notif_count = 0
        self._last_notif_ts = 0.0
        self._last_opt_ts = 0.0
        self._last_opt: bytes | None = None
        self._last_raw_b4 = 0
        self._last_raw_b5 = 0
        self._last_opt_active_ts = 0.0

        # stick telemetry (raw + decoded)
        self._last_stick_raw = (0, 0, 0)
        self._last_stick_x12 = 0
        self._last_stick_y12 = 0

        # stick calibration state
        self._stick_center_x12: int | None = None
        self._stick_center_y12: int | None = None
        self._stick_cal_x: list[int] = []
        self._stick_cal_y: list[int] = []

        # scroll accumulator + timing (mouse mode)
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

        # motion resampler state (mouse mode)
        self._last_motion_ts = 0.0
        self._dx_accum = 0.0
        self._dy_accum = 0.0
        self._pump_task: asyncio.Task | None = None

        # for status/debug correlation (minimal)
        self._last_opt_dx = 0
        self._last_opt_dy = 0
        self._last_emit_ix = 0
        self._last_emit_iy = 0

        # mouse button edge tracking
        self._prev_left = False
        self._prev_right = False
        self._prev_middle = False

        # mode toggle edge tracking
        self._prev_c = False

        # gamepad button tracking
        self._gp_prev = {
            "south": False,
            "east": False,
            "north": False,
            "west": False,
            "tl": False,
            "tr": False,
            "select": False,
            "start": False,
            "mode": False,
            "thumb": False,
        }

        # cached GATT interfaces for restart logic
        self._notify_props = None
        self._notify_ch = None
        self._ctrl_ch = None
        self._handler_installed = False

        self._dev = None
        self._dev_props = None


        # Mouse uinput
        self.ui_mouse = UInput(
            {
                e.EV_REL: [
                    e.REL_X,
                    e.REL_Y,
                    e.REL_WHEEL,
                    getattr(e, "REL_WHEEL_HI_RES", e.REL_WHEEL),
                ],
                e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE],
            },
            name="jc2mouse (mouse)",
        )

        # Gamepad uinput (Xbox-ish)
        self.ui_pad = UInput(
            {
                e.EV_KEY: [
                    e.BTN_SOUTH,  # A
                    e.BTN_EAST,   # B
                    e.BTN_NORTH,  # X
                    e.BTN_WEST,   # Y
                    e.BTN_TL,     # LB
                    e.BTN_TR,     # RB
                    e.BTN_SELECT, # Back
                    e.BTN_START,  # Start
                    e.BTN_MODE,   # Guide
                    e.BTN_THUMBL, # L3
                ],
                # IMPORTANT: for older evdev, ABS axes must be (code, AbsInfo(...)) tuples
                e.EV_ABS: [
                    (e.ABS_X, AbsInfo(32768, 0, 65535, 0, 512, 0)),
                    (e.ABS_Y, AbsInfo(32768, 0, 65535, 0, 512, 0)),
                ],
            },
            name="jc2mouse (gamepad)",
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

    async def _wait_services_resolved(self, timeout_s: float = 8.0) -> None:
        dev_intro = await self.bus.introspect(BLUEZ, self.dev_path)
        dev_obj = self.bus.get_proxy_object(BLUEZ, self.dev_path, dev_intro)
        props = dev_obj.get_interface("org.freedesktop.DBus.Properties")

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                v = await props.call_get(DEVICE_IFACE, "ServicesResolved")
                if bool(_unwrap(v)):
                    return
            except Exception:
                pass
            await asyncio.sleep(0.15)

        raise RuntimeError("Timed out waiting for ServicesResolved=True")

    async def _wait_first_notification(self, timeout_s: float = 2.0) -> bool:
        start_cnt = self._notif_count
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self._notif_count > start_cnt:
                return True
            await asyncio.sleep(0.05)
        return False

    @staticmethod
    def _optical_active(opt: bytes | None) -> bool:
        # optical stream "on" when any of bytes 1..4 are non-zero
        return opt is not None and any(b != 0 for b in opt[1:])

    async def _wait_optical_active(self, timeout_s: float = 2.0) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self._optical_active(self._last_opt):
                return True
            await asyncio.sleep(0.05)
        return False

    async def _refresh_objects_until_gatt(self, timeout_s: float = 60.0) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            self.objects = await self._get_managed_objects()

            found_notify = False
            found_ctrl = False

            for path, ifaces in self.objects.items():
                ch = ifaces.get(GATT_CHRC_IFACE)
                if not ch:
                    continue
                if self.dev_path and not path.startswith(self.dev_path + "/"):
                    continue

                uuid = str(_unwrap(ch.get("UUID", "")) or "").lower()
                if uuid == self.notify_uuid:
                    found_notify = True
                if uuid == self.ctrl_uuid:
                    found_ctrl = True

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

        self._dev = dev
        self._dev_props = props


        try:
            await props.call_set(DEVICE_IFACE, "Trusted", Variant("b", True))
        except Exception:
            pass

        _stderr("[jc2] Connecting...")
        try:
            await dev.call_connect()
        except Exception as ex:
            if "Already" not in str(ex) and "already" not in str(ex):
                raise

        _stderr("[jc2] Connected. Waiting for services...")
        await self._wait_services_resolved(timeout_s=8.0)
        _stderr("[jc2] Services resolved. Waiting for GATT discovery...")

        ok = await self._refresh_objects_until_gatt(timeout_s=60.0)
        if not ok:
            raise RuntimeError("Connected, but notify/control characteristics never appeared.")

        self._pick_characteristics()

    async def start(self):
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

    async def _cycle_connection_once(self) -> None:
        """Disconnect/connect once to un-wedge notify/write on rare BlueZ/device weirdness."""
        if self._dev is None:
            return

        _stderr("[jc2] Cycling device connection once...")
        try:
            await asyncio.wait_for(self._dev.call_disconnect(), timeout=2.0)
        except Exception:
            pass

        await asyncio.sleep(0.25)

        try:
            await asyncio.wait_for(self._dev.call_connect(), timeout=3.0)
        except Exception:
            # even if connect throws "AlreadyConnected", we can proceed
            pass

        # Wait for ServicesResolved again
        await self._wait_services_resolved(timeout_s=8.0)

        # Rebind notify/control proxies (disconnect can invalidate old ones)
        ch_intro = await self.bus.introspect(BLUEZ, self.notify_path)
        ch_obj = self.bus.get_proxy_object(BLUEZ, self.notify_path, ch_intro)
        self._notify_ch = ch_obj.get_interface(GATT_CHRC_IFACE)
        self._notify_props = ch_obj.get_interface(PROP_IFACE)

        ctrl_intro = await self.bus.introspect(BLUEZ, self.ctrl_path)
        ctrl_obj = self.bus.get_proxy_object(BLUEZ, self.ctrl_path, ctrl_intro)
        self._ctrl_ch = ctrl_obj.get_interface(GATT_CHRC_IFACE)

        # handler is already installed; no need to re-install

    async def ensure_notify_and_init(self):
        if self._notify_ch is None or self._ctrl_ch is None:
            raise RuntimeError("Driver not started yet (missing cached GATT interfaces).")

        async def _call_with_timeout(coro, timeout_s: float, label: str):
            try:
                return await asyncio.wait_for(coro, timeout=timeout_s)
            except asyncio.TimeoutError:
                raise RuntimeError(f"Timed out: {label}")

        async def safe_start_notify():
            try:
                await _call_with_timeout(self._notify_ch.call_start_notify(), 2.5, "StartNotify")
            except Exception as ex:
                msg = str(ex)
                # BlueZ sometimes reports this while enabling; treat as non-fatal.
                if "In Progress" not in msg and "InProgress" not in msg:
                    raise

        async def safe_stop_notify():
            try:
                await _call_with_timeout(self._notify_ch.call_stop_notify(), 2.0, "StopNotify")
            except Exception:
                pass

        async def write_cmd(hexstr: str):
            b = bytes.fromhex(hexstr)
            opts = {"type": Variant("s", "command")}
            await _call_with_timeout(self._ctrl_ch.call_write_value(b, opts), 2.5, f"WriteValue({hexstr[:8]}...)")

        async def send_optical_init():
            await write_cmd("0c91010200040000ff000000")
            await write_cmd("0c91010400040000ff000000")

        _stderr("[jc2] Enabling notifications + optical...")

        # If user doesn't move the controller, some firmwares can sit at zeros for a bit.
        # These attempts are tuned for "works reliably without hanging".
        attempts = [
            (0.10, False),
            (0.20, True),
            (0.35, True),
            (0.60, True),
        ]

        async with self._bringup_lock:
            # First pass: try normal cycles
            for i, (delay_s, do_cycle) in enumerate(attempts, 1):
                if do_cycle:
                    await safe_stop_notify()
                    await asyncio.sleep(0.05)

                await safe_start_notify()
                await asyncio.sleep(delay_s)

                await send_optical_init()

                # Confirm we are receiving notifications at all
                if not await self._wait_first_notification(timeout_s=1.25):
                    continue

                # Give optical a bit more time; small motions help
                if await self._wait_optical_active(timeout_s=2.25):
                    _stderr("[jc2] Optical stream active.")
                    return

                if self.verbose:
                    _stderr(f"[jc2][dbg] optical inactive on attempt {i}")

            _stderr("[jc2] Optical still inactive after bringup attempts; trying one reconnect cycle...")

            # Second pass: one hard reconnect + one more attempt set
            await self._cycle_connection_once()

            for i, (delay_s, do_cycle) in enumerate(attempts, 1):
                if do_cycle:
                    await safe_stop_notify()
                    await asyncio.sleep(0.05)

                await safe_start_notify()
                await asyncio.sleep(delay_s)
                await send_optical_init()

                if not await self._wait_first_notification(timeout_s=1.25):
                    continue

                if await self._wait_optical_active(timeout_s=2.25):
                    _stderr("[jc2] Optical stream active.")
                    return

        _stderr("[jc2] Init sent, but optical still appears inactive (opt bytes are zero).")


    # Mode switching helpers
    # -------------------------
    def _set_mode(self, new_mode: str):
        if new_mode == self.mode:
            return

        # release everything so we don't leave buttons stuck
        self._release_mouse_buttons()
        self._release_gamepad_buttons()

        # reset motion backlog
        self._dx_accum = 0.0
        self._dy_accum = 0.0
        self._wheel_accum = 0.0

        self.mode = new_mode
        _stderr(f"[jc2] Mode: {self.mode}")

    def _release_mouse_buttons(self):
        # release mouse buttons
        self.ui_mouse.write(e.EV_KEY, e.BTN_LEFT, 0)
        self.ui_mouse.write(e.EV_KEY, e.BTN_RIGHT, 0)
        self.ui_mouse.write(e.EV_KEY, e.BTN_MIDDLE, 0)
        self.ui_mouse.syn()
        self._prev_left = False
        self._prev_right = False
        self._prev_middle = False

    def _release_gamepad_buttons(self):
        # release all gamepad buttons + center stick
        self.ui_pad.write(e.EV_KEY, e.BTN_SOUTH, 0)
        self.ui_pad.write(e.EV_KEY, e.BTN_EAST, 0)
        self.ui_pad.write(e.EV_KEY, e.BTN_NORTH, 0)
        self.ui_pad.write(e.EV_KEY, e.BTN_WEST, 0)
        self.ui_pad.write(e.EV_KEY, e.BTN_TL, 0)
        self.ui_pad.write(e.EV_KEY, e.BTN_TR, 0)
        self.ui_pad.write(e.EV_KEY, e.BTN_SELECT, 0)
        self.ui_pad.write(e.EV_KEY, e.BTN_START, 0)
        self.ui_pad.write(e.EV_KEY, e.BTN_MODE, 0)
        self.ui_pad.write(e.EV_KEY, e.BTN_THUMBL, 0)

        self.ui_pad.write(e.EV_ABS, e.ABS_X, 32768)
        self.ui_pad.write(e.EV_ABS, e.ABS_Y, 32768)
        self.ui_pad.syn()

        for k in self._gp_prev:
            self._gp_prev[k] = False

    # -------------------------
    # Stick handling
    # -------------------------
    def _decode_stick_and_calibrate(self, data: bytes, now: float) -> tuple[int, int, int, int] | None:
        """
        Returns (x12, y12, cx, cy) once calibrated, else None.
        Maintains calibration / gentle recenter.
        """
        if len(data) <= STICK_BASE_IDX + 2:
            self._prev_notif_ts = now if self._prev_notif_ts is None else self._prev_notif_ts
            return None

        b0 = data[STICK_BASE_IDX]
        b1 = data[STICK_BASE_IDX + 1]
        b2 = data[STICK_BASE_IDX + 2]

        self._last_stick_raw = (b0, b1, b2)
        x12, y12 = decode_stick_12(b0, b1, b2)
        self._last_stick_x12 = x12
        self._last_stick_y12 = y12

        # dt for scroll (mouse mode)
        if self._prev_notif_ts is None:
            dt = 0.0
        else:
            dt = now - self._prev_notif_ts
        self._prev_notif_ts = now

        # calibration
        if self._stick_center_x12 is None or self._stick_center_y12 is None:
            self._stick_cal_x.append(x12)
            self._stick_cal_y.append(y12)
            if len(self._stick_cal_x) >= STICK_CAL_SAMPLES:
                self._stick_center_x12 = int(statistics.median(self._stick_cal_x))
                self._stick_center_y12 = int(statistics.median(self._stick_cal_y))
            return None

        # gentle recenter
        dx0 = x12 - self._stick_center_x12
        dy0 = y12 - self._stick_center_y12
        if abs(dx0) <= STICK_RECENTER_RADIUS and abs(dy0) <= STICK_RECENTER_RADIUS:
            cx = self._stick_center_x12 * (1.0 - STICK_RECENTER_ALPHA) + x12 * STICK_RECENTER_ALPHA
            cy = self._stick_center_y12 * (1.0 - STICK_RECENTER_ALPHA) + y12 * STICK_RECENTER_ALPHA
            self._stick_center_x12 = int(cx)
            self._stick_center_y12 = int(cy)

        return x12, y12, self._stick_center_x12, self._stick_center_y12

    def _emit_scroll_from_stick(self, x12: int, y12: int, cx: int, cy: int, now: float) -> bool:
        """Mouse mode scroll from stick Y deflection."""
        # Need dt
        if self._prev_notif_ts is None:
            return False

        # approximate dt since prev_notif_ts updated in _decode_stick_and_calibrate
        # (we can’t perfectly recover it here; good enough for smooth scroll)
        # optionally use small fixed dt based on typical notif rate
        dt = 1.0 / 40.0

        dy = y12 - cy
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
                self.ui_mouse.write(e.EV_REL, hires_code, hires_units)
                self._wheel_accum -= hires_units / 120.0
                wrote = True

        step = int(clamp(self._wheel_accum, -SCROLL_MAX_STEP, SCROLL_MAX_STEP))
        if step != 0:
            self.ui_mouse.write(e.EV_REL, e.REL_WHEEL, step)
            self._wheel_accum -= step
            wrote = True

        return wrote

    def _emit_gamepad_stick(self, x12: int, y12: int, cx: int, cy: int):
        """
        Gamepad mode: use stick as analog (rotated clockwise).
        We build a signed vector around center, invert Y so up is positive,
        then rotate clockwise: (x,y) -> (y, -x)
        """
        dx = x12 - cx
        dy = cy - y12  # invert so up is positive

        # deadzone
        if abs(dx) <= STICK_DEADZONE_12:
            dx = 0
        if abs(dy) <= STICK_DEADZONE_12:
            dy = 0

        # rotate clockwise
        rx = dy
        ry = -dx

        # clamp to range [-2048, +2048]
        rx = int(clamp(rx, -2048, 2048))
        ry = int(clamp(ry, -2048, 2048))

        # map to 0..65535 with center 32768
        ax = int(clamp(32768 + (rx * 32768 / 2048), 0, 65535))
        ay = int(clamp(32768 - (ry * 32768 / 2048), 0, 65535))  # ABS_Y down is +

        # FIX: invert X so left is left
        ax = 65535 - ax

        self.ui_pad.write(e.EV_ABS, e.ABS_X, ax)
        self.ui_pad.write(e.EV_ABS, e.ABS_Y, ay)
        self.ui_pad.syn()


    # -------------------------
    # Notification handling
    # -------------------------
    def handle_notification(self, data: bytes):
        now = time.time()
        self._notif_count += 1
        self._last_notif_ts = now

        # record button bytes (status)
        if len(data) > 4:
            self._last_raw_b4 = data[4]
        if len(data) > 5:
            self._last_raw_b5 = data[5]

        # --- Mode toggle (C button) edge-detect ---
        mode_btn = btn_pressed(data, BTN5, BTN_C)
        if mode_btn and not self._prev_mode_btn:
            new_mode = "gamepad" if self.mode == "mouse" else "mouse"
            self._set_mode(new_mode)

            # When entering gamepad mode, don't let optical watchdog think we're "dead"
            if self.mode == "gamepad":
                self._last_opt_active_ts = time.time()

        self._prev_mode_btn = mode_btn
 

        # --- Stick decode ---
        stick = self._decode_stick_and_calibrate(data, now)
        if stick is not None:
            x12, y12, cx, cy = stick
            if self.mode == "mouse":
                # wheel only in mouse mode
                if self._emit_scroll_from_stick(x12, y12, cx, cy, now):
                    self.ui_mouse.syn()
            else:
                # stick -> analog in gamepad mode
                self._emit_gamepad_stick(x12, y12, cx, cy)

        # --- Buttons ---
        # Face buttons raw
        a = btn_pressed(data, BTN4, BTN_A)
        b = btn_pressed(data, BTN4, BTN_B)
        x = btn_pressed(data, BTN4, BTN_X)
        y = btn_pressed(data, BTN4, BTN_Y)

        sr = btn_pressed(data, BTN4, BTN_SR)
        sl = btn_pressed(data, BTN4, BTN_SL)

        # "shoulder" + "trigger" (on Right JC2)
        sh = btn_pressed(data, BTN4, BTN_L)
        tr = btn_pressed(data, BTN4, BTN_ZL)

        home = btn_pressed(data, BTN5, BTN_HOME)
        r3 = btn_pressed(data, BTN5, BTN_R3)

        if self.mode == "mouse":
            # mouse clicks
            left = sh        # bumper -> left click
            right = tr       # trigger -> right click
            middle = r3      # stick click -> middle click

            if left != self._prev_left:
                self.ui_mouse.write(e.EV_KEY, e.BTN_LEFT, 1 if left else 0)
                self._prev_left = left

            if right != self._prev_right:
                self.ui_mouse.write(e.EV_KEY, e.BTN_RIGHT, 1 if right else 0)
                self._prev_right = right

            if middle != self._prev_middle:
                self.ui_mouse.write(e.EV_KEY, e.BTN_MIDDLE, 1 if middle else 0)
                self._prev_middle = middle

            # optical motion only in mouse mode
            self._handle_optical_motion(data, now)

            # sync for button changes (motion pump does its own syn)
            self.ui_mouse.syn()

        else:
            # GAMEPAD MODE
            #
            # Horizontal (clockwise) mapping:
            #   A -> B
            #   B -> Y
            #   Y -> X
            #   X -> A
            #
            # Then map virtual A/B/X/Y to Xbox-style BTN_SOUTH/EAST/NORTH/WEST:
            #   vA -> SOUTH, vB -> EAST, vX -> NORTH, vY -> WEST
            vA = x
            vB = a
            vX = y
            vY = b

            gp_south = vA
            gp_east  = vB
            gp_north = vX
            gp_west  = vY

            # shoulders: SL/SR -> LB/RB
            gp_tl = sl
            gp_tr = sr

            # + and HOME mapping
            plus = btn_pressed(data, BTN5, BTN_PLUS)
            gp_start  = plus          # +  -> START
            gp_select = home          # HOME -> SELECT/BACK

            # optional: R3 -> THUMBL
            gp_thumb = r3

            # disable shoulder/trigger (sh/tr)
            _ = sh
            _ = tr

            def emit_btn(keycode, name, pressed):
                prev = self._gp_prev[name]
                if pressed != prev:
                    self.ui_pad.write(e.EV_KEY, keycode, 1 if pressed else 0)
                    self._gp_prev[name] = pressed

            emit_btn(e.BTN_SOUTH,  "south",  gp_south)
            emit_btn(e.BTN_EAST,   "east",   gp_east)
            emit_btn(e.BTN_NORTH,  "north",  gp_north)
            emit_btn(e.BTN_WEST,   "west",   gp_west)
            emit_btn(e.BTN_TL,     "tl",     gp_tl)
            emit_btn(e.BTN_TR,     "tr",     gp_tr)
            emit_btn(e.BTN_SELECT, "select", gp_select)
            emit_btn(e.BTN_START,  "start",  gp_start)
            emit_btn(e.BTN_THUMBL, "thumb",  gp_thumb)

            self.ui_pad.syn()


    def _handle_optical_motion(self, data: bytes, now: float):
        # must have optical bytes
        if len(data) < OPT_OFFSET + OPT_LEN:
            return

        opt = data[OPT_OFFSET: OPT_OFFSET + OPT_LEN]
        self._last_opt = opt
        self._last_opt_ts = now
        if self._optical_active(opt):
            self._last_opt_active_ts = now

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

    async def start_motion_pump(self):
        if self._pump_task is not None:
            return

        async def _pump():
            period = 1.0 / MOTION_HZ

            # Drain behavior (close to perfect)
            DRAIN_FRACTION = 0.25
            MIN_PER_TICK = 1.0
            MAX_PER_TICK = float(MOTION_MAX_PER_TICK)

            while True:
                await asyncio.sleep(period)
                now = time.time()

                # only pump motion in mouse mode
                if self.mode != "mouse":
                    continue

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

                self.ui_mouse.write(e.EV_REL, e.REL_X, ix)
                self.ui_mouse.write(e.EV_REL, e.REL_Y, iy)
                self.ui_mouse.syn()

                # subtract exactly what we emitted
                self._dx_accum -= ix
                self._dy_accum -= iy

        self._pump_task = asyncio.create_task(_pump())


async def run(mac: str, *, status: bool = True, status_hz: float = 5.0, verbose: bool = False):
    drv = JC2OpticalMouse(mac, verbose=verbose)
    await drv.connect()
    await drv.start()
    await drv.start_motion_pump()

    _stderr("[jc2] Tip: press C to toggle mouse/gamepad mode.")

    last_print = 0.0
    last_count = 0
    last_restart = 0.0
    period = 1.0 / max(0.5, float(status_hz))

    while True:
        await asyncio.sleep(0.2)
        now = time.time()

        # watchdog: Only enforce optical bringup in MOUSE mode.
        if drv.mode == "mouse":
            age = (now - drv._last_notif_ts) if drv._last_notif_ts else 999.0

            if age < 0.5 and (now - drv._last_opt_active_ts) > 2.0:
                if (now - drv._last_opt_warn_ts) > 5.0:
                    drv._last_opt_warn_ts = now
                    sys.stderr.write("\n[jc2] WARNING: optical inactive; re-sending notify + optical init...\n")
                    sys.stderr.flush()

                if (now - last_restart) > 3.0:
                    last_restart = now
                    try:
                        async with drv._bringup_lock:
                            await drv.ensure_notify_and_init()
                    except Exception as ex:
                        if verbose:
                            sys.stderr.write(f"[jc2] optical restart failed: {ex}\n")
                            sys.stderr.flush()


        if not status:
            continue

        if now - last_print >= period:
            last_print = now

            cnt = drv._notif_count
            dt_rate = max(1e-6, period)
            rate = (cnt - last_count) / dt_rate
            last_count = cnt

            opt_age = (now - drv._last_opt_ts) if drv._last_opt_ts else 999.0
            opt = drv._last_opt
            opt_s = "?? ?? ?? ?? ??" if opt is None else " ".join(f"{b:02x}" for b in opt)

            b4 = drv._last_raw_b4
            b5 = drv._last_raw_b5

            sr0, sr1, sr2 = drv._last_stick_raw
            cx = drv._stick_center_x12
            cy = drv._stick_center_y12

            mode_ch = "M" if drv.mode == "mouse" else "G"

            sys.stderr.write(
                f"\r[jc2] mode={mode_ch} notifs={cnt:6d} rate={rate:5.1f}/s age={age:4.1f}s "
                f"opt_age={opt_age:4.1f}s b4=0x{b4:02x} b5=0x{b5:02x} "
                f"stick={sr0:02x}{sr1:02x}{sr2:02x} "
                f"sx12={drv._last_stick_x12:4d} sy12={drv._last_stick_y12:4d} "
                f"c=({cx if cx is not None else -1},{cy if cy is not None else -1}) "
                f"opt=[{opt_s}]   "
            )
            sys.stderr.flush()
