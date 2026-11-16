#!/usr/bin/env python3
"""songdrop: a one-box phone page that queues a song link in MeTube (as M4A audio)
and reports when the file has shown up in Jellyfin. Standard library only."""
import json
import os
import re
import struct
import threading
import time
import urllib.parse
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

METUBE = os.environ.get("METUBE_URL", "http://metube:8081").rstrip("/")
JELLYFIN = os.environ.get("JELLYFIN_URL", "http://jellyfin:8096").rstrip("/")
JELLYFIN_KEY = os.environ.get("JELLYFIN_API_KEY", "")
JELLYFIN_PUBLIC = os.environ.get("JELLYFIN_PUBLIC_URL", JELLYFIN).rstrip("/")
METUBE_PUBLIC = os.environ.get("METUBE_PUBLIC_URL", METUBE).rstrip("/")
MUSIC_PATH = os.environ.get("JELLYFIN_MUSIC_PATH", "/data/media/music/YouTube").rstrip("/")
MUSIC_DIR = os.environ.get("MUSIC_DIR", "/music")     # the same download folder, mounted here so the artist tag can be written
DATA_FILE = os.environ.get("DATA_FILE", "/data/requests.json")
PORT = int(os.environ.get("PORT", "8090"))
APP_DIR = os.path.dirname(os.path.abspath(__file__))

KEEP = 60                 # requests remembered
RESCAN_AFTER = 75         # seconds a finished file may wait before Jellyfin is asked to scan
RESCAN_EVERY = 180        # at most one forced scan per this many seconds

lock = threading.Lock()
requests_ = []            # newest first
cache = {"hist": (0, None), "jf": (0, None), "rescan": 0}


# ---------------------------------------------------------------- storage
SEEN_FILE = os.path.join(os.path.dirname(DATA_FILE), "seen.json")
seen = {}                 # link key -> file path in Jellyfin, for every song this page has seen arrive


def load():
    global requests_
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            requests_ = json.load(f)
    except (OSError, ValueError):
        requests_ = []
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            seen.update(json.load(f))
    except (OSError, ValueError):
        pass


def remember(key, path):
    if not path or seen.get(key) == path:
        return
    seen[key] = path
    tmp = SEEN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(seen, f)
    os.replace(tmp, SEEN_FILE)


def jf_item(j):
    return {
        "item_id": j["Id"],
        "name": j.get("Name"),
        "artist": j.get("AlbumArtist") or ", ".join(j.get("Artists") or []),
        "cover_id": j.get("AlbumId") if j.get("AlbumPrimaryImageTag") else (j["Id"] if (j.get("ImageTags") or {}).get("Primary") else None),
    }


def save():
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(requests_[:KEEP], f)
    os.replace(tmp, DATA_FILE)


# ---------------------------------------------------------------- http helpers
def call(url, body=None, headers=None, timeout=20, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw.strip() else {}


def jf_headers():
    return {"Authorization": 'MediaBrowser Token="%s"' % JELLYFIN_KEY}


# ---------------------------------------------------------------- link handling
YT_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be")


def normalize(text):
    """Pick the first link out of pasted/shared text. For YouTube, keep only the
    video id so a link shared from inside a mix or playlist queues one song."""
    m = re.search(r"https?://[^\s<>\"']+", text or "")
    if not m:
        return None
    url = m.group(0).rstrip(").,")
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or "").lower()
    if host not in YT_HOSTS:
        return {"kind": "song", "vid": None, "url": url, "key": url}
    qs = urllib.parse.parse_qs(p.query)
    vid = None
    if host == "youtu.be":
        vid = p.path.strip("/").split("/")[0]
    elif p.path == "/watch":
        vid = (qs.get("v") or [None])[0]
    else:
        mm = re.match(r"^/(shorts|live|embed)/([\w-]{6,})", p.path)
        if mm:
            vid = mm.group(2)
    if vid and re.fullmatch(r"[\w-]{6,20}", vid):
        site = "music.youtube.com" if host == "music.youtube.com" else "www.youtube.com"
        return {"kind": "song", "vid": vid, "url": "https://%s/watch?v=%s" % (site, vid), "key": "yt:" + vid}
    if qs.get("list") or p.path.startswith("/playlist"):
        return {"kind": "playlist", "vid": None, "url": url, "key": url}
    return {"kind": "song", "vid": None, "url": url, "key": url}


