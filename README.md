# ereader-bridge

Kavita's **Send to Device** for e-readers that aren't Kindles. The bridge pretends to be an SMTP server for Kavita and
delivers each file to the device's own Wi-Fi transfer server. If the device is asleep or away, the file waits in a
queue until the device shows up again.

```
Kavita "Send to Device" ──SMTP :2525──┐
                                      ├─▶ queue (/data/queue/<device>/) ─▶ find device ─▶ device's Wi-Fi transfer server
curl / scripts ─────────HTTP :8086────┘                 ▲
                                                         └── KOReader plugin pulls its own queue (VPN, hotspot, …)
```

It also includes:

- **A KOReader plugin** that pulls the device's queue when KOReader starts or wakes up, so files arrive even where the bridge can't push. It updates itself from the bridge.
- **Paper book → EPUB sync**: photograph the page you're on in a printed book, and the bridge finds that paragraph in the EPUB and moves your Kavita progress there. KOReader, or any app using Kavita's KOReader sync, then offers to jump to it. See [docs/paper-sync.md](docs/paper-sync.md).
- **Audiobook ↔ EPUB sync**: moves your position between an Audiobookshelf audiobook and the Kavita EPUB, either way. The bridge transcribes 30 s of the audiobook with a whisper.cpp server and finds that passage in the book. See [docs/audio-sync.md](docs/audio-sync.md).
- A small web page at `http://<bridge>:8086/` with the queue, history, cancel and resend.

Python standard library plus `aiosmtpd`, in one Docker container (with `ffmpeg` for audiobook sync, which also needs a whisper.cpp server).

> **No authentication.** Anyone who can reach port 8086 can queue files, read the queue and change your Kavita
> reading progress. Run it on your LAN, or reach it through a VPN (WireGuard, Tailscale). Don't expose it to the internet.

## Supported devices

| driver | devices | how it delivers | identity |
|---|---|---|---|
| `boox` | BOOX (tested: Palma 2) with **BOOX Drop** open (port 8085) | `POST /api/library/upload` → `Books/` | the device `id` from `GET http://<boox>:8085/api/device`. If its IP changes, the bridge rescans `SCAN_SUBNET` (at most every 5 min). |
| `moosnap` | Moosnap readers (tested: Ocean, Neo) with **WiFi Import** open (port 10123) | `POST /test.php?action=addBook` | the IP address. The device doesn't report anything identifying, so **give it a fixed IP** in your router. |
| (any) | anything running KOReader | the plugin in `koreader/` pulls the queue | the device name in the plugin settings |

Adding another device type means writing three functions (`probe`, `exists`, `upload`) in `bridge.py`.

## Setup

1. Configure your devices in `docker-compose.yml` (`DEVICES` JSON). The device name is the bridge's address for that device:
   ```json
   {"palma":   {"driver": "boox", "id": "aa:bb:cc:dd:ee:ff", "host": "192.168.1.50"},
    "moosnap": {"driver": "moosnap", "host": "192.168.1.51", "alt_hosts": ["10.0.0.51"]}}
   ```
   `alt_hosts` are other fixed addresses of the same device, for example its VPN IP when you're away. The bridge tries the last address that worked, then `host`, then `alt_hosts`.
2. `cp .env.example .env` and fill it in (both values are optional), then run `docker compose up -d --build`.
3. In Kavita, go to **Server settings → Email** and set:
   - host: the bridge (`host.docker.internal` if Kavita runs in Docker on the same machine), port `2525`
   - SSL off, any sender address
   - a size limit that fits your books
4. In Kavita, go to **User settings → Devices** and add one device per bridge device:
   - platform: Custom
   - email: `<device-name>@anything`, for example `palma@ereader-bridge.local`

   The part before `@` must match a device name in `DEVICES`. Unknown names are rejected with SMTP 550.

Kavita's "sent successfully" means *queued on the bridge*. Check `http://<bridge>:8086/` or `/status` to see whether it actually arrived, or set `NOTIFY_URL` to get notified (below).

You can send without Kavita too:

```bash
curl -T book.epub "http://<bridge>:8086/send/palma/book.epub?wait=60"   # waits up to 60 s for delivery
```

## How delivery works

