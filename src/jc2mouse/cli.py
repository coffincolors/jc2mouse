import argparse
import asyncio
import os
import sys
import subprocess
import time
from typing import Optional, Dict, Any, List, Tuple

# Avoid root-owned __pycache__ + permission weirdness
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

from dbus_next.aio import MessageBus
from dbus_next.constants import BusType
from jc2mouse.driver import run as run_driver
from jc2mouse.mapper import run_button_wizard

BLUEZ = "org.bluez"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"

# Manufacturer / Joy-Con 2 signature (observed stable pattern)
NINTENDO_COMPANY_ID = 0x0553
JC2_MFG_LEN = 24
JC2_MFG_PREFIX = bytes.fromhex("01 00 03 7e 05")  # first 5 bytes
JC2_SIDE_BYTE_IDX = 5  # 0-based in mfg payload
JC2_SIDE_RIGHT = 0x66
JC2_SIDE_LEFT = 0x67


def _require_root():
    if os.geteuid() != 0:
        print("ERROR: must run as root (sudo) for uinput + bluetooth session control", file=sys.stderr)
        raise SystemExit(1)


def _call_session(cmd: str):
    subprocess.check_call(["/usr/local/sbin/jc2-session", cmd])


def _unwrap(v):
    return getattr(v, "value", v)


def _format_bytes(b: bytes, max_len: int = 24) -> str:
    if len(b) <= max_len:
        return b.hex()
    return b[:max_len].hex() + "…"


def _side_from_mfg(mfg: bytes) -> str:
    if len(mfg) <= JC2_SIDE_BYTE_IDX:
        return "unknown"
    sb = mfg[JC2_SIDE_BYTE_IDX]
    if sb == JC2_SIDE_RIGHT:
        return "right"
    if sb == JC2_SIDE_LEFT:
        return "left"
    return "unknown"


def _is_jc2_mfg(mfg: bytes) -> bool:
    return (len(mfg) == JC2_MFG_LEN) and mfg.startswith(JC2_MFG_PREFIX)


def _rssi_live(rssi: Optional[int]) -> bool:
    # BlueZ will show -999 or None for stale cache
    if rssi is None:
        return False
    if rssi <= -999:
        return False
    # reasonable bounds
    return -127 <= rssi <= 20


async def _get_managed_objects(bus: MessageBus) -> Dict[str, Any]:
    intro = await bus.introspect(BLUEZ, "/")
    om_obj = bus.get_proxy_object(BLUEZ, "/", intro)
    om = om_obj.get_interface(OM_IFACE)
    return await om.call_get_managed_objects()


def _find_adapters(objects: Dict[str, Any]) -> List[str]:
    return sorted([p for p, ifaces in objects.items() if ADAPTER_IFACE in ifaces])


def _extract_device_candidate(objects: Dict[str, Any], path: str) -> Optional[Dict[str, Any]]:
    ifaces = objects.get(path, {})
    dev = ifaces.get(DEVICE_IFACE)
    if not dev:
        return None

    addr = _unwrap(dev.get("Address"))
    if not addr:
        return None

    connected = bool(_unwrap(dev.get("Connected")) or False)

    mfg_map = _unwrap(dev.get("ManufacturerData"))
    if not isinstance(mfg_map, dict) or NINTENDO_COMPANY_ID not in mfg_map:
        return None

    raw = _unwrap(mfg_map.get(NINTENDO_COMPANY_ID))
    try:
        mfg = bytes(raw)
    except Exception:
        return None

    if not _is_jc2_mfg(mfg):
        return None

    rssi_v = _unwrap(dev.get("RSSI"))
    rssi: Optional[int]
    try:
        rssi = int(rssi_v) if rssi_v is not None else None
    except Exception:
        rssi = None

    name = _unwrap(dev.get("Name")) or _unwrap(dev.get("Alias")) or ""
    side = _side_from_mfg(mfg)

    return {
        "path": path,
        "mac": str(addr).upper(),
        "name": str(name),
        "connected": connected,
        "rssi": rssi,
        "mfg": mfg,
        "side": side,
        "seen_ts": time.time(),  # refreshed whenever we re-extract it
    }


