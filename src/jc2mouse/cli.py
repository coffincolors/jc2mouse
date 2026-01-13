import argparse
import sys

def main():
    ap = argparse.ArgumentParser(prog="jc2mouse")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Show bluetooth session status")
    sub.add_parser("start", help="Enter jc2 session mode (stop stock bluetooth, start patched bluetoothd)")
    sub.add_parser("stop", help="Exit jc2 session mode (stop patched bluetoothd, restore stock bluetooth)")
    sub.add_parser("setup", help="Install systemd unit + helper scripts (one-time)")

    args = ap.parse_args()

    print(f"[stub] command={args.cmd}", file=sys.stderr)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
