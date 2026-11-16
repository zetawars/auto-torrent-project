#!/usr/bin/env python3
"""stallwatch: keeps the qBittorrent download queue moving.

A download that makes no progress for STALL_MINUTES is stopped, and its stall count and
last-try time are recorded. Stopped torrents are started again RETRY_HOURS later, oldest
last-try first, at the bottom of the queue. After MAX_STALLS stalls a torrent is left
stopped and tagged "needs-review". Only torrents this service stopped itself are ever
started again; torrents stopped by hand and force-started torrents are left alone.
No qBittorrent setting is changed: it only stops, starts, reorders and tags torrents.
"""
import html, http.cookiejar, json, os, threading, time, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

QB = os.environ.get("QBIT_URL", "http://gluetun:8080").rstrip("/")
USER = os.environ.get("QBIT_USERNAME", "")
PASSWORD = os.environ.get("QBIT_PASSWORD", "")
STALL_MIN = float(os.environ.get("STALL_MINUTES", "30"))
RETRY_H = float(os.environ.get("RETRY_HOURS", "6"))
MAX_STALLS = int(os.environ.get("MAX_STALLS", "5"))
MIN_BYTES = int(os.environ.get("MIN_PROGRESS_BYTES", str(1 << 20)))   # less than this per window = no progress
CHECK_MIN = float(os.environ.get("CHECK_MINUTES", "1"))            # how often qBittorrent is looked at
DRY = os.environ.get("DRY_RUN", "") == "1"
PORT = int(os.environ.get("PORT", "8091"))
RADARR = os.environ.get("RADARR_URL", "").rstrip("/")
RADARR_KEY = os.environ.get("RADARR_API_KEY", "")
REPLACE_AT = int(os.environ.get("REPLACE_STALLS", "3"))             # stalls before Radarr is asked for another release
MIN_SEEDERS = int(os.environ.get("MIN_SEEDERS", "2"))
MAX_SWAPS = 5                                                       # per check, each one is a full Radarr search
CLEAN_H = float(os.environ.get("CLEAN_HOURS", "24"))                # finished + done seeding this long and still here = nobody is importing it
MIN_FREE = float(os.environ.get("MIN_FREE_GB", "50"))               # below this, downloads are paused until space is back
GUARD_MIN = 5                                                       # how often free space is checked
DONE_STATES = {"stoppedUP", "pausedUP"}                             # finished and stopped by qBittorrent's seeding limit
STATE_FILE = "/data/state.json"
RUNNING = {"downloading", "stalledDL", "metaDL"}      # states that hold a download slot
TAG_STALLED, TAG_REVIEW = "stalled", "needs-review"
BOTH_TAGS = TAG_STALLED + "," + TAG_REVIEW
TAG_NO_HINDI = "no-hindi"                             # Radarr tag for movies whose file has no Hindi audio

lock = threading.Lock()
S = {"torrents": {}, "events": []}     # torrents: hash -> entry
last = {"ok": None, "error": "", "torrents": []}


def load():
    try:
        with open(STATE_FILE) as f:
            S.update(json.load(f))
    except (OSError, ValueError):
        pass


def save():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(S, f)
    os.replace(tmp, STATE_FILE)


def event(text):
    print(("DRY " if DRY else "") + text, flush=True)
    S["events"] = ([{"t": time.time(), "text": text}] + S["events"])[:200]


class Qbit:
    def __init__(self):
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def _send(self, path, data):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(QB + path, data=body, headers={"Referer": QB})
        with self.opener.open(req, timeout=30) as r:
            return r.read()

    def call(self, path, data=None):
        try:
            return self._send(path, data)
        except urllib.error.HTTPError as e:
            if e.code != 403:
                raise
        out = self._send("/api/v2/auth/login", {"username": USER, "password": PASSWORD})
        if b"Fails" in out:          # 5.x answers 204 with no body on success, older versions "Ok."
            raise RuntimeError("qBittorrent login refused")
        return self._send(path, data)

    def torrents(self):
        return json.loads(self.call("/api/v2/torrents/info"))

    def act(self, action, hashes, **extra):
        if DRY or not hashes:
            return
        self.call("/api/v2/torrents/" + action, dict(hashes="|".join(hashes), **extra))


qb = Qbit()


