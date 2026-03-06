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
from dbus_next.errors import DBusError

from jc2mouse.driver import run as run_driver, run_combined
from jc2mouse.ns2pro import run_ns2_pro
from jc2mouse.mapper import run_button_wizard

BLUEZ = "org.bluez"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
PROP_IFACE = "org.freedesktop.DBus.Properties"

# Manufacturer / Joy-Con 2 signature (observed stable pattern)
NINTENDO_COMPANY_ID = 0x0553
NS2_MFG_LEN = 24
NS2_MFG_PREFIX = bytes.fromhex("01 00 03 7e 05")
NS2_SUBTYPE_IDX = 5

NS2_SUBTYPE_RIGHT = 0x66
NS2_SUBTYPE_LEFT = 0x67
NS2_SUBTYPE_PRO = 0x69


# Session services (see scripts/jc2-session.sh)
STOCK_BT_SERVICE = "bluetooth.service"
JC2_BT_SERVICE = "jc2-bluetooth.service"


def _require_root():
    if os.geteuid() != 0:
        print("ERROR: must run as root (sudo) for uinput + session control", file=sys.stderr)
        raise SystemExit(1)


def _call_session(cmd: str):
    subprocess.check_call(["/usr/local/sbin/jc2-session", cmd])


def _service_is_active(unit: str) -> bool:
    # returns 0 if active
    r = subprocess.run(["systemctl", "is-active", "--quiet", unit])
    return r.returncode == 0

def _service_is_masked(unit: str) -> bool:
    p = subprocess.run(["systemctl", "is-enabled", unit], capture_output=True, text=True)
    return "masked" in (p.stdout + p.stderr)


def _ensure_session(mode: str) -> bool:
    """
    Returns True if we started session mode ourselves and should stop it on exit.
    mode:
      - auto: start jc2 session only if not already active
      - on:   always start jc2 session and leave it running
      - off:  do not touch services
    """
    if mode == "off":
        return False

    already = _service_is_active(JC2_BT_SERVICE)
    if mode == "auto":
        if already:
            return False
        _call_session("start")
        return True

    # mode == "on"
    _call_session("start")
    return False  # leave it on


def _unwrap(v):
    return getattr(v, "value", v)


def _format_bytes(b: bytes, max_len: int = 24) -> str:
    if len(b) <= max_len:
        return b.hex()
    return b[:max_len].hex() + "…"


def _classify_nintendo_mfg(mfg: bytes) -> Dict[str, str]:
    if len(mfg) != NS2_MFG_LEN or not mfg.startswith(NS2_MFG_PREFIX):
        return {"kind": "unknown", "side": "unknown"}

    if len(mfg) <= NS2_SUBTYPE_IDX:
        return {"kind": "unknown", "side": "unknown"}

    subtype = mfg[NS2_SUBTYPE_IDX]

    if subtype == NS2_SUBTYPE_RIGHT:
        return {"kind": "jc2", "side": "right"}
    if subtype == NS2_SUBTYPE_LEFT:
        return {"kind": "jc2", "side": "left"}
    if subtype == NS2_SUBTYPE_PRO:
        return {"kind": "ns2pro", "side": "pro"}

    return {"kind": "unknown", "side": "unknown"}


def _is_supported_nintendo_mfg(mfg: bytes) -> bool:
    return (len(mfg) == NS2_MFG_LEN) and mfg.startswith(NS2_MFG_PREFIX)


def _rssi_live(rssi: Optional[int]) -> bool:
    # BlueZ will show -999 or None for stale cache
    if rssi is None:
        return False
    if rssi <= -999:
        return False
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

    if not _is_supported_nintendo_mfg(mfg):
        return None

    info = _classify_nintendo_mfg(mfg)
    if info["kind"] == "unknown":
        return None

    rssi_v = _unwrap(dev.get("RSSI"))
    try:
        rssi = int(rssi_v) if rssi_v is not None else None
    except Exception:
        rssi = None

    name = _unwrap(dev.get("Name")) or _unwrap(dev.get("Alias")) or ""
    kind = info["kind"]
    side = info["side"]

    return {
        "path": path,
        "mac": str(addr).upper(),
        "name": str(name),
        "connected": connected,
        "rssi": rssi,
        "mfg": mfg,
        "kind": kind,
        "side": side,
        "seen_ts": time.time(),
    }