# ---------------------------------------------------------------- MeTube / Jellyfin state
def metube_history():
    t, val = cache["hist"]
    if val is not None and time.time() - t < 2:
        return val
    val = call(METUBE + "/history", timeout=10)
    cache["hist"] = (time.time(), val)
    return val


def jellyfin_index():
    """path -> item, for every audio track Jellyfin knows."""
    t, val = cache["jf"]
    if val is not None and time.time() - t < 8:
        return val
    q = "Recursive=true&IncludeItemTypes=Audio&Fields=Path&Limit=50000&EnableTotalRecordCount=false"
    items = call(JELLYFIN + "/Items?" + q, headers=jf_headers(), timeout=30).get("Items", [])
    val = {i.get("Path"): i for i in items if i.get("Path")}
    cache["jf"] = (time.time(), val)
    return val


def jellyfin_rescan():
    if time.time() - cache["rescan"] < RESCAN_EVERY:
        return
    cache["rescan"] = time.time()
    try:
        call(JELLYFIN + "/Library/Refresh", headers=jf_headers(), method="POST", timeout=10)
    except Exception:
        pass


def find_in_metube(hist, r):
    best = None
    for bucket in ("queue", "pending", "done"):
        for it in hist.get(bucket, []):
            same = (r.get("vid") and it.get("id") == r["vid"]) or it.get("url") == r["url"]
            if same and (best is None or (it.get("timestamp") or 0) >= (best.get("timestamp") or 0)):
                best = it
    return best


def describe(r, hist, need_jf):
    """Return the row the page shows for one request."""
    out = {"key": r["key"], "url": r["url"], "added": r["added"], "title": r.get("title") or r["url"],
           "playlist": r.get("playlist") if r.get("playlist_id") else None, "playlisted": bool(r.get("playlisted")),
           "want_artist": r.get("artist")}
    if r.get("tag_error") or r.get("playlist_error"):
        out["note"] = r.get("tag_error") or ("not added to the playlist: " + r["playlist_error"])
    if r.get("item"):                                   # already confirmed in Jellyfin
        out.update(state="ready", **r["item"])
        return out
    if r["kind"] == "playlist":
        out.update(state="playlist")
        return out
    it = find_in_metube(hist, r) if hist is not None else None
    if hist is None:
        out.update(state="unknown", note="MeTube is not answering")
        return out
    if it is None:
        fresh = time.time() - r["added"] < 180
        out.update(state="queued" if fresh else "unknown", note="" if fresh else "Not in MeTube's list any more")
        return out
    if it.get("title") and r.get("title") != it["title"]:
        r["title"] = it["title"]
        r["_dirty"] = True
    out["title"] = r.get("title") or out["title"]
    status = it.get("status")
    if status == "error" or it.get("error"):
        out.update(state="failed", note=str(it.get("msg") or it.get("error") or "Download failed")[:200])
        return out
    if status != "finished":
        pct = it.get("percent")
        out.update(state="downloading" if pct else "queued", percent=round(pct) if pct else None)
        return out
    filename = it.get("filename")
    if not r.get("saved_at"):
        r["saved_at"] = time.time()
        r["_dirty"] = True
    out.update(state="saved")
    if filename and r.get("artist") and not r.get("tagged"):
        err = write_artist(filename, r["artist"])
        r["tagged"], r["_dirty"] = True, True
        if err:
            r["tag_error"] = err
        cache["rescan"] = 0                         # the file changed: have Jellyfin read it again now
        jellyfin_rescan()
    if filename:
        need_jf.append((r, out, MUSIC_PATH + "/" + filename))
    return out


