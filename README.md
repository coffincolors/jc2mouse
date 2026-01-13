# jc2mouse — Joy-Con 2 Optical Mouse (bondless / no pairing)

This tool runs Joy-Con 2's optical sensor as a real Linux mouse cursor without pairing/bonding.

## Important
This project currently relies on two system-level behaviors:
1) **Kernel**: Linux Bluetooth SMP must not send pairing requests (Joy-Con 2 disconnects if it receives one).
2) **BlueZ**: A patched `bluetoothd` is used to force MEDIUM security for unpaired LE connections.

## Session Mode (safe / reversible)
`jc2mouse` temporarily:
- Stops the system `bluetooth.service`
- Starts a bundled patched `bluetoothd` (org.bluez)
- Runs the driver
- Restores stock Bluetooth on exit

## Status
- Optical stream enable (FF) works
- 16-bit counter deltas decode correctly
- uinput mouse device works (EV_REL + EV_KEY buttons)

Next: implement D-Bus GATT client, discovery UX, reconnect, button mapping.

