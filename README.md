# Beacon — Live Laser Status App for LightBurn - BETA

Beacon watches [LightBurn](https://lightburnsoftware.com) and shows what your laser is doing —
**Running, Paused, Framing, Ready or Disconnected** — in a small window or floating pill, on a
USB or Wi-Fi **stack light**, and on your phone. It also times every job and keeps statistics.
Beacon allows for integrating Arduino hardware into your laser workflows, allowing nearly unlimited ways to interact with your lasers. 

**MUST HAVE LIGHTBURN LASER WINDOW OPEN IF THE VERSION IS LOWER THAN 2.2; IN VERSION 2.2 LIGHTBURN CAN BE MINIMIZED. Both old and new versions will work with Beacon minimized. Reason - OCR needs screen to be open, and 2.2 version uses API, which allows the LightBurn application to be minimized. 
**
Windows 10/11 (64-bit) and macOS 12.3 or newer. Free for noncommercial use — see [License](#license).

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

**Windows:** download **Beacon-Setup-x.y.z.exe** from the [Releases](../../releases) page and
run it. Windows may warn that the installer isn't signed: choose *More info › Run anyway*.

**Mac** (Apple Silicon or Intel): download **Beacon-x.y.z.pkg** and double-click it. The
installer walks you through a few pages, puts Beacon in Applications and opens it. Everything
Beacon needs is included. If you prefer, download **Beacon-x.y.z.dmg** instead, open it and drag
**Beacon** onto **Applications**.

If the installer isn't signed, macOS blocks it the first time. Open *System Settings › Privacy &
Security*, scroll down, choose *Open Anyway*, then open the installer again. Beacon then asks
for:

- **Screen Recording**: needed to read LightBurn's window. It isn't needed in API-only mode.
- **Local Network**: needed for the Wi-Fi light, smart plugs and the phone dashboard.
- **Notifications**: needed for pop-up alerts.

## Run from the source

1. Install [Python 3.10+](https://www.python.org/downloads/) (tick *Add Python to PATH*).
2. Double-click **`Install requirements.bat`** (once).
3. Double-click **`Start Laser Status.bat`**, or run `pythonw laser_status_app.py`.

On a Mac, install Python 3.12 or 3.13 from python.org. Double-click
**`Install requirements (Mac).command`** once, then **`Start Beacon (Mac).command`**. You can
also run `python3 laser_status_app.py` instead of the second file.

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

To build the Mac version, you need Python 3.12 or 3.13 from python.org and the Xcode Command
Line Tools (`xcode-select --install`). Run `installer/build_mac.sh`. It writes two files to
`installer/Output/`:

- `Beacon-<version>.pkg`: an installer with welcome, license and setup pages
- `Beacon-<version>.dmg`: a disk image to drag Beacon into Applications

The app is universal: it runs natively on both Apple Silicon and Intel Macs, whichever kind you
build on. Signing and notarizing with an Apple Developer ID is optional and described at the top
of the script.

## Project layout

| Path | What |
|---|---|
| `laser_status_app.py` | The app: LightBurn readers, window, pill, settings, statistics |
| `beacon/` | Modules: LightBurn API client, notifications, smart plugs, exports, phone dashboard, updater, Windows and macOS integration, detection log |
| `laser_stack_light/` | Arduino Uno sketch (stack light, buzzer, relays, safety inputs) |
| `esp32_stack_light/` | ESP32 Wi-Fi version of the same |
| `docs/hardware-kit.md` | Parts list, wiring and safety notes |
| `installer/` | Inno Setup script and Windows build script, `build_mac.sh` for the Mac .dmg / .pkg, installer license text |

## License

Copyright (c) 2026 the Beacon project. Licensed under the
[PolyForm Noncommercial License 1.0.0](LICENSE.md): you may use, study, change and share Beacon
for any **noncommercial** purpose. **Selling it or using it commercially is not allowed.**

LightBurn is a trademark of LightBurn Software, LLC. Beacon is an independent project and is not
affiliated with, endorsed or sponsored by LightBurn Software, LLC.
