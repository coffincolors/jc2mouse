# jc2mouse — Joy-Con 2 mouse + controller (bondless / no pairing)

`jc2mouse` is a Linux userspace tool that connects to Nintendo Switch 2 Joy-Con 2 controllers over BLE **without pairing/bonding**, enables the optical sensor stream, and exposes virtual input devices via **uinput**.

It supports:
- **Single Joy-Con mode**: mouse + compact gamepad mode
- **Combined mode (Left + Right)**: one full virtual Xbox-style controller
- **Mouse overlay in combined mode**: press **C** on the Right Joy-Con to turn *only the Right Joy-Con* into a mouse while the Left Joy-Con continues as the left half of the gamepad

---

## Why “session mode” is required

Joy-Con 2 will disconnect if Linux/BlueZ tries to enforce normal pairing/security behavior for LE connections.

This repo uses a patched `bluetoothd` in a safe, reversible **session mode**:
- stop `bluetooth.service`
- start `jc2-bluetooth.service` (patched bluetoothd)
- run `jc2mouse`
- restore stock Bluetooth on exit

The session swap is controlled by a helper script installed as:
- `/usr/local/sbin/jc2-session`

---

## Features

### Single Joy-Con mode

**Right Joy-Con 2**
- Mouse: optical motion -> `REL_X/REL_Y`
- Clicks: (default) R = left click, ZR = right click, R3 = middle click
- Scroll: stick Y deflection (smooth)
- Toggle mouse/gamepad: **C**

**Left Joy-Con 2**
- Mouse (optional): optical + clicks + stick scroll
- Toggle mouse/gamepad: **hold L + ZL**
- Gamepad mode uses an Xbox-style mapping (Steam/xpad compat handled)

### Combined mode (Left + Right)

- One virtual **Xbox-style** uinput controller:
  - Left stick -> `ABS_X/ABS_Y`
  - Right stick -> `ABS_RX/ABS_RY`
  - D-pad -> `BTN_DPAD_*`
  - ABXY -> Xbox positions (South=A, East=B, West=X, North=Y)
  - LB/LT from Left, RB/RT from Right
  - Start/Select/Guide mapped sensibly
- **Right mouse overlay in combined mode**
  - Press **C** on the Right Joy-Con:
    - Right becomes mouse (optical + buttons + scroll)
    - Right half of the gamepad is suppressed
    - Left continues driving the left half of the combined controller
  - Press **C** again to return to full combined controller

---

## Repository layout (high level)

- `src/jc2mouse/driver.py` — BLE (BlueZ D-Bus) + optical + uinput logic
- `src/jc2mouse/cli.py` — CLI entrypoint + auto discovery + session integration
- `scripts/build_bluez.sh` — builds patched BlueZ bluetoothd into `/opt/jc2mouse/bluez`
- `scripts/setup.sh` — installs systemd unit + session helper
- `scripts/jc2-session.sh` — start/stop/status session mode
- `systemd/jc2-bluetooth.service` — systemd unit for patched bluetoothd

---

## Setup

### 1) Install the systemd unit + session helper

This installs:
- `/etc/systemd/system/jc2-bluetooth.service`
- `/usr/local/sbin/jc2-session`

Run:

```bash
sudo scripts/setup.sh
```

### 2) Build and install patched bluetoothd

Build script downloads BlueZ (default 5.72), applies patches, and installs into:

- `/opt/jc2mouse/bluez`

Run:

```bash
sudo scripts/build_bluez.sh
```

The `jc2-bluetooth.service` unit uses:

```text
ExecStart=/opt/jc2mouse/bluez/libexec/bluetooth/bluetoothd -E -n -d
```

### 3) Python install (venv recommended)

From repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
```

---

## Usage

### Session mode

```bash
sudo jc2-session start
sudo jc2-session status
sudo jc2-session stop
```

### Running with a venv + sudo

`sudo` usually won’t see your venv PATH. Use one of these patterns:

**A) Explicit venv path**
```bash
sudo -E .venv/bin/jc2mouse run --auto
```

**B) Use `which` while the venv is activated**
```bash
sudo -E "$(which jc2mouse)" run --auto
```

### Single Joy-Con (auto)

```bash
sudo -E .venv/bin/jc2mouse run --auto
```

Filter by side:

```bash
sudo -E .venv/bin/jc2mouse run --auto --side left
sudo -E .venv/bin/jc2mouse run --auto --side right
```

### Combined mode (full controller)

```bash
sudo -E .venv/bin/jc2mouse run --auto --combined
```

Tips:
- Hold **PAIR** on Left until it connects, then hold **PAIR** on Right.
- Some systems require a retry or two due to unpaired BLE timing.

---

## Troubleshooting

### “Bluetooth LE connection was aborted locally” / `le-connection-abort-by-local`

Usually means the Joy-Con wasn’t connectable at the moment BlueZ attempted the connect.

Try:
1) Hold the **PAIR** button during the connection attempt.
2) Don’t press other buttons while connecting.
3) Retry the command; combined mode may need a retry for the second Joy-Con.

### `bluetooth.service` is masked

If stock Bluetooth is masked, session restore may fail.

Fix:

```bash
sudo systemctl unmask bluetooth.service
sudo systemctl enable --now bluetooth.service
```

### Service sanity checks

```bash
systemctl status jc2-bluetooth.service --no-pager
systemctl status bluetooth.service --no-pager
```

---

## Roadmap / QoL

- LED management (player LEDs / mode indicator)
- GUI frontend (mode notifications / smoother UX)
- IMU decode (gyro + accel) and DualShock/DualSense-style emulation mode
- Battery level reporting (investigate feasibility without bonding)
- Rumble
- Windows port + Android/Winlator Cmod integration

---

## License / Contributions

Open to contributions and issue reports. Please include logs and device info (distro, kernel, BlueZ version) when reporting bugs.
