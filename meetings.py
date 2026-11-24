"""Meeting reminders for the office: reads your Outlook (Teams) calendar from its published ICS link
and makes sure you see each meeting 10, 5 and 1 minutes before it starts.

The link lives in calendar-link.txt next to the server (not in git: anyone with it can read your
calendar). The calendar is read once a minute. A due reminder shows twice: as a small window on top
of every other window on this computer, and as a banner in the office page (so a phone sees it too).
Pressing Got it in either place clears both.

Standard library only. Windows has no time zone database for Python, so the time zones come from
the VTIMEZONE blocks Outlook puts in the file itself.
"""
import base64
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
LINK_FILE = os.path.join(ROOT, "calendar-link.txt")
ACKS_FILE = os.path.join(ROOT, "meeting-acks.json")
LEADS = (10, 5, 1)  # minutes before the start at which a reminder shows; each one replaces the last
ALARM = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Media", "Alarm01.wav")
AHEAD = 7 * 86400  # how far ahead meetings are worked out (and listed in the page)

lock = threading.Lock()
status = {"linked": False, "ok": False, "error": "", "updated": 0, "meetings": []}
acked = {}  # reminder key -> when it was acknowledged
popups = {}  # reminder key -> the pop-up window's process
try:
    with open(ACKS_FILE, encoding="utf-8") as f:
        acked = {k: v for k, v in json.load(f).items() if isinstance(v, (int, float))}
except (OSError, ValueError, AttributeError):
    pass


def link():
    try:
        with open(LINK_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def set_link(url):
    """Saves the published calendar link. Returns an error message, or "" when it was saved."""
    url = url.strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[9:]
    if url and not re.fullmatch(r"https://[^\s]+", url):
        return "That doesn't look like a calendar link. It should start with https:// and end in .ics."
    with open(LINK_FILE, "w", encoding="utf-8") as f:
        f.write(url)
    with lock:
        status.update(linked=bool(url), ok=False, error="", meetings=[])
    if url:
        threading.Thread(target=refresh, daemon=True).start()
    return ""


# ----- reading the ICS file -----

def unfold(text):
    """Logical lines of an ICS file: a line starting with a space or tab continues the one before."""
    out = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        elif line:
            out.append(line)
    return out


def prop(line):
    """'DTSTART;TZID=Taipei Standard Time:20261122T090000' -> ('DTSTART', {'TZID': ...}, '2026...')."""
    head, quoted, i = [], False, 0
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            break
    else:
        i = len(line)
    parts = re.findall(r'(?:[^;"]|"[^"]*")+', line[:i])
    name = (parts[0] if parts else "").upper()
    params = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.upper()] = v.strip('"')
    return name, params, line[i + 1:]


def blocks(lines):
    """Nested BEGIN/END blocks as {"type", "props": [(name, params, value)], "children": [...]}."""
    root = {"type": "ROOT", "props": [], "children": []}
    stack = [root]
    for line in lines:
        name, params, value = prop(line)
        if name == "BEGIN":
            b = {"type": value.upper(), "props": [], "children": []}
            stack[-1]["children"].append(b)
            stack.append(b)
        elif name == "END":
            if len(stack) > 1:
                stack.pop()
        else:
            stack[-1]["props"].append((name, params, value))
    return root


def first(b, name):
    return next(((p, v) for n, p, v in b["props"] if n == name), (None, None))


def text_value(v):
    return re.sub(r"\\([\\;,nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), v or "")


def offset(v):
    m = re.fullmatch(r"([+-])(\d\d)(\d\d)(\d\d)?", (v or "").strip())
    if not m:
        return timedelta(0)
    d = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)), seconds=int(m.group(4) or 0))
    return -d if m.group(1) == "-" else d


