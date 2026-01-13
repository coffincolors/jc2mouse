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

# Optical decode (your proven logic)
OPT_OFFSET = 0x0F
OPT_LEN = 5
X_LO_IDX, X_HI_IDX = 1, 2
Y_LO_IDX, Y_HI_IDX = 3, 4

SENS_X = 1.0
SENS_Y = 1.0
DEADZONE = 0
MAX_STEP = 200
INVERT_X = False
INVERT_Y = False

DEFAULT_NOTIFY_UUID = "ab7de9be-89fe-49ad-828f-118f09df7fd2"
DEFAULT_CTRL_UUID   = "649d4ac9-8eb7-4e6c-af44-1ea54fe5f005"


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v

def u16_from_opt(opt, lo_idx, hi_idx):
    return opt[lo_idx] | (opt[hi_idx] << 8)

def delta_u16(curr, prev):
    d = (curr - prev) & 0xFFFF
    return d - 0x10000 if d > 0x7FFF else d


class JC2OpticalMouse:
    def __init__(self, mac: str, notify_uuid: str | None = None, ctrl_uuid: str | None = None):
        self.mac = mac.upper()
        #self.notify_uuid = notify_uuid.lower() if notify_uuid else None
        #self.ctrl_uuid = ctrl_uuid.lower() if ctrl_uuid else None
        self.notify_uuid = (notify_uuid or DEFAULT_NOTIFY_UUID).lower()
        self.ctrl_uuid   = (ctrl_uuid or DEFAULT_CTRL_UUID).lower()


        self.bus: MessageBus | None = None
        self.objects = None

        self.dev_path: str | None = None
        self.notify_path: str | None = None
        self.ctrl_path: str | None = None

        self.prev_x16 = None
        self.prev_y16 = None
        self.last_dbg = time.time()

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
        raise RuntimeError(f"Device {self.mac} not found in BlueZ object tree. Use bluetoothctl (in this session) to scan once if needed.")

    def _pick_characteristics(self):
        notify = []
        writable = []

        for path, ifaces in self.objects.items():
            ch = ifaces.get(GATT_CHRC_IFACE)
            if not ch:
                continue
            if self.dev_path and not path.startswith(self.dev_path + "/"):
                continue

            uuid_v = ch.get("UUID")
            flags_v = ch.get("Flags")

            uuid = str(getattr(uuid_v, "value", uuid_v) or "").lower()
            flags_raw = getattr(flags_v, "value", flags_v) or []
            flags = [str(x).lower() for x in flags_raw]

            if "notify" in flags:
                notify.append((path, uuid))
            if ("write-without-response" in flags) or ("write" in flags):
                writable.append((path, uuid))

        def by_uuid(cands, want):
            for p, u in cands:
                if u == want:
                    return p
            return None

        # We ALWAYS have defaults now, so require exact matches
        self.notify_path = by_uuid(notify, self.notify_uuid)
        if not self.notify_path:
            raise RuntimeError(f"Notify characteristic UUID not found: {self.notify_uuid}")

        self.ctrl_path = by_uuid(writable, self.ctrl_uuid)
        if not self.ctrl_path:
            raise RuntimeError(f"Control characteristic UUID not found: {self.ctrl_uuid}")

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
            raise RuntimeError("Connected, but no GATT characteristics appeared (ServicesResolved never populated).")

        # Now pick notify/control characteristics
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
                data = bytes(changed["Value"].value)
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
            #await ctrl.call_write_value(list(b), opts)
            await ctrl.call_write_value(b, opts)


        print("[jc2] Sending optical init (FF)...", file=sys.stderr)
        await write_cmd("0c91010200040000ff000000")
        await write_cmd("0c91010400040000ff000000")
        print("[jc2] Init sent. Moving cursor should work if notifications are correct.", file=sys.stderr)

    def handle_notification(self, data: bytes):
        if len(data) < OPT_OFFSET + OPT_LEN:
            return
        opt = data[OPT_OFFSET:OPT_OFFSET + OPT_LEN]

        x16 = u16_from_opt(opt, X_LO_IDX, X_HI_IDX)
        y16 = u16_from_opt(opt, Y_LO_IDX, Y_HI_IDX)

        if self.prev_x16 is None:
            self.prev_x16, self.prev_y16 = x16, y16
            return

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
        if dx == 0 and dy == 0:
            return

        mdx = int(clamp(dx * SENS_X, -MAX_STEP, MAX_STEP))
        mdy = int(clamp(dy * SENS_Y, -MAX_STEP, MAX_STEP))
        if mdx == 0 and mdy == 0:
            return

        self.ui.write(e.EV_REL, e.REL_X, mdx)
        self.ui.write(e.EV_REL, e.REL_Y, mdy)
        self.ui.syn()

        now = time.time()
        if now - self.last_dbg > 0.35:
            print(f"opt={list(opt)} x16={x16:5d} y16={y16:5d} dx={dx:+6d} dy={dy:+6d} -> {mdx:+4d},{mdy:+4d}", file=sys.stderr)
            self.last_dbg = now

        async def _refresh_objects_until_gatt(self, timeout_s: float = 60.0):
        deadline = time.time() + timeout_s
        last_msg = 0.0

        want_notify = self.notify_uuid
        want_ctrl = self.ctrl_uuid

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
                uuid_v = ch.get("UUID")
                uuid = str(getattr(uuid_v, "value", uuid_v) or "").lower()

                if uuid == want_notify:
                    found_notify = True
                if uuid == want_ctrl:
                    found_ctrl = True

            now = time.time()
            if now - last_msg > 0.5:
                print(f"[jc2] GATT chrc={total} have_notify={found_notify} have_ctrl={found_ctrl}", file=sys.stderr)
                last_msg = now

            if found_notify and found_ctrl:
                return True

            await asyncio.sleep(0.25)

        return False



async def run(mac: str):
    drv = JC2OpticalMouse(mac)
    await drv.connect()
    await drv.start()
    while True:
        await asyncio.sleep(1.0)
