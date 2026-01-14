import argparse
import asyncio
import os
import sys
import subprocess

# Avoid root-owned __pycache__ + permission weirdness
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

from jc2mouse.driver import run as run_driver
from jc2mouse.mapper import run_button_wizard


def _require_root():
    if os.geteuid() != 0:
        print("ERROR: must run as root (sudo) for uinput + bluetooth session control", file=sys.stderr)
        raise SystemExit(1)


def _call_session(cmd: str):
    subprocess.check_call(["/usr/local/sbin/jc2-session", cmd])


def main():
    ap = argparse.ArgumentParser(prog="jc2mouse")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Show bluetooth session status")
    sub.add_parser("start", help="Enter jc2 session mode (stop stock bluetooth, start patched bluetoothd)")
    sub.add_parser("stop", help="Exit jc2 session mode (stop patched bluetoothd, restore stock bluetooth)")

    p_run = sub.add_parser("run", help="Run optical mouse driver (requires session mode active)")
    p_run.add_argument("--mac", required=True, help="Joy-Con 2 MAC address (e.g. 98:E2:55:DF:56:13)")
    p_run.add_argument("--no-status", action="store_true", help="Disable the one-line status output")
    p_run.add_argument("--status-hz", type=float, default=5.0, help="Status refresh rate (default: 5 Hz)")
    p_run.add_argument("--verbose", action="store_true", help="Developer verbosity (rarely needed)")

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
    if args.cmd == "run":
        try:
            asyncio.run(
                run_driver(
                    args.mac,
                    status=(not args.no_status),
                    status_hz=args.status_hz,
                    verbose=args.verbose,
                )
            )
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
