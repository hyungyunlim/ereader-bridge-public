# Audiobook ↔ EPUB sync

Moves your position between an audiobook in [Audiobookshelf](https://www.audiobookshelf.org/) and the EPUB in Kavita,
in either direction. KOReader and Readest follow Kavita's progress, so you can stop listening and pick up the book on
your reader, or stop reading and keep listening.

```
Audiobookshelf position (seconds) ⇄ bridge ⇄ Kavita progress (paragraph) ⇄ KOReader / Readest
                                     ↑
                 30 s of the audiobook transcribed by a whisper.cpp server
```

Open `http://<bridge>:8086/audio` (handy as a home-screen bookmark on your phone). The `/sync` page links to it too.

- Each book has two buttons. Nothing changes until you confirm with **Sync**.
  - **🎧→📖 Sync book**: finds the listening position in the book and lists the paragraphs around it. Pick the one you
    listened up to and it is saved to Kavita (the same picker as paper → EPUB sync).
  - **📖→🎧 Sync audiobook**: finds the time where the narrator starts Kavita's paragraph and saves it to Audiobookshelf.
- Books listed: EPUBs on Kavita's On Deck and audiobooks you are listening to in Audiobookshelf that exist in **both,
  with the same title** (spaces and punctuation ignored).
- Time taken: about 5 s for the book, 10–20 s for the audiobook (a few transcriptions). The first list after the bridge
  restarts takes ~10 s while it indexes EPUBs.
- If the Audiobookshelf app is open, reopen the book to pick up the new position. An app that keeps playing writes its
  own position over the server's.

## Auto sync (per book)

Press **Turn on auto sync** under a book on `/audio`, and whenever you **stop reading or listening for 5 minutes**, the
other side is moved to your position. The two positions at the moment you turn it on count as "in step"; after that,
only the side that moved is carried over. The latest auto action is shown in one line under the book.

- **Watching**: every minute the bridge reads Kavita's progress and Audiobookshelf's position (two API calls).
- **When it syncs**: after one side changes and **both sides have stayed put for 5 minutes**, the side that moved last
  is carried over to the other.
  - Nothing happens while you are reading (turning pages) or listening (the position keeps changing).
  - A short pause that you resume within 5 minutes is ignored.
  - While Audiobookshelf is playing its position keeps changing, so the audiobook position is never touched then.
- **Forward only**: if the new position is behind the other side (somewhere you have already read or heard), nothing
  changes. Re-reading or re-listening never drags the other side back; use the buttons to move back on purpose.
- **No echo**: right after syncing, both positions are read again and recorded as "in step", so the bridge doesn't
  mistake its own write for new progress and sync back the other way.
- **One transcription run per session**: book → audiobook takes 2–5 transcriptions (10–20 s), audiobook → book one
  (~5 s). If an anchor is nearby and the two positions already agree, it ends as "already in step" without transcribing.
- **On failure**: a position that can't be found is skipped and tried again the next time you move. On a server error
  it retries after 5 minutes.
- **State**: the books with auto sync and their state live in `data/audio_auto.json` and survive restarts. Change the
  wait with `AUDIOSYNC_IDLE_SECONDS` (default 300).
- **Self-check**: `test_audiosync.py` runs on a simulated clock and checks: reading → 5 minutes → the audiobook follows;
  no echo of the bridge's own write; listening with short pauses → 5 minutes → the book follows; going back is ignored;
  no transcription when already in step; state survives a restart; turning it off stops it.

## How it works

- **Audiobook → book**
  1. ffmpeg cuts 30 s from the listening position and whisper.cpp transcribes it.
  2. The transcript is looked up in the book text, **only within ±6% of the expected position**. Narration pace is even
     enough that the time fraction and the text fraction usually agree within about 1%.
  3. The exact start comes from where the transcript's distinctive 12-character pieces sit in the book.
- **Book → audiobook**
  1. Guess the time from the text position.
  2. Transcribe at that time and see which character it lands on.
  3. Correct the time by the character gap divided by the local narration pace. Repeat (at most 5 times) until the clip
     starts within 60 characters (~15 s) of the paragraph.
- **Anchors**: every match is saved as a (time, character offset) pair in `data/audio_anchors.json`, so later guesses
  in that book start closer.
- **Why not search the whole book**: speech recognition makes different mistakes than photo OCR. It spells names by
  sound and writes spoken numbers as digits ("twenty-eight" → "28"). A whole-book search missed 3 of 8 test clips; the
  ±6% window found all of them.
- **Language**: Korean books are transcribed as Korean; anything else uses whisper's language detection.
- **Tested** on a 10 h 44 min Korean audiobook: audio → book hit all 8 test positions, and book → audio landed within
  2 s of the paragraph start after 3 rounds. A 21 h English audiobook matched at 71.3% of the book for 71.4% of the audio.

## Limits

- Audiobook chapter markers aren't used; they often don't line up with the book's chapters (some audiobooks are just
  one-hour tracks). The text is matched directly instead.
