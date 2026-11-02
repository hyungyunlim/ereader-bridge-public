"""Audiobook <-> EPUB position sync (Audiobookshelf <-> Kavita).

A short clip of the audiobook is transcribed by a whisper.cpp server and looked up in the EPUB text,
but only near where the narration should be: narration pace is even enough that the time fraction is
within ~1% of the text fraction, and searching just that stretch keeps speech-recognition quirks
(names spelled by sound, numbers as digits) from sending the match elsewhere.
  audio -> book: transcribe at the listening position, find it, offer the paragraphs (like paper sync).
  book -> audio: guess the time from the text position, transcribe there, correct by the character
                 gap at the local pace, repeat until the clip starts at the paragraph.
Every successful match is kept as an anchor (time, char offset), so later guesses start closer.

Auto mode (per book): poll both positions every minute; once neither has moved for IDLE seconds (a reading
or listening session ended, short pauses don't count), carry the side that moved last over to the other,
forward only. Nothing is transcribed while you read or listen, and only once per session after.
"""
import bisect
import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import textsync as T

log = logging.getLogger("audiosync")

ABS_URL = os.environ.get("AUDIOBOOKSHELF_URL", "").rstrip("/")
ABS_TOKEN = os.environ.get("AUDIOBOOKSHELF_TOKEN", "")
WHISPER_URL = os.environ.get("WHISPER_URL", "http://host.docker.internal:8178").rstrip("/")
CLIP_SECONDS = 30
SPAN = 0.06       # share of the book searched on each side of the expected position
MIN_SCORE = float(os.environ.get("AUDIOSYNC_MIN_SCORE", "0.3"))  # share of the clip's n-grams found
CLOSE_ENOUGH = 60  # chars (~15 s of narration) between the clip's start and the target paragraph
IDLE = float(os.environ.get("AUDIOSYNC_IDLE_SECONDS", "300"))  # auto mode: a side is done after this long unmoved
AUTO_POLL = 60


def hms(s):
    s = int(round(s))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def enabled():
    return bool(ABS_URL and ABS_TOKEN and T.enabled())


# ---------------------------------------------------------------- Audiobookshelf

class Abs:
    def __init__(self, url=ABS_URL, token=ABS_TOKEN):
        self.url, self.token = url, token
        self.cache = (0.0, [])

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw and raw[:1] in b"{[" else None

    def books(self):
        """Every audiobook: [{id, title, duration}] (cached for an hour)."""
        if time.time() - self.cache[0] < 3600:
            return self.cache[1]
        out = []
        for lib in (self._req("GET", "/api/libraries") or {}).get("libraries", []):
            if lib.get("mediaType") != "book":
                continue
            for it in (self._req("GET", f"/api/libraries/{lib['id']}/items?limit=0") or {}).get("results", []):
                m = it.get("media", {})
                if m.get("duration"):
                    out.append({"id": it["id"], "title": m.get("metadata", {}).get("title", ""), "duration": m["duration"]})
        self.cache = (time.time(), out)
        return out

    def in_progress(self):
        return [it["id"] for it in (self._req("GET", "/api/me/items-in-progress") or {}).get("libraryItems", [])]

    def item(self, item_id):
        """{title, duration, files: [(path, start, duration)]} with paths as Audiobookshelf sees them."""
        it = self._req("GET", f"/api/items/{item_id}?expanded=1")
        m = it["media"]
        files, start = [], 0.0
        for f in sorted(m.get("audioFiles", []), key=lambda f: f.get("index", 0)):
            if f.get("exclude"):
                continue
            files.append((f["metadata"]["path"], start, f["duration"]))
            start += f["duration"]
        return {"title": m.get("metadata", {}).get("title", ""), "duration": m.get("duration") or start, "files": files}

    def progress(self, item_id):
        """Listening position in seconds (0 if never played)."""
        try:
            return float((self._req("GET", f"/api/me/progress/{item_id}") or {}).get("currentTime") or 0)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 0.0
            raise

    def set_progress(self, item_id, seconds, duration):
        self._req("PATCH", f"/api/me/progress/{item_id}",
                  {"currentTime": seconds, "duration": duration, "progress": min(1.0, seconds / duration)})