def items():
    try:
        hist = metube_history()
    except Exception:
        hist = None
    with lock:
        need_jf, rows = [], []
        for r in requests_[:KEEP]:
            rows.append(describe(r, hist, need_jf))
        if need_jf:
            try:
                idx = jellyfin_index()
            except Exception:
                idx = None
            for r, out, path in need_jf:
                j = idx.get(path) if idx else None
                want = r.get("artist") if not r.get("tag_error") else None
                waited = time.time() - r.get("saved_at", time.time())
                if j and want and want not in (j.get("Artists") or []) and waited < 420:
                    out["note"] = "Jellyfin is picking up the artist"      # it indexed the file before the artist was written
                    if waited > RESCAN_AFTER:
                        jellyfin_rescan()
                elif j:
                    r["item"] = jf_item(j)
                    remember(r["key"], path)
                    r["_dirty"] = True
                    if r.get("playlist_id") and not r.get("playlisted"):
                        try:
                            playlist_add(r["playlist_id"], j["Id"])
                            r["playlisted"] = True
                        except Exception as e:
                            r["playlist_error"] = str(e)[:120]
                    out.update(state="ready", **r["item"])
                elif idx is None:
                    out["note"] = "Jellyfin is not answering"
                elif time.time() - r.get("saved_at", time.time()) > RESCAN_AFTER:
                    jellyfin_rescan()
        if [r for r in requests_ if r.pop("_dirty", False)]:
            save()
    return {"items": rows, "jellyfin": JELLYFIN_PUBLIC, "metube": METUBE_PUBLIC}


# ---------------------------------------------------------------- artist and playlist
def jf_user():
    if not cache.get("user"):
        users = call(JELLYFIN + "/Users", headers=jf_headers(), timeout=15)
        admins = [u for u in users if (u.get("Policy") or {}).get("IsAdministrator")]
        cache["user"] = os.environ.get("JELLYFIN_USER_ID") or (admins or users)[0]["Id"]
    return cache["user"]


def playlists():
    """[{id, name}] of the Jellyfin playlists, plus artist -> playlist ids (who is already in which)."""
    t, val = cache.get("pl", (0, None))
    if val is not None and time.time() - t < 45:
        return val
    u = jf_user()
    pls = call(JELLYFIN + "/Items?Recursive=true&IncludeItemTypes=Playlist&SortBy=SortName&UserId=" + u, headers=jf_headers(), timeout=20).get("Items", [])
    out, by_artist = [], {}
    for p in pls:
        out.append({"id": p["Id"], "name": p["Name"]})
        for it in call(JELLYFIN + "/Playlists/%s/Items?UserId=%s" % (p["Id"], u), headers=jf_headers(), timeout=30).get("Items", []):
            for a in (it.get("Artists") or []) + [it.get("AlbumArtist") or ""]:
                if a:
                    by_artist.setdefault(a.strip().lower(), set()).add(p["Id"])
    val = (out, by_artist)
    cache["pl"] = (time.time(), val)
    return val


def playlist_add(pid, item_id):
    u = jf_user()
    have = call(JELLYFIN + "/Playlists/%s/Items?UserId=%s" % (pid, u), headers=jf_headers(), timeout=30).get("Items", [])
    if not any(i["Id"] == item_id for i in have):
        call(JELLYFIN + "/Playlists/%s/Items?UserId=%s&Ids=%s" % (pid, u, item_id), headers=jf_headers(), method="POST", timeout=20)
    cache["pl"] = (0, None)


CHANNEL_JUNK = re.compile(r"(\s*-\s*topic|vevo|\bofficial\b)", re.I)


def guess_artist(title, author):
    """YouTube titles are usually 'Artist - Song (Official Video)'; otherwise fall back to the channel name."""
    title, author = (title or "").strip(), (author or "").strip()
    clean_author = re.sub(r"\s{2,}", " ", CHANNEL_JUNK.sub("", author)).strip(" -|") or author
    head = re.split(r"\s[-–—]\s", title, maxsplit=1)
    cand = head[0] if len(head) == 2 else ""
    if cand and re.match(r"\W*(official|lyric|lyrical|full|video|audio|hd\b|4k\b|from\b)", head[1], re.I):
        cand = ""                                  # 'Song - Lyric Video': the part before the dash is the song, not the artist
    if not cand:                                   # no dash: 'Artist | Song' only counts when it matches the channel
        first = re.split(r"\s*[|｜]\s*", title)[0]
        if first and first.lower() in (author.lower(), clean_author.lower()):
            cand = first
    cand = re.split(r"\s*[|｜]\s*", cand)[0].strip(" \"'“”「」『』[]")
    if cand and len(cand) <= 40 and not re.search(r"[(\[]|official|lyric|video|audio|full song", cand, re.I):
        return cand
    return clean_author