- Every 30 s (`POLL_SECONDS`) the bridge checks each device with something queued and sends files in order. A failed send is retried on the next round.
- BOOX only: on the first try, a file with the same name already on the device is marked `duplicate` and not sent. Retries overwrite, in case an earlier transfer was cut off halfway.
- The queue lives on disk (`./data`), so it survives restarts. Delivered files are kept for `SENT_KEEP_DAYS` (7) so you can resend them, including to another device. Undelivered jobs expire after `QUEUE_EXPIRE_DAYS` (30).
- History is kept in `data/history.jsonl`, and the last 200 entries are shown on the web page.

## KOReader plugin (`koreader/bridgesync.koplugin`)

For devices the bridge can't push to, for example when you're away from home on a VPN or phone hotspot, the device fetches its own queue. The connection goes from the device to the bridge, so a VPN client on the reader is enough.

- On start and on wake-up it checks the queue. If there are new files it asks **[Get all] / [Choose] / [Later]**. [Later] stays quiet until something new is queued.
- Downloads run in a subprocess, so the UI stays responsive. Tap the progress box to stop; the remaining files stay queued.
- **Tools → Bridge sync** has *Show queue* and *Check for plugin update*.
- **Self-update:** once per session, the plugin compares its `_meta.lua` version with the copy the bridge serves and offers to update. The download is checked with sha256 before it replaces the plugin. To roll out a change, bump `version` in `_meta.lua` and rebuild the container.
- **Install:** copy the folder to `<koreader>/plugins/`, and create `<koreader>/settings/bridgesync.lua`:
  ```lua
  return { ["url"] = "http://<bridge-host>:8086", ["device"] = "<device-name>", ["dir"] = "/storage/emulated/0/Books" }
  ```
  On Android readers with adb, `scripts/install-plugin-adb.sh <serial> <bridge-url> <device-name> <dir>` does both.
- Push and pull can overlap safely: downloading a job claims it, so the same file is never delivered twice.

Pull API:
- `GET /pull/<device>` lists queued jobs.
- `GET /pull/<device>/<job>` downloads a file and claims the job. An unfinished claim is released after 15 minutes.
- `POST /pull/<device>/<job>/done` reports that the file arrived.
- `POST /pull/<device>/<job>/release` gives the job back.

## Kavita reading progress with KOReader

This is a Kavita feature; the bridge doesn't do the syncing itself. In KOReader, set Progress sync to a custom server `http://<kavita>/api/koreader/<your Kavita API key>`. Any username and password work, because the key in the URL is what authenticates. Set document matching to **Binary**.

Only files that are **byte-identical** to Kavita's copy are matched. Books delivered through this bridge are, because it sends Kavita's original file. Readest works the same way (Settings → KOReader Sync, checksum "File Content").

## Notifications (optional)

Set `NOTIFY_URL` and the bridge POSTs `{"event", "device", "file"}` on each event in `NOTIFY_EVENTS` (default `queued,delivered,pulled`). `ha/ereader_bridge.example.yaml` is a Home Assistant package that turns this into phone notifications, and also watches `/health` to tell you when the bridge is down.

## Other HTTP endpoints

- `GET /status` returns the devices, queues and history as JSON.
- `GET /health` returns `{"ok": true}`.
- `DELETE /jobs/<device>/<job>` cancels a job. It returns 409 if the job is being delivered.
- `POST /jobs/<device>/<job>/resend?to=<device>` sends a delivered file again.
- `GET /sync`, `POST /locate`, `POST /locate/apply` and `GET /locate/books` belong to paper → EPUB sync ([docs/paper-sync.md](docs/paper-sync.md)).
- `GET /audio`, `GET /audio/books`, `POST /audio/locate` and `POST /audio/apply` belong to audiobook ↔ EPUB sync ([docs/audio-sync.md](docs/audio-sync.md)).

## Development

- `python test_bridge.py` runs end-to-end checks against fake BOOX/Moosnap/webhook servers (needs `aiosmtpd`).
- `python test_textsync.py` checks the paper → EPUB matcher with synthetic Korean and English books.
- `python test_audiosync.py` checks audiobook ↔ EPUB sync with a fake narrator whose pace varies by chapter.
- `luajit -bl koreader/bridgesync.koplugin/main.lua > /dev/null` checks the plugin's syntax.
- `scripts/deploy.sh` does `git pull --ff-only` → `docker compose up -d --build` → waits for `/health`.
- `scripts/boox_push.py` uploads a whole folder to a BOOX via BOOX Drop, staged and then renamed.

## License

[MIT](LICENSE)
