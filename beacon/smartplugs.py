"""Smart plugs as relay targets: Shelly (Gen1 and Gen2+), Tasmota, TP-Link Kasa
and Home Assistant entities. Standard library only."""

import json
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# What a relay button can switch: saved key -> label in the relay editor.
# "arduino" is the board's own relay pin; the rest are network devices, switched
# from the PlugWorker thread below.
PLUG_TYPES = {"arduino": "Arduino / Wi-Fi light pin", "shelly": "Shelly plug",
              "tasmota": "Tasmota plug", "kasa": "TP-Link Kasa plug",
              "homeassistant": "Home Assistant entity"}


def _get(url, timeout=4, data=None, headers=None):
    """A GET (or a POST when data is given). Returns (status, text); HTTP and
    network errors are raised and turned into readable messages by
    set_plug()."""
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def _host(address):
    """An address as a base URL: "192.168.1.50" -> "http://192.168.1.50"."""
    a = address.strip().rstrip("/")
    return a if a.startswith("http") else f"http://{a}"


def shelly(address, on):
    """Switch a Shelly plug's first relay. Newer devices (Gen2 and later) use
    the RPC API; if that path doesn't exist (404/400), it's a Gen1 device and
    the older /relay/0 URL is used."""
    base = _host(address)
    try:                                    # Gen2/Gen3/Plus devices
        code, _ = _get(f"{base}/rpc/Switch.Set?id=0&on={'true' if on else 'false'}")
        if code == 200:
            return
    except urllib.error.HTTPError as e:
        if e.code not in (404, 400):
            raise
    except urllib.error.URLError:
        raise
    _get(f"{base}/relay/0?turn={'on' if on else 'off'}")     # Gen1


def tasmota(address, on):
    """Switch a plug running Tasmota firmware with its "Power On/Off" command."""
    _get(f"{_host(address)}/cm?cmnd=" + urllib.parse.quote(f"Power {'On' if on else 'Off'}"))


def _kasa_crypt(data, decrypt=False):
    """TP-Link Kasa's simple XOR "encryption" (autokey cipher starting at 171):
    each byte is XORed with the previous encrypted byte. Not security, just
    the format the plugs expect."""
    key, out = 171, bytearray()
    for b in data:
        if decrypt:
            out.append(key ^ b)
            key = b
        else:
            key ^= b
            out.append(key)
    return bytes(out)


def kasa(address, on, timeout=4):
    """Switch a TP-Link Kasa plug over its local protocol: TCP port 9999, a
    4-byte big-endian length, then the encrypted JSON command. The reply has
    the same format; a non-zero err_code means the plug refused."""
    msg = json.dumps({"system": {"set_relay_state": {"state": 1 if on else 0}}}).encode()
    host = address.strip().replace("http://", "").split("/")[0]
    with socket.create_connection((host, 9999), timeout=timeout) as s:
        s.sendall(struct.pack(">I", len(msg)) + _kasa_crypt(msg))
        head = s.recv(4)
        if len(head) == 4:
            size, buf = struct.unpack(">I", head)[0], b""
            while len(buf) < size:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            reply = json.loads(_kasa_crypt(buf, decrypt=True) or b"{}")
            err = reply.get("system", {}).get("set_relay_state", {}).get("err_code", 0)
            if err:
                raise RuntimeError(f"Kasa error {err}")


def homeassistant(ha_url, ha_token, entity, on):
    """Switch a Home Assistant entity through its REST API, e.g.
    switch.exhaust_fan -> POST /api/services/switch/turn_on. The service
    domain comes from the entity ID, so light.* and fan.* entities work too.
    Needs a long-lived access token from the Home Assistant profile page."""
    if not (ha_url and ha_token and entity):
        raise RuntimeError("Set the Home Assistant address and token, and the entity ID.")
    domain = entity.split(".")[0] if "." in entity else "switch"
    url = f"{_host(ha_url)}/api/services/{domain}/turn_{'on' if on else 'off'}"
    _get(url, data=json.dumps({"entity_id": entity}).encode(),
         headers={"Authorization": f"Bearer {ha_token}", "Content-Type": "application/json"})


def set_plug(kind, address, on, ha_url="", ha_token="", entity=""):
    """Switch one smart plug. Raises RuntimeError with a readable reason."""
    try:
        if kind == "shelly":
            shelly(address, on)
        elif kind == "tasmota":
            tasmota(address, on)
        elif kind == "kasa":
            kasa(address, on)
        elif kind == "homeassistant":
            homeassistant(ha_url, ha_token, entity, on)
        else:
            raise RuntimeError(f"Unknown plug type {kind}")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise RuntimeError("Not authorised: check the token or the plug's login.") from e
        raise RuntimeError(f"The plug answered with error {e.code}.") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise RuntimeError(f"Can't reach the plug at {address or ha_url}.") from e


class PlugWorker(threading.Thread):
    """Keeps smart plugs in the wanted state: switches them on changes and
    re-sends every 60 s so a plug that was switched by hand or rebooted
    comes back in line. status[i] holds a short readable result per relay."""

    RESEND = 60.0

    def __init__(self):
        super().__init__(daemon=True)
        self.wanted = {}            # relay index -> (kind, address, entity, on)
        self.sent = {}              # relay index -> (args, time)
        self.status = {}
        self.ha = ("", "")
        self.wake = threading.Event()
        self.stop_evt = threading.Event()

    def set_wanted(self, wanted, ha):
        """Called by the app (UI thread) with what every plug should be; wakes
        the thread only when something changed. ha: (Home Assistant URL,
        token)."""
        self.ha = ha
        if wanted != self.wanted:
            self.wanted = dict(wanted)
            self.wake.set()

    def run(self):
        """Send each plug its wanted state when it changed or RESEND seconds
        passed. A failure is retried after 10 s instead of 60. Relays no
        longer using a plug are forgotten."""
        while not self.stop_evt.is_set():
            now = time.time()
            for i, args in list(self.wanted.items()):
                last = self.sent.get(i)
                if last and last[0] == args and now - last[1] < self.RESEND:
                    continue
                kind, address, entity, on = args
                try:
                    set_plug(kind, address, on, self.ha[0], self.ha[1], entity)
                    self.status[i] = ("On" if on else "Off") + " ✓"
                    self.sent[i] = (args, now)
                except RuntimeError as e:
                    self.status[i] = str(e)
                    self.sent[i] = (args, now - self.RESEND + 10)     # retry in 10 s
            for i in list(self.sent):
                if i not in self.wanted:
                    self.sent.pop(i, None)
                    self.status.pop(i, None)
            self.wake.wait(2.0)
            self.wake.clear()
