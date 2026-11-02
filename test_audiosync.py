"""Self-check for audiobook <-> EPUB sync: synthetic EPUB, fake Kavita/Audiobookshelf, and a fake narrator
whose pace changes from chapter to chapter, with speech-recognition-like errors. Run: python test_audiosync.py"""
import bisect
import random
import tempfile
from pathlib import Path

import audiosync as A
import textsync as T
from test_textsync import make_epub, ocr_noise


def demo():
    tmp = Path(tempfile.mkdtemp())
    epub = tmp / "book.epub"
    make_epub(epub, 11)
    idx = T.build_index(epub)
    text = A.Text(idx)

    # narration: 15 s opening, then each chapter at its own pace with a 10 s pause before it
    pace = [4.0, 4.7, 3.7]
    starts, t = [], 15.0                 # (char offset, time) at each chapter start
    for spine, d in enumerate(idx["docs"]):
        n = sum(len(tn) for _, tn, _ in d["paras"])
        starts.append((sum(len(tn) for dd in idx["docs"][:spine] for _, tn, _ in dd["paras"]), t, pace[spine]))
        t += n / pace[spine] + 10
    duration = t + 15

    def time_of(offset):
        o0, t0, r = starts[max(0, bisect.bisect_right([s[0] for s in starts], offset) - 1)]
        return t0 + (offset - o0) / r

    def offset_at(seconds):
        for o0, t0, r in reversed(starts):
            if seconds >= t0:
                return min(text.total, int(o0 + (seconds - t0) * r))
        return 0

    def narrator(files, seconds, length, language):
        assert files == [("/audiobooks/book.m4b", 0.0, duration)] and language == "ko"
        o = offset_at(seconds)
        return ocr_noise(text.book[o:o + int(length * 4.2)], random.Random(int(seconds)), rate=0.06)

    class Kavita:
        book_ = {"chapterId": 7, "volumeId": 1, "seriesId": 1, "libraryId": 1, "title": "Test book", "path": str(epub)}
        pos = (0, None)
        def recent(self): return [self.book_]
        def all(self): return [self.book_]
        def book(self, cid): return self.book_ if cid == 7 else None
        def progress(self, cid): return self.pos

    class Abs:
        time, saved = 0.0, None
        def books(self): return [{"id": "li_1", "title": "Test book", "duration": duration}]
        def in_progress(self): return ["li_1"]
        def item(self, i): return {"title": "Test book", "duration": duration, "files": [("/audiobooks/book.m4b", 0.0, duration)]}
        def progress(self, i): return self.time
        def set_progress(self, i, s, d): self.saved = (i, s, d)

    kav, abs_ = Kavita(), Abs()
    ts = T.TextSync(tmp / "data", kav)
    au = A.AudioSync(ts, tmp / "data", abs=abs_, transcriber=narrator)

    # 0. the pace really is uneven: a plain time fraction would be this far off somewhere
    worst = max(abs(o / text.total - time_of(o) / duration) for o in range(0, text.total, 500))
    assert 0.02 < worst < A.SPAN, worst

    # 1. pairing by title, with both positions
    (p,) = au.pairs()
    assert p["chapterId"] == 7 and p["audio"]["itemId"] == "li_1" and p["audio"]["duration"] == duration

    # 2. audio -> book: the listening position lands on its paragraph (or the one before, mid-sentence)
    rng, exact = random.Random(1), 0
    for _ in range(12):
        o = rng.randrange(1000, text.total - 2000)
        abs_.time = time_of(o)
        r = au.to_book(7, "li_1")
        assert r["found"], r
        want = text.at(o)
        got = next(i for i, q in enumerate(text.paras) if q[:2] == (r["target"]["pageNum"], r["target"]["bookScrollId"]))
        assert want - 1 <= got <= want, (want, got)
        exact += got == want
        assert any(q["bookScrollId"] == text.paras[want][1] for q in r["target"]["paras"])
    assert exact >= 9, exact

    # 3. book -> audio: Kavita's paragraph -> the time its narration starts, within 3 s (one correction alone is ~10 s off)
    au.anchors.clear()   # start from nothing: only the time fraction to go on
    for spine, k in [(0, 12), (1, 30), (2, 20), (2, 38)]:
        paras = [q for q in text.paras if q[0] == spine and q[2]]
        kav.pos = (spine, paras[k][1])
        r = au.to_audio(7, "li_1")
        assert r["found"], r
        true = time_of(text.offset(spine, paras[k][1]))
        assert abs(r["audio"]["time"] - true) < 3, (spine, k, r["audio"]["time"], true)

    # 4. anchors are kept on disk; apply writes Audiobookshelf progress
    assert A.AudioSync(ts, tmp / "data", abs=abs_, transcriber=narrator).anchors["7:li_1"]
    assert au.apply_audio("li_1", 1234.5)["ok"] and abs_.saved == ("li_1", 1234.5, duration)

    # 5. a clip from nowhere in this book is not forced onto it
    au.transcribe = lambda *a: "가나다라마바사 아자차카타파하 " * 20
    abs_.time = duration / 2
    assert not au.to_book(7, "li_1")["found"]
    print("ok: all audiosync checks passed")


if __name__ == "__main__":
    demo()
