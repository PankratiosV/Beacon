#!/usr/bin/env python3
"""
Beacon - live laser status for LightBurn (Windows 10/11 and macOS 12.3+)

Reads what LightBurn's Laser panel says (Ready / Busy / Paused / Framing /
Disconnected) and shows it in a small window or floating pill, on an Arduino
or ESP32 stack light, and on your phone. It times every job, keeps
statistics and sends notifications. Nothing in LightBurn or the laser is
changed: Beacon only reads.

Setup (once), in a Command Prompt:
    pip install customtkinter pyserial pywin32 pillow psutil winrt-runtime winrt-Windows.Foundation winrt-Windows.Foundation.Collections winrt-Windows.Media.Ocr winrt-Windows.Graphics.Imaging winrt-Windows.Storage.Streams winrt-Windows.Globalization
(or double-click "Install requirements.bat")
On macOS, in Terminal:
    pip3 install -r installer/requirements-mac.txt
(or double-click "Install requirements (Mac).command")

Run:
    double-click "Start Laser Status.bat" ("Start Beacon (Mac).command" on a
    Mac), or:  python laser_status_app.py
    python laser_status_app.py --dev     a separate dev copy (see DEV below)

Settings are saved in %APPDATA%\\LaserStatusV2\\settings.json
(macOS: ~/Library/Application Support/LaserStatusV2/settings.json)

Platform code: Windows uses Win32 (pywin32), Windows OCR (winrt) and
beacon/winsys.py; macOS uses Quartz, Vision and beacon/macsys.py +
beacon/maccapture.py. Both offer the same calls, imported as `osys`.

How the code fits together (top to bottom in this file):

  Settings            DEFAULTS, load_settings(), save_settings()
  Window capture      find LightBurn's windows and screenshot them
  Text recognition    WindowsOCR (built into Windows), TesseractOCR fallback
  Status matching     find_status(): OCR text -> "busy", "ready", ...
  Detection engine    background threads that read LightBurn and post what
                      they see to a queue:
                        Detector        reads the screen (any version)
                        ApiDetector     LightBurn 2.2+ REST API (beacon/lbapi.py)
                        HybridDetector  both together
  Light links         ArduinoLink (USB serial), NetLight (ESP32 over UDP);
                      the protocol is described above LinkBase
  RunLog              the job history (jobs.jsonl): running stretches only
  Look & feel         colours, rounded panels, the job-finished chime
  Notifications       Telegram calls, usage reports
  App                 the window, its pages and all the glue: poll() reads
                      the detector queue on the UI thread, set_state() turns
                      readings into status changes, render() redraws
  Chart, StatsView    the Statistics page
  main()              single-instance check, OCR start-up, App

Feature modules live in beacon/ (see the imports below). Arduino and ESP32
sketches are in laser_stack_light/ and esp32_stack_light/.

Threads: Tk may only be used from the main (UI) thread. Every background
thread hands results to the UI through App.q (see App._on_ui and App.poll).
"""

# Standard library only in this first block; the third-party packages are
# imported further down inside try/except so a missing one gives a friendly
# message box instead of a crash.
import asyncio
import csv
import ctypes
import gc
import difflib
import io
import json
import math
import os
import queue
import re
import shutil
import socket
import sys
import threading
import time
import tkinter as tk
from datetime import date, datetime, timedelta
from tkinter import filedialog, messagebox, ttk

# App identity. These few names decide what the user sees (window title,
# taskbar, installer shortcut) and where the settings live. Change APP_VERSION
# together with MyAppVersion in installer/LaserStatus.iss when you release.
APP_NAME = "Beacon"
HEADER_TEXT = "Live Laser Status"   # heading inside the main window (title bar says APP_NAME)
APP_VERSION = "2.1.0"   # keep in step with installer\LaserStatus.iss
# v2 has its own identity (name, settings folder, taskbar ID, single-instance
# port, installer ID) so it runs alongside the original without touching it
SETTINGS_FOLDER = "LaserStatusV2"

# Dev mode, for trying changes: run the source with --dev (the "Beacon (dev)"
# shortcut does). The bundled Beacon.exe that customers get ignores it.
# Dev has its own name, taskbar ID, single-instance port, hotkey, dashboard
# port and settings, so it runs next to the installed Beacon without touching
# its settings, stats or Telegram bot. Its settings live in .devdata next to
# this file (not %APPDATA%), so every tool that starts it sees the same files.
DEV = "--dev" in sys.argv and not getattr(sys, "frozen", False)
RELEASE_LABEL = "BETA"    # in the title and installer shortcuts; "" for a final release
WINDOW_TITLE = f"{APP_NAME} {APP_VERSION} {RELEASE_LABEL}".strip()
# Dev mode replaces the visible names so the two copies are easy to tell apart
# on the taskbar and in the title bar.
if DEV:
    APP_NAME = "Beacon (dev)"       # pairing, autostart, firewall rule: keep version-free
    WINDOW_TITLE = f"Beacon {APP_VERSION} DEV"
    HEADER_TEXT = "Live Laser Status · DEV"


def fatal(msg):
    """Show an error even when started without a console (pythonw)."""
    try:
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(APP_NAME, msg)
        r.destroy()
    except Exception:
        print(msg)
    sys.exit(1)


# Beacon reads other apps' windows through each system's own APIs (Win32 on
# Windows, Quartz on macOS), so it runs on those two only.
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
if not (IS_WIN or IS_MAC):
    fatal("This app is for Windows and macOS.")
OS_NAME = "Windows" if IS_WIN else "macOS"
COMPUTER = "PC" if IS_WIN else "Mac"

# Third-party packages. Each import is tried on its own and the names of the
# missing ones are collected, so a user running from source learns in one
# message exactly which `pip install` to run.
# - customtkinter: the modern-looking widgets the whole UI is built from
# - pywin32 (win32gui, win32ui, ...): finding and capturing LightBurn's window
#   on Windows; pyobjc (Quartz, Vision, AppKit) does that on macOS
# - psutil: telling which process owns a window ("is this LightBurn?")
# - pillow: images (screen captures, the rounded panels, the tray icon)
_missing = []
try:
    import customtkinter as ctk
except ImportError:
    _missing.append("customtkinter")
if IS_WIN:
    try:
        import win32con
        import win32gui
        import win32ui
        import win32process
    except ImportError:
        _missing.append("pywin32")
else:
    for _mod, _pkg in (("Quartz", "pyobjc-framework-Quartz"), ("Vision", "pyobjc-framework-Vision"),
                       ("AppKit", "pyobjc-framework-Cocoa")):
        try:
            __import__(_mod)
        except ImportError:
            _missing.append(_pkg)
try:
    import psutil
except ImportError:
    _missing.append("psutil")
try:
    from PIL import Image, ImageDraw, ImageTk
except ImportError:
    _missing.append("pillow")
if _missing:
    fatal("Missing packages: " + ", ".join(_missing) +
          ("\n\nOpen a Command Prompt and run:\npip install " if IS_WIN else
           "\n\nOpen Terminal and run:\npip3 install ") + " ".join(_missing))


def _smooth_wheel():
    """CustomTkinter scrolls a page once per mouse-wheel tick. A fast spin sends
    dozens of ticks in a burst and Windows never finishes redrawing the page's
    widgets between jumps, so text smears. Gather a burst into one jump at most
    every WHEEL_MS instead (same distance overall), and paint what each jump
    uncovered straight away (paint_now)."""
    from customtkinter.windows.widgets.ctk_scrollable_frame import CTkScrollableFrame
    original = CTkScrollableFrame._mouse_wheel_all
    original_init = CTkScrollableFrame.__init__
    # 16 ms is about one screen refresh at 60 Hz: the page moves at most once
    # per frame, which is as smooth as the eye can see anyway.
    WHEEL_MS = 16
    RDW_ALLCHILDREN, RDW_UPDATENOW = 0x80, 0x100

    def paint_now(canvas):
        """Windows moves a scrolled page at once but paints the parts it
        uncovered at low priority, behind further wheel ticks, so text left
        trails. Ask for just those pending paints now. (Repainting the whole
        page instead, RDW_INVALIDATE, also cured the trails but was too
        slow.) Measured on Settings with every category open, a fast spin:
        frames on screen 2-4 % wrong pixels (up to 21 %) before, 0.2 % (up to
        1.4 %) after."""
        if not sys.platform.startswith("win"):
            return
        canvas.update_idletasks()               # Tk moves the page...
        ctypes.windll.user32.RedrawWindow(canvas.winfo_id(), None, None,
                                          RDW_ALLCHILDREN | RDW_UPDATENOW)
        canvas.update_idletasks()               # ...and draws what Windows asked for

    def flush(self):
        """Apply all the wheel ticks gathered since the last jump in one scroll.
        Windows sends 120 per notch; CustomTkinter's own handler scrolls
        delta/6 units, so the same formula keeps the scroll distance
        unchanged."""
        self._wheel_job = None
        delta, self._wheel_delta = self._wheel_delta, 0
        steps = -int(delta / 6)
        try:
            if steps and self._parent_canvas.yview() != (0.0, 1.0):
                self._parent_canvas.yview("scroll", steps, "units")
                paint_now(self._parent_canvas)
        except tk.TclError:             # the page was closed meanwhile
            pass

    def init(self, *args, **kwargs):
        """Dragging the scrollbar moves the page too: paint after each move."""
        original_init(self, *args, **kwargs)
        if self._orientation == "vertical":
            canvas = self._parent_canvas

            def drag(*how):
                canvas.yview(*how)
                paint_now(canvas)
            self._scrollbar.configure(command=drag)

    def wheel(self, event):
        """Replacement wheel handler: only adds up the ticks and schedules one
        flush() a frame later. Shift+wheel (sideways scroll) keeps the
        original."""
        if not sys.platform.startswith("win") or self._shift_pressed:
            return original(self, event)
        if not self._check_if_valid_scroll(event.widget):
            return
        self._wheel_delta = getattr(self, "_wheel_delta", 0) + event.delta
        if not getattr(self, "_wheel_job", None):
            self._wheel_job = self.after(WHEEL_MS, lambda: flush(self))

    # Monkey-patch: every scrollable page in the app now uses the gathered
    # version. Done once at import time, before any window exists.
    CTkScrollableFrame._mouse_wheel_all = wheel
    CTkScrollableFrame.__init__ = init


_smooth_wheel()

try:
    # Optional packages: the app still runs without them, just without that
    # feature. pyserial talks to the Arduino over USB; winsound plays the
    # Windows chime.
    import serial
    import serial.tools.list_ports
    HAVE_SERIAL = True
except ImportError:
    HAVE_SERIAL = False
 
try:
    import winsound
except ImportError:
    winsound = None

# Beacon's own modules (folder beacon/), each one feature kept out of this file:
# - camera: webcam snapshot attached to alerts
# - channels: Discord, Pushover, email and SMS senders
# - exporters: CSV / Excel / PDF reports
# - smartplugs: Shelly / Tasmota / Home Assistant switches used as relays
# - updater: checks UPDATE_URL for a newer version
# - webdash: the phone dashboard (a tiny web server on your Wi-Fi)
# - winsys / macsys (imported as osys): tray or menu-bar icon, global hotkey,
#   autostart, firewall rule, idle time
# - lbapi: LightBurn 2.2+ REST API client (the "API" detection mode)
# - detectlog: the opt-in log of how accurately LightBurn was read
import logging
import platform
import subprocess
import webbrowser
import zipfile
from beacon import camera, channels, exporters, smartplugs, updater, webdash
from beacon import lbapi
if IS_WIN:
    from beacon import winsys as osys
else:
    from beacon import macsys as osys
from beacon.detectlog import DetectionLog

# Where the app looks for updates: a small JSON file you publish (see
# beacon/updater.py and installer/update-example.json). Empty = off.
UPDATE_URL = ""
SUPPORT_EMAIL = ""      # shown in Help when set, e.g. "support@yourshop.com"
TIP_URL = "https://paypal.me/SevaKorolev"   # tip page (Ko-fi, PayPal.me, GitHub Sponsors...); the setup's last page
TIP_LABEL = "PayPal  @SevaKorolev"          # what the tip button says under the ♥
ETRANSFER_EMAIL = "beacon.lasertools@protonmail.com"   # Interac e-Transfer tips (Canada); "" = hidden
                        # shows a "Tip $5" button when set
# Questions and answers shown on the Help page, in this order. Plain text; the
# page wraps them itself.
HELP_FAQ = [
    ("It says “Not found”.",
     "Open LightBurn and show the Laser panel (Window › Laser). LightBurn can sit behind "
     "other windows but not be minimised. Press the banner or Look again after moving it. "
     "With LightBurn 2.2 or newer, Settings › Detection › LightBurn API works even "
     "when LightBurn is minimised."),
    ("The status is wrong or flickers.",
     "Make the Laser panel a little wider so its status line isn't cut off, and keep the "
     "display scaling the same as when it worked. Settings › Check every 1 s is steadier."),
    ("The Arduino isn't found.",
     "Close the Arduino IDE's Serial Monitor (only one program can use the port), unplug and "
     "replug the USB cable, and check the Beacon sketch is uploaded."),
    ("The light stays on when the PC is off.",
     "The sketch switches everything off a few seconds after messages stop. If it doesn't, "
     "re-upload the latest sketch."),
    ("Telegram messages don't arrive.",
     "Send your bot any message first, then press Find to fill in the Chat ID, and Send test."),
    ("My phone can't open the dashboard.",
     ("The phone must be on the same Wi-Fi. Allow Beacon on Private networks in Windows "
      "Firewall, and check the PIN." if IS_WIN else
      "The phone must be on the same Wi-Fi. Allow Beacon in System Settings › Privacy & "
      "Security › Local Network, and check the PIN.")),
    ("Do safety sensors make the laser safe?",
     "No. They are an extra warning, not a certified safety system. Never leave a running laser "
     "unattended and keep an extinguisher nearby."),
]

# Own taskbar identity, so the window and a pinned shortcut share this app's
# icon instead of Python's. Must match make_shortcut.py.
APP_ID = "LaserStatusV2.Dev" if DEV else "LaserStatusV2.App"
# Bundled as an .exe (PyInstaller), files like the icon are unpacked to sys._MEIPASS
BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
ICON_PATH = os.path.join(BASE_DIR, "laser_status.ico")
if IS_WIN:
    try:
        # Tell Windows this process is "Beacon", not "Python", so the taskbar
        # groups the window with its pinned shortcut and shows Beacon's icon.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass

    try:
        # Per-monitor DPI awareness: without it Windows scales the window as a
        # blurry bitmap on high-DPI screens, and (important for OCR) window
        # coordinates and captures would be in scaled pixels that don't match
        # the real screen. SetProcessDPIAware is the older fallback for Windows
        # versions without shcore.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
elif "SSL_CERT_FILE" not in os.environ:
    # macOS: python.org's Python looks for trusted certificates in its own
    # folder, which a Mac without that Python (every customer's) doesn't have,
    # so HTTPS (Telegram, Discord, email, updates) would fail. Use the
    # certificate list bundled with the app (certifi) instead.
    try:
        import certifi
        os.environ["SSL_CERT_FILE"] = certifi.where()
    except ImportError:
        pass


def open_path(path):
    """Open a file or folder the way double-clicking it would (Explorer on
    Windows, Finder or the file's app on macOS)."""
    if IS_WIN:
        os.startfile(path)
    else:
        subprocess.Popen(["open", path])


# ===========================================================================
# Settings
# ===========================================================================
# All user data lives in one folder: %APPDATA%\LaserStatusV2 (dev mode: the
# .devdata folder next to this file). Files in it:
# - settings.json: everything in DEFAULTS below, saved as you change it
# - jobs.jsonl: one line per finished job (see RunLog)
# - open_job.json: the job running right now, so a crash doesn't lose it
# - job_meta.json: customer / material edits made later on the Statistics page
# - sounds/: your own copy of a chosen finish sound
# - logs/: beacon.log, the app's log (see setup_logging)
# - detection-check/: the opt-in detection log and its pictures
if IS_WIN:
    CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), SETTINGS_FOLDER)
else:
    CONFIG_DIR = os.path.join(os.path.expanduser("~/Library/Application Support"),
                              SETTINGS_FOLDER)
if DEV:
    CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".devdata")
CONFIG_PATH = os.path.join(CONFIG_DIR, "settings.json")
# Every setting and its default value. load_settings() starts from this and
# overlays what's saved, keeping only keys that exist here; so a new setting
# just needs a line here, and an old one removed from here is dropped from the
# file on the next save. Keys are grouped by the Settings section using them.
DEFAULTS = {
    "blink": False,          # blink for Framing / Disconnected
    "topmost": True,
    "chime": False,          # sound when a job finishes
    "chime_file": "",        # own copy of the chosen sound ("" = Windows chime)
    "notify_pc": False,      # Windows notification pop-ups
    "tg_token": "",          # Telegram bot token (from @BotFather)
    "tg_chat": "",           # Telegram chat to message
    "notify_finished": True,
    "notify_paused": True,
    "notify_lost": True,     # laser disconnected during a job
    "notify_started": False,
    "report_hourly": False,  # usage reports of laser-on time
    "report_daily": False,
    "report_daily_hour": 18,
    "report_weekly": False,
    "report_weekly_day": 6,  # 0 = Monday ... 6 = Sunday
    "report_weekly_hour": 18,
    "report_skip_empty": True,
    "report_tg": True,       # send reports to Telegram
    "report_pc": False,      # and/or as Windows notifications
    "report_last": None,     # {"hourly": ts, "daily": ts, "weekly": ts} of the last sent slot
    "appearance": "Dark",    # System / Light / Dark
    "interval": 1.0,         # seconds between checks
    "arduino": False,
    "port": "Auto-detect",
    "compact": False,
    "compact_pos": None,
    "diagnostics": False,
    "relays": None,          # filled in by load_settings()
    "costs": None,           # filled in by load_settings()
    # --- alerts
    "alert_long_run_min": 0,     # a job running longer than this (0 = off)
    "alert_unusual": False,      # ...or 3x longer than this job name usually takes
    "alert_long_pause_min": 0,   # paused longer than this (0 = off)
    "notify_stuck": True,
    "notify_free": False,        # laser free again after a job
    "free_after_min": 3,
    "notify_unreadable": True,   # can't read LightBurn for a minute
    "unattended_min": 0,         # nobody touched the PC this long during a job (0 = off)
    "notify_maintenance": True,
    # --- channels
    "quiet_on": False, "quiet_start": 22, "quiet_end": 7,
    "mute_until": 0,
    "tg_commands": True,
    "discord_on": False, "discord_url": "",
    "pushover_on": False, "pushover_token": "", "pushover_user": "",
    "email_on": False, "email_host": "", "email_port": 587, "email_user": "",
    "email_pass": "", "email_to": "",
    "sms_on": False, "twilio_sid": "", "twilio_token": "", "twilio_from": "", "sms_to": "",
    "sms_alerts_only": True,
    "report_other": False,       # also send reports to Discord / Pushover / email
    "snapshot_on": False, "camera_index": 0,
    # --- phone dashboard and Wi-Fi light
    "dash_on": False, "dash_port": 8765, "dash_pin": "",
    "wifi_on": False, "wifi_host": "beacon-light.local",
    # --- safety inputs (Arduino A1-A5 / ESP32)
    "sensor_fire": False, "fire_relay_off": -1,
    "sensor_temp": False, "temp_type": "LM35", "temp_max": 45,
    "sensor_door": False, "sensor_flow": False,
    "button_action": "none",     # none / mute / relay:<index>
    "ha_url": "", "ha_token": "",
    # --- business
    "client": "", "material": "", "clients": None, "materials": None,
    "shop_days": "weekdays", "shop_start": 8, "shop_end": 17, "goal_hours": 0,
    "maint": None,               # filled in by load_settings()
    "quote": None,
    # --- Windows
    "setup_done": False,
    "autostart": False, "tray_always": True, "hotkey_on": True, "hotkey_key": "B",
    # --- updates
    "auto_update": True, "update_last_check": 0, "skip_version": "",
    # --- beta: log how well LightBurn is read (beacon/detectlog.py)
    "check_log": False,
    "settings_order": [],        # Settings page sections, top to bottom ([] = default order)
    # --- how LightBurn is read: "ocr" (screen, any version) or "api" (LightBurn 2.2+)
    "detect_mode": "ocr",
    "lb_api_secret": "",         # from pairing; LightBurn can revoke it any time
    "lb_api_trusted": {},        # devices whose "connected" flag can be believed
}

# Starting maintenance reminders (Statistics › Maintenance). every_h = laser-on
# hours between services, last_h = the total laser hours when it was last done.
MAINT_DEFAULTS = [
    {"name": "Clean the lens / protective window", "every_h": 40, "last_h": 0.0},
    {"name": "Clean the exhaust fan and duct", "every_h": 100, "last_h": 0.0},
    {"name": "Check belts, rails and mirrors", "every_h": 200, "last_h": 0.0},
    {"name": "Cooling check (chiller water / fans)", "every_h": 500, "last_h": 0.0},
]
# Last values typed into the quote calculator, so it opens as you left it.
QUOTE_DEFAULTS = {"minutes": 10.0, "qty": 1, "material": 0.0, "setup": 0.0, "margin": 50.0}
if DEV:     # don't fight the installed Beacon for its hotkey or dashboard port
    DEFAULTS.update(hotkey_key="D", dash_port=8766)

# Running-cost inputs used to turn laser time into money (cost_per_hour):
# electricity (power_kw x kwh_price), wear (machine_price / lifetime_h) and the
# per-hour maintenance, labour and other costs.
COST_DEFAULTS = {"currency": "$", "power_kw": 0.5, "kwh_price": 0.15, "machine_price": 0.0,
                 "lifetime_h": 10000.0, "maint_h": 0.0, "labor_h": 0.0, "other_h": 0.0}

# Uno pins for extra relays (0/1 are USB serial; 4, 8, 9, 10 are the stack
# light, buzzer and buzzer switch; A1-A5 are the safety inputs).
# Order must match RELAY_PINS in laser_stack_light.ino.
RELAY_PINS = ["D2", "D3", "D5", "D6", "D7", "D11", "D12", "D13", "A0"]
# Choices offered in the relay editor. The dict key is what's saved, the value
# is what the dropdown shows.
ON_DELAYS = {0: "At once", 5: "After 5 s", 10: "After 10 s", 30: "After 30 s", 60: "After 1 min"}
SCHED_DAYS = {"every": "Every day", "weekdays": "Mon–Fri", "weekends": "Sat–Sun"}
# key: (label, statuses that switch the relay on by itself)
AUTO_MODES = {
    "manual":  ("Manual only", ()),
    "running": ("While running", ("busy",)),
    "job":     ("While running or paused", ("busy", "paused")),
    "framing": ("While framing", ("framing",)),
    "active":  ("Running, paused or framing", ("busy", "paused", "framing")),
}
# How long an automatic relay stays on after its status ends (e.g. keep the
# exhaust fan running a minute after the job to clear the smoke).
KEEP_ON = {0: "No delay", 10: "+10 s after", 30: "+30 s after", 60: "+1 min after",
           120: "+2 min after", 300: "+5 min after"}


def default_relays():
    """One settings dict per relay pin, all off and manual. Fields: name (button
    label), use (show the button), auto (an AUTO_MODES key), keep (KEEP_ON
    seconds), on_delay (ON_DELAYS seconds), sched_* (switch on by time of
    day), target ("arduino" pin, or a smart plug / Home Assistant switch at
    address / entity)."""
    return [{"name": f"Relay {i + 1}", "use": False, "auto": "manual", "keep": 0,
             "on_delay": 0, "sched_on": False, "sched_start": 8, "sched_end": 17,
             "sched_days": "every", "target": "arduino", "address": "", "entity": ""}
            for i in range(len(RELAY_PINS))]


def load_settings():
    """Read settings.json over DEFAULTS and repair anything missing or of the
    wrong type, so the rest of the app can trust every key exists. A missing
    or broken file just gives the defaults (first run)."""
    s = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            s.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except Exception:
        pass
    # Merge saved relays field by field into fresh defaults: a settings file
    # from an older version (fewer fields or fewer relays) still loads cleanly.
    relays = default_relays()
    for r, saved in zip(relays, s.get("relays") or []):
        if isinstance(saved, dict):
            r.update({k: saved[k] for k in r if k in saved})
    s["relays"] = relays
    costs = dict(COST_DEFAULTS)
    if isinstance(s.get("costs"), dict):
        costs.update({k: v for k, v in s["costs"].items() if k in costs})
    s["costs"] = costs
    if not isinstance(s.get("maint"), list):
        s["maint"] = [dict(t) for t in MAINT_DEFAULTS]
    quote = dict(QUOTE_DEFAULTS)
    if isinstance(s.get("quote"), dict):
        quote.update({k: v for k, v in s["quote"].items() if k in quote})
    s["quote"] = quote
    for key in ("clients", "materials"):
        if not isinstance(s.get(key), list):
            s[key] = []
    return s


def save_settings(s):
    """Write all settings to settings.json. Failures (disk full, folder
    read-only) are ignored on purpose: losing a setting is better than a
    crash."""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except Exception:
        pass


# ===========================================================================
# Finding and capturing LightBurn's windows
# ===========================================================================
# PrintWindow flag that asks the window to draw itself fully into our bitmap,
# including GPU-drawn (Qt / DirectX) content like LightBurn's. Without it the
# capture is often black.
PW_RENDERFULLCONTENT = 2
# pid -> process name. Asking Windows for a process name is slow-ish and the
# window list is walked every check, so names are remembered.
_name_cache = {}


def process_name(pid):
    """The .exe name of a process ("LightBurn.exe"), or "" if it's gone or
    protected."""
    if pid not in _name_cache:
        try:
            _name_cache[pid] = psutil.Process(pid).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            _name_cache[pid] = ""
    return _name_cache[pid]


def find_lightburn_windows(include_minimized=False):
    """Handles (hwnd) of LightBurn's visible top-level windows that are big
    enough to hold the Laser panel (tiny tool windows and pop-ups are
    skipped). Minimised windows report a tiny size, so they're only included
    on request; they can't be read on screen but the title bar still names
    the open file."""
    hwnds = []

    def callback(hwnd, _):
        """Called by EnumWindows once per top-level window. Return True to keep
        going."""
        if not win32gui.IsWindowVisible(hwnd):
            return True
        if win32gui.IsIconic(hwnd):
            if include_minimized:       # minimised windows report a tiny size
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if "lightburn" in process_name(pid).lower():
                    hwnds.append(hwnd)
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if "lightburn" not in process_name(pid).lower():
            return True
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        if (r - l) >= 200 and (b - t) >= 120:
            hwnds.append(hwnd)
        return True

    win32gui.EnumWindows(callback, None)
    return hwnds


def lightburn_running():
    """True if any LightBurn process exists, even with no window open (used to
    tell "LightBurn is closed" from "the Laser panel isn't visible")."""
    for p in psutil.process_iter(["name"]):
        if "lightburn" in (p.info.get("name") or "").lower():
            return True
    return False


def capture_window(hwnd):
    """Screenshot one window into a PIL image by asking it to paint itself
    (PrintWindow). Works even when other windows cover it, which a plain
    screen grab can't. Returns None if the window refuses.

    The Win32 objects (device contexts, bitmap) must always be released,
    hence the try/finally: leaking them every second would exhaust GDI
    handles."""
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = r - l, b - t
    if w <= 0 or h <= 0:
        return None
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    try:
        bmp.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bmp)
        if not ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), PW_RENDERFULLCONTENT):
            return None
        info = bmp.GetInfo()
        data = bmp.GetBitmapBits(True)
        return Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]),
                                data, "raw", "BGRX", 0, 1)
    finally:
        win32gui.DeleteObject(bmp.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)


def is_blank(img):
    """True for an all-one-colour image (shrunk to 64x64 and greyscale first, so
    it's quick). Some windows answer PrintWindow with a black or white
    picture."""
    lo, hi = img.convert("L").resize((64, 64)).getextrema()
    return hi - lo < 10


def capture_best(hwnd):
    """A screenshot of the window, by whichever method gives a real picture:
    PrintWindow first (works when covered), then a plain screen grab of the
    window's rectangle (works for windows that refuse PrintWindow but only
    shows what's actually visible). None if both fail."""
    img = capture_window(hwnd)
    if img is not None and not is_blank(img):
        return img
    try:
        from PIL import ImageGrab
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        img = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True)
        if not is_blank(img):
            return img
    except Exception:
        pass
    return None


def window_title(hwnd):
    """A window's title bar text."""
    return win32gui.GetWindowText(hwnd)


# On macOS the same calls come from beacon/maccapture.py (Quartz instead of
# Win32; window IDs stand in for hwnds), replacing the Windows ones above.
if IS_MAC:
    from beacon.maccapture import (VisionOCR, capture_best, find_lightburn_windows,
                                   lightburn_running, screen_permission, window_title)


# ===========================================================================
# Text recognition. read() returns [(line_text, [(word, x, y, w, h), ...])]
# with positions in the coordinates of the image passed in.
# ===========================================================================
class WindowsOCR:
    """Text recognition with the OCR engine built into Windows 10/11 (no extra
    install, fast). Talks to it through the winrt Python packages.

    The WinRT API is async, so a private asyncio event loop is kept and each
    call is run to completion on it from the detector thread."""
    name = "Windows OCR"

    def __init__(self):
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.graphics.imaging import (
            SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode)
        from winrt.windows.storage.streams import DataWriter
        import winrt.windows.foundation.collections  # noqa: F401  (needed for results)
        self.SoftwareBitmap = SoftwareBitmap
        self.BitmapPixelFormat = BitmapPixelFormat
        self.BitmapAlphaMode = BitmapAlphaMode
        self.DataWriter = DataWriter
        self.engine = OcrEngine.try_create_from_user_profile_languages()
        if self.engine is None:
            raise RuntimeError("No Windows OCR language installed "
                               "(Settings > Time & language > Language: add English).")
        self.max_dim = OcrEngine.max_image_dimension
        self.loop = asyncio.new_event_loop()
        self.working = None

    def read(self, img, max_scale=2.0):
        """Recognise the text in img. Small images are enlarged (up to
        max_scale) because the engine reads tiny UI text much better at 2x;
        never beyond the engine's maximum image size. Word boxes are scaled
        back to img's own coordinates so callers can crop at the right place."""
        scale = min(max_scale, self.max_dim / max(img.width, img.height))
        if abs(scale - 1.0) > 0.01:
            img = img.resize((max(1, int(img.width * scale)),
                              max(1, int(img.height * scale))), Image.LANCZOS)
        result = self._recognize_any(img)
        out = []
        for line in result.lines:
            words = []
            for w in line.words:
                r = w.bounding_rect
                words.append((w.text, r.x / scale, r.y / scale, r.width / scale, r.height / scale))
            out.append((line.text, words))
        return out

    def _recognize_any(self, img):
        """Run OCR, trying the ways of building a Windows bitmap until one
        works. Different winrt package versions accept different call
        signatures, so the first that succeeds is remembered (self.working)
        and tried first next time."""
        order = ([self.working] if self.working else []) + \
                [m for m in (self._bmp_copy4, self._bmp_copy5, self._bmp_decoder)
                 if m is not self.working]
        errors = []
        for make_bitmap in order:
            try:
                bitmap = self.loop.run_until_complete(make_bitmap(img))
                result = self.loop.run_until_complete(self.engine.recognize_async(bitmap))
                self.working = make_bitmap
                return result
            except Exception as e:
                errors.append(f"{make_bitmap.__name__}: {e}")
        raise RuntimeError(" / ".join(errors))

    def _bgra_buffer(self, img):
        """The image's pixels as a WinRT buffer in BGRA order (what Windows
        wants)."""
        writer = self.DataWriter()
        writer.write_bytes(img.convert("RGBA").tobytes("raw", "BGRA"))
        return writer.detach_buffer()

    async def _bmp_copy4(self, img):
        """Build the bitmap straight from pixels (4-argument form)."""
        return self.SoftwareBitmap.create_copy_from_buffer(
            self._bgra_buffer(img), self.BitmapPixelFormat.BGRA8, img.width, img.height)

    async def _bmp_copy5(self, img):
        """Same, with the alpha mode given (5-argument form some versions need)."""
        return self.SoftwareBitmap.create_copy_from_buffer(
            self._bgra_buffer(img), self.BitmapPixelFormat.BGRA8, img.width, img.height,
            self.BitmapAlphaMode.PREMULTIPLIED)

    async def _bmp_decoder(self, img):
        """Slowest but most compatible: encode a PNG in memory and let Windows
        decode it into a bitmap."""
        from winrt.windows.storage.streams import InMemoryRandomAccessStream
        from winrt.windows.graphics.imaging import BitmapDecoder
        png = io.BytesIO()
        img.save(png, format="PNG")
        stream = InMemoryRandomAccessStream()
        writer = self.DataWriter(stream.get_output_stream_at(0))
        writer.write_bytes(png.getvalue())
        await writer.store_async()
        await writer.flush_async()
        writer.detach_stream()
        stream.seek(0)
        decoder = await BitmapDecoder.create_async(stream)
        return await decoder.get_software_bitmap_async()


class TesseractOCR:
    """Fallback OCR using Tesseract (a separate free install), for PCs where
    Windows OCR isn't available (no OCR language pack installed)."""
    name = "Tesseract"

    def __init__(self):
        import pytesseract
        for default in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                        "/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract"):
            if os.path.exists(default):
                pytesseract.pytesseract.tesseract_cmd = default
                break
        pytesseract.get_tesseract_version()
        self.pt = pytesseract

    def read(self, img, max_scale=2.0):
        """Same contract as WindowsOCR.read. Tesseract returns words with block
        / paragraph / line numbers; words are grouped back into lines here.
        "--psm 11" = sparse text: find as much text as possible in no set
        order, which suits a UI screenshot better than page layout analysis."""
        s = max_scale
        big = img.resize((int(img.width * s), int(img.height * s)), Image.LANCZOS).convert("L")
        d = self.pt.image_to_data(big, config="--psm 11", output_type=self.pt.Output.DICT)
        lines = {}
        for i, word in enumerate(d["text"]):
            if not word.strip():
                continue
            key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
            lines.setdefault(key, []).append(
                (word, d["left"][i] / s, d["top"][i] / s, d["width"][i] / s, d["height"][i] / s))
        return [(" ".join(w[0] for w in ws), ws) for ws in lines.values()]


def make_ocr():
    """The first OCR engine that starts, as (engine, None), or (None, reasons)
    when none can be used."""
    errors = []
    for cls in ((WindowsOCR if IS_WIN else VisionOCR), TesseractOCR):
        try:
            return cls(), None
        except Exception as e:
            errors.append(f"{cls.name}: {e}")
    return None, "\n".join(errors)


# ===========================================================================
# Status matching
# ===========================================================================
# Turning OCR text into a status. LightBurn's Laser panel shows one of these
# words. If several are found on screen (another panel may mention "Ready"), the
# earliest in this list wins: a job that's running matters more.
STATUS_WORDS = ["paused", "busy", "framing", "disconnected", "ready"]   # priority order
if IS_MAC:      # a status box with none of these is read again, more carefully
    VisionOCR.retry_words = frozenset(STATUS_WORDS)


def _close_enough(word, target):
    """True if an OCR'd word is the status word, allowing one misread letter in
    long words ("disconected", "framimg"): OCR on small UI text isn't
    perfect."""
    if word == target:
        return True
    # Short words must match exactly so "read"/"already" don't count.
    if len(target) < 7 or abs(len(word) - len(target)) > 1:
        return False
    return difflib.SequenceMatcher(None, word, target).ratio() >= 0.8


def _resume_below(rect, resumes):
    """True if a "Resume" button sits just under the status word (the Laser
    panel's Pause button turns into Resume while the job is paused)."""
    x, y, w, h = rect
    return any(y + h * 0.5 < ry < y + h * 8 and x - h * 3 < rx < x + h * 35
               for rx, ry in resumes)


def find_status(lines):
    """Returns (status, (x, y, w, h) or None). "Framing" right after "Live"
    is the Live Framing window's title, not the status, so it's skipped.
    LightBurn keeps saying "Busy" while paused, so Busy with a Resume button
    right below it counts as paused."""
    found = {}
    resumes = []
    for text, words in lines:
        toks = words or [(t, None, None, None, None) for t in text.split()]
        prev = ""
        for wtext, x, y, w, h in toks:
            for part in re.findall(r"[a-z]+", wtext.lower()):
                if "resum" in part and x is not None:
                    resumes.append((x, y))
                for target in STATUS_WORDS:
                    if _close_enough(part, target) and not (target == "framing" and prev == "live"):
                        found.setdefault(target, (x, y, w, h) if x is not None else None)
                prev = part
    for target in STATUS_WORDS:
        if target in found:
            rect = found[target]
            if target == "busy" and rect and _resume_below(rect, resumes):
                return "paused", rect
            return target, rect
    return None, None


# Used to tell Paused from Busy on LightBurn versions that keep showing "Busy"
# during a pause: the button under the status reads "Resume" instead.
def find_pause_button(lines):
    """Returns ("pause" or "resume", (x, y, w, h)) for a Pause/Resume button."""
    for text, words in lines:
        for wtext, x, y, w, h in words:
            low = wtext.lower()
            if "resum" in low:
                return "resume", (x, y, w, h)
            if "pause" in low and "paused" not in low:
                return "pause", (x, y, w, h)
    return None, None


# ===========================================================================
# Detection engine (runs in a background thread)
# ===========================================================================
# How the screen reader works, in short:
# 1. Find LightBurn's windows and screenshot them.
# 2. OCR the whole window once to find where the status word is.
# 3. From then on only OCR a small box around that spot (much faster), until the
#    word isn't there any more; then go back to step 2.
# 4. Debounce: a new status must be read the same way twice in a row before it
#    is reported (one misread frame can't flash the light).
# The result goes to the UI thread through a queue (out_q); the detector never
# touches the window itself, because Tk isn't thread-safe.
FULL_SCAN_GAP = 1.0     # seconds between whole-window scans, whatever the check speed
SKIP = object()         # scan() result meaning "nothing read this time"
# "63%", "63 %" but not the "3%" inside "163%". Used for the job's progress.
PROGRESS_RE = re.compile(r"(?<!\d)(\d{1,3})\s?%")


def parse_progress(lines):
    """The job's % from the Laser panel's progress bar text (e.g. "63%"), or None."""
    for text in lines:
        for m in PROGRESS_RE.finditer(text):
            v = int(m.group(1))
            if 0 <= v <= 100:
                return v
    return None


def lightburn_file_name(windows):
    """The open file's name from LightBurn's title bar, e.g. "Coasters" from
    "Coasters.lbrn2 * - JCZFiber - LightBurn Pro 2.1.04"."""
    for h in windows:
        title = window_title(h)
        if "lightburn" in title.lower() and "live framing" not in title.lower():
            name = title.split(" - ")[0].strip().rstrip("*").strip()
            name = re.sub(r"\.lbrn2?$", "", name, flags=re.I)
            return "" if name in ("<untitled>", "untitled") else name
    return ""


class Detector(threading.Thread):
    """Once the status readout has been found, only that small area is read
    each time (fast, light on CPU). If it disappears, the whole window is
    scanned again."""

    # State kept between checks (all of it belongs to this thread):
    # - region: where the status word was last found, so only that box is read
    # - shown: the confirmed status being reported; pending/pending_count: a
    #   different status seen but not yet confirmed (see debounce)
    # - lf_*: the same "remember where it was" trick for the Live Framing window
    # - since: set when a change really began earlier than this check (the
    #   debounce wait or the API's hold), so the UI can backdate job times
    def __init__(self, ocr, out_q, interval):
        super().__init__(daemon=True)
        self.ocr = ocr
        self.out_q = out_q
        self.interval = interval
        self.stop_evt = threading.Event()
        self.region = None          # (hwnd, (x0, y0, x1, y1))
        self.shown = None
        self.pending, self.pending_count = None, 0
        self._pending_at = 0.0      # when the pending status was first read
        self.last_full = 0.0
        self.lf_region = None       # (hwnd, box) of Live Framing's Pause/Resume button
        self.lf_word = None
        self.lf_last_full = 0.0
        self.file_name = ""
        self.crop = None            # picture of the status area just read (detection check)
        self.since = None           # when a new status really began, if earlier than now

    def reset(self):
        """Forget where the status was and look at every window again."""
        self.region = self.lf_region = None
        self.last_full = self.lf_last_full = 0.0

    def run(self):
        """The thread's loop: scan, debounce, send one message to the UI, sleep
        until the next check. Every message carries everything the UI needs
        (the status, raw reading, a note for the banner, the OCR text for
        Diagnostics, progress %, the open file's name and the picture read),
        so the UI never has to ask the detector anything. A scan that crashes
        is logged and reported as "Detection error" instead of killing the
        thread."""
        while not self.stop_evt.is_set():
            start = time.time()
            self.crop = self.since = None
            try:
                status, note, lines, mode = self.scan()
            except Exception as e:
                logging.exception("detection failed")
                status, note, lines, mode = None, "Detection error", [f"[error: {e}]"], "error"
            if status is not SKIP:
                self.debounce(status)
                progress = self.progress_from(lines) if self.shown in ("busy", "paused") else None
                self.out_q.put({"shown": self.shown, "raw": status, "note": note,
                                "lines": lines, "mode": mode, "progress": progress,
                                "file": self.file_name, "crop": self.crop,
                                "since": self.since})
            # No LightBurn window: checking for its process is slow, so don't rush
            # Sleep only what's left of the interval, so checks stay evenly
            # spaced however long the OCR took (never less than 20 ms, to leave
            # the CPU some air).
            gap = max(self.interval, 1.0) if mode == "none" else self.interval
            self.stop_evt.wait(max(0.02, gap - (time.time() - start)))

    def progress_from(self, lines):
        """The job's progress % for this reading. Screen: parsed from the OCR
        text. The API reader overrides this with the number the API gives."""
        return parse_progress(lines)

    CONFIRM_READS = 2   # readings in a row before Ready / Framing / Disconnected show

    def debounce(self, status):
        # Worked example at 0.5 s checks: Ready, Ready, *Framing*, Framing ->
        # the first Framing is pending (count 1), the second confirms it (count
        # 2) and `since` is set to the time of the first one, half a second
        # earlier.
        # Busy/Paused show at once; other changes need CONFIRM_READS reads in a
        # row ("not found" needs 3 and at least 1 s) so one misread can't flip
        # the light. A confirmed change is dated from its first reading
        # (self.since), so the confirming doesn't change job times. (A job's
        # end is held back separately, in App.set_state.)
        now = time.time()
        if status == self.shown:
            self.pending, self.pending_count = None, 0
            return
        if status == self.pending:
            self.pending_count += 1
        else:
            self.pending, self.pending_count, self._pending_at = status, 1, now
        if status in ("busy", "paused"):
            needed = 1
        elif status is None:
            needed = max(3, round(1.0 / self.interval))
        else:
            needed = self.CONFIRM_READS
        if self.pending_count >= needed:
            self.shown, self.pending, self.pending_count = status, None, 0
            if self.since is None and needed > 1:
                self.since = self._pending_at

    def refine(self, status, windows):
        """While Live Framing is open its own button is the truth: the Laser
        panel's button can stay on "Resume" after the job is resumed."""
        if status not in ("busy", "paused"):
            return status
        button = self.live_framing_button(windows)
        if button == "resume":
            return "paused"
        if button == "pause":
            return "busy"
        return status

    def live_framing_button(self, windows):
        """"pause" / "resume" read from LightBurn's Live Framing window if it's
        open (None otherwise). Uses the same remembered-box trick as scan():
        a quick read of the button's area, and a full read of the window at
        most once per FULL_SCAN_GAP when the button isn't where it was."""
        hwnd = next((h for h in windows
                     if "live framing" in window_title(h).lower()), None)
        if hwnd is None:
            self.lf_region, self.lf_word = None, None
            return None
        img = capture_best(hwnd)
        if img is None:
            return self.lf_word
        if self.lf_region and self.lf_region[0] == hwnd:
            x0, y0, x1, y1 = self.lf_region[1]
            x1, y1 = min(x1, img.width), min(y1, img.height)
            if x1 - x0 > 10 and y1 - y0 > 5:
                word, _ = find_pause_button(self.ocr.read(img.crop((x0, y0, x1, y1)),
                                                          max_scale=3.0))
                if word:
                    self.lf_word = word
                    return word
        # Read the whole Live Framing window, at most once a second
        if time.time() - self.lf_last_full < FULL_SCAN_GAP:
            return self.lf_word
        self.lf_last_full = time.time()
        word, rect = find_pause_button(self.ocr.read(img, max_scale=2.0))
        if rect:
            x, y, w, h = rect
            self.lf_region = (hwnd, (max(0, int(x - h * 3)), max(0, int(y - h)),
                                     int(x + w + h * 4), int(y + h * 2)))
        else:
            self.lf_region = None
        self.lf_word = word
        return word

    def scan(self):
        """One screen reading. Returns (status, note, lines, mode):
        - status: a STATUS_WORDS word, None (can't tell) or SKIP (didn't
          look)
        - note: a short explanation for the banner when status is None
        - lines: the text read, for Settings › Diagnostics
        - mode: how it was read: "region" (quick box), "full" (whole window),
          "wait" (skipped to keep full scans 1 s apart), "none" (no window)"""
        windows = find_lightburn_windows()
        try:
            self.file_name = lightburn_file_name(windows)
        except Exception:
            pass
        if not windows:
            self.region = None
            note = ("LightBurn is minimized. Restore it so the status can be read."
                    if lightburn_running() else "LightBurn isn't open")
            return None, note, [], "none"

        # 1) Quick read of the remembered area
        if self.region and self.region[0] in windows:
            hwnd, box = self.region
            img = capture_best(hwnd)
            if img is not None:
                x0, y0, x1, y1 = box
                x1, y1 = min(x1, img.width), min(y1, img.height)
                if x1 - x0 > 20 and y1 - y0 > 10:
                    self.crop = img.crop((x0, y0, x1, y1))
                    lines = self.ocr.read(self.crop, max_scale=3.0)
                    status, _ = find_status(lines)
                    if status:
                        return self.refine(status, windows), "", [t for t, _ in lines], "region"
        self.region = None

        # 2) Full scan of every LightBurn window (heavy, so at most once a second)
        if time.time() - self.last_full < FULL_SCAN_GAP:
            return SKIP, "", [], "wait"
        self.last_full = time.time()
        all_text = []
        any_capture = False
        for hwnd in windows:
            img = capture_best(hwnd)
            if img is None:
                continue
            any_capture = True
            lines = self.ocr.read(img, max_scale=2.0)
            all_text.extend(t for t, _ in lines)
            status, rect = find_status(lines)
            if status:
                if rect and rect[0] is not None:
                    # Remember a box around the word: a little left/above it,
                    # wide enough for the whole status line and tall enough to
                    # also hold the Pause/Resume button row, so the next quick
                    # reads can still tell Paused from Busy.
                    x, y, w, h = rect
                    # k: pixels per screen point (2 for a Retina Mac capture,
                    # see beacon/maccapture.py), so the box covers the same area
                    k = img.info.get("scale", 1)
                    # tall enough to include the Pause/Resume button row below
                    self.region = (hwnd, (max(0, int(x - 30 * k)), max(0, int(y - 18 * k)),
                                          int(x + 380 * k), int(y + h * 8 + 18 * k)))
                    x0, y0, x1, y1 = self.region[1]
                    self.crop = img.crop((x0, y0, min(x1, img.width), min(y1, img.height)))
                return self.refine(status, windows), "", all_text, "full"
        if not any_capture:
            if IS_MAC and not screen_permission():
                return None, ("Allow Beacon in System Settings › Privacy & Security › "
                              "Screen & System Audio Recording, then quit and reopen "
                              "Beacon"), all_text, "full"
            return None, "LightBurn's window captured blank", all_text, "full"
        return None, "Can't see LightBurn's status readout", all_text, "full"


class ApiDetector(Detector):
    """Reads the status from LightBurn's REST API (LightBurn 2.2+) instead of
    the screen, so it keeps working with LightBurn minimised or covered.
    Sends the same messages as Detector.

    The API reports a job as idle / running / paused, plus whether a device
    link is open. Some controllers (Ruida and similar) report "not connected"
    even while working, so "not connected" only counts as Disconnected for a
    device that has been seen connected while idle (remembered per device in
    `trusted`, a dict saved with the settings)."""

    # How the API's answer becomes a status (see scan below):
    # - job "idle": Ready, or Disconnected if the device is trusted and says it
    #   isn't connected
    # - job "paused": Paused
    # - job "running": Busy or Framing; the API doesn't say which, so it's
    #   worked out from the progress number in running_status()
    TRUST_READS = 10            # idle + connected readings in a row before trusting it
    # Framing: the API has no framing state, but (measured on a JCZ galvo with
    # LightBurn 2.2, 26 Oct) framing reports "running" with progress hovering at
    # ~98-99.9 (a loop can start anywhere from ~80 and jump there within 0.4 s,
    # or start at 100 for ~0.6 s), while a job reports exactly 100, or climbs
    # slowly from low values. A job started from framing goes straight from
    # 99.x to 100.
    FRAME_BAND = 90.0
    START_HOLD = 1.0            # a run starting at 100 from idle: wait this long before "job"
    LOW_HOLD = 1.0              # a run below FRAME_BAND must stay there this long to be a job

    def __init__(self, out_q, interval, secret, trusted, on_trust=None):
        super().__init__(None, out_q, interval)
        self.secret = secret
        self.trusted = trusted          # {device name: True}
        self.on_trust = on_trust        # called (from this thread) when a device is learned
        self.api_progress = None
        self._idle_connected = 0
        self._device = None
        self._run_start = None          # when the current running/paused phase began
        self._run_low = False           # progress went below FRAME_BAND: a real, climbing job
        self._last_progress = None
        self._full_since = None         # when progress last reached 100 (a job, after the hold)
        self._from_framing = False      # this 100 came straight after framing values
        self._low_since = None          # when progress first read below FRAME_BAND this run

    # At most 5 API requests a second, whatever "Check every" says: 10 a second
    # coincided with LightBurn misbehaving; 5 a second ran for 12 minutes of
    # framing, jobs and pauses with LightBurn answering in 4-24 ms.
    MIN_INTERVAL = 0.2
    CONFIRM_READS = 1           # the API says what LightBurn knows; no misreads to filter

    @property
    def interval(self):
        """Seconds between checks. The setter enforces MIN_INTERVAL, so neither
        the Settings slider nor a hand-edited settings file can flood
        LightBurn."""
        return self._interval

    @interval.setter
    def interval(self, value):
        self._interval = max(float(value), self.MIN_INTERVAL)

    def running_status(self, progress, now):
        """"busy" or "framing" for a running report. While a run is still
        ambiguous it shows as Framing (never a false Running); once it's
        clearly a job, the job is dated from when it really began."""
        # Each running reading moves through these rules (the instance variables
        # set in __init__ remember the run so far):
        # 1. First reading of a run: start a fresh record of it.
        # 2. Progress dropping by more than 5 means a new phase (e.g. the galvo
        #    goes back to framing after the job), so forget that it was
        #    climbing.
        # 3. Arriving at exactly 100 is noted with its time and whether it came
        #    straight from framing values (90-99.9).
        prev = self._last_progress
        if self._run_start is None:
            self._run_start, self._run_low, self._low_since = now, False, None
            self._from_framing = False
        elif progress is not None and prev is not None and progress < prev - 5:
            self._run_low, self._low_since = False, None    # a new phase (framing after a job)
        if progress is not None and progress >= 100 and (prev is None or prev < 100):
            self._full_since = now
            # straight from framing values to exactly 100: a job started from the frame
            self._from_framing = prev is not None and self.FRAME_BAND <= prev < 100
        self._last_progress = progress
        if progress is None:
            return "busy"
        if progress < self.FRAME_BAND and not self._run_low:
            # framing loops can start low too, but jump to ~99 within a reading or two;
            # a real job stays low while it climbs
            if self._low_since is None:
                self._low_since = now
            if now - self._low_since < self.LOW_HOLD:
                return "framing"
            self._run_low = True
            if self.shown not in ("busy", "paused"):
                self.since = self._low_since
        elif progress >= self.FRAME_BAND and not self._run_low:
            self._low_since = None
        if self._run_low:
            return "busy"
        if progress < 100:
            return "framing"
        # Exactly 100 is a job, but framing can also start at 100 for ~0.6 s. From
        # framing it's a job at once; from idle, watch START_HOLD first.
        if self.shown not in ("busy", "paused"):
            if not self._from_framing and now - self._full_since < self.START_HOLD:
                return "framing"
            self.since = self._full_since
        return "busy"

    def progress_from(self, lines):
        """The API's own progress number (set by scan), not OCR text."""
        return self.api_progress

    def scan(self):
        """One API reading: fetch /api/events/poll through lbapi.snapshot() and
        map it to a status (see the overview above TRUST_READS). Returns the
        same (status, note, lines, mode) as Detector.scan; `lines` here is a
        readable summary of the API answer for Settings › Diagnostics.

        401/403 means LightBurn no longer accepts our pairing secret (revoked
        or reinstalled): the note tells the user to connect again. Any other
        error while LightBurn isn't running just means "LightBurn isn't
        open"."""
        self.api_progress = None
        try:
            self.file_name = lightburn_file_name(find_lightburn_windows(include_minimized=True))
        except Exception:
            pass
        if not self.secret:
            return (None, "Beacon isn't connected to LightBurn's API yet: press Connect in "
                          "Settings › Detection.", [], "api-error")
        try:
            snap = lbapi.snapshot(self.secret)
        except lbapi.ApiError as e:
            if e.code in (401, 403):
                return None, str(e), [], "api-error"
            if not lightburn_running():
                return None, "LightBurn isn't open", [], "none"
            return None, str(e), [], "api-error"
        job = snap.get("job") or {}
        conn = snap.get("connection") or {}
        state, connected = job.get("state"), conn.get("connected")
        device = conn.get("device_name") or ""
        lines = [f"job: {state}", f"progress: {job.get('progress', '-')}",
                 f"connected: {connected}", f"device: {device}"]
        # A different laser was selected in LightBurn: start counting its idle +
        # connected readings from zero (trust is per device).
        if device != self._device:
            self._device, self._idle_connected = device, 0
        p = job.get("progress")
        p = float(p) if isinstance(p, (int, float)) else None

        if state == "running":
            status = self.running_status(p, time.time())
            if status is SKIP:
                return SKIP, "", lines, "api"
        elif state == "paused":
            status = "paused"
        elif state == "idle":
            self._run_start, self._last_progress = None, None
            if connected:
                self._idle_connected += 1
                # 10 idle readings in a row reporting "connected" prove this
                # controller reports its link honestly, so from now on "not
                # connected" really means the laser is unplugged or off.
                # on_trust saves the trusted list to settings.
                if self._idle_connected >= self.TRUST_READS and not self.trusted.get(device):
                    self.trusted[device] = True
                    if self.on_trust:
                        self.on_trust()
            else:
                self._idle_connected = 0
            status = ("disconnected" if connected is False and self.trusted.get(device)
                      else "ready")
        else:
            return None, f"LightBurn reported an unknown job state: {state}", lines, "api"
        self.api_progress = p if status in ("busy", "paused") else None
        return status, "", lines, "api"


class HybridDetector(Detector):
    """Screen reading and LightBurn's API together ("Both" in Settings).

    While the screen can be read, its status is shown (it reacts fastest and
    sees Framing directly), and every change is dated from whichever reader
    saw the new state first. When LightBurn is minimised or the screen can't
    be read, the API takes over. Both sub-readers are driven from this one
    thread; the API is asked at most ApiDetector.MIN_INTERVAL apart."""

    def __init__(self, ocr, out_q, interval, secret, trusted, on_trust=None):
        super().__init__(ocr, out_q, interval)
        # The two sub-readers are ordinary Detector / ApiDetector objects whose
        # scan() and debounce() are called from this thread's scan(); their own
        # threads are never started. They share out_q but never put to it (only
        # run() does that, and theirs doesn't run).
        self.screen = Detector(ocr, out_q, interval)            # not started: run from here
        self.api = ApiDetector(out_q, interval, secret, trusted, on_trust)
        self.source = None                                     # "screen" or "api" right now
        self._api_at = 0.0
        self._api_read = (None, "", [], "api-error")
        self._raw = {}                  # source -> its latest raw status
        self._raw_since = {}            # source -> when that raw status first appeared
        self.api_progress = None

    def reset(self):
        """Forget remembered screen areas in both this object and the screen
        sub-reader."""
        super().reset()
        self.screen.reset()

    def _mark(self, source, status, now):
        """Remember when a reader's raw status last changed: the raw time is the
        earliest moment that reader saw the new state, before any confirming."""
        if self._raw.get(source) != status:
            self._raw[source], self._raw_since[source] = status, now

    def progress_from(self, lines):
        """Prefer the API's progress (exact) and fall back to the screen's %."""
        return self.api_progress if self.api_progress is not None else parse_progress(lines)

    def debounce(self, status):
        """No debouncing here: each sub-reader has already confirmed its own
        status in scan()."""
        self.shown = status             # the sub-readers have already confirmed it

    def scan(self):
        """Read both sources and decide what to show:
        1. The screen reader scans and debounces (every check).
        2. The API is asked if MIN_INTERVAL has passed since the last
           request.
        3. If the screen gave a confirmed status, show it; otherwise
           (LightBurn minimised, covered by a full-screen app, OCR failing)
           show the API's.
        4. A new status is dated from the earliest moment either reader saw
           it, which makes job times as accurate as the faster reader allows."""
        now = time.time()
        # Screen
        sc = self.screen
        sc.crop = sc.since = None
        s_status, s_note, s_lines, s_mode = sc.scan()
        if s_status is not SKIP:
            sc.debounce(s_status)
            self._mark("screen", s_status, now)
            self._screen_read = (s_note, s_lines, s_mode)
        self.file_name, self.crop = sc.file_name, sc.crop
        # API (at most 5 a second)
        api = self.api
        if now - self._api_at >= ApiDetector.MIN_INTERVAL:
            self._api_at = now
            api.since = None
            a = api.scan()
            if a[0] is not SKIP:
                api.debounce(a[0])
                self._mark("api", a[0], now)
                self._api_read = a
        self.api_progress = api.api_progress if api.shown in ("busy", "paused") else None

        if sc.shown is not None:        # the screen can be read: show it
            status, self.source = sc.shown, "screen"
            note, lines, mode = getattr(self, "_screen_read", ("", [], "region"))
            if mode in ("wait", "none"):
                mode = "region"
        else:                           # minimised / unreadable: the API alone
            status, self.source = api.shown, "api"
            _, note, lines, mode = self._api_read
            if status is None and self._raw.get("screen") is None and mode != "none":
                note = note or getattr(self, "_screen_read", ("",))[0]
        # Date a change from whichever reader saw the new state first
        if status is not None and status != self.shown:
            firsts = [self._raw_since[s] for s in ("screen", "api")
                      if self._raw.get(s) == status and s in self._raw_since]
            for sub in (sc, api):
                if sub.shown == status and sub.since:
                    firsts.append(sub.since)
            if firsts:
                self.since = min(firsts)
        lines = ([f"reading: {'screen + API' if self.source == 'screen' else 'API only'}"]
                 + list(lines))
        return status, note, lines, mode


# ===========================================================================
# Arduino link (background thread)
# ===========================================================================
def list_ports():
    """Serial ports for the Settings dropdown as (device, description), e.g.
    ("COM5", "USB-SERIAL CH340 (COM5)"). Bluetooth serial ports are hidden:
    opening one can hang for many seconds."""
    if not HAVE_SERIAL:
        return []
    out = []
    for p in serial.tools.list_ports.comports():
        # macOS names it in the device path (/dev/cu.Bluetooth-Incoming-Port)
        if "bluetooth" in f"{p.description or ''} {p.device}".lower():
            continue
        out.append((p.device, p.description or p.device))
    return out


def find_arduino_port():
    """The first port that looks like an Arduino (Auto-detect), or None. Matches
    by name and by USB vendor ID: 2341 / 2a03 are Arduino, 1a86 is the CH340
    chip on most Uno clones."""
    if not HAVE_SERIAL:
        return None
    for p in serial.tools.list_ports.comports():
        text = f"{p.description or ''} {p.manufacturer or ''} {p.hwid or ''} {p.device}".lower()
        if "bluetooth" in text:
            continue
        if any(k in text for k in ("arduino", "ch340", "usb-serial", "usb serial",
                                   "vid:pid=2341", "vid:pid=2a03", "vid:pid=1a86")):
            return p.device
    return None


# The Uno sends this line every 250 ms; the ESP32 sends it as its reply to each
# message (see the .ino sketches). temp raw is the analog reading 0-1023; the
# app converts it to degrees for the chosen sensor type. presses counts button
# presses since the board started, so the app can spot new presses by comparing
# with the last value.
def parse_inputs(line):
    """The board's sensor report "S,<fire>,<temp raw>,<door open>,<flow>,<button presses>"
    as a dict, or None."""
    parts = line.strip().split(",")
    if len(parts) != 6 or parts[0] != "S":
        return None
    try:
        fire, temp, door, flow, presses = (int(p) for p in parts[1:])
    except ValueError:
        return None
    return {"fire": bool(fire), "temp_raw": temp, "door_open": bool(door),
            "flow": bool(flow), "presses": presses, "time": time.time()}


# The serial / UDP protocol, one command per line (see the .ino sketches):
#     B P R         status: Busy (running), Paused, Ready
#     F f / D d     Framing / Disconnected; capital = blink, small = steady
#     U             Unknown (LightBurn can't be read): all lights off
#     K<hex>        relay bits, e.g. K0005 = RELAY_PINS[0] and [2] on
#     A / a         app alarm on / off (buzzer)
#     C             attention beeps (nobody at the PC during a job)
#     X             silence the alarm, and clear a latched fire alarm once the
#                   sensor is clear
# The board switches the lights and relays off if no message arrives for 5 s, so
# a crashed app or unplugged PC can't leave the red light or a relay on.
class LinkBase(threading.Thread):
    """What the app sends every half second (and at once on a change):
    status letter, relay bits, alarm on/off, and one-off commands (chirp)."""

    def __init__(self):
        super().__init__(daemon=True)
        self.enabled = False
        self.code = b"U"
        self.relays = 0                    # bit i = RELAY_PINS[i] on
        self.alarm = False                 # app-raised alarm (overheat, stuck, no exhaust)
        self.pending = b""                 # one-off commands, e.g. b"C\n" = chirp
        self.status = "Off"
        self.connected = False
        self.inputs = None                 # last parse_inputs() result from the board
        self.stop_evt = threading.Event()
        self.wake = threading.Event()      # set to send a new code right away

    def message(self):
        """The full message for one send: status, relays, alarm, plus any queued
        one-off commands (which are then cleared, so they're sent once)."""
        msg = self.code + b"\n" + b"K%04X\n" % self.relays + (b"A\n" if self.alarm else b"a\n")
        msg, self.pending = msg + self.pending, b""
        return msg

    def chirp(self):
        """Queue a short beep and wake the thread so it goes out now."""
        self.pending += b"C\n"
        self.wake.set()


class ArduinoLink(LinkBase):
    """The USB link to an Arduino Uno (laser_stack_light.ino) at 9600 baud.
    Sends the message every 0.5 s or immediately when woken, reads back the
    sensor reports, and reconnects by itself if the cable is unplugged."""
    def __init__(self):
        super().__init__()
        self.port = "Auto-detect"
        self._ser = None
        self._open_port = None
        self._buf = b""

    def _read_inputs(self):
        """Read whatever the board sent (non-blocking, timeout=0) and keep the
        newest complete sensor report. Partial lines stay in _buf until the
        rest arrives; _buf is capped so garbage on the line can't grow it
        forever."""
        try:
            data = self._ser.read(512)
        except Exception:
            return
        if not data:
            return
        self._buf = (self._buf + data)[-2048:]
        *lines, self._buf = self._buf.split(b"\n")
        for line in lines:
            got = parse_inputs(line.decode("ascii", "ignore"))
            if got:
                self.inputs = got

    def _close(self, send_off=False):
        """Close the port. send_off first switches the light and relays off,
        used when the user disables the Arduino or the app quits."""
        if self._ser is not None:
            try:
                if send_off:
                    self._ser.write(b"U\nK0000\na\n")
                    self._ser.flush()
                self._ser.close()
            except Exception:
                pass
        self._ser, self._open_port, self.connected = None, None, False

    def run(self):
        """Keep the link alive: pick the port (chosen, or the one auto-detect
        found), open it if needed, send the message, read sensors, wait 0.5 s
        or until woken. Every failure just updates `status` (shown in
        Settings) and retries after a pause; nothing here ever raises into
        the app."""
        while not self.stop_evt.is_set():
            if not HAVE_SERIAL:
                self.status = "pyserial not installed"
                self.stop_evt.wait(2)
                continue
            if not self.enabled:
                self._close(send_off=True)
                self.status = "Off"
                self.stop_evt.wait(0.5)
                continue
            wanted = self.port if self.port != "Auto-detect" else (
                self._open_port or find_arduino_port())
            if self._ser is not None and wanted != self._open_port:
                self._close(send_off=True)
            if self._ser is None:
                if not wanted:
                    self.status = "No Arduino found"
                    self.stop_evt.wait(2)
                    continue
                try:
                    self.status = f"Connecting to {wanted}..."
                    self._ser = serial.Serial(wanted, 9600, timeout=0)
                    time.sleep(2.0)          # Uno resets when the port opens
                    self._open_port = wanted
                    self.connected = True
                    self.status = f"Connected on {wanted}"
                except Exception:
                    self._close()
                    self.status = f"Can't open {wanted}"
                    self.stop_evt.wait(2)
                    continue
            try:
                self._ser.write(self.message())
                self._read_inputs()
            except Exception:
                self._close()
                self.status = "Arduino unplugged"
            self.wake.wait(0.5)
            self.wake.clear()
        self._close(send_off=True)


WIFI_PORT = 4210        # UDP port of the ESP32 Wi-Fi light (esp32_stack_light.ino)


# UDP rather than TCP: no connection to keep up or time out, and a lost packet
# doesn't matter because the full state is resent every half second.
class NetLight(LinkBase):
    """The same messages as the USB Arduino, sent over Wi-Fi (UDP) to an ESP32.
    The board answers each message with its sensor report, which is how we
    know it's connected."""

    def __init__(self):
        super().__init__()
        self.host = ""
        self._addr = None
        self._resolved_at = 0.0
        self._last_reply = 0.0

    def run(self):
        """Like ArduinoLink.run, over UDP. The host name ("beacon-light.local")
        is looked up again every minute in case the router gave the ESP32 a
        new address. Connected = a reply came in the last 3 seconds."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.3)
        while not self.stop_evt.is_set():
            if not self.enabled or not self.host:
                if self.connected or self.status != "Off":
                    try:
                        if self._addr:
                            sock.sendto(b"U\nK0000\na\n", self._addr)
                    except OSError:
                        pass
                self.connected, self.status, self._addr = False, "Off", None
                self.stop_evt.wait(0.5)
                continue
            now = time.time()
            if self._addr is None or now - self._resolved_at > 60:
                try:
                    self._addr = (socket.gethostbyname(self.host.strip()), WIFI_PORT)
                    self._resolved_at = now
                except OSError:
                    self.connected, self.status = False, f"Can't find {self.host} on the network"
                    self.stop_evt.wait(2)
                    continue
            try:
                sock.sendto(self.message(), self._addr)
                data, _ = sock.recvfrom(256)
                got = parse_inputs(data.decode("ascii", "ignore"))
                if got:
                    self.inputs = got
                self._last_reply = time.time()
            except OSError:
                pass
            self.connected = time.time() - self._last_reply < 3
            self.status = (f"Connected to {self.host} ({self._addr[0]})" if self.connected
                           else f"No answer from {self.host}")
            self.wake.wait(0.5)
            self.wake.clear()
        try:
            if self._addr:
                sock.sendto(b"U\nK0000\na\n", self._addr)
        except OSError:
            pass


# ===========================================================================
# Run-time log (counts Running only, never paused, stopped or test mode)
# ===========================================================================
# jobs.jsonl is "JSON lines": one JSON object per line, appended as each job
# ends, so writing never has to rewrite the whole file and a damaged line only
# loses that one job (it's skipped on load).
JOBS_PATH = os.path.join(CONFIG_DIR, "jobs.jsonl")
OPEN_JOB_PATH = os.path.join(CONFIG_DIR, "open_job.json")
META_PATH = os.path.join(CONFIG_DIR, "job_meta.json")   # later edits: customer / material
CHECKPOINT_EVERY = 30.0
# Labels a job can carry: LightBurn file name, customer and material (picked on
# the main page while it runs, or edited later in Statistics).
JOB_TAGS = ("name", "client", "material")


# Example jobs.jsonl line (times are Unix seconds):
#     {"start": 1730000000.0, "end": 1730000600.0, "run": 540.0,
#      "seg": [[1730000000.0, 1730000300.0], [1730000360.0, 1730000600.0]],
#      "name": "Coasters", "client": "Ann", "material": "Oak 3 mm"}
# Here the job was paused for a minute: 600 s from start to end, 540 s of actual
# laser time ("run"). Statistics only ever count "run".
class RunLog:
    """One line per job in jobs.jsonl: {"start", "end", "run", "seg": [[s, e], ...]}
    where "seg" are the stretches the laser was actually running. The job in
    progress is checkpointed so a crash or power cut loses at most 30 s."""

    # On start: load every finished job, then rescue a job that was still open
    # when the app last stopped (crash, power cut) from open_job.json.
    def __init__(self):
        self.jobs = []
        self.job = None              # job in progress
        self.seg_start = None        # running stretch in progress
        self.session_start = time.time()
        self.session_run = 0.0       # finished stretches since the app opened
        self._last_checkpoint = 0.0
        try:
            with open(JOBS_PATH, encoding="utf-8") as f:
                for line in f:
                    try:
                        j = json.loads(line)
                    except ValueError:
                        continue
                    if j.get("seg"):
                        self.jobs.append(j)
        except OSError:
            pass
        try:                         # left open by a crash: keep what was saved
            with open(OPEN_JOB_PATH, encoding="utf-8") as f:
                j = json.load(f)
            if j.get("seg"):
                self._append(j)
        except (OSError, ValueError):
            pass
        self._remove_open()
        try:
            with open(META_PATH, encoding="utf-8") as f:
                self.meta = json.load(f)
        except (OSError, ValueError):
            self.meta = {}
        self._apply_meta()

    def _apply_meta(self):
        """Copy later edits (job_meta.json, keyed by the job's start time) onto
        the loaded jobs. Edits are kept in their own file so jobs.jsonl is
        only ever appended to, never rewritten."""
        for j in self.jobs:
            j.update(self.meta.get(str(j["start"]), {}))

    def set_tag(self, starts, key, value):
        """Change a tag (customer / material / name) on saved jobs."""
        for start in starts:
            self.meta.setdefault(str(start), {})[key] = value
        self._apply_meta()
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(META_PATH, "w", encoding="utf-8") as f:
                json.dump(self.meta, f)
        except OSError:
            pass

    def observe(self, status, now=None, tags=None):
        """status: what the app shows, or "test" while test mode is on.
        A job starts when the laser starts running and ends at Ready,
        Framing or Disconnected; Paused and "can't see" only stop the clock.
        tags: {"name", "client", "material"} recorded when a job starts."""
        # Called on every status the UI shows (several times a second). A job is
        # a list of running stretches ("segments"): Busy opens a segment,
        # anything else closes it, and Ready/Framing/Disconnected close the
        # whole job.
        now = now or time.time()
        if status == "busy":
            if self.job is None:
                self.job = {"start": round(now, 1), "seg": []}
                self.job.update({k: v for k, v in (tags or {}).items() if v})
            if self.seg_start is None:
                self.seg_start = now
        elif self.seg_start is not None:
            self._close_segment(now)
        if status in ("ready", "framing", "disconnected", "test") and self.job is not None:
            self.close(now)

    def close(self, now=None):
        """End the job in progress (if any) and save it. Jobs with no running
        time at all (e.g. started and stopped while paused) are not saved."""
        now = now or time.time()
        if self.job is None:
            return
        if self.seg_start is not None:
            self._close_segment(now)
        job = self._snapshot(self.job, now)
        self.job = None
        if job["run"] > 0:
            self._append(job)
        self._remove_open()

    def checkpoint(self, now=None):
        """Save the job in progress to open_job.json, at most every
        CHECKPOINT_EVERY seconds. Called from the UI's poll loop; if the app
        dies, the next start finds this file and keeps the job (see
        __init__)."""
        now = now or time.time()
        if self.job is None or now - self._last_checkpoint < CHECKPOINT_EVERY:
            return
        self._last_checkpoint = now
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(OPEN_JOB_PATH, "w", encoding="utf-8") as f:
                json.dump(self.current(now), f)
        except OSError:
            pass

    def current(self, now):
        """The job in progress as it would be saved right now, or None."""
        if self.job is None:
            return None
        job = dict(self.job, seg=list(self.job["seg"]))
        if self.seg_start is not None:
            job["seg"].append([round(self.seg_start, 1), round(now, 1)])
        return self._snapshot(job, now)

    def all_jobs(self, now):
        """Saved jobs plus the one in progress (as it stands now)."""
        cur = self.current(now)
        return self.jobs + ([cur] if cur else [])

    def run_between(self, start, end, now):
        """Seconds running between two timestamps (saved jobs + the one in progress)."""
        return sum(max(0.0, min(e, end) - max(s, start))
                   for j in self.all_jobs(now) for s, e in j["seg"])

    def jobs_between(self, start, end, now):
        """Jobs that started between two timestamps."""
        return [j for j in self.all_jobs(now) if start <= j["start"] < end]

    def run_on(self, day, now):
        """Seconds the laser ran on one calendar day, over every launch of the
        app: saved jobs (cached, they don't change) plus the job in progress."""
        start = datetime.combine(day, datetime.min.time()).timestamp()
        end = datetime.combine(day + timedelta(days=1), datetime.min.time()).timestamp()

        def overlap(segs):
            return sum(max(0.0, min(e, end) - max(s, start)) for s, e in segs)
        key = (day, len(self.jobs))
        if getattr(self, "_day_cache", (None,))[0] != key:
            self._day_cache = (key, sum(overlap(j["seg"]) for j in self.jobs))
        cur = self.current(now)
        return self._day_cache[1] + (overlap(cur["seg"]) if cur else 0.0)

    def session_run_time(self, now):
        """Running seconds since the app was opened, including the stretch in
        progress (a job already running at launch only counts from the
        launch)."""
        open_part = (now - max(self.seg_start, self.session_start)
                     if self.seg_start is not None else 0.0)
        return self.session_run + open_part

    def _close_segment(self, now):
        """End the running stretch in progress and add it to the job."""
        self.job["seg"].append([round(self.seg_start, 1), round(now, 1)])
        self.session_run += now - max(self.seg_start, self.session_start)
        self.seg_start = None

    def total_run(self, now):
        """All-time running seconds (for maintenance intervals)."""
        if getattr(self, "_total_cache", (None,))[0] != len(self.jobs):
            self._total_cache = (len(self.jobs), sum(j["run"] for j in self.jobs))
        cur = self.current(now)
        return self._total_cache[1] + (cur["run"] if cur else 0.0)

    def typical_run(self, name):
        """Median run time of earlier jobs with this name (None if fewer than 3)."""
        runs = sorted(j["run"] for j in self.jobs[-300:] if name and j.get("name") == name)
        return runs[len(runs) // 2] if len(runs) >= 3 else None

    @staticmethod
    def _snapshot(job, now):
        """The job dict as saved: start, end (now), total run time from the
        segments, the segments, and any tags that are set."""
        snap = {"start": job["start"], "end": round(now, 1),
                "run": round(sum(e - s for s, e in job["seg"]), 1), "seg": job["seg"]}
        snap.update({k: job[k] for k in JOB_TAGS if job.get(k)})
        return snap

    def _append(self, job):
        """Keep a finished job in memory and add it as one line to jobs.jsonl."""
        self.jobs.append(job)
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(JOBS_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(job) + "\n")
        except OSError:
            pass

    def _remove_open(self):
        """Delete open_job.json (no job is open any more)."""
        try:
            os.remove(OPEN_JOB_PATH)
        except OSError:
            pass


def hourly_buckets(segments):
    """{local datetime at the start of each hour: seconds running in it}"""
    out = {}
    for s, e in segments:
        while s < e:
            hour = datetime.fromtimestamp(s).replace(minute=0, second=0, microsecond=0)
            part = min(e, (hour + timedelta(hours=1)).timestamp()) - s
            if part <= 0:               # clock-change edge case
                break
            out[hour] = out.get(hour, 0.0) + part
            s += part
    return out


# ===========================================================================
# Look & feel
# ===========================================================================
# Colours. A pair is (light theme, dark theme): CustomTkinter widgets accept
# pairs directly and switch with the theme; plain Tk widgets and Pillow need a
# single colour, which pick() chooses for the current theme.
BG = ("#F4F5F7", "#0E1014")
CARD = ("#FFFFFF", "#171A21")
CARD_BORDER = ("#E5E7EB", "#232733")
TEXT = ("#0F172A", "#F1F5F9")
MUTED = ("#6B7280", "#8B93A3")
ACCENT = ("#2563EB", "#3B82F6")

# Status colours, the same in both themes.
RED = "#EF4444"
GREEN = "#22C55E"
AMBER = "#F59E0B"
SLATE = "#64748B"
DIM = "#475569"

# status: (color, title, blinks-when-blink-enabled)
LOOK = {
    "busy":         (RED,   "Running",      False),
    "paused":       (AMBER, "Paused",       False),
    "framing":      (GREEN, "Framing",      True),
    "ready":        (GREEN, "Ready",        False),
    "disconnected": (SLATE, "Disconnected", True),
    None:           (DIM,   "Not found",    False),
}


def look(state):
    """(colour, title, blinks) for a status."""
    return LOOK.get(state, LOOK[None])


# See-through colour for the corners of the pill and its menu (light, dark).
# Edge pixels are smoothed against it, so it must be close to the theme's
# colours or a dark rim shows round a light pill. Never used inside them.
KEY_COLOR = ("#F4F5F7", "#010203")
PILL_PAD = 10           # compact pill: gap around the dot, between dot and text, and at the ends
PILL_TEXT_LIFT = 5      # compact pill: raises the text this much (x2) to centre it optically

FONT = "Segoe UI" if IS_WIN else "Helvetica Neue"
MONO_FONT = "Consolas" if IS_WIN else "Menlo"
# Right mouse button: Tk on macOS calls it button 2, and Control-click counts too
RIGHT_CLICK = ("<Button-3>",) if IS_WIN else ("<Button-2>", "<Control-Button-1>")


def pick(pair):
    """The colour of a (light, dark) pair for the current theme."""
    return pair[1] if ctk.get_appearance_mode() == "Dark" else pair[0]


# Light widgets for long pages. CustomTkinter draws every frame and label
# (even an invisible one) with its own canvas, so each costs two or three
# windows; with every Settings category open that was ~650 windows, which took
# 2.5 s to show and painted slowly when scrolled. PlainFrame and PlainLabel are
# one plain Tk window each, used for layout and text; buttons, switches and
# menus stay CustomTkinter. Both follow the Light / Dark switch and display
# scaling, and answer cget("bg") with their (light, dark) pair, so a
# CustomTkinter widget placed inside one still picks the right corner colour.
def as_pair(color):
    """A colour as a (light, dark) pair."""
    return tuple(color) if isinstance(color, (tuple, list)) else (color, color)


def background_of(widget):
    """The (light, dark) colour behind anything placed in `widget`: the same
    search CustomTkinter makes for its "transparent" colour."""
    while widget is not None:
        if isinstance(widget, (PlainFrame, PlainLabel)):
            return widget.bg_pair
        if isinstance(widget, (ctk.CTkScrollableFrame, ctk.CTkBaseClass, ctk.CTk,
                               ctk.CTkToplevel)):
            fg = widget.cget("fg_color")
            if fg not in (None, "transparent"):
                return as_pair(fg)
            # a scrollable frame sits in a canvas in a frame: look past both
            widget = (widget.master.master.master if isinstance(widget, ctk.CTkScrollableFrame)
                      else widget.master)
            continue
        return as_pair(widget.cget("bg"))
    return BG


class PlainFrame(tk.Frame):
    """A see-through layout frame: one window, coloured like its parent."""

    def __init__(self, master, height=None, **kw):
        self.bg_pair = background_of(master)
        super().__init__(master, bd=0, highlightthickness=0, bg=pick(self.bg_pair), **kw)
        if height:                      # a spacer: scale it like CustomTkinter would
            self.configure(height=round(height * ctk.ScalingTracker.get_widget_scaling(self)))
        ctk.AppearanceModeTracker.add(self._appearance, self)

    def _appearance(self, mode):
        """Light / Dark switched: recolour."""
        self.configure(bg=self.bg_pair[1] if mode == "Dark" else self.bg_pair[0])

    def cget(self, key):
        """The background as a (light, dark) pair, for CustomTkinter widgets inside."""
        if key in ("bg", "background"):
            return self.bg_pair
        return super().cget(key)

    def destroy(self):
        ctk.AppearanceModeTracker.remove(self._appearance)
        super().destroy()


class PlainLabel(tk.Label):
    """Text in one window, taking the same font, text_color, wraplength,
    anchor and justify arguments as CTkLabel (configure too), so either can
    be used."""

    def __init__(self, master, text="", font=None, text_color=TEXT, wraplength=0,
                 anchor="w", justify="left", **kw):
        self.bg_pair = background_of(master)
        self._color, self._font, self._wrap = as_pair(text_color), font, wraplength
        super().__init__(master, text=text, anchor=anchor, justify=justify, bd=0, padx=0,
                         pady=0, highlightthickness=0, bg=pick(self.bg_pair),
                         fg=pick(self._color), **kw)
        self._scale(ctk.ScalingTracker.get_widget_scaling(self))
        ctk.AppearanceModeTracker.add(self._appearance, self)
        ctk.ScalingTracker.add_widget(self._rescale, self)

    def _scale(self, scaling):
        """Font size and wrap width in pixels for this display's scaling."""
        self._scaling = scaling
        font = self._font or ctk.CTkFont(family=FONT, size=12)
        super().configure(font=font.create_scaled_tuple(scaling),
                          wraplength=round(self._wrap * scaling))

    def _rescale(self, widget_scaling, window_scaling):
        """The window moved to a screen with different scaling."""
        self._scale(widget_scaling)

    def _appearance(self, mode):
        """Light / Dark switched: recolour."""
        i = 1 if mode == "Dark" else 0
        super().configure(bg=self.bg_pair[i], fg=self._color[i])

    def configure(self, **kw):
        """Like CTkLabel.configure: text_color, font and wraplength included."""
        if "text_color" in kw:
            self._color = as_pair(kw.pop("text_color"))
            kw["fg"] = pick(self._color)
        if "font" in kw or "wraplength" in kw:
            self._font = kw.pop("font", self._font)
            self._wrap = kw.pop("wraplength", self._wrap)
            self._scale(self._scaling)
        if kw:
            super().configure(**kw)

    def cget(self, key):
        if key in ("bg", "background"):
            return self.bg_pair
        if key == "text_color":
            return self._color
        return super().cget(key)

    def destroy(self):
        ctk.AppearanceModeTracker.remove(self._appearance)
        ctk.ScalingTracker.remove_widget(self._rescale, self)
        super().destroy()


# ===========================================================================
# Job-finished chime: Windows Media Player's engine (MP3, WAV, WMA, any file
# name; part of Windows, driven through pywin32). The older MCI player is the
# fallback; on some PCs it plays WAV but fails on MP3 ("error 277").
# ===========================================================================
# A chosen sound is copied into SOUNDS_DIR, so the chime keeps working if the
# original file is moved or deleted. WMP_PLAYING is Media Player's playState
# value for "playing".
SOUNDS_DIR = os.path.join(CONFIG_DIR, "sounds")
SOUND_TYPES = [("Sound files", "*.mp3 *.wav *.wma *.m4a"), ("All files", "*.*")]
WMP_PLAYING = 3


def _mci(command):
    """Send one command string to Windows' old MCI multimedia player. 0 = OK."""
    return ctypes.windll.winmm.mciSendStringW(command, None, 0, 0)


def _mci_play(path):
    """Play a file with MCI under the alias "lschime" (closing the previous one
    first). True if it started."""
    _mci("close lschime")
    return (_mci(f'open "{path}" type mpegvideo alias lschime') == 0
            and _mci("play lschime") == 0)


class ChimePlayer:
    """Plays the job-finished sound without blocking the UI. The Windows Media
    Player COM object is created on first use (it takes a moment); if it
    can't be created, the MCI player is used instead."""
    def __init__(self):
        self._wmp = None            # created on first use; False if unavailable
        self._errors_before = 0     # Media Player's error count when play started

    def _player(self):
        """The Media Player COM object, or None if this PC doesn't have it."""
        if self._wmp is None:
            try:
                import pythoncom
                import win32com.client
                pythoncom.CoInitialize()
                self._wmp = win32com.client.Dispatch("WMPlayer.OCX")
                self._wmp.settings.autoStart = False
                self._wmp.settings.volume = 100
            except Exception:
                self._wmp = False
        return self._wmp or None

    def play(self, path):
        """Starts playing without waiting. False if it couldn't even start."""
        self.stop()
        if not path or not os.path.exists(path):
            return False
        wmp = self._player()
        if wmp:
            try:
                self._errors_before = wmp.error.errorCount
                wmp.URL = path
                wmp.controls.play()
                return True
            except Exception:
                pass
        return _mci_play(path)

    def check(self, path, timeout=3.0):
        """Plays the file and waits until it really starts (or fails): used to
        test a newly chosen sound. Doubles as its preview."""
        if not self.play(path):
            return False
        wmp = self._player()
        if not wmp:
            return True                 # MCI opened it, that's as good as it gets
        import pythoncom
        end = time.time() + timeout
        while time.time() < end:
            pythoncom.PumpWaitingMessages()
            try:
                if wmp.error.errorCount > self._errors_before:
                    break
                if wmp.playState == WMP_PLAYING:
                    return True
            except Exception:
                break
            time.sleep(0.05)
        self.stop()
        return _mci_play(path)          # last try with the older player

    def stop(self):
        """Stop whatever is playing, in both players."""
        _mci("close lschime")
        if self._wmp:
            try:
                self._wmp.controls.stop()
            except Exception:
                pass


class MacChimePlayer:
    """The same player on macOS, through the built-in `afplay` (MP3, WAV, M4A,
    AIFF; not WMA). Each sound is its own afplay process, so stop() just ends
    it."""
    DEFAULT = "/System/Library/Sounds/Glass.aiff"

    def __init__(self):
        self._proc = None

    def play(self, path):
        """Starts playing without waiting. False if it couldn't even start."""
        self.stop()
        if not path or not os.path.exists(path):
            return False
        try:
            self._proc = subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
            return True
        except OSError:
            return False

    def check(self, path, timeout=3.0):
        """Plays the file; False if afplay gives up on it within half a second
        (it exits at once with an error for files it can't decode)."""
        if not self.play(path):
            return False
        try:
            return self._proc.wait(timeout=min(timeout, 0.5)) == 0
        except subprocess.TimeoutExpired:
            return True                 # still playing: it works

    def stop(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        self._proc = None


# One shared player for the whole app, plus short helpers used by the UI.
CHIME = ChimePlayer() if IS_WIN else MacChimePlayer()


def play_sound(path):
    return CHIME.play(path)


def stop_sound():
    CHIME.stop()


def play_chime(path):
    """The chosen sound, or the system chime if there isn't one (or it won't play)."""
    if play_sound(path):
        return
    if winsound:
        try:
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass
    elif IS_MAC:
        play_sound(MacChimePlayer.DEFAULT)


def set_window_key(win):
    """Make the current theme's KEY_COLOR see-through in a borderless window.
    macOS has no colour key; there the window itself is made transparent and
    whatever is drawn in "systemTransparent" (see RoundPanel) shows through."""
    if IS_MAC:
        win.configure(bg="systemTransparent")
        try:
            win.attributes("-transparent", True)
        except tk.TclError:
            pass
        return
    key = pick(KEY_COLOR)
    win.configure(bg=key)
    try:
        win.attributes("-transparentcolor", key)
    except tk.TclError:
        pass


def rounded_path(x0, y0, x1, y1, r, step):
    """Points every `step` px round a rounded rectangle, clockwise from the top."""
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    pts = []

    def line(ax, ay, bx, by):
        """Points along a straight edge, about `step` apart."""
        n = max(1, int(math.hypot(bx - ax, by - ay) / step))
        pts.extend((ax + (bx - ax) * i / n, ay + (by - ay) * i / n) for i in range(n))

    def arc(cx, cy, a0):
        """Points along a quarter circle round (cx, cy), starting at angle a0."""
        n = max(2, int(math.pi / 2 * r / step))
        pts.extend((cx + r * math.cos(a0 + math.pi / 2 * i / n),
                    cy + r * math.sin(a0 + math.pi / 2 * i / n)) for i in range(n))
    line(x0 + r, y0, x1 - r, y0)
    arc(x1 - r, y0 + r, -math.pi / 2)
    line(x1, y0 + r, x1, y1 - r)
    arc(x1 - r, y1 - r, 0)
    line(x1 - r, y1, x0 + r, y1)
    arc(x0 + r, y1 - r, math.pi / 2)
    line(x0, y1 - r, x0, y0 + r)
    arc(x0 + r, y0 + r, math.pi)
    pts.append(pts[0])
    return pts


def dash_runs(pts, on, off):
    """Cuts a path into the "on" stretches of an on/off dash pattern."""
    runs, cur, pos = [], [pts[0]], 0.0
    for a, b in zip(pts, pts[1:]):
        pos += math.hypot(b[0] - a[0], b[1] - a[1])
        if pos % (on + off) < on:
            cur.append(b)
        elif len(cur) > 1:
            runs.append(cur)
            cur = [b]
        else:
            cur = [b]
    if len(cur) > 1:
        runs.append(cur)
    return runs


def panel_image(w, h, r, fill, outline, width, dash, behind, hard_edge=False):
    """A rounded panel as one smoothed image: drawn 2-3x larger, then scaled down
    (2x for big panels keeps it quick; it still looks smooth).

    hard_edge: for see-through windows. Windows can't blend an edge with what's
    behind the window, so outer edge pixels are either fully panel (in its own
    edge colour) or fully `behind`; never a half-mix that shows as a dark rim."""
    # Supersampling: draw at S times the size and shrink, so curves and lines
    # get smooth (anti-aliased) edges that Pillow's drawing doesn't do by
    # itself.
    S = 2 if w * h > 60000 else 3
    W, H, R, lw = w * S, h * S, r * S, max(1, round(width * S))
    edge = (fill if dash else outline) if hard_edge else behind
    img = Image.new("RGB", (W, H), edge)
    d = ImageDraw.Draw(img)
    if not dash:
        d.rounded_rectangle((0, 0, W - 1, H - 1), radius=R, fill=outline)
        d.rounded_rectangle((lw, lw, W - 1 - lw, H - 1 - lw), radius=max(0, R - lw), fill=fill)
    else:
        d.rounded_rectangle((0, 0, W - 1, H - 1), radius=R, fill=fill)
        half = lw / 2
        path = rounded_path(half, half, W - 1 - half, H - 1 - half, R - half, S)
        for run in dash_runs(path, dash[0] * S, dash[1] * S):
            d.line(run, fill=outline, width=lw, joint="curve")
    img = img.resize((w, h), Image.BOX)     # plain averaging: no dark overshoot at edges
    if hard_edge:
        mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, W - 1, H - 1), radius=R, fill=255)
        mask = mask.resize((w, h), Image.BOX).point(lambda v: 255 if v >= 128 else 0)
        img = Image.composite(img, Image.new("RGB", (w, h), behind), mask)
    return img


class RoundPanel(tk.Canvas):
    """A rounded card whose outline can be any colour, solid or dashed
    (CTkFrame only draws solid borders). Child widgets pack inside as usual;
    give them bg_color=CARD so they blend into the fill."""

    _cache = {}                     # drawing args -> PhotoImage, oldest first

    def __init__(self, master, radius, behind, scale, hard_edge=False):
        super().__init__(master, highlightthickness=0, bd=0, width=1, height=1)
        self.radius = radius        # logical px, or None for fully round ends
        self.behind = behind        # colour outside the rounded corners
        self.hard_edge = hard_edge  # True in see-through windows (see panel_image)
        self.scale = scale
        self.style = (CARD_BORDER, 1, None)
        self._img = self._drawn = self._pending = None
        self.bind("<Configure>", lambda e: self._schedule())

    # The main window's status card uses this to show the status colour as its
    # border (dashed while blinking statuses like Framing are on).
    def set_style(self, color, width, dash=None):
        """width in logical px; dash = (on, off) in logical px, or None for solid."""
        self.style = (color, width, dash)
        self.redraw()

    def _schedule(self):
        # Small panels (the pill) redraw at once so the outline never lags a
        # resize; big ones coalesce bursts of resize events while dragging
        if self._pending:
            self.after_cancel(self._pending)
            self._pending = None
        if self.winfo_width() * self.winfo_height() <= 60000:
            self.redraw()
        else:
            self._pending = self.after(15, self.redraw)

    def redraw(self):
        """Draw the panel at its current size and style. The picture is skipped
        if nothing changed, reused from the cache if this exact look was
        drawn recently, and otherwise rendered by panel_image(). It's kept at
        the bottom of the canvas (tag_lower) so child widgets stay on top."""
        self._pending = None
        w, h = self.winfo_width(), self.winfo_height()
        if w < 8 or h < 8:
            return
        color, width, dash = self.style
        s = self.scale
        r = h / 2 if self.radius is None else self.radius * s
        args = (w, h, r, pick(CARD), pick(color) if isinstance(color, tuple) else color,
                width * s, (dash[0] * s, dash[1] * s) if dash else None, pick(self.behind),
                self.hard_edge)
        if args == self._drawn:
            return
        self._drawn = args
        if IS_MAC and self.hard_edge:
            self._draw_shape(*args[:7])
            return
        self.configure(bg=args[-2])
        # Recently drawn looks are kept, so flipping Running <-> Ready is instant
        key = args + (id(self.tk),)         # images belong to one Tk instance
        img = (RoundPanel._cache.pop(key, None)
               or ImageTk.PhotoImage(panel_image(*args), master=self))
        RoundPanel._cache[key] = img
        while len(RoundPanel._cache) > 16:
            RoundPanel._cache.pop(next(iter(RoundPanel._cache)))
        self._img = img
        self.delete("panel")
        self.create_image(0, 0, anchor="nw", image=self._img, tags="panel")
        self.tag_lower("panel")

    def _draw_shape(self, w, h, r, fill, outline, width, dash):
        """macOS, see-through window: a see-through canvas there draws shapes
        but not pictures, so the panel is a rounded polygon instead (macOS
        smooths its edges itself)."""
        self.configure(bg="systemTransparent")
        half = width / 2
        pts = rounded_path(half, half, w - half, h - half, max(0.0, r - half), 2)
        self.delete("panel")
        self.create_polygon([c for p in pts for c in p], fill=fill, outline=outline,
                            width=width, dash=tuple(round(d) for d in dash) if dash else "",
                            tags="panel")
        self.tag_lower("panel")


def blend(c1, c2, t):
    """Mix two "#rrggbb" colours: t=0 gives c1, t=1 gives c2. Used for fades
    (e.g. the blinking dot)."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(a[i] + (b[i] - a[i]) * t):02x}" for i in range(3))


# ===========================================================================
# Notifications: Windows pop-ups and a Telegram bot
# ===========================================================================
# Events the user can switch on in Settings › Notifications, as (key, label).
# The key is used in settings ("notify_<key>") and when sending.
NOTIFY_EVENTS = [("finished", "A job finishes"), ("paused", "A job is paused"),
                 ("lost", "The laser disconnects mid-job"), ("started", "A job starts"),
                 ("stuck", "A job runs too long or stays paused"),
                 ("free", "The laser is free again"),
                 ("unreadable", "LightBurn can't be read"),
                 ("maintenance", "Maintenance is due")]
# Get through quiet hours and mute, and go by SMS when "alerts only" is set
CRITICAL_EVENTS = {"lost", "fire", "temp", "flow", "door", "unattended", "stuck"}
# Never held back by the flood guard, however often they fire
SAFETY_EVENTS = {"fire", "temp", "flow", "door"}


def cost_per_hour(c):
    """What one hour of laser time costs from the Statistics › Costs inputs:
    electricity + machine wear + maintenance + labour + other."""
    wear = c["machine_price"] / c["lifetime_h"] if c["lifetime_h"] > 0 else 0.0
    return c["power_kw"] * c["kwh_price"] + wear + c["maint_h"] + c["labor_h"] + c["other_h"]


def telegram_call(token, method, params=None, timeout=10):
    """Calls the Telegram Bot API (HTTPS). Returns its "result", or raises
    RuntimeError with a readable reason."""
    import urllib.error
    import urllib.parse
    import urllib.request
    # Plain urllib (no extra package): the Bot API takes form-encoded POSTs.
    # Telegram explains errors in JSON even on HTTP errors, so that's read too
    # and turned into a message a user can act on.
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode("utf-8")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=timeout) as r:
            reply = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            reply = json.loads(e.read().decode("utf-8"))
        except ValueError:
            raise RuntimeError(f"Telegram error {e.code}")
    except (urllib.error.URLError, OSError, ValueError):
        raise RuntimeError("Can't reach Telegram. Check the internet connection.")
    if not reply.get("ok"):
        reason = reply.get("description", "Telegram refused the request")
        if reply.get("error_code") == 401:
            reason = "That bot token isn't valid. Copy it again from @BotFather."
        elif "chat not found" in reason.lower():
            reason = "Chat not found. Send your bot a message, then press Find."
        raise RuntimeError(reason)
    return reply.get("result")


# Usage reports (hourly / daily / weekly) sent to Telegram or as pop-ups. The
# App's report timer decides when; build_report() writes the text.
REPORT_SPAN = {"hourly": 3600, "daily": 86400, "weekly": 7 * 86400}
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _change_line(cur, prev, what):
    """"vs the week before: ▲ 12%" comparing this period with the one before, or
    "" when there's nothing to say."""
    if prev <= 0:
        return f"vs {what}: no laser time then" if cur > 0 else ""
    pct = (cur - prev) / prev * 100
    arrow = "▲" if pct >= 0 else "▼"
    return f"vs {what}: {arrow} {abs(pct):.0f}%"


def build_report(kind, end, runlog, costs, now=None):
    """(title, body, seconds running) for the period of `kind` ending at `end`."""
    now = now or time.time()
    span = REPORT_SPAN[kind]
    end_ts = end.timestamp()
    start_ts = end_ts - span
    start = datetime.fromtimestamp(start_ts)
    run = runlog.run_between(start_ts, end_ts, now)
    prev = runlog.run_between(start_ts - span, start_ts, now)
    jobs = runlog.jobs_between(start_ts, end_ts, now)
    # Example daily report body:
    #     Laser on: 3h 12m
    #     Jobs: 14  ·  longest 41m 3s
    #     Cost: $4.80
    #     vs the 24 h before: ▲ 18%
    lines = []
    if kind == "hourly":
        title = f"⏱ Hourly report · {start:%H:%M}–{end:%H:%M}"
        lines.append(f"Laser on: {fmt_human(run)} ({run / 36:.0f}% of the hour)")
    elif kind == "daily":
        title = f"\U0001F4C5 Daily report · {start:%a %H:%M} → {end:%a %H:%M}"
        lines.append(f"Laser on: {fmt_human(run)}")
    else:
        title = f"\U0001F4CA Weekly report · {start:%d %b} – {end:%d %b}"
        lines.append(f"Laser on: {fmt_human(run)}  (avg {fmt_human(run / 7)} a day)")
    job_line = f"Jobs: {len(jobs)}"
    if jobs and kind != "hourly":
        job_line += f"  ·  longest {fmt_human(max(j['run'] for j in jobs))}"
    lines.append(job_line)
    rate = cost_per_hour(costs)
    if rate > 0:
        lines.append(f"Cost: {costs['currency']}{run * rate / 3600:,.2f}")
    change = _change_line(run, prev, {"hourly": "the hour before", "daily": "the 24 h before",
                                      "weekly": "the week before"}[kind])
    if change:
        lines.append(change)
    if kind == "weekly":            # day-by-day bars for the 7 calendar days up to `end`
        days = [end.date() - timedelta(days=6 - i) for i in range(7)]
        per_day = [runlog.run_on(d, now) for d in days]
        top = max(per_day) or 1
        lines.append("")
        for d, v in zip(days, per_day):
            bar = "█" * round(v / top * 10) if v > 0 else "·"
            lines.append(f"{d:%a} {bar} {fmt_human(v) if v > 0 else ''}".rstrip())
    return title, "\n".join(lines), run


def bars_icon(color):
    """Little bar-chart glyph for the Statistics button."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for x, h in ((8, 26), (26, 46), (44, 34)):
        d.rounded_rectangle((x, 58 - h, x + 12, 58), radius=3, fill=color)
    return img


def fmt_duration(sec):
    """Clock style: "4:05" -> "04:05", or "1:02:03" with hours."""
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def fmt_human(sec):
    """Short words: "45s", "3m 20s", "2h 5m"."""
    sec = int(sec)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


# ===========================================================================
# The app
# ===========================================================================
# The whole user interface and the glue between the parts. One App object is
# created by main(); it owns:
# - the Tk window (self.root) with its pages: Home (live status), Statistics,
#   Settings, Help, the setup assistant ("wizard"), Diagnostics. Most pages are
#   built the first time they're shown (see ensure_page / LAZY_PAGES), so the
#   window opens quickly.
# - the compact "pill" window, the tray icon and the global hotkey
# - the background workers: the detector thread (Detector / ApiDetector /
#   HybridDetector), the Arduino and Wi-Fi light links, the phone dashboard
#   server, notification sending
# - the job bookkeeping (RunLog) and statistics
#
# Threads never touch Tk widgets: they put messages on queues, and poll() on the
# UI thread empties those queues every few milliseconds (Tk isn't thread-safe).
# Settings are in self.s (a dict) and saved with save().
class App:
    def __init__(self, ocr):
        # Window setup. The window starts fully transparent (alpha 0) and is
        # revealed once everything is drawn, so the user never sees it
        # half-built.
        self.s = load_settings()
        ctk.set_appearance_mode(self.s["appearance"].lower())
        ctk.set_default_color_theme("blue")

        self.root = ctk.CTk(fg_color=BG)
        self.root.report_callback_exception = lambda *exc: logging.error(
            "Error in the window", exc_info=exc)
        self.root.attributes("-alpha", 0.0)     # revealed once drawn (see reveal_window)
        self.root.title(WINDOW_TITLE)
        if IS_WIN and os.path.exists(ICON_PATH):    # a Mac app's icon comes from its bundle
            try:
                self.root.iconbitmap(ICON_PATH)
            except tk.TclError:
                pass
        if IS_MAC:
            # The Dock icon clicked (with every window hidden), Beacon › Settings…
            # (⌘,) and Beacon › Quit (⌘Q): Tk calls these by name
            self.root.createcommand("::tk::mac::ReopenApplication",
                                    lambda *a: self.bring_to_front_full())
            self.root.createcommand("::tk::mac::ShowPreferences",
                                    lambda *a: (self.bring_to_front_full(),
                                                self.show_page("settings")))
            self.root.createcommand("::tk::mac::Quit", lambda *a: self.quit())
        self.root.geometry("380x560")
        self.root.minsize(340, 500)
        self.root.attributes("-topmost", self.s["topmost"])
        try:
            self.scale = ctk.ScalingTracker.get_window_scaling(self.root)
        except Exception:
            self.scale = 1.0

        # runtime state
        # self.q receives the detector's messages (see Detector.run); poll()
        # reads them. The rest is what the UI remembers between those messages.
        self.q = queue.Queue()
        self.state = None
        self.last_msg = {"note": "Starting...", "lines": [], "mode": ""}
        self.job_start = None
        self.last_job = None
        self.last_check = None
        self._orb_key = None
        self._pill_key = None
        self.page = "status"
        self.test_mode = False      # statuses come from the Test buttons, not LightBurn
        self.demo_after = None
        self.demo_end = None
        # Per relay (index = position in RELAY_PINS): a manual override from
        # pressing its button, whether the automatic rule wants it on, and the
        # timing needed for "keep on after" and "switch on after a delay".
        n = len(RELAY_PINS)
        self.relay_override = [None] * n    # manual press, kept until the auto rule changes
        self.relay_auto = [False] * n
        self.relay_last = [0.0] * n         # last time the auto rule wanted it on
        self.relay_on = [False] * n
        self.relay_buttons = {}
        self._relay_keys = {}
        self.relay_cond_since = [None] * n  # when the auto rule started wanting it on
        self.runlog = RunLog()
        self._min_h = 500
        self._small_size = self._stats_size = None
        # monitoring / alerts
        self.progress = None                # job % from LightBurn's progress bar
        self.file_name = ""                 # open LightBurn file
        self.job_alerts = set()             # alerts already sent for the current job
        self.idle_since = None              # when the laser last stopped after a job
        self.free_sent = True
        self.unreadable_since = None
        self.unreadable_sent = False
        self.pause_since = None
        # safety inputs
        self.fire_active = False
        self.temp_alerted = False
        self.door_alerted = False
        self.flow_bad_since = None
        self.flow_alerted = False
        self.last_presses = None
        self.app_alarm = False
        self.maint_alerted = set()
        # Flood guard: if the same alert fires more than 3 times in 60 s, one
        # warning is sent and that alert is paused for 10 minutes (never for
        # SAFETY_EVENTS).
        self._alerts_sent, self._alerts_paused = {}, {}     # notify() flood guard
        self._end_pending = None        # a job that stopped but isn't finished yet
        self._resume_pending = None     # Paused -> Running not yet confirmed as a resume
        # Python's automatic garbage collector can run on whichever thread
        # happens to allocate. If it runs on a worker thread while freeing Tk
        # objects, Tk crashes (Tk objects must only be touched from the UI
        # thread). So automatic collection is off and poll() collects on the UI
        # thread at set times.
        gc.disable()                    # collected on the UI thread in poll()
        self._gc_at = self._gc_full_at = time.time()

        # Start the workers. Each runs in its own thread and is told what to do
        # by setting attributes (enabled, port, host...) from the UI thread;
        # they only read those, so no locks are needed.
        self.ocr = ocr
        self.ocr_name = ocr.name if ocr else "unavailable"
        if not isinstance(self.s.get("lb_api_trusted"), dict):
            self.s["lb_api_trusted"] = {}
        if ocr is None:                 # screen reading can't start: the API is all we have
            self.s["detect_mode"] = "api"
        self.detector = self.make_detector()
        self.arduino = ArduinoLink()
        self.arduino.enabled = self.s["arduino"]
        self.arduino.port = self.s["port"]
        self.netlight = NetLight()
        self.netlight.enabled = self.s["wifi_on"]
        self.netlight.host = self.s["wifi_host"]
        self.plugs = smartplugs.PlugWorker()
        self.dash = webdash.Dashboard(APP_NAME, self.dash_state, self.dash_command)
        self.checklog = DetectionLog(os.path.join(CONFIG_DIR, "detection-check"))
        self.checklog.enabled = bool(self.s["check_log"])
        self._dash_snapshot = {}
        # The tray icon and the global hotkey use a hidden Win32 window whose
        # messages Tk's own loop dispatches. Its window procedure runs in the
        # middle of that dispatch, where touching Tk crashes Python, so its
        # callbacks are handed over with post=self._on_ui and run on the next
        # poll() (see beacon/winsys.py).
        self.tray = osys.TrayHost(APP_NAME, ICON_PATH,
                                    on_menu=lambda x, y: self.show_tray_menu(x, y),
                                    on_open=self.bring_to_front_full, on_hotkey=self.hotkey_pressed,
                                    post=self._on_ui)

        # Only the Status page is built now; the others (5 s of building in all)
        # are built the first time they're opened, see ensure_page()
        self.subpages = {}
        self.test_buttons = {}
        self.test_var = ctk.BooleanVar(value=False)
        self.build_status_page()
        self.build_pill()
        self.update_test_ui()
        self.rebuild_relay_panel()
        self.show_page("status")

        self.detector.start()
        self.arduino.start()
        self.netlight.start()
        self.plugs.start()
        threading.Thread(target=self.telegram_poller, daemon=True).start()
        self.apply_windows_settings()
        if self.s["dash_on"]:
            self.start_dashboard()
        # Windows sends "close" to every window at shutdown/log-off (and so do
        # task tools): any of them quits the whole app properly
        for win in (self.root, self.pill, self.pill_menu):
            win.protocol("WM_DELETE_WINDOW", self.quit)
        # Two timers keep the UI alive: poll() (fast: detector messages, links,
        # alerts) and tick() (once a second: clocks, timers). Both reschedule
        # themselves with root.after().
        self.poll()
        self.tick()
        self._mark_reports_sent()           # reports switched on but never sent: start fresh
        self.root.after(5000, self.check_reports)
        self.root.after(15000, lambda: self.check_updates(auto=True))
        # Settings is the page opened most: build it just after the window shows,
        # and lay it out under the Home page a moment later (park_settings), so
        # it opens at once
        self.root.after(1200, self._prebuild_settings)
        self.root.bind("<Configure>", self._root_resized, add="+")
        # Always start in the full window (the pill keeps its remembered spot);
        # started with Windows: go straight to the small pill
        self.s["compact"] = False
        if not self.s["setup_done"]:
            self.show_page("wizard")
        self.root.update()              # paint while still invisible (see reveal_window)
        if "--startup" in sys.argv and self.s["setup_done"]:
            self.root.after(300, self.enter_compact)
        else:
            self.root.after(40, self._finish_reveal)

    # -------------------------------------------------------------- layout
    def font(self, size, weight="normal"):
        """The app's font (Segoe UI) at a size in points."""
        return ctk.CTkFont(family=FONT, size=size, weight=weight)

    def icon_button(self, master, text, command):
        """A small square, borderless button showing one symbol (the ⚙ ? ↘ in
        the header)."""
        return ctk.CTkButton(master, text=text, width=34, height=34, corner_radius=10,
                             fg_color="transparent", hover_color=CARD_BORDER,
                             text_color=MUTED, font=self.font(16), command=command)

    def build_status_page(self):
        """Build the Home page: header with the page buttons, the banners (test
        mode, can't read LightBurn, alarm; only packed when needed), the
        status card with the big coloured orb and status text, the relay
        buttons, and the footer (last check, Arduino connection)."""
        p = self.status_page = ctk.CTkFrame(self.root, fg_color="transparent")

        head = ctk.CTkFrame(p, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(16, 0))
        ctk.CTkLabel(head, text=HEADER_TEXT, font=self.font(15, "bold"),
                     text_color=TEXT).pack(side="left")
        self.icon_button(head, "\u2699", lambda: self.show_page("settings")).pack(side="right")
        self.icon_button(head, "?", lambda: self.show_page("help")).pack(side="right", padx=(0, 2))
        self.icon_button(head, "\u2198", self.enter_compact).pack(side="right", padx=(0, 2))
        stats_img = ctk.CTkImage(light_image=bars_icon(MUTED[0]), dark_image=bars_icon(MUTED[1]),
                                 size=(18, 18))
        ctk.CTkButton(head, text="", image=stats_img, width=34, height=34, corner_radius=10,
                      fg_color="transparent", hover_color=CARD_BORDER,
                      command=lambda: self.show_page("stats")).pack(side="right", padx=(0, 2))

        self.test_banner = ctk.CTkButton(p, text="TEST MODE  \u00b7  click to go back to live",
                                         height=26, corner_radius=10, fg_color=AMBER,
                                         hover_color=blend(AMBER, "#000000", 0.15),
                                         text_color="#1F1300", font=self.font(11, "bold"),
                                         command=lambda: self.set_test(False))
        # Shown when LightBurn can't be read for a while (minimised, covered, moved)
        self.health_banner = ctk.CTkButton(
            p, text="Can't read LightBurn  \u00b7  click to look again", height=26,
            corner_radius=10, fg_color=SLATE, hover_color=blend(SLATE, "#000000", 0.15),
            text_color="#FFFFFF", font=self.font(11, "bold"), command=self.find_lightburn_again)
        # Shown while an alarm is active (fire, overheating, no exhaust)
        self.alarm_banner = ctk.CTkButton(
            p, text="", height=30, corner_radius=10, fg_color=RED,
            hover_color=blend(RED, "#000000", 0.15), text_color="#FFFFFF",
            font=self.font(12, "bold"), command=self.acknowledge_alarm)

        card = RoundPanel(p, radius=22, behind=BG, scale=self.scale)
        card.pack(fill="both", expand=True, padx=int(18 * self.scale), pady=int(14 * self.scale))
        self.card = card

        size = int(230 * self.scale)
        self.orb = tk.Canvas(card, width=size, height=size, highlightthickness=0, bd=0)
        self.orb.pack(pady=(26, 6))
        self.orb_size = size

        self.title_lbl = ctk.CTkLabel(card, text="Starting", font=self.font(34, "bold"),
                                      text_color=TEXT, bg_color=CARD)
        self.title_lbl.pack()
        self.sub_lbl = ctk.CTkLabel(card, text="Looking for LightBurn", font=self.font(14),
                                    text_color=MUTED, wraplength=290, bg_color=CARD)
        self.sub_lbl.pack(pady=(2, 18))

        self.relay_panel = ctk.CTkFrame(p, fg_color="transparent")
        self.relay_panel.pack(fill="x", padx=15, pady=(0, 8))

        foot = ctk.CTkFrame(p, fg_color="transparent")
        foot.pack(fill="x", padx=22, pady=(0, 14))
        self.check_lbl = ctk.CTkLabel(foot, text="", font=self.font(11), text_color=MUTED)
        self.check_lbl.pack(side="left")
        self.ard_lbl = ctk.CTkLabel(foot, text="", font=self.font(11), text_color=MUTED)
        self.ard_lbl.pack(side="right")
        # Green = connected, red = not; a label of its own so only the dot is coloured
        self.ard_dot = ctk.CTkLabel(foot, text="", font=self.font(11), width=0)
        self.ard_dot.pack(side="right", padx=(0, 4))

    def section(self, master, title, key=None):
        """A titled card. With a key (Settings page) it's a category: a heading
        with its short description and ▲ ▼ to move it, above the card. It's
        laid out by apply_settings_order(), in the order the user chose."""
        if key:
            holder = PlainFrame(master)
            self.settings_sections[key] = holder
            self.section_titles[key] = title
            head = PlainFrame(holder)
            head.pack(fill="x", padx=(8, 2), pady=(20, 8))
            arrows = {}
            for sym, step in (("▼", 1), ("▲", -1)):
                arrows[step] = ctk.CTkButton(
                    head, text=sym, width=26, height=24, corner_radius=6,
                    fg_color="transparent", hover_color=CARD_BORDER, text_color=MUTED,
                    text_color_disabled=CARD_BORDER, font=self.font(10),
                    command=lambda s=step: self.move_section(key, s))
                arrows[step].pack(side="right")
            self.section_arrows[key] = arrows
            PlainLabel(head, text=title, font=self.font(14, "bold")).pack(fill="x")
            PlainLabel(head, text=self.CATEGORY_HINTS.get(key, ""), font=self.font(11),
                       text_color=MUTED).pack(fill="x", pady=(1, 0))
            box = ctk.CTkFrame(holder, fg_color=CARD, corner_radius=16, border_width=1,
                               border_color=CARD_BORDER)
            box.pack(fill="x")
            return box
        PlainLabel(master, text=title.upper(), font=self.font(11, "bold"),
                   text_color=MUTED).pack(fill="x", padx=6, pady=(14, 6))
        box = ctk.CTkFrame(master, fg_color=CARD, corner_radius=16, border_width=1,
                           border_color=CARD_BORDER)
        box.pack(fill="x")
        return box

    # ------------------------------------------- Settings categories and order
    # Order on a fresh install (no saved order); people can rearrange it.
    # Each key names one category created with section(body, title, key) in
    # build_settings_page(). The order the user chooses is saved as a list of
    # these keys in settings["settings_order"].
    SETTINGS_ORDER = ["detection", "arduino", "relays", "telegram", "notifications",
                      "display", "reports", "windows", "shop", "test", "help",
                      "diagnostics", "check"]
    CATEGORY_HINTS = {
        "detection": "Screen, LightBurn API or both; how often",
        "arduino": "USB stack light and buzzer",
        "relays": "Buttons for a fan, air assist and more",
        "telegram": "Bot, chat, commands from your phone",
        "notifications": "What to be told, quiet hours, channels",
        "display": "Blink, always on top, chime, appearance",
        "reports": "Hourly, daily and weekly laser-time reports",
        "windows": ("Start with Windows, tray icon, shortcut" if IS_WIN else "Open at login, menu bar icon, shortcut"),
        "shop": "Opening hours and weekly goal",
        "test": "Try the app without the laser",
        "help": "Help, setup assistant, updates",
        "diagnostics": "What Beacon reads from LightBurn",
        "check": "Measure how well LightBurn is read (beta)",
    }

    def settings_order(self):
        """The saved order, with categories it doesn't know (new in this version)
        slotted in where they sit in the default order."""
        keys = [k for k in self.SETTINGS_ORDER if k in self.settings_sections]
        saved = [k for k in (self.s.get("settings_order") or []) if k in keys]
        if not saved:
            return keys
        order = list(dict.fromkeys(saved))
        for k in keys:
            if k not in order:
                before = keys[:keys.index(k)]
                at = max((order.index(b) + 1 for b in before if b in order), default=0)
                order.insert(at, k)
        return order

    def apply_settings_order(self):
        """Lay every category out, in the saved order, above the footer."""
        order = self.settings_order()
        for key in order:
            self.settings_sections[key].pack_forget()
        for key in order:
            self.settings_sections[key].pack(fill="x", before=self.settings_footer)
        self._update_section_arrows(order)

    def _update_section_arrows(self, order):
        """Grey out ▲ on the first category and ▼ on the last."""
        for n, key in enumerate(order):
            arrows = self.section_arrows[key]
            arrows[-1].configure(state="disabled" if n == 0 else "normal")
            arrows[1].configure(state="disabled" if n == len(order) - 1 else "normal")

    def move_section(self, key, step):
        """Move a category one place up (step=-1) or down (step=1) and save the
        new order (the ▲ ▼ buttons). Only that category is re-placed, and the
        page scrolls with it, so it stays under the mouse for another click."""
        order = self.settings_order()
        i = order.index(key)
        j = i + step
        if not 0 <= j < len(order):
            return
        order[i], order[j] = order[j], order[i]
        self.s["settings_order"] = order
        save_settings(self.s)
        holder = self.settings_sections[key]
        canvas = self.settings_body._parent_canvas
        before = holder.winfo_y()
        if j + 1 < len(order):
            holder.pack_configure(before=self.settings_sections[order[j + 1]])
        else:
            holder.pack_configure(after=self.settings_sections[order[j - 1]])
        self._update_section_arrows(order)
        canvas.update_idletasks()
        height = max(holder.master.winfo_height(), 1)
        top = canvas.canvasy(0) + holder.winfo_y() - before
        canvas.yview_moveto(max(0.0, top / height))

    def reset_settings_order(self):
        """Back to the default order (SETTINGS_ORDER)."""
        self.s["settings_order"] = []
        save_settings(self.s)
        self.apply_settings_order()

    def row(self, box, label, sub=None):
        """A settings row: a label (and optional grey explanation) on the left.
        Returns the row frame so the caller can pack a control on its right."""
        r = PlainFrame(box)
        r.pack(fill="x", padx=14, pady=9)
        txt = PlainFrame(r)
        txt.pack(side="left", fill="x", expand=True)
        PlainLabel(txt, text=label, font=self.font(13)).pack(fill="x", pady=(2, 2))
        if sub:
            PlainLabel(txt, text=sub, font=self.font(11), text_color=MUTED,
                       wraplength=200).pack(fill="x", pady=(0, 2))
        return r

    def note(self, box, text, pady=(0, 8)):
        """A paragraph of small grey explanatory text inside a card."""
        lbl = PlainLabel(box, text=text, font=self.font(11), text_color=MUTED, wraplength=300)
        lbl.pack(fill="x", padx=14, pady=pady)
        return lbl

    def menu_row(self, box, key, label, sub, options, on_change=None, width=130):
        """A setting picked from a menu. options: {stored value: shown text}."""
        r = self.row(box, label, sub)
        shown = {v: t for v, t in options.items()}
        back = {t: v for v, t in shown.items()}

        def changed(text):
            self.s[key] = back[text]
            save_settings(self.s)
            if on_change:
                on_change()
        om = ctk.CTkOptionMenu(r, values=list(shown.values()), width=width, height=28,
                               font=self.font(12), dropdown_font=self.font(12), command=changed)
        om.set(shown.get(self.s[key], list(shown.values())[0]))
        om.pack(side="right")
        return om

    def entry_row(self, box, key, label, secret=False, kind=str, placeholder=""):
        """A labelled text box saved as you type (kind=int/float for numbers)."""
        PlainLabel(box, text=label, font=self.font(11), text_color=MUTED).pack(
            fill="x", padx=14, pady=(8, 2))
        var = ctk.StringVar(value="" if self.s[key] in (None, "") else str(self.s[key]))

        def changed(*_):
            text = var.get().strip()
            if kind is str:
                self.s[key] = text
            else:
                try:
                    self.s[key] = kind(text or 0)
                except ValueError:
                    return
            save_settings(self.s)
        var.trace_add("write", changed)
        e = ctk.CTkEntry(box, textvariable=var, show="•" if secret else "", height=30,
                         font=self.font(12), placeholder_text=placeholder)
        e.pack(fill="x", padx=14)
        return var

    def open_row(self, box, label, sub, page, button="Open"):
        """A row with an "Open ›" button that goes to another page."""
        r = self.row(box, label, sub)
        ctk.CTkButton(r, text=button + "  ›", width=90, height=30, corner_radius=10,
                      fg_color="transparent", hover_color=CARD_BORDER, text_color=ACCENT,
                      font=self.font(12, "bold"),
                      command=lambda: self.show_page(page)).pack(side="right")
        return r

    def action_button(self, master, text, command, width=120):
        """An outlined secondary button (Connect, Forget, Send test...)."""
        return ctk.CTkButton(master, text=text, width=width, height=30, corner_radius=8,
                             fg_color="transparent", border_width=1, border_color=CARD_BORDER,
                             hover_color=CARD_BORDER, text_color=ACCENT,
                             font=self.font(12, "bold"), command=command)

    def page_frame(self, title, back="settings", back_text="Back"):
        """A page with a header (← Back · Title) and a scrolling body. Returns (page, body)."""
        p = ctk.CTkFrame(self.root, fg_color="transparent")
        head = ctk.CTkFrame(p, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(16, 0))
        ctk.CTkButton(head, text="←  " + back_text, width=80, height=32,
                      corner_radius=10, fg_color="transparent", hover_color=CARD_BORDER,
                      text_color=ACCENT, font=self.font(13, "bold"),
                      command=lambda: self.show_page(back)).pack(side="left")
        ctk.CTkLabel(head, text=title, font=self.font(15, "bold"),
                     text_color=TEXT).pack(side="right", padx=4)
        body = ctk.CTkScrollableFrame(p, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        return p, body

    def switch_row(self, box, key, label, sub, on_change=None):
        """An on/off switch bound to the boolean setting `key`: saved as soon as
        it's flipped. Forces the orb and pill to redraw, since several
        switches change how they look."""
        r = self.row(box, label, sub)
        var = ctk.BooleanVar(value=bool(self.s[key]))

        def changed():
            self.s[key] = bool(var.get())
            save_settings(self.s)
            if on_change:
                on_change()
            self._orb_key = self._pill_key = None
        ctk.CTkSwitch(r, text="", variable=var, onvalue=True, offvalue=False,
                      width=46, command=changed).pack(side="right")
        return var

    def build_settings_page(self):
        """Settings: every category open on one scrolling page, each under its
        heading, in the order the user chose (▲ ▼). Layout and text are
        PlainFrame / PlainLabel, which keeps the long page quick to show and
        to scroll."""
        p = self.settings_page = PlainFrame(self.root)      # placed, see park_settings
        head = PlainFrame(p)
        head.pack(fill="x", padx=18, pady=(16, 0))
        ctk.CTkButton(head, text="\u2190  Done", width=80, height=32, corner_radius=10,
                      fg_color="transparent", hover_color=CARD_BORDER, text_color=ACCENT,
                      font=self.font(13, "bold"),
                      command=lambda: self.show_page("status")).pack(side="left")
        PlainLabel(head, text="Settings", font=self.font(15, "bold")).pack(side="right", padx=4)
        body = ctk.CTkScrollableFrame(p, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        self.settings_body = body
        self.settings_sections, self.section_titles, self.section_arrows = {}, {}, {}

        # Categories are built in any order; apply_settings_order() lays them out.
        # --- Display
        # Every category below follows the same pattern: box = self.section(...)
        # for the card, then rows built with the helpers above. Each control
        # saves its setting the moment it changes; there is no Save button.
        box = self.section(body, "Display", "display")
        self.switch_row(box, "blink", "Blink for Framing / Disconnected",
                        "Off keeps every state steady")
        self.switch_row(box, "topmost", "Always on top", None,
                        on_change=lambda: self.root.attributes("-topmost", self.s["topmost"]))
        self.chime_var = self.switch_row(box, "chime", "Chime when a job finishes", None)
        r = self.row(box, "Chime sound")
        ctk.CTkButton(r, text="Choose…", width=74, height=28, corner_radius=8,
                      font=self.font(12), command=self.choose_chime).pack(side="right")
        self.chime_reset_btn = ctk.CTkButton(
            r, text="✕", width=28, height=28, corner_radius=8, fg_color="transparent",
            hover_color=CARD_BORDER, text_color=MUTED, font=self.font(12),
            command=self.reset_chime)
        self.chime_reset_btn.pack(side="right", padx=(0, 4))
        ctk.CTkButton(r, text="▶", width=28, height=28, corner_radius=8,
                      fg_color="transparent", hover_color=CARD_BORDER, text_color=ACCENT,
                      font=self.font(12),
                      command=lambda: play_chime(self.s["chime_file"])).pack(side="right",
                                                                              padx=(0, 2))
        self.chime_name = PlainLabel(box, text="", font=self.font(11), text_color=MUTED,
                                       anchor="w")
        self.chime_name.pack(fill="x", padx=14, pady=(0, 8))
        self.show_chime_name()

        r = self.row(box, "Appearance")
        seg = ctk.CTkSegmentedButton(r, values=["System", "Light", "Dark"],
                                     command=self.set_appearance, font=self.font(12))
        seg.set(self.s["appearance"])
        seg.pack(side="right")
        PlainFrame(box, height=4).pack()

        # --- Detection: screen reading or LightBurn's API
        box = self.section(body, "Detection", "detection")
        self.row(box, "Read LightBurn's status from", None)
        self.detect_seg = ctk.CTkSegmentedButton(box, values=list(self.DETECT_MODES.values()),
                                                 command=self.set_detect_mode,
                                                 font=self.font(12))
        self.detect_seg.set(self.DETECT_MODES[self.s["detect_mode"]])
        self.detect_seg.pack(fill="x", padx=14, pady=(0, 6))
        self.note(box, "Screen: any LightBurn version, but LightBurn must not be minimised. "
                       "API: LightBurn 2.2 or newer, works when minimised; it has no Framing "
                       "state, so Beacon works it out from LightBurn's progress numbers. "
                       "Both: reads the screen and the API together for the quickest, most "
                       "accurate times, and uses the API alone while LightBurn is minimised.",
                  pady=(0, 6))
        self.row(box, "Check every", "Faster reacts quicker but uses more CPU. "
                                     "LightBurn's API is asked at most 5 times a second.")
        labels = {0.1: "0.1 s", 0.25: "0.25 s", 0.5: "0.5 s", 1.0: "1 s", 2.0: "2 s"}
        seg2 = ctk.CTkSegmentedButton(box, values=list(labels.values()),
                                      command=self.set_interval, font=self.font(12))
        seg2.set(labels.get(float(self.s["interval"]), "1 s"))
        seg2.pack(fill="x", padx=14, pady=(0, 8))
        r = self.row(box, "LightBurn API", None)
        self.api_forget_btn = self.action_button(r, "Forget", self.forget_api, 70)
        self.api_forget_btn.pack(side="right")
        self.api_btn = self.action_button(r, "Connect", self.connect_api, 110)
        self.api_btn.pack(side="right", padx=6)
        self.api_status = self.note(box, "", pady=(0, 12))
        self.show_api_status()

        # --- Relay buttons
        box = self.section(body, "Relay buttons", "relays")
        r = self.row(box, "Buttons", "Add, rename or remove buttons for a fan, "
                                     "air assist and more")
        ctk.CTkButton(r, text="Manage  ›", width=90, height=30, corner_radius=10,
                      fg_color="transparent", hover_color=CARD_BORDER, text_color=ACCENT,
                      font=self.font(12, "bold"),
                      command=lambda: self.open_relays("settings")).pack(side="right")

        # --- Telegram bot
        box = self.section(body, "Telegram bot", "telegram")
        PlainLabel(box, text="In Telegram, message @BotFather, send /newbot and paste the "
                               "token it gives you below. Then send your new bot any message "
                               "and press Find.",
                     font=self.font(11), text_color=MUTED, anchor="w", justify="left",
                     wraplength=300).pack(fill="x", padx=14, pady=(10, 0))

        self.tg_token_var = ctk.StringVar(value=self.s["tg_token"])
        self.tg_token_var.trace_add("write", lambda *_: self._set_text_setting(
            "tg_token", self.tg_token_var))
        PlainLabel(box, text="Bot token", font=self.font(11), text_color=MUTED,
                     anchor="w").pack(fill="x", padx=14, pady=(8, 0))
        ctk.CTkEntry(box, textvariable=self.tg_token_var, show="•", height=30,
                     font=self.font(12)).pack(fill="x", padx=14)

        PlainLabel(box, text="Chat ID", font=self.font(11), text_color=MUTED,
                     anchor="w").pack(fill="x", padx=14, pady=(8, 0))
        chat_row = PlainFrame(box)
        chat_row.pack(fill="x", padx=14)
        self.tg_chat_var = ctk.StringVar(value=str(self.s["tg_chat"]))
        self.tg_chat_var.trace_add("write", lambda *_: self._set_text_setting(
            "tg_chat", self.tg_chat_var))
        ctk.CTkButton(chat_row, text="Find", width=64, height=30, corner_radius=8,
                      font=self.font(12), command=self.tg_find_chat).pack(side="right")
        ctk.CTkEntry(chat_row, textvariable=self.tg_chat_var, height=30,
                     font=self.font(12)).pack(side="left", fill="x", expand=True, padx=(0, 6))

        test_row = PlainFrame(box)
        test_row.pack(fill="x", padx=14, pady=(8, 0))
        ctk.CTkButton(test_row, text="Send test", width=90, height=30, corner_radius=8,
                      fg_color="transparent", border_width=1, border_color=CARD_BORDER,
                      hover_color=CARD_BORDER, text_color=ACCENT, font=self.font(12, "bold"),
                      command=self.send_test_notification).pack(side="left")
        self.tg_status = PlainLabel(box, text="", font=self.font(11), text_color=MUTED,
                                      anchor="w", justify="left", wraplength=300)
        self.tg_status.pack(fill="x", padx=14, pady=(4, 0))
        self.switch_row(box, "tg_commands", "Telegram commands",
                        "Reply to /status, /today, /relays and more from your phone")
        PlainFrame(box, height=4).pack()

        # --- Notifications
        box = self.section(body, "Notifications", "notifications")
        self.switch_row(box, "notify_pc", f"{OS_NAME} notifications",
                        "Pop-ups in the corner of the screen", on_change=self._pc_notify_changed)
        PlainLabel(box, text="Notify me when", font=self.font(13), text_color=TEXT,
                     anchor="w").pack(fill="x", padx=14, pady=(10, 2))
        for key, label in NOTIFY_EVENTS:
            var = ctk.BooleanVar(value=bool(self.s[f"notify_{key}"]))
            ctk.CTkCheckBox(box, text=label, variable=var, font=self.font(12),
                            checkbox_width=20, checkbox_height=20,
                            command=lambda k=key, v=var: self._set_flag(f"notify_{k}", v)
                            ).pack(fill="x", padx=14, pady=3)
        self.note(box, "Safety alarms (fire, overheating, no exhaust, unattended) are always "
                       "sent when their sensor is on.", pady=(4, 0))
        hours = {h: f"{h:02d}:00" for h in range(24)}
        self.switch_row(box, "quiet_on", "Quiet hours",
                        "Only safety alarms and disconnects get through")
        q = PlainFrame(box)
        q.pack(fill="x", padx=14, pady=(0, 6))
        for label, key in (("from", "quiet_start"), ("to", "quiet_end")):
            PlainLabel(q, text=label, font=self.font(12),
                         text_color=MUTED).pack(side="left", padx=(0, 6))
            om = ctk.CTkOptionMenu(q, values=list(hours.values()), width=84, height=28,
                                   font=self.font(12), dropdown_font=self.font(12),
                                   command=lambda v, k=key: self._set_value(k, int(v[:2])))
            om.set(hours[int(self.s[key])])
            om.pack(side="left", padx=(0, 10))
        self.open_row(box, "More channels", "Discord, Pushover, email, SMS and camera snapshots",
                      "channels")
        self.open_row(box, "Alerts & safety", "Long jobs, laser free, sensors, unattended runs",
                      "safety")
        self.open_row(box, "Phone & Wi-Fi", "Phone dashboard and the Wi-Fi light", "remote")
        PlainFrame(box, height=6).pack()

        # --- Usage reports
        box = self.section(body, "Usage reports", "reports")
        PlainLabel(box, text="Laser-on time (running only, pauses left out), with jobs, cost "
                               "and how it compares with the period before.",
                     font=self.font(11), text_color=MUTED, anchor="w", justify="left",
                     wraplength=300).pack(fill="x", padx=14, pady=(10, 0))
        hours = [f"{h:02d}:00" for h in range(24)]

        def report_row(kind, label, sub):
            """An on/off switch for one report kind (hourly / daily / weekly)."""
            r = self.row(box, label, sub)
            var = ctk.BooleanVar(value=bool(self.s[f"report_{kind}"]))
            ctk.CTkSwitch(r, text="", variable=var, onvalue=True, offvalue=False, width=46,
                          command=lambda: self._report_toggled(kind, var)).pack(side="right")

        def time_line(parts):
            """A line of words and dropdowns under a report switch, e.g. "on
            [Sunday] at [18:00]". parts: (kind, "text") or (kind, (setting
            key, choices, current choice, text -> value))."""
            line = PlainFrame(box)
            line.pack(fill="x", padx=14, pady=(0, 6))
            for kind, item in parts:
                if isinstance(item, str):
                    PlainLabel(line, text=item, font=self.font(12),
                                 text_color=MUTED).pack(side="left", padx=(0, 6))
                    continue
                key, values, current, parse = item
                om = ctk.CTkOptionMenu(line, values=values, width=96, height=28,
                                       font=self.font(12), dropdown_font=self.font(12),
                                       command=lambda v, k=key, p=parse, kd=kind:
                                       self._set_report_time(k, p(v), kd))
                om.set(current)
                om.pack(side="left", padx=(0, 6))

        report_row("hourly", "Hourly", "At the top of every hour, for the hour before")
        report_row("daily", "Daily", "Every day, for the last 24 hours")
        time_line([("daily", "at"),
                   ("daily", ("report_daily_hour", hours, hours[int(self.s["report_daily_hour"])],
                              lambda v: int(v[:2])))])
        report_row("weekly", "Weekly", "Once a week, for the last 7 days, day by day")
        time_line([("weekly", "on"),
                   ("weekly", ("report_weekly_day", WEEKDAY_NAMES,
                               WEEKDAY_NAMES[int(self.s["report_weekly_day"])],
                               WEEKDAY_NAMES.index)),
                   ("weekly", "at"),
                   ("weekly", ("report_weekly_hour", hours,
                               hours[int(self.s["report_weekly_hour"])], lambda v: int(v[:2])))])
        self.switch_row(box, "report_skip_empty", "Skip quiet periods",
                        "Don't send a report when the laser wasn't used")

        PlainLabel(box, text="Send reports to", font=self.font(13), text_color=TEXT,
                     anchor="w").pack(fill="x", padx=14, pady=(8, 2))
        for key, label in (("report_tg", "Telegram"), ("report_pc", f"{OS_NAME} notifications"),
                           ("report_other", "Discord, Pushover and email (if set up)")):
            var = ctk.BooleanVar(value=bool(self.s[key]))
            ctk.CTkCheckBox(box, text=label, variable=var, font=self.font(12),
                            checkbox_width=20, checkbox_height=20,
                            command=lambda k=key, v=var: self._set_flag(k, v)
                            ).pack(fill="x", padx=14, pady=3)
        ctk.CTkButton(box, text="Send a report now", width=140, height=30, corner_radius=8,
                      fg_color="transparent", border_width=1, border_color=CARD_BORDER,
                      hover_color=CARD_BORDER, text_color=ACCENT, font=self.font(12, "bold"),
                      command=self.send_report_now).pack(anchor="w", padx=14, pady=(8, 0))
        self.report_status = PlainLabel(box, text="Sends the last 24 hours, so you can see "
                                                    "what reports look like.",
                                          font=self.font(11), text_color=MUTED, anchor="w",
                                          justify="left", wraplength=300)
        self.report_status.pack(fill="x", padx=14, pady=(4, 12))

        # --- Arduino
        # The port list is filled by refresh_ports(); port_status shows the
        # link's live status (Connected on COM5 / No Arduino found...), updated
        # in poll().
        box = self.section(body, "Arduino light", "arduino")
        self.switch_row(box, "arduino", "Send status to Arduino",
                        "Red = running, green = stopped",
                        on_change=lambda: setattr(self.arduino, "enabled", self.s["arduino"]))
        r = self.row(box, "Port")
        self.port_box = ctk.CTkComboBox(r, values=["Auto-detect"], width=150,
                                        command=self.set_port, font=self.font(12),
                                        dropdown_font=self.font(12))
        self.port_box.pack(side="right")
        ctk.CTkButton(r, text="\u21bb", width=30, height=28, corner_radius=8,
                      fg_color="transparent", hover_color=CARD_BORDER, text_color=MUTED,
                      font=self.font(14), command=self.refresh_ports).pack(side="right", padx=4)
        self.refresh_ports()
        self.port_status = PlainLabel(box, text="", font=self.font(11), text_color=MUTED,
                                        anchor="w")
        self.port_status.pack(fill="x", padx=14, pady=(0, 10))

        # --- Windows
        box = self.section(body, OS_NAME, "windows")
        self.switch_row(box, "autostart", "Start with Windows" if IS_WIN else "Open at login",
                        "Opens as the compact pill when you sign in",
                        on_change=self.apply_windows_settings)
        self.switch_row(box, "tray_always", "Icon by the clock" if IS_WIN else "Menu bar icon",
                        "Right-click it to show, mute or quit" if IS_WIN else
                        "Click it to show, mute or quit",
                        on_change=self.apply_windows_settings)
        self.switch_row(box, "hotkey_on", "Keyboard shortcut",
                        "Show / hide from anywhere", on_change=self.apply_windows_settings)
        self.menu_row(box, "hotkey_key", "Shortcut", None,
                      {k: (f"Ctrl + Alt + {k}" if IS_WIN else f"⌃ ⌥ {k}")
                       for k in "BLSJ"},
                      on_change=self.apply_windows_settings, width=140)
        self.hotkey_status = self.note(box, "", pady=(0, 8))

        # --- Shop
        box = self.section(body, "Shop", "shop")
        self.note(box, "Used for utilization (how much of your opening hours the laser runs) "
                       "and the weekly goal on the Statistics page.", pady=(10, 0))
        self.menu_row(box, "shop_days", "Open on", None, SCHED_DAYS, width=120)
        hours_menu = {h: f"{h:02d}:00" for h in range(25)}
        self.menu_row(box, "shop_start", "Opens at", None, hours_menu, width=100)
        self.menu_row(box, "shop_end", "Closes at", None, hours_menu, width=100)
        self.menu_row(box, "goal_hours", "Weekly goal", "Laser hours you aim for each week",
                      {h: (f"{h} h" if h else "Off") for h in
                       (0, 5, 10, 15, 20, 25, 30, 40, 50, 60)}, width=100)

        # --- Test mode
        # Test mode replaces LightBurn as the source of statuses: the buttons
        # below set a status by hand, and the 10 s test job runs Running ->
        # Ready by itself. Test jobs are never counted in statistics (see
        # RunLog.observe).
        box = self.section(body, "Test mode", "test")
        r = self.row(box, "Simulate LightBurn",
                     "Fake a status to try the app and Arduino light without the laser")
        ctk.CTkSwitch(r, text="", variable=self.test_var, onvalue=True, offvalue=False,
                      width=46, command=lambda: self.set_test(self.test_var.get())
                      ).pack(side="right")
        grid = PlainFrame(box)
        grid.pack(fill="x", padx=11, pady=(0, 4))
        self.test_buttons = {}
        for i, st in enumerate(["ready", "busy", "paused", "framing", "disconnected", None]):
            b = ctk.CTkButton(grid, text=look(st)[1], height=32, corner_radius=10,
                              font=self.font(12), command=lambda st=st: self.simulate(st))
            b.grid(row=i // 3, column=i % 3, padx=3, pady=3, sticky="ew")
            self.test_buttons[st] = b
        for col in range(3):
            grid.grid_columnconfigure(col, weight=1)
        self.demo_btn = ctk.CTkButton(box, text="Run a 10 s test job", height=32,
                                      corner_radius=10, fg_color="transparent",
                                      border_width=1, border_color=CARD_BORDER,
                                      hover_color=CARD_BORDER, text_color=ACCENT,
                                      font=self.font(12, "bold"), command=self.toggle_demo_job)
        self.demo_btn.pack(fill="x", padx=14, pady=(2, 12))

        # --- Diagnostics
        box = self.section(body, "Diagnostics", "diagnostics")
        self.switch_row(box, "diagnostics", "Show what it reads",
                        "Helpful if the status looks wrong")
        self.diag = ctk.CTkTextbox(box, height=130, font=ctk.CTkFont(family=MONO_FONT, size=11),
                                   corner_radius=10, wrap="word")
        self.diag.pack(fill="x", padx=12, pady=(0, 12))
        self.diag.configure(state="disabled")

        # --- Help & updates
        box = self.section(body, "Help & updates", "help")
        self.open_row(box, "Help", "Wiring diagrams, questions and answers, support", "help")
        r = self.row(box, "Setup assistant", "Walk through the setup again")
        self.action_button(r, "Start", lambda: self.show_page("wizard"), width=80).pack(side="right")
        self.switch_row(box, "auto_update", "Check for updates", "Once a day")
        r = self.row(box, f"{APP_NAME} {APP_VERSION}", None)
        self.update_btn = self.action_button(r, "Check now", lambda: self.check_updates(), 100)
        self.update_btn.pack(side="right")
        self.update_status = self.note(box, "" if UPDATE_URL else
                                       "Updates aren't set up in this build.", pady=(0, 10))

        # --- Detection check (beta)
        box = self.section(body, "Detection check (beta)", "check")
        self.switch_row(box, "check_log", "Log detection accuracy",
                        "Measures how well LightBurn is read", on_change=self._check_changed)
        self.note(box, "Saves each status change and suspected misread with a small picture of "
                       "LightBurn's status area (never the whole screen). Stays on this PC "
                       "until you export it. If the status is ever wrong, right-click the pill "
                       "or tray icon › Status is wrong…", pady=(0, 6))
        self.check_summary = self.note(box, "", pady=(0, 6))
        r = PlainFrame(box)
        r.pack(fill="x", padx=14, pady=(2, 0))
        self.check_report_btn = self.action_button(r, "Status is wrong…",
                                                   self.report_wrong_from_settings, 130)
        self.check_report_btn.pack(side="left")
        self.action_button(r, "Export…", self.export_check, 80).pack(side="left", padx=6)
        r = PlainFrame(box)
        r.pack(fill="x", padx=14, pady=(6, 0))
        self.action_button(r, "Open folder", self.open_check_folder, 100).pack(side="left")
        self.action_button(r, "Start again", self.reset_check, 100).pack(side="left", padx=6)
        self.check_status = self.note(box, "", pady=(4, 10))
        self.update_check_summary()

        # At the end of the page: Reset order, and the version / OCR engine /
        # settings folder for support questions.
        foot = self.settings_footer = PlainFrame(body)
        foot.pack(pady=(22, 6))
        ctk.CTkButton(foot, text="Reset order", width=140, height=24, corner_radius=8,
                      fg_color="transparent", hover_color=CARD_BORDER, text_color=MUTED,
                      font=self.font(11), command=self.reset_settings_order).pack()
        PlainLabel(foot, text=f"{APP_NAME} {APP_VERSION}  \u00b7  reading with {self.ocr_name}"
                              f"\nsettings in %APPDATA%\\{SETTINGS_FOLDER}", font=self.font(10),
                   text_color=MUTED, anchor="center", justify="center").pack(pady=(6, 0))
        self.apply_settings_order()

    def build_relays_page(self):
        """Build the Relay buttons page (header plus a scrolling body); the body
        itself is filled by rebuild_relays_page(), which runs again whenever
        a relay is added or removed."""
        p = self.relays_page = ctk.CTkFrame(self.root, fg_color="transparent")
        head = ctk.CTkFrame(p, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(16, 0))
        ctk.CTkButton(head, text="←  Back", width=80, height=32, corner_radius=10,
                      fg_color="transparent", hover_color=CARD_BORDER, text_color=ACCENT,
                      font=self.font(13, "bold"),     # back to where it was opened from
                      command=lambda: self.show_page(getattr(self, "relays_return", "settings"))
                      ).pack(side="left")
        ctk.CTkLabel(head, text="Relay buttons", font=self.font(15, "bold"),
                     text_color=TEXT).pack(side="right", padx=4)

        self.relays_body = ctk.CTkScrollableFrame(p, fg_color="transparent")
        self.relays_body.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        self.relay_name_vars = {}
        self.rebuild_relays_page()

    def rebuild_relays_page(self):
        """Fill the Relay buttons page: one card per relay in use (name,
        automatic rule, keep-on time, switch-on delay, schedule, and which
        device it switches), a Home Assistant card when any relay uses it,
        and "Add a button" with the pins still free."""
        if not hasattr(self, "relays_body"):    # not opened yet: built fresh when it is
            return
        body = self.relays_body
        for w in body.winfo_children():
            w.destroy()
        self.relay_name_vars, self._relay_boxes, self._plug_status_lbls = {}, {}, {}
        ctk.CTkLabel(body, text="Name each button and pick when it switches on by itself. "
                                "Pressing a button on the main screen overrides the automatic "
                                "rule until the laser's status changes. Right-click a button "
                                "there to rename it.",
                     font=self.font(11), text_color=MUTED, wraplength=300, justify="left",
                     anchor="w").pack(fill="x", padx=6, pady=(10, 0))

        mode_labels = {k: v[0] for k, v in AUTO_MODES.items()}
        mode_keys = {v: k for k, v in mode_labels.items()}
        keep_keys = {v: k for k, v in KEEP_ON.items()}
        for i, pin in enumerate(RELAY_PINS):
            r = self.s["relays"][i]
            if not r["use"]:
                continue
            box = self.section(body, f"Pin {pin}")
            self._relay_boxes[i] = box

            top = ctk.CTkFrame(box, fg_color="transparent")
            top.pack(fill="x", padx=12, pady=(10, 6))
            ctk.CTkButton(top, text="Remove", width=70, height=30, corner_radius=10,
                          fg_color="transparent", hover_color=CARD_BORDER, text_color=RED,
                          font=self.font(12),
                          command=lambda i=i: self.set_relay(i, "use", False)).pack(side="right")
            name = ctk.StringVar(value=r["name"])
            name.trace_add("write", lambda *_, i=i, v=name:
                           self.set_relay(i, "name", v.get().strip()[:24] or f"Relay {i + 1}"))
            ctk.CTkEntry(top, textvariable=name, height=32, font=self.font(13)).pack(
                side="left", fill="x", expand=True, padx=(0, 10))
            self.relay_name_vars[i] = name

            bottom = ctk.CTkFrame(box, fg_color="transparent")
            bottom.pack(fill="x", padx=12, pady=(0, 12))
            mode = ctk.CTkOptionMenu(bottom, values=list(mode_labels.values()), width=180,
                                     height=30, font=self.font(12), dropdown_font=self.font(12),
                                     command=lambda v, i=i: self.set_relay(i, "auto", mode_keys[v]))
            mode.set(mode_labels.get(r["auto"], mode_labels["manual"]))
            mode.pack(side="left")
            keep = ctk.CTkOptionMenu(bottom, values=list(KEEP_ON.values()), width=110,
                                     height=30, font=self.font(12), dropdown_font=self.font(12),
                                     command=lambda v, i=i: self.set_relay(i, "keep", keep_keys[v]))
            keep.set(KEEP_ON.get(r["keep"], KEEP_ON[0]))
            keep.pack(side="right")

            def menu(parent, options, current, key, i=i, width=110, rebuild=False):
                """A dropdown for relay i's setting `key`. options: {stored
                value: shown text}. rebuild=True redraws the page after a
                change (choosing a smart plug adds an address field)."""
                back = {t: v for v, t in options.items()}

                def changed(text):
                    self.set_relay(i, key, back[text])
                    if rebuild:
                        self.rebuild_relays_page()
                om = ctk.CTkOptionMenu(parent, values=list(options.values()), width=width,
                                       height=28, font=self.font(12), dropdown_font=self.font(12),
                                       command=changed)
                om.set(options.get(current, list(options.values())[0]))
                return om

            line = ctk.CTkFrame(box, fg_color="transparent")
            line.pack(fill="x", padx=12, pady=(0, 6))
            ctk.CTkLabel(line, text="Switches on", font=self.font(12),
                         text_color=MUTED).pack(side="left", padx=(0, 6))
            menu(line, {k: v for k, v in ON_DELAYS.items()}, r["on_delay"],
                 "on_delay").pack(side="left")

            sched = ctk.CTkFrame(box, fg_color="transparent")
            sched.pack(fill="x", padx=12, pady=(0, 6))
            svar = ctk.BooleanVar(value=bool(r["sched_on"]))
            ctk.CTkCheckBox(sched, text="Schedule", variable=svar, font=self.font(12),
                            checkbox_width=18, checkbox_height=18, width=20,
                            command=lambda i=i, v=svar: self.set_relay(i, "sched_on", bool(v.get()))
                            ).pack(side="left", padx=(0, 6))
            hrs = {h: f"{h:02d}:00" for h in range(25)}
            menu(sched, {k: v for k, v in SCHED_DAYS.items()}, r["sched_days"], "sched_days",
                 width=96).pack(side="left", padx=(0, 4))
            menu(sched, hrs, r["sched_start"], "sched_start", width=70).pack(side="left")
            ctk.CTkLabel(sched, text="–", text_color=MUTED).pack(side="left", padx=3)
            menu(sched, hrs, r["sched_end"], "sched_end", width=70).pack(side="left")

            dev = ctk.CTkFrame(box, fg_color="transparent")
            dev.pack(fill="x", padx=12, pady=(0, 6))
            ctk.CTkLabel(dev, text="Device", font=self.font(12),
                         text_color=MUTED).pack(side="left", padx=(0, 6))
            menu(dev, {k: v for k, v in smartplugs.PLUG_TYPES.items()}, r["target"], "target",
                 width=200, rebuild=True).pack(side="left")
            if r["target"] != "arduino":
                field = "entity" if r["target"] == "homeassistant" else "address"
                hint = {"entity": "Entity ID, e.g. switch.exhaust_fan",
                        "address": "Plug's IP address, e.g. 192.168.1.50"}[field]
                ctk.CTkLabel(box, text=hint, font=self.font(11), text_color=MUTED,
                             anchor="w").pack(fill="x", padx=12)
                fvar = ctk.StringVar(value=r[field])
                fvar.trace_add("write", lambda *_, i=i, k=field, v=fvar:
                               self.set_relay(i, k, v.get().strip()))
                ctk.CTkEntry(box, textvariable=fvar, height=30, font=self.font(12)).pack(
                    fill="x", padx=12)
                self._plug_status_lbls[i] = ctk.CTkLabel(box, text="", font=self.font(11),
                                                         text_color=MUTED, anchor="w")
                self._plug_status_lbls[i].pack(fill="x", padx=12)
            ctk.CTkFrame(box, height=6, fg_color="transparent").pack()

        if any(r["use"] and r["target"] == "homeassistant" for r in self.s["relays"]):
            box = self.section(body, "Home Assistant")
            self.entry_row(box, "ha_url", "Address, e.g. http://homeassistant.local:8123")
            self.entry_row(box, "ha_token", "Long-lived access token (your profile page)",
                           secret=True)
            ctk.CTkFrame(box, height=10, fg_color="transparent").pack()

        free = [pin for pin, r in zip(RELAY_PINS, self.s["relays"]) if not r["use"]]
        box = self.section(body, "Add a button")
        if free:
            row = ctk.CTkFrame(box, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=12)
            ctk.CTkLabel(row, text="Wired to pin", font=self.font(13),
                         text_color=TEXT).pack(side="left")
            self.add_pin = ctk.CTkOptionMenu(row, values=free, width=80, height=30,
                                             font=self.font(12), dropdown_font=self.font(12))
            self.add_pin.set(free[0])
            self.add_pin.pack(side="left", padx=8)
            ctk.CTkButton(row, text="+  Add", width=80, height=30, corner_radius=10,
                          font=self.font(12, "bold"), command=self.add_relay).pack(side="right")
        else:
            ctk.CTkLabel(box, text=f"All {len(RELAY_PINS)} relay slots have a button.",
                         font=self.font(12), text_color=MUTED).pack(padx=12, pady=12)

    def add_relay(self):
        """Turn on the relay for the pin picked in "Add a button" (the page is
        rebuilt by set_relay) and scroll its new card into view."""
        i = RELAY_PINS.index(self.add_pin.get())
        self.set_relay(i, "use", True)

        def scroll_to_new():             # bring the new button's card into view
            body = self.relays_body
            body.update_idletasks()
            box = self._relay_boxes.get(i)
            if box is not None and body.winfo_height() > 0:
                body._parent_canvas.yview_moveto(max(0.0, (box.winfo_y() - 40) /
                                                     body.winfo_height()))
        self.root.after(50, scroll_to_new)

    def build_pill(self):
        """Build the compact "pill": a small borderless always-on-top window
        with a coloured dot, the status word and (during a job) its timer.

        It's see-through round its rounded ends: the window's background is
        KEY_COLOR, which Windows is told to make transparent
        (set_window_key), and the RoundPanel inside draws the pill shape with
        hard edges against it. Drag it to move it, double-click to go back to
        the full window, right-click for its menu."""
        s = self.scale
        self.pill = tk.Toplevel(self.root)
        self.pill.withdraw()
        self.pill.overrideredirect(True)
        self.pill.attributes("-topmost", True)
        set_window_key(self.pill)
        # Sized to its contents with the same gap all round, so it hugs
        # the dot and text and only grows when the job timer shows
        pad = PILL_PAD
        frame = RoundPanel(self.pill, radius=None, behind=KEY_COLOR, scale=s, hard_edge=True)
        frame.pack()
        self.pill_frame = frame
        # Measured nudges (screen px): the rounded right end needs ~2 px more
        # to look as roomy as the left, the label adds ~3 px of its own on its
        # left, and the dot draws 1 px low in its box
        d = int(26 * s)
        p = int(pad * s)
        self.pill_dot = tk.Canvas(frame, width=d, height=d, highlightthickness=0, bd=0)
        self.pill_dot.pack(side="left", padx=(p, p - 3), pady=(p - 1, p + 1))
        self.pill_dot_size = d
        self.pill_title = ctk.CTkLabel(frame, text="", font=self.font(16, "bold"),
                                       text_color=TEXT, height=26, bg_color=CARD)
        # Segoe UI draws letters ~2 px below the middle of their box: lift them
        # so capitals and the dot share the pill's centre line
        # The right-end gap rides on whichever item is last (word, or timer when
        # shown); a separate spacer strip used to cut through the outline
        self.pill_end_gap = int(pad + 2)
        self.pill_title.pack(side="left", padx=(0, self.pill_end_gap),
                             pady=(0, PILL_TEXT_LIFT))
        self.pill_time = ctk.CTkLabel(frame, text="", font=self.font(12), text_color=MUTED,
                                      height=26, bg_color=CARD)   # packed while a job runs

        self.build_pill_menu()

        def bind_all(widget):
            """Give the window and every widget inside it the same mouse
            bindings, so the pill can be dragged or right-clicked anywhere on
            it."""
            widget.bind("<ButtonPress-1>", self._pill_press, add="+")
            widget.bind("<B1-Motion>", self._pill_drag, add="+")
            widget.bind("<Double-Button-1>", lambda e: self.leave_compact(), add="+")
            for button in RIGHT_CLICK:
                widget.bind(button, self.show_pill_menu, add="+")
            for child in widget.winfo_children():
                bind_all(child)
        bind_all(self.pill)
        self.root.bind("<Map>", self._root_mapped, add="+")   # restored from the taskbar
        self._pill_minimized = False

    def build_pill_menu(self):
        """Right-click menu (pill and tray icon), drawn in the app's colours
        (Windows' own menu is always white). Items are filled in when shown."""
        m = self.pill_menu = tk.Toplevel(self.root)
        m.withdraw()
        m.overrideredirect(True)
        m.attributes("-topmost", True)
        set_window_key(m)
        self.pill_menu_box = ctk.CTkFrame(m, corner_radius=12, fg_color=CARD, bg_color=KEY_COLOR,
                                          border_width=1, border_color=CARD_BORDER)
        self.pill_menu_box.pack()
        m.bind("<FocusOut>", lambda e: self.root.after(80, self._menu_focus_check))
        m.bind("<Escape>", lambda e: self.hide_pill_menu())

    def _mute_item(self):
        """The menu's Mute / Unmute item, depending on the current state."""
        return (("Unmute", self.unmute) if self.muted()
                else ("Mute for 1 hour", lambda: self.mute(3600)))

    def popup_menu(self, x, y, items, above=False):
        """Show the app's own popup menu at screen position (x, y). items:
        [(text, command), ...]. above=True opens it up and to the left of the
        point, which is where there's room next to the tray icon.

        On macOS it's a native menu instead: borderless Tk windows can't take
        keyboard focus there, so the click-outside-to-close check wouldn't
        work, and a Mac menu already follows Light / Dark."""
        if IS_MAC:
            menu = self._mac_menu = tk.Menu(self.root, tearoff=0)
            for text, command in items:
                menu.add_command(label=text, command=command)
            osys.activate_app()
            menu.tk_popup(int(x), int(y))
            return
        box = self.pill_menu_box
        for w in box.winfo_children():
            w.destroy()
        for n, (text, command) in enumerate(items):
            ctk.CTkButton(box, text=text, width=150, height=30, corner_radius=8, anchor="w",
                          fg_color="transparent", hover_color=CARD_BORDER, text_color=TEXT,
                          font=self.font(13),
                          command=lambda c=command: (self.hide_pill_menu(), c())).pack(
                padx=5, pady=(5 if n == 0 else 0, 5 if n == len(items) - 1 else 0))
        m = self.pill_menu
        m.update_idletasks()
        if above:                       # tray icon: open upwards from the taskbar
            y -= m.winfo_reqheight() + 4
            x -= m.winfo_reqwidth()
        m.geometry(f"+{int(x) + 2}+{int(y) + 2}")
        m.deiconify()
        m.lift()
        m.focus_force()

    def _check_items(self, x, y, above=False):
        """"Status is wrong…" while the detection check is on."""
        if not self.checklog.enabled:
            return []
        return [("Status is wrong…", lambda: self.root.after(
            10, lambda: self.ask_true_status(x, y, above)))]

    def show_pill_menu(self, e):
        """Right-click menu of the pill."""
        self.popup_menu(e.x_root, e.y_root, [("Expand", self.leave_compact),
                                             ("Minimize", self.minimize_pill),
                                             *self._check_items(e.x_root, e.y_root),
                                             self._mute_item(), ("Quit", self.quit)])

    def show_tray_menu(self, x, y):
        """Right-click menu of the tray icon (called on the UI thread via
        _on_ui)."""
        self.popup_menu(x, y, [("Show window", self.bring_to_front_full),
                               ("Compact pill", self.enter_compact),
                               *self._check_items(x, y, above=True),
                               self._mute_item(), ("Quit", self.quit)], above=True)

    # ------------------------------------------------------ detection check
    # The opt-in detection check (beacon/detectlog.py) measures how often the
    # status shown was wrong. The user reports a wrong status from the pill or
    # tray menu; these are the choices offered as "what it really was".
    TRUE_STATES = (("Running", "busy"), ("Paused", "paused"), ("Ready (idle)", "ready"),
                   ("Framing", "framing"), ("Disconnected", "disconnected"))

    def ask_true_status(self, x, y, above=False):
        """Ask what the laser is really doing, and log it as a wrong-status report."""
        shown = look(self.state)[1]
        items = [(f"Really: {label}", lambda v=value: self.report_wrong(v))
                 for label, value in self.TRUE_STATES if value != self.state]
        items.append((f"Cancel (shows {shown})", lambda: None))
        self.popup_menu(x, y, items, above=above)

    def report_wrong(self, truth):
        """Log that the status shown was wrong and what it really was."""
        self.checklog.report(truth)
        self.update_check_summary()
        if hasattr(self, "check_status"):
            self.check_status.configure(text="Report saved ✓  Thank you.")

    def _check_changed(self):
        """The "Log detection accuracy" switch was flipped."""
        self.checklog.enabled = bool(self.s["check_log"])
        if self.checklog.enabled:
            self.checklog.save()
        self.update_check_summary()

    def update_check_summary(self):
        """Refresh the detection check's counts shown in Settings (only touches
        the label when the text actually changed, as this runs often)."""
        if not hasattr(self, "check_summary"):
            return
        text = (self.checklog.summary_text() if self.checklog.enabled or
                self.checklog.summary["reads"] else "Off.")
        if self.check_summary.cget("text") != text:
            self.check_summary.configure(text=text)

    def report_wrong_from_settings(self):
        """The "Status is wrong…" button in Settings: open the question menu
        just under the button."""
        b = self.check_report_btn
        self.ask_true_status(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

    def export_check(self):
        """Save the detection check log, its pictures and some debug info as one
        zip file the user can send in."""
        path = filedialog.asksaveasfilename(
            parent=self.root, defaultextension=".zip",
            initialfile=f"{APP_NAME}-detection-check-{date.today():%Y%m%d}.zip",
            filetypes=[("Zip file", "*.zip")])
        if not path:
            return
        try:
            self.checklog.export(path, {"debug.txt": self.debug_info()})
        except OSError as e:
            self.check_status.configure(text=str(e))
            return
        self.check_status.configure(text=f"Saved. Email the zip to {SUPPORT_EMAIL}."
                                    if SUPPORT_EMAIL else "Saved. Send the zip to the seller.")

    def open_check_folder(self):
        """Open the detection check folder in Explorer."""
        os.makedirs(self.checklog.folder, exist_ok=True)
        open_path(self.checklog.folder)

    def reset_check(self):
        """Delete the detection check log after asking, and start counting
        again."""
        if messagebox.askyesno(APP_NAME, "Delete the detection check log and pictures, and "
                                         "start counting again?", parent=self.root):
            self.checklog.reset()
            self.update_check_summary()
            self.check_status.configure(text="Started again.")

    def hide_pill_menu(self):
        """Close the popup menu."""
        self.pill_menu.withdraw()

    def _menu_focus_check(self):
        # Clicking anywhere outside the menu takes focus away: close it
        try:
            focus = self.pill_menu.focus_displayof()
        except (tk.TclError, KeyError):
            focus = None
        if focus is None or focus.winfo_toplevel() is not self.pill_menu:
            self.hide_pill_menu()

    # ------------------------------------------------------------ actions
    # Pages built on first use: page name -> the method that builds it. Building
    # every page at startup took about 5 seconds; this way the window opens in
    # well under a second and each page costs its time only when first opened.
    LAZY_PAGES = {"settings": "build_settings_page", "relays": "build_relays_page",
                  "stats": "build_stats_page", "help": "build_help_page",
                  "wizard": "build_wizard_page", "channels": "build_subpages",
                  "safety": "build_subpages", "remote": "build_subpages"}

    def ensure_page(self, name):
        """Build a page the first time it's needed (keeps startup quick)."""
        if name == "wizard":
            self.ensure_page("settings")        # the wizard reuses Telegram's fields
        built = (name in self.subpages if name in ("channels", "safety", "remote")
                 else hasattr(self, f"{name}_page"))
        if not built and name in self.LAZY_PAGES:
            getattr(self, self.LAZY_PAGES[name])()

    def show_page(self, name):
        """Switch the main window to a page: build it if needed, hide all
        others, show it. Statistics is wider, so the window is resized when
        entering or leaving it, and its live refresh is started or stopped."""
        self.ensure_page(name)
        prev, self.page = self.page, name
        pages = {k: getattr(self, f"{k}_page") for k in
                 ("status", "settings", "relays", "stats", "wizard", "help")
                 if hasattr(self, f"{k}_page")}
        pages.update(self.subpages)
        settings = pages.pop("settings", None)      # parked, not packed: see park_settings
        for page in pages.values():
            page.pack_forget()
        if (name == "stats") != (prev == "stats"):
            self.resize_for_stats(name == "stats")
        if name == "settings":
            self.park_settings()
            settings.lift()
        else:
            pages[name].pack(fill="both", expand=True)
            if settings is not None:
                settings.lower()
        if name == "stats":
            self.stats.refresh(force=True)
        elif prev == "stats":
            self.stats.stop()

    def park_settings(self):
        """Settings stays laid out under the other pages instead of being
        unpacked: showing it is then just a raise (~55 ms) instead of mapping
        its ~600 windows again (~0.5 s). Its size is fixed, so dragging the
        window's edge on other pages doesn't lay the long page out at every
        step; it's brought to the window's size here, when it's shown."""
        w, h = self.root.winfo_width(), self.root.winfo_height()
        page = self.settings_page
        if page.winfo_manager() != "place":
            page.place(x=0, y=0, width=w, height=h)
            if self.page != "settings":
                page.lower()
        elif (page.winfo_width(), page.winfo_height()) != (w, h):
            page.place_configure(width=w, height=h)

    def _prebuild_settings(self):
        """Build Settings in the background after start-up, then park it (a
        separate step, so the window stays responsive in between). Started as
        the pill, the window isn't shown yet: it's parked when first opened."""
        self.ensure_page("settings")
        self.root.after(300, lambda: self.root.winfo_viewable() and self.park_settings())

    def _root_resized(self, event):
        """The window was resized while Settings shows: resize the page once
        the dragging pauses. Laying the long page out at every step of a
        drag took ~0.5 s each; meanwhile the page just keeps its size (the
        strip it leaves is the same colour as the page)."""
        if event.widget is self.root and self.page == "settings":
            if getattr(self, "_resize_job", None):
                self.root.after_cancel(self._resize_job)
            self._resize_job = self.root.after(150, self._resize_settings)

    def _resize_settings(self):
        """The resize paused: fit the page to the window."""
        self._resize_job = None
        if self.page == "settings":
            self.park_settings()

    def choose_chime(self):
        """Let the user pick a sound file for the job-finished chime. The file
        is copied into SOUNDS_DIR, test-played (if Windows can't play it, the
        old sound is kept) and switched on."""
        path = filedialog.askopenfilename(parent=self.root, title="Choose a chime sound",
                                          filetypes=SOUND_TYPES)
        if not path:
            return
        # Keep our own copy, so the chime still works if the original moves
        dest = os.path.join(SOUNDS_DIR, os.path.basename(path))
        try:
            os.makedirs(SOUNDS_DIR, exist_ok=True)
            stop_sound()
            if os.path.normcase(os.path.abspath(path)) != os.path.normcase(dest):
                shutil.copy2(path, dest)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Couldn't copy the sound:\n{e}", parent=self.root)
            return
        if not CHIME.check(dest):       # plays it once as a preview, too
            messagebox.showerror(APP_NAME, f"{OS_NAME} can't play that file. Try an MP3 or WAV.",
                                 parent=self.root)
            self._remove_sounds(keep=self.s["chime_file"])
            return
        self._remove_sounds(keep=dest)
        self.s["chime_file"] = dest
        self.s["chime"] = True          # picking a sound means you want to hear it
        self.chime_var.set(True)
        save_settings(self.s)
        self.show_chime_name()

    def reset_chime(self):
        """The ✕ button: forget the chosen sound and go back to the Windows
        chime."""
        stop_sound()
        self._remove_sounds(keep=None)
        self.s["chime_file"] = ""
        save_settings(self.s)
        self.show_chime_name()

    def _remove_sounds(self, keep):
        """Delete old copies in SOUNDS_DIR except `keep`, so only the sound in
        use stays on disk."""
        try:
            for name in os.listdir(SOUNDS_DIR):
                f = os.path.join(SOUNDS_DIR, name)
                if not keep or os.path.normcase(f) != os.path.normcase(keep):
                    os.remove(f)
        except OSError:
            pass

    def show_chime_name(self):
        """Show which sound is chosen under the chime row."""
        f = self.s["chime_file"]
        custom = bool(f) and os.path.exists(f)
        self.chime_name.configure(text="\u266a  " + os.path.basename(f) if custom
                                  else f"{OS_NAME} default chime  \u00b7  choose an MP3 to use your own")
        self.chime_reset_btn.configure(state="normal" if custom else "disabled")

    # ------------------------------------------------------- notifications
    def _set_text_setting(self, key, var):
        """Save a text box's contents (trimmed) as setting `key`."""
        self.s[key] = var.get().strip()
        save_settings(self.s)

    def _set_flag(self, key, var):
        """Save a checkbox as a boolean setting."""
        self.s[key] = bool(var.get())
        save_settings(self.s)

    def _on_ui(self, fn):
        """Run fn on the window's thread (background threads can't touch widgets)."""
        # The detector's queue doubles as a "please run this" queue: poll() runs
        # any {"cmd": "call"} message on the UI thread. Every background thread
        # (Telegram, pairing, dashboard), and the tray icon's window procedure,
        # use this to update the UI.
        self.q.put({"cmd": "call", "fn": fn})

    def _tg_status(self, text, ok=True):
        """Show a line under the Telegram settings, from any thread."""
        self._on_ui(lambda: self.tg_status.configure(text=text, text_color=MUTED if ok else RED))

    def _pc_notify_changed(self):
        """Windows notifications switched on: show one straight away, so the
        user sees what they look like and Windows asks any permission now."""
        if self.s["notify_pc"]:
            self.tray.show(APP_NAME, "Notifications are on. You'll get a pop-up here when a "
                                     "job finishes.")

    def tg_find_chat(self):
        """The Telegram "Find" button: fill in the Chat ID automatically.

        A bot can't message someone first; once you message it, Telegram
        keeps that message for the bot to fetch with getUpdates. The chat id
        of the latest message is the chat to send notifications to. Runs on a
        thread so the window doesn't freeze while Telegram answers."""
        token = self.s["tg_token"]
        if not token:
            self.tg_status.configure(text="Paste your bot token first.", text_color=RED)
            return
        self.tg_status.configure(text="Asking Telegram…", text_color=MUTED)

        def work():
            """Ask Telegram for the bot's name and its recent messages, then
            fill in the chat (on the UI thread)."""
            try:
                bot = telegram_call(token, "getMe")
                updates = telegram_call(token, "getUpdates", {"limit": 50, "timeout": 0})
            except RuntimeError as e:
                return self._tg_status(str(e), ok=False)
            chats = [(u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {})
                     .get("chat") for u in updates or []]
            chats = [c for c in chats if c]
            if not chats:
                return self._tg_status(f"Connected to @{bot.get('username')}, but it has no "
                                       "messages yet. Send it a message in Telegram, then "
                                       "press Find again.", ok=False)
            chat = chats[-1]
            name = chat.get("title") or " ".join(
                p for p in (chat.get("first_name"), chat.get("last_name")) if p) or "your chat"

            def fill():
                self.tg_chat_var.set(str(chat["id"]))
                self.tg_status.configure(text=f"Found {name}. Connected to "
                                              f"@{bot.get('username')}. Press Send test.",
                                         text_color=MUTED)
            self._on_ui(fill)
        threading.Thread(target=work, daemon=True).start()

    def send_test_notification(self):
        """The "Send test" button: one test message to Windows and/or Telegram,
        with the result shown under the button."""
        sent = []
        if self.s["notify_pc"]:
            self.tray.show(f"\U0001F514 {APP_NAME} test", f"{OS_NAME} notifications are working.")
            sent.append(OS_NAME)
        token, chat = self.s["tg_token"], self.s["tg_chat"]
        if token and chat:
            self.tg_status.configure(text="Sending…", text_color=MUTED)

            def work():
                try:
                    telegram_call(token, "sendMessage", {
                        "chat_id": chat,
                        "text": f"\U0001F514 {APP_NAME} test\nTelegram notifications are working."})
                    self._tg_status("Test sent to Telegram ✓"
                                    + (f" and {OS_NAME}" if sent else ""))
                except RuntimeError as e:
                    self._tg_status(str(e), ok=False)
            threading.Thread(target=work, daemon=True).start()
        elif sent:
            self.tg_status.configure(text=f"{OS_NAME} test sent. Add a bot token and chat ID "
                                          "for Telegram.", text_color=MUTED)
        else:
            self.tg_status.configure(text=f"Turn on {OS_NAME} notifications or set up Telegram "
                                          "first.", text_color=RED)

    def notify(self, event, title, body, photo=False):
        """Send one event to every channel that's on. Ticked events only, except
        safety alarms, which always go (their sensor being on is the opt-in)."""
        # Order of the checks: is this event ticked in Settings? (CRITICAL ones
        # skip mute and quiet hours) -> flood guard (not for safety alarms) ->
        # deliver. Every alert is logged so problems can be traced afterwards.
        critical = event in CRITICAL_EVENTS
        if event in dict(NOTIFY_EVENTS) and not self.s.get(f"notify_{event}"):
            return
        if not critical and (self.muted() or self.in_quiet_hours()):
            return
        if event not in SAFETY_EVENTS:
            flood = self.flood_check(event)
            if flood is None:
                return
            if flood:
                title, body, photo = flood[0], flood[1], False
        if self.test_mode:
            title += "  " + "(test)"
        logging.info("notify %s: %s", event, title)
        self.deliver(title, body, critical=critical, photo=photo)

    # More than FLOOD_COUNT of one alert within FLOOD_WINDOW seconds pauses that
    # alert for FLOOD_PAUSE seconds (a flickering USB cable once sent dozens of
    # "Job finished" messages in a few minutes).
    FLOOD_COUNT, FLOOD_WINDOW, FLOOD_PAUSE = 3, 60.0, 600.0

    def flood_check(self, event):
        """Stops a flickering status from sending a message every few seconds.
        Returns None to drop this alert, (title, body) to send a one-off
        warning instead, or () to send it as normal."""
        now = time.time()
        sent = self._alerts_sent.setdefault(event, [])
        sent[:] = [t for t in sent if now - t < self.FLOOD_WINDOW]
        if now < self._alerts_paused.get(event, 0):
            logging.info("notify %s: held back (too many in a row)", event)
            return None
        sent.append(now)
        if len(sent) <= self.FLOOD_COUNT:
            return ()
        self._alerts_paused[event] = now + self.FLOOD_PAUSE
        name = dict(NOTIFY_EVENTS).get(event, event)
        return ("⚠ " + "Status is changing very quickly",
                f"{len(sent)} “{name}” alerts in a minute: LightBurn or the laser "
                f"connection may be unstable. These alerts are paused for "
                f"{int(self.FLOOD_PAUSE // 60)} minutes.")

    # ----------------------------------------------------- delivery & mute
    def muted(self):
        """True while "Mute for 1 hour" is in effect."""
        return time.time() < float(self.s.get("mute_until") or 0)

    def mute(self, seconds):
        """Hold back non-critical alerts for `seconds` (saved, so it survives a
        restart)."""
        self.s["mute_until"] = time.time() + seconds
        save_settings(self.s)

    def unmute(self):
        """End a mute now."""
        self.s["mute_until"] = 0
        save_settings(self.s)

    def in_quiet_hours(self, now=None):
        """True during the quiet hours set in Notifications. Handles ranges that
        cross midnight (22:00 to 07:00)."""
        if not self.s["quiet_on"]:
            return False
        h = (now or datetime.now()).hour
        a, b = int(self.s["quiet_start"]), int(self.s["quiet_end"])
        return (a <= h < b) if a < b else (h >= a or h < b)

    def deliver(self, title, body, critical=False, report=False, photo=False, status=None):
        """Send to every channel that's set up. Reports follow the report
        channel settings; SMS only gets alerts unless set otherwise.
        Returns the list of channel names it went to."""
        s = self.s
        where = []
        if (s["report_pc"] if report else s["notify_pc"]):
            self.tray.show(title, body)
            where.append(OS_NAME)
        # Each channel that's set up becomes a (name, send function) job. The
        # sends happen on one background thread (network calls can take
        # seconds), one after the other; a failing channel doesn't stop the
        # others.
        jobs = []
        if (s["report_tg"] if report else True) and s["tg_token"] and s["tg_chat"]:
            where.append("Telegram")
            jobs.append(("Telegram", lambda jpg: (
                channels.telegram_photo(s["tg_token"], s["tg_chat"], f"{title}\n{body}", jpg)
                if jpg else telegram_call(s["tg_token"], "sendMessage",
                                          {"chat_id": s["tg_chat"], "text": f"{title}\n{body}"}))))
        others = (not report) or s["report_other"]
        if others and s["discord_on"] and s["discord_url"]:
            where.append("Discord")
            jobs.append(("Discord", lambda jpg: channels.discord_send(s["discord_url"], title,
                                                                      body, jpg)))
        if others and s["pushover_on"] and s["pushover_token"] and s["pushover_user"]:
            where.append("Pushover")
            jobs.append(("Pushover", lambda jpg: channels.pushover_send(
                s["pushover_token"], s["pushover_user"], title, body, 1 if critical else 0)))
        if others and s["email_on"] and s["email_host"] and s["email_to"]:
            where.append("Email")
            jobs.append(("Email", lambda jpg: channels.email_send(
                s["email_host"], s["email_port"], s["email_user"], s["email_pass"], s["email_to"],
                f"{APP_NAME}: {title}", body, jpg)))
        if (s["sms_on"] and not report and (critical or not s["sms_alerts_only"])):
            where.append("SMS")
            jobs.append(("SMS", lambda jpg: channels.twilio_send(
                s["twilio_sid"], s["twilio_token"], s["twilio_from"], s["sms_to"],
                f"{title}\n{body}")))
        if not jobs:
            return where
        want_photo = photo and s["snapshot_on"] and camera.available()

        def work():
            jpg = None
            if want_photo:
                try:
                    jpg = camera.snapshot(s["camera_index"])
                except RuntimeError as e:
                    logging.warning("snapshot failed: %s", e)
            failed = []
            for name, send in jobs:
                try:
                    send(jpg if name in ("Telegram", "Discord", "Email") else None)
                except RuntimeError as e:
                    logging.warning("%s failed: %s", name, e)
                    failed.append(f"{name}: {e}")
            if status is not None:
                text = ("Sent to" + " " + ", ".join(where) + " ✓") if not failed \
                    else "Not sent" + " — " + "; ".join(failed)
                self._on_ui(lambda: status.configure(text=text, text_color=RED if failed else MUTED))
            elif failed:
                self._tg_status("Last message failed" + ": " + "; ".join(failed), ok=False)
        threading.Thread(target=work, daemon=True).start()
        return where

    # -------------------------------------------------------- usage reports
    # How reports are timed: every report kind has "slots" (the top of each
    # hour, each day at the chosen hour, each week on the chosen day and hour).
    # settings["report_last"] remembers the last slot sent per kind. Every 20 s
    # check_reports() looks at the latest slot; if it hasn't been sent, the
    # report for the period ending at that slot is built and sent.
    def report_slots(self, now):
        """{kind: datetime the most recent report of that kind was due} for the
        reports that are switched on."""
        s, out = self.s, {}
        if s["report_hourly"]:
            out["hourly"] = now.replace(minute=0, second=0, microsecond=0)
        if s["report_daily"]:
            t = now.replace(hour=int(s["report_daily_hour"]), minute=0, second=0, microsecond=0)
            out["daily"] = t if t <= now else t - timedelta(days=1)
        if s["report_weekly"]:
            t = now.replace(hour=int(s["report_weekly_hour"]), minute=0, second=0, microsecond=0)
            t -= timedelta(days=(now.weekday() - int(s["report_weekly_day"])) % 7)
            out["weekly"] = t if t <= now else t - timedelta(days=7)
        return out

    def _mark_reports_sent(self, kinds=None):
        """Treat the current slots as sent, so switching a report on (or changing
        its time) doesn't fire an old report straight away."""
        last = dict(self.s.get("report_last") or {})
        for kind, end in self.report_slots(datetime.now()).items():
            if kinds is None and kind in last:
                continue
            if kinds is None or kind in kinds:
                last[kind] = end.timestamp()
        self.s["report_last"] = last
        save_settings(self.s)

    def check_reports(self):
        """Timer: look for reports that are due, then check again in 20 s."""
        try:
            self._check_reports(datetime.now())
        finally:
            self.root.after(20000, self.check_reports)

    def _check_reports(self, now):
        """Send every report whose latest slot hasn't been sent yet."""
        last = dict(self.s.get("report_last") or {})
        changed = False
        for kind, end in self.report_slots(now).items():
            ts = end.timestamp()
            if last.get(kind, 0) >= ts:
                continue
            last[kind], changed = ts, True
            # An hourly report is only useful on time: don't catch up old hours.
            # A missed daily/weekly report (PC was off) is sent once on return.
            if kind == "hourly" and now - end > timedelta(minutes=10):
                continue
            title, body, run = build_report(kind, end, self.runlog, self.s["costs"])
            if run <= 0 and self.s["report_skip_empty"]:
                continue
            self.send_report(title, body)
        if changed:
            self.s["report_last"] = last
            save_settings(self.s)

    def send_report(self, title, body, status=None):
        """Sends a report to the chosen places. Returns where it went."""
        if self.s["goal_hours"] and "Weekly report" in title:
            body += "\n" + self.goal_line()
        return self.deliver(title, body, report=True, status=status)

    def _report_toggled(self, kind, var):
        """A report switch was flipped; switching on starts from the next slot."""
        self.s[f"report_{kind}"] = bool(var.get())
        save_settings(self.s)
        if var.get():
            self._mark_reports_sent([kind])

    def _set_report_time(self, key, value, kind):
        """A report's day or hour was changed; start from the next slot."""
        self.s[key] = value
        save_settings(self.s)
        self._mark_reports_sent([kind])

    def send_report_now(self):
        """The "Send a report now" button: a daily report for the last 24 hours,
        so people can see what reports look like."""
        title, body, _ = build_report("daily", datetime.now(), self.runlog, self.s["costs"])
        where = self.send_report(title, body, status=self.report_status)
        if not where:
            self.report_status.configure(text="Choose where to send reports, and set up Telegram "
                                              "above if you want them there.", text_color=RED)
        elif where == [OS_NAME]:
            self.report_status.configure(text=f"Report shown as a {OS_NAME} notification ✓",
                                         text_color=MUTED)
        else:
            self.report_status.configure(text="Sending…", text_color=MUTED)

    def _job_label(self):
        """A second line naming the file and customer for job messages, or ""."""
        bits = [b for b in (self.file_name, self.s["client"]) if b]
        return ("\n" + " · ".join(bits)) if bits else ""

    def _notify_change(self, old, new):
        """Turn a status change into the right notification: started, paused,
        finished (with run time, cost and today's total) or lost
        (disconnected mid-job). Called by set_state when a change is final."""
        job = ("busy", "paused")
        if new == "busy" and old not in job:
            self.notify("started", "▶ " + "Job started",
                        "The laser is running." + self._job_label())
        elif old == "busy" and new == "paused":
            self.notify("paused", "⏸ " + "Job paused",
                        "The job is waiting for you." + self._job_label())
        elif old in job and new in ("ready", "framing"):
            run = (self.runlog.jobs[-1]["run"] if not self.test_mode and self.runlog.jobs
                   else (self.last_job or 0))
            body = f"Run time {fmt_human(run)}"
            rate = cost_per_hour(self.s["costs"])
            if rate > 0:
                body += f"  ·  cost {self.s['costs']['currency']}{run * rate / 3600:,.2f}"
            body += (f"\nToday so far: "
                     f"{fmt_human(self.runlog.run_on(date.today(), time.time()))}")
            body += self._job_label()
            self.notify("finished", "✅ " + "Job finished", body, photo=True)
        elif old in job and new == "disconnected":
            self.notify("lost", "⚠ " + "Laser disconnected",
                        "LightBurn lost the laser during a job." + self._job_label())

    def set_appearance(self, value):
        """Switch between System / Light / Dark. CustomTkinter widgets follow by
        themselves; the orb and pill are hand-drawn, so they're told to
        redraw."""
        self.s["appearance"] = value
        save_settings(self.s)
        ctk.set_appearance_mode(value.lower())
        self._orb_key = self._pill_key = None

    def set_interval(self, value):
        """The "Check every" buttons ("0.5 s" etc.). The running detector picks
        up the new interval on its next loop."""
        self.s["interval"] = float(value.replace(" s", ""))
        self.detector.interval = self.s["interval"]
        save_settings(self.s)

    # ------------------------------------------------- how LightBurn is read
    # How LightBurn is read: saved key -> button label in Settings › Detection.
    # - ocr: screen reading (Detector), any LightBurn version
    # - api: LightBurn's REST API (ApiDetector), 2.2+, works when minimised
    # - both: HybridDetector, the screen when visible plus the API
    DETECT_MODES = {"ocr": "Screen", "api": "API (2.2+)", "both": "Both"}

    def make_detector(self):
        """A new (not yet started) detector for the chosen mode. Without OCR,
        Both falls back to API. on_trust saves settings when the API reader
        learns a trustworthy device; it's posted to the UI thread, since the
        detector thread mustn't write settings while the UI might be."""
        mode = self.s["detect_mode"]
        if mode == "both" and self.ocr is None:
            mode = "api"
        trust = dict(on_trust=lambda: self._on_ui(lambda: save_settings(self.s)))
        if mode == "api":
            return ApiDetector(self.q, self.s["interval"], self.s["lb_api_secret"],
                               self.s["lb_api_trusted"], **trust)
        if mode == "both":
            return HybridDetector(self.ocr, self.q, self.s["interval"],
                                  self.s["lb_api_secret"], self.s["lb_api_trusted"], **trust)
        return Detector(self.ocr, self.q, self.s["interval"])

    def restart_detector(self):
        """Swap to a fresh reader (new mode or new API key) without a restart."""
        self.detector.stop_evt.set()
        self.detector = self.make_detector()
        self.detector.start()
        self.unreadable_since = None
        self.sub_lbl.configure(text="Looking for LightBurn…")

    def set_detect_mode(self, label):
        """A Detection mode button was pressed: save it and switch readers at
        once. Choosing API or Both for the first time starts pairing."""
        mode = next(k for k, v in self.DETECT_MODES.items() if v == label)
        if mode in ("ocr", "both") and self.ocr is None:
            self.detect_seg.set(self.DETECT_MODES["api"])
            self.api_status.configure(text="Screen reading isn't available on this PC "
                                           f"({OS_NAME} text recognition didn't start).",
                                      text_color=RED)
            return
        if mode == self.s["detect_mode"]:
            return
        self.s["detect_mode"] = mode
        save_settings(self.s)
        self.restart_detector()
        self.show_api_status()
        if mode in ("api", "both") and not self.s["lb_api_secret"]:
            self.connect_api()              # first time: pair straight away

    def connect_api(self):
        """Pair with LightBurn's API (the Connect button).

        LightBurn shows its own "Allow Beacon?" pop-up; when the user clicks
        Allow, LightBurn returns a secret that Beacon keeps in settings and
        uses to sign every request (see beacon/lbapi.py). Pairing waits up to
        about 30 seconds for that click, so it runs on a thread and reports
        back through _on_ui."""
        if getattr(self, "_pairing", False):
            return
        self._pairing = True
        self.api_btn.configure(state="disabled")
        self.api_status.configure(text="Click Allow in the pop-up that LightBurn shows now "
                                       "(within 30 seconds)…", text_color=AMBER)

        def work():
            try:
                if not lbapi.available():
                    raise lbapi.ApiError("LightBurn's API isn't answering. Open LightBurn 2.2 "
                                         "or newer and try again.")
                secret = lbapi.pair(APP_NAME)
            except lbapi.ApiError as e:
                self._on_ui(lambda m=str(e): self._paired(None, m))
                return
            self._on_ui(lambda: self._paired(secret, ""))
        threading.Thread(target=work, daemon=True).start()

    def _paired(self, secret, error):
        """Pairing finished (on the UI thread): save the secret and restart the
        reader so it uses it, or show why it failed."""
        self._pairing = False
        self.api_btn.configure(state="normal")
        if not secret:
            self.api_status.configure(text=error, text_color=RED)
            return
        self.s["lb_api_secret"] = secret
        save_settings(self.s)
        if self.s["detect_mode"] in ("api", "both"):
            self.restart_detector()
        self.api_status.configure(text="✓ Connected to LightBurn.", text_color=MUTED)
        self.root.after(1500, self.show_api_status)

    def forget_api(self):
        """The Forget button: drop the pairing secret."""
        self.s["lb_api_secret"] = ""
        save_settings(self.s)
        if self.s["detect_mode"] in ("api", "both"):
            self.restart_detector()
        self.show_api_status()

    def show_api_status(self):
        """The Connect button's label and the line under it."""
        if not hasattr(self, "api_btn"):
            return
        paired = bool(self.s["lb_api_secret"])
        self.api_btn.configure(text="Connect again" if paired else "Connect")
        self.api_forget_btn.configure(state="normal" if paired else "disabled")
        if getattr(self, "_pairing", False):
            return
        if not paired:
            text, color = ("Not connected. Press Connect, then click Allow in LightBurn's "
                           "pop-up."), MUTED
        elif self.s["detect_mode"] == "ocr":
            text, color = "Connected. Choose API or Both above to use it.", MUTED
        elif self.s["detect_mode"] == "both" and isinstance(self.detector, HybridDetector):
            api_ok = self.detector.api.shown is not None
            if self.detector.source == "screen":
                text = ("✓ Reading the screen and LightBurn's API" if api_ok else
                        "Reading the screen. LightBurn's API isn't answering: "
                        + (self.detector._api_read[1] or "waiting…"))
                color = MUTED if api_ok else AMBER
            elif api_ok:
                text, color = ("✓ LightBurn is minimised or hidden: reading its API "
                               "only"), MUTED
            else:
                text, color = self.last_msg.get("note") or "Waiting for LightBurn…", AMBER
        else:
            msg = self.last_msg if self.last_msg.get("mode") in ("api", "api-error", "none") \
                else {}
            if msg.get("mode") == "api":
                dev = next((l.split(": ", 1)[1] for l in msg.get("lines", [])
                            if l.startswith("device: ")), "")
                text, color = "✓ Reading LightBurn's API" + (f" · {dev}" if dev else ""), MUTED
            elif msg:
                text, color = msg.get("note") or "Waiting for LightBurn…", AMBER
            else:
                text, color = "Waiting for LightBurn…", MUTED
        if self.api_status.cget("text") != text:
            self.api_status.configure(text=text, text_color=color)

    def refresh_ports(self):
        """Fill the Port dropdown with the serial ports there are now (the ↻
        button). The saved port stays listed even if it's unplugged."""
        ports = list_ports()
        values = ["Auto-detect"] + [dev for dev, _ in ports]
        if self.s["port"] not in values:
            values.append(self.s["port"])
        self.port_box.configure(values=values)
        self.port_box.set(self.s["port"])

    def set_port(self, value):
        """A port was picked; the Arduino thread switches to it on its next
        loop."""
        self.s["port"] = value
        self.arduino.port = value
        save_settings(self.s)

    # ---------------------------------------------------------- test mode
    def set_test(self, on):
        """Switch test mode on or off. On: the detector's messages are ignored
        and statuses come from the test buttons. Off: go straight back to
        what the detector last reported."""
        self._cancel_demo()
        self.test_mode = bool(on)
        self.test_var.set(self.test_mode)
        self.runlog.observe("test" if self.test_mode else self.state)
        if not self.test_mode:
            self.set_state(self.last_msg.get("shown"))
        self.update_test_ui()

    def simulate(self, status):
        """A test button: show this status as if LightBurn reported it."""
        self._cancel_demo()
        self.test_mode = True
        self.test_var.set(True)
        self.set_state(status)
        self.update_test_ui()

    def toggle_demo_job(self):
        """Start a 10-second test job (Running, then Ready by itself), or end it
        early if one is running. Exercises the light, chime and
        notifications."""
        if self.demo_after:             # stop early = job finished
            self._cancel_demo()
            self.set_state("ready")
            self.update_test_ui()
            return
        self.simulate("busy")
        self.job_start = time.time()
        self.demo_end = time.time() + 10
        self.demo_after = self.root.after(10000, self.toggle_demo_job)

    def _cancel_demo(self):
        """Stop a running test job's timer."""
        if self.demo_after:
            self.root.after_cancel(self.demo_after)
        self.demo_after = self.demo_end = None

    def update_test_ui(self):
        """Highlight the test button of the current status and show or hide the
        TEST MODE banner."""
        for st, b in self.test_buttons.items():
            if self.test_mode and st == self.state:
                b.configure(fg_color=look(st)[0], hover_color=look(st)[0], text_color="#FFFFFF")
            else:
                b.configure(fg_color=CARD_BORDER, hover_color=("#D1D5DB", "#2E3340"),
                            text_color=TEXT)
        if self.test_mode:
            self.test_banner.pack(fill="x", padx=18, pady=(10, 0), before=self.card)
        else:
            self.test_banner.pack_forget()
        self._outline_key = None        # restyle the outlines on the next render

    # ------------------------------------------------------------- relays
    def rebuild_relay_panel(self):
        """Rebuild the relay buttons on the Home page (two per row), or an "Add
        relay buttons" link when there are none, and make the window tall
        enough for them."""
        for w in self.relay_panel.winfo_children():
            w.destroy()
        self.relay_buttons, self._relay_keys = {}, {}
        used = [i for i, r in enumerate(self.s["relays"]) if r["use"]]
        if not used:
            ctk.CTkButton(self.relay_panel, text="+  Add relay buttons", height=30,
                          corner_radius=10, fg_color="transparent", hover_color=CARD_BORDER,
                          text_color=MUTED, font=self.font(12),
                          command=lambda: self.open_relays("status")).pack(fill="x")
            extra = 38
        else:
            cols = 2
            for n, i in enumerate(used):
                b = ctk.CTkButton(self.relay_panel, height=46, corner_radius=12, border_width=1,
                                  font=self.font(12, "bold"),
                                  command=lambda i=i: self.toggle_relay(i))
                b.grid(row=n // cols, column=n % cols, padx=3, pady=3, sticky="ew")
                for button in RIGHT_CLICK:
                    b.bind(button, lambda e, i=i: self.rename_relay(i))
                self.relay_buttons[i] = b
            for col in range(cols):
                self.relay_panel.grid_columnconfigure(col, weight=1, uniform="relay")
            extra = ((len(used) + cols - 1) // cols) * 52
        self.update_relays()
        # Grow the window so the orb keeps its room
        self._min_h = 500 + extra
        if self.page == "stats":
            return
        self.root.minsize(340, self._min_h)
        m = re.match(r"(\d+)x(\d+)", self.root.geometry())
        w, h = (int(m.group(1)), int(m.group(2))) if m else (380, 560)
        if h < 560 + extra:
            self.root.geometry(f"{max(w, 380)}x{560 + extra}")

    def set_relay(self, i, key, value):
        """Change one setting of relay i and save. Adding or removing a relay
        rebuilds both relay screens; anything else just re-evaluates the
        relays."""
        self.s["relays"][i][key] = value
        save_settings(self.s)
        if key == "use":
            self.rebuild_relay_panel()
            self.rebuild_relays_page()
        else:
            self.update_relays()

    def toggle_relay(self, i):
        """A relay button on the Home page was pressed: flip it by hand. The
        override lasts until the laser's status or the automatic rule changes
        (see update_relays)."""
        self.relay_override[i] = not self.relay_on[i]
        self.update_relays()

    def rename_relay(self, i):
        """Right-click a relay button: ask for a new name. Setting the name
        variable saves it through its trace (see rebuild_relays_page)."""
        d = ctk.CTkInputDialog(text=f"New name for the pin {RELAY_PINS[i]} button:",
                               title="Rename button")
        d.attributes("-topmost", True)
        name = d.get_input()
        if name and name.strip():
            self.relay_name_vars[i].set(name.strip()[:24])

    @staticmethod
    def schedule_active(r, now_dt):
        """True if relay settings r have their schedule covering this moment
        (day type and hour range, which may cross midnight)."""
        days = r.get("sched_days", "every")
        wd = now_dt.weekday()
        if (days == "weekdays" and wd >= 5) or (days == "weekends" and wd < 5):
            return False
        a, b = int(r.get("sched_start", 8)), int(r.get("sched_end", 17))
        return (a <= now_dt.hour < b) if a <= b else (now_dt.hour >= a or now_dt.hour < b)

    def update_relays(self):
        """Work out whether every relay should be on right now, and send that
        out. Runs on every status change and every UI tick (so delays and
        keep-on times expire on time).

        For each relay, in order:
        1. wanted: the current status is one its AUTO_MODES rule lists
        2. on_delay: wanted must have lasted this long before it counts
        3. keep: it stays on this long after the rule last wanted it
        4. schedule: forces it on during its hours
        5. a manual press overrides all that, until something above changes
        6. safety: the relay chosen to cut during a fire alarm is forced off
        Arduino relays become bits of one mask sent to both light links;
        smart plugs / Home Assistant switches go to the PlugWorker thread."""
        now = time.time()
        now_dt = datetime.now()
        mask = 0
        plugs = {}
        status_changed = self.state != getattr(self, "_relay_state", None)
        self._relay_state = self.state
        for i, r in enumerate(self.s["relays"]):
            statuses = AUTO_MODES.get(r["auto"], AUTO_MODES["manual"])[1]
            wanted = self.state in statuses
            # "Switches on after N s": the rule must hold that long first
            if wanted:
                if self.relay_cond_since[i] is None:
                    self.relay_cond_since[i] = now
                if now - self.relay_cond_since[i] >= r.get("on_delay", 0):
                    self.relay_last[i] = now
            else:
                self.relay_cond_since[i] = None
            auto = bool(statuses) and now - self.relay_last[i] <= r["keep"] and (
                self.relay_last[i] > 0)
            if r.get("sched_on") and self.schedule_active(r, now_dt):
                auto = True
            # A manual press on an automatic relay lasts until the laser's
            # status (or the rule's answer) changes
            if auto != self.relay_auto[i] or (statuses and status_changed):
                self.relay_override[i] = None
            self.relay_auto[i] = auto
            on = bool(r["use"]) and (auto if self.relay_override[i] is None
                                     else self.relay_override[i])
            if self.fire_active and i == int(self.s["fire_relay_off"]):
                on = False                  # safety: e.g. air assist off during a fire alarm
            self.relay_on[i] = on
            target = r.get("target", "arduino")
            if target == "arduino":
                if on:
                    mask |= 1 << i
            elif r["use"]:
                plugs[i] = (target, r.get("address", ""), r.get("entity", ""), on)
        for link in (self.arduino, self.netlight):
            if mask != link.relays:
                link.relays = mask
                link.wake.set()
        self.plugs.set_wanted(plugs, (self.s["ha_url"], self.s["ha_token"]))
        self.refresh_relay_buttons()

    def refresh_relay_buttons(self):
        """Update each Home page relay button's text and colour (green = on).
        Only buttons that actually changed are reconfigured."""
        for i, b in self.relay_buttons.items():
            r = self.s["relays"][i]
            on = self.relay_on[i]
            auto = ((r["auto"] != "manual" or r.get("sched_on"))
                    and self.relay_override[i] is None)
            text = (r["name"] + "\n" + ("On" if on else "Off")
                    + ("  ·  " + "auto" if auto else ""))
            if self._relay_keys.get(i) == (text, on):
                continue
            self._relay_keys[i] = (text, on)
            if on:
                b.configure(text=text, fg_color=GREEN, border_color=GREEN, text_color="#FFFFFF",
                            hover_color=blend(GREEN, "#000000", 0.12))
            else:
                b.configure(text=text, fg_color=CARD, border_color=CARD_BORDER, text_color=TEXT,
                            hover_color=CARD_BORDER)

    def build_stats_page(self):
        """Build the Statistics page: a header, then everything else is the
        StatsView class (further down in this file)."""
        p = self.stats_page = ctk.CTkFrame(self.root, fg_color="transparent")
        head = ctk.CTkFrame(p, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(16, 0))
        ctk.CTkButton(head, text="←  Back", width=80, height=32, corner_radius=10,
                      fg_color="transparent", hover_color=CARD_BORDER, text_color=ACCENT,
                      font=self.font(13, "bold"),
                      command=lambda: self.show_page("status")).pack(side="left")
        ctk.CTkLabel(head, text="Statistics", font=self.font(15, "bold"),
                     text_color=TEXT).pack(side="right", padx=4)
        self.stats = StatsView(self, p)

    def resize_for_stats(self, entering):
        """Statistics needs room for its charts: widen the window while it's
        shown and give the small size back afterwards (both are remembered)."""
        m = re.match(r"(\d+)x(\d+)", self.root.geometry())
        size = f"{m.group(1)}x{m.group(2)}" if m else None
        if entering:
            self._small_size = size
            self.root.minsize(760, 580)
            self.root.geometry(self._stats_size or "960x720")
            self.root.update_idletasks()
            # Keep it on screen if the small window sat near the right edge
            x, y = self.root.winfo_x(), self.root.winfo_y()
            over = x + self.root.winfo_width() - self.root.winfo_screenwidth()
            if over > 0:
                self.root.wm_geometry(f"+{max(0, x - over - 10)}+{y}")
        else:
            self._stats_size = size
            self.root.minsize(340, self._min_h)
            self.root.geometry(self._small_size or "380x560")

    def enter_compact(self):
        """Switch to the compact pill: hide the main window and show the pill
        where it was last left (or over the window the first time)."""
        self.s["compact"] = True
        save_settings(self.s)
        pos = self.s.get("compact_pos")
        if pos:
            self.pill.geometry(f"+{pos[0]}+{pos[1]}")
        else:
            x = self.root.winfo_rootx()
            y = self.root.winfo_rooty()
            self.pill.geometry(f"+{x}+{y}")
        self.root.withdraw()
        self.pill.deiconify()
        self._pill_key = None

    def leave_compact(self):
        """Back from the pill to the full window (double-click or Expand),
        remembering where the pill was."""
        self.s["compact"] = False
        self.s["compact_pos"] = [self.pill.winfo_x(), self.pill.winfo_y()]
        save_settings(self.s)
        self.reveal_window()        # the pill stays up until the window is ready

    def reveal_window(self):
        """Bring the main window back fully drawn: shown invisible first so every
        widget paints off-screen, then made visible in one step (otherwise it
        appears white and fills in piece by piece over ~0.6 s)."""
        self.root.attributes("-alpha", 0.0)
        self.root.deiconify()
        self._orb_key = None
        self.render()
        self.root.update()
        self.root.after(40, self._finish_reveal)

    def _finish_reveal(self):
        """Second half of reveal_window: make the window visible, hide the pill."""
        self.root.update_idletasks()
        self.root.attributes("-alpha", 1.0)
        self.pill.withdraw()
        self.root.lift()

    def _pill_press(self, e):
        """Mouse down on the pill: remember where on the pill it was grabbed."""
        self.hide_pill_menu()
        self._drag = (e.x_root - self.pill.winfo_x(), e.y_root - self.pill.winfo_y())

    def _pill_drag(self, e):
        """Mouse moved with the button down: move the pill along."""
        dx, dy = getattr(self, "_drag", (0, 0))
        self.pill.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

    def quit(self):
        """Close the app cleanly: hide every window at once, then save settings,
        finish the running job up to now, remove the tray icon, stop the
        dashboard and plug workers, switch the light off (the links send
        "off" when they stop) and give the Arduino thread a moment to do so."""
        if getattr(self, "_quitting", False):     # several windows asked at once
            return
        self._quitting = True
        try:
            if self.s["compact"]:
                self.s["compact_pos"] = [self.pill.winfo_x(), self.pill.winfo_y()]
            # Disappear at once; the clean-up below (saving, closing the Arduino
            # port) can take a moment and shouldn't leave a frozen window behind
            for w in (self.pill_menu, self.pill, self.root):
                w.withdraw()
            self.root.update_idletasks()
        except tk.TclError:
            pass
        save_settings(self.s)
        self.runlog.close()         # a job still running counts up to now
        stop_sound()
        self.tray.remove()
        self.dash.stop()
        self.plugs.stop_evt.set()
        if self.checklog.enabled:
            self.checklog.save()
        self.detector.stop_evt.set()
        for link in (self.arduino, self.netlight):
            link.enabled = False
            link.stop_evt.set()
            link.wake.set()
        if self.arduino.is_alive():
            self.arduino.join(timeout=1.5)
        self.root.destroy()

    # ------------------------------------------------------------ updates
    def poll(self):
        """The UI thread's fast loop (every 50 ms): handle every queued message,
        then settle a pending job end and collect garbage.

        Message kinds on self.q:
        - {"cmd": "show"}: another copy of Beacon was started; show this one
        - {"cmd": "call", "fn": f}: run f here (from a background thread)
        - anything else: a detector reading (see Detector.run)
        In test mode readings are ignored so the test buttons stay in charge."""
        try:
            while True:
                msg = self.q.get_nowait()
                if msg.get("cmd") == "show":      # someone launched the app again
                    self.bring_to_front()
                    continue
                if msg.get("cmd") == "call":      # a background thread's UI update
                    try:
                        msg["fn"]()
                    except Exception:
                        logging.exception("UI call failed")
                    continue
                self.last_msg = msg
                self.last_check = time.time()
                if not self.test_mode:
                    self.checklog.record(msg)
                    if msg.get("file"):
                        self.file_name = msg["file"]
                    self.progress = msg.get("progress")
                    self.set_state(msg["shown"], msg.get("since"))
        except queue.Empty:
            pass
        self.settle_job_end()
        # Python's cycle collector would otherwise run on whichever thread happens
        # to allocate, and freeing a Tk variable off the main thread makes Tk
        # complain ("main thread is not in main loop"). Collect here instead.
        now = time.time()
        if now - self._gc_at > 2.0:
            self._gc_at = now
            gc.collect(2 if now - self._gc_full_at > 60 else 0)
            if now - self._gc_full_at > 60:
                self._gc_full_at = now
        self.root.after(50, self.poll)

    # ---- The status state machine ------------------------------------------
    # set_state() decides what's shown; _apply_state() does the bookkeeping;
    # settle_job_end() completes changes that were held back. Two hold-backs:
    # - _end_pending: a job stopped (Running/Paused -> something else). The
    #   light and pill change at once, but the job only ends after JOB_END_GRACE
    #   without running again. If it runs again within that time it's the same
    #   job (multi-pass jobs, flickering connections).
    # - _resume_pending: Paused -> Running. LightBurn briefly shows Busy when a
    #   paused job is stopped, so Running is only shown after RESUME_HOLD; if
    #   something else comes first, it was a stop, and Paused never turned red.
    JOB_END_GRACE = 2.0     # a job is only finished after this long without running

    def set_state(self, new, at=None):
        """Show a new status straight away. at: when the change really
        happened (readers confirm some changes a moment later), used for job
        times; defaults to now.

        Leaving a job (Running/Paused -> Ready, Framing, Disconnected) shows
        at once on the light and pill, but the job is only finished (stats,
        "Job finished", chime) after JOB_END_GRACE seconds: if the laser goes
        back to running before then, it is still the same job. That keeps a
        flickering connection or multi-pass job from becoming many tiny jobs."""
        old = self.state
        if new == old:
            return
        now = time.time()
        if DEV:
            logging.info("reader says %s (showing %s, dated %+.2fs)", new, old,
                         (at - now) if at else 0.0)
        if at and 0 <= now - at < 10:
            now = at
        job = ("busy", "paused")
        # Paused -> Running: LightBurn also shows Busy for ~0.5 s when a paused job
        # is stopped, so a resume is only shown once it has lasted RESUME_HOLD
        # (dated from when it began); anything else first means it was a stop.
        res = self._resume_pending
        if res:
            if new == "busy":
                return                              # still checking
            self._resume_pending = None
            if new == "paused":
                return
        elif old == "paused" and new == "busy" and not self.test_mode:
            # "at" dates the job; the wait counts from when we saw it ("seen"),
            # because a reader can date a change from before the blip began
            self._resume_pending = {"at": now, "seen": time.time()}
            return
        pend = self._end_pending
        if pend and new in job:                     # back within the grace: same job
            self._end_pending = None
            self.state = pend["from"]
            if new == pend["from"]:
                self.push_arduino()
                self.update_relays()
                return
            return self._apply_state(new, now)
        if pend:                                    # e.g. Ready -> Framing while waiting
            self.state = new
            self.push_arduino()
            self.update_relays()
            return
        if old in job and new not in job and new is not None and not self.test_mode:
            self._end_pending = {"from": old, "at": now, "seen": time.time()}
            self.state = new
            self.push_arduino()
            self.update_relays()
            return
        self._apply_state(new, now)

    RESUME_HOLD = 0.8       # Paused -> Running must last this long to count as a resume

    # Both hold-backs are timed from "seen" (when this app received the change),
    # not "at" (when the reader says it began): a reader can backdate a change,
    # and timing from that could end the wait before it started.
    def settle_job_end(self):
        """Called every tick: confirm a resume, and finish a job once it has
        really stopped."""
        res = self._resume_pending
        if res and time.time() - res["seen"] >= self.RESUME_HOLD:
            self._resume_pending = None
            if self.state == "paused":
                self._apply_state("busy", res["at"])
        pend = self._end_pending
        if pend and time.time() - pend["seen"] >= self.JOB_END_GRACE:
            self._end_pending = None
            shown = self.state
            self.state = pend["from"]
            self._apply_state(shown, pend["at"])

    def _apply_state(self, new, now):
        """The bookkeeping for a status change: job start/end, run log, alerts."""
        old = self.state
        if new == old:
            return
        if new == "busy" and old != "paused":
            self.job_start = now
        job = ("busy", "paused")
        if old in job and new not in job:
            if self.job_start:
                self.last_job = now - self.job_start
            self.job_start = None
            self.progress = None
            if self.s["chime"] and new in ("ready", "framing") and not self.muted():
                play_chime(self.s["chime_file"])
            if new in ("ready", "framing"):
                self.idle_since, self.free_sent = now, False
        if new == "busy" and old not in job:
            self.job_alerts = set()
            self.idle_since, self.free_sent = None, True
        if new == "paused" and old != "paused":
            self.pause_since = now
        elif new != "paused":
            self.pause_since = None
        self.state = new
        self.runlog.observe("test" if self.test_mode else new, now=now,
                            tags={"name": self.file_name, "client": self.s["client"],
                                  "material": self.s["material"]})
        self._notify_change(old, new)
        self.push_arduino()
        self.update_relays()

    def push_arduino(self):
        """Send the current status letter (and app alarm) to the USB and Wi-Fi
        lights, waking their threads only when something changed."""
        code = self.arduino_code()
        if DEV and code != self.arduino.code:      # dev only: what the light is told, and why
            logging.info("light %s <- %s (resume pending %s, end pending %s)",
                         code.decode(), self.state, bool(self._resume_pending),
                         bool(self._end_pending))
        for link in (self.arduino, self.netlight):
            if code != link.code or link.alarm != self.app_alarm:
                link.code, link.alarm = code, self.app_alarm
                link.wake.set()

    def arduino_code(self):
        """The one-letter status code for the light (see the protocol above
        LinkBase). Framing/Disconnected use capitals when blinking is on."""
        blink = self.s["blink"]
        return {
            "busy": b"B", "paused": b"P", "ready": b"R",
            "framing": b"F" if blink else b"f",
            "disconnected": b"D" if blink else b"d",
        }.get(self.state, b"U")

    def time_left(self):
        """Estimated seconds left from the % done and the running time so far."""
        p = self.progress
        cur = self.runlog.current(time.time())
        run = cur["run"] if cur else (time.time() - self.job_start if self.job_start else 0)
        if p is None or not 2 <= p <= 99 or run < 10:
            return None
        return run * (100 - p) / p

    def last_job_time(self):
        """Run time of the last job, including one that just stopped and is
        still settling (finished JOB_END_GRACE seconds later)."""
        pend = self._end_pending
        if pend:
            cur = self.runlog.current(pend["at"])
            if cur:
                return cur["run"]
            if self.job_start:
                return pend["at"] - self.job_start
        return self.last_job

    def subtitle(self):
        """The line under the big status word on the Home page: job timer, % and
        time left while running; the last job's time when ready or framing;
        and otherwise a hint about why the status can't be seen."""
        st = self.state
        if st == "busy":
            text = "Job running"
            if self.job_start:
                text += f"  \u00b7  {fmt_duration(time.time() - self.job_start)}"
            if self.progress is not None and 0 < self.progress < 100:
                text += f"  \u00b7  {self.progress}%"
                left = self.time_left()
                if left:
                    text += f"  \u00b7  ~{fmt_human(left)} left"
            return text
        if st == "paused":
            return "Job paused"
        if st == "framing":     # a galvo goes straight back to framing after a job
            last = self.last_job_time()
            return "Red-dot framing" + (f"  ·  last job {fmt_human(last)}" if last else "")
        if st == "ready":
            last = self.last_job_time()
            return f"Last job took {fmt_human(last)}" if last else "Laser idle"
        if st == "disconnected":
            return "LightBurn isn't talking to the laser"
        if self.test_mode:
            return "Simulated: LightBurn's status can't be seen"
        return (self.last_msg.get("note") or "Can't see LightBurn's status")

    def tick(self):
        """Redraw five times a second (blinking, timers), forever."""
        self.render()
        self.root.after(200, self.tick)

    def outline_style(self, pill):
        """(colour, width, dash) for the main card or the pill's outline.
        Test mode keeps its own look: orange round the pill, plain card."""
        if self.test_mode:
            return (AMBER, 2, None) if pill else (CARD_BORDER, 1, None)
        if self.state in ("busy", "paused"):
            return (look(self.state)[0], 2, None)
        if self.state in ("ready", "framing"):
            return (look("ready")[0], 2, (6, 4))   # dashed, like LightBurn's framing line
        return (CARD_BORDER, 1, None)       # disconnected / can't see LightBurn

    def render(self):
        """Bring everything on screen up to date. Called by tick() and when the
        window is revealed. Work is skipped where possible: the orb and pill
        dot are only redrawn when their look changed, hidden windows aren't
        touched, and page-specific updates only run while that page is open.
        Also runs the monitor (alerts) and keeps the light, relays and job
        checkpoint current."""
        now = time.time()
        try:
            self.monitor(now)               # alerts, sensors, maintenance, dashboard
        except Exception:
            logging.exception("monitor failed")
        color, title, blinks = look(self.state)
        on = True
        if blinks and self.s["blink"]:
            on = int(time.time() / 0.6) % 2 == 0
        mode = ctk.get_appearance_mode()
        if mode != getattr(self, "_key_mode", None):     # theme changed (or System did)
            self._key_mode = mode
            for win in (self.pill, self.pill_menu):
                set_window_key(win)
        outline_key = (self.state, self.test_mode, mode)
        if outline_key != getattr(self, "_outline_key", None):
            self._outline_key = outline_key
            self.card.set_style(*self.outline_style(pill=False))
            self.pill_frame.set_style(*self.outline_style(pill=True))

        if self.root.state() != "withdrawn":
            key = (color, on, mode, self.state)
            if key != self._orb_key:
                self.draw_orb(color, on)
                self._orb_key = key
            self.title_lbl.configure(text=title)
            self.sub_lbl.configure(text=self.subtitle())
            self.check_lbl.configure(
                text="Run time today" + " " + fmt_human(self.runlog.run_on(date.today(), now))
                + ("  \u00b7  \U0001F507" if self.muted() else ""))
            if self.s["arduino"] or self.s["wifi_on"]:
                link = self.arduino if self.s["arduino"] else self.netlight
                self.ard_dot.configure(text="\u25cf",
                                       text_color=GREEN if link.connected else RED)
                self.ard_lbl.configure(text="Arduino" if self.s["arduino"] else "Wi-Fi light")
            else:
                self.ard_dot.configure(text="")
                self.ard_lbl.configure(text="")
            self.update_banners()
            if self.page in self.subpages or self.page == "relays":
                self.refresh_subpage_status()
            if self.page == "wizard":
                self.update_wizard_live()
            if self.page == "settings":
                self.port_status.configure(text=self.arduino.status)
                self.update_check_summary()
                self.show_api_status()
                self.demo_btn.configure(
                    text=f"Test job running · {max(0, int(self.demo_end - time.time() + 0.99))} s"
                         "  (click to finish)" if self.demo_end else "Run a 10 s test job")
                if self.s["diagnostics"]:
                    self.update_diag()

        if self.pill.state() != "withdrawn":
            key = (color, on, mode, self.state)
            if key != self._pill_key:
                self.draw_pill(color, on)
                self._pill_key = key
            # Capitals: no descenders, so every word sits centred beside the dot
            self.pill_title.configure(text=title.upper())
            t = ""
            if self.state == "busy" and self.job_start:
                t = fmt_duration(time.time() - self.job_start)
                if self.progress is not None and 0 < self.progress < 100:
                    t += f"  ·  {self.progress}%"
            self.pill_time.configure(text=t)
            if t and not self.pill_time.winfo_ismapped():
                self.pill_title.pack_configure(padx=(0, 0))
                self.pill_time.pack(side="left", padx=(PILL_PAD, self.pill_end_gap),
                                    pady=(0, PILL_TEXT_LIFT))
            elif not t and self.pill_time.winfo_ismapped():
                self.pill_time.pack_forget()
                self.pill_title.pack_configure(padx=(0, self.pill_end_gap))

        self.push_arduino()
        self.update_relays()        # keep-on delays run out here
        self.runlog.checkpoint()

    def update_diag(self):
        """Fill the Diagnostics box with the last reading: how it was read, the
        raw and shown status, the note, and the text that was read."""
        m = self.last_msg
        text = (f"read: {m.get('mode', '')}   raw: {m.get('raw')}   shown: {m.get('shown')}\n"
                + ("note: " + m["note"] + "\n" if m.get("note") else "")
                + "\n".join(m.get("lines", [])[:60]))
        if getattr(self, "_diag_text", None) == text:
            return
        self._diag_text = text
        self.diag.configure(state="normal")
        self.diag.delete("1.0", "end")
        self.diag.insert("1.0", text)
        self.diag.configure(state="disabled")

    def draw_orb(self, color, on):
        """Draw the big status orb: a glow of 12 rings fading out from the core,
        the solid core, and a slightly lighter centre for depth. on=False
        draws it dimmed (the "off" phase of blinking)."""
        c = self.orb
        card = pick(CARD)
        c.configure(bg=card)
        c.delete("all")
        size = self.orb_size
        cx = cy = size / 2
        R = size * 0.27
        rings = 12
        for i in range(rings, 0, -1):
            r = R + i * (size * 0.018)
            strength = (0.30 if on else 0.05) * (1 - i / (rings + 1)) ** 1.8
            c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=blend(card, color, strength),
                          outline="")
        core = color if on else blend(card, color, 0.22)
        c.create_oval(cx - R, cy - R, cx + R, cy + R, fill=core, outline="")
        hi = R * 0.78
        c.create_oval(cx - hi, cy - hi, cx + hi, cy + hi,
                      fill=blend(core, "#FFFFFF", 0.10 if on else 0.03), outline="")

    def draw_pill(self, color, on):
        """Draw the pill's dot: a faint halo and a solid centre."""
        c = self.pill_dot
        card = pick(CARD)
        c.configure(bg=card)
        c.delete("all")
        d = self.pill_dot_size
        c.create_oval(1, 1, d - 1, d - 1, fill=blend(card, color, 0.25 if on else 0.05),
                      outline="")
        p = d * 0.2
        c.create_oval(p, p, d - p, d - p, fill=color if on else blend(card, color, 0.22),
                      outline="")

    # ============================================================ monitoring
    def _job_run(self, now):
        """Running seconds of the current job so far (pauses not counted)."""
        cur = self.runlog.current(now)
        return cur["run"] if cur else ((now - self.job_start) if self.job_start else 0.0)

    def monitor(self, now):
        """Runs every tick: alerts, sensors, maintenance, dashboard snapshot."""
        # Each check below fires its notification once and remembers that it did
        # (job_alerts for things that reset with every new job, or its own
        # flag).
        s, st = self.s, self.state
        if s.get("mute_until") and now > float(s["mute_until"]):
            self.unmute()
        label = self._job_label()
        # --- long / unusual / long-paused jobs
        if st in ("busy", "paused"):
            run = self._job_run(now)
            lim = int(s["alert_long_run_min"] or 0)
            if lim and run > lim * 60 and "long" not in self.job_alerts:
                self.job_alerts.add("long")
                self.notify("stuck", "⏱ " + "Job is taking long",
                            f"Running for {fmt_human(run)}" + label)
            if s["alert_unusual"] and "unusual" not in self.job_alerts:
                typical = self.runlog.typical_run(self.file_name)
                if typical and run > max(300, typical * 3):
                    self.job_alerts.add("unusual")
                    self.notify("stuck", "⏱ " + "Much longer than usual",
                                f"{fmt_human(run)} — usually {fmt_human(typical)}"
                                + label)
        pause_lim = int(s["alert_long_pause_min"] or 0)
        if (st == "paused" and self.pause_since and pause_lim
                and now - self.pause_since > pause_lim * 60 and "pause" not in self.job_alerts):
            self.job_alerts.add("pause")
            self.notify("stuck", "⏸ " + "Paused for a long time",
                        f"Paused for {fmt_human(now - self.pause_since)}" + label)
        # --- laser free again
        if (not self.free_sent and self.idle_since and st in ("ready", "framing")
                and now - self.idle_since >= int(s["free_after_min"]) * 60):
            self.free_sent = True
            if (self.last_job or 0) >= 60:
                self.notify("free", "\U0001F7E2 " + "The laser is free",
                            f"Idle for {fmt_human(now - self.idle_since)}")
        # --- can't read LightBurn (minimised, covered, moved)
        note = self.last_msg.get("note") or ""
        if st is None and not self.test_mode and note != "LightBurn isn't open":
            if self.unreadable_since is None:
                self.unreadable_since = now
            elif now - self.unreadable_since > 60 and not self.unreadable_sent:
                self.unreadable_sent = True
                self.notify("unreadable", "\U0001F441 " + "Can't read LightBurn",
                            (note or "Can't see LightBurn's status"))
        else:
            self.unreadable_since, self.unreadable_sent = None, False
        # --- nobody at the PC while the laser runs
        unatt = int(s["unattended_min"] or 0)
        if st == "busy" and unatt and "unattended" not in self.job_alerts:
            idle = osys.idle_seconds()
            if idle > unatt * 60:
                self.job_alerts.add("unattended")
                for link in (self.arduino, self.netlight):
                    link.chirp()
                self.notify("unattended", "\U0001F440 " + "Nobody at the PC",
                            f"No keyboard or mouse for {fmt_human(idle)} "
                            f"while the laser runs." + label)
        # --- maintenance (once a minute)
        if now - getattr(self, "_maint_checked", 0) > 60:
            self._maint_checked = now
            hours = self.runlog.total_run(now) / 3600
            for t in s["maint"]:
                due = hours - float(t.get("last_h", 0)) >= float(t.get("every_h", 1e9))
                if due and t["name"] not in self.maint_alerted:
                    self.maint_alerted.add(t["name"])
                    self.notify("maintenance", "\U0001F527 " + "Maintenance due",
                                f"{t['name']} (every {t['every_h']} h)")
                elif not due:
                    self.maint_alerted.discard(t["name"])
        # --- sensors and alarms
        self.process_inputs(now)
        # --- Wi-Fi light settings can change in Settings at any time
        self.netlight.enabled, self.netlight.host = bool(s["wifi_on"]), s["wifi_host"]
        # --- what the phone dashboard shows
        color, title, _ = look(st)
        midnight = datetime.combine(date.today(), datetime.min.time()).timestamp()
        self._dash_snapshot = {
            "title": title, "subtitle": self.subtitle(), "color": color,
            "progress": (self.progress if st == "busy" and self.progress is not None
                         and 0 < self.progress < 100 else None),
            "today": fmt_human(self.runlog.run_on(date.today(), now)),
            "jobs_today": len(self.runlog.jobs_between(midnight, now + 1, now)),
            "relays": [{"index": i, "name": r["name"], "on": self.relay_on[i],
                        "auto": r["auto"] != "manual" and self.relay_override[i] is None}
                       for i, r in enumerate(s["relays"]) if r["use"]]}
        # Hovering the tray icon shows the current status.
        tip = f"{WINDOW_TITLE} — {title}"
        if tip != getattr(self, "_tray_tip", None):
            self._tray_tip = tip
            self.tray.set_tip(tip)

    def temperature(self, raw):
        """Degrees C from the board's analog reading (0-1023 for 0-5 V). LM35:
        10 mV per degree from 0 V. TMP36: 10 mV per degree with a 500 mV
        offset (so it can read below zero)."""
        if self.s["temp_type"] == "TMP36":
            return (raw * 5000 / 1023 - 500) / 10
        return raw * 500 / 1023                     # LM35: 10 mV per °C

    def process_inputs(self, now):
        """React to the safety inputs from whichever light link is enabled and
        has reported in the last 5 seconds: fire/smoke, temperature, lid
        opened during a job, no exhaust/coolant flow (for 10 s while
        running), and presses of the board's button.

        Builds alarm_text for the red banner and app_alarm for the buzzer. An
        alarm the user acknowledged stays silent until every alarm has
        cleared; a new problem after that sounds again."""
        link = next((l for l in (self.arduino, self.netlight)
                     if l.enabled and l.inputs and now - l.inputs["time"] < 5), None)
        self.live_inputs = link.inputs if link else None
        if not link:
            self.alarm_text, self.app_alarm = "", False
            return
        inp, s, alarms = link.inputs, self.s, []
        label = self._job_label()
        # fire / smoke: the board sounds its own alarm too, even if the PC fails
        fire = bool(s["sensor_fire"] and inp["fire"])
        if fire and not self.fire_active:
            self.fire_active = True
            self.notify("fire", "\U0001F525 " + "FIRE / SMOKE DETECTED",
                        "The fire sensor at the laser was triggered. Check the machine now."
                        + label, photo=True)
            self.update_relays()
        elif not fire and self.fire_active:
            self.fire_active = False
            self.update_relays()
        if self.fire_active:
            alarms.append("Fire / smoke detected")
        # temperature
        self.temp_c = None
        if s["sensor_temp"]:
            c = self.temp_c = self.temperature(inp["temp_raw"])
            limit = float(s["temp_max"])
            if c > limit and not self.temp_alerted:
                self.temp_alerted = True
                self.notify("temp", "\U0001F321 " + "Too hot",
                            f"{c:.0f} °C (limit {limit:.0f} °C)" + label)
            elif c < limit - 3:
                self.temp_alerted = False
            if self.temp_alerted:
                alarms.append(f"Too hot: {c:.0f} °C")
        # lid / door opened during a job
        if s["sensor_door"] and inp["door_open"] and self.state == "busy" \
                and "door" not in self.job_alerts:
            self.job_alerts.add("door")
            self.notify("door", "\U0001F6AA " + "Lid opened during a job",
                        "The lid / door switch opened while the laser was running." + label)
        # exhaust / cooling flow
        if s["sensor_flow"] and self.state == "busy" and not inp["flow"]:
            if self.flow_bad_since is None:
                self.flow_bad_since = now
            elif now - self.flow_bad_since > 10 and not self.flow_alerted:
                self.flow_alerted = True
                self.notify("flow", "\U0001F4A8 " + "Exhaust / cooling not running",
                            "The flow sensor reports no flow while the laser runs." + label)
        else:
            self.flow_bad_since = None
            if inp["flow"] or self.state != "busy":
                self.flow_alerted = False
        if self.flow_alerted:
            alarms.append("No exhaust / cooling flow")
        # the button on A1
        # The board counts presses; a different count than last time means it
        # was pressed. First silence an alarm, otherwise do the chosen button
        # action.
        if self.last_presses is not None and inp["presses"] != self.last_presses:
            if alarms and not getattr(self, "alarm_acked", False):
                self.acknowledge_alarm()
            elif s["button_action"] == "mute":
                self.unmute() if self.muted() else self.mute(3600)
            elif s["button_action"].startswith("relay:"):
                i = int(s["button_action"].split(":")[1])
                if 0 <= i < len(RELAY_PINS) and s["relays"][i]["use"]:
                    self.toggle_relay(i)
        self.last_presses = inp["presses"]
        if not alarms:
            self.alarm_acked = False
        self.alarm_text = " · ".join(alarms)
        self.app_alarm = bool(alarms) and not getattr(self, "alarm_acked", False)

    def acknowledge_alarm(self):
        """Silence the alarm (it comes back if a new problem starts)."""
        self.alarm_acked, self.app_alarm = True, False
        for link in (self.arduino, self.netlight):
            link.pending += b"X\n"          # also clears the board's latched fire alarm
            link.wake.set()
        self.push_arduino()

    def update_banners(self):
        """Show or hide the Home page's banners above the status card: the red
        alarm banner, and "Can't read LightBurn" after 20 s without a status."""
        now = time.time()
        alarm = getattr(self, "alarm_text", "")
        want = {
            self.alarm_banner: bool(alarm),
            self.health_banner: bool(self.state is None and not self.test_mode
                                     and self.unreadable_since and now - self.unreadable_since > 20),
        }
        if alarm:
            text = "⚠  " + alarm + ("  ·  " + "click to silence"
                                         if self.app_alarm else "  ·  " + "silenced")
            if self.alarm_banner.cget("text") != text:
                self.alarm_banner.configure(text=text)
        for banner, show in want.items():
            if show and not banner.winfo_ismapped():
                banner.pack(fill="x", padx=18, pady=(10, 0), before=self.card)
            elif not show and banner.winfo_ismapped():
                banner.pack_forget()

    def find_lightburn_again(self):
        """The "click to look again" banner: forget where the status was and
        scan every window afresh."""
        self.detector.reset()
        self.unreadable_since = None
        self.sub_lbl.configure(text="Looking for LightBurn…")

    # ======================================================= phone dashboard
    def dash_state(self):
        """What the phone dashboard shows, called from the dashboard's server
        thread. It returns a copy of a snapshot that monitor() builds on the
        UI thread, so the server never reads live UI state."""
        return dict(self._dash_snapshot)

    def dash_command(self, name, data):
        """A button pressed on the phone (server thread): hand it to the UI
        thread. Only relay toggling is offered."""
        if name == "relay":
            self._on_ui(lambda: self._dash_toggle(int(data.get("index", -1))))

    def _dash_toggle(self, i):
        """Toggle relay i from the phone, if it's a relay in use."""
        if 0 <= i < len(RELAY_PINS) and self.s["relays"][i]["use"]:
            self.toggle_relay(i)

    def start_dashboard(self):
        """Start the phone dashboard web server on the chosen port and PIN."""
        return self.dash.start(self.s["dash_port"], self.s["dash_pin"])

    def _dash_changed(self):
        """The dashboard switch or its port changed: start or stop the server
        and check the firewall."""
        if self.s["dash_on"]:
            self.start_dashboard()
        else:
            self.dash.stop()
        self.check_firewall()

    def check_firewall(self):
        """Look up (in the background) whether phones may reach the dashboard port."""
        port = self.s["dash_port"]

        def work():
            ok = osys.firewall_allowed(port, f"{APP_NAME} phone dashboard")
            self._on_ui(lambda: self._show_firewall(ok))
        threading.Thread(target=work, daemon=True).start()

    def _show_firewall(self, ok):
        """Show the firewall result on the Phone & Wi-Fi page (None = unknown)."""
        self._fw_ok = ok
        if ok:
            self.fw_status.configure(text=("✓ Allowed through Windows Firewall" if IS_WIN else
                                           "✓ The macOS firewall is off: phones can connect"),
                                     text_color=MUTED)
            self.fw_btn.pack_forget()
        elif ok is False:
            self.fw_status.configure(text="Windows Firewall is blocking phones: press Allow on "
                                          "Wi-Fi.", text_color=AMBER)
            if not self.fw_btn.winfo_ismapped():
                self.fw_btn.pack(side="left", padx=6)

    def allow_firewall(self):
        """The "Allow on Wi-Fi" button: add a Windows Firewall rule for the
        dashboard port. Windows asks for administrator permission; the result
        is checked again 3 seconds later."""
        if not osys.firewall_allow(self.s["dash_port"], f"{APP_NAME} phone dashboard"):
            self.fw_status.configure(text="Not allowed: Windows asked for permission and it "
                                          "was declined.", text_color=RED)
            return
        self.fw_status.configure(text="Allowing…", text_color=MUTED)
        self.root.after(3000, self.check_firewall)

    # ===================================================== Telegram commands
    def telegram_poller(self):
        """Background: reads messages sent to the bot (long polling) and passes
        commands from your chat to the UI thread. Also remembers the last chat
        that wrote, for the Find button."""
        # Long polling: getUpdates waits up to 25 s for a new message, so
        # replies are instant without asking Telegram every second. offset =
        # last update id + 1 tells Telegram which messages have been handled.
        # Only messages from the configured chat are obeyed; anyone else who
        # finds the bot is ignored.
        offset = None
        self._tg_seen = None
        while True:
            s = self.s
            token = s["tg_token"]
            if not (token and s["tg_commands"]):
                time.sleep(3)
                continue
            try:
                params = {"timeout": 25}
                if offset:
                    params["offset"] = offset
                updates = telegram_call(token, "getUpdates", params, timeout=35)
            except RuntimeError:
                time.sleep(10)
                continue
            for u in updates or []:
                offset = u["update_id"] + 1
                msg = u.get("message") or u.get("channel_post")
                if not msg:
                    continue
                chat = msg.get("chat", {})
                self._tg_seen = chat
                text = (msg.get("text") or "").strip()
                if str(chat.get("id")) == str(s["tg_chat"]) and text.startswith("/"):
                    self._on_ui(lambda t=text: self.tg_command(t))

    def tg_reply(self, text):
        """Send a reply to the configured chat in the background."""
        token, chat = self.s["tg_token"], self.s["tg_chat"]

        def work():
            try:
                telegram_call(token, "sendMessage", {"chat_id": chat, "text": text})
            except RuntimeError as e:
                logging.warning("Telegram reply failed: %s", e)
        threading.Thread(target=work, daemon=True).start()

    def tg_command(self, text):
        """Answer one Telegram command (UI thread). "/on fan" turns on the first
        relay whose name starts with "fan"; "/mute 2" mutes for two hours.
        "/status@MyBot" (the form used in groups) works too."""
        cmd, _, arg = text[1:].partition(" ")
        cmd, arg = cmd.split("@")[0].lower(), arg.strip()
        now = time.time()
        used = [(i, r) for i, r in enumerate(self.s["relays"]) if r["use"]]
        if cmd in ("start", "help"):
            reply = (f"{APP_NAME} " + "commands" + ":\n/status\n/today\n/report — "
                     + "last 24 hours" + "\n/week\n/relays\n/on <name>  /off <name>\n"
                     "/mute [hours]  /unmute\n/photo")
        elif cmd == "status":
            reply = f"{look(self.state)[1]}\n{self.subtitle()}"
            if self.file_name:
                reply += f"\n{self.file_name}"
        elif cmd == "today":
            midnight = datetime.combine(date.today(), datetime.min.time()).timestamp()
            jobs = self.runlog.jobs_between(midnight, now + 1, now)
            reply = (f"Today: {fmt_human(self.runlog.run_on(date.today(), now))} "
                     f"laser on, {len(jobs)} jobs")
        elif cmd in ("report", "week"):
            t, b, _ = build_report("weekly" if cmd == "week" else "daily", datetime.now(),
                                   self.runlog, self.s["costs"])
            reply = f"{t}\n{b}"
        elif cmd == "relays":
            reply = "\n".join(f"{r['name']}: {'On' if self.relay_on[i] else 'Off'}"
                              for i, r in used) or "No relay buttons set up."
        elif cmd in ("on", "off"):
            match = [(i, r) for i, r in used if r["name"].lower().startswith(arg.lower())] \
                if arg else []
            if not match:
                reply = "No relay called" + f" “{arg}”. /relays"
            else:
                i, r = match[0]
                self.relay_override[i] = cmd == "on"
                self.update_relays()
                reply = f"{r['name']}: {'On' if cmd == 'on' else 'Off'} ✓"
        elif cmd == "mute":
            try:
                hours = float(arg or 1)
            except ValueError:
                hours = 1
            self.mute(hours * 3600)
            reply = f"\U0001F507 Muted for {hours:g} h (safety alarms still come through)"
        elif cmd == "unmute":
            self.unmute()
            reply = "\U0001F514 " + "Notifications back on"
        elif cmd == "photo":
            if not camera.available():
                reply = "Camera support isn't installed on the PC."
            else:
                s = self.s

                def work():
                    try:
                        jpg = camera.snapshot(s["camera_index"])
                        channels.telegram_photo(s["tg_token"], s["tg_chat"],
                                                f"{look(self.state)[1]} · {datetime.now():%H:%M}",
                                                jpg)
                    except RuntimeError as e:
                        self.tg_reply(str(e))
                threading.Thread(target=work, daemon=True).start()
                return
        else:
            reply = "Unknown command. Send /help"
        self.tg_reply(reply)

    # ================================================ Windows: tray, hotkey
    def apply_windows_settings(self):
        """Apply the system settings: the "Start with Windows" registry entry
        (macOS: an "open at login" LaunchAgent), started with --startup,
        which opens straight into the pill; the tray (menu-bar) icon; and the
        global hotkey Ctrl+Alt+<key> (Control+Option on a Mac). The hotkey
        can fail if another app already owns it, shown under the setting."""
        s = self.s
        try:
            osys.autostart_set(APP_NAME, bool(s["autostart"]),
                               osys.launch_command(os.path.abspath(__file__),
                                                   "--dev" if DEV else ""))
        except OSError as e:
            logging.warning("autostart: %s", e)
        if s["tray_always"]:
            self.tray.show_icon()
        else:
            self.tray.hide_icon()
        ok = self.tray.set_hotkey(s["hotkey_key"] if s["hotkey_on"] else None)
        if hasattr(self, "hotkey_status"):
            self.hotkey_status.configure(
                text="" if ok else "That shortcut is taken by another app. Pick another key.",
                text_color=RED)

    def hotkey_pressed(self):
        """Ctrl+Alt+<key>: toggle between the full window and the pill, or show
        the window if it's minimised or hidden."""
        if self.s["compact"]:
            self.leave_compact()
        elif self.root.state() == "normal":
            self.enter_compact()
        else:
            self.bring_to_front_full()

    def bring_to_front_full(self):
        """Show the full window (from the pill, tray or another launch)."""
        if self.s["compact"]:
            self.leave_compact()
        else:
            self.bring_to_front()

    def _set_value(self, key, value):
        """Save one setting (used by dropdowns that convert their text first)."""
        self.s[key] = value
        save_settings(self.s)

    # ================================================================ updates
    def check_updates(self, auto=False):
        """Look for a newer version at UPDATE_URL (empty in this build, so off).
        auto=True is the hourly timer: it only checks once a day and only if
        "Check for updates" is on. The network request runs on a thread."""
        if auto:
            self.root.after(3600 * 1000, lambda: self.check_updates(auto=True))
            if (not self.s["auto_update"] or not UPDATE_URL
                    or time.time() - float(self.s["update_last_check"] or 0) < 86400):
                return
        if not UPDATE_URL:
            self.update_status.configure(text="Updates aren't set up in this build.")
            return
        self.s["update_last_check"] = time.time()
        save_settings(self.s)
        if not auto:
            self.update_status.configure(text="Checking…", text_color=MUTED)

        def work():
            try:
                info = updater.check(UPDATE_URL, APP_VERSION)
            except RuntimeError as e:
                if not auto:
                    self._on_ui(lambda m=str(e): self.update_status.configure(text=m,
                                                                              text_color=RED))
                return
            self._on_ui(lambda: self._update_found(info, auto))
        threading.Thread(target=work, daemon=True).start()

    def _update_found(self, info, auto):
        """Show the result of an update check. A new version turns the button
        into "Install <version>"; the automatic check also shows one Windows
        notification per version."""
        self.ensure_page("settings")            # its Help & updates section shows this
        if not info:
            if not auto:
                self.update_status.configure(text=f"You have the latest version "
                                                  f"({APP_VERSION}).", text_color=MUTED)
            return
        self.pending_update = info
        v = info["version"]
        self.update_status.configure(text=f"Version {v} is available. "
                                          f"{info.get('notes', '')}", text_color=MUTED)
        self.update_btn.configure(text=f"Install {v}", command=self.install_update)
        if auto and v != self.s["skip_version"]:
            self.s["skip_version"] = v              # tell once per version
            save_settings(self.s)
            self.tray.show(f"{APP_NAME} {v} is available",
                           "Open Settings › Help & updates to install it.")

    def install_update(self):
        """Download the new installer (showing progress), start it, and quit so
        it can replace the files."""
        info = getattr(self, "pending_update", None)
        if not info:
            return
        self.update_btn.configure(state="disabled")

        def progress(f):
            text = "Downloading…" + (f" {f:.0%}" if f is not None else "")
            self._on_ui(lambda: self.update_status.configure(text=text))

        def work():
            try:
                path = updater.download(info["url"], progress=progress)
            except RuntimeError as e:
                self._on_ui(lambda m=str(e): (self.update_status.configure(text=m, text_color=RED),
                                     self.update_btn.configure(state="normal")))
                return
            self._on_ui(lambda: (updater.run_installer(path), self.quit()))
        threading.Thread(target=work, daemon=True).start()

    # ============================================================ diagnostics
    # Secret settings. They're replaced with *** in the settings.json copy put
    # in a diagnostics zip, so sending one to support never shares passwords,
    # tokens or the LightBurn pairing secret.
    REDACT = ("tg_token", "pushover_token", "pushover_user", "email_pass", "twilio_token",
              "twilio_sid", "discord_url", "ha_token", "dash_pin", "lb_api_secret")

    def debug_info(self):
        """A plain-text summary for support: versions, reading mode, the current
        status and reading, link states, which features are on (names only,
        never their values) and the last text read."""
        s = self.s
        on = [k for k in ("arduino", "wifi_on", "dash_on", "notify_pc", "tg_commands", "discord_on",
                          "pushover_on", "email_on", "sms_on", "snapshot_on", "sensor_fire",
                          "sensor_temp", "sensor_door", "sensor_flow", "autostart") if s.get(k)]
        lines = [f"{APP_NAME} {APP_VERSION}", f"Python {sys.version.split()[0]} "
                 f"({'bundled' if getattr(sys, 'frozen', False) else 'script'})",
                 (f"Windows {platform.version()}" if IS_WIN else f"macOS {platform.mac_ver()[0]}") + f" {platform.machine()}",
                 f"Reader: {self.DETECT_MODES[s['detect_mode']]} (screen OCR: {self.ocr_name}, "
                 f"API key: {'yes' if s['lb_api_secret'] else 'no'})   "
                 f"check every {s['interval']} s",
                 f"Status: {self.state}  raw: {self.last_msg.get('raw')}  "
                 f"read: {self.last_msg.get('mode')}  note: {self.last_msg.get('note')}",
                 f"File: {self.file_name or '-'}   progress: {self.progress}",
                 f"Arduino: {self.arduino.status}   Wi-Fi light: {self.netlight.status}",
                 f"Inputs: {getattr(self, 'live_inputs', None)}",
                 f"Telegram: {'set up' if s['tg_token'] and s['tg_chat'] else 'not set up'}",
                 f"On: {', '.join(on) or '-'}",
                 f"Relays in use: {sum(1 for r in s['relays'] if r['use'])}",
                 f"Jobs recorded: {len(self.runlog.jobs)}",
                 "Text read:"] + ["  " + t for t in self.last_msg.get("lines", [])[:25]]
        return "\n".join(lines)

    def copy_debug_info(self):
        """Help page: copy debug_info() to the clipboard."""
        self.root.clipboard_clear()
        self.root.clipboard_append(self.debug_info())
        self.help_status.configure(text="Copied. Paste it into your email to support.")

    def save_diagnostics(self):
        """Help page: save a zip with debug info, the settings (secrets
        redacted), the log files, the last 200 jobs and the detection check
        summary."""
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".zip",
                                            initialfile=f"{APP_NAME}-diagnostics.zip",
                                            filetypes=[("Zip file", "*.zip")])
        if not path:
            return
        settings = {k: ("***" if k in self.REDACT and v else v) for k, v in self.s.items()}
        try:
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("debug.txt", self.debug_info())
                z.writestr("settings.json", json.dumps(settings, indent=2))
                logs = os.path.join(CONFIG_DIR, "logs")
                for name in (os.listdir(logs) if os.path.isdir(logs) else []):
                    z.write(os.path.join(logs, name), f"logs/{name}")
                z.writestr("recent_jobs.json", json.dumps(self.runlog.jobs[-200:], indent=1))
                if self.checklog.summary["reads"]:
                    z.writestr("detection-check.txt", self.checklog.summary_text())
            self.help_status.configure(text="Saved. Attach the file to your email to support.")
        except OSError as e:
            self.help_status.configure(text=str(e))

    def open_settings_folder(self):
        """Open the settings folder in Explorer."""
        os.makedirs(CONFIG_DIR, exist_ok=True)
        open_path(CONFIG_DIR)

    # ======================================================== shop & goals
    def shop_seconds(self, start, end):
        """Opening-hours seconds between two timestamps (Shop settings)."""
        s, total = self.s, 0.0
        d = datetime.fromtimestamp(start).date()
        while datetime.combine(d, datetime.min.time()).timestamp() < end:
            wd = d.weekday()
            open_day = (s["shop_days"] == "every" or (s["shop_days"] == "weekdays" and wd < 5)
                        or (s["shop_days"] == "weekends" and wd >= 5))
            if open_day:
                a = datetime.combine(d, datetime.min.time()).timestamp() + int(s["shop_start"]) * 3600
                b = datetime.combine(d, datetime.min.time()).timestamp() + int(s["shop_end"]) * 3600
                total += max(0.0, min(b, end) - max(a, start))
            d += timedelta(days=1)
        return total

    def week_start(self):
        """Monday 00:00 of this week."""
        today = date.today()
        return datetime.combine(today - timedelta(days=today.weekday()), datetime.min.time())

    def goal_line(self):
        """"Weekly goal: 12.5 / 20 h (62%)", or "" when no goal is set."""
        now = time.time()
        run = self.runlog.run_between(self.week_start().timestamp(), now, now) / 3600
        goal = float(self.s["goal_hours"] or 0)
        return f"Weekly goal: {run:.1f} / {goal:g} h ({run / goal:.0%})" if goal else ""

    # ===================================================== settings subpages
    def build_subpages(self):
        """Build the three pages opened from Settings › Notifications: More
        channels, Alerts & safety, Phone & Wi-Fi (built together on first
        use)."""
        self.subpages = {}
        self._build_channels_page()
        self._build_safety_page()
        self._build_remote_page()

    def channel_test(self, box, name):
        """A "Send test" button with a result line under it, for one channel."""
        row = ctk.CTkFrame(box, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(8, 0))
        status = ctk.CTkLabel(box, text="", font=self.font(11), text_color=MUTED, anchor="w",
                              justify="left", wraplength=300)
        self.action_button(row, "Send test", lambda: self.test_channel(name, status), 100).pack(
            side="left")
        status.pack(fill="x", padx=14, pady=(4, 10))

    def test_channel(self, name, status):
        """Send a test through one channel (Discord, Pushover, Email, SMS or a
        camera photo) on a background thread and show the result in `status`."""
        s = self.s
        title, body = f"\U0001F514 {APP_NAME} test", "This channel is working."
        status.configure(text="Sending…", text_color=MUTED)

        def work():
            try:
                if name == "Discord":
                    channels.discord_send(s["discord_url"], title, body)
                elif name == "Pushover":
                    channels.pushover_send(s["pushover_token"], s["pushover_user"], title, body)
                elif name == "Email":
                    channels.email_send(s["email_host"], s["email_port"], s["email_user"],
                                        s["email_pass"], s["email_to"], f"{APP_NAME}: {title}", body)
                elif name == "SMS":
                    channels.twilio_send(s["twilio_sid"], s["twilio_token"], s["twilio_from"],
                                         s["sms_to"], f"{title}\n{body}")
                elif name == "Camera":
                    jpg = camera.snapshot(s["camera_index"])
                    if s["tg_token"] and s["tg_chat"]:
                        channels.telegram_photo(s["tg_token"], s["tg_chat"], title, jpg)
                    elif s["discord_on"] and s["discord_url"]:
                        channels.discord_send(s["discord_url"], title, body, jpg)
                    else:
                        raise RuntimeError(f"Photo taken ({len(jpg) // 1024} KB). "
                                           + "Set up Telegram or Discord to receive it.")
                ok, text = True, "Test sent ✓"
            except RuntimeError as e:
                ok, text = False, str(e)
            self._on_ui(lambda: status.configure(text=text, text_color=MUTED if ok else RED))
        threading.Thread(target=work, daemon=True).start()

    def _build_channels_page(self):
        """More channels page: Discord, Pushover, email, SMS (Twilio) and the
        camera snapshot, each with its own on switch, fields and test button.
        Secret fields (tokens, passwords) are typed into masked boxes."""
        p, body = self.page_frame("More channels")
        self.subpages["channels"] = p
        box = self.section(body, "Discord")
        self.switch_row(box, "discord_on", "Send to Discord", None)
        self.note(box, "In Discord: channel settings › Integrations › Webhooks › "
                       "New Webhook › Copy Webhook URL.")
        self.entry_row(box, "discord_url", "Webhook URL", secret=True)
        self.channel_test(box, "Discord")

        box = self.section(body, "Pushover")
        self.switch_row(box, "pushover_on", "Send to Pushover", "Reliable phone alerts (paid app)")
        self.note(box, "On pushover.net: copy your User Key, then Create an Application to get "
                       "an API Token.")
        self.entry_row(box, "pushover_token", "API Token", secret=True)
        self.entry_row(box, "pushover_user", "User Key", secret=True)
        self.channel_test(box, "Pushover")

        box = self.section(body, "Email")
        self.switch_row(box, "email_on", "Send emails", None)
        self.note(box, "Gmail: server smtp.gmail.com, port 587, your address, and an App Password "
                       "(Google account › Security › App passwords).")
        self.entry_row(box, "email_host", "Mail server (SMTP)")
        self.entry_row(box, "email_port", "Port (587 or 465)", kind=int)
        self.entry_row(box, "email_user", "Username / your email")
        self.entry_row(box, "email_pass", "Password / app password", secret=True)
        self.entry_row(box, "email_to", "Send to")
        self.channel_test(box, "Email")

        box = self.section(body, "SMS (Twilio)")
        self.switch_row(box, "sms_on", "Send text messages", "Twilio charges a few cents per text")
        self.switch_row(box, "sms_alerts_only", "Only alarms",
                        "Safety alarms, disconnects and stuck jobs")
        self.entry_row(box, "twilio_sid", "Account SID", secret=True)
        self.entry_row(box, "twilio_token", "Auth Token", secret=True)
        self.entry_row(box, "twilio_from", "From (your Twilio number, e.g. +15551234567)")
        self.entry_row(box, "sms_to", "To (your mobile)")
        self.channel_test(box, "SMS")

        box = self.section(body, "Camera snapshot")
        self.switch_row(box, "snapshot_on", "Photo when a job finishes",
                        "Sent with the message to Telegram, Discord and email")
        self.menu_row(box, "camera_index", "Camera", None,
                      {i: f"Camera {i + 1}" for i in range(4)}, width=110)
        self.note(box, "Ready." if camera.available() else
                  "Camera support isn't installed. Run: pip install opencv-python-headless",
                  pady=(4, 0))
        self.channel_test(box, "Camera")

    def _build_safety_page(self):
        """Alerts & safety page: long or unusual jobs, long pauses, "laser
        free", unattended runs, and the safety sensors with what the board's
        button does. The pin names (A1-A5) match laser_stack_light.ino."""
        p, body = self.page_frame("Alerts & safety")
        self.subpages["safety"] = p
        mins = lambda vals: {v: (f"{v} min" if v else "Off") for v in vals}
        box = self.section(body, "Long jobs")
        self.menu_row(box, "alert_long_run_min", "Alert when a job runs longer than", None,
                      mins((0, 15, 30, 60, 120, 240)), width=100)
        self.switch_row(box, "alert_unusual", "…or 3× longer than usual",
                        "Compared with earlier runs of the same file")
        self.menu_row(box, "alert_long_pause_min", "Alert when paused longer than", None,
                      mins((0, 5, 10, 15, 30, 60)), width=100)
        box = self.section(body, "Laser free")
        self.switch_row(box, "notify_free", "Tell me when the laser is free",
                        "After a job, once it has stood idle for a while")
        self.menu_row(box, "free_after_min", "Idle for", None,
                      {v: f"{v} min" for v in (1, 3, 5, 10, 15)}, width=100)
        box = self.section(body, "Unattended runs")
        self.menu_row(box, "unattended_min", "Warn when nobody touched the PC for", None,
                      mins((0, 5, 10, 15, 30)), width=100)
        self.note(box, "Beeps on the Arduino / Wi-Fi light and sends a message while the laser "
                       "keeps running.")
        box = self.section(body, "Sensors")
        self.note(box, "Wire them to the Arduino (A1–A5) or Wi-Fi light as shown in Help "
                       "› Wiring, then switch on the ones you fitted.", pady=(10, 0))
        self.switch_row(box, "sensor_fire", "Fire / smoke sensor (A2)",
                        "The board sounds its alarm by itself, even if the PC is off")
        relay_names = {-1: "Nothing"}
        relay_names.update({i: r["name"] for i, r in enumerate(self.s["relays"]) if r["use"]})
        self.menu_row(box, "fire_relay_off", "During a fire alarm, switch off", None,
                      relay_names, width=130)
        self.switch_row(box, "sensor_temp", "Temperature sensor (A3)", None)
        self.menu_row(box, "temp_type", "Sensor type", None, {"LM35": "LM35", "TMP36": "TMP36"},
                      width=100)
        self.menu_row(box, "temp_max", "Alarm above", None,
                      {v: f"{v} °C" for v in (30, 35, 40, 45, 50, 55, 60, 70)}, width=100)
        self.switch_row(box, "sensor_door", "Lid / door switch (A4)", "Alert if opened mid-job")
        self.switch_row(box, "sensor_flow", "Exhaust / cooling flow switch (A5)",
                        "Alarm if there's no flow while running")
        actions = {"none": "Silences alarms only", "mute": "Mute for 1 hour"}
        actions.update({f"relay:{i}": f"Toggle {r['name']}"
                        for i, r in enumerate(self.s["relays"]) if r["use"]})
        self.menu_row(box, "button_action", "Button (A1)", "Always silences an alarm first",
                      actions, width=150)
        self.sensor_status = self.note(box, "", pady=(4, 4))
        self.note(box, "Sensor alerts are an extra aid, not a certified safety system. Never "
                       "leave a running laser unattended.", pady=(0, 10))

    def _build_remote_page(self):
        """Phone & Wi-Fi page: the phone dashboard (port, PIN, firewall) and the
        ESP32 Wi-Fi light's address."""
        p, body = self.page_frame("Phone & Wi-Fi")
        self.subpages["remote"] = p
        box = self.section(body, "Phone dashboard")
        self.switch_row(box, "dash_on", "Phone dashboard",
                        "Live status and relay buttons in your phone's browser",
                        on_change=self._dash_changed)
        self.entry_row(box, "dash_port", "Port", kind=int)
        self.entry_row(box, "dash_pin", "PIN (optional, numbers)", secret=True)
        r = ctk.CTkFrame(box, fg_color="transparent")
        r.pack(fill="x", padx=14, pady=(8, 0))
        self.action_button(r, "Apply", self._dash_changed, 80).pack(side="left")
        self.fw_btn = self.action_button(r, "Allow on Wi-Fi", self.allow_firewall, 120)
        self.fw_btn.pack(side="left", padx=6)
        self.dash_status = self.note(box, "", pady=(6, 0))
        self.fw_status = self.note(box, "", pady=(4, 0))
        self.note(box, ("Your phone must be on the same Wi-Fi as this PC. Windows Firewall "
                        "blocks phones until you press Allow on Wi-Fi once (it asks for "
                        "permission)." if IS_WIN else
                        "Your phone must be on the same Wi-Fi as this Mac. The first time, "
                        "macOS asks whether Beacon may accept incoming connections and find "
                        "devices on the local network: allow both."),
                  pady=(4, 10))
        self._fw_ok = None
        self.check_firewall()
        box = self.section(body, "Wi-Fi light (ESP32)")
        self.switch_row(box, "wifi_on", "Send status to the Wi-Fi light",
                        "Stack light, buzzer, relays and sensors over Wi-Fi")
        self.entry_row(box, "wifi_host", "Light's address")
        self.note(box, "beacon-light.local works on most networks. If not, use the IP address "
                       "the light shows in the Arduino IDE's Serial Monitor.", pady=(4, 0))
        self.wifi_status = self.note(box, "", pady=(4, 10))

    def refresh_subpage_status(self):
        """Live lines on the open subpage (called from render): dashboard
        addresses and Wi-Fi light status, live sensor readings, or the smart
        plugs' status on the Relay buttons page."""
        if self.page == "remote":
            self.wifi_status.configure(text=self.netlight.status if self.s["wifi_on"] else "")
            if self.dash.running():
                self.dash_status.configure(text="Open on your phone:" + "\n" +
                                           "\n".join(self.dash.urls()), text_color=MUTED)
            else:
                self.dash_status.configure(text=self.dash.error, text_color=RED)
        elif self.page == "safety":
            inp = getattr(self, "live_inputs", None)
            if not inp:
                text = "No sensor readings: connect the Arduino or Wi-Fi light."
            else:
                parts = [f"Fire: {'ALARM' if inp['fire'] else 'ok'}",
                         f"Lid: {'open' if inp['door_open'] else 'closed'}",
                         f"Flow: {'yes' if inp['flow'] else 'no'}"]
                if self.s["sensor_temp"] and getattr(self, "temp_c", None) is not None:
                    parts.insert(1, f"{self.temp_c:.1f} °C")
                text = "Live" + ":  " + "   ".join(parts)
            self.sensor_status.configure(text=text)
        elif self.page == "relays":
            for i, lbl in self._plug_status_lbls.items():
                lbl.configure(text=self.plugs.status.get(i, "Waiting…"))

    # ================================================================ wizard
    # The setup assistant. Each step is a method (_wiz_welcome, _wiz_lightburn,
    # ...) that fills wiz_body with text and real controls (the same helpers and
    # settings as the Settings page, so a choice made here is the same setting).
    # wiz_show() clears the body and calls the current step. To add a step,
    # write a method and add it to wiz_steps. A step can create a live status
    # line with _wiz_live_label(); update_wizard_live() keeps it current.
    def build_wizard_page(self):
        """The setup assistant: a short page per feature, most important first,
        with the real controls on each page. Shown on first run and from
        Settings › Help & updates › Setup assistant."""
        p = self.wizard_page = ctk.CTkFrame(self.root, fg_color="transparent")
        self.wiz_title = ctk.CTkLabel(p, text="", font=self.font(19, "bold"), text_color=TEXT,
                                      anchor="w")
        self.wiz_title.pack(fill="x", padx=22, pady=(20, 2))
        nav = ctk.CTkFrame(p, fg_color="transparent")
        nav.pack(side="bottom", fill="x", padx=18, pady=12)
        self.wiz_back = self.action_button(nav, "Back", lambda: self.wiz_go(-1), 80)
        self.wiz_back.pack(side="left")
        self.wiz_next = ctk.CTkButton(nav, text="Next", width=110, height=32, corner_radius=10,
                                      font=self.font(13, "bold"), command=lambda: self.wiz_go(1))
        self.wiz_next.pack(side="right")
        self.wiz_dots = ctk.CTkLabel(nav, text="", text_color=MUTED, font=self.font(12))
        self.wiz_dots.pack(side="top")
        self.wiz_body = ctk.CTkScrollableFrame(p, fg_color="transparent")
        self.wiz_body.pack(fill="both", expand=True, padx=10)
        self.wiz_step = 0
        self.wiz_steps = [self._wiz_welcome, self._wiz_lightburn, self._wiz_stacklight,
                          self._wiz_relays, self._wiz_telegram, self._wiz_notifications,
                          self._wiz_reports, self._wiz_test, self._wiz_support]
        self.wiz_live = None
        self.wiz_show()

    def wiz_go(self, delta):
        """Next (+1) / Back (-1). Past the last step = Finish: leave test mode,
        mark setup as done (so it isn't shown at the next start) and go Home."""
        if self.wiz_step + delta >= len(self.wiz_steps):
            if self.test_mode:
                self.set_test(False)            # leave test mode on Finish
            self.s["setup_done"] = True
            save_settings(self.s)
            self.show_page("status")
            return
        self.wiz_step = max(0, self.wiz_step + delta)
        self.wiz_show()

    def wiz_show(self):
        """Draw the current step and update the buttons and "3 of 9" counter."""
        for w in self.wiz_body.winfo_children():
            w.destroy()
        self.wiz_live = None
        self.wiz_steps[self.wiz_step]()
        last = self.wiz_step == len(self.wiz_steps) - 1
        if DEV:                                 # dev: a read-only preview of the pages
            if not last:                        # the thank-you page has no controls to explain
                self._wiz_text("Dev mode: a preview. The buttons and switches on these pages "
                               "are turned off.", color=MUTED, size=11, pady=(10, 4))
            self._wiz_disable(self.wiz_body)
        self.wiz_next.configure(text="Finish" if last else "Next")
        self.wiz_back.configure(state="normal" if self.wiz_step else "disabled")
        self.wiz_dots.configure(text=f"{self.wiz_step + 1} of {len(self.wiz_steps)}")
        self.wiz_body._parent_canvas.yview_moveto(0)

    # -- building blocks
    def _wiz_disable(self, parent):
        """Grey out every control on a setup page (dev mode), so the pages can be
        read and their layout checked without changing settings, running a
        test job or opening links. Back / Next stay outside wiz_body; a control
        marked wiz_keep (the tip and e-Transfer Copy buttons) stays on."""
        for w in parent.winfo_children():
            if getattr(w, "wiz_keep", False):
                continue
            if isinstance(w, (ctk.CTkButton, ctk.CTkSwitch, ctk.CTkCheckBox,
                              ctk.CTkSegmentedButton, ctk.CTkComboBox, ctk.CTkEntry)):
                w.configure(state="disabled")
            else:
                self._wiz_disable(w)

    def _wiz_text(self, text, color=TEXT, size=12, bold=False, pady=(2, 6)):
        """A wrapped paragraph in the setup assistant."""
        ctk.CTkLabel(self.wiz_body, text=text, font=self.font(size, "bold" if bold else "normal"),
                     text_color=color, anchor="w", justify="left",
                     wraplength=310).pack(fill="x", padx=8, pady=pady)

    def _wiz_card(self, top=4):
        """A card to hold controls in the setup assistant; top = space above it."""
        box = ctk.CTkFrame(self.wiz_body, fg_color=CARD, corner_radius=14, border_width=1,
                           border_color=CARD_BORDER)
        box.pack(fill="x", padx=4, pady=(top, 6))
        return box

    def _wiz_live_label(self):
        """A bold live status line (e.g. "Beacon can see LightBurn: Ready")."""
        packed = self.wiz_body.pack_slaves()
        self.wiz_live_after = packed[-1] if packed else None    # where to re-show it
        self.wiz_live = ctk.CTkLabel(self.wiz_body, text="", font=self.font(12, "bold"),
                                     text_color=TEXT, anchor="w", justify="left", wraplength=310)
        self.wiz_live.pack(fill="x", padx=8, pady=(12, 8))
        self.update_wizard_live()

    # -- the steps
    def _wiz_welcome(self):
        """Step: welcome and the feature list."""
        self.wiz_title.configure(text=f"Welcome to {APP_NAME}")
        self._wiz_text(f"{APP_NAME} shows what your laser is doing (Running, Paused, Framing, "
                       "Ready) on your screen, on a stack light and on your phone.",
                       color=TEXT, size=13)
        for line in ("Reads LightBurn's status: from the screen, LightBurn's API, or both",
                     "Drives a red / green stack light and buzzer on an Arduino",
                     "Relay buttons for a fan, air assist, exhaust and more",
                     f"Messages on Telegram, {OS_NAME} pop-ups and more",
                     "Run-time reports, job statistics, costs and quotes"):
            self._wiz_text("•  " + line, color=TEXT, pady=(1, 1))
        self._wiz_text("The next pages show each feature and where to set it up in "
                       "Settings.", pady=(10, 4))

    def _wiz_lightburn(self):
        """Step 1: how LightBurn is read, with the mode switch and a live check
        that Beacon can see it."""
        self.wiz_title.configure(text="1  ·  Reading LightBurn")
        self._wiz_text("Screen (OCR)", color=TEXT, bold=True, pady=(4, 0))
        self._wiz_text("Beacon looks at LightBurn's Laser panel and reads the status text, the "
                       "way you would. Works with every LightBurn version, including older than "
                       "2.2. LightBurn can sit behind other windows, but not be minimised.")
        self._wiz_text("LightBurn API (2.2+)", color=TEXT, bold=True, pady=(4, 0))
        self._wiz_text("LightBurn tells Beacon its status directly, so it keeps working while "
                       "LightBurn is minimised or covered.")
        self._wiz_text("Both: recommended for LightBurn 2.2+", color=TEXT, bold=True, pady=(4, 0))
        self._wiz_text("Beacon reads both and takes whichever notices a change first, for the "
                       "most accurate times; when LightBurn is minimised it carries on with the "
                       "API alone.")
        seg = ctk.CTkSegmentedButton(self.wiz_body, values=list(self.DETECT_MODES.values()),
                                     command=self._wiz_set_mode, font=self.font(12))
        seg.set(self.DETECT_MODES[self.s["detect_mode"]])
        seg.pack(fill="x", padx=8, pady=(8, 4))
        self._wiz_text("The mode is chosen in Settings › Detection. With API or Both, LightBurn "
                       "asks once whether to allow Beacon.", size=11, pady=(0, 2))
        self._wiz_live_label()
        self.action_button(self.wiz_body, "Look again", self.find_lightburn_again, 110).pack(
            anchor="w", padx=8, pady=(2, 8))

    def _wiz_set_mode(self, label):
        """The mode switch in the assistant: same as Settings › Detection."""
        self.set_detect_mode(label)
        if hasattr(self, "detect_seg"):         # keep Settings' switch in step
            self.detect_seg.set(self.DETECT_MODES[self.s["detect_mode"]])

    def _wiz_stacklight(self):
        """Step 2: Arduino stack light wiring, upload and COM port."""
        self.wiz_title.configure(text="2  ·  Stack light")
        self._wiz_text("Optional. An Arduino Uno switches a red / green stack light (red = "
                       "running, green = ready) and beeps when a job ends.")
        for line in ("Wiring: red on D10, green on D9, buzzer on D8 (diagram in Help › "
                     "Wiring).",
                     "Sketch: laser_stack_light, in Beacon's install folder › Arduino, uploaded "
                     "with the Arduino IDE.",
                     "Connection: USB. Beacon finds the COM port itself, or it can be picked "
                     "in Settings › Arduino light."):
            self._wiz_text("•  " + line, color=TEXT, pady=(1, 3))
        box = self._wiz_card(top=12)
        self.switch_row(box, "arduino", "Send status to Arduino", None,
                        on_change=lambda: setattr(self.arduino, "enabled", self.s["arduino"]))
        r = self.row(box, "COM port")
        ports = ["Auto-detect"] + [dev for dev, _ in list_ports()]
        if self.s["port"] not in ports:
            ports.append(self.s["port"])
        combo = ctk.CTkComboBox(r, values=ports, width=140, command=self.set_port,
                                font=self.font(12), dropdown_font=self.font(12))
        combo.set(self.s["port"])
        combo.pack(side="right")

        def refresh():
            """Re-list the COM ports in the assistant's dropdown."""
            values = ["Auto-detect"] + [dev for dev, _ in list_ports()]
            combo.configure(values=values)
        ctk.CTkButton(r, text="↻", width=30, height=28, corner_radius=8,
                      fg_color="transparent", hover_color=CARD_BORDER, text_color=MUTED,
                      font=self.font(14), command=refresh).pack(side="right", padx=4)
        self._wiz_live_label()

    def _wiz_relays(self):
        """Step 3: what relay buttons are, with a link to set them up."""
        self.wiz_title.configure(text="3  ·  Relay buttons")
        self._wiz_text("Relay buttons switch extra things from Beacon: a fan, air assist, "
                       "exhaust or lights. They use relays on the Arduino, or smart plugs "
                       "(Shelly, Tasmota, Kasa, Home Assistant).")
        self._wiz_text("Each button can follow the laser by itself (for example the exhaust "
                       "on while running and off 2 minutes after) or be switched by hand on "
                       "the main screen.")
        self.action_button(self.wiz_body, "Set up relay buttons  ›",
                           self._wiz_open_relays, 180).pack(anchor="w", padx=8, pady=(8, 4))
        self._wiz_text("Optional. Buttons are added in Settings › Relay buttons.", size=11)

    def _wiz_open_relays(self):
        """Open Relay buttons from the assistant; Back returns to it."""
        self.open_relays("wizard")

    def open_relays(self, back_to):
        """Open Relay buttons; its Back button returns to back_to."""
        self.relays_return = back_to
        self.show_page("relays")

    def _wiz_telegram(self):
        """Step 4: Telegram bot token, Chat ID (Find), test and commands. Uses
        the same text variables as Settings, so both show the same values."""
        self.wiz_title.configure(text="4  ·  Telegram bot")
        self._wiz_text("Optional. A message on your phone when a job finishes, pauses or the "
                       "laser disconnects, and /status from anywhere.")
        self._wiz_text("•  The bot comes from @BotFather in Telegram (/newbot), which gives "
                       "its token.", color=TEXT, pady=(1, 3))
        self._wiz_text("•  The token goes in Settings › Telegram bot. After one message to the "
                       "bot, Find fills in the Chat ID.", color=TEXT, pady=(1, 3))
        box = self._wiz_card(top=12)
        ctk.CTkLabel(box, text="Bot token", font=self.font(11), text_color=MUTED,
                     anchor="w").pack(fill="x", padx=14, pady=(8, 0))
        ctk.CTkEntry(box, textvariable=self.tg_token_var, show="•", height=30,
                     font=self.font(12)).pack(fill="x", padx=14)
        ctk.CTkLabel(box, text="Chat ID", font=self.font(11), text_color=MUTED,
                     anchor="w").pack(fill="x", padx=14, pady=(6, 0))
        row = ctk.CTkFrame(box, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(0, 4))
        ctk.CTkButton(row, text="Find", width=64, height=30, corner_radius=8,
                      font=self.font(12), command=self.tg_find_chat).pack(side="right")
        self.action_button(row, "Send test", self.send_test_notification, 90).pack(
            side="right", padx=6)
        ctk.CTkEntry(row, textvariable=self.tg_chat_var, height=30, placeholder_text="Chat ID",
                     font=self.font(12)).pack(side="left", fill="x", expand=True)
        self.switch_row(box, "tg_commands", "Telegram commands",
                        "/status, /today, /relays and more from your phone")
        self._wiz_live_label()

    def _wiz_notifications(self):
        """Step 5: Windows pop-ups, the main events, quiet hours, and how Beacon
        avoids nagging."""
        self.wiz_title.configure(text="5  ·  Notifications")
        self._wiz_text("Each event can be switched on or off in Settings › Notifications. "
                       f"{OS_NAME} pop-ups and Telegram both follow these choices.")
        box = self._wiz_card()
        self.switch_row(box, "notify_pc", f"{OS_NAME} pop-ups", None,
                        on_change=self._pc_notify_changed)
        for key, label in NOTIFY_EVENTS[:5]:
            var = ctk.BooleanVar(value=bool(self.s[f"notify_{key}"]))
            ctk.CTkCheckBox(box, text=label, variable=var, font=self.font(12),
                            checkbox_width=20, checkbox_height=20,
                            command=lambda k=key, v=var: self._set_flag(f"notify_{k}", v)
                            ).pack(fill="x", padx=14, pady=3)
        self.switch_row(box, "quiet_on", "Quiet hours", "22:00 to 07:00 by default")
        self._wiz_text("Built not to nag: if the same alert repeats more than 3 times in a "
                       "minute, Beacon sends one warning and pauses it; quiet hours let only "
                       "safety alarms through; and you can mute for an hour from the pill or "
                       "tray icon.", size=11, pady=(2, 6))

    def _wiz_reports(self):
        """Step 6: daily / weekly reports and what Statistics offers."""
        self.wiz_title.configure(text="6  ·  Reports & quotes")
        self._wiz_text("Beacon counts only real running time (pauses left out) and can send "
                       "a report by Telegram or Windows pop-up, switched on in Settings › "
                       "Usage reports:")
        box = self._wiz_card()
        for kind, label, sub in (("daily", "Daily report", "The last 24 hours, each day"),
                                 ("weekly", "Weekly report", "The last 7 days, day by day")):
            r = self.row(box, label, sub)
            var = ctk.BooleanVar(value=bool(self.s[f"report_{kind}"]))
            ctk.CTkSwitch(r, text="", variable=var, onvalue=True, offvalue=False, width=46,
                          command=lambda k=kind, v=var: self._report_toggled(k, v)).pack(
                side="right")
        self._wiz_text("The Statistics page (chart button on the main screen) shows run time "
                       "per day, every job with its customer and material, what jobs cost, a "
                       "quote calculator, maintenance reminders, and monthly PDF / Excel "
                       "reports.", pady=(4, 6))

    def _wiz_test(self):
        """Step 7: test mode and a 10 s test job."""
        self.wiz_title.configure(text="7  ·  Test mode")
        self._wiz_text("Test mode fakes LightBurn's status, so the light, relays and "
                       "notifications can be tried without the laser. A test job runs for "
                       "10 seconds. Test jobs never count in the statistics.")
        self.action_button(self.wiz_body, "Run a 10 s test job", self.toggle_demo_job,
                           170).pack(anchor="w", padx=8, pady=(8, 4))
        self._wiz_text("Test mode and its buttons (Paused, Framing, Disconnected…) are in "
                       "Settings › Test mode.", size=11)

    def _wiz_support(self):
        """Last page: thanks, a tip button when TIP_URL is set and the e-Transfer
        address (with Copy) when ETRANSFER_EMAIL is set."""
        self.wiz_title.configure(text="Thank you!")
        self._wiz_text(f"{APP_NAME} is free and its source code is public, so anyone can use "
                       "it and help improve it.", color=TEXT, size=13)
        self._wiz_text("If you enjoy the app and it helps with your work, please support the "
                       "developer with a $5 tip (or any amount). It helps keep development "
                       "going.",
                       pady=(6, 8))
        if TIP_URL:
            tip = ctk.CTkButton(self.wiz_body, text=f"♥  {TIP_LABEL}", width=220, height=34,
                                corner_radius=10, font=self.font(13, "bold"),
                                command=lambda: webbrowser.open(TIP_URL))
            tip.pack(anchor="w", padx=8, pady=(2, 6))
            tip.wiz_keep = True                 # works in the dev preview too
        if ETRANSFER_EMAIL:
            self._wiz_text("Interac e-Transfer (Canada)", bold=True, pady=(8, 0))
            row = ctk.CTkFrame(self.wiz_body, fg_color="transparent")
            row.pack(fill="x", padx=8, pady=(0, 6))
            ctk.CTkLabel(row, text=ETRANSFER_EMAIL, font=self.font(12), text_color=TEXT,
                         anchor="w").pack(side="left")
            copied = ctk.CTkLabel(row, text="", font=self.font(11), text_color=MUTED)

            def copy():
                """Copy the e-Transfer address to the clipboard."""
                self.root.clipboard_clear()
                self.root.clipboard_append(ETRANSFER_EMAIL)
                copied.configure(text="Copied")
            copy_btn = self.action_button(row, "Copy", copy, 60)
            copy_btn.pack(side="left", padx=8)
            copy_btn.wiz_keep = True            # works in the dev preview too
            copied.pack(side="left")
        self._wiz_text("These pages can be opened again from Settings › Help & updates.",
                       size=11, pady=(6, 4))

    def update_wizard_live(self):
        """The live line on a setup page (called every tick while it's shown)."""
        if not self.wiz_live:
            return
        step = self.wiz_steps[self.wiz_step]
        color = TEXT
        if step == self._wiz_lightburn:
            if self.state is None:
                text = "⏳  " + (self.last_msg.get("note") or "Looking for LightBurn…")
            else:
                text = "✅  Beacon can see LightBurn: " + look(self.state)[1]
            if self.s["detect_mode"] != "ocr" and hasattr(self, "api_status"):
                api = self.api_status.cget("text")
                if api and api.strip() not in text:     # skip it when it repeats the line above
                    text += "\n" + api
        elif step == self._wiz_stacklight:
            text = ("Arduino: " + self.arduino.status) if self.s["arduino"] else ""
            color = MUTED
        elif step == self._wiz_telegram:
            text, color = self.tg_status.cget("text"), MUTED
        else:
            text = ""
        if self.wiz_live.cget("text") != text:
            self.wiz_live.configure(text=text, text_color=color)
        # An empty line would leave a gap: hide it, and put it back in place when
        # there is something to show.
        shown = self.wiz_live.winfo_manager() == "pack"
        if not text and shown:
            self.wiz_live.pack_forget()
        elif text and not shown:
            where = {"after": self.wiz_live_after} if self.wiz_live_after else {}
            self.wiz_live.pack(fill="x", padx=8, pady=(12, 8), **where)

    # ============================================================ help page
    def build_help_page(self):
        """Help page: Arduino wiring diagram, ESP32 pins, the FAQ (HELP_FAQ) and
        support tools (copy debug info, save diagnostics, open settings
        folder)."""
        p, body = self.page_frame("Help", back="status")
        self.help_page = p
        box = self.section(body, "Wiring (Arduino Uno)")
        self._draw_wiring(box)
        self.note(box, "Relay boards: VCC to 5 V (or their own 5 V supply for more than 2–3 "
                       "relays), GND to GND. Sensors: switch-type ones go between the pin and GND; "
                       "LM35/TMP36 temperature sensors use 5 V, GND and A3.", pady=(4, 10))
        box = self.section(body, "Wi-Fi light (ESP32)")
        self.note(box, "Sketch: esp32_stack_light. Red GPIO 25, green 26, buzzer 27, buzzer switch "
                       "14, relays 16 17 18 19 21 22 23 32 33, button 4, fire 13, temperature 34 "
                       "(TMP36 on 3.3 V), lid 35, flow 39 (35 and 39 need a 10 k pull-up to "
                       "3.3 V). Put your Wi-Fi name and password at the top of the sketch.",
                  pady=(10, 10))
        box = self.section(body, "Questions")
        for q, a in HELP_FAQ:
            ctk.CTkLabel(box, text=q, font=self.font(12, "bold"), text_color=TEXT, anchor="w",
                         justify="left", wraplength=300).pack(fill="x", padx=14, pady=(10, 0))
            self.note(box, a, pady=(2, 0))
        ctk.CTkFrame(box, height=10, fg_color="transparent").pack()
        box = self.section(body, "Support")
        self.note(box, (f"Email: {SUPPORT_EMAIL}. " if SUPPORT_EMAIL else "When asking for help, ")
                  + "include the debug info or the diagnostics file.", pady=(10, 4))
        r = ctk.CTkFrame(box, fg_color="transparent")
        r.pack(fill="x", padx=14, pady=4)
        self.action_button(r, "Copy debug info", self.copy_debug_info, 130).pack(side="left")
        self.action_button(r, "Save diagnostics…", self.save_diagnostics, 140).pack(
            side="left", padx=6)
        r = ctk.CTkFrame(box, fg_color="transparent")
        r.pack(fill="x", padx=14, pady=4)
        self.action_button(r, "Open settings folder", self.open_settings_folder, 160).pack(
            side="left")
        self.help_status = self.note(box, "", pady=(4, 10))

    def _draw_wiring(self, box):
        """Draw the Arduino Uno wiring as a simple diagram: the board on the
        left, one coloured line per pin to what connects there. Pin numbers
        must match laser_stack_light.ino."""
        s = self.scale
        W, H = int(320 * s), int(330 * s)
        c = tk.Canvas(box, width=W, height=H, highlightthickness=0, bd=0, bg=pick(CARD))
        c.pack(padx=10, pady=10)
        fg, muted = pick(TEXT), pick(MUTED)
        f = (FONT, max(7, int(8 * s)))
        fb = (FONT, max(8, int(9 * s)), "bold")
        ux0, uy0, ux1 = 8 * s, 10 * s, 92 * s
        rows = [("D10", "Red light relay", RED), ("D9", "Green light relay", GREEN),
                ("D8", "Buzzer +", AMBER), ("D4", "Buzzer on/off switch", muted),
                ("D2–D7, D11–D13, A0", "Extra relays (9)", ACCENT[1]),
                ("A1", "Button → GND", muted), ("A2", "Fire / smoke sensor", RED),
                ("A3", "Temperature sensor", AMBER), ("A4", "Lid switch → GND", muted),
                ("A5", "Flow switch → GND", muted), ("GND", "All grounds together", muted),
                ("5V", "Relay boards / sensors VCC", muted)]
        step = (H - 30 * s) / len(rows)
        c.create_rectangle(ux0, uy0, ux1, H - 10 * s, outline=ACCENT[1], width=2)
        c.create_text((ux0 + ux1) / 2, uy0 + 12 * s, text="Arduino\nUno", fill=fg, font=fb,
                      justify="center")
        for n, (pin, what, col) in enumerate(rows):
            y = uy0 + 34 * s + n * step
            c.create_text(ux1 - 4 * s, y, text=pin, fill=fg, font=f, anchor="e")
            c.create_line(ux1, y, ux1 + 40 * s, y, fill=col, width=2)
            c.create_oval(ux1 + 38 * s, y - 3 * s, ux1 + 44 * s, y + 3 * s, fill=col, outline="")
            c.create_text(ux1 + 50 * s, y, text=what, fill=fg, font=f, anchor="w")

    def bring_to_front(self):
        """Show whichever view is in use (pill or window) and raise it."""
        if self.s["compact"]:
            self.restore_pill()
            self.pill.attributes("-topmost", True)
            return
        self.root.deiconify()
        self.root.attributes("-alpha", 1.0)
        if IS_MAC:
            osys.activate_app()                      # macOS only raises the active app
        self.root.attributes("-topmost", True)       # get past Windows' focus rules...
        self.root.lift()
        self.root.focus_force()
        self.root.after(200, lambda: self.root.attributes("-topmost", self.s["topmost"]))

    def listen_for_launches(self, server):
        """Second launches connect here (see claim_single_instance) and ask us
        to come forward instead of opening another copy."""
        def serve():
            """Accept connections forever; each one means "show yourself"."""
            while True:
                try:
                    conn, _ = server.accept()
                except OSError:
                    return
                conn.close()
                self.q.put({"cmd": "show"})
        threading.Thread(target=serve, daemon=True).start()

    def minimize_pill(self):
        """Hide the pill to the taskbar; clicking the taskbar icon brings it back."""
        self._pill_minimized = True
        self.hide_pill_menu()
        self.pill.withdraw()
        self.root.attributes("-alpha", 0.0)    # the main window only lends its taskbar icon
        self.root.iconify()

    def _root_mapped(self, e):
        """The main window was shown (e.g. its taskbar icon clicked)."""
        # Restored from the taskbar while in pill mode: show the pill again
        if e.widget is self.root and self._pill_minimized and self.s["compact"]:
            self.restore_pill()

    def restore_pill(self):
        """Show the pill again after it was minimised to the taskbar."""
        self._pill_minimized = False
        self.root.withdraw()
        self.pill.deiconify()
        self.pill.lift()
        self._pill_key = None

    # Hand control to Tk: from here on everything happens in callbacks (poll,
    # tick, button commands) until quit() destroys the window.
    def run(self):
        self.root.mainloop()


# ===========================================================================
# Charts (drawn on a plain canvas so they match the app and need no extras)
# ===========================================================================
BLUE = "#3B82F6"


def nice_125(target):
    """The smallest "nice" number (1, 2 or 5 times a power of ten) at least
    target: 0.7 -> 1, 13 -> 20, 340 -> 500. Used for chart grid steps."""
    e = 10 ** math.floor(math.log10(target))
    for m in (1, 2, 5, 10):
        if m * e >= target:
            return m * e


def nice_step(vmax, ticks):
    """Distance between chart grid lines for values up to vmax, aiming at about
    4 lines. ticks="dur" (seconds) snaps to clock-friendly steps (1, 5, 10,
    15, 30 min, 1, 2, 3, 6, 12, 24 h); "count" keeps whole numbers; anything
    else (money) uses nice_125."""
    target = vmax / 4 if vmax > 0 else 1
    if ticks == "dur":
        for c in (60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400):
            if c >= target:
                return c
        return nice_125(target / 3600) * 3600
    if ticks == "count":
        return max(1, nice_125(target))
    return nice_125(target)


# A chart is described by a plain dict ("spec") built by StatsView.spec() and
# drawn by the method named after its kind (_bars, _line, ...). Common keys:
# values, labels (under the x axis), tips (hover text per value), color, fmt
# (value -> text), axis (grid value -> text), ticks (see nice_step). Every drawn
# element registers a hover rectangle in self.hits for the tooltip.
class Chart(tk.Canvas):
    """spec kinds: bars / line (labels, tips, values), heatmap (rows, cols,
    col_tips, matrix), donut (parts, center, center_sub), empty (text)."""

    def __init__(self, master, scale):
        super().__init__(master, highlightthickness=0, bd=0, height=200)
        self.s = scale
        self.spec = None
        self.hits = []              # (x0, y0, x1, y1, tooltip)
        self.f = (FONT, 9)
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Motion>", self._hover)
        self.bind("<Leave>", lambda e: self.delete("tip"))

    def show(self, spec):
        """Draw a new spec."""
        self.spec = spec
        self.redraw()

    def redraw(self):
        """Draw the current spec at the canvas's current size (also on resize
        and theme change). Too small to draw: leave it blank."""
        self.delete("all")
        self.hits = []
        card = pick(CARD)
        self.configure(bg=card)
        w, h = self.winfo_width(), self.winfo_height()
        if w > 60 and h > 60 and self.spec:
            getattr(self, "_" + self.spec["kind"])(self.spec, w, h, card)

    def _empty(self, sp, w, h, card):
        """No data: a centred message instead of a chart."""
        self.create_text(w / 2, h / 2, text=sp["text"], fill=pick(MUTED), font=(FONT, 11),
                         width=w * 0.7, justify="center")

    def _axes(self, sp, w, h):
        """Draw the horizontal grid lines and their labels. Returns the plot
        area (left, right, top, bottom) and the value at the top line."""
        s = self.s
        L, R, T, B = 66 * s, w - 16 * s, 16 * s, h - 32 * s
        vmax = max(sp["values"]) if sp["values"] else 0
        step = nice_step(vmax, sp["ticks"])
        top = max(step, math.ceil(vmax / step - 1e-9) * step)
        k = 0
        while k * step <= top + 1e-9:
            y = B - k * step / top * (B - T)
            self.create_line(L, y, R, y, fill=pick(CARD_BORDER))
            self.create_text(L - 8 * s, y, text=sp["axis"](k * step), anchor="e",
                             fill=pick(MUTED), font=self.f)
            k += 1
        return L, R, T, B, top

    def _xlabels(self, labels, L, R, B):
        """Labels under the x axis; when there are too many to fit (about one
        per 56 px), only every n-th is drawn."""
        n = len(labels)
        slot = (R - L) / n
        every = max(1, math.ceil(n / max(1.0, (R - L) / (56 * self.s))))
        for i, text in enumerate(labels):
            if i % every == 0:
                self.create_text(L + slot * (i + 0.5), B + 15 * self.s, text=text,
                                 fill=pick(MUTED), font=self.f)

    def _bars(self, sp, w, h, card):
        """Bar chart: one bar per value, centred in its slot."""
        L, R, T, B, top = self._axes(sp, w, h)
        vals = sp["values"]
        slot = (R - L) / len(vals)
        bw = max(1.0, slot * 0.68)
        for i, v in enumerate(vals):
            x0 = L + slot * i + (slot - bw) / 2
            if v > 0:
                self.create_rectangle(x0, B - v / top * (B - T), x0 + bw, B,
                                      fill=sp["color"], outline="")
            self.hits.append((L + slot * i, T, L + slot * (i + 1), B,
                              f"{sp['tips'][i]}\n{sp['fmt'](v)}"))
        self._xlabels(sp["labels"], L, R, B)

    def _line(self, sp, w, h, card):
        """Line chart with a lightly filled area under it (cumulative charts).
        Dots mark the points while there are few enough to tell apart."""
        L, R, T, B, top = self._axes(sp, w, h)
        vals = sp["values"]
        slot = (R - L) / len(vals)
        pts = [(L + slot * (i + 0.5), B - v / top * (B - T)) for i, v in enumerate(vals)]
        flat = [c for p in [(pts[0][0], B)] + pts + [(pts[-1][0], B)] for c in p]
        self.create_polygon(flat, fill=blend(card, sp["color"], 0.18), outline="")
        if len(pts) > 1:
            self.create_line([c for p in pts for c in p], fill=sp["color"], width=2 * self.s)
        if len(pts) <= 45:
            r = 3 * self.s
            for x, y in pts:
                self.create_oval(x - r, y - r, x + r, y + r, fill=sp["color"], outline="")
        for i, v in enumerate(vals):
            self.hits.append((L + slot * i, T, L + slot * (i + 1), B,
                              f"{sp['tips'][i]}\n{sp['fmt'](v)}"))
        self._xlabels(sp["labels"], L, R, B)

    def _heatmap(self, sp, w, h, card):
        """Day-of-week by hour grid: the stronger the colour, the more running
        in that hour. Empty cells are a faint grey."""
        s = self.s
        L, R, T, B = 48 * s, w - 16 * s, 16 * s, h - 32 * s
        m = sp["matrix"]
        vmax = max(max(row) for row in m) or 1
        cw, ch = (R - L) / len(sp["cols"]), (B - T) / len(sp["rows"])
        empty = blend(card, pick(MUTED), 0.08)
        for r, row_label in enumerate(sp["rows"]):
            self.create_text(L - 8 * s, T + ch * (r + 0.5), text=row_label, anchor="e",
                             fill=pick(MUTED), font=self.f)
            for c, v in enumerate(m[r]):
                x0, y0 = L + cw * c, T + ch * r
                fill = blend(card, sp["color"], 0.15 + 0.85 * v / vmax) if v > 0 else empty
                self.create_rectangle(x0 + 1, y0 + 1, x0 + cw - 1, y0 + ch - 1, fill=fill,
                                      outline="")
                self.hits.append((x0, y0, x0 + cw, y0 + ch,
                                  f"{row_label} {sp['col_tips'][c]}\n{sp['fmt'](v)}"))
        for c, text in enumerate(sp["cols"]):
            if c % 3 == 0:
                self.create_text(L + cw * (c + 0.5), B + 15 * s, text=text, fill=pick(MUTED),
                                 font=self.f)

    def _donut(self, sp, w, h, card):
        """Donut chart with a legend on the right (the cost breakdown). Slices
        start at the top and go clockwise; a single 100% slice is drawn as a
        full circle because Tk can't draw a 360° arc."""
        s = self.s
        parts = [p for p in sp["parts"] if p[1] > 0]
        total = sum(v for _, v, _ in parts)
        size = min(h - 40 * s, w * 0.42)
        cx, cy = 30 * s + size / 2, h / 2
        box = (cx - size / 2, cy - size / 2, cx + size / 2, cy + size / 2)
        start = 90.0
        for label, v, color in parts:
            extent = -360.0 * v / total
            if abs(extent) > 359.9:
                self.create_oval(*box, fill=color, outline="")
            else:
                self.create_arc(*box, start=start, extent=extent, fill=color, outline=card,
                                width=2, style="pieslice")
            start += extent
        hole = size * 0.31
        self.create_oval(cx - hole, cy - hole, cx + hole, cy + hole, fill=card, outline="")
        self.create_text(cx, cy - 8 * s, text=sp["center"], fill=pick(TEXT),
                         font=(FONT, 13, "bold"))
        self.create_text(cx, cy + 12 * s, text=sp["center_sub"], fill=pick(MUTED), font=self.f)
        x = cx + size / 2 + 36 * s
        y = cy - len(parts) * 15 * s
        for label, v, color in parts:
            self.create_rectangle(x, y - 6 * s, x + 12 * s, y + 6 * s, fill=color, outline="")
            self.create_text(x + 20 * s, y, anchor="w", fill=pick(TEXT), font=(FONT, 10),
                             text=f"{label}   {sp['fmt'](v)}  ({v / total:.0%})")
            y += 30 * s

    def _hover(self, e):
        """Show the tooltip of the element under the mouse, kept inside the
        canvas (flipped left / below near the edges)."""
        self.delete("tip")
        for x0, y0, x1, y1, text in self.hits:
            if x0 <= e.x < x1 and y0 <= e.y < y1:
                pad = 6 * self.s
                t = self.create_text(0, 0, text=text, anchor="nw", fill=pick(TEXT),
                                     font=self.f, tags="tip")
                bx0, by0, bx1, by1 = self.bbox(t)
                tw, th = bx1 - bx0, by1 - by0
                x, y = e.x + 14 * self.s, e.y - th - 2 * pad - 6 * self.s
                if x + tw + 2 * pad > self.winfo_width():
                    x = e.x - tw - 2 * pad - 14 * self.s
                if y < 0:
                    y = e.y + 16 * self.s
                self.coords(t, x + pad, y + pad)
                self.create_rectangle(x, y, x + tw + 2 * pad, y + th + 2 * pad,
                                      fill=pick(BG), outline=pick(CARD_BORDER), tags="tip")
                self.tag_raise(t)
                return


# ===========================================================================
# Statistics window
# ===========================================================================
# Statistics choices. Chart menus map a label to (kind, grouping) used by
# StatsView.spec(): "time" = bars per day/week/month, "cumulative" = running
# total line, "hours" / "weekdays" = totals by hour of day / day of week,
# "heatmap", "jobs" (count per day), "breakdown" (cost donut).
RANGES = {"Last 7 days": 7, "Last 30 days": 30, "Last 90 days": 90,
          "Last 12 months": 365, "All time": None}
USAGE_CHARTS = {"Run time per day": ("time", "day"), "Run time per week": ("time", "week"),
                "Run time per month": ("time", "month"),
                "Cumulative run time": ("cumulative", None), "Time of day": ("hours", None),
                "Day of week": ("weekdays", None), "Heatmap (day × hour)": ("heatmap", None),
                "Jobs per day": ("jobs", None)}
COST_CHARTS = {"Cost per day": ("time", "day"), "Cost per week": ("time", "week"),
               "Cost per month": ("time", "month"), "Cumulative cost": ("cumulative", None),
               "Cost by time of day": ("hours", None), "Cost by day of week": ("weekdays", None),
               "Cost breakdown per hour": ("breakdown", None)}
COST_FIELDS = [  # key, label, unit, hint
    ("currency", "Currency symbol", "", None),
    ("power_kw", "Power draw", "kW", "Laser, chiller and exhaust together"),
    ("kwh_price", "Electricity price", "per kWh", None),
    ("machine_price", "Machine price", "", "Spread over its lifetime as wear"),
    ("lifetime_h", "Expected lifetime", "hours", "Run hours before replacement"),
    ("maint_h", "Maintenance & parts", "per hour", None),
    ("labor_h", "Operator / labour", "per hour", None),
    ("other_h", "Other costs", "per hour", None),
]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def fmt_axis_dur(v):
    """Grid label for seconds: "0", "30 m", "1.5 h"."""
    if v == 0:
        return "0"
    return f"{v / 3600:g} h" if v >= 3600 else f"{v / 60:g} m"


# Six tabs: Usage, Costs, Jobs, Customers, Quote, Maintenance. Everything is
# computed from RunLog's jobs. While the page is open, refresh() runs every 3 s
# so the numbers follow a running job; charts and lists are only redrawn when
# what they show changed (see the signatures in self._sigs).
class StatsView:
    """The Statistics page, built inside the main window."""

    def __init__(self, app, parent):
        self.app = app
        self.win = app.root
        self._after = None
        self._sigs = {}
        tabs = self.tabs = ctk.CTkTabview(parent, fg_color="transparent",
                                          segmented_button_selected_color=ACCENT,
                                          segmented_button_selected_hover_color=ACCENT)
        tabs.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.build_usage(tabs.add("Usage"))
        self.build_costs(tabs.add("Costs"))
        self.build_jobs(tabs.add("Jobs"))
        self.build_customers(tabs.add("Customers"))
        self.build_quote(tabs.add("Quote"))
        self.build_maint(tabs.add("Maintenance"))

    def alive(self):
        """True while the Statistics page is the one shown."""
        return self.app.page == "stats"

    def stop(self):
        """Stop the 3-second refresh (the page was left)."""
        if self._after:
            self.win.after_cancel(self._after)
            self._after = None

    # -------------------------------------------------------------- layout
    def card(self, parent, row, col, title):
        """A number card (title, big value, small line under it) placed in a
        grid. Returns the (value label, sub label) pair that set() fills in."""
        f = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=14, border_width=1,
                         border_color=CARD_BORDER)
        f.grid(row=row, column=col, padx=5, pady=5, sticky="nsew")
        ctk.CTkLabel(f, text=title.upper(), font=self.app.font(10, "bold"), text_color=MUTED,
                     anchor="w").pack(fill="x", padx=14, pady=(10, 0))
        value = ctk.CTkLabel(f, text="–", font=self.app.font(21, "bold"), text_color=TEXT,
                             anchor="w")
        value.pack(fill="x", padx=14)
        sub = ctk.CTkLabel(f, text="", font=self.app.font(11), text_color=MUTED, anchor="w")
        sub.pack(fill="x", padx=14, pady=(0, 10))
        return value, sub

    def card_grid(self, parent, names, cols):
        """A grid of number cards. names: [(key, title), ...]. Returns {key:
        (value label, sub label)}."""
        grid = ctk.CTkFrame(parent, fg_color="transparent")
        grid.pack(fill="x")
        for c in range(cols):
            grid.grid_columnconfigure(c, weight=1, uniform="cards")
        return {key: self.card(grid, n // cols, n % cols, title)
                for n, (key, title) in enumerate(names)}

    def chart_area(self, parent, charts, default):
        """The chart and range dropdowns above a chart. Returns (chart, chart
        choice variable, range variable, the controls row for extra buttons)."""
        ctl = ctk.CTkFrame(parent, fg_color="transparent")
        ctl.pack(fill="x", pady=(10, 6), padx=5)
        chart_var = ctk.StringVar(value=default)
        range_var = ctk.StringVar(value="Last 30 days")
        for var, values, width in ((chart_var, list(charts), 230), (range_var, list(RANGES), 150)):
            ctk.CTkOptionMenu(ctl, variable=var, values=values, width=width, height=30,
                              font=self.app.font(12), dropdown_font=self.app.font(12),
                              command=lambda _: self.refresh(force=True)).pack(
                side="left", padx=(0, 8))
        box = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=14, border_width=1,
                           border_color=CARD_BORDER)
        box.pack(fill="both", expand=True, padx=5, pady=(0, 5))
        chart = Chart(box, self.app.scale)
        chart.pack(fill="both", expand=True, padx=10, pady=10)
        return chart, chart_var, range_var, ctl

    def build_usage(self, tab):
        """Usage tab: ten number cards and a chart with CSV export."""
        self.ucards = self.card_grid(tab, [
            ("total", "Total run time"), ("session", "This session"), ("today", "Today"),
            ("week", "Last 7 days"), ("month", "Last 30 days"), ("jobs", "Jobs"),
            ("longest", "Longest job"), ("busiest", "Busiest hour"),
            ("util", "Utilization (7 days)"), ("goal", "Weekly goal")], 5)
        self.uchart, self.uchart_var, self.urange_var, ctl = self.chart_area(
            tab, USAGE_CHARTS, "Run time per day")
        ctk.CTkButton(ctl, text="Export CSV", width=100, height=30, corner_radius=10,
                      fg_color="transparent", border_width=1, border_color=CARD_BORDER,
                      hover_color=CARD_BORDER, text_color=ACCENT, font=self.app.font(12, "bold"),
                      command=self.export_csv).pack(side="right")
        ctk.CTkLabel(ctl, text="Counts running time only · pauses, stops and test mode "
                               "are left out", font=self.app.font(11),
                     text_color=MUTED).pack(side="right", padx=10)

    def build_costs(self, tab):
        """Costs tab: the cost inputs on the left (saved as you type), cost
        cards and a cost chart on the right."""
        left = ctk.CTkScrollableFrame(tab, width=250, fg_color=CARD, corner_radius=14,
                                      border_width=1, border_color=CARD_BORDER)
        left.pack(side="left", fill="y", padx=(5, 5), pady=5)
        ctk.CTkLabel(left, text="Your machine costs", font=self.app.font(14, "bold"),
                     text_color=TEXT, anchor="w").pack(fill="x", padx=8, pady=(8, 0))
        ctk.CTkLabel(left, text="Saved as you type. Costs apply to running time only.",
                     font=self.app.font(11), text_color=MUTED, anchor="w", justify="left",
                     wraplength=230).pack(fill="x", padx=8, pady=(0, 6))
        costs = self.app.s["costs"]
        self.cost_entries = {}
        for key, label, unit, hint in COST_FIELDS:
            row = ctk.CTkFrame(left, fg_color="transparent")
            row.pack(fill="x", padx=8, pady=(8, 0))
            ctk.CTkLabel(row, text=label, font=self.app.font(12), text_color=TEXT,
                         anchor="w").pack(side="left")
            if unit:
                ctk.CTkLabel(row, text=unit, font=self.app.font(11),
                             text_color=MUTED).pack(side="right")
            value = costs[key]
            var = ctk.StringVar(value=value if key == "currency" else f"{value:g}")
            entry = ctk.CTkEntry(left, textvariable=var, height=30, font=self.app.font(12))
            entry.pack(fill="x", padx=8, pady=(2, 0))
            var.trace_add("write", lambda *_, k=key, v=var, e=entry: self.set_cost(k, v, e))
            self.cost_entries[key] = entry
            if hint:
                ctk.CTkLabel(left, text=hint, font=self.app.font(10), text_color=MUTED,
                             anchor="w").pack(fill="x", padx=8)

        right = ctk.CTkFrame(tab, fg_color="transparent")
        right.pack(side="left", fill="both", expand=True)
        self.ccards = self.card_grid(right, [
            ("rate", "Cost per run hour"), ("total", "Total cost"), ("session", "This session"),
            ("month", "Last 30 days"), ("avg", "Average per job"), ("last", "Last job")], 3)
        self.cchart, self.cchart_var, self.crange_var, _ = self.chart_area(
            right, COST_CHARTS, "Cost per day")

    # ------------------------------------------------------------- costs
    def set_cost(self, key, var, entry):
        """Validate and save one cost input. Bad numbers turn the box's border
        red and aren't saved; a comma works as the decimal point too."""
        text = var.get().strip()
        if key == "currency":
            value = text[:4]
        else:
            try:
                value = float(text.replace(",", ".")) if text else 0.0
                if value < 0 or math.isnan(value) or math.isinf(value):
                    raise ValueError
            except ValueError:
                entry.configure(border_color=RED)
                return
        entry.configure(border_color=("#979DA2", "#565B5E"))   # CTk's default entry border
        self.app.s["costs"][key] = value
        save_settings(self.app.s)
        self.refresh(force=True)

    def cost_parts(self):
        """The hourly cost split into (label, amount per hour, colour) parts."""
        c = self.app.s["costs"]
        wear = c["machine_price"] / c["lifetime_h"] if c["lifetime_h"] > 0 else 0.0
        return [("Electricity", c["power_kw"] * c["kwh_price"], AMBER),
                ("Machine wear", wear, BLUE),
                ("Maintenance", c["maint_h"], GREEN),
                ("Labour", c["labor_h"], "#A855F7"),
                ("Other", c["other_h"], SLATE)]

    def money(self, v):
        """An amount with the chosen currency symbol: "$1,234.50"."""
        return f"{self.app.s['costs']['currency']}{v:,.2f}"

    # ------------------------------------------------------------ refresh
    def refresh(self, force=False):
        """Recalculate every number on every tab, then schedule itself again in
        3 s. force=True redraws charts and lists even if nothing seems
        changed (after a dropdown change or a theme switch).

        The heavy part is turned into one structure first: `hourly`, the
        running seconds per clock hour over all jobs, from which totals,
        charts and the busiest hour are all summed."""
        if self._after:
            self.win.after_cancel(self._after)
            self._after = None
        if not self.alive():
            return
        now = time.time()
        log = self.app.runlog
        jobs = log.all_jobs(now)
        hourly = hourly_buckets([seg for j in jobs for seg in j["seg"]])
        today = date.today()

        def run_since(days):
            start = today - timedelta(days=days - 1)
            return sum(v for hr, v in hourly.items() if hr.date() >= start)

        def jobs_since(days):
            start = today - timedelta(days=days - 1)
            return sum(1 for j in jobs if datetime.fromtimestamp(j["start"]).date() >= start)

        total = sum(hourly.values())
        session = log.session_run_time(now)
        runs = [j["run"] for j in jobs]
        avg = sum(runs) / len(runs) if runs else 0.0

        u = self.ucards
        self.set(u["total"], fmt_human(total), "Since " + datetime.fromtimestamp(
            jobs[0]["start"]).strftime("%d %b %Y") if jobs else "No jobs recorded yet")
        self.set(u["session"], fmt_human(session),
                 f"App open {fmt_human(now - log.session_start)}")
        for key, days in (("today", 1), ("week", 7), ("month", 30)):
            n = jobs_since(days)
            self.set(u[key], fmt_human(run_since(days)), f"{n} job{'s' if n != 1 else ''}")
        self.set(u["jobs"], str(len(jobs)),
                 f"Average {fmt_human(avg)}" if jobs else "None yet")
        if jobs:
            longest = max(jobs, key=lambda j: j["run"])
            self.set(u["longest"], fmt_human(longest["run"]),
                     datetime.fromtimestamp(longest["start"]).strftime("%d %b %Y, %H:%M"))
            by_hour = [0.0] * 24
            for hr, v in hourly.items():
                by_hour[hr.hour] += v
            h = max(range(24), key=lambda i: by_hour[i])
            self.set(u["busiest"], f"{h:02d}:00", f"{fmt_human(by_hour[h])} in total")
        else:
            self.set(u["longest"], "–", "")
            self.set(u["busiest"], "–", "")
        open_s = self.app.shop_seconds(now - 7 * 86400, now)
        if open_s:
            self.set(u["util"], f"{run_since(7) / open_s:.0%}",
                     f"of {fmt_human(open_s)} opening hours")
        else:
            self.set(u["util"], "–", "Set opening hours in Settings")
        goal = float(self.app.s["goal_hours"] or 0)
        week = log.run_between(self.app.week_start().timestamp(), now, now)
        if goal:
            self.set(u["goal"], f"{week / 3600 / goal:.0%}",
                     f"{week / 3600:.1f} of {goal:g} h this week")
        else:
            self.set(u["goal"], fmt_human(week), "this week · set a goal in Settings")

        rate = sum(v for _, v, _ in self.cost_parts())
        per_s = rate / 3600
        c = self.ccards
        self.set(c["rate"], self.money(rate), "per hour of running")
        self.set(c["total"], self.money(total * per_s), fmt_human(total) + " run time")
        self.set(c["session"], self.money(session * per_s), fmt_human(session) + " run time")
        self.set(c["month"], self.money(run_since(30) * per_s), fmt_human(run_since(30)))
        self.set(c["avg"], self.money(avg * per_s), f"over {len(jobs)} jobs")
        if jobs:
            last = jobs[-1]
            self.set(c["last"], self.money(last["run"] * per_s),
                     ("Job in progress · " if log.job else "")
                     + fmt_human(last["run"]))
        else:
            self.set(c["last"], "–", "")

        # Charts: redraw only when something they show has changed
        mode = ctk.get_appearance_mode()
        costs = tuple(self.app.s["costs"].values())
        for chart, name_var, range_var, money in (
                (self.uchart, self.uchart_var, self.urange_var, False),
                (self.cchart, self.cchart_var, self.crange_var, True)):
            sig = (name_var.get(), range_var.get(), len(jobs), int(total / 10), today, mode,
                   costs if money else None)
            if force or self._sigs.get(chart) != sig:
                self._sigs[chart] = sig
                chart.show(self.spec(name_var.get(), range_var.get(), hourly, jobs, money))
        self.refresh_jobs(jobs, per_s, force)
        self.refresh_customers(jobs, rate, force)
        self.refresh_quote()
        self.refresh_maint(now)
        self._after = self.win.after(3000, self.refresh)

    @staticmethod
    def set(card, value, sub):
        """Fill in one number card."""
        card[0].configure(text=value)
        card[1].configure(text=sub)

    def spec(self, name, range_key, hourly, jobs, money):
        """Build the chart spec (see Chart) for the chosen chart and range.
        money=True turns run time into cost using the hourly rate. Returns an
        "empty" spec with a hint when there's nothing to draw."""
        kind, group = (COST_CHARTS if money else USAGE_CHARTS)[name]
        rate = sum(v for _, v, _ in self.cost_parts())
        if kind == "breakdown":
            if rate <= 0:
                return {"kind": "empty", "text": "Enter your machine costs on the left to see "
                                                 "what an hour of running costs."}
            return {"kind": "donut", "parts": self.cost_parts(), "fmt": self.money,
                    "center": self.money(rate), "center_sub": "per run hour"}

        today = date.today()
        days = RANGES[range_key]
        if days:
            start = today - timedelta(days=days - 1)
        else:
            start = min([hr.date() for hr in hourly] or [today])
        scale = rate / 3600 if money else 1.0
        base = {"color": AMBER if money else BLUE,
                "fmt": self.money if money else fmt_human,
                "axis": self.money if money else fmt_axis_dur,
                "ticks": "money" if money else "dur"}
        if money and rate <= 0:
            return {"kind": "empty", "text": "Enter your machine costs on the left to see "
                                             "cost charts."}

        if kind == "jobs":
            counts = {}
            for j in jobs:
                d = datetime.fromtimestamp(j["start"]).date()
                counts[d] = counts.get(d, 0) + 1
            ds = [start + timedelta(days=i) for i in range((today - start).days + 1)]
            vals = [counts.get(d, 0) for d in ds]
            spec = dict(base, kind="bars", values=vals, ticks="count", axis=lambda v: f"{v:g}",
                        fmt=lambda v: f"{v} job{'s' if v != 1 else ''}",
                        labels=[f"{d.day} {d:%b}" for d in ds],
                        tips=[f"{d:%a %d %b %Y}" for d in ds])
        elif kind in ("time", "cumulative"):
            def key(d):
                """The bucket a day falls in: itself, its week's Monday, or the
                1st."""
                if group == "week":
                    return d - timedelta(days=d.weekday())
                if group == "month":
                    return d.replace(day=1)
                return d
            keys = []
            d = start
            while d <= today:
                k = key(d)
                if not keys or keys[-1] != k:
                    keys.append(k)
                d += timedelta(days=1)
            totals = dict.fromkeys(keys, 0.0)
            before = 0.0
            for hr, v in hourly.items():
                d = hr.date()
                if d < start:
                    before += v
                elif key(d) in totals:
                    totals[key(d)] += v
            vals = [totals[k] * scale for k in keys]
            if group == "week":
                labels = [f"{k.day} {k:%b}" for k in keys]
                tips = [f"Week of {k:%d %b %Y}" for k in keys]
            elif group == "month":
                labels = [f"{k:%b %y}" for k in keys]
                tips = [f"{k:%B %Y}" for k in keys]
            else:
                labels = [f"{k.day} {k:%b}" for k in keys]
                tips = [f"{k:%a %d %b %Y}" for k in keys]
            if kind == "cumulative":
                acc, cum = before * scale, []
                for v in vals:
                    acc += v
                    cum.append(acc)
                vals = cum
                tips = [t + " (total so far)" for t in tips]
            spec = dict(base, kind="line" if kind == "cumulative" else "bars", values=vals,
                        labels=labels, tips=tips)
        elif kind in ("hours", "weekdays"):
            n = 24 if kind == "hours" else 7
            vals = [0.0] * n
            for hr, v in hourly.items():
                if hr.date() >= start:
                    vals[hr.hour if kind == "hours" else hr.weekday()] += v * scale
            if kind == "hours":
                labels = [f"{h:02d}" for h in range(24)]
                tips = [f"{h:02d}:00–{(h + 1) % 24:02d}:00" for h in range(24)]
            else:
                labels, tips = WEEKDAYS, ["Mondays", "Tuesdays", "Wednesdays", "Thursdays",
                                          "Fridays", "Saturdays", "Sundays"]
            spec = dict(base, kind="bars", values=vals, labels=labels, tips=tips)
        else:  # heatmap
            m = [[0.0] * 24 for _ in range(7)]
            for hr, v in hourly.items():
                if hr.date() >= start:
                    m[hr.weekday()][hr.hour] += v * scale
            if not any(any(r) for r in m):
                return {"kind": "empty", "text": "No run time recorded in this range yet."}
            return dict(base, kind="heatmap", matrix=m, rows=WEEKDAYS,
                        cols=[f"{h:02d}" for h in range(24)],
                        col_tips=[f"{h:02d}:00–{(h + 1) % 24:02d}:00" for h in range(24)])

        if not any(spec["values"]):
            return {"kind": "empty", "text": "No run time recorded in this range yet."}
        return spec

    # -------------------------------------------------------- shared bits
    def panel(self, parent, **pack):
        """A rounded card frame, packed with the given options (full width by
        default)."""
        f = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=14, border_width=1,
                         border_color=CARD_BORDER)
        f.pack(**(pack or dict(fill="x", padx=5, pady=5)))
        return f

    def ghost_btn(self, parent, text, command, width=90):
        """An outlined secondary button, as used on the Statistics tabs."""
        return ctk.CTkButton(parent, text=text, width=width, height=30, corner_radius=10,
                             fg_color="transparent", border_width=1, border_color=CARD_BORDER,
                             hover_color=CARD_BORDER, text_color=ACCENT,
                             font=self.app.font(12, "bold"), command=command)

    def label(self, parent, text, size=12, color=TEXT, bold=False, **kw):
        """A left-aligned label in the app's font."""
        return ctk.CTkLabel(parent, text=text, text_color=color, anchor="w",
                            font=self.app.font(size, "bold" if bold else "normal"), **kw)

    def tree(self, parent, columns, stretch=1):
        """A themed table: columns = [(key, heading, width, anchor)]; the
        `stretch`-th column takes the spare width."""
        box = ctk.CTkFrame(parent, fg_color="transparent")
        box.pack(fill="both", expand=True, padx=8, pady=8)
        t = ttk.Treeview(box, columns=[c[0] for c in columns], show="headings",
                         style="Beacon.Treeview", selectmode="extended")
        for key, heading, width, anchor in columns:
            t.heading(key, text=heading, anchor=anchor)
            t.column(key, width=int(width * self.app.scale), anchor=anchor,
                     stretch=key == columns[stretch][0])
        bar = ctk.CTkScrollbar(box, command=t.yview)
        t.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        t.pack(side="left", fill="both", expand=True)
        return t

    def tree_style(self):
        """Make Tk's table widget (ttk.Treeview) match the app's colours. Called
        again when the theme changes, since ttk styles don't follow
        CustomTkinter by themselves."""
        st = ttk.Style(self.win)
        if st.theme_use() != "clam":
            st.theme_use("clam")
        rh = int(26 * self.app.scale)
        st.configure("Beacon.Treeview", background=pick(CARD), fieldbackground=pick(CARD),
                     foreground=pick(TEXT), rowheight=rh, borderwidth=0, font=(FONT, 10))
        st.configure("Beacon.Treeview.Heading", background=pick(CARD), foreground=pick(MUTED),
                     relief="flat", borderwidth=0, font=(FONT, 9, "bold"))
        st.map("Beacon.Treeview.Heading", background=[("active", pick(CARD_BORDER))])
        st.map("Beacon.Treeview", background=[("selected", pick(ACCENT))],
               foreground=[("selected", "#FFFFFF")])
        st.layout("Beacon.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    def remember(self, kind, value):
        """Add a customer / material name to the pick lists."""
        value = value.strip()
        names = self.app.s[kind]
        if value and value not in names:
            names.append(value)
            names.sort(key=str.lower)
            save_settings(self.app.s)
            for combo in self._combos.get(kind, []):
                combo.configure(values=names)

    def combo(self, parent, kind, var, width=150, on_pick=None):
        """An editable dropdown for customers or materials: pick a remembered
        name or type a new one, which is remembered when picked or when the
        box loses focus. All boxes of one kind share the same list."""
        c = ctk.CTkComboBox(parent, variable=var, values=self.app.s[kind] or [""], width=width,
                            height=30, font=self.app.font(12), dropdown_font=self.app.font(12),
                            command=lambda v: (self.remember(kind, v), on_pick and on_pick()))
        c.bind("<FocusOut>", lambda e: self.remember(kind, var.get()), add="+")
        self._combos.setdefault(kind, []).append(c)
        return c

    # --------------------------------------------------------------- jobs
    def build_jobs(self, tab):
        """Jobs tab: the customer / material new jobs are tagged with, the table
        of the last 500 jobs (newest first), and an editor that sets the
        name, customer or material of the selected jobs."""
        self._combos, self._tag_rev = {}, 0
        self.tree_style()
        top = self.panel(tab)
        row = ctk.CTkFrame(top, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=10)
        self.label(row, "New jobs go to", bold=True).pack(side="left", padx=(0, 8))
        s = self.app.s
        for key, kind, hint in (("client", "clients", "Customer"), ("material", "materials",
                                                                     "Material")):
            var = ctk.StringVar(value=s[key])
            var.trace_add("write", lambda *_, k=key, v=var: self._set_current(k, v))
            self.label(row, hint, color=MUTED, size=11).pack(side="left", padx=(8, 4))
            self.combo(row, kind, var).pack(side="left")
        self.label(row, "The job name is the LightBurn file name", color=MUTED,
                   size=11).pack(side="right")

        box = self.panel(tab, fill="both", expand=True, padx=5, pady=5)
        self.jobs_tree = self.tree(box, [("start", "Started", 130, "w"), ("name", "Job", 220, "w"),
                                         ("client", "Customer", 130, "w"),
                                         ("material", "Material", 120, "w"),
                                         ("run", "Run time", 80, "e"), ("cost", "Cost", 80, "e")])
        self.jobs_tree.bind("<<TreeviewSelect>>", lambda e: self._job_selected())

        edit = self.panel(tab)
        row = ctk.CTkFrame(edit, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(10, 2))
        self.label(row, "Selected jobs", bold=True).pack(side="left", padx=(0, 8))
        self.tag_vars = {k: ctk.StringVar() for k in JOB_TAGS}
        ctk.CTkEntry(row, textvariable=self.tag_vars["name"], width=170, height=30,
                     placeholder_text="Job name", font=self.app.font(12)).pack(side="left")
        self.combo(row, "clients", self.tag_vars["client"], 140).pack(side="left", padx=6)
        self.combo(row, "materials", self.tag_vars["material"], 130).pack(side="left")
        self.ghost_btn(row, "Apply", self.apply_tags, 70).pack(side="left", padx=6)
        self.jobs_status = self.label(edit, "Pick jobs in the list (Ctrl or Shift for several). "
                                            "Empty boxes are left as they are.",
                                      size=11, color=MUTED)
        self.jobs_status.pack(fill="x", padx=12, pady=(0, 8))

    def _set_current(self, key, var):
        """Save the customer / material that new jobs get."""
        self.app.s[key] = var.get().strip()
        save_settings(self.app.s)

    def _job_selected(self):
        """Selecting one job fills the editor with its tags, ready to change."""
        iids = self.jobs_tree.selection()
        if len(iids) == 1:                  # show its tags, ready to edit
            vals = self.jobs_tree.item(iids[0], "values")
            for k, v in zip(("name", "client", "material"), (vals[1], vals[2], vals[3])):
                self.tag_vars[k].set("" if v == "–" else v)
        n = len(iids)
        self.jobs_status.configure(text=f"{n} job{'s' if n != 1 else ''} selected")

    def apply_tags(self):
        """Apply the editor's non-empty boxes to every selected job. The table's
        row ids are the jobs' start times, which is how RunLog.set_tag finds
        them. The job still running is updated too."""
        iids = self.jobs_tree.selection()
        if not iids:
            self.jobs_status.configure(text="Pick one or more jobs in the list first.")
            return
        log = self.app.runlog
        changed = []
        for key in JOB_TAGS:
            value = self.tag_vars[key].get().strip()
            if not value:
                continue
            log.set_tag(iids, key, value)
            if log.job and str(log.job["start"]) in iids:
                log.job[key] = value
            if key != "name":
                self.remember(key + "s", value)
            changed.append(key)
        self._tag_rev += 1
        n = len(iids)
        self.jobs_status.configure(
            text=(f"Updated {n} job{'s' if n != 1 else ''}." if changed
                  else "Type a name, customer or material to apply."))
        self.refresh(force=True)

    def refresh_jobs(self, jobs, per_s, force):
        """Refill the jobs table when something in it changed (a job added, tags
        edited, the theme, the cost rate, or the running job's time moved on
        by 30 s), keeping the selection."""
        mode = ctk.get_appearance_mode()
        sig = (len(jobs), self._tag_rev, mode, round(per_s, 6),
               int(jobs[-1]["run"] // 30) if jobs else 0)
        old = self._sigs.get("jobs")
        if not force and sig == old:
            return
        if not old or old[2] != mode:
            self.tree_style()
        self._sigs["jobs"] = sig
        t = self.jobs_tree
        sel = set(t.selection())
        t.delete(*t.get_children())
        for j in reversed(jobs[-500:]):
            if t.exists(str(j["start"])):       # same start twice: an edited log
                continue
            t.insert("", "end", iid=str(j["start"]), values=(
                datetime.fromtimestamp(j["start"]).strftime("%d %b %Y  %H:%M"),
                j.get("name") or "–", j.get("client") or "", j.get("material") or "",
                fmt_human(j["run"]), self.money(j["run"] * per_s)))
        keep = [i for i in sel if t.exists(i)]
        if keep:
            t.selection_set(keep)

    # ---------------------------------------------------------- customers
    def build_customers(self, tab):
        """Customers tab: a month picker (or All time), PDF / Excel export of a
        month, four number cards, and run time / cost per customer and per
        material."""
        top = ctk.CTkFrame(tab, fg_color="transparent")
        top.pack(fill="x", padx=5, pady=(5, 0))
        today = date.today()
        self.months = {"All time": None}
        y, m = today.year, today.month
        for _ in range(12):
            self.months[f"{date(y, m, 1):%B %Y}"] = (y, m)
            y, m = (y, m - 1) if m > 1 else (y - 1, 12)
        self.month_var = ctk.StringVar(value=list(self.months)[1])
        ctk.CTkOptionMenu(top, variable=self.month_var, values=list(self.months), width=170,
                          height=30, font=self.app.font(12), dropdown_font=self.app.font(12),
                          command=lambda _: self.refresh(force=True)).pack(side="left")
        self.pdf_btn = self.ghost_btn(top, "PDF report", lambda: self.export_month("pdf"), 100)
        self.pdf_btn.pack(side="left", padx=(8, 0))
        self.xlsx_btn = self.ghost_btn(top, "Excel", lambda: self.export_month("xlsx"), 80)
        self.xlsx_btn.pack(side="left", padx=6)
        self.month_status = self.label(top, "", size=11, color=MUTED)
        self.month_status.pack(side="left", padx=8)
        self.mcards = self.card_grid(tab, [("run", "Run time"), ("jobs", "Jobs"),
                                           ("cost", "Machine cost"), ("days", "Active days")], 4)
        lists = ctk.CTkFrame(tab, fg_color="transparent")
        lists.pack(fill="both", expand=True)
        self.group_trees = {}
        for key, title in (("clients", "Customer"), ("materials", "Material")):
            box = self.panel(lists, side="left", fill="both", expand=True, padx=5, pady=5)
            self.group_trees[key] = self.tree(box, [("k", title, 150, "w"),
                                                    ("n", "Jobs", 50, "e"),
                                                    ("run", "Run time", 80, "e"),
                                                    ("cost", "Cost", 80, "e")], stretch=0)

    def month_data(self, jobs, rate):
        """Totals and per-customer / per-material groups for the chosen month
        (exporters.month_summary) or for all time (computed here the same
        way)."""
        ym = self.months.get(self.month_var.get())
        c = self.app.s["costs"]["currency"]
        if ym:
            return exporters.month_summary(jobs, rate, c, *ym)
        out = {}
        for key in ("client", "material"):
            groups = {}
            for j in jobs:
                g = groups.setdefault(j.get(key) or "(none)", [0, 0.0])
                g[0] += 1
                g[1] += j["run"]
            out[key + "s"] = sorted(((k, n, r, r * rate / 3600) for k, (n, r) in groups.items()),
                                    key=lambda x: -x[2])
        total = sum(j["run"] for j in jobs)
        days = {datetime.fromtimestamp(j["start"]).date() for j in jobs}
        return dict(out, total=total, jobs=len(jobs), cost=total * rate / 3600,
                    active_days=len(days))

    def refresh_customers(self, jobs, rate, force):
        """Refill the Customers tab when its data changed."""
        sig = (self.month_var.get(), len(jobs), self._tag_rev, round(rate, 6),
               int(jobs[-1]["run"] // 30) if jobs else 0)
        if not force and self._sigs.get("cust") == sig:
            return
        self._sigs["cust"] = sig
        m = self.month_data(jobs, rate)
        self.set(self.mcards["run"], fmt_human(m["total"]), self.month_var.get())
        self.set(self.mcards["jobs"], str(m["jobs"]), "")
        self.set(self.mcards["cost"], self.money(m["cost"]), "at your Costs-tab rate")
        self.set(self.mcards["days"], str(m["active_days"]), "days the laser ran")
        for key, t in self.group_trees.items():
            t.delete(*t.get_children())
            for k, n, r, cost in m[key]:
                t.insert("", "end", values=(k, n, fmt_human(r), self.money(cost)))
        monthly = self.months.get(self.month_var.get()) is not None
        for b in (self.pdf_btn, self.xlsx_btn):
            b.configure(state="normal" if monthly else "disabled")

    def export_month(self, kind):
        """Save the chosen month as a PDF or Excel report (beacon/exporters.py)
        and open it."""
        ym = self.months.get(self.month_var.get())
        if not ym:
            return
        rate = sum(v for _, v, _ in self.cost_parts())
        m = exporters.month_summary(self.app.runlog.all_jobs(time.time()), rate,
                                    self.app.s["costs"]["currency"], *ym)
        ext = ".pdf" if kind == "pdf" else ".xlsx"
        path = filedialog.asksaveasfilename(
            parent=self.win, defaultextension=ext,
            initialfile=f"{APP_NAME} {ym[0]}-{ym[1]:02d}{ext}",
            filetypes=[("PDF file", "*.pdf")] if kind == "pdf" else [("Excel file", "*.xlsx")])
        if not path:
            return
        try:
            if kind == "pdf":
                exporters.write_month_pdf(path, m, APP_NAME)
            else:
                exporters.write_month_xlsx(path, m)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Couldn't save the file:\n{e}", parent=self.win)
            return
        self.month_status.configure(text="Saved ✓")
        try:
            open_path(path)
        except OSError:
            pass

    # -------------------------------------------------------------- quote
    # Quote calculator inputs: (setting key, label, unit). "$" is replaced by
    # the chosen currency symbol.
    QUOTE_FIELDS = (("minutes", "Laser time per piece", "min"), ("qty", "Quantity", "pcs"),
                    ("material", "Material per piece", "$"), ("setup", "Setup / design (once)", "$"),
                    ("margin", "Markup", "%"))

    def build_quote(self, tab):
        """Quote tab: inputs on the left, the price breakdown on the right."""
        left = self.panel(tab, side="left", fill="y", padx=5, pady=5)
        self.label(left, "Quote a job", 14, bold=True).pack(fill="x", padx=14, pady=(12, 0))
        self.label(left, "Machine time is priced at your Costs-tab rate.", 11, MUTED,
                   wraplength=230, justify="left").pack(fill="x", padx=14, pady=(0, 6))
        q = self.app.s["quote"]
        cur = self.app.s["costs"]["currency"]
        for key, text, unit in self.QUOTE_FIELDS:
            row = ctk.CTkFrame(left, fg_color="transparent")
            row.pack(fill="x", padx=14, pady=(8, 0))
            self.label(row, text).pack(side="left")
            self.label(row, cur if unit == "$" else unit, 11, MUTED).pack(side="right")
            var = ctk.StringVar(value=f"{q[key]:g}")
            entry = ctk.CTkEntry(left, textvariable=var, width=230, height=30,
                                 font=self.app.font(12))
            entry.pack(padx=14, pady=(2, 0))
            var.trace_add("write", lambda *_, k=key, v=var, e=entry: self.set_quote(k, v, e))
        ctk.CTkFrame(left, height=12, fg_color="transparent").pack()
        right = ctk.CTkFrame(tab, fg_color="transparent")
        right.pack(side="left", fill="both", expand=True)
        self.qcards = self.card_grid(right, [
            ("time", "Machine time"), ("machine", "Machine cost"), ("material", "Material"),
            ("cost", "Your cost"), ("price", "Quote price"), ("each", "Price per piece")], 3)
        self.quote_note = self.label(right, "", 11, MUTED, wraplength=520, justify="left")
        self.quote_note.pack(fill="x", padx=10, pady=8)

    def set_quote(self, key, var, entry):
        """Validate and save one quote input (red border when it isn't a
        number)."""
        try:
            value = float(var.get().replace(",", ".") or 0)
            if value < 0 or math.isnan(value) or math.isinf(value):
                raise ValueError
        except ValueError:
            entry.configure(border_color=RED)
            return
        entry.configure(border_color=("#979DA2", "#565B5E"))
        self.app.s["quote"][key] = value
        save_settings(self.app.s)
        self.refresh_quote()

    def refresh_quote(self):
        """The quote: machine = laser hours x the hourly cost from the Costs tab
        your cost = machine + material x quantity + setup price = your cost +
        markup %"""
        q = self.app.s["quote"]
        rate = sum(v for _, v, _ in self.cost_parts())
        qty = max(1, int(q["qty"]))
        secs = q["minutes"] * 60 * qty
        machine = secs / 3600 * rate
        material = q["material"] * qty
        cost = machine + material + q["setup"]
        price = cost * (1 + q["margin"] / 100)
        c = self.qcards
        self.set(c["time"], fmt_human(secs), f"{qty} × {q['minutes']:g} min")
        self.set(c["machine"], self.money(machine), f"{self.money(rate)} per hour")
        self.set(c["material"], self.money(material), f"+ setup {self.money(q['setup'])}")
        self.set(c["cost"], self.money(cost), "machine + material + setup")
        self.set(c["price"], self.money(price), f"with {q['margin']:g}% markup")
        self.set(c["each"], self.money(price / qty), f"profit {self.money(price - cost)}")
        self.quote_note.configure(text="" if rate > 0 else
                                  "Enter your machine costs in the Costs tab so laser time "
                                  "is priced in.")

    # -------------------------------------------------------- maintenance
    def build_maint(self, tab):
        """Maintenance tab: total laser hours, one row per task, Add task."""
        head = ctk.CTkFrame(tab, fg_color="transparent")
        head.pack(fill="x", padx=5, pady=(5, 0))
        self.maint_total = self.label(head, "", 12, MUTED)
        self.maint_total.pack(side="left", padx=4)
        self.ghost_btn(head, "Add task", self.add_maint, 90).pack(side="right")
        self.maint_box = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        self.maint_box.pack(fill="both", expand=True)
        self.label(tab, "You get a message when a task is due (Settings › Notifications). "
                        "Press Done after doing it to start counting again.", 11, MUTED).pack(
            fill="x", padx=8, pady=(0, 6))
        self.build_maint_rows()

    def build_maint_rows(self):
        """One card per maintenance task: editable name and interval, Done, ✕,
        and a progress bar towards the next service."""
        for w in self.maint_box.winfo_children():
            w.destroy()
        self.maint_rows = []
        for t in self.app.s["maint"]:
            box = self.panel(self.maint_box)
            row = ctk.CTkFrame(box, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=(10, 4))
            name = ctk.StringVar(value=t["name"])
            ctk.CTkEntry(row, textvariable=name, height=30, font=self.app.font(12, "bold"),
                         border_width=0, fg_color="transparent").pack(side="left", fill="x",
                                                                      expand=True)
            name.trace_add("write", lambda *_, t=t, v=name: self.set_maint(t, "name", v))
            ctk.CTkButton(row, text="✕", width=28, height=28, corner_radius=8,
                          fg_color="transparent", hover_color=CARD_BORDER, text_color=MUTED,
                          command=lambda t=t: self.remove_maint(t)).pack(side="right")
            self.ghost_btn(row, "Done", lambda t=t: self.maint_done(t), 70).pack(
                side="right", padx=6)
            every = ctk.StringVar(value=f"{t['every_h']:g}")
            self.label(row, "h", 11, MUTED).pack(side="right", padx=(2, 8))
            ctk.CTkEntry(row, textvariable=every, width=56, height=28,
                         font=self.app.font(12)).pack(side="right")
            self.label(row, "every", 11, MUTED).pack(side="right", padx=4)
            every.trace_add("write", lambda *_, t=t, v=every: self.set_maint(t, "every_h", v))
            bar = ctk.CTkProgressBar(box, height=8, corner_radius=4)
            bar.pack(fill="x", padx=14, pady=(2, 4))
            info = self.label(box, "", 11, MUTED)
            info.pack(fill="x", padx=14, pady=(0, 10))
            self.maint_rows.append((t, bar, info))
        self.refresh_maint(time.time())

    def set_maint(self, t, key, var):
        """Save a task's edited name or interval (intervals must be > 0)."""
        if key == "name":
            t["name"] = var.get()
        else:
            try:
                v = float(var.get().replace(",", "."))
                if v <= 0:
                    raise ValueError
            except ValueError:
                return
            t["every_h"] = v
        save_settings(self.app.s)
        self.refresh_maint(time.time())

    def maint_done(self, t):
        """Done: the task was just serviced, so count from the current total."""
        t["last_h"] = round(self.app.runlog.total_run(time.time()) / 3600, 3)
        self.app.maint_alerted.discard(t["name"])
        save_settings(self.app.s)
        self.refresh_maint(time.time())

    def add_maint(self):
        """Add a new task, counting from now."""
        self.app.s["maint"].append({"name": "New task", "every_h": 50.0,
                                    "last_h": round(self.app.runlog.total_run(time.time()) / 3600,
                                                    3)})
        save_settings(self.app.s)
        self.build_maint_rows()

    def remove_maint(self, t):
        """Delete a task."""
        self.app.s["maint"].remove(t)
        save_settings(self.app.s)
        self.build_maint_rows()

    def refresh_maint(self, now):
        """Update each task's bar and text: green, amber past 80%, red when due."""
        hours = self.app.runlog.total_run(now) / 3600
        self.maint_total.configure(text=f"Laser hours in total: {hours:.1f} h")
        for t, bar, info in self.maint_rows:
            used = max(0.0, hours - float(t.get("last_h", 0)))
            frac = min(1.0, used / float(t["every_h"])) if t["every_h"] else 0
            bar.set(frac)
            due = used >= float(t["every_h"])
            bar.configure(progress_color=RED if due else (AMBER if frac > 0.8 else GREEN))
            text = (f"Due now · {used:.1f} h since last done" if due else
                    f"{used:.1f} of {t['every_h']:g} h · due in {t['every_h'] - used:.1f} h")
            if info.cget("text") != text:
                info.configure(text=text, text_color=RED if due else MUTED)

    def export_csv(self):
        """Usage tab: save every job as a CSV file (opens in Excel), with run
        time, time spent paused or stopped, and cost."""
        path = filedialog.asksaveasfilename(parent=self.win, defaultextension=".csv",
                                            initialfile="laser_jobs.csv",
                                            filetypes=[("CSV file", "*.csv")])
        if not path:
            return
        per_s = sum(v for _, v, _ in self.cost_parts()) / 3600
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                out = csv.writer(f)
                out.writerow(["Start", "End", "Job", "Customer", "Material", "Run time (min)",
                              "Paused/stopped (min)",
                              f"Cost ({self.app.s['costs']['currency']})"])
                for j in self.app.runlog.all_jobs(time.time()):
                    out.writerow([
                        datetime.fromtimestamp(j["start"]).strftime("%Y-%m-%d %H:%M:%S"),
                        datetime.fromtimestamp(j["end"]).strftime("%Y-%m-%d %H:%M:%S"),
                        j.get("name") or "", j.get("client") or "", j.get("material") or "",
                        round(j["run"] / 60, 2),
                        round(max(0.0, j["end"] - j["start"] - j["run"]) / 60, 2),
                        round(j["run"] * per_s, 2)])
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Couldn't save the file:\n{e}", parent=self.win)


# ---------------------------------------------------------------------------
# Start-up. Only one copy of Beacon may run (two would fight over the Arduino's
# port and send every notification twice). The running copy holds a local TCP
# port; a second launch finds it taken, connects to it (which makes the first
# copy show its window, see listen_for_launches) and exits. Dev mode uses its
# own port so it can run next to the installed Beacon.
INSTANCE_PORT = 47614      # local only; held by the running copy (the original uses 47613)
if DEV:
    INSTANCE_PORT = 47615


def claim_single_instance():
    """Returns a listening socket if this is the only copy running. If another
    copy already holds the port, asks it to come to the front and exits."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        server.bind(("127.0.0.1", INSTANCE_PORT))
        server.listen(4)
        return server
    except OSError:
        server.close()
    try:
        socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=1.5).close()
    except OSError:
        return None             # port busy with something else: run anyway
    sys.exit(0)


def setup_logging():
    """Errors go to %APPDATA%\\LaserStatusV2\\logs\\beacon.log (in the diagnostics zip)."""
    from logging.handlers import RotatingFileHandler
    try:
        logs = os.path.join(CONFIG_DIR, "logs")
        os.makedirs(logs, exist_ok=True)
        handler = RotatingFileHandler(os.path.join(logs, "beacon.log"), maxBytes=500_000,
                                      backupCount=2, encoding="utf-8")
    except OSError:
        return
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(threadName)s: %(message)s")
    logging.info("%s %s starting", APP_NAME, APP_VERSION)
    sys.excepthook = lambda *exc: logging.error("Uncaught error", exc_info=exc)
    threading.excepthook = lambda a: logging.error(
        "Error in thread %s", a.thread.name if a.thread else "?",
        exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


def main():
    """Start Beacon: logging, the single-instance check, OCR (Windows OCR, else
    Tesseract), then the App, which runs until quit.

    Without any OCR the app can still work if LightBurn's API was paired
    before; otherwise there's no way to read LightBurn, so it says why."""
    setup_logging()
    server = claim_single_instance()
    ocr, err = make_ocr()
    if ocr is None:
        if load_settings()["lb_api_secret"]:        # the API can still read LightBurn
            logging.warning("Text recognition unavailable, using LightBurn's API: %s", err)
        else:
            fatal("Couldn't start text recognition.\n\n" + err +
                  "\n\nInstall the packages listed at the top of laser_status_app.py.")
    if IS_MAC and ocr is not None and load_settings()["detect_mode"] != "api":
        screen_permission(ask=True)     # macOS asks once; reading the screen needs it
    app = App(ocr)
    if server:
        app.listen_for_launches(server)
    app.run()


if __name__ == "__main__":
    main()
