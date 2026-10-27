"""Monthly reports as PDF and Excel (.xlsx), written without extra packages.

month_summary() turns a list of jobs into totals; write_month_pdf() and
write_month_xlsx() save it. Jobs are dicts with "start", "run", "seg"
(running stretches) and optional "name", "client", "material"."""

import calendar
import zipfile
from datetime import date, datetime, timedelta
from xml.sax.saxutils import escape


def _hm(sec):
    sec = int(sec)
    h, m = divmod(sec // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {sec % 60:02d}s"


def month_summary(jobs, rate_per_hour, currency, year, month):
    first = datetime(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    end = first + timedelta(days=last_day)
    a, b = first.timestamp(), end.timestamp()
    days = {date(year, month, d): 0.0 for d in range(1, last_day + 1)}
    day_jobs = {d: 0 for d in days}
    for j in jobs:
        for s, e in j.get("seg", []):
            s, e = max(s, a), min(e, b)
            while s < e:
                d = datetime.fromtimestamp(s).date()
                nxt = datetime.combine(d + timedelta(days=1), datetime.min.time()).timestamp()
                part = min(e, nxt) - s
                if d in days:
                    days[d] += part
                s += part
    month_jobs = [j for j in jobs if a <= j["start"] < b]
    for j in month_jobs:
        day_jobs[datetime.fromtimestamp(j["start"]).date()] += 1

    def group(key):
        out = {}
        for j in month_jobs:
            k = j.get(key) or "(none)"
            t = out.setdefault(k, [0, 0.0])
            t[0] += 1
            t[1] += j.get("run", 0)
        return sorted(((k, n, r, r * rate_per_hour / 3600) for k, (n, r) in out.items()),
                      key=lambda x: -x[2])

    total = sum(days.values())
    busiest = max(days.items(), key=lambda kv: kv[1]) if days else (None, 0)
    return {
        "title": f"{calendar.month_name[month]} {year}",
        "currency": currency, "rate": rate_per_hour,
        "total": total, "jobs": len(month_jobs),
        "cost": total * rate_per_hour / 3600,
        "active_days": sum(1 for v in days.values() if v > 0),
        "busiest": busiest,
        "days": [(d, days[d], day_jobs[d], days[d] * rate_per_hour / 3600) for d in sorted(days)],
        "clients": group("client"), "materials": group("material"), "names": group("name"),
        "job_list": sorted(month_jobs, key=lambda j: j["start"]),
    }


# ======================================================================== PDF
class _PDF:
    W, H = 595.0, 842.0           # A4 in points

    def __init__(self):
        self.pages = []
        self.new_page()

    def new_page(self):
        self.ops = []
        self.pages.append(self.ops)

    @staticmethod
    def _txt(s):
        s = str(s).encode("cp1252", "replace").decode("latin-1")
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    def text(self, x, y, s, size=10, bold=False, color=(0.1, 0.12, 0.16), right=False):
        if right:                 # rough right-align: Helvetica averages ~0.52 em per char
            x -= len(str(s)) * size * (0.56 if bold else 0.52)
        r, g, b = color
        self.ops.append(f"BT {r:.3f} {g:.3f} {b:.3f} rg /{'F2' if bold else 'F1'} {size} Tf "
                        f"{x:.1f} {self.H - y:.1f} Td ({self._txt(s)}) Tj ET")

    def rect(self, x, y, w, h, color):
        r, g, b = color
        self.ops.append(f"{r:.3f} {g:.3f} {b:.3f} rg {x:.1f} {self.H - y - h:.1f} {w:.1f} {h:.1f} re f")

    def line(self, x1, y1, x2, y2, color=(0.85, 0.86, 0.88), width=0.6):
        r, g, b = color
        self.ops.append(f"{r:.3f} {g:.3f} {b:.3f} RG {width} w {x1:.1f} {self.H - y1:.1f} m "
                        f"{x2:.1f} {self.H - y2:.1f} l S")

    def save(self, path):
        objs = ["<< /Type /Catalog /Pages 2 0 R >>", None,
                "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
                "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"]
        kids = []
        for ops in self.pages:
            stream = "\n".join(ops).encode("latin-1")
            objs.append(f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1") + stream +
                        b"\nendstream")
            content_no = len(objs)
            objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {self.W:.0f} {self.H:.0f}] "
                        f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
                        f"/Contents {content_no} 0 R >>")
            kids.append(len(objs))
        objs[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>"
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, o in enumerate(objs, 1):
            offsets.append(len(out))
            body = o if isinstance(o, bytes) else o.encode("latin-1")
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
        out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
        out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
        with open(path, "wb") as f:
            f.write(out)


ACCENT = (0.23, 0.51, 0.96)
MUTED = (0.45, 0.48, 0.55)


def write_month_pdf(path, m, app_name="Beacon"):
    pdf = _PDF()
    cur = m["currency"]
    money = (lambda v: f"{cur}{v:,.2f}") if m["rate"] > 0 else (lambda v: "-")

    def header(title):
        pdf.rect(0, 0, pdf.W, 70, (0.09, 0.10, 0.13))
        pdf.text(40, 32, f"{app_name}  -  Monthly report", 18, True, (1, 1, 1))
        pdf.text(40, 54, title, 11, False, (0.75, 0.78, 0.84))

    header(m["title"])
    # summary tiles
    tiles = [("Laser on", _hm(m["total"])), ("Jobs", str(m["jobs"])),
             ("Cost", money(m["cost"])), ("Active days", str(m["active_days"]))]
    for i, (label, value) in enumerate(tiles):
        x = 40 + i * 130
        pdf.rect(x, 90, 120, 56, (0.95, 0.96, 0.98))
        pdf.text(x + 10, 108, label.upper(), 8, True, MUTED)
        pdf.text(x + 10, 132, value, 15, True)
    if m["busiest"][0] and m["busiest"][1] > 0:
        pdf.text(40, 166, f"Busiest day: {m['busiest'][0]:%A %d %B} ({_hm(m['busiest'][1])})",
                 10, False, MUTED)

    # bar chart of laser time per day
    top = max([d[1] for d in m["days"]] + [1])
    cx, cy, cw, ch = 40, 185, 515, 150
    pdf.text(cx, cy - 4, "Laser time per day", 11, True)
    n = len(m["days"])
    slot = cw / n
    for i, (d, run, _, _) in enumerate(m["days"]):
        h = ch * run / top
        if run > 0:
            pdf.rect(cx + i * slot + slot * 0.15, cy + 10 + ch - h, slot * 0.7, h, ACCENT)
        if i % 2 == 0:
            pdf.text(cx + i * slot + slot * 0.2, cy + ch + 24, str(d.day), 7, False, MUTED)
    pdf.line(cx, cy + 10 + ch, cx + cw, cy + 10 + ch)
    pdf.text(cx + cw, cy - 4, f"tallest bar {_hm(top)}", 8, False, MUTED, right=True)

    def table(y, title, cols, rows):
        pdf.text(40, y, title, 11, True)
        y += 18
        xs = [40] + [40 + sum(w for _, w in cols[:i + 1]) for i in range(len(cols) - 1)]
        for (name, w), x in zip(cols, xs):
            pdf.text(x if name in ("Day", "Name", "Customer", "Material", "Date") else x + w - 6,
                     y, name, 8, True, MUTED, right=name not in ("Day", "Name", "Customer",
                                                               "Material", "Date"))
        pdf.line(40, y + 5, 555, y + 5)
        y += 18
        for row in rows:
            if y > pdf.H - 50:
                pdf.new_page()
                header(m["title"] + " (continued)")
                y = 100
            for i, ((name, w), x) in enumerate(zip(cols, xs)):
                left = name in ("Day", "Name", "Customer", "Material", "Date")
                pdf.text(x if left else x + w - 6, y, row[i], 9, False, right=not left)
            y += 15
        return y + 14

    y = table(392, "Day by day", [("Day", 175), ("Laser on", 120), ("Jobs", 100), ("Cost", 120)],
              [(f"{d:%a %d %b}", _hm(r) if r else "-", str(n) if n else "-",
                money(c) if r else "-") for d, r, n, c in m["days"]])
    for title, key, label in (("By customer", "clients", "Customer"),
                              ("By material", "materials", "Material"),
                              ("By job name", "names", "Name")):
        rows = m[key]
        if not rows or (len(rows) == 1 and rows[0][0] == "(none)"):
            continue
        if y > pdf.H - 120:
            pdf.new_page()
            header(m["title"] + " (continued)")
            y = 100
        y = table(y, title, [(label, 235), ("Jobs", 80), ("Laser on", 100), ("Cost", 100)],
                  [(k[:40], str(n), _hm(r), money(c)) for k, n, r, c in rows[:25]])
    pdf.text(40, pdf.H - 24, f"Made by {app_name} on {datetime.now():%d %b %Y %H:%M}. "
                             f"Laser time counts running only (pauses left out).", 7, False, MUTED)
    pdf.save(path)


# ====================================================================== XLSX
def _col(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _sheet_xml(rows):
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
           '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
           'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
           '<cols><col min="1" max="1" width="22" customWidth="1"/>'
           '<col min="2" max="12" width="15" customWidth="1"/></cols><sheetData>']
    for r, row in enumerate(rows):
        cells = []
        for c, v in enumerate(row):
            ref = f"{_col(c)}{r + 1}"
            style = ' s="1"' if r == 0 else ""
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                cells.append(f'<c r="{ref}"{style}><v>{v}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"{style}><is><t xml:space="preserve">'
                             f'{escape(str(v))}</t></is></c>')
        out.append(f'<row r="{r + 1}">{"".join(cells)}</row>')
    out.append("</sheetData></worksheet>")
    return "".join(out)


def write_xlsx(path, sheets):
    """sheets: [(name, rows)] where rows[0] is the header row."""
    ct = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    wb = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>']
    rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for i, (name, rows) in enumerate(sheets, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(rows))
            ct.append(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType='
                      '"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
            wb.append(f'<sheet name="{escape(name[:31])}" sheetId="{i}" r:id="rId{i}"/>')
            rels.append(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/'
                        f'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>')
        n = len(sheets) + 1
        rels.append(f'<Relationship Id="rId{n}" Type="http://schemas.openxmlformats.org/'
                    'officeDocument/2006/relationships/styles" Target="styles.xml"/>')
        z.writestr("[Content_Types].xml", "".join(ct) + "</Types>")
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
                   '2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml", "".join(wb) + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels", "".join(rels) + "</Relationships>")
        z.writestr("xl/styles.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
                   '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
                   '<fills count="2"><fill><patternFill patternType="none"/></fill>'
                   '<fill><patternFill patternType="gray125"/></fill></fills>'
                   '<borders count="1"><border/></borders>'
                   '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
                   '<cellXfs count="2"><xf fontId="0"/><xf fontId="1" applyFont="1"/></cellXfs>'
                   '</styleSheet>')


def write_month_xlsx(path, m):
    cur = m["currency"]
    hours = lambda s: round(s / 3600, 3)
    days = [["Day", "Laser on (h)", "Jobs", f"Cost ({cur})"]] + [
        [f"{d:%Y-%m-%d}", hours(r), n, round(c, 2)] for d, r, n, c in m["days"]]
    days.append(["Total", hours(m["total"]), m["jobs"], round(m["cost"], 2)])
    jobs = [["Started", "Ended", "Name", "Customer", "Material", "Run time (min)",
             "Paused/stopped (min)", f"Cost ({cur})"]] + [
        [datetime.fromtimestamp(j["start"]).strftime("%Y-%m-%d %H:%M"),
         datetime.fromtimestamp(j.get("end", j["start"])).strftime("%Y-%m-%d %H:%M"),
         j.get("name", ""), j.get("client", ""), j.get("material", ""),
         round(j.get("run", 0) / 60, 2),
         round(max(0, j.get("end", j["start"]) - j["start"] - j.get("run", 0)) / 60, 2),
         round(j.get("run", 0) * m["rate"] / 3600, 2)] for j in m["job_list"]]
    sheets = [("Days", days), ("Jobs", jobs)]
    for title, key, label in (("Customers", "clients", "Customer"),
                              ("Materials", "materials", "Material")):
        sheets.append((title, [[label, "Jobs", "Laser on (h)", f"Cost ({cur})"]] +
                       [[k, n, hours(r), round(c, 2)] for k, n, r, c in m[key]]))
    write_xlsx(path, sheets)