async def discover_jc2(
    timeout_s: float = 8.0,
    side_filter: str = "any",
    prefer_connected: bool = True,
    ask: bool = False,
) -> Dict[str, Any]:
    """
    Returns a Joy-Con 2 candidate dict.

    Selection priority:
      1) If prefer_connected: any currently Connected Joy-Con 2 (filtered by side)
      2) Otherwise: any "live" advertising Joy-Con 2 (valid RSSI) seen during scan window
      3) Never auto-select stale cache (RSSI None/-999). If only stale exists -> error.
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    objects = await _get_managed_objects(bus)

    # 1) Prefer already-connected devices
    all_now: List[Dict[str, Any]] = []
    for path in objects.keys():
        c = _extract_device_candidate(objects, path)
        if not c:
            continue
        if side_filter in ("right", "left") and c["side"] != side_filter:
            continue
        all_now.append(c)

    if prefer_connected:
        connected_now = [c for c in all_now if c["connected"]]
        if connected_now:
            # If multiple connected, pick best RSSI if any, else first.
            connected_now.sort(key=lambda x: (x["rssi"] is not None, x["rssi"] or -999), reverse=True)
            pick = connected_now[0]
            sys.stderr.write(
                f"[jc2] Using already-connected device: {pick['mac']} (side={pick['side']})\n"
            )
            sys.stderr.flush()
            return pick

    adapters = _find_adapters(objects)
    if not adapters:
        raise RuntimeError("No Bluetooth adapter found (org.bluez.Adapter1).")

    adapter_path = next((p for p in adapters if p.endswith("/hci0")), adapters[0])
    ad_intro = await bus.introspect(BLUEZ, adapter_path)
    ad_obj = bus.get_proxy_object(BLUEZ, adapter_path, ad_intro)
    adapter = ad_obj.get_interface(ADAPTER_IFACE)

    # 2) Scan for LIVE advertisers
    try:
        await adapter.call_start_discovery()
    except Exception as ex:
        raise RuntimeError(f"StartDiscovery failed: {ex}")

    candidates_live: Dict[str, Dict[str, Any]] = {}
    candidates_stale: Dict[str, Dict[str, Any]] = {}

    try:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            objects = await _get_managed_objects(bus)
            now = time.time()

            for path in objects.keys():
                c = _extract_device_candidate(objects, path)
                if not c:
                    continue
                if side_filter in ("right", "left") and c["side"] != side_filter:
                    continue

                # Track live vs stale based on RSSI
                if _rssi_live(c["rssi"]):
                    prev = candidates_live.get(c["mac"])
                    if (prev is None) or ((c["rssi"] or -999) >= (prev["rssi"] or -999)):
                        candidates_live[c["mac"]] = c
                    else:
                        prev["seen_ts"] = now
                else:
                    candidates_stale[c["mac"]] = c

            await asyncio.sleep(0.30)

    finally:
        try:
            await adapter.call_stop_discovery()
        except Exception:
            pass

    # Keep only live sightings that were “recent” within the scan window
    now = time.time()
    live = [c for c in candidates_live.values() if (now - c["seen_ts"]) < 2.0]
    live.sort(key=lambda x: x.get("rssi", -999) or -999, reverse=True)

    sys.stderr.write("\n[jc2] Auto-discovery (LIVE advertisers):\n")
    if not live:
        sys.stderr.write("  (none)\n")
    else:
        for i, c in enumerate(live, 1):
            sys.stderr.write(
                f"  {i}) {c['mac']}  side={c['side']:<5}  rssi={c['rssi']:>4}  mfg={_format_bytes(c['mfg'])}\n"
            )

    # Show stale cache for debugging only
    stale = list(candidates_stale.values())
    if stale:
        sys.stderr.write("[jc2] Stale cache (ignored):\n")
        for c in stale:
            rssi_s = "None" if c["rssi"] is None else str(c["rssi"])
            sys.stderr.write(
                f"  -  {c['mac']}  side={c['side']:<5}  rssi={rssi_s:>4}  mfg={_format_bytes(c['mfg'])}\n"
            )
    sys.stderr.flush()

    if not live:
        raise RuntimeError(
            "No LIVE Joy-Con 2 advertisers found.\n"
            "Hold the pairing button so it advertises, then retry.\n"
            "(Note: cached devices with RSSI -999 are ignored.)"
        )

    if len(live) == 1:
        pick = live[0]
        sys.stderr.write(f"[jc2] Selected: {pick['mac']} (side={pick['side']}, rssi={pick['rssi']})\n")
        sys.stderr.flush()
        return pick

    if ask and sys.stdin.isatty():
        while True:
            try:
                choice = input(f"Select device [1-{len(live)}] (or Enter = best RSSI): ").strip()
            except EOFError:
                choice = ""
            if choice == "":
                pick = live[0]
                break
            try:
                idx = int(choice)
                if 1 <= idx <= len(live):
                    pick = live[idx - 1]
                    break
            except ValueError:
                pass
            print("Invalid choice.", file=sys.stderr)

        sys.stderr.write(f"[jc2] Selected: {pick['mac']} (side={pick['side']}, rssi={pick['rssi']})\n")
        sys.stderr.flush()
        return pick

    pick = live[0]
    sys.stderr.write(f"[jc2] Selected best RSSI: {pick['mac']} (side={pick['side']}, rssi={pick['rssi']})\n")
    sys.stderr.flush()
    return pick


def main():
    ap = argparse.ArgumentParser(prog="jc2mouse")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Show bluetooth session status")
    sub.add_parser("start", help="Enter jc2 session mode (stop stock bluetooth, start patched bluetoothd)")
    sub.add_parser("stop", help="Exit jc2 session mode (stop patched bluetoothd, restore stock bluetooth)")

    p_run = sub.add_parser("run", help="Run optical mouse driver (requires session mode active)")
    p_run.add_argument("--mac", help="Joy-Con 2 MAC address (e.g. 98:E2:55:DF:56:13)")
    p_run.add_argument("--auto", action="store_true", help="Auto-detect Joy-Con 2 and run it")
    p_run.add_argument("--side", choices=["any", "right", "left"], default="any", help="When using --auto, filter by side")
    p_run.add_argument("--timeout", type=float, default=8.0, help="Auto-discovery scan time seconds (default: 8)")
    p_run.add_argument("--ask", action="store_true", help="When multiple devices match, ask which to use")
    p_run.add_argument("--prefer-connected", action="store_true", help="Prefer already-connected Joy-Con 2 (default)")
    p_run.add_argument("--no-prefer-connected", action="store_true", help="Do not prefer already-connected device")
    p_run.add_argument("--no-status", action="store_true", help="Disable the one-line status output")
    p_run.add_argument("--status-hz", type=float, default=5.0, help="Status refresh rate (default: 5 Hz)")
    p_run.add_argument("--verbose", action="store_true", help="Developer verbosity (rarely needed)")

    p_scan = sub.add_parser("scan", help="Scan and list LIVE Joy-Con 2 candidates (no connect)")
    p_scan.add_argument("--side", choices=["any", "right", "left"], default="any", help="Filter by side")
    p_scan.add_argument("--timeout", type=float, default=8.0, help="Scan time seconds (default: 8)")

    p_map = sub.add_parser("dev-map-buttons", help="Developer: interactively discover button bit/byte positions")
    p_map.add_argument("--mac", required=True, help="Joy-Con 2 MAC address")

    args = ap.parse_args()
    _require_root()

    if args.cmd == "status":
        _call_session("status")
        return 0
    if args.cmd == "start":
        _call_session("start")
        return 0
    if args.cmd == "stop":
        _call_session("stop")
        return 0

    if args.cmd == "scan":
        async def _do_scan():
            sys.stderr.write("[jc2] Hold Joy-Con 2 pairing button now...\n")
            sys.stderr.flush()
            await discover_jc2(
                timeout_s=args.timeout,
                side_filter=args.side,
                prefer_connected=False,  # scan is scan
                ask=False,
            )

        try:
            asyncio.run(_do_scan())
        except KeyboardInterrupt:
            print("\n[jc2] Stopped.", file=sys.stderr)
        return 0

    if args.cmd == "run":
        async def _do_run():
            mac = args.mac

            if args.auto:
                prefer = True
                if args.no_prefer_connected:
                    prefer = False
                if not args.prefer_connected and not args.no_prefer_connected:
                    prefer = True  # default

                if prefer:
                    sys.stderr.write("[jc2] (auto) Will use already-connected Joy-Con 2 if found.\n")
                sys.stderr.write("[jc2] If not connected, hold Joy-Con 2 pairing button now...\n")
                sys.stderr.flush()

                pick = await discover_jc2(
                    timeout_s=args.timeout,
                    side_filter=args.side,
                    prefer_connected=prefer,
                    ask=args.ask,
                )
                mac = pick["mac"]
                sys.stderr.write(f"[jc2] Auto-selected {mac} (side={pick['side']}, rssi={pick['rssi']})\n")
                sys.stderr.flush()

            if not mac:
                raise SystemExit("ERROR: provide --mac or use --auto")

            await run_driver(
                mac,
                status=(not args.no_status),
                status_hz=args.status_hz,
                verbose=args.verbose,
            )

        try:
            asyncio.run(_do_run())
        except KeyboardInterrupt:
            print("\n[jc2] Stopped.", file=sys.stderr)
        return 0

    if args.cmd == "dev-map-buttons":
        try:
            asyncio.run(run_button_wizard(args.mac))
        except KeyboardInterrupt:
            print("\n[jc2] Stopped.", file=sys.stderr)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
