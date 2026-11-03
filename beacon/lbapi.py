"""LightBurn's REST API (LightBurn 2.2 and newer), standard library only.

LightBurn listens on http://127.0.0.1:19520 while it's open. A program pairs
once with POST /api/connect: LightBurn shows a consent pop-up, and if the user
approves within 30 s it returns a secret. Every other call sends
"Authorization: Bearer <token>", where the token is the hex HMAC-SHA256 of
the current Unix minute (as a decimal string) keyed with that secret.

Beacon asks for "state" (job state, progress, connection) and "project", for
the open file's name only (GET /api/project), shown while a job runs.
"project" would also allow reading the camera overlay image; Beacon never
asks for it. A pairing made before Beacon asked for "project" still reads
the state; the file name then comes from LightBurn's title bar instead.
Docs: https://github.com/LightBurnSoftware/LightBurn-REST"""

import hashlib
import hmac
import json
import re
import time
import urllib.error
import urllib.request

HOST = "127.0.0.1"
PORT = 19520
CAPABILITIES = ["state", "project"]
PAIR_TIMEOUT = 35            # LightBurn's consent pop-up waits 30 s

# Never send these local calls through a system proxy
# All requests go through this opener.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ApiError(RuntimeError):
    """code: HTTP status, or 0 when LightBurn couldn't be reached at all."""

    def __init__(self, message, code=0):
        super().__init__(message)
        self.code = code


def token(secret, now=None):
    """The bearer token for this minute: HMAC-SHA256 of the Unix minute number
    (as text) with the pairing secret as key, in hex. It changes every
    minute, so a captured token is useless soon after; LightBurn computes the
    same value to check it. now: a Unix time, for tests."""
    minute = int(time.time() if now is None else now) // 60
    return hmac.new(secret.encode(), str(minute).encode(), hashlib.sha256).hexdigest()


def _request(method, path, secret=None, body=None, timeout=3.0, host=HOST, port=PORT):
    """One HTTP request to LightBurn. Sends `body` as JSON, adds the
    Authorization header when a secret is given, and returns the JSON reply
    as a dict. Every failure becomes an ApiError with a message a user can
    understand (LightBurn's own "error" text when it sends one) and the HTTP
    code, so callers can tell "not allowed" (401/403) from "not running" (0)."""
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if secret:
        headers["Authorization"] = f"Bearer {token(secret)}"
    req = urllib.request.Request(f"http://{host}:{port}{path}", data=data, headers=headers,
                                 method=method)
    try:
        with _opener.open(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}").get("error", "")
        except ValueError:
            detail = ""
        raise ApiError(detail or f"LightBurn answered HTTP {e.code}", e.code) from e
    except (urllib.error.URLError, OSError) as e:
        raise ApiError("LightBurn's API isn't answering (LightBurn closed, or older than "
                       "2.2)") from e
    try:
        return json.loads(raw) if raw else {}
    except ValueError as e:
        raise ApiError("LightBurn sent something that isn't JSON") from e


def available(timeout=1.0):
    """True if something answers on LightBurn's API port (no pairing needed)."""
    try:
        with _opener.open(f"http://{HOST}:{PORT}/", timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def pair(app_name, capabilities=CAPABILITIES, timeout=PAIR_TIMEOUT):
    """Ask LightBurn for access; the user must approve its pop-up. Returns the secret."""
    try:
        got = _request("POST", "/api/connect",
                       body={"application_name": app_name, "capabilities": list(capabilities)},
                       timeout=timeout)
    except ApiError as e:
        if e.code == 403:
            raise ApiError("LightBurn didn't allow it: the pop-up was declined or not "
                           "answered within 30 seconds.", 403) from e
        raise
    secret = got.get("secret") or ""
    if not secret:
        raise ApiError("LightBurn approved but sent no key. Try again.")
    return secret


def get(path, secret, timeout=3.0):
    """An authenticated GET. 401/403 mean the pairing was revoked or is wrong."""
    try:
        return _request("GET", path, secret=secret, timeout=timeout)
    except ApiError as e:
        if e.code in (401, 403):
            raise ApiError("LightBurn no longer accepts Beacon's key. Connect again in "
                           "Settings.", e.code) from e
        raise


def snapshot(secret, timeout=2.0):
    """Job state, progress and connection in one call (GET /api/events/poll):
    {"job": {"state": "idle"|"running"|"paused", "progress"?: 0-100},
     "connection": {"connected": bool, "device_name": str, ...}, ...}"""
    return get("/api/events/poll", secret, timeout=timeout)


def project_name(secret, timeout=2.0):
    """The open project's name without folder or extension ("Coasters" for
    "C:\\Jobs\\Coasters.lbrn2"), or "" when it has none yet (GET
    /api/project). Raises ApiError; 401/403 here usually means the pairing
    was made without the "project" capability."""
    try:
        got = _request("GET", "/api/project", secret=secret, timeout=timeout)
    except ApiError as e:
        if e.code in (401, 403):
            raise ApiError("Beacon's key can't read the file name: press Connect again in "
                           "Settings to allow it.", e.code) from e
        raise
    name = re.split(r"[\\/]", str(got.get("filename") or ""))[-1]
    return re.sub(r"\.lbrn2?$", "", name, flags=re.I).strip()