- Very uneven narration, or an audiobook that skips large parts, can fall outside the ±6% window. The result is then
  "not found" and nothing is changed. Once one position matches, its anchor makes nearby positions easier.
- A different edition (abridged, another translation) won't match.
- For audiobooks split into several files, a 30 s clip that crosses a file boundary is only transcribed up to the end
  of the first file (the position is still right; the text is a little shorter).

## Setup

1. **whisper.cpp server.** Any machine the bridge can reach works; set `WHISPER_URL` to it. On a Mac, run it natively,
   because Docker on macOS can't use the GPU:
   ```bash
   brew install whisper-cpp ffmpeg
   scripts/install-whisper-server.sh
   ```
   The script downloads `ggml-large-v3-turbo-q5_0.bin` (547 MB) to `~/whisper-models/` and runs `whisper-server` on
   `127.0.0.1:8178` as a launchd login service (`org.ereader-bridge.whisper-server`, log in
   `~/Library/Logs/whisper-server.log`). The bridge container reaches it at `http://host.docker.internal:8178` (the
   default `WHISPER_URL`). Transcribing 30 s takes about 4 s on an M1.
2. **Audiobookshelf API key.** Create one in Audiobookshelf under Settings → API Keys and put it in `.env` as
   `AUDIOBOOKSHELF_TOKEN`. Set `AUDIOBOOKSHELF_URL` in `docker-compose.yml` to your server
   (default `http://host.docker.internal:13378`). `curl -H "Authorization: Bearer $TOKEN" $URL/api/me` should return 200.
   Paper → EPUB sync's Kavita settings (`KAVITA_URL`, `KAVITA_API_KEY`, the books mount) are needed too.
3. **Audio files.** Mount your audiobooks read-only at the **same paths Audiobookshelf sees inside its container**
   (e.g. `/audiobooks`). The bridge opens the file paths Audiobookshelf reports.
4. **Optional: Docker networks.** If Kavita and Audiobookshelf run in Docker on the same host, the bridge can join their
   networks and call them by container name. Uncomment the `networks:` lines in `docker-compose.yml` and use
   `KAVITA_URL=http://kavita:5000`, `AUDIOBOOKSHELF_URL=http://audiobookshelf:80`. This avoids Docker Desktop's host port
   forwarding, where new connections occasionally stalled for ~20 s. The bridge then won't start while those networks
   are missing.

Self-check: `python test_audiosync.py` (fake Kavita and Audiobookshelf, and a fake narrator whose pace changes from
chapter to chapter, with speech-recognition-like errors).

## API

- `GET /audio` serves the page.
- `GET /audio/books` → `{books: [{chapterId, title, book{percent, ...}, audio{itemId, title, duration, time}}]}`
- `POST /audio/locate` `{"chapterId", "itemId", "direction": "book"}` → same shape as `POST /locate` (with the
  paragraph list); save with `POST /locate/apply`.
- `POST /audio/locate` `{"chapterId", "itemId", "direction": "audio"}` →
  `{found, book{percent, head}, audio{itemId, title, time, current, duration}, backward}`
- `POST /audio/apply` `{"itemId", "time"}` saves the Audiobookshelf listening position.
- `POST /audio/auto` `{"chapterId", "itemId", "on"}` turns auto sync on or off for a book; `GET /audio/books`
  reports it per book as `auto{on, last{at, msg}}`.

The JSON endpoints return 503 when `AUDIOBOOKSHELF_URL`/`AUDIOBOOKSHELF_TOKEN` or the Kavita settings are missing.
