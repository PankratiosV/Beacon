# Beacon — live laser status for LightBurn

Beacon watches [LightBurn](https://lightburnsoftware.com) and shows what your laser is doing —
**Running, Paused, Framing, Ready or Disconnected** — in a small window or floating pill, on a
USB or Wi-Fi **stack light**, and on your phone. It also times every job and keeps statistics.

Windows 10/11 (64-bit). Free for noncommercial use — see [License](#license).

## Features

- **Reads LightBurn three ways:** the screen (any LightBurn version), LightBurn's REST API
  (LightBurn 2.2+, works while LightBurn is minimised), or **both** together for the fastest,
  most accurate times.
- **Stack light, buzzer and relays** on an Arduino Uno (USB) or an ESP32 (Wi-Fi): red while
  running, green when ready, beeps when a job finishes, relay buttons for a fan or air assist.
- **Safety inputs:** fire/smoke (the board alarms on its own, even with the PC off),
  temperature, lid and exhaust/coolant flow sensors.
- **Notifications:** Windows, Telegram (with commands like `/status`), Discord, Pushover, email
  and SMS; hourly, daily and weekly usage reports.
- **Statistics:** run time per day and week, jobs, customers and materials, cost per job,
  a quote calculator and maintenance reminders; PDF/Excel monthly reports.
- **Phone dashboard** on your Wi-Fi, a detection-accuracy log, and more.

> **Safety:** Beacon is a convenience, **not a safety device**. Never leave a running laser
> unattended. See [docs/hardware-kit.md](docs/hardware-kit.md) before wiring anything.

## Install

Download **Beacon-Setup-x.y.z.exe** from the [Releases](../../releases) page and run it.
Windows may warn that the installer isn't signed: choose *More info › Run anyway*.

## Run from the source

1. Install [Python 3.10+](https://www.python.org/downloads/) (tick *Add Python to PATH*).
2. Double-click **`Install requirements.bat`** (once).
3. Double-click **`Start Laser Status.bat`**, or run `pythonw laser_status_app.py`.

### Dev mode

`python laser_status_app.py --dev` runs a separate **Beacon DEV** with its own settings
(in `.devdata/`), hotkey and ports, so it can run next to an installed Beacon.
`python make_shortcut.py --dev` makes a Start-menu shortcut for it. The bundled `.exe` ignores
`--dev`.

### Build the installer

Needs [Inno Setup 6](https://jrsoftware.org/isinfo.php) and Python 3.11+. Run
`installer\build_installer.bat`; it bundles the app with PyInstaller (in its own environment,
with the versions in `installer/build-requirements.txt`) and writes
`installer/Output/Beacon-Setup-<version>.exe`. Optional code signing is described at the top of
the script.

## Project layout

| Path | What |
|---|---|
| `laser_status_app.py` | The app: LightBurn readers, window, pill, settings, statistics |
| `beacon/` | Modules: LightBurn API client, notifications, smart plugs, exports, phone dashboard, updater, Windows integration, detection log |
| `laser_stack_light/` | Arduino Uno sketch (stack light, buzzer, relays, safety inputs) |
| `esp32_stack_light/` | ESP32 Wi-Fi version of the same |
| `docs/hardware-kit.md` | Parts list, wiring and safety notes |
| `installer/` | Inno Setup script, build script, installer license text |

## License

Copyright (c) 2026 the Beacon project. Licensed under the
[PolyForm Noncommercial License 1.0.0](LICENSE.md): you may use, study, change and share Beacon
for any **noncommercial** purpose. **Selling it or using it commercially is not allowed.**

LightBurn is a trademark of LightBurn Software, LLC. Beacon is an independent project and is not
affiliated with, endorsed or sponsored by LightBurn Software, LLC.
