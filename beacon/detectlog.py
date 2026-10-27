"""Detection check: an opt-in log for measuring how well Beacon reads
LightBurn, e.g. during a beta. Everything stays in one folder on this PC:

  summary.json   running totals since the check was switched on (or reset)
  events.jsonl   one line per status change, suspected misread, unreadable
                 spell and "status is wrong" report, with the text that was read
  pictures/      a small picture of LightBurn's status area for each change,
                 suspected misread and report, so a person can check them

Only the status area of LightBurn's Laser panel is pictured, never the screen.
Nothing is sent anywhere: testers export a zip and email it."""

import json
import os
import shutil
import threading
import time
import zipfile
from collections import deque
from datetime import datetime

FLIP_SECONDS = 3.0        # A -> B -> A this fast is counted as a likely misread
MAX_PICTURES = 400        # oldest pictures are deleted beyond this
# _UNSET: "no status seen yet", different from None (which means "not found").
_UNSET = object()


def _blank():
    """Fresh running totals. seconds_* split the time between readings into
    LightBurn open and readable / open but unreadable / closed; by_raw and
    by_mode count readings per raw status and per way of reading; changes,
    flips, glitches, unreadable_spells and reports count events."""
    return {"since": time.time(), "reads": 0,
            "seconds_open": 0.0, "seconds_read": 0.0, "seconds_unreadable": 0.0,
            "seconds_closed": 0.0, "by_raw": {}, "by_mode": {}, "changes": 0,
            "flips": 0, "glitches": 0, "unreadable_spells": 0, "reports": 0,
            "report_kinds": {}}


