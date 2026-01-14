import asyncio
import sys
import time

from dbus_next.aio import MessageBus
from dbus_next.constants import BusType
from dbus_next import Variant

from evdev import UInput, ecodes as e

BLUEZ = "org.bluez"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
PROP_IFACE = "org.freedesktop.DBus.Properties"
DEVICE_IFACE = "org.bluez.Device1"
GATT_CHRC_IFACE = "org.bluez.GattCharacteristic1"

# ---- Joy-Con 2 known GATT UUIDs (from your discovery logs) ----
DEFAULT_NOTIFY_UUID = "ab7de9be-89fe-49ad-828f-118f09df7fd2"
DEFAULT_CTRL_UUID   = "649d4ac9-8eb7-4e6c-af44-1ea54fe5f005"

# ---- Optical decode (your proven logic) ----
OPT_OFFSET = 0x0F
OPT_LEN = 5
X_LO_IDX, X_HI_IDX = 1, 2
Y_LO_IDX, Y_HI_IDX = 3, 4

# Tuning
SENS_X = 1.0
SENS_Y = 1.0
DEADZONE = 0
MAX_STEP = 200
INVERT_X = False
INVERT_Y = False

# ---- Button bitfields (current best map) ----
BTN4 = 4
BTN5 = 5

# byte[4]
BTN_Y  = 0x01
BTN_X  = 0x02
BTN_B  = 0x04
BTN_A  = 0x08
BTN_SR = 0x10
BTN_SL = 0x20
BTN_L  = 0x40
BTN_ZL = 0x80

# byte[5]
BTN_R3   = 0x04
BTN_HOME = 0x10
BTN_C    = 0x40


def _unwrap(v):
    """dbus-next returns Variant objects in GetManagedObjects; unwrap safely."""
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


class JC2OpticalMouse:
    def __init__(self, mac: str, notify_uuid: str | None = None, ctrl_uuid: str | None = None):
        self.mac = mac.upper()
        self.notify_uuid = (notify_uuid or DEFAULT_NOTIFY_UUID).lower()
        self.ctrl_uuid = (ctrl_uuid or DEFAULT_CTRL_UUID).lower()

        self.bus: MessageBus | None = None
        self.objects = None

        self.dev_path: str | None = None
        self.notify_path: str | None = None
        self.ctrl_path: str | None = None

        self.prev_x16: int | None = None
        self.prev_y16: int | None = None
        self.last_dbg = time.time()

        # Button edge tracking
        self._prev_left = False
        self._prev_right = False
        self._prev_middle = False

        # uinput device: must include EV_KEY buttons
        self.ui = UInput(
            {
                e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL],
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
        """Poll BlueZ until *our* notify/control UUIDs appear under the device."""
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
                print(f"[jc2] GATT chrc={total} have_notify={found_notify} have_ctrl={found_ctrl}", file=sys.stderr)
                last_msg = now

            if found_notify and found_ctrl:
                return True

            await asyncio.sleep(0.25)

        return False

    async def connect(self):
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self.objects = await self._get_managed_objects()

        self.dev_path = self._find_device_path()

        # Connect + trust first
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

        # Pick notify/control characteristics by UUID
        self._pick_characteristics()
        print(f"[jc2] notify_path={self.notify_path}", file=sys.stderr)
        print(f"[jc2] ctrl_path={self.ctrl_path}", file=sys.stderr)

    async def start(self):
        # Subscribe to notifications
        ch_intro = await self.bus.introspect(BLUEZ, self.notify_path)
        ch_obj = self.bus.get_proxy_object(BLUEZ, self.notify_path, ch_intro)
        ch = ch_obj.get_interface(GATT_CHRC_IFACE)
        props = ch_obj.get_interface(PROP_IFACE)

        def on_props_changed(_iface, changed, _invalidated):
            if "Value" in changed:
                data = bytes(_unwrap(changed["Value"]))
                self.handle_notification(data)

        props.on_properties_changed(on_props_changed)

        print("[jc2] StartNotify()", file=sys.stderr)
        await ch.call_start_notify()

        # Send optical init writes (type=command)
        ctrl_intro = await self.bus.introspect(BLUEZ, self.ctrl_path)
        ctrl_obj = self.bus.get_proxy_object(BLUEZ, self.ctrl_path, ctrl_intro)
        ctrl = ctrl_obj.get_interface(GATT_CHRC_IFACE)

        async def write_cmd(hexstr: str):
            b = bytes.fromhex(hexstr)
            opts = {"type": Variant("s", "command")}
            await ctrl.call_write_value(b, opts)

        print("[jc2] Sending optical init (FF)...", file=sys.stderr)
        await write_cmd("0c91010200040000ff000000")
        await write_cmd("0c91010400040000ff000000")
        print("[jc2] Init sent. Moving cursor should work if notifications are correct.", file=sys.stderr)

    def handle_notification(self, data: bytes):
        # We need enough bytes for buttons + optical slice
        if len(data) < OPT_OFFSET + OPT_LEN:
            return

        # ---- decode optical counters ----
        opt = data[OPT_OFFSET:OPT_OFFSET + OPT_LEN]
        x16 = u16_from_opt(opt, X_LO_IDX, X_HI_IDX)
        y16 = u16_from_opt(opt, Y_LO_IDX, Y_HI_IDX)

        if self.prev_x16 is None:
            # Initialize counters but DO NOT return — buttons must still work
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

        # ---- buttons (must work even when not moving) ----
        left = btn_pressed(data, BTN4, BTN_L)       # L -> left click
        right = btn_pressed(data, BTN4, BTN_ZL)     # ZL -> right click
        middle = btn_pressed(data, BTN5, BTN_R3)    # R3 -> middle click

        did_any = False

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

        # ---- movement ----
        if abs(dx) <= DEADZONE:
            dx = 0
        if abs(dy) <= DEADZONE:
            dy = 0

        if dx != 0 or dy != 0:
            mdx = int(clamp(dx * SENS_X, -MAX_STEP, MAX_STEP))
            mdy = int(clamp(dy * SENS_Y, -MAX_STEP, MAX_STEP))
            if mdx != 0 or mdy != 0:
                self.ui.write(e.EV_REL, e.REL_X, mdx)
                self.ui.write(e.EV_REL, e.REL_Y, mdy)
                did_any = True

                # optional debug (only when moving)
                now = time.time()
                if now - self.last_dbg > 0.35:
                    print(
                        f"opt={list(opt)} x16={x16:5d} y16={y16:5d} "
                        f"dx={dx:+6d} dy={dy:+6d} -> {mdx:+4d},{mdy:+4d}",
                        file=sys.stderr
                    )
                    self.last_dbg = now

        if did_any:
            self.ui.syn()


async def run(mac: str):
    drv = JC2OpticalMouse(mac)
    await drv.connect()
    await drv.start()
    while True:
        await asyncio.sleep(1.0)
