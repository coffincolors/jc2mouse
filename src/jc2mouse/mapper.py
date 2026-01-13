import asyncio
import sys
import time
from collections import Counter
from dataclasses import dataclass

from dbus_next import Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType

def uniq_count(pkts: list[bytes], idx: int) -> int:
    s = set()
    for p in pkts:
        if idx < len(p):
            s.add(p[idx])
            if len(s) > 10:
                break
    return len(s)

def most_common_xor(base_pkts: list[bytes], press_pkts: list[bytes], idx: int):
    c = Counter()
    n = 0
    for b, p in zip(base_pkts, press_pkts):
        if idx < len(b) and idx < len(p):
            c[b[idx] ^ p[idx]] += 1
            n += 1
    if not c or n < 8:
        return None
    val, cnt = c.most_common(1)[0]
    return val, cnt, n

BLUEZ = "org.bluez"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
PROP_IFACE = "org.freedesktop.DBus.Properties"
DEVICE_IFACE = "org.bluez.Device1"
GATT_CHRC_IFACE = "org.bluez.GattCharacteristic1"

DEFAULT_NOTIFY_UUID = "ab7de9be-89fe-49ad-828f-118f09df7fd2"
DEFAULT_CTRL_UUID   = "649d4ac9-8eb7-4e6c-af44-1ea54fe5f005"


def _unwrap(v):
    return getattr(v, "value", v)


@dataclass
class Change:
    idx: int
    base: int
    pressed: int
    xor: int


class JC2DevMapper:
    def __init__(self, mac: str):
        self.mac = mac.upper()
        self.bus: MessageBus | None = None
        self.objects = None

        self.dev_path: str | None = None
        self.notify_path: str | None = None
        self.ctrl_path: str | None = None

        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=500)

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

    def _pick_paths_by_uuid(self):
        notify_uuid = DEFAULT_NOTIFY_UUID
        ctrl_uuid = DEFAULT_CTRL_UUID

        notify_path = None
        ctrl_path = None

        for path, ifaces in self.objects.items():
            ch = ifaces.get(GATT_CHRC_IFACE)
            if not ch:
                continue
            if self.dev_path and not path.startswith(self.dev_path + "/"):
                continue

            uuid = str(_unwrap(ch.get("UUID", "")) or "").lower()
            if uuid == notify_uuid:
                notify_path = path
            if uuid == ctrl_uuid:
                ctrl_path = path

        if not notify_path or not ctrl_path:
            raise RuntimeError(f"Could not find notify/control UUIDs yet. notify={bool(notify_path)} ctrl={bool(ctrl_path)}")

        self.notify_path = notify_path
        self.ctrl_path = ctrl_path

    async def _wait_for_paths(self, timeout_s: float = 60.0):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            self.objects = await self._get_managed_objects()
            try:
                self._pick_paths_by_uuid()
                return
            except RuntimeError:
                await asyncio.sleep(0.25)
        raise RuntimeError("Timed out waiting for notify/control characteristic objects.")

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

        print("[map] Connecting...", file=sys.stderr)
        await dev.call_connect()
        print("[map] Connected. Waiting for GATT export...", file=sys.stderr)

        await self._wait_for_paths(timeout_s=60.0)
        print(f"[map] notify_path={self.notify_path}", file=sys.stderr)
        print(f"[map] ctrl_path={self.ctrl_path}", file=sys.stderr)

    async def start_notify(self, do_optical_init: bool = True):
        # Subscribe to notifications
        ch_intro = await self.bus.introspect(BLUEZ, self.notify_path)
        ch_obj = self.bus.get_proxy_object(BLUEZ, self.notify_path, ch_intro)
        ch = ch_obj.get_interface(GATT_CHRC_IFACE)
        props = ch_obj.get_interface(PROP_IFACE)

        def on_props_changed(_iface, changed, _invalidated):
            if "Value" in changed:
                data = bytes(_unwrap(changed["Value"]))
                # non-blocking queue put
                try:
                    self._queue.put_nowait(data)
                except asyncio.QueueFull:
                    pass

        props.on_properties_changed(on_props_changed)

        print("[map] StartNotify()", file=sys.stderr)
        await ch.call_start_notify()

        if do_optical_init:
            ctrl_intro = await self.bus.introspect(BLUEZ, self.ctrl_path)
            ctrl_obj = self.bus.get_proxy_object(BLUEZ, self.ctrl_path, ctrl_intro)
            ctrl = ctrl_obj.get_interface(GATT_CHRC_IFACE)

            async def write_cmd(hexstr: str):
                b = bytes.fromhex(hexstr)
                opts = {"type": Variant("s", "command")}
                await ctrl.call_write_value(b, opts)

            print("[map] Sending optical init (FF)...", file=sys.stderr)
            await write_cmd("0c91010200040000ff000000")
            await write_cmd("0c91010400040000ff000000")

    async def _drain(self):
        # clear queue
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except Exception:
                break

    async def capture(self, seconds: float) -> list[bytes]:
        """Capture packets for N seconds."""
        out: list[bytes] = []
        t0 = time.time()
        while time.time() - t0 < seconds:
            try:
                pkt = await asyncio.wait_for(self._queue.get(), timeout=0.5)
                out.append(pkt)
            except asyncio.TimeoutError:
                continue
        return out


