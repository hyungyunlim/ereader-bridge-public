"""Paper -> EPUB position sync.

A photo of a printed page (OCR'd on the phone) is matched against the reader's EPUBs; the best
paragraph becomes Kavita's reading progress, and KOReader follows through its Kavita progress sync.

Matching: text is normalised (no spaces/punctuation), each paragraph scores the share of its 3-grams
(2-grams for short dialogue lines) found in the photo, and the best run of consecutive matching
paragraphs wins; its first paragraph is the position. Real pages score ~0.7-0.9, unrelated text ~0.
"""
import gzip
import hashlib
import json
import logging
import os
import posixpath
import re
import threading
import time
import urllib.error
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

log = logging.getLogger("textsync")

KAVITA_URL = os.environ.get("KAVITA_URL", "").rstrip("/")
KAVITA_API_KEY = os.environ.get("KAVITA_API_KEY", "")
MIN_SCORE = float(os.environ.get("TEXTSYNC_MIN_SCORE", "0.3"))
EPUB = 3  # Kavita MangaFormat.Epub
BLOCKS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre", "td"}
VOID = {"br", "img", "hr", "meta", "link", "input", "col", "area", "base", "wbr", "source"}
INDEX_VERSION = 2


def enabled():
    return bool(KAVITA_URL and KAVITA_API_KEY)


# ---------------------------------------------------------------- EPUB text index

class _Node:
    def __init__(self, tag, parent=None):
        self.tag, self.parent, self.children = tag, parent, []

    def elements(self):
        return [c for c in self.children if isinstance(c, _Node)]

    def text(self):
        return "".join(c.text() if isinstance(c, _Node) else c for c in self.children)


