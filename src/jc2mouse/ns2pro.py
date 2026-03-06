from __future__ import annotations

import argparse
import asyncio
import inspect
import importlib.metadata as _ilmd
import statistics
import sys
import time

from dbus_next import Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType
from evdev import UInput, ecodes as e

try:
    from evdev import AbsInfo
except Exception:
    from evdev.device import AbsInfo


BLUEZ = "org.bluez"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
PROP_IFACE = "org.freedesktop.DBus.Properties"
DEVICE_IFACE = "org.bluez.Device1"
GATT_CHRC_IFACE = "org.bluez.GattCharacteristic1"

# Nintendo manufacturer data
NINTENDO_COMPANY_ID = 0x0553
NS2_MFG_LEN = 24
NS2_MFG_PREFIX = bytes.fromhex("01 00 03 7e 05")
NS2_SUBTYPE_IDX = 5
NS2_SUBTYPE_PRO = 0x69

# Known working GATT UUIDs
DEFAULT_NOTIFY_UUID = "ab7de9be-89fe-49ad-828f-118f09df7fd2"
DEFAULT_CTRL_UUID = "649d4ac9-8eb7-4e6c-af44-1ea54fe5f005"

# ---- Packet layout baseline for Switch 2 Pro ----
# These are the current best-effort defaults based on your findings.
# They are deliberately centralized so future remaps are easy.

# Buttons:
# b4: face + shoulder cluster
# b5: misc / system / stick-click cluster
# b6: dpad cluster
BTN4 = 4
BTN5 = 5
BTN6 = 6
BTN7 = 7

# Face cluster on byte 4
BIT_Y = 0x01   # north
BIT_X = 0x02   # east? / north label overlap family
BIT_B = 0x04   # south
BIT_A = 0x08   # east

# Shoulder / trigger mapping for Switch 2 Pro (based on your testing)
# Right side lives on b4 high bits
BIT_R_SHOULDER_B4 = 0x40
BIT_R_TRIGGER_B4  = 0x80

# Left side lives on b6 high bits
BIT_L_SHOULDER_B6 = 0x40
BIT_L_TRIGGER_B6  = 0x80

# Misc/system on byte 5
BIT_SELECT = 0x01   # minus
BIT_START  = 0x02   # plus
BIT_THUMBR = 0x04   # current best guess
BIT_THUMBL = 0x08   # current best guess
BIT_HOME   = 0x10
BIT_CAPTURE = 0x20  # optional / debug
BIT_C = 0x40        # ignored for Pro mode

# D-pad on byte 6
BIT_DDOWN  = 0x01
BIT_DUP    = 0x02
BIT_DRIGHT = 0x04
BIT_DLEFT  = 0x08

# Candidate stick locations
# These mirror the family layout you've already been using successfully enough
# to get meaningful movement on the Pro Controller.
LEFT_STICK_BASE = 10
RIGHT_STICK_BASE = 13

STICK_DEADZONE_12 = 70
STICK_RECENTER_RADIUS = 25
STICK_RECENTER_ALPHA = 0.02

# ---- Compatibility / mapping tweaks ----
INVERT_LEFT_STICK_Y = True
INVERT_RIGHT_STICK_Y = True

# Steam often expects Xbox X/Y positions rather than Nintendo printed labels.
# Set this True if Steam shows X and Y flipped.
COMPAT_SWAP_WEST_NORTH = True

# same 12-bit Nintendo packing
def decode_stick_12(b0: int, b1: int, b2: int) -> tuple[int, int]:
    x = b0 | ((b1 & 0x0F) << 8)
    y = (b1 >> 4) | (b2 << 4)
    return x, y

def _unwrap(v):
    return getattr(v, "value", v)

def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v

def _stderr(msg: str):
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()

def _uinput_ctor_kwargs_supported() -> set[str]:
    try:
        return set(inspect.signature(UInput.__init__).parameters.keys())
    except Exception:
        return {
            "events", "name", "vendor", "product", "version",
            "bustype", "devnode", "phys", "input_props", "max_effects"
        }

def mk_uinput(caps: dict, *, verbose: bool = False, **kwargs):
    supported = _uinput_ctor_kwargs_supported()
    filtered = {k: v for k, v in kwargs.items() if k in supported}

    if verbose:
        dropped = sorted(set(kwargs.keys()) - set(filtered.keys()))
        try:
            ver = _ilmd.version("evdev")
        except Exception:
            ver = "unknown"
        _stderr(f"[ns2pro][dbg] evdev={ver} supported={sorted(supported)} dropped={dropped}")

    return UInput(caps, **filtered)