def mode_byte(pkts: list[bytes], idx: int) -> int | None:
    c = Counter()
    for p in pkts:
        if idx < len(p):
            c[p[idx]] += 1
    if not c:
        return None
    return c.most_common(1)[0][0]


def diff_stable_xor(base_pkts: list[bytes], press_pkts: list[bytes], max_len: int = 64) -> list[tuple[int,int,int,int]]:
    """
    Return list of (idx, xor, hits, total) for bytes that:
      - are stable in baseline and pressed
      - have a consistent XOR mask
    """
    out = []
    # align sizes so zip works
    n = min(len(base_pkts), len(press_pkts))
    base_pkts = base_pkts[:n]
    press_pkts = press_pkts[:n]

    for i in range(max_len):
        if uniq_count(base_pkts, i) > 2:
            continue
        if uniq_count(press_pkts, i) > 2:
            continue

        mc = most_common_xor(base_pkts, press_pkts, i)
        if not mc:
            continue
        xor, hits, total = mc

        # ignore "no change"
        if xor == 0:
            continue

        # require consistency
        if hits / max(1, total) < 0.65:
            continue

        out.append((i, xor, hits, total))

    return out



async def run_button_wizard(mac: str):
    mapper = JC2DevMapper(mac)
    await mapper.connect()
    await mapper.start_notify(do_optical_init=True)

    buttons = [
        ("A", "Press and hold A"),
        ("B", "Press and hold B"),
        ("X", "Press and hold X"),
        ("Y", "Press and hold Y"),
        ("PLUS", "Press and hold +"),
        ("L", "Press and hold L (L1)"),
        ("ZL", "Press and hold ZL (L2)"),
        ("SR", "Press and hold SR"),
        ("SL", "Press and hold SL"),
        ("R3", "Press and hold stick click (R3)"),
        ("HOME", "Press and hold Home"),
        ("C", "Press and hold C (GameChat)"),
    ]

    print("\n[jc2 dev] Button mapping wizard", file=sys.stderr)
    print("[jc2 dev] For each prompt: keep controller still, then HOLD the button for ~1 second, then release.", file=sys.stderr)
    print("[jc2 dev] We’ll sample baseline (no press) and pressed (hold).", file=sys.stderr)

    report: dict[str, list[Change]] = {}

    for name, prompt in buttons:
        await mapper._drain()
        input(f"\n--- {name} ---\n{prompt}\n1) Keep still, press Enter to capture BASELINE...")
        base = await mapper.capture(1.2)

        await mapper._drain()
        input(f"2) Now HOLD {name}, press Enter to capture PRESSED (keep holding while it captures)...")
        pressed = await mapper.capture(1.2)

        input(f"3) Release {name}, press Enter to continue...")

        ch = diff_stable_xor(base, pressed, max_len=64)
        report[name] = ch

        if not ch:
            print("No stable XOR changes detected (try again; hold more firmly).", file=sys.stderr)
        else:
            for (idx, xor, hits, total) in ch:
                print(f"byte[{idx:02d}] xor=0x{xor:02x} (hits {hits}/{total})", file=sys.stderr)


        ch = diff_modes(base, pressed, max_len=64)
        report[name] = ch

        if not ch:
            print("No stable byte changes detected (try again / hold longer).", file=sys.stderr)
        else:
            for c in ch[:12]:
                print(f"byte[{c.idx:02d}] base=0x{c.base:02x} pressed=0x{c.pressed:02x} xor=0x{c.xor:02x}", file=sys.stderr)
            if len(ch) > 12:
                print(f"... and {len(ch)-12} more byte(s) changed (increase max_len or inspect)", file=sys.stderr)

        print("Release. Next...", file=sys.stderr)
        await asyncio.sleep(0.5)

    # Print a final condensed report
    print("\n===== jc2 button map report (stable XOR, first 64 bytes) =====")
    for name, changes in report.items():
        if not changes:
            print(f"{name}: (no stable XOR)")
            continue
        parts = [f"{idx}:0x{xor:02x}" for (idx, xor, _hits, _total) in changes]
        print(f"{name}: " + " ".join(parts))

