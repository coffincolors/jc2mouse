import argparse
import asyncio
import os
import sys
import subprocess

from jc2mouse.driver import run as run_driver

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
            asyncio.run(run_driver(args.mac))
        except KeyboardInterrupt:
            print("\n[jc2] Stopped.", file=sys.stderr)
        return 0

    return 2

if __name__ == "__main__":
    raise SystemExit(main())
