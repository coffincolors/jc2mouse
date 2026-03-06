# jc2mouse

`jc2mouse` is a Linux userspace tool for using **Nintendo Switch 2 Joy-Con 2 controllers** and the **Nintendo Switch 2 Pro Controller** over BLE, with support for **bondless / no-pairing workflows**.

It currently supports:

- **Single Joy-Con 2 mode**
  - mouse mode
  - compact gamepad mode
- **Combined Joy-Con 2 mode**
  - Left + Right as one full virtual controller
  - optional **right-side mouse overlay**
- **Nintendo Switch 2 Pro Controller mode**
  - full gamepad via uinput

---

## Current state of BlueZ / session mode

Originally, this project depended on a **patched BlueZ session-mode workflow** to work around BLE / GATT behavior seen on some systems.

On newer systems, **stock BlueZ may already work well enough** for jc2mouse without needing the patched session daemon.

Because of that, the project now treats patched BlueZ as:

- an **optional fallback**
- a **troubleshooting path**
- a way to test behavior against a custom BlueZ build when stock behavior is unreliable

So the normal recommendation is:

1. **Try jc2mouse with your stock Bluetooth stack first**
2. If your controller still drops, fails to enumerate correctly, or behaves inconsistently, then try the **patched BlueZ session mode**

---

## Features

### 1) Single Joy-Con 2 mode

#### Right Joy-Con 2

- Optical mouse motion
- Mouse buttons from controller buttons
- Stick-based scroll
- Toggle mouse/gamepad mode with **C**

#### Left Joy-Con 2

- Optical mouse motion
- Mouse buttons from controller buttons
- Stick-based scroll
- Toggle mouse/gamepad mode with **hold L + ZL**

### 2) Combined Joy-Con 2 mode

- Left + Right exposed as one full virtual gamepad
- Xbox-style face-button layout for better Linux / Steam compatibility
- Right stick, left stick, shoulders, triggers, d-pad, etc.
- **Right-only mouse overlay**:
  - press **C** on the Right Joy-Con
  - Right side becomes mouse
  - Left side continues as the left half of the controller

### 3) Nintendo Switch 2 Pro Controller

- Auto-detected from the main CLI when supported advertisements are visible
- Exposed as a full virtual gamepad
- Can also be run directly through the dedicated ns2pro path if needed

---

## Repository layout

- `src/jc2mouse/cli.py` — CLI, discovery, session handling
- `src/jc2mouse/driver.py` — Joy-Con 2 driver logic
- `src/jc2mouse/ns2pro.py` — Switch 2 Pro Controller driver
- `src/jc2mouse/mapper.py` — developer button-mapping helper
- `scripts/setup.sh` — installs helper/service files
- `scripts/jc2-session.sh` — session-mode helper
- `scripts/build_bluez.sh` — optional patched BlueZ build/install script
- `systemd/jc2-bluetooth.service` — optional patched session-mode bluetoothd unit

---

## Requirements

- Linux
- Python 3.10+ recommended
- root privileges for uinput / Bluetooth control
- working BlueZ / D-Bus environment
- `uinput` available

---

## Installation

### 1) Python environment