def nth_weekday(year, month, wd, n):
    """The date of the nth (or, negative, nth from last) given weekday (0 = Monday) of a month."""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(wd - d.weekday()) % 7 + 7 * (n - 1))
        return d if d.month == month else None
    nxt = date(year + month // 12, month % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    d -= timedelta(days=(d.weekday() - wd) % 7 + 7 * (-n - 1))
    return d if d.month == month else None


DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


class Zone:
    """One VTIMEZONE: its standard and daylight rules, each starting on a yearly rule."""

    def __init__(self, b):
        self.rules = []  # (start local datetime, yearly rule dict or None, offset to)
        for c in b["children"]:
            if c["type"] not in ("STANDARD", "DAYLIGHT"):
                continue
            _, start = first(c, "DTSTART")
            _, to = first(c, "TZOFFSETTO")
            _, rr = first(c, "RRULE")
            try:
                start = datetime.strptime((start or "16010101T000000")[:15], "%Y%m%dT%H%M%S")
            except ValueError:
                start = datetime(1601, 1, 1)
            self.rules.append((start, rrule(rr) if rr else None, offset(to)))

    def utcoffset(self, local):
        """The offset in force at a local wall-clock time."""
        best, off = None, None
        for start, rule, to in self.rules:
            for y in (local.year - 1, local.year):
                at = start
                if rule and rule.get("BYMONTH"):
                    month = int(rule["BYMONTH"][0])
                    bd = (rule.get("BYDAY") or [""])[0]
                    m = re.fullmatch(r"([+-]?\d)?(MO|TU|WE|TH|FR|SA|SU)", bd)
                    day = None
                    if m:
                        day = nth_weekday(y, month, DAYS[m.group(2)], int(m.group(1) or 1))
                    elif rule.get("BYMONTHDAY"):
                        day = date(y, month, int(rule["BYMONTHDAY"][0]))
                    if not day:
                        continue
                    at = datetime.combine(day, start.time())
                elif y != local.year:
                    continue
                if at <= local and (best is None or at > best):
                    best, off = at, to
        if off is None and self.rules:
            off = min(self.rules, key=lambda r: r[0])[2]
        return off or timedelta(0)


def rrule(v):
    out = {}
    for part in (v or "").split(";"):
        k, _, val = part.partition("=")
        if k:
            out[k.upper()] = val.upper().split(",")
    return out


def moment(params, v, zones):
    """An ICS date-time as a UTC datetime, or None for an all-day date."""
    v = (v or "").strip()
    if (params or {}).get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", v):
        return None
    try:
        local = datetime.strptime(v[:15], "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    if v.endswith("Z"):
        return local.replace(tzinfo=timezone.utc)
    zone = zones.get((params or {}).get("TZID", ""))
    if zone:
        return (local - zone.utcoffset(local)).replace(tzinfo=timezone.utc)
    return local.astimezone(timezone.utc)  # no zone given: this computer's own time


def to_local(utc, params, zones):
    """The wall-clock time of a UTC moment in the event's own zone (recurring meetings repeat on it)."""
    zone = zones.get((params or {}).get("TZID", ""))
    if not zone:
        return utc.astimezone().replace(tzinfo=None)
    naive = utc.replace(tzinfo=None)
    guess = naive + zone.utcoffset(naive)
    return naive + zone.utcoffset(guess)


def from_local(local, params, zones):
    zone = zones.get((params or {}).get("TZID", ""))
    if not zone:
        return local.astimezone(timezone.utc)
    return (local - zone.utcoffset(local)).replace(tzinfo=timezone.utc)


def on_day(rule, d, d0):
    """Whether a recurrence rule that started on date d0 has an occurrence on date d."""
    freq = (rule.get("FREQ") or ["DAILY"])[0]
    interval = max(1, int((rule.get("INTERVAL") or ["1"])[0] or 1))
    byday = rule.get("BYDAY") or []
    plain = [DAYS[x[-2:]] for x in byday if x[-2:] in DAYS]
    if freq == "DAILY":
        return (d - d0).days % interval == 0 and (not plain or d.weekday() in plain)
    if freq == "WEEKLY":
        wkst = DAYS.get((rule.get("WKST") or ["MO"])[0], 0)
        week0 = d0 - timedelta(days=(d0.weekday() - wkst) % 7)
        if ((d - week0).days // 7) % interval:
            return False
        return d.weekday() in (plain or [d0.weekday()])
    months = (d.year - d0.year) * 12 + d.month - d0.month
    if freq == "MONTHLY":
        if months % interval:
            return False
    elif freq == "YEARLY":
        if (d.year - d0.year) % interval:
            return False
        if d.month not in [int(m) for m in rule.get("BYMONTH") or [d0.month]]:
            return False
    else:
        return False
    if rule.get("BYMONTHDAY"):
        last = (date(d.year + d.month // 12, d.month % 12 + 1, 1) - timedelta(days=1)).day
        return any(d.day == (int(x) if int(x) > 0 else last + 1 + int(x)) for x in rule["BYMONTHDAY"] if x.lstrip("-").isdigit())
    if byday:
        setpos = rule.get("BYSETPOS")
        for x in byday:
            m = re.fullmatch(r"([+-]?\d+)?(MO|TU|WE|TH|FR|SA|SU)", x)
            if not m:
                continue
            n = int(m.group(1)) if m.group(1) else (int(setpos[0]) if setpos else 0)
            if n == 0:
                if d.weekday() == DAYS[m.group(2)]:
                    return True
            elif nth_weekday(d.year, d.month, DAYS[m.group(2)], n) == d:
                return True
        return False
    return d.day == d0.day


def teams_link(*texts):
    for t in texts:
        m = re.search(r"https://teams\.microsoft\.com/(?:l/meetup-join|meet)/[^\s\"<>\\]+", t or "")
        if m:
            return m.group(0)
    return ""


def occurrences(ics, start, end):
    """Timed meetings starting between two UTC datetimes: [{id, title, start, end, join, where}]."""
    root = blocks(unfold(ics))
    cal = next((c for c in root["children"] if c["type"] == "VCALENDAR"), root)
    zones = {first(z, "TZID")[1]: Zone(z) for z in cal["children"] if z["type"] == "VTIMEZONE"}
    events = [e for e in cal["children"] if e["type"] == "VEVENT"]
    moved = {}  # (uid, original start) -> the replacing VEVENT, for one changed occurrence of a series
    for e in events:
        p, rid = first(e, "RECURRENCE-ID")
        if rid:
            when = moment(p, rid, zones)
            if when:
                moved[(first(e, "UID")[1], when)] = e
    out = []

    def add(e, s, length):
        title = text_value(first(e, "SUMMARY")[1]).strip() or "(no title)"
        if (first(e, "STATUS")[1] or "").upper() == "CANCELLED" or re.match(r"(?i)cancell?ed\s*:", title):
            return
        if not (start <= s < end):
            return
        _, desc = first(e, "DESCRIPTION")
        _, url = first(e, "X-MICROSOFT-SKYPETEAMSMEETINGURL")
        _, where = first(e, "LOCATION")
        out.append({"id": "%s@%d" % (first(e, "UID")[1] or title, s.timestamp()), "title": title[:200],
                    "start": s.timestamp(), "end": (s + length).timestamp(),
                    "join": teams_link(url, text_value(desc), text_value(where)),
                    "where": text_value(where).strip()[:200]})

    for e in events:
        sp, sv = first(e, "DTSTART")
        s = moment(sp, sv, zones)
        if s is None:
            continue  # all-day: nothing to be on time for
        ep, ev = first(e, "DTEND")
        fin = moment(ep, ev, zones)
        length = (fin - s) if fin and fin > s else timedelta(minutes=30)
        _, dur = first(e, "DURATION")
        m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?", (dur or "").strip())
        if not fin and m and any(m.groups()):
            length = timedelta(days=int(m.group(1) or 0), hours=int(m.group(2) or 0), minutes=int(m.group(3) or 0))
        if first(e, "RECURRENCE-ID")[1]:
            add(e, s, length)
            continue
        _, rr = first(e, "RRULE")
        if not rr:
            add(e, s, length)
            continue
        rule = rrule(rr)
        until = None
        if rule.get("UNTIL"):
            u = rule["UNTIL"][0]
            try:
                if "T" not in u:  # a date: the series runs through that whole day
                    until = from_local(datetime.strptime(u[:8] + "235959", "%Y%m%d%H%M%S"), sp, zones)
                elif u.endswith("Z"):
                    until = moment({}, u, zones)
                else:
                    until = from_local(datetime.strptime(u[:15], "%Y%m%dT%H%M%S"), sp, zones)
            except ValueError:
                until = None
        count = int(rule["COUNT"][0]) if rule.get("COUNT") and rule["COUNT"][0].isdigit() else None
        if until and until < start:
            continue
        skip = set()
        for n, p, v in e["props"]:
            if n == "EXDATE":
                for x in v.split(","):
                    w = moment(p, x, zones)
                    if w:
                        skip.add(w)
        local0 = to_local(s, sp, zones)
        d0, d = local0.date(), local0.date()
        last = to_local(end, sp, zones).date()
        if count is None:  # no need to walk the years before the window, only to count
            d = max(d0, to_local(start, sp, zones).date() - timedelta(days=1))
        seen = 0
        while d <= last:
            if d == d0 or on_day(rule, d, d0):
                w = from_local(datetime.combine(d, local0.time()), sp, zones)
                if until and w > until:
                    break
                seen += 1
                if count is not None and seen > count:
                    break
                if w not in skip:
                    if (first(e, "UID")[1], w) not in moved:
                        add(e, w, length)
            d += timedelta(days=1)
    out.sort(key=lambda m: m["start"])
    return out


# ----- keeping up to date -----

def refresh():
    url = link()
    with lock:
        status["linked"] = bool(url)
    if not url:
        return
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "office-calendar/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            ics = r.read(20 * 1024 * 1024).decode("utf-8", "replace")
        if "BEGIN:VCALENDAR" not in ics:
            raise ValueError("the link did not return a calendar")
        now = datetime.now(timezone.utc)
        found = occurrences(ics, now - timedelta(hours=2), now + timedelta(seconds=AHEAD))
        with lock:
            status.update(ok=True, error="", updated=time.time(), meetings=found)
    except Exception as e:  # keep the last good list; say what went wrong
        with lock:
            status.update(ok=False, error=str(e)[:200] or type(e).__name__)


def due(now=None):
    """Reminders showing right now: [{key, id, title, start, lead, join}], earliest first.
    Each reminder gives way to the next one; the 1-minute one stays until 5 minutes after the start."""
    now = now or time.time()
    out = []
    with lock:
        meetings = list(status["meetings"])
    for m in meetings:
        for i, lead in enumerate(LEADS):
            begin = m["start"] - lead * 60
            stop = m["start"] - LEADS[i + 1] * 60 if i + 1 < len(LEADS) else m["start"] + 300
            key = "%s|%d" % (m["id"], lead)
            if begin <= now < stop and key not in acked:
                out.append(dict(key=key, id=m["id"], title=m["title"], start=m["start"], lead=lead,
                                join=m["join"], where=m["where"]))
    return out


def ack(key):
    with lock:
        acked[key] = time.time()
        cutoff = time.time() - 3 * 86400
        for k in [k for k, v in acked.items() if v < cutoff]:
            del acked[k]
        try:
            with open(ACKS_FILE, "w", encoding="utf-8") as f:
                json.dump(acked, f)
        except OSError:
            pass
        p = popups.pop(key, None)
    if p and p.poll() is None:
        p.kill()


POPUP = r"""
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
$f = New-Object System.Windows.Forms.Form
$f.Text = 'Meeting in ' + $env:MTG_LEAD
$f.TopMost = $true; $f.ControlBox = $false; $f.FormBorderStyle = 'FixedDialog'
$f.StartPosition = 'CenterScreen'; $f.ShowInTaskbar = $true
$f.ClientSize = New-Object System.Drawing.Size(440, 210)
$f.BackColor = [System.Drawing.Color]::FromArgb(42, 41, 39); $f.ForeColor = [System.Drawing.Color]::FromArgb(250, 249, 245)
$big = New-Object System.Drawing.Font('Segoe UI Semibold', 15)
$small = New-Object System.Drawing.Font('Segoe UI', 10.5)
$when = New-Object System.Windows.Forms.Label
$when.Text = 'Starts in ' + $env:MTG_LEAD + '  ·  ' + $env:MTG_TIME
$when.Font = $small; $when.ForeColor = [System.Drawing.Color]::FromArgb(229, 142, 99)
$when.SetBounds(22, 18, 400, 24)
$title = New-Object System.Windows.Forms.Label
$title.Text = $env:MTG_TITLE; $title.Font = $big; $title.UseMnemonic = $false
$title.SetBounds(22, 46, 400, 84)
$ok = New-Object System.Windows.Forms.Button
$ok.Text = 'Got it'; $ok.Font = $small; $ok.SetBounds(302, 150, 116, 40)
$ok.FlatStyle = 'Flat'; $ok.BackColor = [System.Drawing.Color]::FromArgb(217, 119, 87); $ok.ForeColor = [System.Drawing.Color]::White
$ok.FlatAppearance.BorderSize = 0
$script:done = $false
$ok.Add_Click({ $script:done = $true; $f.Close() })
$f.Controls.AddRange(@($when, $title, $ok))
if ($env:MTG_JOIN) {
  $join = New-Object System.Windows.Forms.Button
  $join.Text = 'Join in Teams'; $join.Font = $small; $join.SetBounds(166, 150, 126, 40)
  $join.FlatStyle = 'Flat'; $join.FlatAppearance.BorderColor = [System.Drawing.Color]::FromArgb(74, 73, 68)
  $join.Add_Click({ Start-Process $env:MTG_JOIN; $script:done = $true; $f.Close() })
  $f.Controls.Add($join)
}
# Only a click closes it: no button takes keyboard focus, so Enter or Space typed into another window
# just as this one opens cannot dismiss it unseen.
foreach ($c in $f.Controls) { $c.TabStop = $false }
$f.Add_FormClosing({ if (-not $script:done) { $_.Cancel = $true } })
# The alarm loops until the window closes
$script:alarm = $null
if ($env:MTG_SOUND -and (Test-Path $env:MTG_SOUND)) { $script:alarm = New-Object System.Media.SoundPlayer $env:MTG_SOUND }
$f.Add_Shown({
  $f.Activate(); $f.ActiveControl = $null
  if ($script:alarm) { $script:alarm.PlayLooping() } else { [System.Media.SystemSounds]::Exclamation.Play() }
})
$f.Add_FormClosed({ if ($script:alarm) { $script:alarm.Stop() } })
[void]$f.ShowDialog()
if ($script:done) { exit 7 }  # a button was pressed; anything else (an error) must not count as seen
exit 3
"""


def show_popup(r):
    """Opens the always-on-top window for a reminder; pressing Got it there acknowledges it."""
    env = dict(os.environ, MTG_TITLE=r["title"], MTG_LEAD="%d minute%s" % (r["lead"], "" if r["lead"] == 1 else "s"),
               MTG_TIME=datetime.fromtimestamp(r["start"]).strftime("%H:%M"), MTG_JOIN=r["join"], MTG_SOUND=ALARM)
    try:
        code = base64.b64encode(POPUP.encode("utf-16-le")).decode()
        p = subprocess.Popen(["powershell", "-NoProfile", "-STA", "-EncodedCommand", code],
                             env=env, creationflags=0x08000000)
    except OSError:
        return
    with lock:
        popups[r["key"]] = p

    def wait():
        if p.wait() == 7:  # closed with Got it (or Join), not killed from the page
            ack(r["key"])
            return
        time.sleep(30)  # it failed or was killed: if it is still due, remind_loop opens it again
        with lock:
            if popups.get(r["key"]) is p:
                del popups[r["key"]]
    threading.Thread(target=wait, daemon=True).start()


def remind_loop():
    """Opens a window for each reminder that falls due, and closes the ones no longer due."""
    while True:
        showing = due()
        keys = {r["key"] for r in showing}
        with lock:
            stale = [k for k in popups if k not in keys]
        for k in stale:  # replaced by the next reminder, or the meeting is over
            with lock:
                p = popups.pop(k, None)
            if p and p.poll() is None:
                p.kill()
        for r in showing:
            with lock:
                open_ = r["key"] in popups
            if not open_:
                show_popup(r)
        time.sleep(2)


def calendar_loop():
    while True:
        refresh()
        time.sleep(60)


def snapshot():
    with lock:
        out = dict(status)
        out["meetings"] = [m for m in status["meetings"] if m["end"] > time.time()][:300]
    out["alerts"] = due()
    return out