def _human(seconds):
    """"45s", "12m", "2h 05m"."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    h, m = divmod(seconds // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


class DetectionLog:
    """Fed every detector reading by the UI thread (record), plus the user's
    reports (report). Cheap enough to run all the time while enabled."""

    def __init__(self, folder):
        self.folder = folder
        self.pictures = os.path.join(folder, "pictures")
        self.enabled = False
        self.recent = deque(maxlen=30)          # last readings, for reports
        self._last_t = None
        self._shown = _UNSET
        self._changes = deque(maxlen=3)         # (time, shown) of recent changes
        self._odd = None                        # a reading that disagreed with the status
        self._unreadable_since = None
        self._saved_at = 0.0
        self._last_crop = None
        self._lock = threading.Lock()
        self.summary = self._load()

    # ---------------------------------------------------------------- files
    def _load(self):
        """The saved totals, or fresh ones. Unknown keys from other versions are
        dropped and missing ones filled in."""
        try:
            with open(os.path.join(self.folder, "summary.json"), encoding="utf-8") as f:
                s = json.load(f)
            base = _blank()
            base.update({k: v for k, v in s.items() if k in base})
            return base
        except (OSError, ValueError):
            return _blank()

    def save(self):
        """Write the totals to summary.json (also done once a minute by record)."""
        self._saved_at = time.time()
        try:
            os.makedirs(self.folder, exist_ok=True)
            with open(os.path.join(self.folder, "summary.json"), "w", encoding="utf-8") as f:
                json.dump(self.summary, f, indent=1)
        except OSError:
            pass

    def _event(self, kind, msg, now, crop=None, **extra):
        """Append one event line to events.jsonl: the time, the event kind, what
        was shown and read, how, the note, the first lines of text read, any
        extra fields, and the name of its picture if one is saved."""
        e = {"time": datetime.fromtimestamp(now).isoformat(timespec="milliseconds"),
             "event": kind, "shown": msg.get("shown"), "raw": msg.get("raw"),
             "read_by": msg.get("mode"), "note": msg.get("note") or "",
             "text": list(msg.get("lines") or [])[:8]}
        e.update(extra)
        if crop is not None:
            name = f"{datetime.fromtimestamp(now):%Y%m%d-%H%M%S-%f}"[:-3] + f"-{kind}.png"
            e["picture"] = name
            self._save_picture(crop.copy(), name)
        try:
            os.makedirs(self.folder, exist_ok=True)
            with self._lock, open(os.path.join(self.folder, "events.jsonl"), "a",
                                  encoding="utf-8") as f:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _save_picture(self, img, name):
        """Save a PNG of the status area on a background thread (so the UI never
        waits on the disk), then delete the oldest pictures beyond
        MAX_PICTURES. File names start with the date and time, so sorting
        them sorts by age."""
        def work():
            try:
                os.makedirs(self.pictures, exist_ok=True)
                img.save(os.path.join(self.pictures, name))
                names = sorted(os.listdir(self.pictures))
                for old in names[:max(0, len(names) - MAX_PICTURES)]:
                    os.remove(os.path.join(self.pictures, old))
            except OSError:
                pass
        threading.Thread(target=work, daemon=True).start()

    # -------------------------------------------------------------- readings
    def record(self, msg, now=None):
        """One detector reading: {"shown", "raw", "mode", "note", "lines", "crop"}."""
        if not self.enabled:
            return
        now = now or time.time()
        s = self.summary
        # Time since the previous reading, counted towards whatever state this
        # reading is in. Capped at 5 s so a sleeping PC doesn't add hours.
        dt = 0.0 if self._last_t is None else min(5.0, max(0.0, now - self._last_t))
        self._last_t = now
        raw, shown, mode = msg.get("raw"), msg.get("shown"), msg.get("mode")
        crop = msg.get("crop")
        if crop is not None:
            self._last_crop = crop
        s["reads"] += 1
        s["by_raw"][raw or "unreadable"] = s["by_raw"].get(raw or "unreadable", 0) + 1
        s["by_mode"][mode] = s["by_mode"].get(mode, 0) + 1
        open_ = mode != "none"                  # "none": LightBurn closed or minimised
        if open_:
            s["seconds_open"] += dt
            s["seconds_read" if raw else "seconds_unreadable"] += dt
        else:
            s["seconds_closed"] += dt
        self.recent.append((round(now, 2), raw, mode))

        # Unreadable while LightBurn is open (covered, panel closed, misread text)
        if open_ and raw is None:
            if self._unreadable_since is None:
                self._unreadable_since = now
                self._event("unreadable_start", msg, now)
        elif self._unreadable_since is not None:
            secs = now - self._unreadable_since
            self._unreadable_since = None
            s["unreadable_spells"] += 1
            self._event("unreadable_end", msg, now, seconds=round(secs, 1))

        # A single reading that disagreed and was smoothed away: likely a misread
        if self._odd is not None:
            odd_msg, odd_crop, odd_t = self._odd
            if shown == odd_msg.get("shown") and raw == shown:
                s["glitches"] += 1
                self._event("glitch", odd_msg, odd_t, odd_crop,
                            note=f"read as {odd_msg.get('raw')} once, status stayed {shown}")
            self._odd = None
        if raw and shown and raw != shown:
            self._odd = (dict(msg), crop, now)

        # Status changes, and quick back-and-forth flips between real states
        if shown != self._shown:
            if self._shown is not _UNSET:
                s["changes"] += 1
                kind = "change"
                if (shown and len(self._changes) >= 2 and self._changes[-2][1] == shown
                        and self._changes[-1][1] and now - self._changes[-1][0] < FLIP_SECONDS):
                    s["flips"] += 1
                    kind = "flip"
                self._event(kind, msg, now, crop, previous=self._shown)
            self._changes.append((now, shown))
            self._shown = shown
        if now - self._saved_at > 60:
            self.save()

    def report(self, truth):
        """The user says the status shown is wrong; truth is what the laser is doing."""
        now = time.time()
        shown = self._shown if self._shown is not _UNSET else None
        s = self.summary
        s["reports"] += 1
        kind = f"{shown or 'not found'} -> {truth}"
        s["report_kinds"][kind] = s["report_kinds"].get(kind, 0) + 1
        self._event("report", {"shown": shown, "raw": self.recent[-1][1] if self.recent else None,
                               "mode": self.recent[-1][2] if self.recent else None},
                    now, self._last_crop, truth=truth, recent_reads=list(self.recent))
        self.save()

    # ---------------------------------------------------------------- results
    def summary_text(self):
        """The totals as a few readable sentences (shown in Settings and put in
        exports)."""
        s = self.summary
        if not s["reads"]:
            return "No readings yet."
        open_ = s["seconds_open"] or 1e-9
        lines = [f"Since {datetime.fromtimestamp(s['since']):%d %b %H:%M}: {s['reads']:,} readings, "
                 f"LightBurn open {_human(s['seconds_open'])}.",
                 f"Status readable {s['seconds_read'] / open_:.1%} of that time "
                 f"({s['unreadable_spells']} unreadable spells, {_human(s['seconds_unreadable'])}).",
                 f"Status changes: {s['changes']}. Quick flips (likely misreads): {s['flips']}. "
                 f"One-off misreads smoothed away: {s['glitches']}.",
                 f"Your wrong-status reports: {s['reports']}."]
        if s["report_kinds"]:
            top = sorted(s["report_kinds"].items(), key=lambda kv: -kv[1])[:3]
            lines.append("Most reported: " + ", ".join(f"{k} ({n})" for k, n in top) + ".")
        return "\n".join(lines)

    def export(self, path, extra=None):
        """Zip the whole check folder (plus extra {name: text}) for emailing."""
        self.save()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(self.folder):
                for name in files:
                    full = os.path.join(root, name)
                    z.write(full, os.path.relpath(full, self.folder))
            z.writestr("summary.txt", self.summary_text())
            for name, text in (extra or {}).items():
                z.writestr(name, text)

    def reset(self):
        """Delete the whole check folder and start counting from zero."""
        with self._lock:
            shutil.rmtree(self.folder, ignore_errors=True)
        self.summary = _blank()
        self.recent.clear()
        self._changes.clear()
        self._last_t = self._odd = self._unreadable_since = self._last_crop = None
        self._shown = _UNSET
        self.save()
