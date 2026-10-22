# Paper book → EPUB sync

You read a few chapters of the printed book, then pick up your e-reader. Photograph the page you're on, and the
bridge finds that page in the EPUB and moves your Kavita progress there. KOReader, or Readest, using Kavita as its
progress sync server, then offers to jump to that position when you open the book.

```
iOS shortcut: photo → on-device text recognition → http://<bridge>:8086/sync?text=…
bridge /sync: search the EPUBs you're reading (Kavita "On Deck"), then every EPUB → confirmation page
[Sync] → Kavita progress (chapter + paragraph) → KOReader / Readest progress sync
```

## Setup

1. Set `KAVITA_API_KEY` in `.env` and `KAVITA_URL` in `docker-compose.yml`. The API key is under Kavita's user settings.
2. Mount your Kavita library into the bridge **at the same paths Kavita uses**, for example `/path/to/books:/books:ro` if Kavita sees the library as `/books`. The bridge opens the EPUB files that Kavita's API reports.
3. Restart the bridge. On start it indexes every EPUB in Kavita in the background; 300 books took about a minute. The index is cached in `data/textindex/` and rebuilt when a file changes.

Progress is saved for the Kavita user who owns the API key. KOReader's progress sync has to use the same user.

## The iOS shortcut

`scripts/make_shortcut.py` builds a four-step shortcut: Take Photo → Extract Text from Image → URL Encode → Open URL.

- **Easiest:** import the shared iCloud link, if one is posted with this project. Shortcuts asks for your bridge address on import.
- **On a Mac:**
  ```bash
  python3 scripts/make_shortcut.py http://192.168.1.10:8086
  shortcuts sign --mode anyone -i "shortcuts/Paper Book Sync.unsigned.shortcut" -o "shortcuts/Paper Book Sync.shortcut"
  open "shortcuts/Paper Book Sync.shortcut"
  ```
  It then syncs to your iPhone through iCloud.

Tip: assign it to Back Tap (Settings → Accessibility → Touch → Back Tap), or add it to the Home Screen. When you're away from home, the phone needs your VPN to reach the bridge.

You can also open `http://<bridge>:8086/sync` in any browser and paste the text.

## Using it

- Photograph the page where you stopped. The page should fill most of the photo; under 30 characters of text is rejected.
- The confirmation page shows the book, the chapter and the position as a percentage.
  - It lists the paragraphs on that page, including the cut-off ones at the top and bottom. **Tap the one you stopped at**, then [Sync].
  - You get a warning if the position is earlier than your current EPUB progress, which usually means the wrong book or an old page.
  - [Wrong book] lets you choose from the books you're reading.

## How matching works, and its limits

- Text is normalized: spaces, punctuation and line breaks are removed. Each EPUB paragraph is then scored by how many of its character n-grams appear in the photo, and the best run of consecutive matching paragraphs wins.
  - **Korean/CJK** uses 3-grams, since one character is roughly one syllable.
  - **Alphabetic scripts** use 7-grams, because short letter sequences like "the" and "ing" appear on every page.
  - Short lines such as `"Yes," said Harry.` are too common to tell apart in English, so they neither help nor break a match.
- Measured results:

  | test | photos | result | wrong-book score |
  |---|---|---|---|
  | Korean novel, heavy simulated OCR errors | 98 | 93 exact paragraph, 5 off by one | ≤ 0.01 |
  | English novels (a 7-book series), 2% OCR errors | 50 body-text pages | all within one paragraph | ≤ 0.05 typical, 0.45 at worst (front matter) |

  The correct book typically scores 0.7–0.9. The threshold is `TEXTSYNC_MIN_SCORE=0.3`.
- A different edition or translation lowers the score. A completely different text won't be found.
- The percentage is by characters, so it can differ a little from what KOReader or Kavita shows.
- Positions are paragraph-level. Readers that re-paginate the EPUB may land a few lines away from the paragraph start.

## API

- `POST /locate` with `{"text": "...", "chapterId": 123?}` returns `{found, searched: recent|all|chosen, score, book, target{pageNum, bookScrollId, percent, section, head, paras[{bookScrollId, percent, head}]}, current{...}, backward, others}`.
- `POST /locate/apply` with `{"chapterId", "pageNum", "bookScrollId"}` saves Kavita progress. Use one of `paras` as the `bookScrollId`.
- `GET /locate/books` lists the EPUBs you're reading, with their current position.
- `GET /sync?text=...` serves the confirmation page.
