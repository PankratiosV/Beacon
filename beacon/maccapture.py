"""Finding, capturing and reading LightBurn's windows on macOS: the same four
calls laser_status_app.py makes on Windows (find_lightburn_windows,
lightburn_running, capture_best, window_title), plus VisionOCR, the text
recognition built into macOS.

A "window" here is its CGWindowID (an int), standing in for the hwnd.
Window titles and pictures of other apps' windows need the Screen Recording
permission (System Settings > Privacy & Security > Screen & System Audio
Recording); without it titles come back empty and pictures blank, and
`screen_permission()` says so."""

import ctypes
import io
import re
import threading

import objc
import psutil
import Quartz
from PIL import Image

# Windows smaller than this (points) can't hold the Laser panel: tool windows,
# pop-ups, LightBurn's splash
MIN_W, MIN_H = 200, 120


def screen_permission(ask=False):
    """True if Beacon may capture other apps' windows. ask=True shows macOS'
    permission prompt (once; after that the user must use System Settings)."""
    try:
        if Quartz.CGPreflightScreenCaptureAccess():
            return True
        if ask:
            return bool(Quartz.CGRequestScreenCaptureAccess())
    except AttributeError:          # macOS before 10.15: no such permission
        return True
    return False


# --------------------------------------------- the window list, as plain C calls
# Read with ctypes rather than pyobjc: through pyobjc, every value read from
# the list (a window's number, owner, size...) left a small wrapper object
# behind that was never freed, ~10 KB per lookup with ~100 windows open and
# several lookups a second. Here each list is converted to plain Python and
# released at once.
_CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_CG = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
_P = ctypes.c_void_p


class _CGRect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double),
                ("width", ctypes.c_double), ("height", ctypes.c_double)]


for _fn, _res, _args in (
        (_CG.CGWindowListCopyWindowInfo, _P, [ctypes.c_uint32, ctypes.c_uint32]),
        (_CG.CGRectMakeWithDictionaryRepresentation, ctypes.c_bool, [_P, ctypes.POINTER(_CGRect)]),
        (_CF.CFArrayGetCount, ctypes.c_long, [_P]),
        (_CF.CFArrayGetValueAtIndex, _P, [_P, ctypes.c_long]),
        (_CF.CFDictionaryGetValue, _P, [_P, _P]),
        (_CF.CFNumberGetValue, ctypes.c_bool, [_P, ctypes.c_int, _P]),
        (_CF.CFBooleanGetValue, ctypes.c_bool, [_P]),
        (_CF.CFStringGetLength, ctypes.c_long, [_P]),
        (_CF.CFStringGetMaximumSizeForEncoding, ctypes.c_long, [ctypes.c_long, ctypes.c_uint32]),
        (_CF.CFStringGetCString, ctypes.c_bool, [_P, ctypes.c_char_p, ctypes.c_long,
                                                 ctypes.c_uint32]),
        (_CF.CFRelease, None, [_P])):
    _fn.restype, _fn.argtypes = _res, _args

# The dictionary keys (CFString constants exported by CoreGraphics)
_KEY = {k: _P.in_dll(_CG, "kCGWindow" + k).value
        for k in ("Number", "Layer", "Bounds", "OwnerName", "Name", "IsOnscreen")}
LIST_ALL, LIST_INCLUDING_WINDOW, LIST_EXCLUDE_DESKTOP = 0, 1 << 3, 1 << 4
CF_SINT64, CF_UTF8 = 4, 0x08000100


def _cf_int(ref):
    out = ctypes.c_int64()
    return out.value if ref and _CF.CFNumberGetValue(ref, CF_SINT64, ctypes.byref(out)) else 0


def _cf_str(ref):
    if not ref:
        return ""
    size = _CF.CFStringGetMaximumSizeForEncoding(_CF.CFStringGetLength(ref), CF_UTF8) + 1
    buf = ctypes.create_string_buffer(size)
    ok = _CF.CFStringGetCString(ref, buf, size, CF_UTF8)
    return buf.value.decode("utf-8", "replace") if ok else ""


def _window_list(option, relative_to=0):
    """Windows as plain dicts: id, layer, owner, name, onscreen, width,
    height (points), front to back."""
    arr = _CG.CGWindowListCopyWindowInfo(option, relative_to)
    if not arr:
        return []
    try:
        out = []
        for i in range(_CF.CFArrayGetCount(arr)):
            d = _CF.CFArrayGetValueAtIndex(arr, i)
            rect = _CGRect()
            bounds = _CF.CFDictionaryGetValue(d, _KEY["Bounds"])
            if bounds:
                _CG.CGRectMakeWithDictionaryRepresentation(bounds, ctypes.byref(rect))
            onscreen = _CF.CFDictionaryGetValue(d, _KEY["IsOnscreen"])
            out.append({"id": _cf_int(_CF.CFDictionaryGetValue(d, _KEY["Number"])),
                        "layer": _cf_int(_CF.CFDictionaryGetValue(d, _KEY["Layer"])),
                        "owner": _cf_str(_CF.CFDictionaryGetValue(d, _KEY["OwnerName"])),
                        "name": _cf_str(_CF.CFDictionaryGetValue(d, _KEY["Name"])),
                        "onscreen": bool(onscreen and _CF.CFBooleanGetValue(onscreen)),
                        "width": rect.width, "height": rect.height})
        return out
    finally:
        _CF.CFRelease(arr)          # "Copy" in the name: ours to release