class _Tree(HTMLParser):
    """Lenient XHTML parser: EPUBs often use HTML entities (&nbsp;) that XML parsers reject."""
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.root = self.cur = _Node("#doc")
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        n = _Node(tag.lower(), self.cur)
        self.cur.children.append(n)
        if n.tag not in VOID:
            self.cur = n

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(_Node(tag.lower(), self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag.lower():
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def norm(s):
    return re.sub(r"[\W_]+", "", s).lower()  # OCR line breaks, spacing and quotes don't matter


def _paragraphs(root):
    """Leaf-most block elements under <body>, with Kavita's BookScrollId path (//body/p[3], //body/div[2]/p[1])."""
    def find_body(n):
        if n.tag == "body":
            return n
        for c in n.elements():
            b = find_body(c)
            if b:
                return b
        return None

    def walk(el, path):
        counts = {}
        for child in el.elements():
            counts[child.tag] = counts.get(child.tag, 0) + 1
            p = f"{path}/{child.tag}[{counts[child.tag]}]"
            if child.tag in BLOCKS and not _has_block(child):
                yield p, child.text()
            else:
                yield from walk(child, p)

    body = find_body(root)
    return list(walk(body, "//body")) if body else []


def _has_block(n):
    return any(c.tag in BLOCKS or _has_block(c) for c in n.elements())


def _toc_titles(z, opf_root, base, manifest):
    """href (no fragment) -> first TOC title pointing at it (EPUB3 nav or EPUB2 NCX)."""
    titles = {}
    nav = next((i for i in manifest.values() if "nav" in (i.get("properties") or "").split()), None)
    ncx = next((i for i in manifest.values() if i.get("media-type") == "application/x-dtbncx+xml"), None)
    try:
        if nav is not None:
            nav_path = posixpath.join(base, nav.get("href"))
            raw = z.read(nav_path).decode("utf-8", "replace")
            for href, label in re.findall(r'<a[^>]+href="([^"#]+)[^"]*"[^>]*>(.*?)</a>', raw, re.S):
                titles.setdefault(posixpath.normpath(posixpath.join(posixpath.dirname(nav_path), href)),
                                  re.sub(r"<[^>]+>|\s+", " ", label).strip())
        elif ncx is not None:
            ncx_path = posixpath.join(base, ncx.get("href"))
            x = ET.fromstring(z.read(ncx_path))
            for np in x.iter("{http://www.daisy.org/z3986/2005/ncx/}navPoint"):
                label = "".join(np.find(".//{*}text").itertext()).strip()
                src = np.find("{*}content").get("src").split("#")[0]
                titles.setdefault(posixpath.normpath(posixpath.join(posixpath.dirname(ncx_path), src)), label)
    except Exception as e:  # a broken TOC only costs us the section names
        log.debug("toc parse failed: %s", e)
    return titles


def build_index(epub_path):
    z = zipfile.ZipFile(epub_path)
    opf = ET.fromstring(z.read("META-INF/container.xml")).find(".//{*}rootfile").get("full-path")
    o = ET.fromstring(z.read(opf))
    base = posixpath.dirname(opf)
    manifest = {i.get("id"): i for i in o.find("{*}manifest")}
    titles = _toc_titles(z, o, base, manifest)
    docs, last_title = [], ""
    for ref in o.find("{*}spine"):
        href = posixpath.normpath(posixpath.join(base, manifest[ref.get("idref")].get("href")))
        last_title = titles.get(href, last_title)
        root = _Tree(z.read(href).decode("utf-8", "replace")).root
        # [path, normalised text, first words as printed] - the head is shown on the confirmation screen
        docs.append({"title": last_title, "paras": [[p, norm(t), " ".join(t.split())[:28]] for p, t in _paragraphs(root)]})
    return {"v": INDEX_VERSION, "docs": docs}


class Index:
    """EPUB -> paragraph index, cached on disk (gzip json) keyed by path + mtime + size."""
    def __init__(self, cache_dir):
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.mem = {}
        self.lock = threading.Lock()

    def get(self, epub_path):
        st = os.stat(epub_path)
        key = hashlib.sha1(f"{epub_path}|{st.st_mtime_ns}|{st.st_size}|{INDEX_VERSION}".encode()).hexdigest()
        with self.lock:
            if key in self.mem:
                return self.mem[key]
        f = self.dir / f"{key}.json.gz"
        try:
            idx = json.loads(gzip.decompress(f.read_bytes()))
        except (FileNotFoundError, ValueError, OSError):
            idx = build_index(epub_path)
            f.write_bytes(gzip.compress(json.dumps(idx, ensure_ascii=False).encode()))
        with self.lock:
            self.mem[key] = idx
        return idx


# ---------------------------------------------------------------- matching

def _grams(s, n):
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _gram_size(s):
    """3 characters for Hangul/CJK (one character ~ one syllable), 7 for alphabetic scripts,
    whose short n-grams ("the", "ing") are everywhere and would match any page."""
    wide = sum(1 for c in s if "\u3040" <= c <= "\u9fff" or "\uac00" <= c <= "\ud7a3")
    return 3 if wide > len(s) * 0.3 else 7


def best_run(idx, text):
    """(spine, path, score, paths) of the best run of consecutive paragraphs found in `text`, or None.
    paths: the run's non-empty paragraphs, i.e. the photographed page, for the user to pick where they stopped."""
    want_n = norm(text)
    n = _gram_size(want_n)
    want, want2 = _grams(want_n, n), _grams(want_n, 2)
    best = None
    for spine, doc in enumerate(idx["docs"]):
        paras = doc["paras"]
        seen = []
        for _, tn, _ in paras:
            g = _grams(tn, n)
            if not tn:
                seen.append(None)                                   # spacer paragraph: doesn't break a run
            elif len(g) < 8 and n == 3:                             # short dialogue line: 2-grams
                g2 = _grams(tn, 2)
                seen.append(len(g2 & want2) / len(g2) if len(g2) >= 2 else float(tn in want_n))
            elif len(g) < 8:                                        # short line, alphabetic: too common to tell
                seen.append(None)
            else:
                seen.append(len(g & want) / len(g))
        i = floor = 0  # floor: end of the previous run, so extending backwards never re-enters it
        while i < len(paras):
            if not seen[i] or seen[i] < 0.5:
                i += 1
                continue
            start = i
            while start > floor and (seen[start - 1] is None or seen[start - 1] >= 0.3):
                start -= 1  # the page's first lines often carry the worst OCR (curled page edge)
            while seen[start] is None:
                start += 1
            j, score = i, 0.0
            while j < len(paras) and (seen[j] is None or seen[j] >= 0.5):
                if seen[j]:
                    score += seen[j] * len(paras[j][1])
                j += 1
            end = j
            while end < len(paras) and (seen[end] is None or seen[end] >= 0.15):
                end += 1  # the page's last paragraph is cut off at the bottom: often only a line or two of it
            if not best or score > best[2]:
                best = (spine, paras[start][0], score, [p for p, tn, _ in paras[start:end] if tn])
            i = floor = end
    if not best:
        return None
    return best[0], best[1], round(best[2] / max(1, len(want_n)), 2), best[3]  # score ~ share of the photo explained


def position(idx, spine, path):
    """Percent through the book (by characters) and section title for (spine, BookScrollId)."""
    total = sum(len(t) for d in idx["docs"] for _, t, _ in d["paras"]) or 1
    before = sum(len(t) for d in idx["docs"][:spine] for _, t, _ in d["paras"])
    head = ""
    if spine < len(idx["docs"]):
        for p, t, h in idx["docs"][spine]["paras"]:
            if p == path:
                head = h
                break
            before += len(t)
    title = idx["docs"][spine]["title"] if spine < len(idx["docs"]) else ""
    return round(100 * before / total, 1), title, head


# ---------------------------------------------------------------- Kavita

class Kavita:
    def __init__(self, url=KAVITA_URL, api_key=KAVITA_API_KEY):
        self.url, self.key, self.token = url, api_key, None
        self.books = {}  # chapterId -> book dict (volume/series/library ids, path, title)
        self.all_cache = (0.0, [])

    def _req(self, method, path, body=None, retry=True):
        if not self.token:
            self.token = self._raw("POST", f"/api/Plugin/authenticate?apiKey={self.key}&pluginName=ereader-bridge")["token"]
        try:
            return self._raw(method, path, body, {"Authorization": "Bearer " + self.token})
        except urllib.error.HTTPError as e:
            if e.code == 401 and retry:
                self.token = None
                return self._req(method, path, body, False)
            raise

    def _raw(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        h = {"Content-Type": "application/json", **(headers or {})}
        with urllib.request.urlopen(urllib.request.Request(self.url + path, data=data, method=method, headers=h), timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else None

    def _epubs_of(self, series):
        out = []
        for v in self._req("GET", f"/api/Series/volumes?seriesId={series['id']}") or []:
            for c in v.get("chapters", []):
                for f in c.get("files", []):
                    if f.get("format") == EPUB:
                        book = {"chapterId": c["id"], "volumeId": v["id"], "seriesId": series["id"],
                                "libraryId": series["libraryId"], "title": series["name"], "path": f["filePath"],
                                "pages": f.get("pages")}
                        self.books[c["id"]] = book
                        out.append(book)
        return out

    def recent(self):
        """EPUBs the user is reading (Kavita's On Deck)."""
        deck = self._req("POST", "/api/Series/on-deck?PageNumber=1&PageSize=20&libraryId=0", {}) or []
        return [b for s in deck if s.get("format") == EPUB for b in self._epubs_of(s)]

    def all(self):
        """Every EPUB in the libraries (cached for an hour)."""
        if time.time() - self.all_cache[0] < 3600:
            return self.all_cache[1]
        books = []
        for lib in self._req("GET", "/api/Library/libraries") or []:
            series = self._req("POST", "/api/Series/all-v2",
                               {"statements": [{"comparison": 0, "field": 19, "value": str(lib["id"])}], "combination": 1}) or []
            for s in series:
                if s.get("format") == EPUB:
                    books += self._epubs_of(s)
        self.all_cache = (time.time(), books)
        return books

    def book(self, chapter_id):
        if chapter_id not in self.books:
            self.recent()
        if chapter_id not in self.books:
            self.all()
        return self.books.get(chapter_id)

    def progress(self, chapter_id):
        p = self._req("GET", f"/api/Reader/get-progress?chapterId={chapter_id}") or {}
        return p.get("pageNum", 0), p.get("bookScrollId")

    def scan_library(self, path):
        """Scan the library holding path. (scan-folder silently skips folders Kavita doesn't know yet;
        a library scan only reads changed folders, about a second for a few hundred books.)"""
        for lib in self._req("GET", "/api/Library/libraries") or []:
            if any(path == f or path.startswith(f.rstrip("/") + "/") for f in lib.get("folders", [])):
                self._req("POST", f"/api/Library/scan?libraryId={lib['id']}&force=false")
                return lib["name"]
        raise LookupError(f"no Kavita library holds {path}")

    def set_progress(self, book, page_num, scroll_id):
        self._req("POST", "/api/Reader/progress", {
            "volumeId": book["volumeId"], "chapterId": book["chapterId"], "seriesId": book["seriesId"],
            "libraryId": book["libraryId"], "pageNum": page_num, "bookScrollId": scroll_id})


# ---------------------------------------------------------------- the feature

class TextSync:
    def __init__(self, data_dir, kavita=None):
        self.index = Index(Path(data_dir) / "textindex")
        self.kavita = kavita or Kavita()

    def _where(self, book, idx, page_num, scroll_id):
        percent, title, head = position(idx, page_num, scroll_id)
        return {"pageNum": page_num, "bookScrollId": scroll_id, "percent": percent, "section": title, "head": head}

    def locate(self, text, chapter_id=None):
        """Find `text` in one book (chapter_id) or in the reader's recent EPUBs, then in all EPUBs."""
        if len(norm(text)) < 30:
            return {"found": False, "reason": "Too little text. Take the photo so more of the page is visible."}
        tiers = [("chosen", [self.kavita.book(chapter_id)])] if chapter_id else [("recent", self.kavita.recent()), ("all", None)]
        tried = set()
        for tier, books in tiers:
            books = books if books is not None else self.kavita.all()
            scored = []
            for b in books:
                if not b or b["chapterId"] in tried:
                    continue
                tried.add(b["chapterId"])
                try:
                    idx = self.index.get(b["path"])
                except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError) as e:
                    log.warning("textsync: cannot read %s: %s", b["path"], e)
                    continue
                hit = best_run(idx, text)
                if hit:
                    scored.append((hit[2], b, idx, hit))
            scored.sort(key=lambda s: -s[0])
            if scored and scored[0][0] >= MIN_SCORE:
                score, b, idx, (spine, path, _, paths) = scored[0]
                target = self._where(b, idx, spine, path)
                target["paras"] = [{k: w[k] for k in ("bookScrollId", "percent", "head")}
                                   for w in (self._where(b, idx, spine, p) for p in paths)]
                cur_page, cur_scroll = self.kavita.progress(b["chapterId"])
                current = self._where(b, idx, cur_page, cur_scroll)
                return {"found": True, "searched": tier, "score": score,
                        "book": {k: b[k] for k in ("chapterId", "title")},
                        "target": target, "current": current, "backward": target["percent"] < current["percent"],
                        "others": [{"chapterId": s[1]["chapterId"], "title": s[1]["title"], "score": s[0]}
                                   for s in scored[1:4] if s[0] >= MIN_SCORE]}
        return {"found": False, "reason": "This page was not found in the books you are reading or in any EPUB."}

    def apply(self, chapter_id, page_num, scroll_id):
        book = self.kavita.book(chapter_id)
        if not book:
            raise KeyError(f"unknown chapter {chapter_id}")
        self.kavita.set_progress(book, page_num, scroll_id)
        return {"ok": True, "title": book["title"]}

    def recent_books(self):
        out = []
        for b in self.kavita.recent():
            try:
                idx = self.index.get(b["path"])
                page, scroll = self.kavita.progress(b["chapterId"])
                out.append({"chapterId": b["chapterId"], "title": b["title"], **self._where(b, idx, page, scroll)})
            except Exception as e:
                log.warning("textsync: %s: %s", b["title"], e)
        return out

    def warm(self):
        """Index every EPUB in the background so the 'search all' fallback is fast."""
        try:
            for b in self.kavita.all():
                try:
                    self.index.get(b["path"])
                except Exception as e:
                    log.debug("textsync warm: %s: %s", b["path"], e)
            log.info("textsync: indexed %d EPUBs", len(self.kavita.all()))
        except Exception as e:
            log.warning("textsync warm-up failed: %s", e)