From the repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
```

### 2) Install the helper/service files

```bash
sudo scripts/setup.sh
```

This installs:

- `/usr/local/sbin/jc2-session`
- `/etc/systemd/system/jc2-bluetooth.service`

Note: installing these files does **not** force you to use patched BlueZ. It just prepares the optional fallback path.

### 3) Optional: build patched BlueZ

Only do this if stock Bluetooth is unreliable on your system, or if you want to test the custom session-mode path.

```bash
sudo scripts/build_bluez.sh
```

This installs a patched bluetoothd under:

```text
/opt/jc2mouse/bluez
```

---

## Basic usage

### Show CLI help

```bash
jc2mouse --help
jc2mouse run --help
jc2mouse scan --help
```

If using a virtualenv under sudo:

```bash
sudo -E .venv/bin/jc2mouse --help
```

---

## Running with stock Bluetooth first

This is now the recommended first test.

### Auto-detect and run a single controller

```bash
sudo -E .venv/bin/jc2mouse run --auto --session off
```

### Auto-detect combined Joy-Con mode

```bash
sudo -E .venv/bin/jc2mouse run --auto --combined --session off
```

### Filter Joy-Con side

```bash
sudo -E .venv/bin/jc2mouse run --auto --side left --session off
sudo -E .venv/bin/jc2mouse run --auto --side right --session off
```

### Verbose mode

```bash
sudo -E .venv/bin/jc2mouse run --auto --verbose --session off
```

### Logging to a file

```bash
sudo -E .venv/bin/jc2mouse run --auto --verbose --session off 2>&1 | tee debug/jc2mouse.log
```

---

## Optional patched BlueZ session mode

If stock Bluetooth is unstable, try session mode.

### Start the patched session manually

```bash
sudo jc2-session start
```

### Check status

```bash
sudo jc2-session status
```

### Stop the patched session

```bash
sudo jc2-session stop
```

### Run jc2mouse with session mode auto-handling

```bash
sudo -E .venv/bin/jc2mouse run --auto
```

The default `--session auto` behavior will attempt to use the optional session helper path when appropriate.

If you explicitly want to avoid any session changes:

```bash
sudo -E .venv/bin/jc2mouse run --auto --session off
```

---

## Common examples

### Single Joy-Con 2

```bash
sudo -E .venv/bin/jc2mouse run --auto --session off
```

### Combined Joy-Con 2 controller

```bash
sudo -E .venv/bin/jc2mouse run --auto --combined --session off
```

### Scan only

```bash
sudo -E .venv/bin/jc2mouse scan --timeout 8
```

### Switch 2 Pro Controller via main auto path

```bash
sudo -E .venv/bin/jc2mouse run --auto --session off
```

If the advertised device is recognized as an NS2 Pro controller, the CLI will route into the `ns2pro` driver path automatically.

### Dedicated ns2pro path

```bash
sudo -E .venv/bin/jc2mouse ns2pro --mac 38:C6:CE:29:60:44 --session off
```

---

## Stopping the app

Press:

```text
Ctrl+C
```

The CLI will stop cleanly.

If you also want the controller disconnected on exit, use:

```bash
--disconnect-on-exit
```

Examples:

```bash
sudo -E .venv/bin/jc2mouse run --auto --session off --disconnect-on-exit
sudo -E .venv/bin/jc2mouse run --auto --combined --session off --disconnect-on-exit
```

---

## Troubleshooting

### 1) Stock works, but `--session auto` fails

If session mode is failing but stock Bluetooth works, just run with:

```bash
--session off
```

Then inspect:

```bash
systemctl status jc2-bluetooth.service --no-pager -l
journalctl -u jc2-bluetooth.service -n 100 --no-pager
```

### 2) bluetooth.service is masked

```bash
sudo systemctl unmask bluetooth.service
sudo systemctl enable --now bluetooth.service
```

### 3) Controller not found

- hold the controller **PAIR** button
- avoid pressing other buttons while it is connecting
- try `scan` first
- use `--ask` if multiple compatible devices are advertising

### 4) Combined mode can require another attempt

On some systems, if the first Joy-Con falls out of pairing state before the second side is ready, you may need to press **PAIR** again on the side that dropped.

### 5) Optical init can sometimes stay idle

If a Joy-Con enters mouse mode but optical data does not become active immediately, re-run and try again. This is known to occasionally happen during bring-up and is not always a permanent failure.

---

## Notes

- This project name started from the Joy-Con 2 mouse workflow, but it now also supports broader Nintendo Switch 2 controller use cases.
- The optional patched BlueZ path is still kept in the repo because it remains useful for regression testing and for systems where stock behavior is not sufficient.

---

## Development notes

Useful local commands:

```bash
git status
git add README.md scripts/setup.sh scripts/build_bluez.sh src/jc2mouse/cli.py src/jc2mouse/driver.py src/jc2mouse/ns2pro.py .gitignore
git commit -m "Add Switch 2 Pro support and refresh CLI/session docs"
```

---

## License

See repository license / project preferences.