def _window_infos():
    """Every window of every app (on screen or not), front to back."""
    return _window_list(LIST_ALL | LIST_EXCLUDE_DESKTOP)


def _one_window(wid):
    """The list entry for one window, or None if it's gone."""
    return next((w for w in _window_list(LIST_INCLUDING_WINDOW, wid) if w["id"] == wid), None)


def find_lightburn_windows(include_minimized=False):
    """IDs of LightBurn's normal windows that are big enough to hold the Laser
    panel. Minimised (or on another Space) windows aren't on screen and can't
    be pictured, so they're only included on request, for their titles."""
    return [w["id"] for w in _window_infos()
            if "lightburn" in w["owner"].lower() and w["layer"] == 0
            and w["width"] >= MIN_W and w["height"] >= MIN_H
            and (w["onscreen"] or include_minimized)]


def lightburn_running():
    """True if any LightBurn process exists, even with no window open."""
    for p in psutil.process_iter(["name"]):
        if "lightburn" in (p.info.get("name") or "").lower():
            return True
    return False


def window_title(wid):
    """A window's title ("" if it has none, or without Screen Recording
    permission)."""
    w = _one_window(wid)
    return w["name"] if w else ""


def _cgimage_to_pil(cg):
    """A CGImage (32-bit BGRA, as window captures come) as an RGB PIL image.

    The pixels are read through memoryview(data), never bytes(data): with
    pyobjc, bytes() of a CFData makes a copy that is never freed, so every
    capture leaked the whole picture (~10 MB for a big window; at "Check
    every 0.1 s" that ran a Mac out of memory in minutes). frombytes()
    copies the pixels into the image, so nothing points into `data` after."""
    w, h = Quartz.CGImageGetWidth(cg), Quartz.CGImageGetHeight(cg)
    if not w or not h:
        return None
    stride = Quartz.CGImageGetBytesPerRow(cg)
    data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(cg))
    return Image.frombytes("RGB", (w, h), memoryview(data), "raw", "BGRX", stride, 1)


def _capture_cg(wid):
    """Capture with CGWindowListCreateImage: fast and synchronous, and shows
    the window even when others cover it. Deprecated in macOS 15, so it may
    be missing; then None."""
    fn = getattr(Quartz, "CGWindowListCreateImage", None)
    if fn is None:
        return None
    with objc.autorelease_pool():
        cg = fn(Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow, wid,
                Quartz.kCGWindowImageBoundsIgnoreFraming)
        return _cgimage_to_pil(cg) if cg is not None else None


def _capture_sck(wid, timeout=3.0):
    """Capture with ScreenCaptureKit (macOS 14+), the current API. It's
    asynchronous, so this waits for its completion handlers."""
    try:
        import ScreenCaptureKit as SCK
    except ImportError:
        return None
    if not hasattr(SCK, "SCScreenshotManager"):
        return None
    done, out = threading.Event(), {}

    def got_image(cg, err):
        if cg is not None:
            out["img"] = _cgimage_to_pil(cg)
        done.set()

    def got_content(content, err):
        window = None
        if content is not None:
            window = next((w for w in content.windows() if w.windowID() == wid), None)
        if window is None:
            done.set()
            return
        flt = SCK.SCContentFilter.alloc().initWithDesktopIndependentWindow_(window)
        cfg = SCK.SCStreamConfiguration.alloc().init()
        frame, scale = window.frame(), flt.pointPixelScale() if hasattr(
            flt, "pointPixelScale") else 2.0
        cfg.setWidth_(int(frame.size.width * scale))
        cfg.setHeight_(int(frame.size.height * scale))
        cfg.setShowsCursor_(False)
        SCK.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
            flt, cfg, got_image)

    with objc.autorelease_pool():
        SCK.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
            True, True, got_content)
        done.wait(timeout)
    return out.get("img")


def is_blank(img):
    """True for an all-one-colour image (what a capture without permission
    returns)."""
    lo, hi = img.convert("L").resize((64, 64)).getextrema()
    return hi - lo < 10


def _points_wide(wid):
    """The window's width in screen points, or 0."""
    w = _one_window(wid)
    return w["width"] if w else 0.0