def _abs_from_signed(v: int) -> int:
    return int(clamp(32768 + (v * 32768 / 2048), 0, 65535))

def _normalize_stick(x12: int, y12: int, cx: int, cy: int) -> tuple[int, int]:
    dx = x12 - cx
    dy = y12 - cy  # down positive for normal full-controller mapping

    if abs(dx) <= STICK_DEADZONE_12:
        dx = 0
    if abs(dy) <= STICK_DEADZONE_12:
        dy = 0

    dx = int(clamp(dx, -2048, 2048))
    dy = int(clamp(dy, -2048, 2048))
    return dx, dy

class _StickCal:
    def __init__(self, base: int):
        self.base = base
        self.center_x: int | None = None
        self.center_y: int | None = None
        self.cal_x: list[int] = []
        self.cal_y: list[int] = []
        self.last_raw = (0, 0, 0)
        self.last_x12 = 0
        self.last_y12 = 0

    def feed(self, data: bytes) -> tuple[int, int, int, int] | None:
        if len(data) <= self.base + 2:
            return None

        b0 = data[self.base]
        b1 = data[self.base + 1]
        b2 = data[self.base + 2]
        self.last_raw = (b0, b1, b2)

        x12, y12 = decode_stick_12(b0, b1, b2)
        self.last_x12 = x12
        self.last_y12 = y12

        if self.center_x is None or self.center_y is None:
            self.cal_x.append(x12)
            self.cal_y.append(y12)
            if len(self.cal_x) >= 8:
                self.center_x = int(statistics.median(self.cal_x))
                self.center_y = int(statistics.median(self.cal_y))
                return x12, y12, self.center_x, self.center_y
            return None

        dx0 = x12 - self.center_x
        dy0 = y12 - self.center_y
        if abs(dx0) <= STICK_RECENTER_RADIUS and abs(dy0) <= STICK_RECENTER_RADIUS:
            cx = self.center_x * (1.0 - STICK_RECENTER_ALPHA) + x12 * STICK_RECENTER_ALPHA
            cy = self.center_y * (1.0 - STICK_RECENTER_ALPHA) + y12 * STICK_RECENTER_ALPHA
            self.center_x = int(cx)
            self.center_y = int(cy)

        return x12, y12, self.center_x, self.center_y

