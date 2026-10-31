"""Beacon's feature modules, used by laser_status_app.py:

  lbapi       LightBurn 2.2+ REST API client (pairing, signed requests)
  channels    Discord, Pushover, email and SMS senders, Telegram photos
  smartplugs  Shelly / Tasmota / Kasa / Home Assistant switches as relays
  exporters   monthly PDF and Excel reports (no extra packages)
  webdash     the phone dashboard web server
  winsys      tray icon, global hotkey, start with Windows, firewall, idle time
  macsys      the same for macOS: menu-bar icon, hotkey, open at login
  maccapture  macOS window capture and Vision text recognition
  updater     update check, download and silent install
  camera      webcam snapshots (needs opencv-python-headless)
  detectlog   the opt-in detection accuracy log"""