def capture_best(wid):
    """A picture of the window, whichever way gives a real one, or None.
    Captures are in real pixels (twice the points on a Retina screen);
    img.info["scale"] says how many, so callers can size boxes in points.

    Nothing is captured without the Screen Recording permission: every
    attempt makes macOS show its "would like to record this computer's
    screen" request again, which at one check a second never stops. macOS
    only applies a newly given permission after Beacon restarts."""
    if not screen_permission():
        return None
    for grab in (_capture_cg, _capture_sck):
        try:
            img = grab(wid)
        except Exception:
            img = None
        if img is not None and not is_blank(img):
            pts = _points_wide(wid)
            img.info["scale"] = round(img.width / pts, 2) if pts else 1
            return img
    return None


# ===========================================================================
# Text recognition with Apple's Vision framework (built in, on-device).
# read() returns [(line_text, [(word, x, y, w, h), ...])] in the coordinates
# of the image passed in, the same contract as WindowsOCR.
# ===========================================================================
class VisionOCR:
    """Vision has two engines. "Fast" is 10-50x quicker on an Intel Mac
    (0.16 s for LightBurn's status box, 0.7 s for a whole 2560 px window,
    against 0.6 s and 16 s) and reads nearly all of LightBurn's text, but now
    and then misreads a short word ("Ready" at 13 px came back "Reaty"). So
    small pictures where Fast finds none of `retry_words` are read again with
    "Accurate" at their own size. Whole windows never are: too slow."""
    name = "macOS Vision"
    retry_words = frozenset()       # set by the app: LightBurn's status words
    RETRY_MAX_PIXELS = 400_000      # the status box is ~50k px (200k on Retina)

    def __init__(self):
        import Vision
        self.Vision = Vision
        # Fail now (not on the first read) if Vision can't run here
        Vision.VNRecognizeTextRequest.alloc().init()

    def read(self, img, max_scale=2.0):
        """Recognise the text in img (see the class docstring for which
        engine)."""
        V = self.Vision
        out = self._read(img, max_scale, V.VNRequestTextRecognitionLevelFast)
        if (self.retry_words and img.width * img.height <= self.RETRY_MAX_PIXELS
                and not self._has_word(out)):
            out = self._read(img, 1.0, V.VNRequestTextRecognitionLevelAccurate)
        return out

    def _has_word(self, lines):
        """True if any of retry_words is in the text read."""
        return any(w in self.retry_words
                   for text, _ in lines for w in re.findall(r"[a-z]+", text.lower()))

    def _read(self, img, max_scale, level):
        """One Vision pass. Small images are enlarged up to max_scale, as on
        Windows; word boxes are scaled back to img's own coordinates. A
        Retina capture is already 2x (img.info["scale"]), so it is enlarged
        that much less."""
        V = self.Vision
        max_scale = max(1.0, max_scale / img.info.get("scale", 1))
        scale = min(max_scale, 4000 / max(img.width, img.height))
        if scale > 1.01:
            img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
        else:
            scale = 1.0
        W, H = img.width, img.height
        png = io.BytesIO()
        img.convert("RGB").save(png, format="PNG")
        out = []
        with objc.autorelease_pool():
            from Foundation import NSData
            data = NSData.dataWithBytes_length_(png.getvalue(), len(png.getvalue()))
            req = V.VNRecognizeTextRequest.alloc().init()
            req.setRecognitionLevel_(level)
            req.setUsesLanguageCorrection_(False)     # status words, not prose
            req.setRecognitionLanguages_(["en-US"])
            # Vision skips text under 1/32 of the image height by default,
            # which is all of a UI's text in a whole-window picture
            req.setMinimumTextHeight_(min(0.03125, 6.0 / H))
            handler = V.VNImageRequestHandler.alloc().initWithData_options_(data, None)
            ok, err = handler.performRequests_error_([req], None)
            if not ok:
                raise RuntimeError(f"Vision: {err}")
            for obs in req.results() or []:
                cands = obs.topCandidates_(1)
                if not cands:
                    continue
                text = str(cands[0].string())
                # Vision's boxes are 0-1 with the origin at the bottom left
                r = obs.boundingBox()
                lx, ly = r.origin.x * W, (1 - r.origin.y - r.size.height) * H
                lw, lh = r.size.width * W, r.size.height * H
                # Each word's box is its share of the line's box, by character
                # position. Asking Vision for every word's exact box
                # (boundingBoxForRange) left ~190 KB of wrapper objects behind
                # per whole-window read, never freed. Measured on LightBurn:
                # the status word's box comes out identical, 95% of words
                # within 13 px.
                n, pos, words = max(1, len(text)), 0, []
                for word in text.split():
                    start = text.index(word, pos)
                    pos = start + len(word)
                    words.append((word, (lx + lw * start / n) / scale, ly / scale,
                                  lw * len(word) / n / scale, lh / scale))
                out.append((text, words))
        return out