HINDI_WORDS = re.compile(r"hindi|punjabi|bollywood|t-series|tips official|zee music|saregama|sony music india|\byrf\b|speed records|desi|ghazal|bhangra|qawwali", re.I)


def suggest_playlist(title, author, artist, pls, by_artist):
    """A playlist id, or None when there is nothing to go on."""
    def named(word):
        return next((p["id"] for p in pls if word in p["name"].lower()), None)
    known = by_artist.get((artist or "").strip().lower()) or by_artist.get((author or "").strip().lower())
    if known and len(known) == 1:                  # this artist's songs already sit in exactly one playlist
        return next(iter(known))
    text = "%s %s" % (title or "", author or "")
    if re.search(r"[぀-ヿ一-鿿]", text):
        return named("japan")
    if re.search(r"[ऀ-ॿ਀-੿]", text) or HINDI_WORDS.search(text):
        return named("hindi")
    return None


def preview(text):
    n = normalize(text)
    if not n:
        return 400, {"error": "That does not look like a link."}
    title = author = thumb = ""
    if n["vid"]:
        try:
            req = urllib.request.Request("https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote("https://www.youtube.com/watch?v=" + n["vid"], safe=""),
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                o = json.loads(r.read())
            title, author, thumb = o.get("title", ""), o.get("author_name", ""), o.get("thumbnail_url", "")
        except Exception:
            pass
    try:
        pls, by_artist = playlists()
    except Exception:
        pls, by_artist = [], {}
    artist = guess_artist(title, author) if title else ""
    dup, _ = duplicate_of(n)
    return 200, {"ok": True, "kind": n["kind"], "url": n["url"], "title": title, "channel": author, "thumb": thumb,
                 "artist": artist, "playlists": pls, "playlist_id": suggest_playlist(title, author, artist, pls, by_artist),
                 "duplicate": dup}


def write_artist(filename, artist):
    """Put the chosen artist into the downloaded file's tags. Returns an error text or None."""
    path = os.path.join(MUSIC_DIR, filename)
    try:
        from mutagen.mp4 import MP4
        if not path.lower().endswith((".m4a", ".mp4")):
            return "artist not written: not an M4A file"
        f = MP4(path)
        f["\xa9ART"] = [artist]
        f["aART"] = [artist]
        f.save()
        return None
    except Exception as e:
        return "artist not written: %s" % e


def duplicate_of(n):
    """Is this link already downloading, or already a file Jellyfin has?
    Returns ("downloading", None), ("library", jellyfin_item) or (None, None)."""
    if n["kind"] != "song":
        return None, None
    try:
        it = find_in_metube(metube_history(), n)
    except Exception:
        it = None
    if it and it.get("status") not in ("finished", "error") and not it.get("error"):
        return "downloading", None
    path = None
    if it and it.get("status") == "finished" and it.get("filename"):
        path = MUSIC_PATH + "/" + it["filename"]
    elif n["key"] in seen:                       # MeTube's own list was cleared; we still remember the file
        path = seen[n["key"]]
    if path:
        try:
            j = jellyfin_index().get(path)       # gone from the library (deleted) -> allowed to download again
        except Exception:
            j = None
        if j:
            return "library", j
    return None, None


def add(text, artist="", playlist_id=""):
    n = normalize(text)
    if not n:
        return 400, {"error": "That does not look like a link. Paste the link from the Share button."}
    artist = re.sub(r"\s+", " ", artist or "").strip()[:120]
    playlist_name = ""
    if playlist_id:
        try:
            playlist_name = next((p["name"] for p in playlists()[0] if p["id"] == playlist_id), "")
        except Exception:
            pass
        if not playlist_name:
            playlist_id = ""
    dup, j = duplicate_of(n)
    if not dup:
        resp = call(METUBE + "/add", body={"url": n["url"], "download_type": "audio", "format": "m4a", "quality": "best"}, timeout=120)
        if resp.get("status") != "ok":
            return 502, {"error": str(resp.get("msg") or "MeTube refused the link")[:300]}
        cache["hist"] = (0, None)
    with lock:
        old = next((r for r in requests_ if r["key"] == n["key"]), {})
        requests_[:] = [r for r in requests_ if r["key"] != n["key"]]
        r = {"key": n["key"], "url": n["url"], "vid": n["vid"], "kind": n["kind"], "added": time.time()}
        if old.get("title"):
            r["title"] = old["title"]
        if n["kind"] == "song":
            if artist and dup != "library":          # a song already in the library keeps the artist it has
                r["artist"] = artist
            if playlist_id:
                r["playlist_id"], r["playlist"] = playlist_id, playlist_name
        if dup == "library":
            r["item"] = jf_item(j)
            remember(n["key"], j.get("Path"))
            if playlist_id:
                try:
                    playlist_add(playlist_id, j["Id"])
                    r["playlisted"] = True
                except Exception:
                    pass
        requests_.insert(0, r)
        save()
    return 200, {"ok": True, "key": n["key"], "kind": n["kind"], "duplicate": dup}


def remove(key):
    with lock:
        requests_[:] = [r for r in requests_ if r["key"] != key]
        save()
    return 200, {"ok": True}


# ---------------------------------------------------------------- icon (drawn in code, no image file needed)
def make_icon(size):
    bg, fg = (79, 70, 229), (255, 255, 255)
    rows = bytearray()
    for y in range(size):
        v = (y + 0.5) / size
        rows.append(0)
        for x in range(size):
            u = abs((x + 0.5) / size - 0.5)
            on = (u < 0.055 and 0.20 < v < 0.56) or (0.44 < v < 0.66 and u < (0.66 - v) * 0.95) or (0.75 < v < 0.83 and u < 0.27)
            rows.extend(fg if on else bg)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))


