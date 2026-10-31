"""Update checks. The app reads a small JSON file you publish, e.g.

    {"version": "2.1.0",
     "url": "https://example.com/downloads/Beacon-Setup-2.1.0.exe",
     "url_mac": "https://example.com/downloads/Beacon-2.1.0.dmg",
     "notes": "Job progress on the pill, bug fixes"}

and offers to download and run the new installer, which upgrades in place
(Inno Setup keeps the same AppId, so settings are kept). On macOS "url_mac"
is used: the new Beacon.app is copied out of the .dmg over the running one
and started again (settings live outside the app, so they're kept too)."""

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

IS_MAC = sys.platform == "darwin"


def version_key(v):
    """'2.1.0' > '2.0.10' > '2.0.0' > '2.0.0-dev' (pre-releases sort below the release)."""
    # A sortable tuple: up to four numbers, then 1 for a release or 0 for a
    # pre-release ("-dev", "-beta"), so 2.1.0-beta < 2.1.0.
    main, _, pre = str(v).strip().lstrip("vV").partition("-")
    nums = [int(x) for x in re.findall(r"\d+", main)][:4]
    nums += [0] * (4 - len(nums))
    return tuple(nums) + ((1,) if not pre else (0,))


def check(url, current, timeout=10):
    """Returns the update info dict if a newer version is published, else None.
    Raises RuntimeError if the check itself fails. On macOS info["url"] is
    the Mac download ("url_mac"); a release without one counts as no update."""
    if not url:
        raise RuntimeError("No update address set (UPDATE_URL in laser_status_app.py).")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Beacon-updater",
                                                   "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            info = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise RuntimeError("Couldn't check for updates. Check the internet connection.") from e
    if not isinstance(info, dict) or "version" not in info or "url" not in info:
        raise RuntimeError("The update file is missing \"version\" or \"url\".")
    if IS_MAC:
        if not info.get("url_mac"):
            return None
        info = dict(info, url=info["url_mac"])
    return info if version_key(info["version"]) > version_key(current) else None


def download(url, progress=None, timeout=30):
    """Downloads the installer to a temp file and returns its path.
    progress(fraction) is called as it goes (fraction may be None if unknown)."""
    ext = ".dmg" if IS_MAC else ".exe"
    name = os.path.basename(url.split("?")[0]) or f"Beacon-Setup{ext}"
    if not name.lower().endswith(ext):
        name += ext
    dest = os.path.join(tempfile.gettempdir(), name)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Beacon-updater"})
        with urllib.request.urlopen(req, timeout=timeout) as r, open(dest + ".part", "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done / total if total else None)
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError("The download failed. Try again later.") from e
    # Downloaded to a .part file first, so a broken download never leaves a
    # half-written installer under the real name.
    os.replace(dest + ".part", dest)
    return dest


def run_installer(path):
    """Starts the downloaded installer quietly; it closes the running app,
    upgrades it and starts it again."""
    if IS_MAC:
        _replace_mac_app(path)
        return
    # Inno Setup switches: /SILENT shows only a progress bar, /SP- skips the
    # "This will install..." question, /CLOSEAPPLICATIONS and
    # /RESTARTAPPLICATIONS close Beacon and start it again afterwards,
    # /NORESTART never reboots Windows.
    subprocess.Popen([path, "/SILENT", "/SP-", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS",
                      "/NORESTART"], close_fds=True)


# Runs detached after Beacon quits: waits for it to exit, mounts the .dmg,
# swaps the new Beacon.app in (keeping the old one until the copy worked) and
# starts it. $1 = pid, $2 = .dmg, $3 = installed .app.
_MAC_SCRIPT = r'''
while kill -0 "$1" 2>/dev/null; do sleep 0.3; done
MNT=$(mktemp -d /tmp/beacon-update.XXXXXX)
if hdiutil attach -nobrowse -noautoopen -quiet -mountpoint "$MNT" "$2"; then
    NEW=$(find "$MNT" -maxdepth 1 -name "*.app" | head -n 1)
    if [ -n "$NEW" ]; then
        rm -rf "$3.old"
        mv "$3" "$3.old" && ditto "$NEW" "$3" && rm -rf "$3.old" || mv "$3.old" "$3"
    fi
    hdiutil detach -quiet "$MNT"
fi
rmdir "$MNT" 2>/dev/null
open "$3"
'''


def _replace_mac_app(dmg):
    """Hand the update to _MAC_SCRIPT. Running from source (no .app to
    replace), just open the .dmg in Finder."""
    exe = os.path.realpath(sys.executable)
    app = exe.split("/Contents/MacOS/")[0] if "/Contents/MacOS/" in exe else ""
    if not getattr(sys, "frozen", False) or not app.endswith(".app"):
        subprocess.Popen(["open", dmg])
        return
    subprocess.Popen(["/bin/sh", "-c", _MAC_SCRIPT, "beacon-update", str(os.getpid()), dmg, app],
                     close_fds=True, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
