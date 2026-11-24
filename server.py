"""Shows every local Claude Code session as a character in an office.

Run:  python server.py     then open http://localhost:4545
Claude Code hooks POST their event JSON to /event (see ~/.claude/hooks/office_hook.py).
"""
import glob
import hashlib
import json
import os
import hmac
import re
import secrets
import socket
import subprocess
import threading
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import meetings
import sounds
import tray

PORT = int(os.environ.get("OFFICE_PORT", "4545"))
ROOT = os.path.dirname(os.path.abspath(__file__))

# What this copy of the office is: its name, which casts it offers, and whether the stock widget
# is on. office.json sits next to this file; the same code serves every edition of the office.
office = {"name": "Office", "themes": ["pokemon", "maple"], "stocks": True, "lan": False}
try:
    with open(os.path.join(ROOT, "office.json"), encoding="utf-8") as f:
        _o = json.load(f)
    office["name"] = str(_o.get("name") or office["name"])[:40]
    office["themes"] = [t for t in _o.get("themes") or [] if t in ("pokemon", "maple")] or office["themes"]
    office["stocks"] = bool(_o.get("stocks", office["stocks"]))
    office["lan"] = bool(_o.get("lan", False))
except (OSError, ValueError, AttributeError):
    pass

# "lan": true in office.json lets other devices on your network (a phone) open the office. They must
# show a private key, kept in lan-key.txt next to this file; this computer itself never needs it.
LAN_KEY = ""
if office["lan"]:
    _kf = os.path.join(ROOT, "lan-key.txt")
    try:
        with open(_kf, encoding="utf-8") as f:
            LAN_KEY = f.read().strip()
    except OSError:
        pass
    if len(LAN_KEY) < 12:
        LAN_KEY = secrets.token_urlsafe(12)
        with open(_kf, "w", encoding="utf-8") as f:
            f.write(LAN_KEY)
ASKING_TOOLS = {"AskUserQuestion", "ExitPlanMode"}

STATE_FILE = os.path.join(ROOT, "state.json")
# The Claude desktop app keeps one JSON record per session; it maps the Claude Code
# session id (what hooks report) to the app's own id, which its claude:// links use.
APP_SESSIONS = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Claude-Data", "*",
                            "claude-code-sessions", "*", "*", "local_*.json")

sessions = {}
lock = threading.Lock()
try:
    with open(STATE_FILE, encoding="utf-8") as f:
        sessions = {s["id"]: s for s in json.load(f)}
except (OSError, ValueError, KeyError):
    pass


HIDDEN_FILE = os.path.join(ROOT, "hidden.json")
hidden = set()  # sessions the user removed; their later events are ignored
try:
    with open(HIDDEN_FILE, encoding="utf-8") as f:
        hidden = set(json.load(f))
except (OSError, ValueError):
    pass


def save():
    try:
        with open(HIDDEN_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(hidden), f)
    except OSError:
        pass
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(list(sessions.values()), f)
    except OSError:
        pass


def mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:  # the app replaced the file between listing and looking
        return 0