ICONS = {192: make_icon(192), 512: make_icon(512), 180: make_icon(180)}
MANIFEST = json.dumps({
    "name": "Add a song", "short_name": "Add song", "start_url": "/", "display": "standalone",
    "background_color": "#101014", "theme_color": "#4f46e5",
    "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
              {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}],
}).encode()


# ---------------------------------------------------------------- web server
class Handler(BaseHTTPRequestHandler):
    server_version = "songdrop"

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/items"):
            super().log_message(fmt, *args)

    def send(self, code, body, ctype="application/json", cache_ctl="no-store"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_ctl)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/":
            with open(os.path.join(APP_DIR, "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/items":
            return self.send(200, items())
        m = re.fullmatch(r"/api/cover/([0-9a-f]{32})", path)
        if m:                                        # cover art, fetched from Jellyfin on the phone's behalf
            try:
                req = urllib.request.Request(JELLYFIN + "/Items/%s/Images/Primary?maxHeight=160&quality=80" % m.group(1), headers=jf_headers())
                with urllib.request.urlopen(req, timeout=15) as r:
                    return self.send(200, r.read(), r.headers.get("Content-Type", "image/jpeg"), "max-age=86400")
            except Exception:
                return self.send(404, {"error": "no cover"})
        if path == "/manifest.webmanifest":
            return self.send(200, MANIFEST, "application/manifest+json", "max-age=3600")
        if path in ("/icon-192.png", "/icon-512.png", "/apple-touch-icon.png", "/favicon.ico"):
            size = 512 if "512" in path else 180 if "apple" in path else 192
            return self.send(200, ICONS[size], "image/png", "max-age=86400")
        self.send(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 8192)
            body = json.loads(self.rfile.read(length) or b"{}")
            if path == "/api/add":
                code, out = add(str(body.get("text") or ""), str(body.get("artist") or ""), str(body.get("playlist_id") or ""))
            elif path == "/api/preview":
                code, out = preview(str(body.get("text") or ""))
            elif path == "/api/remove":
                code, out = remove(str(body.get("key") or ""))
            else:
                code, out = 404, {"error": "not found"}
        except ValueError:
            code, out = 400, {"error": "bad request"}
        except Exception as e:                       # MeTube down, timeout, ...
            code, out = 502, {"error": "Could not reach MeTube: %s" % e}
        self.send(code, out)


if __name__ == "__main__":
    load()
    print("songdrop on :%d  metube=%s  jellyfin=%s  key=%s" % (PORT, METUBE, JELLYFIN, "set" if JELLYFIN_KEY else "MISSING"), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