class NS2ProGamepad:
    def __init__(
        self,
        mac: str,
        *,
        notify_uuid: str | None = None,
        ctrl_uuid: str | None = None,
        verbose: bool = False,
    ):
        self.mac = mac.upper()
        self.notify_uuid = (notify_uuid or DEFAULT_NOTIFY_UUID).lower()
        self.ctrl_uuid = (ctrl_uuid or DEFAULT_CTRL_UUID).lower()
        self.verbose = verbose

        self.bus: MessageBus | None = None
        self.objects = None

        self.dev_path: str | None = None
        self.notify_path: str | None = None
        self.ctrl_path: str | None = None

        self._dev = None
        self._dev_props = None
        self._notify_props = None
        self._notify_ch = None
        self._ctrl_ch = None
        self._handler_installed = False

        self.notif_count = 0
        self.last_notif_ts = 0.0

        self.last_b4 = 0
        self.last_b5 = 0
        self.last_b6 = 0
        self.last_b7 = 0

        self.left_stick = _StickCal(LEFT_STICK_BASE)
        self.right_stick = _StickCal(RIGHT_STICK_BASE)

        UINPUT_BUSTYPE = getattr(e, "BUS_VIRTUAL", 0x06)
        UINPUT_VENDOR  = 0x045E
        UINPUT_PRODUCT = 0x028E
        UINPUT_VERSION = 0x0001

        self.ui = mk_uinput(
            {
                e.EV_KEY: [
                    e.BTN_SOUTH, e.BTN_EAST, e.BTN_WEST, e.BTN_NORTH,
                    e.BTN_TL, e.BTN_TR,
                    getattr(e, "BTN_TL2", e.BTN_TL),
                    getattr(e, "BTN_TR2", e.BTN_TR),
                    e.BTN_SELECT, e.BTN_START, e.BTN_MODE,
                    e.BTN_THUMBL, getattr(e, "BTN_THUMBR", e.BTN_THUMBL),
                    e.BTN_DPAD_UP, e.BTN_DPAD_DOWN, e.BTN_DPAD_LEFT, e.BTN_DPAD_RIGHT,
                    getattr(e, "BTN_MISC", e.BTN_MODE),
                ],
                e.EV_ABS: [
                    (e.ABS_X, AbsInfo(32768, 0, 65535, 0, 512, 0)),
                    (e.ABS_Y, AbsInfo(32768, 0, 65535, 0, 512, 0)),
                    (getattr(e, "ABS_RX", e.ABS_X), AbsInfo(32768, 0, 65535, 0, 512, 0)),
                    (getattr(e, "ABS_RY", e.ABS_Y), AbsInfo(32768, 0, 65535, 0, 512, 0)),
                ],
            },
            name="jc2mouse (ns2 pro)",
            bustype=UINPUT_BUSTYPE,
            vendor=UINPUT_VENDOR,
            product=UINPUT_PRODUCT,
            version=UINPUT_VERSION,
            phys=f"jc2mouse/{self.mac}/ns2pro",
            verbose=self.verbose,
        )

        self.prev_btn = {
            "south": False, "east": False, "west": False, "north": False,
            "tl": False, "tr": False, "tl2": False, "tr2": False,
            "select": False, "start": False, "mode": False,
            "thumbl": False, "thumbr": False,
            "dup": False, "ddown": False, "dleft": False, "dright": False,
            "misc": False,
        }
        self.prev_axes = {
            "lx": 32768, "ly": 32768,
            "rx": 32768, "ry": 32768,
        }

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
        raise RuntimeError(f"Device {self.mac} not found in BlueZ object tree.")

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
            raise RuntimeError(f"Notify characteristic UUID not found: {self.notify_uuid}")
        if not ctrl_path:
            raise RuntimeError(f"Control characteristic UUID not found: {self.ctrl_uuid}")

        self.notify_path = notify_path
        self.ctrl_path = ctrl_path

    async def _validate_is_pro(self, props):
        try:
            md = await props.call_get(DEVICE_IFACE, "ManufacturerData")
            md = _unwrap(md)
            if not isinstance(md, dict) or NINTENDO_COMPANY_ID not in md:
                _stderr("[ns2pro] WARNING: ManufacturerData missing; continuing anyway.")
                return

            raw = _unwrap(md[NINTENDO_COMPANY_ID])
            mfg = bytes(raw)

            if len(mfg) != NS2_MFG_LEN or not mfg.startswith(NS2_MFG_PREFIX):
                _stderr("[ns2pro] WARNING: ManufacturerData prefix unexpected; continuing anyway.")
                return

            subtype = mfg[NS2_SUBTYPE_IDX]
            if subtype != NS2_SUBTYPE_PRO:
                _stderr(f"[ns2pro] WARNING: subtype byte is 0x{subtype:02x}, not 0x69; continuing anyway.")
            elif self.verbose:
                _stderr("[ns2pro][dbg] confirmed subtype 0x69 (Switch 2 Pro Controller)")
        except Exception as ex:
            _stderr(f"[ns2pro] WARNING: could not validate ManufacturerData: {ex}")

    async def _wait_services_resolved(self, timeout_s: float = 25.0):
        dev_intro = await self.bus.introspect(BLUEZ, self.dev_path)
        dev_obj = self.bus.get_proxy_object(BLUEZ, self.dev_path, dev_intro)
        props = dev_obj.get_interface(PROP_IFACE)

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                v = await props.call_get(DEVICE_IFACE, "ServicesResolved")
                if bool(_unwrap(v)):
                    return
            except Exception:
                pass
            await asyncio.sleep(0.20)

        raise RuntimeError("Timed out waiting for ServicesResolved=True")

    async def _refresh_objects_until_gatt(self, timeout_s: float = 60.0):
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
                return

            await asyncio.sleep(0.25)

        raise RuntimeError("Connected, but notify/control characteristics never appeared.")

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

        await self._validate_is_pro(props)

        try:
            await props.call_set(DEVICE_IFACE, "Trusted", Variant("b", True))
        except Exception:
            pass

        _stderr("[ns2pro] Connecting...")
        try:
            await dev.call_connect()
        except Exception as ex:
            s = str(ex)
            if "Already" not in s and "already" not in s:
                raise

        _stderr("[ns2pro] Connected. Waiting for services...")
        await self._wait_services_resolved(timeout_s=25.0)
        _stderr("[ns2pro] Services resolved. Waiting for GATT discovery...")

        await self._refresh_objects_until_gatt(timeout_s=60.0)
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

    async def ensure_notify_and_init(self):
        async def safe_start_notify():
            try:
                await self._notify_ch.call_start_notify()
            except Exception as ex:
                msg = str(ex)
                if "In Progress" not in msg and "InProgress" not in msg:
                    raise

        async def write_cmd(hexstr: str):
            b = bytes.fromhex(hexstr)
            opts = {"type": Variant("s", "command")}
            await self._ctrl_ch.call_write_value(b, opts)

        # Keeping the same init that already works for you.
        async def send_init():
            await write_cmd("0c91010200040000ff000000")
            await write_cmd("0c91010400040000ff000000")

        await safe_start_notify()
        await asyncio.sleep(0.20)
        await send_init()
        _stderr("[ns2pro] Notifications enabled. Full gamepad mode active.")

    def _emit_btn(self, keycode: int, name: str, pressed: bool):
        prev = bool(self.prev_btn.get(name, False))
        pressed = bool(pressed)
        if prev == pressed:
            return
        self.ui.write(e.EV_KEY, keycode, 1 if pressed else 0)
        self.prev_btn[name] = pressed

    def _emit_axis(self, code: int, name: str, value: int):
        prev = int(self.prev_axes.get(name, 32768))
        value = int(value)
        if prev == value:
            return
        self.ui.write(e.EV_ABS, code, value)
        self.prev_axes[name] = value

    def _emit_state(self):
        self.ui.syn()

    def handle_notification(self, data: bytes):
        self.notif_count += 1
        self.last_notif_ts = time.time()

        if len(data) > BTN4:
            self.last_b4 = data[BTN4]
        if len(data) > BTN5:
            self.last_b5 = data[BTN5]
        if len(data) > BTN6:
            self.last_b6 = data[BTN6]
        if len(data) > BTN7:
            self.last_b7 = data[BTN7]

        # ----- sticks -----
        l = self.left_stick.feed(data)
        r = self.right_stick.feed(data)

        if l is not None:
            x12, y12, cx, cy = l
            dx, dy = _normalize_stick(x12, y12, cx, cy)

            if INVERT_LEFT_STICK_Y:
                dy = -dy

            self._emit_axis(e.ABS_X, "lx", _abs_from_signed(dx))
            self._emit_axis(e.ABS_Y, "ly", _abs_from_signed(dy))

        ABS_RX = getattr(e, "ABS_RX", e.ABS_X)
        ABS_RY = getattr(e, "ABS_RY", e.ABS_Y)

        if r is not None:
            x12, y12, cx, cy = r
            dx, dy = _normalize_stick(x12, y12, cx, cy)

            if INVERT_RIGHT_STICK_Y:
                dy = -dy

            self._emit_axis(ABS_RX, "rx", _abs_from_signed(dx))
            self._emit_axis(ABS_RY, "ry", _abs_from_signed(dy))

        # ----- face buttons -----
        b4 = self.last_b4
        b5 = self.last_b5
        b6 = self.last_b6

        # Nintendo physical positions -> Xbox positional buttons
        south = bool(b4 & BIT_B)   # physical south
        east  = bool(b4 & BIT_A)   # physical east
        west  = bool(b4 & BIT_Y)   # physical west
        north = bool(b4 & BIT_X)   # physical north

        if COMPAT_SWAP_WEST_NORTH:
            west, north = north, west

        self._emit_btn(e.BTN_SOUTH, "south", south)
        self._emit_btn(e.BTN_EAST,  "east",  east)
        self._emit_btn(e.BTN_WEST,  "west",  west)
        self._emit_btn(e.BTN_NORTH, "north", north)

        # shoulders / triggers
        BTN_TL2 = getattr(e, "BTN_TL2", e.BTN_TL)
        BTN_TR2 = getattr(e, "BTN_TR2", e.BTN_TR)
        BTN_THUMBR = getattr(e, "BTN_THUMBR", e.BTN_THUMBL)
        BTN_MISC = getattr(e, "BTN_MISC", e.BTN_MODE)

        # Based on current confirmed behavior:
        #   Right bumper  -> b4 & 0x40
        #   Right trigger -> b4 & 0x80
        #   Left bumper   -> b6 & 0x40
        #   Left trigger  -> b6 & 0x80
        self._emit_btn(e.BTN_TL,  "tl",   bool(b6 & BIT_L_SHOULDER_B6))
        self._emit_btn(BTN_TL2,   "tl2",  bool(b6 & BIT_L_TRIGGER_B6))
        self._emit_btn(e.BTN_TR,  "tr",   bool(b4 & BIT_R_SHOULDER_B4))
        self._emit_btn(BTN_TR2,   "tr2",  bool(b4 & BIT_R_TRIGGER_B4))

        # misc/system
        self._emit_btn(e.BTN_SELECT, "select", bool(b5 & BIT_SELECT))
        self._emit_btn(e.BTN_START,  "start",  bool(b5 & BIT_START))
        self._emit_btn(e.BTN_MODE,   "mode",   bool(b5 & BIT_HOME))
        self._emit_btn(e.BTN_THUMBL, "thumbl", bool(b5 & BIT_THUMBL))
        self._emit_btn(BTN_THUMBR,   "thumbr", bool(b5 & BIT_THUMBR))
        self._emit_btn(BTN_MISC,     "misc",   bool(b5 & BIT_CAPTURE))

        # dpad
        self._emit_btn(e.BTN_DPAD_UP,    "dup",    bool(b6 & BIT_DUP))
        self._emit_btn(e.BTN_DPAD_DOWN,  "ddown",  bool(b6 & BIT_DDOWN))
        self._emit_btn(e.BTN_DPAD_LEFT,  "dleft",  bool(b6 & BIT_DLEFT))
        self._emit_btn(e.BTN_DPAD_RIGHT, "dright", bool(b6 & BIT_DRIGHT))

        self._emit_state()

    async def disconnect(self):
        if self._dev is None:
            return
        try:
            await self._dev.call_disconnect()
        except Exception:
            pass