def radarr(method, path, body=None):
    req = urllib.request.Request(RADARR + "/api/v3" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Api-Key": RADARR_KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:      # a release search can take minutes
        raw = r.read()
    return json.loads(raw) if raw.strip() else None


def is_hindi(release):
    return any(f.get("name") == "Hindi" for f in release.get("customFormats") or [])


def find_replacement(h):
    """Radarr's queue item for this torrent and the best other release that still has seeders.

    A release named Hindi is only ever swapped for another one named Hindi, at any allowed
    quality. Liveness comes first (seeders, capped at 20), then Radarr's own score.
    """
    queue = radarr("GET", "/queue?pageSize=1000")["records"]
    item = next((r for r in queue if (r.get("downloadId") or "").lower() == h.lower() and r.get("movieId")), None)
    if not item:
        return None, None
    need_hindi = is_hindi(item)

    def usable(r):
        if r.get("protocol") != "torrent" or (r.get("seeders") or 0) < MIN_SEEDERS:
            return False
        if (r.get("infoHash") or "").lower() == h.lower() or r.get("title") == item.get("title"):
            return False
        if need_hindi and not is_hindi(r):
            return False
        # the stalled download itself makes Radarr reject equals as "already in queue"; anything else is a real no
        return all("queue" in x.lower() for x in r.get("rejections") or [])

    found = [r for r in radarr("GET", "/release?movieId=%d" % item["movieId"]) if usable(r)]
    pick = max(found, key=lambda r: (min(r.get("seeders") or 0, 20), r.get("customFormatScore") or 0), default=None)
    return item, pick


def replace(h, name):
    """Swap a torrent that keeps stalling for another release of the same movie."""
    item, pick = find_replacement(h)
    if not item:
        return
    if not pick:
        return "no live %salternative, keeps retrying: %s" % ("Hindi " if is_hindi(item) else "", name)
    if not DRY:
        radarr("DELETE", "/queue/%d?removeFromClient=true&blocklist=true&skipRedownload=true" % item["id"])
        radarr("POST", "/release", {"guid": pick["guid"], "indexerId": pick["indexerId"]})
    return "replaced dead torrent: %s -> %s (%d seeders)" % (name, pick["title"], pick.get("seeders") or 0)


def guard():
    """Pause every download while the disk is nearly full; start them again once space is back."""
    free = json.loads(qb.call("/api/v2/sync/maindata"))["server_state"]["free_space_on_disk"] / 1e9
    last["free"] = free
    with lock:
        held = S.setdefault("guard", [])
        if free < MIN_FREE:
            new = [t["hash"] for t in qb.torrents() if t["progress"] < 1 and not t.get("force_start")
                   and t["state"] in RUNNING | {"queuedDL"}]
            if new:
                qb.act("stop", new)
                if not held:
                    event("disk nearly full (%.0f GB free): paused %d downloads" % (free, len(new)))
                S["guard"] = held + new
                save()
        elif held and free >= MIN_FREE + 20:
            qb.act("start", held)
            event("disk space is back (%.0f GB free): started %d downloads again" % (free, len(held)))
            S["guard"] = []
            save()


def clean(ts, now):
    """Remove finished torrents that are done seeding and that nobody imported for CLEAN_H hours.

    Only Radarr's are removed, and only when Radarr is not tracking them at all or refuses
    them as "not an upgrade". Everything else is left and shown on the page.
    """
    with lock:
        done = S.setdefault("done", {})
        stopped = {t["hash"]: t for t in ts if t["progress"] >= 1 and t["state"] in DONE_STATES
                   and t["category"].endswith("-fallback")}
        for h in list(done):
            if h not in stopped:
                del done[h]
        for h in stopped:
            done.setdefault(h, now)
        old = [stopped[h] for h, t0 in done.items()
               if now - t0 >= CLEAN_H * 3600 and stopped[h]["category"].startswith("radarr")]
    if not old or not RADARR:
        return
    queue = {(r.get("downloadId") or "").lower(): r for r in radarr("GET", "/queue?pageSize=1000&includeUnknownMovieItems=true")["records"]}
    for t in old:
        item = queue.get(t["hash"].lower())
        why = " ".join(m for s in (item or {}).get("statusMessages") or [] for m in s.get("messages") or [])
        if item is None:
            qb.act("delete", [t["hash"]], deleteFiles="true")
            text = "removed finished download Radarr is not tracking: " + t["name"]
        elif "upgrade" in why.lower():
            if not DRY:
                radarr("DELETE", "/queue/%d?removeFromClient=true&blocklist=false" % item["id"])
            text = "removed finished download Radarr refused (not an upgrade): " + t["name"]
        else:
            continue
        with lock:
            event(text)
            save()


def tag_audio():
    """Tag movies whose imported file really has no Hindi audio track (names lie), untag when one arrives.

    Files with an unlabelled track are left alone: it may be the Hindi one.
    """
    tags = {t["label"]: t["id"] for t in radarr("GET", "/tag")}
    tid = tags.get(TAG_NO_HINDI) or radarr("POST", "/tag", {"label": TAG_NO_HINDI})["id"]
    add, drop = [], []
    for m in radarr("GET", "/movie"):
        langs = ((m.get("movieFile") or {}).get("mediaInfo") or {}).get("audioLanguages")
        langs = [x for x in (langs or "").split("/") if x]
        missing = bool(langs) and "hin" not in langs and "und" not in langs
        if missing and tid not in m["tags"]:
            add.append(m)
        elif not missing and tid in m["tags"]:
            drop.append(m)
    for ms, how in ((add, "add"), (drop, "remove")):
        if ms and not DRY:
            radarr("PUT", "/movie/editor", {"movieIds": [m["id"] for m in ms], "tags": [tid], "applyTags": how})
    with lock:
        for m in add[:20]:
            event("no Hindi audio in the file, tagged %s: %s" % (TAG_NO_HINDI, m["title"]))
        if len(add) > 20:
            event("...and %d more tagged %s" % (len(add) - 20, TAG_NO_HINDI))
        for m in drop:
            event("Hindi audio is there now, tag removed: " + m["title"])
        if add or drop:
            save()


def tick():
    ts = qb.torrents()
    now = time.time()
    by = {t["hash"]: t for t in ts}
    T = S["torrents"]
    swaps = []
    with lock:
        for h in list(T):                       # finished or deleted: forget it
            if h not in by or by[h]["progress"] >= 1:
                if h in by:
                    qb.act("removeTags", [h], tags=BOTH_TAGS)
                del T[h]

        for h, t in by.items():
            if t["progress"] >= 1:
                continue
            e = T.get(h)
            if t["state"] not in RUNNING or t.get("force_start"):
                if e:
                    e.pop("t0", None)
                continue
            if e is None:
                e = T[h] = {"stalls": 0, "ours": False, "review": False}
            e["name"], e["cat"] = t["name"], t["category"]
            if e["review"]:                     # a person started it again: clean slate
                e.update(review=False, stalls=0)
                qb.act("removeTags", [h], tags=BOTH_TAGS)
                event("review cleared by hand: " + t["name"])
            e["ours"] = False
            left = t["amount_left"]
            if "t0" not in e or e["left0"] - left >= MIN_BYTES or left > e["left0"]:
                e["t0"], e["left0"] = now, left
            elif now - e["t0"] >= STALL_MIN * 60:
                e["stalls"] += 1
                e.update(last_try=now, ours=True, review=e["stalls"] >= MAX_STALLS)
                e.pop("t0", None)
                qb.act("stop", [h])
                if RADARR and e["stalls"] >= REPLACE_AT and t["category"].startswith("radarr"):
                    swaps.append((h, t["name"]))
                if e["review"]:
                    qb.act("removeTags", [h], tags=TAG_STALLED)
                    qb.act("addTags", [h], tags=TAG_REVIEW)
                    event("stall %d, stopped for review: %s" % (e["stalls"], t["name"]))
                else:
                    qb.act("addTags", [h], tags=TAG_STALLED)
                    event("stall %d, paused: %s" % (e["stalls"], t["name"]))

        due = sorted((e["last_try"], h) for h, e in T.items()
                     if e["ours"] and not e["review"] and by[h]["state"] == "stoppedDL"
                     and now - e["last_try"] >= RETRY_H * 3600 and not S.get("guard"))
        for _, h in due:                        # oldest first, each to the back of the queue
            qb.act("start", [h])
            qb.act("bottomPrio", [h])
            qb.act("removeTags", [h], tags=TAG_STALLED)
            T[h]["resumed"] = now
            event("retry after stall %d: %s" % (T[h]["stalls"], T[h].get("name", h)))
        save()
        last.update(ok=now, error="", torrents=ts)

    for h, name in swaps[:MAX_SWAPS]:           # outside the lock: each search takes minutes
        try:
            text = replace(h, name)
        except Exception as e:
            text = "could not replace %s: %s: %s" % (name, type(e).__name__, e)
        if text:
            with lock:
                event(text)
                save()
    for job in (lambda: clean(ts, now), tag_audio) if RADARR else ():
        try:
            job()
        except Exception as e:
            print("radarr job error: %s: %s" % (type(e).__name__, e), flush=True)


def loop():
    next_tick = 0
    while True:
        try:
            guard()
            if time.time() >= next_tick:
                next_tick = time.time() + CHECK_MIN * 60 - 5
                tick()
        except Exception as e:                  # qBittorrent restarting, VPN down: try again next time
            last["error"] = "%s: %s" % (type(e).__name__, e)
            print("error:", last["error"], flush=True)
        time.sleep(min(CHECK_MIN, GUARD_MIN) * 60)


def retry_now(h):
    with lock:
        e = S["torrents"].get(h)
        if not e:
            return
        e.update(stalls=0, review=False, ours=False)
        e.pop("t0", None)
        qb.act("removeTags", [h], tags=BOTH_TAGS)
        qb.act("start", [h])
        event("started again by hand: " + e.get("name", h))
        save()


def ago(t, now):
    if not t:
        return "-"
    s = abs(now - t)
    txt = "%d d" % (s // 86400) if s >= 86400 else "%d h" % (s // 3600) if s >= 3600 else "%d min" % (s // 60)
    return "in " + txt if t > now else txt + " ago"


CSS = """
:root{--bg:#f6f5f1;--card:#fff;--fg:#1c1b19;--mut:#6b6a65;--line:#e3e1da;--warn:#b4530a;--btn:#1c1b19;--btnfg:#fff}
@media(prefers-color-scheme:dark){:root{--bg:#161614;--card:#21201d;--fg:#eceae4;--mut:#a09e97;--line:#35332f;--warn:#f0a35a;--btn:#eceae4;--btnfg:#161614}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:20px 16px 48px}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}
.sub{color:var(--mut);margin:0 0 16px}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}.stat b{display:block;font-size:22px;font-variant-numeric:tabular-nums}
.stat span{color:var(--mut);font-size:13px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.row{display:flex;gap:12px;align-items:center;padding:10px 12px;border-top:1px solid var(--line)}.row:first-child{border-top:0}
.row .n{flex:1;min-width:0;overflow-wrap:anywhere}.row small{display:block;color:var(--mut)}.empty{padding:14px 12px;color:var(--mut)}
button{background:var(--btn);color:var(--btnfg);border:0;border-radius:8px;padding:8px 12px;font:inherit;cursor:pointer;white-space:nowrap}
.warn{color:var(--warn)}.err{background:var(--card);border:1px solid var(--warn);color:var(--warn);border-radius:10px;padding:10px 12px;margin-bottom:12px}
"""


def page():
    now = time.time()
    with lock:
        T = json.loads(json.dumps(S["torrents"]))
        events = list(S["events"][:25])
        done, held = dict(S.get("done", {})), len(S.get("guard", []))
    by = {t["hash"]: t for t in last["torrents"]}
    left = sorted(((h, {"name": by[h]["name"], "t": t0, "cat": by[h]["category"]}) for h, t0 in done.items()
                   if h in by and now - t0 >= CLEAN_H * 3600), key=lambda x: x[1]["t"])
    esc = html.escape

    def pct(h):
        return "%.0f%%" % (by[h]["progress"] * 100) if h in by else "?"

    def rows(items, line, button=False):
        if not items:
            return '<div class="empty">Nothing here.</div>'
        out = []
        for h, e in items:
            b = ""
            if button:
                b = '<form method="post" action="retry"><input type="hidden" name="hash" value="%s"><button>Try again</button></form>' % h
            out.append('<div class="row"><div class="n">%s<small>%s</small></div>%s</div>' % (esc(e.get("name", h)), esc(line(h, e)), b))
        return "".join(out)

    review = sorted(((h, e) for h, e in T.items() if e["review"]), key=lambda x: x[1].get("last_try", 0))
    paused = sorted(((h, e) for h, e in T.items()
                     if e["ours"] and not e["review"] and by.get(h, {}).get("state") == "stoppedDL"),
                    key=lambda x: x[1].get("last_try", 0))
    active = sorted(((h, e) for h, e in T.items() if "t0" in e), key=lambda x: x[1]["t0"])
    queued = sum(1 for t in last["torrents"] if t["state"] == "queuedDL")
    moving = sum(1 for t in last["torrents"] if t["state"] in ("downloading", "forcedDL") and t["dlspeed"] > 0)

    def review_line(h, e):
        return "stalled %d times, last tried %s, %s done, %s" % (e["stalls"], ago(e.get("last_try"), now), pct(h), e.get("cat") or "no category")

    def paused_line(h, e):
        return "stalled %d of %d, last tried %s, next try %s, %s done" % (
            e["stalls"], MAX_STALLS, ago(e.get("last_try"), now), ago(e.get("last_try", now) + RETRY_H * 3600, now), pct(h))

    def active_line(h, e):
        return "%s done, no progress for %d min, stalled %d times before" % (pct(h), (now - e["t0"]) // 60, e["stalls"])

    parts = {
        "css": CSS,
        "stall": "%g" % STALL_MIN, "retry": "%g" % RETRY_H, "max": MAX_STALLS, "swap": REPLACE_AT, "check": "%g" % CHECK_MIN, "checked": ago(last["ok"], now),
        "err": '<div class="err">Cannot reach qBittorrent: %s</div>' % esc(last["error"]) if last["error"] else "",
        "dry": '<div class="err">Dry run: nothing is being stopped or started.</div>' if DRY else "",
        "guard": '<div class="err">Disk nearly full: %d downloads are paused until %g GB is free.</div>' % (held, MIN_FREE + 20) if held else "",
        "free": "%.0f" % last["free"] if last.get("free") is not None else "?",
        "left": rows(left, lambda h, e: "finished and done seeding %s, never imported, %s" % (ago(e["t"], now), e["cat"])),
        "moving": moving, "queued": queued, "npaused": len(paused), "nreview": len(review),
        "warn": "warn" if review else "",
        "review": rows(review, review_line, True), "paused": rows(paused, paused_line, True),
        "active": rows(active, active_line),
        "events": "".join('<div class="row"><div class="n">%s<small>%s</small></div></div>' % (esc(ev["text"]), ago(ev["t"], now))
                          for ev in events) or '<div class="empty">Nothing yet.</div>',
    }
    return """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60"><title>Stalled downloads</title><style>%(css)s</style></head><body><main>
<h1>Stalled downloads</h1>
<p class="sub">No progress for %(stall)s minutes pauses a download. It is tried again %(retry)s hours later. After %(swap)s stalls a movie is swapped for another release that still has seeders (a Hindi one only for another Hindi one), and after %(max)s stalls with no swap it waits here for you. Checked every %(check)s minutes, last %(checked)s.</p>
%(err)s%(dry)s%(guard)s
<div class="stats"><div class="stat"><b>%(free)s GB</b><span>free for downloads</span></div><div class="stat"><b>%(moving)s</b><span>downloading now</span></div><div class="stat"><b>%(queued)s</b><span>waiting in queue</span></div>
<div class="stat"><b>%(npaused)s</b><span>paused, will retry</span></div><div class="stat"><b class="%(warn)s">%(nreview)s</b><span>need review</span></div></div>
<h2>Need review</h2><div class="card">%(review)s</div>
<h2>Finished but never imported</h2><div class="card">%(left)s</div>
<h2>Paused, will retry</h2><div class="card">%(paused)s</div>
<h2>Holding a download slot</h2><div class="card">%(active)s</div>
<h2>Recent actions</h2><div class="card">%(events)s</div>
</main></body></html>""" % parts


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="text/html; charset=utf-8", extra=()):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self.send(200, page())
        elif path == "/api/state":
            with lock:
                self.send(200, json.dumps(S), "application/json")
        else:
            self.send(404, "not found", "text/plain")

    def do_POST(self):
        if self.path.split("?")[0] != "/retry":
            return self.send(404, "not found", "text/plain")
        n = min(int(self.headers.get("Content-Length") or 0), 4096)
        h = urllib.parse.parse_qs(self.rfile.read(n).decode()).get("hash", [""])[0]
        if len(h) in (40, 64) and h.isalnum():
            try:
                retry_now(h)
            except Exception as e:
                return self.send(502, "qBittorrent error: %s" % e, "text/plain")
        self.send(303, "", extra=[("Location", "./")])


if __name__ == "__main__":
    load()
    threading.Thread(target=loop, daemon=True).start()
    print("stallwatch on :%d  stall=%gmin retry=%gh max=%d dry=%s" % (PORT, STALL_MIN, RETRY_H, MAX_STALLS, DRY), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
