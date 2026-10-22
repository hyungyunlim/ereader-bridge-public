"""Self-check for paper -> EPUB position sync, with synthetic EPUBs and a fake Kavita. Run: python test_textsync.py"""
import json
import random
import socket
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import textsync as T

SYL = "가나다라마바사아자차카타파하고노도로모보소오조초코토포호구누두루무부수우주추쿠투푸후기니디리미비시이지치"


def sentence(rng, words):
    return " ".join("".join(rng.choice(SYL) for _ in range(rng.randint(1, 4))) for _ in range(words)) + "."


WORDS = ("the and of to a in was he that it his her you had with she as for at on said but him not they be "
         "from have were one all we by would there their what could out been when into up down over back then "
         "harry door light hand eyes voice night house room table window face looked turned something nothing "
         "long little old great quite very again still away before after through under behind around").split()


def en_sentence(rng, words):
    return " ".join(rng.choice(WORDS) for _ in range(words)).capitalize() + "."


def make_epub(path, seed, sentence=sentence):
    """3 chapters; chapter 2 has spacer <p>s, &nbsp;, a nested <div>, dialogue lines and an NCX TOC."""
    rng = random.Random(seed)
    chapters = []
    for c in range(3):
        paras = []
        if c == 1:
            paras += ["<p>&nbsp;</p>", "<p></p>"]  # spacers like the real book has
        for k in range(40):
            if k % 7 == 3:
                paras.append(f"<p>“{sentence(rng, 2)}”</p>")  # short dialogue line
            else:
                paras.append(f"<p>{' '.join(sentence(rng, rng.randint(8, 20)) for _ in range(rng.randint(1, 3)))}</p>")
        if c == 2:
            paras.insert(5, f"<div class='box'><p>{sentence(rng, 15)}</p><p>{sentence(rng, 15)}</p></div>")
        chapters.append(f"<html xmlns='http://www.w3.org/1999/xhtml'><head><title>c</title></head><body>{''.join(paras)}</body></html>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", "<container xmlns='urn:oasis:names:tc:opendocument:xmlns:container'><rootfiles>"
                   "<rootfile full-path='OEBPS/content.opf' media-type='application/oebps-package+xml'/></rootfiles></container>")
        items = "".join(f"<item id='c{i}' href='Text/c{i}.xhtml' media-type='application/xhtml+xml'/>" for i in range(3))
        spine = "".join(f"<itemref idref='c{i}'/>" for i in range(3))
        z.writestr("OEBPS/content.opf", "<package xmlns='http://www.idpf.org/2007/opf' version='2.0'><manifest>"
                   f"{items}<item id='ncx' href='toc.ncx' media-type='application/x-dtbncx+xml'/></manifest><spine toc='ncx'>{spine}</spine></package>")
        nav = "".join(f"<navPoint id='n{i}'><navLabel><text>{i + 1}장</text></navLabel><content src='Text/c{i}.xhtml'/></navPoint>" for i in range(3))
        z.writestr("OEBPS/toc.ncx", f"<ncx xmlns='http://www.daisy.org/z3986/2005/ncx/'><navMap>{nav}</navMap></ncx>")
        for i, html in enumerate(chapters):
            z.writestr(f"OEBPS/Text/c{i}.xhtml", html)