def forever(loop):
    """Run a background loop for good: if it ever raises, note why in errors.log and start it
    again, so one bad moment cannot quietly stop desks being matched or pull requests checked."""
    while True:
        try:
            loop()
        except Exception:
            try:
                with open(os.path.join(ROOT, "errors.log"), "a", encoding="utf-8") as f:
                    f.write("%s %s\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), loop.__name__, traceback.format_exc()))
            except OSError:
                pass
        time.sleep(5)


APP_BLOCK = re.compile(r"<([a-z]+-[a-z-]+)\b[^>]*(>.*?(</\1>|$)|$)", re.S)


def first_line(prompt):
    """A desk's stand-in name: the first line the person wrote, skipping the blocks the app puts
    into a prompt itself (<system-reminder>, <cross-session-message>) when sessions start or
    message one another."""
    text = APP_BLOCK.sub("", prompt or "").strip()
    return text.splitlines()[0][:60] if text else ""


for _s in sessions.values():  # names taken from such a block before first_line existed
    if APP_BLOCK.match(str(_s.get("title") or "")):
        _s["title"] = first_line(_s.get("prompt"))


def resolve_loop():
    """Fill in app_id / app_title for sessions from the desktop app's records."""
    while True:
        time.sleep(2)
        try:
            reconcile()
        except Exception:
            pass
        with lock:
            wanted = set(sessions)
            for i, x in sessions.items():
                if not x.get("transcript"):
                    found = glob.glob(os.path.join(os.path.expanduser("~"), ".claude", "projects", "*", i + ".jsonl"))
                    x["transcript"] = found[0] if found else ""
            todo = [(i, x["transcript"]) for i, x in sessions.items()
                    if x.get("transcript") and not x.get("pr_scanned")]
        for sid, path in todo:
            text = transcript_text(path)
            with lock:
                if sid in sessions:
                    note_prs(sessions[sid], text)
                    sessions[sid]["pr_scanned"] = True
                    save()
        with lock:
            urls = {p["url"] for x in sessions.values() for p in x.get("prs") or []}
            urls |= {u for x in sessions.values() for u in x.get("waiting_pr") or []}
        for url in urls:
            refresh_pr(url)
        with lock:
            changed = False
            for i, x in sessions.items():
                # pull requests this session created, as found in its transcript (score 1000+ = gh pr create)
                made = set(x.get("pr_made") or []) | {k for k, v in ((credits.get(i) or {}).get("prs") or {}).items()
                                                      if v >= 1000 and not k.startswith("?")}
                x.pop("pr_created", None)  # worked out under an older, looser rule
                before = [p["url"] for p in x.get("prs") or []]
                x["pr_made"] = sorted(made)
                note_prs(x, "")  # re-pick which three to show
                changed = changed or before != [p["url"] for p in x["prs"]]
                apply_pr_info(x)
                changed = review_hold(x) or changed
            if changed:
                save()
        if not wanted:
            continue
        cli = cli_rc_ids()
        with lock:
            for sid in wanted & set(cli):
                if sid in sessions and not sessions[sid].get("app_id") and sessions[sid].get("rc_id") != cli[sid]:
                    sessions[sid]["rc_id"] = cli[sid]
                    save()
        files = sorted(glob.glob(APP_SESSIONS), key=mtime, reverse=True)[:60]
        for path in files:
            try:
                with open(path, encoding="utf-8") as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                continue
            for sid in {rec.get("cliSessionId"), rec.get("lastAssistantCliSessionId")} & wanted:
                with lock:
                    if sid in sessions:
                        sessions[sid]["app_id"] = rec.get("sessionId", "")
                        sessions[sid]["app_title"] = rec.get("title", "")
                        sessions[sid]["rc_id"] = rc_id(rec)
                        # finished work counts as seen once the user has opened that session in Claude
                        x = sessions[sid]
                        if x.get("state") == "done" and (rec.get("lastFocusedAt") or 0) / 1000 > x.get("stopped_at", float("inf")) + 2:
                            x["state"] = "resting"
                            save()


PR_URL = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
PR_LINE = re.compile(r"\s*https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)\s*")


def created_prs(output):
    """Pull requests that a `gh pr create` command's output says it opened: gh prints the new
    link on a line of its own. Links that only appear inside other text do not count, so a
    script that merely contains the words, or prints a list, is not mistaken for opening one."""
    if isinstance(output, dict):
        output = output.get("stdout") or ""
    if isinstance(output, list):
        output = "\n".join(b.get("text") or "" for b in output if isinstance(b, dict))
    found = (PR_LINE.fullmatch(line) for line in str(output or "").splitlines())
    return {"%s#%s" % (m.group(1).removesuffix(".git"), m.group(2)) for m in found if m}


PR_CMD = re.compile(r"gh pr (?:view|checks|comment|merge|edit|ready|diff|review|close|reopen)\s+(\d+)")
repo_cache = {}


def repo_for(cwd):
    """owner/name of the GitHub repo checked out in cwd, or ''."""
    if cwd not in repo_cache:
        repo = ""
        try:
            out = subprocess.run(["git", "-C", cwd, "remote", "get-url", "origin"], capture_output=True,
                                 text=True, timeout=3, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?\s*$", out)
            repo = m.group(1) if m else ""
        except (OSError, subprocess.SubprocessError):
            pass
        repo_cache[cwd] = repo
    return repo_cache[cwd]


pr_info = {}  # PR url -> {"status", "title", "checked"}


def refresh_pr(url):
    """Ask GitHub (through the gh CLI) for a PR's status: draft, open, merged or closed."""
    old = pr_info.get(url)
    if old and (old["status"] == "merged" or time.time() - old["checked"] < 60):
        return
    info = {"status": (old or {}).get("status", ""), "title": (old or {}).get("title", ""), "checked": time.time(),
            "approved": (old or {}).get("approved", False)}
    try:
        out = subprocess.run(["gh", "pr", "view", url, "--json", "state,isDraft,title,reviewDecision"], capture_output=True,
                             text=True, encoding="utf-8", timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        data = json.loads(out)
        info["status"] = "draft" if data.get("isDraft") and data.get("state") == "OPEN" else data.get("state", "").lower()
        info["title"] = data.get("title", "")
        info["approved"] = data.get("reviewDecision") == "APPROVED"
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    pr_info[url] = info


def apply_pr_info(s):
    for p in s.get("prs") or []:
        info = pr_info.get(p["url"]) or {}
        p["status"] = info.get("status", "")
        p["title"] = info.get("title", "")
        p["approved"] = bool(info.get("approved"))


def note_prs(s, text, weight=1):
    """Count pull-request mentions for a session and keep the top three it created as s['prs']."""
    counts = s.setdefault("pr_counts", {})
    for repo, num in PR_URL.findall(text):
        key = f"{repo.removesuffix('.git')}#{num}"
        counts[key] = counts.get(key, 0) + weight
    nums = PR_CMD.findall(text)
    repo = repo_for(s.get("cwd", "")) if nums and s.get("cwd") else ""
    for num in nums if repo else []:
        counts[f"{repo}#{num}"] = counts.get(f"{repo}#{num}", 0) + 3 * weight
    # Only pull requests this session created are shown (s["pr_made"]): the newest three, newest first.
    # A higher number means opened later, which holds within one repository.
    top = sorted(s.get("pr_made") or [], key=lambda k: -int(k.split("#")[1]))[:3]
    s["prs"] = [{"url": "https://github.com/%s/pull/%s" % tuple(k.split("#")), "label": "#" + k.split("#")[1]}
                for k in top]
    apply_pr_info(s)


def review_hold(s):
    """A finished turn whose pull request is still waiting for its review shows no book: the desk
    waits until that pull request is approved or merged, and the book appears then. s["prs"] only
    ever holds pull requests the session created itself. Returns True when s was changed."""
    waiting = s.get("waiting_pr") or []
    if s.get("state") == "done":
        mine = [p["url"] for p in s.get("prs") or [] if p.get("status") == "open" and not p.get("approved")]
        if not mine:
            return False
        s["state"], s["waiting_pr"] = "resting", sorted(set(waiting) | set(mine))
        return True
    if not waiting or s.get("state") not in ("resting", "idle"):
        return False  # busy or asking: the list is kept, and dropped when the next prompt arrives
    info = [pr_info.get(u) or {} for u in waiting]
    status = ["approved" if x.get("status") == "open" and x.get("approved") else x.get("status", "") for x in info]
    if any(x in ("open", "") for x in status):  # "" = GitHub has not answered yet
        return False
    s.pop("waiting_pr")
    if "merged" in status or "approved" in status:  # closed unmerged, or back to a draft: no book
        s["state"], s["stopped_at"] = "done", time.time()
    return True


def transcript_text(path):
    """What the user and Claude wrote, and the commands Claude ran, from a session transcript."""
    texts = []
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if "pull/" not in line and "gh pr" not in line:
                    continue
                try:
                    content = (json.loads(line).get("message") or {}).get("content")
                except (ValueError, AttributeError):
                    continue
                if isinstance(content, str):
                    texts.append(content)
                for b in content if isinstance(content, list) else []:
                    if isinstance(b, dict) and b.get("type") == "text":
                        texts.append(b.get("text", ""))
                    elif isinstance(b, dict) and b.get("type") == "tool_use":
                        texts.append(json.dumps(b.get("input")))
    except OSError:
        pass
    return "\n".join(texts)


# ---- stock widget -------------------------------------------------------------
STOCKS_FILE = os.path.join(ROOT, "stocks.json")
try:
    with open(STOCKS_FILE, encoding="utf-8") as f:
        watch = [str(x) for x in json.load(f)]
except (OSError, ValueError):
    watch = []
quotes = {}  # symbol -> latest quote


# The Taiwan exchange's certificate lacks a field that newer Python insists on by default.
# This context still verifies the certificate chain and host name; it only drops that strict check.
import ssl
TLS = ssl.create_default_context()
TLS.verify_flags &= ~ssl.VERIFY_X509_STRICT


def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10, context=TLS) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def is_tw(sym):
    return sym[:1].isdigit() and "." not in sym


def fetch_twse(sym):
    """Taiwan exchange's own live feed (listed and OTC)."""
    q = urllib.parse.quote("tse_%s.tw|otc_%s.tw" % (sym, sym))
    data = http_json("https://mis.twse.com.tw/stock/api/getStockInfo.jsp?json=1&delay=0&ex_ch=" + q)
    m = next((x for x in data.get("msgArray") or [] if x.get("c") == sym), None)
    if not m:
        return None
    price = num(m.get("z"))
    if price is None:  # no trade in this snapshot: keep the last trade we saw, else mid of best bid/ask
        old = quotes.get(sym) or {}
        bid, ask = num((m.get("b") or "").split("_")[0]), num((m.get("a") or "").split("_")[0])
        if old.get("day") == m.get("d") and old.get("price"):
            price = old["price"]
        elif bid and ask:
            price = round((bid + ask) / 2, 2)
        else:
            price = num(m.get("y"))
    return {"symbol": sym, "name": m.get("n") or sym, "open": num(m.get("o")), "price": price,
            "prev": num(m.get("y")), "day": m.get("d"), "as_of": "%s %s" % (m.get("d") or "", m.get("t") or "")}


def fetch_yahoo(sym):
    ysym = sym + ".TW" if is_tw(sym) else sym
    data = http_json("https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=1d&range=1d" % urllib.parse.quote(ysym))
    r = data["chart"]["result"][0]
    meta = r["meta"]
    opens = ((r.get("indicators") or {}).get("quote") or [{}])[0].get("open") or [None]
    period = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    t = meta.get("regularMarketTime")
    return {"symbol": sym, "name": meta.get("shortName") or sym, "open": num(opens[0]),
            "price": num(meta.get("regularMarketPrice")), "prev": num(meta.get("chartPreviousClose")),
            "as_of": datetime.fromtimestamp(t).strftime("%Y%m%d %H:%M:%S") if t else "",
            "start": period.get("start"), "end": period.get("end")}


def market_open(sym):
    if is_tw(sym):
        now = datetime.now(timezone(timedelta(hours=8)))
        return now.weekday() < 5 and (9, 0) <= (now.hour, now.minute) <= (13, 40)
    q = quotes.get(sym) or {}
    return bool(q.get("start") and q["start"] <= time.time() <= q.get("end", 0) + 300)


def stock_loop():
    """Refresh each watched symbol every 5 minutes while its market is open, every 30 otherwise."""
    while True:
        for sym in list(watch):
            q = quotes.get(sym)
            age = time.time() - q["fetched"] if q else 10**9
            if age < (300 if market_open(sym) else 1800):
                continue
            new = None
            for fetch in ([fetch_twse, fetch_yahoo] if is_tw(sym) else [fetch_yahoo]):
                try:
                    new = fetch(sym)
                except Exception:
                    new = None
                if new and new.get("price") is not None:
                    break
            new = new or dict(q or {"symbol": sym, "name": sym, "open": None, "price": None, "prev": None, "as_of": ""})
            new["fetched"] = time.time()
            quotes[sym] = new
        time.sleep(15)


# ---- stock chart ---------------------------------------------------------------
CHART_RANGES = {"1d": ("1d", "5m"), "5d": ("5d", "30m"), "1mo": ("1mo", "1d"), "6mo": ("6mo", "1d"),
                "ytd": ("ytd", "1d"), "1y": ("1y", "1d"), "5y": ("5y", "1wk"), "max": ("max", "1mo")}
chart_cache = {}  # (symbol, range) -> (fetched at, result)


def yahoo_series(ysym, rng, interval):
    """Yahoo's chart feed for one symbol: (meta, [[time, close, open], ...]) with empty slots dropped."""
    data = http_json("https://query1.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=%s"
                     % (urllib.parse.quote(ysym), rng, interval))
    r = data["chart"]["result"][0]
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    closes, opens = q.get("close") or [], q.get("open") or []
    rows = [[t, round(c, 4), opens[i] if i < len(opens) else None]
            for i, (t, c) in enumerate(zip(r.get("timestamp") or [], closes)) if c is not None]
    return r["meta"], rows


def fetch_chart(sym, rng):
    """Price history for the chart modal. 1D falls back to the last day that traded when today
    has no trades yet (a holiday, or a fund that has not traded today)."""
    rng = rng if rng in CHART_RANGES else "1d"
    old = chart_cache.get((sym, rng))
    if old and time.time() - old[0] < (60 if rng in ("1d", "5d") else 900):
        return old[1]
    ysym = sym + ".TW" if is_tw(sym) else sym
    meta, rows = yahoo_series(ysym, *CHART_RANGES[rng])
    off = meta.get("gmtoffset") or 0
    prev = meta.get("previousClose") or meta.get("chartPreviousClose")
    period = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    session = [period.get("start"), period.get("end")]
    if rng == "1d" and not rows:
        _, week = yahoo_series(ysym, "5d", "30m")
        if week:
            day = (week[-1][0] + off) // 86400
            before = [x for x in week if (x[0] + off) // 86400 < day]
            rows = [x for x in week if (x[0] + off) // 86400 == day]
            prev, session = (before[-1][1] if before else None), None
    out = {"ok": True, "symbol": sym, "name": meta.get("longName") or meta.get("shortName") or sym,
           "currency": meta.get("currency") or "", "exchange": meta.get("fullExchangeName") or meta.get("exchangeName") or "",
           "range": rng, "points": [x[:2] for x in rows], "prev": prev if rng == "1d" else None,
           "price": num(meta.get("regularMarketPrice")), "time": meta.get("regularMarketTime"), "gmtoffset": off,
           "session": session if rng == "1d" else None,
           "open": next((round(x[2], 2) for x in rows if x[2] is not None), None) if rng == "1d" else None,
           "high": num(meta.get("regularMarketDayHigh")), "low": num(meta.get("regularMarketDayLow")),
           "prev_close": num(meta.get("previousClose") or (prev if rng == "1d" else None)),
           "hi52": num(meta.get("fiftyTwoWeekHigh")), "lo52": num(meta.get("fiftyTwoWeekLow"))}
    chart_cache[(sym, rng)] = (time.time(), out)
    return out


# ---- merged pull requests --------------------------------------------------------
MERGED_FILE = os.path.join(ROOT, "merged.json")
merged_cache = {}  # first day of a week -> [fetched at, result]
merged_busy = set()  # weeks being refreshed in the background
try:
    with open(MERGED_FILE, encoding="utf-8") as f:
        merged_cache = json.load(f)
except (OSError, ValueError):
    pass


def gh_search(query, page=1, per_page=100):
    """One page of a GitHub issue search through the gh CLI, or None when it fails."""
    try:
        out = subprocess.run(["gh", "api", "-X", "GET", "search/issues", "-f", "q=" + query,
                              "-f", "per_page=%d" % per_page, "-f", "page=%d" % page],
                             capture_output=True, text=True, encoding="utf-8", timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        data = json.loads(out)
        return data if isinstance(data.get("items"), list) else None
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        return None


def merged_week(k):
    """The signed-in GitHub user's pull requests merged in the k-th week back from today (UTC),
    newest first. Asking a week at a time keeps them in true merge order, which GitHub's search
    cannot sort by, and has no limit on how far back it can go.

    Answers at once from the saved copy when there is one; a copy that has gone stale is
    refreshed in the background, so the page never waits on GitHub for something it has seen."""
    k = max(0, min(int(k), 1000))
    key = (datetime.now(timezone.utc).date() - timedelta(days=7 * k + 6)).isoformat()
    old = merged_cache.get(key)
    if not old:
        return fetch_week(k)
    if time.time() - old[0] > (60 if k == 0 else 3600) and key not in merged_busy:
        merged_busy.add(key)
        threading.Thread(target=fetch_week, args=(k,), daemon=True).start()
    return old[1]


def fetch_week(k):
    """Ask GitHub for one week of merged pull requests and save the answer."""
    end = datetime.now(timezone.utc).date() - timedelta(days=7 * k)
    start = end - timedelta(days=6)
    try:
        return fetch_week_now(k, start, end)
    finally:
        merged_busy.discard(start.isoformat())


def fetch_week_now(k, start, end):
    items, ok = [], True
    for page in range(1, 11):
        data = gh_search("is:pr is:merged author:@me merged:%s..%s" % (start, end), page)
        if data is None:
            ok = False
            break
        for it in data["items"]:
            items.append({"url": it.get("html_url", ""), "number": it.get("number"), "title": it.get("title", ""),
                          "repo": "/".join((it.get("repository_url") or "").split("/")[-2:]),
                          "merged": (it.get("pull_request") or {}).get("merged_at") or it.get("closed_at") or ""})
        if len(data["items"]) < 100:
            break
    items.sort(key=lambda p: p["merged"], reverse=True)
    out = {"ok": ok, "items": items, "from": start.isoformat(), "to": end.isoformat()}
    if ok and k == 0:
        total = gh_search("is:pr is:merged author:@me", per_page=1)
        out["total"] = (total or {}).get("total_count")
    if ok:
        merged_cache[start.isoformat()] = [time.time(), out]
        for key in [x for x, v in list(merged_cache.items()) if time.time() - v[0] > 2 * 86400]:
            merged_cache.pop(key, None)  # the weeks shift every day, so old copies are never asked for again
        try:
            with open(MERGED_FILE, "w", encoding="utf-8") as f:
                json.dump(merged_cache, f)
        except OSError:
            pass
    elif start.isoformat() in merged_cache:
        return merged_cache[start.isoformat()][1]  # GitHub unreachable: keep showing the last good copy
    return out


def merged_loop():
    """Keep this week's list warm so opening the panel never waits on GitHub."""
    while True:
        try:
            fetch_week(0)
        except Exception:
            pass
        time.sleep(300)


# ---- which sessions worked on which pull request --------------------------------
CREDITS_FILE = os.path.join(ROOT, "credits.json")
CREDIT_RULE = 2  # raise this when scan_transcript's scoring changes
TRANSCRIPTS = os.path.join(os.path.expanduser("~"), ".claude", "projects", "*", "*.jsonl")
credits = {}      # session id -> {"m": transcript mtime, "z": size, "prs": {"owner/repo#n": score}}
credit_for = {}   # "owner/repo#n" -> [(score, session id), ...] best first
credits_ready = False
try:
    with open(CREDITS_FILE, encoding="utf-8") as f:
        saved = json.load(f)
    if saved.get("rule") == CREDIT_RULE:  # scores from an older rule are thrown away and rebuilt
        credits = saved["sessions"]
except (OSError, ValueError, KeyError, AttributeError):
    pass


def scan_transcript(path):
    """Score the pull requests one session worked on: 1000 for creating it (gh pr create answered
    with its link), 3 for a gh pr command on it, 1 each time the user or Claude wrote its link.
    Command output is ignored, so merely listing pull requests earns nothing."""
    scores, creating = {}, set()

    def note(text, cwd):
        for repo, n in PR_URL.findall(text):
            key = "%s#%s" % (repo.removesuffix(".git"), n)
            scores[key] = scores.get(key, 0) + 1
        nums = PR_CMD.findall(text)
        repo = (repo_for(cwd) if cwd else "") or "?"  # "?" = repo unknown (its folder is gone); matched by number
        for n in nums:
            scores["%s#%s" % (repo, n)] = scores.get("%s#%s" % (repo, n), 0) + 3

    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if "pull/" not in line and "gh pr" not in line:
                    continue
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                msg = j.get("message") if isinstance(j, dict) else None
                content = msg.get("content") if isinstance(msg, dict) else None
                cwd = j.get("cwd") or ""
                if isinstance(content, str):
                    note(content, cwd)
                for b in content if isinstance(content, list) else []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        note(b.get("text") or "", cwd)
                    elif b.get("type") == "tool_use":
                        text = json.dumps(b.get("input"))
                        note(text, cwd)
                        command = (b.get("input") or {}).get("command") if isinstance(b.get("input"), dict) else ""
                        if "gh pr create" in str(command or ""):
                            creating.add(b.get("id"))
                    elif b.get("type") == "tool_result" and b.get("tool_use_id") in creating:
                        for key in created_prs(b.get("content")):
                            scores[key] = scores.get(key, 0) + 1000
    except OSError:
        pass
    return scores


def credit_loop():
    """Keep the index current: read every transcript once, then only the ones that changed."""
    global credit_for, credits_ready
    while True:
        changed = False
        for path in glob.glob(TRANSCRIPTS):
            sid = os.path.basename(path)[:-6]
            try:
                m, z = os.path.getmtime(path), os.path.getsize(path)
            except OSError:
                continue
            old = credits.get(sid)
            if old and old.get("m") == m and old.get("z") == z:
                continue
            credits[sid] = {"m": m, "z": z, "prs": scan_transcript(path)}
            changed = True
        if changed or not credits_ready:
            index = {}
            for sid, rec in list(credits.items()):
                for key, score in rec["prs"].items():
                    index.setdefault(key, []).append((score, sid))
            for rows in index.values():
                rows.sort(reverse=True)
            credit_for = index
            try:
                with open(CREDITS_FILE, "w", encoding="utf-8") as f:
                    json.dump({"rule": CREDIT_RULE, "sessions": credits}, f)
            except OSError:
                pass
        credits_ready = True
        time.sleep(120)


titles = {"at": 0, "map": {}}


def session_titles():
    """Session id -> title, from the desktop app's records (re-read every ten minutes)."""
    if time.time() - titles["at"] > 600:
        found = {}
        for path in glob.glob(APP_SESSIONS):
            try:
                with open(path, encoding="utf-8") as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                continue
            for sid in (rec.get("cliSessionId"), rec.get("lastAssistantCliSessionId")):
                if sid and rec.get("title"):
                    found[sid] = rec["title"]
        titles["at"], titles["map"] = time.time(), found
    return titles["map"]


def credits_of(repo, number):
    """The one session to thank for a pull request: the one that created it, else the one that
    worked on it most. Others that only mentioned or reviewed it are left out."""
    rows = {}
    for score, sid in credit_for.get("%s#%s" % (repo, number), []) + credit_for.get("?#%s" % number, []):
        rows[sid] = rows.get(sid, 0) + score
    names, out = session_titles(), []
    for sid, score in sorted(rows.items(), key=lambda kv: -kv[1])[:1]:
        if score < 2:  # a single passing mention is not help
            continue
        with lock:
            desk = sessions.get(sid) or {}
        out.append({"mon": desk.get("mon") or mon_for(sid), "created": score >= 1000,
                    "sid": sid, "maple": desk.get("maple"),  # the page picks the MapleStory monster from these
                    "name": desk.get("custom_name") or desk.get("app_title") or names.get(sid) or desk.get("title") or ""})
    return out


CLI_SESSIONS = os.path.join(os.path.expanduser("~"), ".claude", "sessions", "*.json")
RC_ID = re.compile(r"(session|cse)_[0-9A-Za-z]{10,60}")


def cli_rc_ids():
    """Remote Control ids of the command-line sessions running on this computer, by session id.
    The command-line tool keeps one small file per running session; one started in a terminal with
    `claude --remote-control` (or under `claude remote-control`) has no app record."""
    out = {}
    for path in glob.glob(CLI_SESSIONS):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and isinstance(d.get("bridgeSessionId"), str) and RC_ID.fullmatch(d["bridgeSessionId"]):
            out[d.get("sessionId")] = "session_" + d["bridgeSessionId"].split("_", 1)[1]
    return out


def cli_idle_since():
    """When each running command-line session last went idle, by session id (those that are idle)."""
    out = {}
    for path in glob.glob(CLI_SESSIONS):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("status") == "idle" and isinstance(d.get("statusUpdatedAt"), (int, float)):
            out[d.get("sessionId")] = d["statusUpdatedAt"] / 1000
    return out


def rc_id(rec):
    """The session's Remote Control id from its app record ("" when it has never been connected).
    claude.ai/code/<that id> opens the session on a phone or in a browser. A session started from
    the phone records it as cse_...; the page address uses the same id as session_..."""
    ids = [x for x in rec.get("bridgeSessionIds") or [] if isinstance(x, str) and re.fullmatch(r"(session|cse)_[0-9A-Za-z]{10,60}", x)]
    return "session_" + ids[-1].split("_", 1)[1] if ids else ""


def app_records(limit=25):
    """Recent non-archived sessions from the desktop app's records, newest first."""
    out = []
    for path in sorted(glob.glob(APP_SESSIONS), key=mtime, reverse=True)[:limit * 2]:
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        sid = rec.get("lastAssistantCliSessionId") or rec.get("cliSessionId")
        if sid and not rec.get("isArchived"):
            out.append({"mon": mon_for(sid), "id": sid, "alt_id": rec.get("cliSessionId") or sid, "app_id": rec.get("sessionId", ""),
                        "rc_id": rc_id(rec), "title": rec.get("title") or "(untitled)", "cwd": rec.get("cwd", ""),
                        "last": rec.get("lastActivityAt", 0)})
    return out[:limit]


def recheck_stopped():
    """On startup, re-judge desks whose lightbulb came from a finished turn, using the transcript's
    last turn: lightbulb only if that turn ended on a question for the user."""
    for s in sessions.values():
        if s.get("state") not in ("needs_input", "resting", "done") or s.get("last_event") not in ("Stop", "Notification"):
            continue
        path = s.get("transcript") or ""
        if not path:
            found = glob.glob(os.path.join(os.path.expanduser("~"), ".claude", "projects", "*", s["id"] + ".jsonl"))
            path = found[0] if found else ""
        try:
            with open(path, "rb") as f:
                f.seek(max(0, os.path.getsize(path) - 600_000))
                lines = f.read().decode("utf-8", "ignore").splitlines()
        except OSError:
            continue
        asked, last_text = None, ""
        for line in lines:
            try:
                j = json.loads(line)
            except ValueError:
                continue
            content = (j.get("message") or {}).get("content") if isinstance(j, dict) else None
            if j.get("type") == "user" and isinstance(content, str):
                asked, last_text = None, ""
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if j.get("type") == "user" and b.get("type") == "text":
                    asked, last_text = None, ""
                elif b.get("type") == "tool_use" and str(b.get("name", "")).endswith("wrap_up"):
                    asked = bool((b.get("input") or {}).get("ask"))
                elif j.get("type") == "assistant" and b.get("type") == "text":
                    last_text = b.get("text", "")
        if asked or asks_user(last_text):
            s["state"] = "needs_input"
        elif WAITING.search(last_text):
            s["state"], s["tool"] = "working", "background task"
        elif s["state"] == "needs_input":
            s["state"] = "done"


# Phrases that mean "I stopped, but only until something in the background finishes".
WAITING = re.compile(
    r"i'll be notified|i will be notified|will be notified|when (it|that|this|the \w+) (finishes|completes|is done|passes)|"
    r"still running|running now|running in the background|i'm waiting (on|for)|i am waiting (on|for)|has not finished|hasn't finished|"
    r"not finished yet|once (it|that|the \w+) (finishes|completes|passes)|i'll check back|will check back", re.I)


def tail_lines(s):
    path = s.get("transcript") or ""
    if not path:
        found = glob.glob(os.path.join(os.path.expanduser("~"), ".claude", "projects", "*", s["id"] + ".jsonl"))
        path = found[0] if found else ""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 600_000))
            return f.read().decode("utf-8", "ignore").splitlines(), os.path.getmtime(path)
    except OSError:
        return [], 0


def reconcile():
    """Catch turn endings whose hook event never arrived (for example while this server was
    restarting): a desk that has looked busy for a while is checked against its transcript."""
    now = time.time()
    with lock:
        stale = [dict(x) for x in sessions.values()
                 if x.get("state") == "working" and x.get("tool") != "background task" and now - x.get("last_seen", 0) > 40]
    for snap in stale:
        lines, mtime = tail_lines(snap)
        if not lines or now - mtime < 20:
            continue
        asked, last_text, ended = None, "", False
        for line in lines:
            try:
                j = json.loads(line)
            except ValueError:
                continue
            if not isinstance(j, dict):
                continue
            kind = j.get("type")
            content = (j.get("message") or {}).get("content") if isinstance(j.get("message"), dict) else None
            if kind == "system" and j.get("subtype") == "stop_hook_summary":
                ended = True
            elif kind == "user" and isinstance(content, str):
                asked, last_text, ended = None, "", False
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if kind == "user" and b.get("type") == "text":
                    asked, last_text, ended = None, "", False
                elif b.get("type") == "tool_use":
                    ended = False
                    if str(b.get("name", "")).endswith("wrap_up"):
                        asked = bool((b.get("input") or {}).get("ask"))
                elif kind == "assistant" and b.get("type") == "text":
                    last_text = b.get("text", "")
        if not ended:
            # No sign of the turn ending in the transcript either (seen with command-line sessions
            # under Remote Control). The command-line tool's own record is the last word: idle since after
            # the desk's last event, and for a little while, means the turn is over.
            idle = cli_idle_since().get(snap["id"])
            if not idle or idle < snap.get("last_seen", 0) or now - idle < 8:
                continue
            mtime = idle
        with lock:
            s = sessions.get(snap["id"])
            if not s or s.get("last_seen") != snap.get("last_seen"):
                continue
            s["message"] = last_text[:4000]
            if asked or asks_user(last_text):
                s["state"] = "needs_input"
            elif WAITING.search(last_text):
                s["state"], s["tool"] = "working", "background task"
            else:
                s["state"] = "done"
            s["last_event"], s["stopped_at"] = "Stop", mtime
            save()


# Ways a turn can end by handing the decision to the user without a question mark.
ASKING = re.compile(
    r"waiting for your|awaiting your|your go-ahead|go-ahead\.?\s*$|your call|let me know|please confirm|please choose|"
    r"tell me (which|if|whether|what|how)|reply (with )?[`'\"]?(yes|go|ok)|say (the word|go|yes)|should i\b|do you want|"
    r"would you like|decisions? for you|need(s)? your (approval|decision|answer|input|confirmation)|once you (approve|confirm|decide)", re.I)


def asks_user(text):
    """True when the end of a message leaves the next move to the user."""
    return "?" in text[-300:] or bool(ASKING.search(text[-900:]))


def watch_foreground(seconds=45):
    """Diagnostics: after a desk click, record which window is in front and when that changes."""
    import ctypes
    from ctypes import wintypes
    u, k = ctypes.windll.user32, ctypes.windll.kernel32

    def front():
        h = u.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(200)
        u.GetWindowTextW(h, buf, 200)
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(h, ctypes.byref(pid))
        exe = ""
        hp = k.OpenProcess(0x1000, False, pid.value)
        if hp:
            nb = ctypes.create_unicode_buffer(400)
            size = wintypes.DWORD(400)
            if k.QueryFullProcessImageNameW(hp, 0, nb, ctypes.byref(size)):
                exe = os.path.basename(nb.value)
            k.CloseHandle(hp)
        return "%s | %s" % (exe, buf.value[:70])

    t0, last = time.time(), None
    while time.time() - t0 < seconds:
        try:
            cur = front()
        except Exception:
            return
        if cur != last:
            last = cur
            with open(os.path.join(ROOT, "clicks.log"), "a", encoding="utf-8") as f:
                f.write("%s server=%.3f %s\n" % (time.strftime("%H:%M:%S"), time.time(),
                        json.dumps({"what": "in front", "after_ms": int((time.time() - t0) * 1000), "window": cur})))
        time.sleep(0.1)


def front_exe():
    """File name of the program whose window is in front (Windows only)."""
    import ctypes
    from ctypes import wintypes
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    pid = wintypes.DWORD()
    u.GetWindowThreadProcessId(u.GetForegroundWindow(), ctypes.byref(pid))
    hp = k.OpenProcess(0x1000, False, pid.value)
    if not hp:
        return ""
    nb, size = ctypes.create_unicode_buffer(400), wintypes.DWORD(400)
    ok = k.QueryFullProcessImageNameW(hp, 0, nb, ctypes.byref(size))
    k.CloseHandle(hp)
    return os.path.basename(nb.value).lower() if ok else ""


def claude_window():
    """Handle of Claude's window when it is the one in front, else None (Windows only)."""
    import ctypes
    from ctypes import wintypes
    if front_exe() != "claude.exe":
        return None
    return wintypes.HWND(ctypes.windll.user32.GetForegroundWindow())


def look(h):
    """A small, blurred picture of the middle of a window as it is on screen right now: enough to
    tell whether what it shows has changed, nothing more. Kept in memory only."""
    import ctypes
    from ctypes import wintypes
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    for f in (u.GetDC, g.CreateCompatibleDC, g.CreateCompatibleBitmap, g.SelectObject):
        f.restype = ctypes.c_void_p
    r = wintypes.RECT()
    if not u.GetWindowRect(h, ctypes.byref(r)):
        return None
    w, ht = r.right - r.left, r.bottom - r.top
    x, y, w, ht = r.left + w // 5, r.top + ht // 7, w // 2, ht * 2 // 3  # the conversation, not the side bars
    W, H = 48, 32
    screen = ctypes.c_void_p(u.GetDC(None))
    mem = ctypes.c_void_p(g.CreateCompatibleDC(screen))
    bmp = ctypes.c_void_p(g.CreateCompatibleBitmap(screen, W, H))
    old = ctypes.c_void_p(g.SelectObject(mem, bmp))
    try:
        g.SetStretchBltMode(mem, 4)  # halftone: every cell is the average of its patch of screen
        g.StretchBlt(mem, 0, 0, W, H, screen, x, y, w, ht, 0x00CC0020)
        head = (ctypes.c_uint32 * 10)(40, W, H, 1 | (32 << 16), 0, 0, 0, 0, 0, 0)  # BITMAPINFOHEADER
        buf = ctypes.create_string_buffer(W * H * 4)
        g.SelectObject(mem, old)
        if g.GetDIBits(mem, bmp, 0, H, buf, head, 0) != H:
            return None
        return buf.raw
    finally:
        g.DeleteObject(bmp)
        g.DeleteDC(mem)
        u.ReleaseDC(None, screen)


def differs(a, b):
    """How much of the picture changed between two looks, 0 to 1."""
    if not a or not b or len(a) != len(b):
        return 0.0
    n = sum(1 for i in range(0, len(a), 4) if abs(a[i] - b[i]) + abs(a[i + 1] - b[i + 1]) + abs(a[i + 2] - b[i + 2]) > 18)
    return n / (len(a) // 4)


def nudge_claude(h, strong=False):
    """Poke Claude's window so it redraws. Claude sometimes has the session link and its window in
    front, yet keeps the old conversation on screen until the window is moved or resized. The
    gentle poke moves and resizes it by one pixel and straight back, which cannot be seen. The
    strong one does what dragging it by hand does: a maximized window is restored and maximized
    again, any other is shifted a few pixels and back. Returns what was done, for clicks.log."""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    if u.IsIconic(h):
        return "minimized"
    r = wintypes.RECT()
    if not u.GetWindowRect(h, ctypes.byref(r)):
        return "no window"
    x, y, w, ht, flags = r.left, r.top, r.right - r.left, r.bottom - r.top, 0x0004 | 0x0010  # no reorder, no activate
    if strong and u.IsZoomed(h):
        u.ShowWindow(h, 9)  # restore
        time.sleep(0.06)
        u.ShowWindow(h, 3)  # maximize
        return "restore+maximize"
    step = 12 if strong else 1
    u.SendMessageTimeoutW(h, 0x0231, 0, 0, 2, 200, None)  # as a drag does: size/move begins
    u.SetWindowPos(h, None, x + step, y, w + step, ht, flags)
    time.sleep(0.06 if strong else 0.03)
    u.SetWindowPos(h, None, x, y, w, ht, flags)
    u.SendMessageTimeoutW(h, 0x0232, 0, 0, 2, 200, None)  # size/move ends
    u.RedrawWindow(h, None, None, 0x0001 | 0x0080 | 0x0400)  # invalidate, all children, frame
    return "shift" if strong else "nudge"


def raise_claude():
    """Bring Claude's main window to the front from here, for the times its own link does not.
    Windows refuses that to a background program unless a key was just pressed, so Alt is tapped
    first. Returns what happened, for clicks.log."""
    import ctypes
    from ctypes import wintypes
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    u.GetWindowTextLengthW.argtypes = u.IsWindowVisible.argtypes = [wintypes.HWND]
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(h, _):
        if u.IsWindowVisible(h) and u.GetWindowTextLengthW(h) and not u.GetWindow(h, 4):  # visible, titled, unowned
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(h, ctypes.byref(pid))
            hp = k.OpenProcess(0x1000, False, pid.value)
            if hp:
                nb, size = ctypes.create_unicode_buffer(400), wintypes.DWORD(400)
                ok = k.QueryFullProcessImageNameW(hp, 0, nb, ctypes.byref(size))
                k.CloseHandle(hp)
                if ok and os.path.basename(nb.value).lower() == "claude.exe":
                    r = wintypes.RECT()
                    u.GetWindowRect(h, ctypes.byref(r))
                    found.append(((r.right - r.left) * (r.bottom - r.top), h))
        return True

    u.EnumWindows(each, 0)
    if not found:
        return "no claude window"
    h = max(found)[1]  # the largest one is the app itself
    u.keybd_event(0x12, 0, 0, 0)  # Alt down, up
    u.keybd_event(0x12, 0, 2, 0)
    if u.IsIconic(h):
        u.ShowWindow(h, 9)  # restore a minimized window
    return "raised" if u.SetForegroundWindow(h) else "refused"


last_opened = [""]  # the session the office last sent Claude to


def open_when_visible(app_id, trial=None):
    """Send Claude the session link, which also brings its window forward, then watch the window
    for a few seconds. About one switch in eight, Claude comes forward and keeps the old
    conversation on screen; when the picture has not changed by the time a good switch would
    have shown, the window is poked the way a hand drag would, which makes it draw. It stops as
    soon as the picture changes. Everything is timed into clicks.log.

    Measured over covered-window trials: link first shows the session about 1.0s after the click;
    bringing the window forward first and sending the link once it is in front (the older order,
    still here as trial {"direct": False}) took about 1.7s. Invisible one-pixel nudges never freed
    a stuck window; restoring and maximizing it did, every time."""
    t0 = time.time()
    trial = trial or {}  # measurements only: {"direct": False, "wait": seconds, "pokes": False}
    direct = trial.get("direct", True)
    try:
        if direct:
            os.startfile("claude://claude.ai/epitaxy/" + app_id)
        # The link normally has Claude in front within about half a second. Now and then it does not
        # come forward at all; after a second without it, it is brought forward from here.
        raise_at, raised = float(trial.get("raise_at", 1.0)) if direct and trial.get("raise") is not False else 99, None
        while time.time() - t0 < 4 and front_exe() != "claude.exe":
            time.sleep(0.05)
            if raised is None and time.time() - t0 >= raise_at:
                raised = [int((time.time() - t0) * 1000), raise_claude()]
        waited = time.time() - t0
        # direct: Claude comes forward still showing the old session for about half a second;
        # that first sight of it is what later looks are compared with
        time.sleep(0.1 if direct else min(3.0, float(trial.get("wait", 0.35))))
        h = claude_window()
        before = look(h) if h else None
        same = last_opened[0] == app_id  # then the picture may rightly stay as it is
        if not direct:
            os.startfile("claude://claude.ai/epitaxy/" + app_id)
        last_opened[0] = app_id
        note = {"what": "session link sent", "app_id": app_id, "claude_in_front_after_ms": int(waited * 1000)}
        if trial:
            note["trial"] = trial
        if raised:
            note["raised"] = raised  # [ms after the click, what happened]: the link alone did not bring Claude forward
        sent, pokes, most, changed = (t0 if direct else time.time()), [], 0.0, None
        # a switch that is going well shows within about 1.5s of the click
        gentle, strong, quiet = ([], [1.6, 2.9, 4.2], 0) if direct else ([0.3, 0.7], [1.5, 2.8, 4.2], 0)
        if trial.get("pokes") is False:
            gentle, strong = [], []
        while h and before and time.time() - sent < 6:
            time.sleep(0.08)
            now = time.time() - sent
            if claude_window() is None:
                note["left_front_ms"] = int(now * 1000)
                break
            if now < quiet:  # the strong poke's own flicker is not Claude changing what it shows
                continue
            d = differs(before, look(h))
            most = max(most, d)
            if d >= CHANGED:
                changed = int(now * 1000)
                break
            if gentle and now >= gentle[0]:
                gentle.pop(0)
                pokes.append([int(now * 1000), nudge_claude(h)])
            elif strong and now >= strong[0] and not same:
                strong.pop(0)
                pokes.append([int(now * 1000), nudge_claude(h, True)])
                quiet = time.time() - sent + 0.45
        note.update({"changed_ms": changed, "most_change": round(most, 3), "pokes": pokes, "same_as_last": same})
        if trial:
            time.sleep(max(0.0, 6.5 - (time.time() - sent)))
            note["end_change"] = round(differs(before, look(h)), 3) if h and before and claude_window() else None
    except Exception as e:
        note = {"what": "session link failed", "error": str(e)}
    with open(os.path.join(ROOT, "clicks.log"), "a", encoding="utf-8") as f:
        f.write("%s server=%.3f %s\n" % (time.strftime("%H:%M:%S"), time.time(), json.dumps(note)))


CHANGED = 0.10  # this much of the picture differing means Claude is showing something else now


SETTINGS_FILE = os.path.join(ROOT, "settings.json")
settings = {"theme": "pokemon"}  # which cast sits at the desks: "pokemon" or "maple"
try:
    with open(SETTINGS_FILE, encoding="utf-8") as f:
        settings.update({k: v for k, v in json.load(f).items() if k in settings})
except (OSError, ValueError, AttributeError):
    pass
if settings["theme"] not in office["themes"]:
    settings["theme"] = office["themes"][0]


TODOS_FILE = os.path.join(ROOT, "todos.json")
TODO_STATUSES = ("progress", "queued", "done")  # the page's sections, top to bottom
todos = []  # [{"id", "text", "status", "added"}], in the order the user dragged them; new ones go last
try:
    with open(TODOS_FILE, encoding="utf-8") as f:
        todos = [t for t in json.load(f) if isinstance(t, dict) and t.get("id") and t.get("text")]
    for t in todos:  # older lists only had done: true/false
        was_done = t.pop("done", False)
        if t.get("status") not in TODO_STATUSES:
            t["status"] = "done" if was_done else "queued"
except (OSError, ValueError, TypeError):
    pass


def save_todos():
    with open(TODOS_FILE, "w", encoding="utf-8") as f:
        json.dump(todos, f)


MUSIC_DIR = os.path.join(ROOT, "music")
MUSIC_TYPES = {"mp3": "audio/mpeg", "ogg": "audio/ogg", "m4a": "audio/mp4", "wav": "audio/wav", "flac": "audio/flac"}


def music_files():
    """Audio files in the music folder, by name. The folder is yours to fill; nothing ships in it."""
    try:
        return sorted(n for n in os.listdir(MUSIC_DIR)
                      if n.rsplit(".", 1)[-1].lower() in MUSIC_TYPES and os.path.isfile(os.path.join(MUSIC_DIR, n)))
    except OSError:
        return []


def mon_for(sid):
    return int(hashlib.md5(sid.encode()).hexdigest(), 16) % 809 + 1


def apply_event(ev):
    sid = ev.get("session_id")
    name = ev.get("hook_event_name", "")
    if not sid or sid in hidden:
        return
    with lock:
        if name == "SessionEnd":
            sessions.pop(sid, None)
            save()
            return
        s = sessions.setdefault(sid, {
            "id": sid, "mon": mon_for(sid), "first_seen": time.time(),
            "state": "idle", "title": "", "prompt": "", "message": "", "tool": "",
        })
        s["cwd"] = ev.get("cwd") or s.get("cwd", "")
        s["last_event"] = name
        s["last_seen"] = time.time()
        s["transcript"] = ev.get("transcript_path") or s.get("transcript", "")
        tool_input = json.dumps(ev.get("tool_input") or {})
        parts = [ev.get("prompt") or "", tool_input, ev.get("last_assistant_message") or ""]
        note_prs(s, "\n".join(parts))
        if "gh pr create" in tool_input:  # the answer carries the new pull request's link: this session opened it
            s["pr_made"] = sorted(set(s.get("pr_made") or []) | created_prs(ev.get("tool_response")))
            note_prs(s, json.dumps(ev.get("tool_response") or ""), 3)
        if name == "UserPromptSubmit":
            s["state"] = "working"
            s.pop("asked", None)
            s.pop("recap", None)
            s.pop("bg", None)
            s.pop("waiting_pr", None)
            s["tool"] = ""  # not "background task" any more: a new prompt means real work
            s["prompt"] = (ev.get("prompt") or "")[:500]
            s["message"] = ""
            if not s["title"] or APP_BLOCK.match(s["title"]):
                s["title"] = first_line(ev.get("prompt"))
        elif name == "PreToolUse":
            s["tool"] = ev.get("tool_name", "")
            if s["tool"] in ASKING_TOOLS:
                s["state"] = "needs_input"
                qs = (ev.get("tool_input") or {}).get("questions") or []
                s["message"] = qs[0].get("question", "") if qs else "Waiting for your decision"
            else:
                s["state"] = "working"
            if s["tool"].endswith("wrap_up"):
                # The desktop app's end-of-turn box: it carries a question only when the user must decide.
                s["asked"] = bool((ev.get("tool_input") or {}).get("ask"))
                s["recap"] = str((ev.get("tool_input") or {}).get("recap") or "")
            if s["tool"] in ("ScheduleWakeup", "Monitor", "Workflow") or (ev.get("tool_input") or {}).get("run_in_background"):
                s["bg"] = True
        elif name == "PostToolUse":
            s["state"] = "working"
        elif name == "Notification":
            text = ev.get("message") or ""
            idle = ev.get("notification_type") == "idle_prompt" or "waiting for your input" in text.lower()
            if not idle:  # a permission prompt or similar; the idle reminder says nothing new
                s["state"] = "needs_input"
                s["message"] = s["message"] or text[:2000]
        elif name == "Stop":
            # Lightbulb only when the turn ended on a question for the user. A turn that ended
            # without one is finished or waiting on background work: no input needed.
            s["message"] = (ev.get("last_assistant_message") or "Finished its turn")[:4000]
            asks = s.pop("asked", None) or asks_user(s["message"])
            waiting = s.pop("bg", False) or bool(WAITING.search(s.pop("recap", "") + " " + s["message"]))
            # question -> lightbulb; waiting on background work -> rest; otherwise the work is
            # finished and waits for the user to look at it (cleared when they click the desk).
            s["state"] = "needs_input" if asks else "working" if waiting else "done"
            s["stopped_at"] = time.time()
            if s["state"] == "working":
                s["tool"] = "background task"
            review_hold(s)
        if s["state"] == "working" and name in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
            s.pop("asleep", None)  # sleep is a one-time mute: it ends when the session next does any work
        save()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json", extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def local(self):
        """Whether the request comes from this computer rather than another device on the network."""
        return self.client_address[0] in ("127.0.0.1", "::1")

    def refused(self):
        """Another device must show the private key: once in the address (?key=...), which is swapped
        for a cookie so it does not stay in the address bar, and by that cookie from then on.
        Returns True when the request has been answered here."""
        if self.local():
            return False
        ok = lambda v: bool(LAN_KEY) and hmac.compare_digest(str(v).encode(), LAN_KEY.encode())
        shown = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("key", [""])[0]
        if self.command == "GET" and ok(shown):
            self._send(302, b"", extra=[("Location", "/"), ("Set-Cookie", "office_key=%s; Max-Age=31536000; Path=/; HttpOnly; SameSite=Lax" % LAN_KEY)])
            return True
        cookies = dict(c.strip().split("=", 1) for c in (self.headers.get("Cookie") or "").split(";") if "=" in c)
        if ok(cookies.get("office_key", "")):
            return False
        self._send(403, b"This office needs its private link. Open the address in phone-link.txt on the computer that runs it.", "text/plain; charset=utf-8")
        return True

    def do_GET(self):
        if self.refused():
            return
        path = self.path.split("?")[0]
        if path == "/state":
            with lock:
                data = sorted(sessions.values(), key=lambda s: (s.get("order", 10**6), s["first_seen"]))
                body = json.dumps(data).encode()
            self._send(200, body)
        elif path == "/logo-maple.png":  # the MapleStory theme's logo; without the file the page keeps its drawn one
            full = os.path.join(ROOT, "logo-maple.png")
            if os.path.isfile(full):
                with open(full, "rb") as f:
                    self._send(200, f.read(), "image/png")
            else:
                self._send(404, b"{}")
        elif path.startswith("/markers/"):
            # optional user-supplied marker images: markers/bulb.png|gif and markers/book.png|gif
            name = os.path.basename(path)
            full = os.path.join(ROOT, "markers", name)
            ctype = {"png": "image/png", "gif": "image/gif", "webp": "image/webp"}.get(name.rsplit(".", 1)[-1].lower())
            if ctype and os.path.isfile(full):
                with open(full, "rb") as f:
                    self._send(200, f.read(), ctype)
            else:
                self._send(404, b"{}")
        elif path == "/stocks":
            out = [dict(quotes.get(x) or {"symbol": x, "name": x}, live=market_open(x)) for x in watch] if office["stocks"] else []
            self._send(200, json.dumps(out).encode())
        elif path == "/music":  # the audio files you have put in the music folder, for MapleStory mode
            self._send(200, json.dumps(music_files()).encode())
        elif path.startswith("/music/"):
            name = os.path.basename(urllib.parse.unquote(path[7:]))
            if name in music_files():  # only ever a file from that folder
                with open(os.path.join(MUSIC_DIR, name), "rb") as f:
                    data = f.read()
                ctype = MUSIC_TYPES[name.rsplit(".", 1)[-1].lower()]
                # phones ask for a piece of the file at a time and will not play audio served any other way
                m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
                if m and (m.group(1) or m.group(2)) and data:
                    a = int(m.group(1)) if m.group(1) else max(0, len(data) - int(m.group(2)))
                    b = min(int(m.group(2)), len(data) - 1) if m.group(1) and m.group(2) else len(data) - 1
                    if a > b:
                        self._send(416, b"", ctype, [("Content-Range", "bytes */%d" % len(data))])
                    else:
                        self._send(206, data[a:b + 1], ctype, [("Accept-Ranges", "bytes"), ("Content-Range", "bytes %d-%d/%d" % (a, b, len(data)))])
                else:
                    self._send(200, data, ctype, [("Accept-Ranges", "bytes")])
            else:
                self._send(404, b"{}")
        elif path == "/config":
            # remote: the page is open on another device, where a desk opens Remote Control instead
            self._send(200, json.dumps(dict(office, remote=not self.local())).encode())
        elif path == "/theme":
            self._send(200, json.dumps({"theme": settings["theme"]}).encode())
        elif path == "/meetings":  # your upcoming meetings and the reminders showing now
            out = meetings.snapshot()
            out["can_link"] = self.local()  # only this computer may change the calendar link
            self._send(200, json.dumps(out).encode())
        elif path == "/alarm":  # Windows' own alarm sound, which a phone rings with for a meeting reminder
            try:
                with open(meetings.ALARM, "rb") as f:
                    self._send(200, f.read(), "audio/wav")
            except OSError:
                self._send(404, b"{}")
        elif path == "/sound":  # a desk's MapleStory sound: /sound?id=GMS/255/Sound/Game.img/QuestAlert
            sid = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("id", [""])[0]
            try:
                self._send(200, sounds.clip(sid), "audio/mpeg")
            except Exception as e:
                self._send(404, json.dumps({"error": str(e)[:200]}).encode())
        elif path == "/todos":
            with lock:
                self._send(200, json.dumps(todos).encode())
        elif path == "/maple":  # the MapleStory monster list: [[id, name, level, boss], ...]
            try:
                with open(os.path.join(ROOT, "maple.json"), "rb") as f:
                    self._send(200, f.read())
            except OSError:
                self._send(404, b"[]")
        elif path == "/chart":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            sym = (qs.get("symbol") or [""])[0]
            out = {"ok": False}
            if office["stocks"] and sym in watch:  # only ever a symbol from the header list
                try:
                    out = fetch_chart(sym, (qs.get("range") or ["1d"])[0])
                except Exception:
                    pass
            self._send(200, json.dumps(out).encode())
        elif path == "/merged":
            week = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("week", ["0"])[0]
            out = dict(merged_week(week if week.isdigit() else 0), indexed=credits_ready)
            out["items"] = [dict(p, credits=credits_of(p["repo"], p["number"])) for p in out["items"]]
            self._send(200, json.dumps(out).encode())
        elif path == "/available":
            recs = app_records()
            with lock:
                recs = [dict(r, removed=bool({r["id"], r["alt_id"]} & hidden)) for r in recs
                        if r["id"] not in sessions and r["alt_id"] not in sessions]
            self._send(200, json.dumps(recs).encode())
        elif path in ("/", "/index.html"):
            with open(os.path.join(ROOT, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        else:
            self._send(404, b"{}")

    def do_POST(self):
        if self.refused():
            return
        raw =self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            ev = json.loads(raw or b"{}")
        except ValueError:
            return self._send(400, b"{}")
        if self.path == "/event":
            apply_event(ev)
        elif self.path in ("/show", "/quit"):
            # For the tray icon and the launcher only: this computer, and not a web page open in a
            # browser here (a browser always names the page a POST comes from).
            if self.local() and not self.headers.get("Origin"):
                if self.path == "/show":
                    threading.Thread(target=tray.show, args=(office["name"], PORT), daemon=True).start()
                else:
                    self._send(200, b"{}")
                    os._exit(0)
        elif office["stocks"] and self.path in ("/stocks/add", "/stocks/remove"):
            sym = str(ev.get("symbol") or "").strip().upper()[:15]
            if sym and self.path.endswith("add") and sym not in watch:
                watch.append(sym)
            elif sym in watch and self.path.endswith("remove"):
                watch.remove(sym)
                quotes.pop(sym, None)
            with open(STOCKS_FILE, "w", encoding="utf-8") as f:
                json.dump(watch, f)
        elif self.path == "/add":
            rec = next((r for r in app_records(60) if ev.get("id") in (r["id"], r["alt_id"])), None)
            if rec:
                with lock:
                    hidden.difference_update({rec["id"], rec["alt_id"]})
                    sessions.setdefault(rec["id"], {
                        "id": rec["id"], "mon": mon_for(rec["id"]), "first_seen": time.time(), "state": "idle",
                        "title": "", "prompt": "", "message": "", "tool": "", "cwd": rec["cwd"],
                        "last_event": "added", "last_seen": time.time(),
                        "app_id": rec["app_id"], "app_title": rec["title"], "rc_id": rec["rc_id"]})
                    save()
        elif self.path == "/ack":
            with lock:
                x = sessions.get(ev.get("id"))
                if x and x.get("state") == "done":
                    x["state"] = "resting"
                    save()
        elif self.path == "/open":
            app_id = str(ev.get("app_id") or "")
            # only ever a session id, and only from this computer: a phone opens Remote Control itself
            if self.local() and re.fullmatch(r"local_[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", app_id):
                threading.Thread(target=open_when_visible, args=(app_id, ev.get("trial")), daemon=True).start()
        elif self.path == "/clicklog":
            # timing diagnostics for "open in Claude": when the click happened and when the browser lost focus
            with open(os.path.join(ROOT, "clicks.log"), "a", encoding="utf-8") as f:
                f.write("%s server=%.3f %s\n" % (time.strftime("%H:%M:%S"), time.time(), json.dumps(ev)))
        elif self.path == "/setmon":
            with lock:
                x = sessions.get(ev.get("id"))
                if x and isinstance(ev.get("mon"), int) and 1 <= ev["mon"] <= 809:
                    x["mon"] = ev["mon"]
                    save()
                elif x and isinstance(ev.get("maple"), int) and 0 < ev["maple"] < 10 ** 8:
                    x["maple"] = ev["maple"]  # a MapleStory monster id; each desk keeps one per theme
                    save()
        elif self.path == "/theme":
            if ev.get("theme") in office["themes"]:
                settings["theme"] = ev["theme"]
                with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                    json.dump(settings, f)
        elif self.path in ("/todos/add", "/todos/status", "/todos/remove", "/todos/order"):
            with lock:
                if self.path.endswith("order"):
                    # {"groups": {status: [ids in order]}}: sets each named to-do's section and its place in it;
                    # any not named keep their section and go after the named ones
                    groups = ev.get("groups") if isinstance(ev.get("groups"), dict) else {}
                    rank = {}
                    for s in TODO_STATUSES:
                        for i in groups.get(s) or []:
                            if isinstance(i, str) and i not in rank:
                                rank[i] = (s, len(rank))
                    for t in todos:
                        if t["id"] in rank:
                            t["status"] = rank[t["id"]][0]
                    todos.sort(key=lambda t: rank[t["id"]][1] if t["id"] in rank else len(rank))
                elif self.path.endswith("add"):
                    text = " ".join(str(ev.get("text") or "").split())[:300]
                    if text:
                        todos.append({"id": secrets.token_hex(6), "text": text, "status": "queued",
                                      "added": time.time()})
                else:
                    t = next((t for t in todos if t["id"] == ev.get("id")), None)
                    if t and self.path.endswith("status"):
                        if ev.get("status") in TODO_STATUSES:
                            t["status"] = ev["status"]
                            todos.remove(t)
                            todos.append(t)  # lands at the bottom of its new section
                    elif t:
                        todos.remove(t)
                save_todos()
                return self._send(200, json.dumps(todos).encode())
        elif self.path == "/meetings/ack":
            if ev.get("key") in {r["key"] for r in meetings.due()}:
                meetings.ack(ev["key"])
        elif self.path == "/meetings/link":
            if not self.local():
                return self._send(403, json.dumps({"error": "Change the calendar link on the computer that runs the office."}).encode())
            err = meetings.set_link(str(ev.get("url") or "")[:4000])
            return self._send(400 if err else 200, json.dumps({"error": err}).encode())
        elif self.path == "/reorder":
            with lock:
                for n, sid in enumerate(ev.get("ids") or []):
                    if sid in sessions:
                        sessions[sid]["order"] = n
                save()
        elif self.path == "/rename":
            with lock:
                if ev.get("id") in sessions:
                    sessions[ev["id"]]["custom_name"] = str(ev.get("name") or "")[:80]
                    save()
        elif self.path == "/sleep":
            # asleep desks are drawn as resting by the page; the real state is still tracked here
            with lock:
                if ev.get("id") in sessions:
                    sessions[ev["id"]]["asleep"] = bool(ev.get("asleep"))
                    save()
        elif self.path == "/dismiss":
            with lock:
                sessions.pop(ev.get("id"), None)
                if ev.get("id"):
                    hidden.add(ev["id"])
                save()
        self._send(200, b"{}")


if __name__ == "__main__":
    recheck_stopped()
    threading.Thread(target=forever, args=(resolve_loop,), daemon=True).start()
    if office["stocks"]:
        threading.Thread(target=forever, args=(stock_loop,), daemon=True).start()
    threading.Thread(target=forever, args=(credit_loop,), daemon=True).start()
    threading.Thread(target=merged_loop, daemon=True).start()
    threading.Thread(target=forever, args=(meetings.calendar_loop,), daemon=True).start()
    threading.Thread(target=forever, args=(meetings.remind_loop,), daemon=True).start()
    print(f"{office['name']} running at http://localhost:{PORT}", flush=True)
    if office["lan"]:
        try:  # this computer's address on the network; connecting a UDP socket sends nothing
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as _s:
                _s.connect(("10.255.255.255", 1))
                _ip = _s.getsockname()[0]
        except OSError:
            _ip = socket.gethostbyname(socket.gethostname())
        _link = f"http://{_ip}:{PORT}/?key={LAN_KEY}"
        _lines = ["Same Wi-Fi as this computer:", _link]
        # With Tailscale on this computer and on the phone, the office is reachable from anywhere
        # through this computer's Tailscale address, which stays the same.
        try:
            _ts = next((p for p in (r"C:\Program Files\Tailscale\tailscale.exe",) if os.path.isfile(p)), "tailscale")
            _out = subprocess.run([_ts, "ip", "-4"], capture_output=True, text=True, timeout=5, creationflags=0x08000000).stdout.strip()
            if re.fullmatch(r"100(\.\d{1,3}){3}", _out):
                _lines += ["", "Anywhere, with Tailscale switched on in the phone:", f"http://{_out}:{PORT}/?key={LAN_KEY}"]
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        with open(os.path.join(ROOT, "phone-link.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(_lines) + "\n")
        print("On your phone (same Wi-Fi): " + _link, flush=True)
    httpd = ThreadingHTTPServer(("0.0.0.0" if office["lan"] else "127.0.0.1", PORT), Handler)
    tray.start(office["name"], PORT)  # only once the port is ours: a second copy must not add an icon
    httpd.serve_forever()
