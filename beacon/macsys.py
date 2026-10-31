"""macOS integration, the same calls as beacon/winsys.py: a menu-bar icon,
notifications, a global hotkey, "open at login", the firewall check and how
long the Mac has been idle.

The menu-bar icon and the hotkey call back from inside Tk's own Cocoa event
loop (Tk owns NSApplication on macOS). As on Windows, a callback there must
not touch Tk directly, so every callback is handed to `post` (the app's
queue, drained by its Tk `after` loop) instead of being called directly."""

import ctypes
import ctypes.util
import os
import plistlib
import subprocess
import sys

import objc
from AppKit import (NSApplication, NSEvent, NSImage, NSMakeSize, NSObject, NSScreen,
                    NSStatusBar, NSVariableStatusItemLength)
from Foundation import NSBundle

LAUNCH_AGENTS = os.path.expanduser("~/Library/LaunchAgents")


def activate_app():
    """Bring Beacon in front of other apps (macOS only raises windows of the
    active app)."""
    try:
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass


# ------------------------------------------------------------- open at login
def launch_command(script_path, extra=""):
    """The command that starts this app, as a list: the app's own executable
    when bundled, else this Python + the script."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--startup"]
    else:
        cmd = [sys.executable, script_path, "--startup"]
    return cmd + ([extra] if extra else [])


def _agent_path(name):
    """~/Library/LaunchAgents/<label>.plist for an app called `name`."""
    label = "com.beacon." + "".join(c for c in name.lower() if c.isalnum())
    return label, os.path.join(LAUNCH_AGENTS, label + ".plist")


def autostart_get(name):
    """True if an "open at login" entry called `name` exists."""
    return os.path.exists(_agent_path(name)[1])


def autostart_set(name, on, command):
    """Add (on=True) or remove the "open at login" entry: a LaunchAgent, which
    macOS runs at sign-in. Per user, no admin rights."""
    label, path = _agent_path(name)
    if not on:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    os.makedirs(LAUNCH_AGENTS, exist_ok=True)
    with open(path, "wb") as f:
        plistlib.dump({"Label": label, "ProgramArguments": list(command),
                       "RunAtLoad": True, "ProcessType": "Interactive"}, f)


# ------------------------------------------------------------- firewall
FIREWALL_RULE = "Beacon phone dashboard"
SOCKETFILTERFW = "/usr/libexec/ApplicationFirewall/socketfilterfw"


def firewall_allowed(port, rule=FIREWALL_RULE):
    """True if the macOS firewall is off (phones can connect); None if it's on,
    because macOS then asks the user itself the first time Beacon listens."""
    try:
        out = subprocess.run([SOCKETFILTERFW, "--getglobalstate"], capture_output=True,
                             text=True, timeout=10).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return None
    return True if "disabled" in out else None


def firewall_allow(port, rule=FIREWALL_RULE):
    """Open the firewall settings, where the user can allow Beacon."""
    subprocess.Popen(["open", "x-apple.systempreferences:com.apple.Network-Settings.extension"
                      "?Firewall"])
    return True


# ------------------------------------------------------------------ idle time
_cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics")
                  or "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
_cg.CGEventSourceSecondsSinceLastEventType.restype = ctypes.c_double
_cg.CGEventSourceSecondsSinceLastEventType.argtypes = [ctypes.c_int32, ctypes.c_uint32]
COMBINED_SESSION_STATE, ANY_INPUT_EVENT = 0, 0xFFFFFFFF


def idle_seconds():
    """Seconds since the last keyboard or mouse input on this Mac."""
    try:
        return float(_cg.CGEventSourceSecondsSinceLastEventType(
            COMBINED_SESSION_STATE, ANY_INPUT_EVENT))
    except Exception:
        return 0.0


# ------------------------------------------------------------------ hotkey
# Carbon's RegisterEventHotKey: still the standard way to own a system-wide
# shortcut, and unlike an event monitor it needs no Accessibility permission.
_carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")


class _EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
_carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
_carbon.InstallEventHandler.argtypes = [ctypes.c_void_p, _HANDLER, ctypes.c_uint32,
                                        ctypes.POINTER(_EventTypeSpec), ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.c_void_p)]
_carbon.RegisterEventHotKey.argtypes = [ctypes.c_uint32, ctypes.c_uint32, _EventHotKeyID,
                                        ctypes.c_void_p, ctypes.c_uint32,
                                        ctypes.POINTER(ctypes.c_void_p)]
_carbon.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]


def _fourcc(s):
    return int.from_bytes(s.encode(), "big")


K_EVENT_CLASS_KEYBOARD, K_EVENT_HOTKEY_PRESSED = _fourcc("keyb"), 5
CONTROL_KEY, OPTION_KEY = 0x1000, 0x0800
# Virtual key codes of the ANSI keyboard layout (Carbon's kVK_ANSI_*).
KEY_CODES = dict(zip("ASDFHGZXCVBQWERYTOUIPLJKNM",
                     (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 15, 16, 17, 31, 32, 34,
                      35, 37, 38, 40, 45, 46)))
HOTKEY_LABEL = "⌃ ⌥ {}"       # ⌃ ⌥ B: Control + Option, the Mac's Ctrl + Alt


# ------------------------------------------------------------- menu-bar icon
class _StatusTarget(NSObject):
    """Receives the menu-bar button's clicks (Cocoa needs an Objective-C object
    with an action method)."""

    def initWithHost_(self, host):
        self = objc.super(_StatusTarget, self).init()
        if self is not None:
            self.host = host
        return self

    def clicked_(self, sender):
        self.host._on_click()


class TrayHost:
    """Same interface as winsys.TrayHost. on_menu(x, y): click on the menu-bar
    icon (x, y in Tk's screen coordinates, just under the icon); on_open():
    unused on macOS (the Dock icon opens the window); on_hotkey().
    post(fn): runs fn later on the UI thread (see the module docstring)."""

    def __init__(self, name, icon_path, on_menu=None, on_open=None, on_hotkey=None, post=None):
        self.name, self.icon_path = name, icon_path
        self.on_menu, self.on_open, self.on_hotkey = on_menu, on_open, on_hotkey
        self.post = post or (lambda fn: fn())
        self.item = None
        self.icon_shown = False
        self.hotkey = None
        self._hotkey_ref = None
        self._handler = None            # the C callback must outlive its registration
        self._target = _StatusTarget.alloc().initWithHost_(self)
        self._notifier = _Notifier(name)

    # menu-bar icon -------------------------------------------------------
    def show_icon(self):
        """Put the icon in the menu bar. True if it's there."""
        if self.icon_shown:
            return True
        try:
            self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(
                NSVariableStatusItemLength)
            button = self.item.button()
            img = NSImage.alloc().initWithContentsOfFile_(self.icon_path) \
                if os.path.exists(self.icon_path) else None
            if img is not None:
                img.setSize_(NSMakeSize(18, 18))
                button.setImage_(img)
            else:
                button.setTitle_("●")
            button.setToolTip_(self.name)
            button.setTarget_(self._target)
            button.setAction_("clicked:")
            self.icon_shown = True
        except Exception:
            self.icon_shown = False
        return self.icon_shown

    def hide_icon(self):
        """Remove the icon from the menu bar."""
        if self.icon_shown and self.item is not None:
            try:
                NSStatusBar.systemStatusBar().removeStatusItem_(self.item)
            except Exception:
                pass
        self.item = None
        self.icon_shown = False

    def set_tip(self, tip):
        """The text shown when hovering the icon."""
        if self.icon_shown and self.item is not None:
            try:
                self.item.button().setToolTip_(tip)
            except Exception:
                pass

    def notify(self, title, body):
        """A macOS notification (kept in Notification Centre)."""
        return self._notifier.send(title, body)

    show = notify

    def _on_click(self):
        """The menu-bar icon was clicked: open the app's menu just under it.
        Cocoa's screen origin is the bottom-left of the main screen, Tk's the
        top-left, so y is flipped."""
        if not self.on_menu:
            return
        try:
            frame = self.item.button().window().frame()
            x = frame.origin.x
            y = NSScreen.screens()[0].frame().size.height - frame.origin.y
        except Exception:
            loc = NSEvent.mouseLocation()
            x, y = loc.x, NSScreen.screens()[0].frame().size.height - loc.y
        self.post(lambda: self.on_menu(int(x), int(y)))

    # hotkey ----------------------------------------------------------------
    def set_hotkey(self, key):
        """key: a letter, e.g. "B" for Control+Option+B; None to remove. True
        if registered."""
        self.clear_hotkey()
        if not key:
            return True
        code = KEY_CODES.get(key.upper())
        if code is None:
            return False
        target = _carbon.GetApplicationEventTarget()
        if self._handler is None:
            def handler(next_handler, event, user_data):
                if self.on_hotkey:
                    self.post(self.on_hotkey)
                return 0
            self._handler = _HANDLER(handler)
            spec = _EventTypeSpec(K_EVENT_CLASS_KEYBOARD, K_EVENT_HOTKEY_PRESSED)
            _carbon.InstallEventHandler(target, self._handler, 1, ctypes.byref(spec),
                                        None, None)
        ref = ctypes.c_void_p()
        err = _carbon.RegisterEventHotKey(code, CONTROL_KEY | OPTION_KEY,
                                          _EventHotKeyID(_fourcc("Bcon"), 1), target, 0,
                                          ctypes.byref(ref))
        if err != 0:
            return False
        self._hotkey_ref, self.hotkey = ref, key
        return True

    def clear_hotkey(self):
        """Release the hotkey, if one is registered."""
        if self._hotkey_ref is not None:
            _carbon.UnregisterEventHotKey(self._hotkey_ref)
            self._hotkey_ref = None
        self.hotkey = None

    def remove(self):
        """Remove the icon and hotkey (called when Beacon quits)."""
        self.close()

    def close(self):
        self.clear_hotkey()
        self.hide_icon()


# ------------------------------------------------------------- notifications
class _Notifier:
    """Notifications through UserNotifications when Beacon runs as an app
    bundle (they show Beacon's icon and name); otherwise, or if the user
    declined them, through AppleScript's `display notification`."""

    def __init__(self, name):
        self.name = name
        self.center = None
        if NSBundle.mainBundle().bundleIdentifier():
            try:
                import UserNotifications as UN
                self.UN = UN
                self.center = UN.UNUserNotificationCenter.currentNotificationCenter()
                self.center.requestAuthorizationWithOptions_completionHandler_(
                    UN.UNAuthorizationOptionAlert | UN.UNAuthorizationOptionSound,
                    lambda granted, err: None)
            except Exception:
                self.center = None

    def send(self, title, body):
        if self.center is not None:
            try:
                UN = self.UN
                content = UN.UNMutableNotificationContent.alloc().init()
                content.setTitle_(title)
                content.setBody_(body)
                req = UN.UNNotificationRequest.requestWithIdentifier_content_trigger_(
                    f"beacon-{os.getpid()}-{id(content)}", content, None)
                self.center.addNotificationRequestWithCompletionHandler_(req, None)
                return True
            except Exception:
                pass
        script = (f"display notification {_applescript_str(body)} "
                  f"with title {_applescript_str(title)}")
        try:
            subprocess.Popen(["osascript", "-e", script],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            return False


def _applescript_str(s):
    """A Python string as an AppleScript string literal."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'