async def run_ns2_pro(mac: str, *, status_hz: float = 5.0, verbose: bool = False):
    drv = NS2ProGamepad(mac, verbose=verbose)
    await drv.connect()
    await drv.start()

    last_print = 0.0
    last_count = 0
    period = 1.0 / max(0.5, float(status_hz))

    try:
        while True:
            await asyncio.sleep(0.20)
            now = time.time()

            if now - last_print >= period:
                last_print = now
                cnt = drv.notif_count
                rate = (cnt - last_count) / max(1e-6, period)
                last_count = cnt

                lraw = drv.left_stick.last_raw
                rraw = drv.right_stick.last_raw

                lcx = drv.left_stick.center_x if drv.left_stick.center_x is not None else -1
                lcy = drv.left_stick.center_y if drv.left_stick.center_y is not None else -1
                rcx = drv.right_stick.center_x if drv.right_stick.center_x is not None else -1
                rcy = drv.right_stick.center_y if drv.right_stick.center_y is not None else -1

                sys.stderr.write(
                    f"\r[ns2pro] notifs={cnt:6d} rate={rate:5.1f}/s "
                    f"b4=0x{drv.last_b4:02x} b5=0x{drv.last_b5:02x} "
                    f"b6=0x{drv.last_b6:02x} b7=0x{drv.last_b7:02x} "
                    f"L={lraw[0]:02x}{lraw[1]:02x}{lraw[2]:02x} "
                    f"R={rraw[0]:02x}{rraw[1]:02x}{rraw[2]:02x} "
                    f"Lc=({lcx},{lcy}) Rc=({rcx},{rcy})   "
                )
                sys.stderr.flush()
    finally:
        try:
            await drv.disconnect()
        except Exception:
            pass

def main():
    ap = argparse.ArgumentParser(description="Switch 2 Pro Controller standalone driver")
    ap.add_argument("mac", help="Bluetooth MAC address of the Switch 2 Pro Controller")
    ap.add_argument("--status-hz", type=float, default=5.0)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    try:
        asyncio.run(run_ns2_pro(args.mac, status_hz=args.status_hz, verbose=args.verbose))
    except KeyboardInterrupt:
        _stderr("\n[ns2pro] Stopped.")
        return 130
    return 0

if __name__ == "__main__":
    main()
