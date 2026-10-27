"""Windows integration: notification-area icon with a menu and pop-ups, a
global hotkey, "start with Windows", and how long the PC has been idle.

TrayHost owns a hidden window whose messages Tk's own message loop
dispatches (they share the UI thread). Its window procedure runs while Tcl is
in the middle of dispatching, outside tkinter's own callback handling, so it
must NOT touch Tk: calling a widget there leaves tkinter's thread state empty
and the next Tk callback aborts Python ("PyEval_RestoreThread ... NULL").
Callbacks are therefore handed to `post` (e.g. a queue the app drains from a
Tk `after` loop) instead of being called directly."""

import ctypes
import ctypes.wintypes
import os
import sys
import winreg

import win32api
import win32con
import win32gui

WM_TRAY = win32con.WM_USER + 20
NIF_INFO, NIIF_USER, NIIF_LARGE_ICON = 0x10, 0x04, 0x20
HOTKEY_ID = 0xB33C
MOD_NOREPEAT = 0x4000
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


# ------------------------------------------------------------ start with Windows
def launch_command(script_path):
    """The command that starts this app: the .exe when bundled, else pythonw + script."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --startup'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pyw if os.path.exists(pyw) else sys.executable}" "{script_path}" --startup'


def autostart_get(name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, name)
            return True
    except OSError:
        return False


def autostart_set(name, on, command):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(k, name)
            except OSError:
                pass


# ------------------------------------------------------------ Windows Firewall
FIREWALL_RULE = "Beacon phone dashboard"


def firewall_allowed(port, rule=FIREWALL_RULE):
    """True if our inbound rule for this port exists (reading needs no admin)."""
    import subprocess
    try:
        out = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={rule}"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return None                     # can't tell
    return any(line.split(":")[-1].strip() == str(port)
               for line in out.splitlines() if line.strip().startswith("LocalPort"))


def firewall_allow(port, rule=FIREWALL_RULE):
    """Ask Windows (one UAC prompt the user answers) to let phones on Private
    networks reach the dashboard port. False if the prompt was declined."""
    rule = f'name="{rule}"'
    cmd = (f'/c netsh advfirewall firewall delete rule {rule} & '
           f'netsh advfirewall firewall add rule {rule} dir=in action=allow '
           f'protocol=TCP localport={int(port)} profile=private')
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", cmd, None, 0) > 32


# ------------------------------------------------------------------ idle time
class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.wintypes.UINT), ("dwTime", ctypes.wintypes.DWORD)]


def idle_seconds():
    """Seconds since the last keyboard or mouse input on this PC."""
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0


# ------------------------------------------------------------- tray + hotkey
class TrayHost:
    """on_menu(x, y): right-click; on_open(): double-click; on_hotkey().
    post(fn): runs fn later on the UI thread (see the module docstring)."""

    def __init__(self, name, icon_path, on_menu=None, on_open=None, on_hotkey=None, post=None):
        self.name, self.icon_path = name, icon_path
        self.on_menu, self.on_open, self.on_hotkey = on_menu, on_open, on_hotkey
        self.post = post or (lambda fn: fn())
        self.hwnd = self.hicon = None
        self.icon_shown = False
        self.hotkey = None
        self._create_window()

    def _create_window(self):
        wc = win32gui.WNDCLASS()
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpszClassName = f"BeaconTrayHost{id(self)}"
        wc.lpfnWndProc = {WM_TRAY: self._on_tray, win32con.WM_HOTKEY: self._on_hotkey_msg,
                          win32con.WM_DESTROY: lambda *a: 0}
        try:
            atom = win32gui.RegisterClass(wc)
        except win32gui.error:
            atom = wc.lpszClassName
        self.hwnd = win32gui.CreateWindow(atom, self.name, 0, 0, 0, 0, 0, 0, 0, wc.hInstance, None)
        if os.path.exists(self.icon_path):
            self.hicon = win32gui.LoadImage(0, self.icon_path, win32con.IMAGE_ICON, 0, 0,
                                            win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE)
        else:
            self.hicon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)

    # tray icon -----------------------------------------------------------
    def show_icon(self):
        if self.icon_shown:
            return True
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, (
                self.hwnd, 1, win32gui.NIF_ICON | win32gui.NIF_TIP | win32gui.NIF_MESSAGE,
                WM_TRAY, self.hicon, self.name))
            self.icon_shown = True
        except win32gui.error:
            self.icon_shown = False
        return self.icon_shown

    def hide_icon(self):
        if self.icon_shown:
            try:
                win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.hwnd, 1))
            except win32gui.error:
                pass
            self.icon_shown = False

    def set_tip(self, tip):
        if self.icon_shown:
            try:
                win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, (
                    self.hwnd, 1, win32gui.NIF_TIP, 0, self.hicon, tip[:127]))
            except win32gui.error:
                pass

    def notify(self, title, body):
        """A Windows notification (Windows 10/11 show it as a toast and keep it
        in the Notification Centre)."""
        if not self.show_icon():
            return False
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, (
                self.hwnd, 1, NIF_INFO, WM_TRAY, self.hicon, self.name, body[:255], 10000,
                title[:63], NIIF_USER | NIIF_LARGE_ICON))
            return True
        except win32gui.error:
            return False

    show = notify                   # same call the older notifier used

    def _on_tray(self, hwnd, msg, wparam, lparam):
        if lparam in (win32con.WM_RBUTTONUP, win32con.WM_CONTEXTMENU) and self.on_menu:
            x, y = win32gui.GetCursorPos()
            self.post(lambda: self.on_menu(x, y))
        elif lparam in (win32con.WM_LBUTTONDBLCLK, win32con.WM_LBUTTONUP) and self.on_open:
            self.post(self.on_open)
        return 0

    # hotkey ----------------------------------------------------------------
    def set_hotkey(self, key):
        """key: a letter, e.g. "B" for Ctrl+Alt+B; None to remove. True if registered."""
        self.clear_hotkey()
        if not key:
            return True
        ok = ctypes.windll.user32.RegisterHotKey(
            self.hwnd, HOTKEY_ID, win32con.MOD_CONTROL | win32con.MOD_ALT | MOD_NOREPEAT,
            ord(key.upper()))
        self.hotkey = key if ok else None
        return bool(ok)

    def clear_hotkey(self):
        if self.hotkey:
            ctypes.windll.user32.UnregisterHotKey(self.hwnd, HOTKEY_ID)
            self.hotkey = None

    def _on_hotkey_msg(self, hwnd, msg, wparam, lparam):
        if wparam == HOTKEY_ID and self.on_hotkey:
            self.post(self.on_hotkey)
        return 0

    def remove(self):
        self.close()

    def close(self):
        self.clear_hotkey()
        self.hide_icon()
        try:
            win32gui.DestroyWindow(self.hwnd)
        except win32gui.error:
            pass