def _sort_key_pick(c: Dict[str, Any]) -> tuple:
    """
    Sort candidates by:
      1) connected first (True > False)
      2) live RSSI (higher is better; None/-999 treated low)
      3) prefer right side (right > left > unknown)
    """
    rssi = c["rssi"]
    rssi_score = rssi if _rssi_live(rssi) else -9999
    side = c.get("side", "unknown")
    side_score = 2 if side == "right" else 1 if side == "left" else 0
    return (1 if c.get("connected") else 0, rssi_score, side_score)


async def discover_jc2(
    timeout_s: float = 8.0,
    side_filter: str = "any",
    prefer_connected: bool = True,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Returns: (picked, live_list, stale_list)

    - prefer_connected=True: if any connected JC2 matches, pick it immediately
    - otherwise scan for LIVE advertisers (valid RSSI) within timeout
    - never auto-select stale cache (RSSI None/-999)
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    objects = await _get_managed_objects(bus)

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
            connected_now.sort(key=_sort_key_pick, reverse=True)
            pick = connected_now[0]
            return pick, [], []

    adapters = _find_adapters(objects)
    if not adapters:
        raise RuntimeError("No Bluetooth adapter found (org.bluez.Adapter1).")

    adapter_path = next((p for p in adapters if p.endswith("/hci0")), adapters[0])
    ad_intro = await bus.introspect(BLUEZ, adapter_path)
    ad_obj = bus.get_proxy_object(BLUEZ, adapter_path, ad_intro)
    adapter = ad_obj.get_interface(ADAPTER_IFACE)

    # After restarting bluetoothd (session mode), BlueZ may need a moment before discovery works.
    # Also ensure adapter is Powered=True.
    props_intro = await bus.introspect(BLUEZ, adapter_path)
    props_obj = bus.get_proxy_object(BLUEZ, adapter_path, props_intro)
    props = props_obj.get_interface("org.freedesktop.DBus.Properties")

    # Best-effort power on
    try:
        await props.call_set(ADAPTER_IFACE, "Powered", __import__("dbus_next").Variant("b", True))
    except Exception:
        pass

    last_ex = None
    for attempt in range(1, 8):  # ~ (0.2+0.3+...+0.8) seconds total
        try:
            await adapter.call_start_discovery()
            last_ex = None
            break
        except Exception as ex:
            last_ex = ex
            msg = str(ex)
            # Common right after service restart
            if "Resource Not Ready" in msg or "In Progress" in msg:
                await asyncio.sleep(min(0.15 + attempt * 0.10, 0.8))
                continue
            raise RuntimeError(f"StartDiscovery failed: {ex}")

    if last_ex is not None:
        raise RuntimeError(f"StartDiscovery failed: {last_ex}")


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

    now = time.time()
    live = [c for c in candidates_live.values() if (now - c["seen_ts"]) < 2.0]
    live.sort(key=_sort_key_pick, reverse=True)

    stale = list(candidates_stale.values())
    return (live[0] if live else {}), live, stale


async def _disconnect_device_by_mac(mac: str) -> bool:
    mac = mac.upper()
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    objects = await _get_managed_objects(bus)

    dev_path = None
    suffix = "dev_" + mac.replace(":", "_")
    for path, ifaces in objects.items():
        if path.endswith(suffix) and DEVICE_IFACE in ifaces:
            dev_path = path
            break

    if not dev_path:
        return False

    intro = await bus.introspect(BLUEZ, dev_path)
    obj = bus.get_proxy_object(BLUEZ, dev_path, intro)
    dev = obj.get_interface(DEVICE_IFACE)

    try:
        await dev.call_disconnect()
        return True
    except Exception:
        return False


def _friendly_connect_error(ex: Exception) -> str:
    s = str(ex)
    # You saw: dbus_next.errors.DBusError: le-connection-abort-by-local
    if "le-connection-abort-by-local" in s:
        return (
            "Bluetooth LE connection was aborted locally.\n"
            "Most commonly this happens if the Joy-Con wasn’t in the right state.\n\n"
            "Try this:\n"
            "  1) Hold the small PAIR button on the rail until you see “Connected”.\n"
            "  2) Do NOT press other buttons while it’s connecting.\n"
            "  3) If it keeps happening, tap PAIR once, wait 2s, then run again.\n"
        )
    return s

def _should_skip_disconnect_cleanup_on_error(ex: BaseException) -> bool:
    """
    Suppress exit-time Device1.Disconnect only for early startup/connect failures,
    to avoid reintroducing connect/disconnect races during bring-up.
    """
    s = str(ex)

    startup_markers = (
        "le-connection-abort-by-local",
        "GATT not usable yet:",
        "Timed out waiting for usable GATT state",
        "notify/control characteristics never appeared",
        "No LIVE Joy-Con 2 advertisers found",
        "could not find a LEFT Joy-Con 2",
        "could not find a RIGHT Joy-Con 2",
        "StartDiscovery failed:",
        "provide --mac or use --auto",
    )

    return any(marker in s for marker in startup_markers)

async def _pick_auto_device(
    *,
    label: str,
    side: str,
    timeout_s: float,
    prefer_connected: bool,
) -> Dict[str, Any]:
    pick, _live, _stale = await discover_jc2(
        timeout_s=timeout_s,
        side_filter=side,
        prefer_connected=prefer_connected,
    )
    if not pick:
        raise SystemExit(f"ERROR: could not find a {label.upper()} Joy-Con 2 (no LIVE advertisers found).")
    return pick

def main():
    ap = argparse.ArgumentParser(prog="jc2mouse")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Show bluetooth session status")
    sub.add_parser("start", help="Enter jc2 session mode (stop stock bluetooth, start patched bluetoothd)")
    sub.add_parser("stop", help="Exit jc2 session mode (stop patched bluetoothd, restore stock bluetooth)")

    p_run = sub.add_parser("run", help="Run Joy-Con 2 / Nintendo Switch 2 controller driver")
    p_run.add_argument("--mac", help="Joy-Con 2 MAC address (e.g. 98:E2:55:DF:56:13)")
    p_run.add_argument("--auto", action="store_true", help="Auto-detect a supported Nintendo Switch 2 controller")
    p_run.add_argument("--side", choices=["any", "right", "left"], default="any",
                       help="When using --auto, filter Joy-Con selection by side")
    p_run.add_argument("--timeout", type=float, default=8.0, help="Auto-discovery scan time seconds (default: 8)")
    p_run.add_argument("--ask", action="store_true", help="If multiple LIVE devices match, ask which to use")
    p_run.add_argument("--no-prefer-connected", action="store_true",
                       help="When using --auto, do NOT prefer already-connected device")
    p_run.add_argument("--print-mfg", action="store_true", help="Print manufacturer hex in listings (developer)")
    p_run.add_argument("--no-status", action="store_true", help="Disable the one-line status output")
    p_run.add_argument("--status-hz", type=float, default=5.0, help="Status refresh rate (default: 5 Hz)")
    p_run.add_argument("--verbose", action="store_true", help="Developer verbosity (rarely needed)")

    # QoL / lifecycle
    p_run.add_argument(
        "--session",
        choices=["auto", "on", "off"],
        default="off",
        help="Bluetooth daemon handling: off=use current system bluetoothd (default), auto=start jc2 session if needed, on=force jc2 session",
    )
    p_run.add_argument("--leave-session", action="store_true",
                       help="When --session auto starts the session, do NOT stop it on exit")
    p_run.add_argument("--disconnect-on-exit", dest="disconnect_on_exit", action="store_true", default=False,
                       help="Best-effort disconnect controller(s) on exit")
    p_run.add_argument("--no-disconnect-on-exit", dest="disconnect_on_exit", action="store_false",
                       help="Do NOT disconnect controller(s) on exit")

    p_run.add_argument(
        "--combined",
        action="store_true",
        help="Use both Joy-Cons as one combined controller; Right C toggles right-side mouse overlay",
    )

    p_scan = sub.add_parser("scan", help="Scan and list LIVE Joy-Con 2 candidates (no connect)")
    p_scan.add_argument("--side", choices=["any", "right", "left"], default="any", help="Filter by side")
    p_scan.add_argument("--timeout", type=float, default=8.0, help="Scan time seconds (default: 8)")
    p_scan.add_argument("--print-mfg", action="store_true", help="Print manufacturer hex (developer)")

    p_map = sub.add_parser("dev-map-buttons", help="Developer: interactively discover button bit/byte positions")
    p_map.add_argument("--mac", required=True, help="Joy-Con 2 MAC address")

    p_ns2pro = sub.add_parser("ns2pro", help="Run Nintendo Switch 2 Pro Controller driver")
    p_ns2pro.add_argument("--mac", required=True, help="Switch 2 Pro Controller MAC address")
    p_ns2pro.add_argument("--status-hz", type=float, default=5.0, help="Status refresh rate (default: 5 Hz)")
    p_ns2pro.add_argument("--verbose", action="store_true", help="Developer verbosity")
    p_ns2pro.add_argument(
        "--session",
        choices=["auto", "on", "off"],
        default="off",
        help="Bluetooth daemon handling: off=use current system bluetoothd (default), auto=start jc2 session if needed, on=force jc2 session",
    )
    p_ns2pro.add_argument("--leave-session", action="store_true",
                          help="When --session auto starts the session, do NOT stop it on exit")
    p_ns2pro.add_argument("--disconnect-on-exit", dest="disconnect_on_exit", action="store_true", default=False,
                          help="Best-effort disconnect controller on exit")
    p_ns2pro.add_argument("--no-disconnect-on-exit", dest="disconnect_on_exit", action="store_false",
                          help="Do NOT disconnect controller on exit")

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

            pick, live, stale = await discover_jc2(
                timeout_s=args.timeout,
                side_filter=args.side,
                prefer_connected=False,
            )

            sys.stderr.write("\n[jc2] Auto-discovery (LIVE advertisers):\n")
            if not live:
                sys.stderr.write("  (none)\n")
            else:
                for i, c in enumerate(live, 1):
                    mfg_s = f"  mfg={_format_bytes(c['mfg'])}" if args.print_mfg else ""
                    sys.stderr.write(
                        f"  {i}) {c['mac']}  side={c['side']:<5}  rssi={c['rssi']:>4}{mfg_s}\n"
                    )

            if stale and args.print_mfg:
                sys.stderr.write("[jc2] Stale cache (ignored):\n")
                for c in stale:
                    rssi_s = "None" if c["rssi"] is None else str(c["rssi"])
                    sys.stderr.write(
                        f"  -  {c['mac']}  side={c['side']:<5}  rssi={rssi_s:>4}  mfg={_format_bytes(c['mfg'])}\n"
                    )
            sys.stderr.flush()

            if not live:
                raise RuntimeError("No LIVE Joy-Con 2 advertisers found.")

        try:
            asyncio.run(_do_scan())
        except KeyboardInterrupt:
            print("\n[jc2] Stopped.", file=sys.stderr)
            return 130
        except Exception as ex:
            print(f"ERROR: {ex}", file=sys.stderr)
            return 1
        return 0

    if args.cmd == "run":
        started_session = False
        chosen_macs: List[str] = []
        chosen_side: str = "unknown"
        chosen_kind: str = "unknown"
        skip_disconnect_cleanup = False

        async def _do_run():
            nonlocal started_session, chosen_macs, chosen_side, chosen_kind

            if args.combined:
                try:
                    started_session = _ensure_session(args.session)
                except Exception as ex:
                    raise SystemExit(f"ERROR: failed to enter session mode: {ex}")

                if not args.auto:
                    raise SystemExit("ERROR: combined mode currently requires --auto (it will auto-pick left+right).")

                prefer_connected = not args.no_prefer_connected

                sys.stderr.write("[jc2] Combined mode: we will pick LEFT then RIGHT.\n")
                sys.stderr.write("[jc2] Hold PAIR on the LEFT Joy-Con first until it appears.\n")
                sys.stderr.write("[jc2] Tip: If the controller disconnects on first attempt (LEDs stop flashing), hold pairing once more on the disconnected Joy-Con 2.\n")
                sys.stderr.flush()

                pick_l = await _pick_auto_device(
                    label="left",
                    side="left",
                    timeout_s=args.timeout,
                    prefer_connected=prefer_connected,
                )

                left_mac = pick_l["mac"]
                sys.stderr.write(f"[jc2] Picked LEFT  {left_mac} (rssi={pick_l.get('rssi')})\n")
                sys.stderr.write("[jc2] Now hold PAIR on the RIGHT Joy-Con.\n")
                sys.stderr.flush()

                pick_r = await _pick_auto_device(
                    label="right",
                    side="right",
                    timeout_s=args.timeout,
                    prefer_connected=prefer_connected,
                )

                right_mac = pick_r["mac"]
                sys.stderr.write(f"[jc2] Picked RIGHT {right_mac} (rssi={pick_r.get('rssi')})\n")
                sys.stderr.write("[jc2] Press Ctrl+C to stop.\n")
                if args.disconnect_on_exit:
                    sys.stderr.write("[jc2] Controllers will be disconnected on exit.\n")
                else:
                    sys.stderr.write("[jc2] Use --disconnect-on-exit to disconnect controllers on exit.\n")
                sys.stderr.flush()

                chosen_macs = [left_mac, right_mac]

                try:
                    await run_combined(
                        left_mac=left_mac,
                        right_mac=right_mac,
                        status=(not args.no_status),
                        status_hz=args.status_hz,
                        verbose=args.verbose,
                    )
                except DBusError as ex:
                    msg = _friendly_connect_error(ex)
                    raise SystemExit(f"ERROR: {msg}")
                except Exception as ex:
                    raise SystemExit(f"ERROR: {ex}")

                return

            try:
                started_session = _ensure_session(args.session)
            except Exception as ex:
                raise SystemExit(f"ERROR: failed to enter session mode: {ex}")

            mac = args.mac

            if args.auto:
                prefer_connected = not args.no_prefer_connected

                sys.stderr.write("[jc2] Auto mode: hold the PAIR button if the controller is not already connected.\n")
                sys.stderr.write("[jc2] Tip: use --ask if multiple Nintendo Switch 2 controllers are advertising.\n")
                sys.stderr.write("[jc2] Tip: avoid pressing other buttons while it’s connecting.\n")
                sys.stderr.flush()

                pick, live, stale = await discover_jc2(
                    timeout_s=args.timeout,
                    side_filter=args.side,
                    prefer_connected=prefer_connected,
                )

                if pick:
                    mac = pick["mac"]
                    chosen_side = pick.get("side", "unknown")
                    chosen_kind = pick.get("kind", "unknown")
                    sys.stderr.write(
                        f"[jc2] Auto-selected {mac} "
                        f"(kind={chosen_kind}, side={chosen_side}, rssi={pick.get('rssi')})\n"
                    )
                    sys.stderr.flush()

                    if chosen_kind == "ns2pro":
                        sys.stderr.write("[jc2] Nintendo Switch 2 Pro Controller detected.\n")
                    elif chosen_side == "left":
                        sys.stderr.write("[jc2] Left Joy-Con tip: hold L+ZL to toggle mouse/gamepad.\n")
                    elif chosen_side == "right":
                        sys.stderr.write("[jc2] Right Joy-Con tip: press C to toggle mouse/gamepad.\n")
                    else:
                        sys.stderr.write("[jc2] Tip: Right uses C; Left uses hold L+ZL to toggle.\n")
                    sys.stderr.flush()

                    if live and len(live) > 1:
                        sys.stderr.write("\n[jc2] LIVE advertisers:\n")
                        for i, c in enumerate(live, 1):
                            mfg_s = f"  mfg={_format_bytes(c['mfg'])}" if args.print_mfg else ""
                            sys.stderr.write(
                                f"  {i}) {c['mac']}  side={c['side']:<5}  rssi={c['rssi']:>4}{mfg_s}\n"
                            )
                        sys.stderr.flush()

                    if args.ask and live and len(live) > 1 and sys.stdin.isatty():
                        while True:
                            choice = input(f"Select device [1-{len(live)}] (Enter=best): ").strip()
                            if choice == "":
                                break
                            try:
                                idx = int(choice)
                                if 1 <= idx <= len(live):
                                    mac = live[idx - 1]["mac"]
                                    chosen_side = live[idx - 1].get("side", "unknown")
                                    chosen_kind = live[idx - 1].get("kind", "unknown")
                                    sys.stderr.write(
                                        f"[jc2] Selected {mac} (kind={chosen_kind}, side={chosen_side})\n"
                                    )
                                    sys.stderr.flush()
                                    break
                            except ValueError:
                                pass
                            print("Invalid choice.", file=sys.stderr)

            if not mac:
                raise SystemExit("ERROR: provide --mac or use --auto")

            chosen_macs = [mac]

            sys.stderr.write("[jc2] Press Ctrl+C to stop.\n")
            if args.disconnect_on_exit:
                sys.stderr.write("[jc2] Controller will be disconnected on exit.\n")
            else:
                sys.stderr.write("[jc2] Use --disconnect-on-exit to disconnect the controller on exit.\n")
            sys.stderr.flush()

            try:
                if chosen_kind == "ns2pro":
                    await run_ns2_pro(
                        mac,
                        status_hz=args.status_hz,
                        verbose=args.verbose,
                    )
                else:
                    await run_driver(
                        mac,
                        status=(not args.no_status),
                        status_hz=args.status_hz,
                        verbose=args.verbose,
                    )
            except DBusError as ex:
                msg = _friendly_connect_error(ex)
                raise SystemExit(f"ERROR: {msg}")
            except Exception as ex:
                raise SystemExit(f"ERROR: {ex}")

        try:
            asyncio.run(_do_run())
        except KeyboardInterrupt:
            print("\n[jc2] Stopped.", file=sys.stderr)
            return 130
        except SystemExit as ex:
            if _should_skip_disconnect_cleanup_on_error(ex):
                skip_disconnect_cleanup = True
            raise
        except Exception as ex:
            if _should_skip_disconnect_cleanup_on_error(ex):
                skip_disconnect_cleanup = True
            raise
        finally:
            if args.disconnect_on_exit and chosen_macs and (not skip_disconnect_cleanup):
                for mac in reversed(chosen_macs):
                    try:
                        asyncio.run(_disconnect_device_by_mac(mac))
                    except Exception:
                        pass

            if started_session and (not args.leave_session):
                if _service_is_masked("bluetooth.service"):
                    print(
                        "\n[jc2] NOTE: bluetooth.service is masked; leaving jc2 session active "
                        "(otherwise there would be no bluetooth daemon running).",
                        file=sys.stderr
                    )
                    print("[jc2] To restore normal bluetooth later:", file=sys.stderr)
                    print("      sudo systemctl unmask bluetooth.service", file=sys.stderr)
                    print("      sudo systemctl enable --now bluetooth.service", file=sys.stderr)
                else:
                    try:
                        _call_session("stop")
                    except Exception:
                        pass

        return 0

    if args.cmd == "dev-map-buttons":
        try:
            asyncio.run(run_button_wizard(args.mac))
        except KeyboardInterrupt:
            print("\n[jc2] Stopped.", file=sys.stderr)
            return 130
        return 0

    if args.cmd == "ns2pro":
        started_session = False
        chosen_macs: List[str] = []
        skip_disconnect_cleanup = False

        async def _do_ns2pro():
            nonlocal started_session, chosen_macs

            try:
                started_session = _ensure_session(args.session)
            except Exception as ex:
                raise SystemExit(f"ERROR: failed to enter session mode: {ex}")

            chosen_macs = [args.mac.upper()]

            sys.stderr.write("[ns2pro] Press Ctrl+C to stop.\n")
            if args.disconnect_on_exit:
                sys.stderr.write("[ns2pro] Controller will be disconnected on exit.\n")
            else:
                sys.stderr.write("[ns2pro] Use --disconnect-on-exit to disconnect the controller on exit.\n")
            sys.stderr.flush()

            try:
                await run_ns2_pro(
                    args.mac,
                    status_hz=args.status_hz,
                    verbose=args.verbose,
                )
            except DBusError as ex:
                msg = _friendly_connect_error(ex)
                raise SystemExit(f"ERROR: {msg}")
            except Exception as ex:
                raise SystemExit(f"ERROR: {ex}")

        try:
            asyncio.run(_do_ns2pro())
        except KeyboardInterrupt:
            print("\n[ns2pro] Stopped.", file=sys.stderr)
            return 130
        except SystemExit as ex:
            if _should_skip_disconnect_cleanup_on_error(ex):
                skip_disconnect_cleanup = True
            raise
        except Exception as ex:
            if _should_skip_disconnect_cleanup_on_error(ex):
                skip_disconnect_cleanup = True
            raise
        finally:
            if args.disconnect_on_exit and chosen_macs and (not skip_disconnect_cleanup):
                for mac in reversed(chosen_macs):
                    try:
                        asyncio.run(_disconnect_device_by_mac(mac))
                    except Exception:
                        pass

            if started_session and (not args.leave_session):
                if _service_is_masked("bluetooth.service"):
                    print(
                        "\n[jc2] NOTE: bluetooth.service is masked; leaving jc2 session active "
                        "(otherwise there would be no bluetooth daemon running).",
                        file=sys.stderr
                    )
                    print("[jc2] To restore normal bluetooth later:", file=sys.stderr)
                    print("      sudo systemctl unmask bluetooth.service", file=sys.stderr)
                    print("      sudo systemctl enable --now bluetooth.service", file=sys.stderr)
                else:
                    try:
                        _call_session("stop")
                    except Exception:
                        pass

        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