# ---------------------------------------------------------------- speech -> text

def transcribe(files, seconds, length, language):
    """Text spoken in [seconds, seconds+length) of the audiobook: ffmpeg cuts the clip, whisper.cpp transcribes it."""
    path, start, _ = next((f for f in reversed(files) if f[1] <= seconds), files[0])
    wav = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{seconds - start:.2f}", "-t", str(length), "-i", path,
                          "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1"], capture_output=True, check=True, timeout=60).stdout
    boundary = uuid.uuid4().hex
    parts = [(b'name="file"; filename="clip.wav"', b"Content-Type: audio/wav\r\n", wav),
             (b'name="response_format"', b"", b"json"), (b'name="language"', b"", language.encode())]
    body = b"".join(b"--" + boundary.encode() + b"\r\nContent-Disposition: form-data; " + d + b"\r\n" + h + b"\r\n" + v + b"\r\n"
                    for d, h, v in parts) + b"--" + boundary.encode() + b"--\r\n"
    req = urllib.request.Request(WHISPER_URL + "/inference", data=body, method="POST",
                                 headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read()).get("text", "")


# ---------------------------------------------------------------- the EPUB as one string

class Text:
    """An EPUB index flattened to one normalised string, with each paragraph's char offset."""
    def __init__(self, idx):
        self.idx = idx
        self.paras = [(spine, p, len(tn), h) for spine, d in enumerate(idx["docs"]) for p, tn, h in d["paras"]]
        self.offs, o = [], 0
        for _, _, n, _ in self.paras:
            self.offs.append(o)
            o += n
        self.total = o or 1
        self.book = "".join(tn for d in idx["docs"] for _, tn, _ in d["paras"])
        sample = self.book[len(self.book) // 3:len(self.book) // 3 + 2000]
        self.language = "ko" if sum("가" <= c <= "힣" for c in sample) > len(sample) * 0.3 else "auto"

    def offset(self, spine, path):
        """Char offset of a Kavita position; a BookScrollId we didn't index falls back to its chapter's start."""
        first = None
        for i, (s, p, _, _) in enumerate(self.paras):
            if s == spine:
                if p == path:
                    return self.offs[i]
                first = self.offs[i] if first is None else first
        return first or 0

    def at(self, offset):
        """Index of the paragraph containing `offset` (spacers skipped)."""
        i = max(0, bisect.bisect_right(self.offs, offset) - 1)
        while i + 1 < len(self.paras) and self.paras[i][2] == 0:
            i += 1
        return i

    def find(self, spoken, center, span):
        """(score, char offset where `spoken` starts) searching only [center-span, center+span]."""
        want = T.norm(spoken)
        n = T._gram_size(want)
        wg = T._grams(want, n)
        if len(want) < 30 or not wg:
            return 0.0, None
        lo, hi = max(0, int(center - span)), min(self.total, int(center + span))
        best = (0.0, None)
        step = max(1, len(want) // 8)
        for start in range(lo, max(lo + 1, hi - len(want) // 2), step):   # stretch of book text the clip's length
            sc = len(wg & T._grams(self.book[start:start + int(len(want) * 1.1)], n)) / len(wg)
            if sc > best[0]:
                best = (sc, start)
        if best[1] is None:
            return best
        # exact start: where the clip's distinctive 12-char pieces sit in the book (median), else the stretch's start
        region = self.book[max(0, best[1] - len(want)):best[1] + 2 * len(want)]
        base = max(0, best[1] - len(want))
        hits = [base + region.index(want[k:k + 12]) - k for k in range(0, len(want) - 12, 6)
                if region.count(want[k:k + 12]) == 1]
        return round(best[0], 2), sorted(hits)[len(hits) // 2] if hits else best[1]


# ---------------------------------------------------------------- the feature

class AudioSync:
    def __init__(self, textsync, data_dir, abs=None, transcriber=transcribe):
        self.ts, self.abs, self.transcribe = textsync, abs or Abs(), transcriber
        self.file = Path(data_dir) / "audio_anchors.json"
        try:
            self.anchors = json.loads(self.file.read_text())
        except (OSError, ValueError):
            self.anchors = {}
        self.lock = threading.Lock()
        self.auto_file = Path(data_dir) / "audio_auto.json"
        try:
            self.auto = json.loads(self.auto_file.read_text())  # chapterId -> auto-mode state
        except (OSError, ValueError):
            self.auto = {}

    # anchors: (seconds, char offset) pairs per (book, audiobook); the ends of the book are implied
    def _points(self, key, duration, total):
        return [(0.0, 0)] + sorted(tuple(a) for a in self.anchors.get(key, [])) + [(duration, total)]

    def _expected_offset(self, key, seconds, duration, total):
        pts = self._points(key, duration, total)
        i = max(1, min(len(pts) - 1, bisect.bisect_left([p[0] for p in pts], seconds)))
        (t0, o0), (t1, o1) = pts[i - 1], pts[i]
        return o0 + (o1 - o0) * (seconds - t0) / max(1e-6, t1 - t0)

    def _expected_time(self, key, offset, duration, total):
        """(seconds where the narration reaches `offset`, local pace in chars/s)."""
        pts = sorted(self._points(key, duration, total), key=lambda p: p[1])
        i = max(1, min(len(pts) - 1, bisect.bisect_left([p[1] for p in pts], offset)))
        (t0, o0), (t1, o1) = pts[i - 1], pts[i]
        # pace over >= 5 minutes of anchors: each anchor is off by a few dozen chars, so neighbours a minute
        # apart can give a pace 30% wrong
        j, k = i - 1, i
        while pts[k][0] - pts[j][0] < 300 and (j > 0 or k < len(pts) - 1):
            j, k = max(0, j - 1), min(len(pts) - 1, k + 1)
        pace = (pts[k][1] - pts[j][1]) / max(1e-6, pts[k][0] - pts[j][0])
        return t0 + (t1 - t0) * (offset - o0) / max(1, o1 - o0), max(0.5, pace)

    def _anchor(self, key, seconds, offset):
        with self.lock:
            pts = [a for a in self.anchors.get(key, []) if abs(a[0] - seconds) > 60]  # one anchor per minute
            self.anchors[key] = sorted(pts + [[round(seconds, 1), offset]])[-200:]
            self.file.write_text(json.dumps(self.anchors))

    def _setup(self, chapter_id, item_id):
        book = self.ts.kavita.book(chapter_id)
        if not book:
            raise KeyError(f"unknown chapter {chapter_id}")
        return book, Text(self.ts.index.get(book["path"])), self.abs.item(item_id), f"{chapter_id}:{item_id}"

    def _hear(self, item, text, key, seconds):
        """(score, char offset) of what is spoken at `seconds`."""
        seconds = max(0.0, min(seconds, item["duration"] - CLIP_SECONDS))
        spoken = self.transcribe(item["files"], seconds, CLIP_SECONDS, text.language)
        score, start = text.find(spoken, self._expected_offset(key, seconds, item["duration"], text.total), SPAN * text.total)
        if score >= MIN_SCORE:
            self._anchor(key, seconds, start)
        return score, start, spoken

    def pairs(self):
        """Books you are reading (Kavita) or listening to (Audiobookshelf) that exist in both, with both positions."""
        by_title = {T.norm(a["title"]): a for a in self.abs.books()}
        found = {}
        for b in self.ts.kavita.recent():
            a = by_title.get(T.norm(b["title"]))
            if a:
                found[b["chapterId"]] = (b, a)
        listening = set(self.abs.in_progress())
        if listening - {a["id"] for _, a in found.values()}:
            kav = {T.norm(b["title"]): b for b in self.ts.kavita.all()}
            for a in self.abs.books():
                b = kav.get(T.norm(a["title"]))
                if a["id"] in listening and b and b["chapterId"] not in found:
                    found[b["chapterId"]] = (b, a)
        out = []
        for b, a in found.values():
            try:
                idx = self.ts.index.get(b["path"])
                page, scroll = self.ts.kavita.progress(b["chapterId"])
                where = self.ts._where(b, idx, page, scroll)
                s = self.auto.get(str(b["chapterId"]), {})
                out.append({"chapterId": b["chapterId"], "title": b["title"], "book": where,
                            "auto": {"on": bool(s), "last": s.get("last")},
                            "audio": {"itemId": a["id"], "title": a["title"], "duration": a["duration"],
                                      "time": self.abs.progress(a["id"])}})
            except Exception as e:
                log.warning("audiosync: %s: %s", b["title"], e)
        return out

    def to_book(self, chapter_id, item_id):
        """Where the listening position is in the EPUB: same answer shape as TextSync.locate (paragraph picker)."""
        book, text, item, key = self._setup(chapter_id, item_id)
        seconds = self.abs.progress(item_id)
        score, start, spoken = self._hear(item, text, key, seconds)
        if score < MIN_SCORE:
            return {"found": False, "reason": "This part of the audiobook was not found in the book."}
        idx = text.idx
        first, last = text.at(start), text.at(start + len(T.norm(spoken)))
        spine, path = text.paras[first][:2]
        target = self.ts._where(book, idx, spine, path)
        target["paras"] = [{k: w[k] for k in ("bookScrollId", "percent", "head")}
                           for w in (self.ts._where(book, idx, s, p) for s, p, n, _ in text.paras[first:last + 1]
                                     if n and s == spine)]
        current = self.ts._where(book, idx, *self.ts.kavita.progress(chapter_id))
        return {"found": True, "searched": "audio", "score": score, "book": {"chapterId": chapter_id, "title": book["title"]},
                "audio": {"itemId": item_id, "time": seconds}, "target": target, "current": current,
                "backward": target["percent"] < current["percent"], "others": []}

    def to_audio(self, chapter_id, item_id):
        """The audiobook time where the narrator reaches Kavita's position."""
        book, text, item, key = self._setup(chapter_id, item_id)
        page, scroll = self.ts.kavita.progress(chapter_id)
        target = text.offset(page, scroll)
        seconds, rate = self._expected_time(key, target, item["duration"], text.total)
        for _ in range(5):
            score, start, spoken = self._hear(item, text, key, seconds)
            if score < MIN_SCORE:
                return {"found": False, "reason": "This position was not found in the audiobook."}
            gap = target - start
            seconds = max(0.0, min(item["duration"], seconds + gap / rate))
            if abs(gap) <= CLOSE_ENOUGH:
                break
            rate = self._expected_time(key, target, item["duration"], text.total)[1]  # local pace from the anchors
        else:
            return {"found": False, "reason": "The audiobook position did not converge."}
        where = self.ts._where(book, text.idx, page, scroll)
        current = self.abs.progress(item_id)
        return {"found": True, "book": {"chapterId": chapter_id, "title": book["title"], **where},
                "audio": {"itemId": item_id, "title": item["title"], "time": round(seconds, 1),
                          "current": current, "duration": item["duration"]},
                "backward": seconds < current}

    def apply_audio(self, item_id, seconds):
        item = self.abs.item(item_id)
        self.abs.set_progress(item_id, float(seconds), item["duration"])
        return {"ok": True, "title": item["title"], "time": float(seconds)}

    # ---------------------------------------------------------------- auto mode

    def set_auto(self, chapter_id, item_id, on):
        """Turn auto mode on/off for a book. Turning it on takes the current positions as already in step."""
        with self.lock:
            if on:
                book, audio, now = list(self.ts.kavita.progress(chapter_id)), self.abs.progress(item_id), time.time()
                self.auto[str(chapter_id)] = {"itemId": item_id, "book": book, "book_at": now, "audio": audio,
                                              "audio_at": now, "done_book": book, "done_audio": audio, "last": None}
            else:
                self.auto.pop(str(chapter_id), None)
            self.auto_file.write_text(json.dumps(self.auto, ensure_ascii=False))
        return {"chapterId": chapter_id, "on": on}

    def auto_loop(self):
        while True:
            time.sleep(AUTO_POLL)
            try:
                self.auto_tick()
            except Exception:
                log.exception("audiosync auto")

    def auto_tick(self, now=None):
        now = now or time.time()
        for cid, s in list(self.auto.items()):
            if now < s.get("retry_at", 0):
                continue
            try:
                self._auto_one(int(cid), s, now)
            except Exception as e:  # Kavita/ABS/whisper down: try again after IDLE, not every minute
                log.warning("audiosync auto %s: %s", cid, e)
                s["last"], s["retry_at"] = {"at": now, "msg": f"Error: {e}"}, now + IDLE
        with self.lock:
            self.auto_file.write_text(json.dumps(self.auto, ensure_ascii=False))

    def _auto_one(self, cid, s, now):
        book, audio = list(self.ts.kavita.progress(cid)), self.abs.progress(s["itemId"])
        if book != s["book"]:
            s["book"], s["book_at"] = book, now
        if abs(audio - s["audio"]) > 1:
            s["audio"], s["audio_at"] = audio, now
        book_new, audio_new = book != s["done_book"], abs(audio - s["done_audio"]) > 1
        if now - max(s["book_at"], s["audio_at"]) < IDLE or not (book_new or audio_new):
            return  # still reading/listening (or paused briefly), or nothing new
        if book_new and (not audio_new or s["book_at"] >= s["audio_at"]):
            msg = self._auto_to_audio(cid, s["itemId"], book, audio)
        else:
            msg = self._auto_to_book(cid, s["itemId"], book, audio)
        # whatever both sides say now (including our own write) is the new "in step": no echo back
        s["book"] = s["done_book"] = list(self.ts.kavita.progress(cid))
        s["audio"] = s["done_audio"] = self.abs.progress(s["itemId"])
        s["last"] = {"at": now, "msg": msg}
        log.info("audiosync auto %s: %s", cid, msg)

    def _anchored(self, key, i, value, scale):
        """True if an anchor sits within 2% of `value` (i: 0 = seconds, 1 = char offset): estimates there are exact."""
        return any(abs(a[i] - value) < 0.02 * scale for a in self.anchors.get(key, []))

    def _auto_to_audio(self, cid, item_id, book, audio):
        _, text, item, key = self._setup(cid, item_id)
        target = text.offset(*book)
        est = self._expected_time(key, target, item["duration"], text.total)[0]
        if self._anchored(key, 1, target, text.total) and abs(est - audio) < 60:
            return "Already in step (checked without transcribing)"
        r = self.to_audio(cid, item_id)
        if not r["found"]:
            return r["reason"]
        t = r["audio"]["time"]
        if t <= audio + 30:
            return f"Audiobook ({hms(audio)}) is already at or past the book position; left as is"
        self.apply_audio(item_id, t)
        return f"📖→🎧 Audiobook moved to {hms(t)}"

    def _auto_to_book(self, cid, item_id, book, audio):
        _, text, item, key = self._setup(cid, item_id)
        current = text.offset(*book)
        est = self._expected_offset(key, audio, item["duration"], text.total)
        if self._anchored(key, 0, audio, item["duration"]) and abs(est - current) < 300:
            return "Already in step (checked without transcribing)"
        r = self.to_book(cid, item_id)
        if not r["found"]:
            return r["reason"]
        t = r["target"]
        if text.offset(t["pageNum"], t["bookScrollId"]) <= current + 200:
            return f"Book ({r['current']['percent']}%) is already at or past the audiobook position; left as is"
        self.ts.apply(cid, t["pageNum"], t["bookScrollId"])
        return f"🎧→📖 Book moved to {t['percent']}% (“{t['head']}…”)"