def ocr_noise(text, rng, rate=0.09):
    out = []
    for ch in text:
        r = rng.random()
        if r < rate * 2 / 3:
            continue
        if r < rate:
            out.append(rng.choice("이가은는을를의."))
        out.append(ch)
        if rng.random() < 0.02:
            out.append("\n")
    return "".join(out)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def demo():
    tmp = Path(tempfile.mkdtemp())
    book_a, book_b = tmp / "a.epub", tmp / "b.epub"
    make_epub(book_a, 1)
    make_epub(book_b, 2)
    saved = {}

    class FakeKavita(BaseHTTPRequestHandler):
        def ok(self, obj):
            b = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if self.path.startswith("/api/Plugin/authenticate"):
                return self.ok({"token": "t"})
            assert self.headers["Authorization"] == "Bearer t"
            if self.path.startswith("/api/Series/on-deck"):
                return self.ok([{"id": 1, "name": "책 A", "libraryId": 1, "format": 3}])  # B is not on deck
            if self.path.startswith("/api/Series/all-v2"):
                return self.ok([{"id": 1, "name": "책 A", "libraryId": 1, "format": 3},
                                {"id": 2, "name": "책 B", "libraryId": 1, "format": 3}])
            if self.path == "/api/Reader/progress":
                saved.update(body)
                return self.ok(None)

        def do_GET(self):
            if self.path.startswith("/api/Series/volumes?seriesId="):
                sid = int(self.path.rsplit("=", 1)[1])
                return self.ok([{"id": 10 + sid, "chapters": [{"id": 100 + sid, "files": [
                    {"filePath": str(book_a if sid == 1 else book_b), "format": 3, "pages": 3}]}]}])
            if self.path.startswith("/api/Reader/get-progress"):
                return self.ok({"pageNum": 2, "bookScrollId": "//body/p[30]"})  # current: far into the book
            if self.path == "/api/Library/libraries":
                return self.ok([{"id": 1}])

        def log_message(self, *a):
            pass

    port = free_port()
    threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", port), FakeKavita).serve_forever, daemon=True).start()
    ts = T.TextSync(tmp / "data", T.Kavita(f"http://127.0.0.1:{port}", "key"))
    idx_a, idx_b = T.build_index(book_a), T.build_index(book_b)

    # index: paths in Kavita's BookScrollId form, nested div, &nbsp; spacer, NCX titles
    paths = [p for p, _, _ in idx_a["docs"][2]["paras"]]
    assert "//body/div[1]/p[2]" in paths and paths[0] == "//body/p[1]", paths[:8]
    assert idx_a["docs"][1]["paras"][0][1] == "" and [d["title"] for d in idx_a["docs"]] == ["1장", "2장", "3장"]

    # 1. noisy photos of random pages land on the page's first paragraph (or its neighbour)
    rng = random.Random(3)
    exact = near = 0
    for _ in range(30):
        spine = rng.randrange(3)
        paras = [(p, h) for p, n, h in idx_a["docs"][spine]["paras"]]
        raw = [p for p in T._paragraphs(T._Tree(zipfile.ZipFile(book_a).read(f"OEBPS/Text/c{spine}.xhtml").decode()).root)]
        start = rng.randrange(0, len(raw) - 8)
        while not T.norm(raw[start][1]):
            start += 1
        page = raw[start:start + rng.randint(4, 7)]
        hit = T.best_run(idx_a, ocr_noise("\n".join(t for _, t in page), rng))
        names = [p for p, _ in raw]
        exact += hit[:2] == (spine, page[0][0])
        near += hit[0] == spine and abs(names.index(hit[1]) - start) <= 1
    assert exact >= 26 and near == 30, (exact, near)

    # 2. unrelated text is rejected; the other book's text matches that book, not this one
    assert (T.best_run(idx_a, sentence(random.Random(99), 120)) or (0, 0, 0.0))[2] < T.MIN_SCORE
    b_page = "\n".join(t for _, t in T._paragraphs(T._Tree(zipfile.ZipFile(book_b).read("OEBPS/Text/c1.xhtml").decode()).root)[10:15])
    assert T.best_run(idx_b, b_page)[2] > 0.6 and (T.best_run(idx_a, b_page) or (0, 0, 0.0))[2] < T.MIN_SCORE

    # 3. locate: on-deck book found, with section, percent, first words and "backward" vs current progress
    a_raw = T._paragraphs(T._Tree(zipfile.ZipFile(book_a).read("OEBPS/Text/c1.xhtml").decode()).root)
    r = ts.locate("\n".join(t for _, t in a_raw[6:11]))
    assert r["found"] and r["searched"] == "recent" and r["book"] == {"chapterId": 101, "title": "책 A"}, r
    assert r["target"]["pageNum"] == 1 and r["target"]["bookScrollId"] == a_raw[6][0] and r["target"]["section"] == "2장"
    assert r["target"]["head"] and r["backward"] and 30 < r["target"]["percent"] < 60, r["target"]
    paras = r["target"]["paras"]  # the photographed page's paragraphs, in order, to pick from
    assert [p["bookScrollId"] for p in paras] == [p for p, t in a_raw[6:11]], paras
    assert all(p["head"] for p in paras) and paras[0]["percent"] < paras[-1]["percent"]
    # the page's last paragraph, cut off after a line, is still offered (often it's where you stopped)
    r = ts.locate("\n".join(t for _, t in a_raw[6:11]) + "\n" + a_raw[11][1][:len(a_raw[11][1]) // 4])
    assert [p["bookScrollId"] for p in r["target"]["paras"]] == [p for p, t in a_raw[6:12]], r["target"]["paras"]

    # 4. a book that is not on deck is found by the full-library fallback
    r = ts.locate(b_page)
    assert r["found"] and r["searched"] == "all" and r["book"]["chapterId"] == 102, r

    # 5. too little text, and a page from no book
    assert not ts.locate("짧다")["found"] and not ts.locate(sentence(random.Random(5), 150))["found"]

    # 6. apply writes Kavita progress with the ids Kavita needs
    ts.apply(101, 1, a_raw[6][0])
    assert saved == {"volumeId": 11, "chapterId": 101, "seriesId": 1, "libraryId": 1, "pageNum": 1, "bookScrollId": a_raw[6][0]}, saved

    # 7. English: letter 3-grams are everywhere, so alphabetic text matches on 7-grams; short lines don't count
    book_en = tmp / "en.epub"
    make_epub(book_en, 4, en_sentence)
    idx_en = T.build_index(book_en)
    raw = T._paragraphs(T._Tree(zipfile.ZipFile(book_en).read("OEBPS/Text/c1.xhtml").decode()).root)
    near = 0
    for k in range(20):
        page = raw[4 + k:10 + k]
        hit = T.best_run(idx_en, ocr_noise("\n".join(t for _, t in page), random.Random(k), rate=0.02))
        names = [p for p, _ in raw]
        near += bool(hit) and hit[0] == 1 and abs(names.index(hit[1]) - (4 + k)) <= 1
    assert near >= 18, near
    other = "\n".join(en_sentence(random.Random(50 + k), 18) for k in range(8))  # same vocabulary, other text
    assert (T.best_run(idx_en, other) or (0, 0, 0.0))[2] < T.MIN_SCORE

    # 8. the index is cached on disk and reused
    assert len(list((tmp / "data" / "textindex").glob("*.json.gz"))) == 2
    print("ok: all textsync checks passed")


if __name__ == "__main__":
    demo()
