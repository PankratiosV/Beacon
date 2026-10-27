#!/usr/bin/env python3
"""
Creates "Beacon V2" shortcuts (this folder, Desktop and Start menu) with the
app's icon. Right-click one and choose "Pin to taskbar". They don't touch the
original app's shortcuts (whose taskbar pin is named "Beacon").

    python make_shortcut.py          "Beacon V2": the source, real settings
    python make_shortcut.py --dev    "Beacon <version> DEV": the source in dev mode
                                     (own settings in .devdata), this folder
                                     and Start menu only. The name follows
                                     APP_VERSION; older dev shortcuts are removed.

Run it again if you move this folder, or (with --dev) after a version bump.
"""

import os
import re
import sys

import pythoncom
from win32com.shell import shell, shellcon
from win32com.propsys import propsys, pscon

DEV = "--dev" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "laser_status_app.py")
_m = re.search(r'^APP_VERSION = "([^"]+)"', open(SCRIPT, encoding="utf-8").read(), re.M)
VERSION = _m.group(1) if _m else "?"
# must match APP_ID in laser_status_app.py
APP_ID = "LaserStatusV2.Dev" if DEV else "LaserStatusV2.App"
# not "Beacon.lnk": that pin belongs to the original app
SHORTCUT = f"Beacon {VERSION} DEV.lnk" if DEV else "Beacon V2.lnk"
OLD_DEV = re.compile(r"^Beacon( \(dev\)| .+ DEV)\.lnk$")    # earlier dev shortcut names
ARGS = " --dev" if DEV else ""
ICON = os.path.join(HERE, "laser_status.ico")
PYTHONW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")


def make_link(path):
    """Write one .lnk shortcut that starts the source with pythonw (no console
    window) and carries the AppUserModelID, so Windows groups the running
    window with the shortcut and shows Beacon's icon on the taskbar."""
    link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None,
                                      pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
    link.SetPath(PYTHONW)
    link.SetArguments(f'"{SCRIPT}"{ARGS}')
    link.SetWorkingDirectory(HERE)
    link.SetIconLocation(ICON, 0)
    link.SetDescription("Beacon from the source, dev mode" if DEV else "LightBurn job light")
    store = link.QueryInterface(propsys.IID_IPropertyStore)
    store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(APP_ID))
    store.Commit()
    link.QueryInterface(pythoncom.IID_IPersistFile).Save(path, 0)


def main():
    """Create the shortcuts (dev: this folder and Start menu; otherwise also the
    Desktop), removing old dev shortcuts first, and refresh a matching
    taskbar pin if there is one."""
    if not os.path.exists(PYTHONW):
        sys.exit(f"Can't find pythonw.exe next to {sys.executable}")
    programs = shell.SHGetFolderPath(0, shellcon.CSIDL_PROGRAMS, None, 0)
    # Started from inside a packaged (MSIX / Store) app, writes
    # under %APPDATA% are redirected into that app's own folder; going through
    # \\localhost\C$ puts the Start menu shortcut where Windows looks for it.
    if programs[1:3] == ":\\" and os.path.isdir("\\\\localhost\\" + programs[0] + "$"):
        programs = "\\\\localhost\\" + programs[0] + "$" + programs[2:]
    folders = [HERE, programs]
    if not DEV:
        folders.insert(1, shell.SHGetFolderPath(0, shellcon.CSIDL_DESKTOPDIRECTORY, None, 0))
    for folder in folders:
        if DEV:
            for name in os.listdir(folder):
                if OLD_DEV.match(name) and name != SHORTCUT:
                    os.remove(os.path.join(folder, name))
                    print("Removed", os.path.join(folder, name))
        path = os.path.join(folder, SHORTCUT)
        make_link(path)
        print("Created", path)
    # Already pinned to the taskbar? Point the pin at the new location too
    pinned = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Internet Explorer",
                          "Quick Launch", "User Pinned", "TaskBar", SHORTCUT)
    if os.path.exists(pinned):
        make_link(pinned)
        print("Updated taskbar pin", pinned)


if __name__ == "__main__":
    main()
