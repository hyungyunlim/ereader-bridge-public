"""ereader-bridge: queue files for LAN e-readers and deliver them when the device shows up.

Inputs:  SMTP (Kavita "Send to Device")  and  HTTP PUT /send/<device>/<filename>
Output:  the device's own Wi-Fi transfer server (BOOX Drop, ...), found by device id even if its IP changes.
"""
import concurrent.futures as cf
import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from collections import deque
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs
from urllib.request import Request, urlopen

import textsync

log = logging.getLogger("bridge")

# {"palma": {"driver": "boox", "id": "aa:bb:cc:dd:ee:ff", "host": "192.168.1.50"},
#  "moosnap": {"driver": "moosnap", "host": "192.168.1.51"}}   (boox "host" is only a first guess)
DEVICES = {k.lower(): v for k, v in json.loads(os.environ.get("DEVICES", "{}")).items()}
DATA = Path(os.environ.get("DATA_DIR", "/data"))
SUBNET = os.environ.get("SCAN_SUBNET", "192.168.1.0/24")
POLL = float(os.environ.get("POLL_SECONDS", "30"))
SCAN_EVERY = float(os.environ.get("SCAN_EVERY_SECONDS", "300"))
PROBE_TIMEOUT = float(os.environ.get("PROBE_TIMEOUT_SECONDS", "4"))  # e-ink Wi-Fi power save can take >1.5s
MAX_BYTES = int(os.environ.get("MAX_BYTES", str(1 << 30)))
SMTP_PORT = int(os.environ.get("SMTP_PORT", "2525"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8086"))
PLUGIN_DIR = Path(os.environ.get("PLUGIN_DIR", Path(__file__).parent / "koreader" / "bridgesync.koplugin"))
PAGE = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")  # GET / : queue page
SYNC_PAGE = (Path(__file__).parent / "web" / "sync.html").read_text(encoding="utf-8")  # GET /sync : paper -> EPUB
# e.g. a Home Assistant webhook: http://homeassistant.local:8123/api/webhook/<id>
NOTIFY_URL = os.environ.get("NOTIFY_URL", "")
NOTIFY_EVENTS = set(os.environ.get("NOTIFY_EVENTS", "queued,delivered,pulled").split(","))
SENT_KEEP_DAYS = float(os.environ.get("SENT_KEEP_DAYS", "7"))        # delivered copies kept for "resend"
QUEUE_EXPIRE_DAYS = float(os.environ.get("QUEUE_EXPIRE_DAYS", "30"))  # undelivered jobs dropped after this

wake = threading.Event()
history = deque(maxlen=200)  # newest first; also appended to DATA/history.jsonl and reloaded on start
# online/last_seen: the bridge can push to it. last_contact: the reader's plugin talked to us (pull/update check).
state = {name: {"online": False, "host": d.get("host"), "last_seen": None, "last_scan": 0.0, "last_contact": None}
         for name, d in DEVICES.items()}
first_try = set()  # queue files that have not failed yet (dup-check only applies to these)

# A job is handed out by push (worker) or pull (device plugin), never both at once.
# A pull claim that is never confirmed expires, so an abandoned download gets retried.
CLAIM_TTL = float(os.environ.get("PULL_CLAIM_TTL_SECONDS", "900"))
claims = {}  # job -> (owner, since)
claims_lock = threading.Lock()


def claim(job, owner):
    with claims_lock:
        held = claims.get(job)
        if held and held[0] != owner and time.time() - held[1] < CLAIM_TTL:
            return False
        claims[job] = (owner, time.time())
        return True


def release(job):
    with claims_lock:
        claims.pop(job, None)


def fetch(method, url, body=None, headers=None, timeout=10):
    with urlopen(Request(url, data=body, method=method, headers=headers or {}), timeout=timeout) as r:
        return r.read()


def http(method, url, body=None, headers=None, timeout=10):
    return json.loads(fetch(method, url, body, headers, timeout) or b"null")


def multipart(fields, file_field, name, path):
    b = uuid.uuid4().hex
    quoted = name.replace('"', "%22")
    head = "".join(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n' for k, v in fields.items())
    head += (f'--{b}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{quoted}"\r\n'
             f'Content-Type: application/octet-stream\r\n\r\n')
    # ponytail: whole file in memory; stream it if files ever exceed a few hundred MB
    return b, head.encode() + path.read_bytes() + f"\r\n--{b}--\r\n".encode()


# ---- drivers: probe(host, dev) -> bool, exists(host, dev, name) -> bool, upload(host, dev, name, path) ----

def boox_probe(host, dev):
    info = http("GET", f"http://{host}:{dev.get('port', 8085)}/api/device", timeout=PROBE_TIMEOUT)
    return info.get("id", "").lower() == dev["id"].lower()


def boox_exists(host, dev, name):
    body = {"selectedMap": {"Document": {"count": 0, "selectedAllMode": False, "selectedList": [name]}}}
    r = http("POST", f"http://{host}:{dev.get('port', 8085)}/api/storage/check/duplicate",
             json.dumps(body).encode(), {"Content-Type": "application/json"})
    return name in r["data"]["list"]


def boox_upload(host, dev, name, path):
    b, body = multipart({"sender": "ereader-bridge"}, "file", name, path)
    r = http("POST", f"http://{host}:{dev.get('port', 8085)}/api/library/upload", body,
             {"Content-Type": f"multipart/form-data; boundary={b}"}, timeout=600)
    if not r.get("successful"):
        raise RuntimeError(f"upload rejected: {r}")


# Moosnap "WiFi Import" (liteserver). No device-id API, so the page itself is the fingerprint.
# Moosnaps expose no identity (Ocean and Neo serve the same page, only the UI language differs),
# so a Moosnap's identity is its pinned "host" and it is never rescanned. Pin the IP in the router.
def moosnap_probe(host, dev):
    return b"<title>WiFi" in fetch("GET", f"http://{host}:{dev.get('port', 10123)}/", timeout=PROBE_TIMEOUT)


def moosnap_exists(host, dev, name):
    return False  # its getBooksList returns an empty reply; nothing to check against


def moosnap_upload(host, dev, name, path):
    b, body = multipart({"Filename": name}, "Filedata", name, path)
    body = body[:-len(f"--{b}--\r\n")] + f'--{b}\r\nContent-Disposition: form-data; name="Upload"\r\n\r\nSubmit Query\r\n--{b}--\r\n'.encode()
    # its web page sends the multipart body with this exact (boundary-less) content type
    r = http("POST", f"http://{host}:{dev.get('port', 10123)}/test.php?action=addBook", body,
             {"Content-Type": "application/octet-stream"}, timeout=600)
    if r.get("code") != 0:
        raise RuntimeError(f"upload rejected: {r}")


DRIVERS = {"boox": (boox_probe, boox_exists, boox_upload),
           "moosnap": (moosnap_probe, moosnap_exists, moosnap_upload)}
RESCANNABLE = {"boox"}  # drivers whose probe checks a device id, so a subnet scan finds the right device


# ---- queue ----

def safe_name(name):
    name = (name or "").replace("\\", "/").split("/")[-1].strip().replace("\x00", "")
    if name in ("", ".", ".."):
        raise ValueError("bad filename")
    return name


def qdir(device):
    d = DATA / "queue" / device
    d.mkdir(parents=True, exist_ok=True)
    return d


def notify(event, device, fname):
    if not NOTIFY_URL or event not in NOTIFY_EVENTS:
        return
    body = json.dumps({"event": event, "device": device, "file": fname}, ensure_ascii=False).encode()

    def send():
        try:
            fetch("POST", NOTIFY_URL, body, {"Content-Type": "application/json"}, timeout=5)
        except Exception as e:
            log.warning("notify %s failed: %s", event, e)
    threading.Thread(target=send, daemon=True).start()


def record(entry):
    history.appendleft(entry)
    with (DATA / "history.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_history():
    path = DATA / "history.jsonl"
    if not path.exists():
        return
    lines = path.read_text(encoding="utf-8").splitlines()[-history.maxlen:]
    path.write_text("".join(l + "\n" for l in lines), encoding="utf-8")  # keep the file as short as the deque
    history.extend(json.loads(l) for l in reversed(lines))


def enqueue(device, name, data):
    job = f"{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    tmp = qdir(device) / f".{job}.part"
    tmp.write_bytes(data)
    final = qdir(device) / f"{job}__{safe_name(name)}"
    tmp.rename(final)  # atomic: the worker never sees half-written files
    first_try.add(final.name)
    log.info("queued %s -> %s (%d bytes)", name, device, len(data))
    notify("queued", device, safe_name(name))
    wake.set()
    return job


def queued(device):
    return sorted(p for p in qdir(device).iterdir() if not p.name.startswith("."))


def sdir(device):
    d = DATA / "sent" / device
    d.mkdir(parents=True, exist_ok=True)
    return d


def sent(device):
    return sorted((p for p in sdir(device).iterdir() if not p.name.startswith(".")), reverse=True)


def plugin_manifest():
    meta = (PLUGIN_DIR / "_meta.lua").read_text(encoding="utf-8")
    version = re.search(r'version\s*=\s*"([^"]+)"', meta).group(1)
    files = [{"path": str(f.relative_to(PLUGIN_DIR)), "size": f.stat().st_size,
              "sha256": hashlib.sha256(f.read_bytes()).hexdigest()}
             for f in sorted(PLUGIN_DIR.rglob("*")) if f.is_file() and not f.name.startswith(".")]
    return {"version": version, "files": files}


def job_info(p):
    job, fname = p.name.split("__", 1)
    st = p.stat()
    return {"job": job, "file": fname, "size": st.st_size, "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime))}


def job_path(device, job):
    return next((p for p in queued(device) if p.name.startswith(job + "__")), None)


def job_status(device, job):
    if any(p.name.startswith(job + "__") for p in queued(device)):
        return "queued"
    return next((h["status"] for h in history if h["job"] == job), "unknown")


# ---- presence + delivery ----

def locate(name, dev):
    probe = DRIVERS[dev["driver"]][0]
    st = state[name]

    def ok(host):
        try:
            return probe(host, dev)
        except Exception:
            return False

    # last known host first, then the configured ones (e.g. home LAN IP, then WireGuard IP)
    for host in dict.fromkeys(h for h in (st["host"], dev.get("host"), *dev.get("alt_hosts", ())) if h):
        if ok(host):
            return host
    if dev["driver"] not in RESCANNABLE or time.time() - st["last_scan"] < SCAN_EVERY:
        return None
    st["last_scan"] = time.time()
    hosts = [str(h) for h in ipaddress.ip_network(SUBNET).hosts() if str(h) != st["host"]]
    with cf.ThreadPoolExecutor(64) as ex:
        for host, hit in zip(hosts, ex.map(ok, hosts)):
            if hit:
                log.info("%s found at %s", name, host)
                return host
    return None


def deliver_all(name, dev):
    _, exists, upload = DRIVERS[dev["driver"]]
    host = locate(name, dev)
    st = state[name]
    st["online"], st["host"] = bool(host), host or st["host"]
    if not host:
        return
    st["last_seen"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    for path in queued(name):
        job, fname = path.name.split("__", 1)
        if not claim(job, "push"):
            continue  # the device is pulling this one right now
        if not path.exists():
            release(job)  # finished by a pull in the meantime
            continue
        try:
            # retries skip the dup check so a half-written file from a dropped upload gets overwritten
            if path.name in first_try and exists(host, dev, fname):
                status = "duplicate"
            else:
                upload(host, dev, fname, path)
                status = "delivered"
        except Exception as e:
            release(job)
            first_try.discard(path.name)
            log.warning("deliver %s -> %s failed: %s", fname, name, e)
            return  # device probably went away; retry next round
        finish(name, path, status)


def finish(name, path, status, keep=True):
    job, fname = path.name.split("__", 1)
    try:
        if keep:  # keep a copy for a while so it can be resent
            path.rename(sdir(name) / path.name)
            os.utime(sdir(name) / path.name)  # age counts from delivery, not from enqueue
        else:
            path.unlink()
    except FileNotFoundError:
        pass
    first_try.discard(path.name)
    release(job)
    record({"job": job, "device": name, "file": fname, "status": status, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    notify(status, name, fname)
    log.info("%s %s -> %s", status, fname, name)


def housekeeping():
    now = time.time()
    for name in DEVICES:
        for p in sent(name):
            if now - p.stat().st_mtime > SENT_KEEP_DAYS * 86400:
                p.unlink(missing_ok=True)
        for p in queued(name):
            job = p.name.split("__", 1)[0]
            if now - p.stat().st_mtime > QUEUE_EXPIRE_DAYS * 86400 and claim(job, "expire"):
                finish(name, p, "expired", keep=False)


def cancel(device, job):
    path = job_path(device, job)
    if not path:
        return 404, job_status(device, job)
    if not claim(job, "cancel"):
        return 409, "being delivered right now"
    finish(device, path, "cancelled", keep=False)
    return 200, "cancelled"


def resend(device, job, to):
    src = next((p for p in sent(device) if p.name.startswith(job + "__")), None)
    if not src:
        return None
    return enqueue(to, src.name.split("__", 1)[1], src.read_bytes())


KEPT_STATE = ("host", "last_seen", "last_contact", "plugin")  # survives restarts/deploys (state.json)


def save_state():
    data = {n: {k: st.get(k) for k in KEPT_STATE} for n, st in state.items()}
    tmp = DATA / ".state.json.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(DATA / "state.json")


def load_state():
    try:
        saved = json.loads((DATA / "state.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return
    for n, kept in saved.items():
        if n in state:
            state[n].update({k: v for k, v in kept.items() if k in KEPT_STATE and v is not None})


def worker():
    last_housekeeping = 0.0
    while True:
        if time.time() - last_housekeeping > 3600:
            last_housekeeping = time.time()
            try:
                housekeeping()
            except Exception:
                log.exception("housekeeping failed")
        for name, dev in DEVICES.items():
            try:
                deliver_all(name, dev)
            except Exception:
                log.exception("worker error for %s", name)
        try:
            save_state()
        except OSError:
            log.exception("saving state failed")
        wake.wait(POLL)
        wake.clear()


# ---- HTTP ----

class Http(BaseHTTPRequestHandler):
    def reply(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def route(self):
        # request line is decoded as latin-1; recover raw UTF-8 (curl -T sends non-ASCII names unescaped)
        url = urlparse(self.path.encode("latin-1").decode("utf-8", "replace"))
        return [unquote(p) for p in url.path.split("/") if p], parse_qs(url.query)

    def do_GET(self):
        parts, q = self.route()
        if parts == ["health"]:
            return self.reply(200, {"ok": True})
        if parts in ([], ["sync"]):
            if parts:
                log.info("sync page: %d chars of text from %s", len(q.get("text", [""])[0]), self.client_address[0])
            body = (PAGE if not parts else SYNC_PAGE).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parts == ["status"]:
            return self.reply(200, {
                "devices": {n: {**state[n], "driver": d["driver"],
                                "queued": [p.name.split("__", 1)[1] for p in queued(n)],
                                "jobs": [job_info(p) for p in queued(n)],
                                "sent": [job_info(p) for p in sent(n)[:50]]}
                            for n, d in DEVICES.items()},
                "history": list(history)})
        if len(parts) == 3 and parts[0] == "jobs" and parts[1] in DEVICES:
            return self.reply(200, {"job": parts[2], "status": job_status(parts[1], parts[2])})
        if parts == ["locate", "books"]:
            if not textsync_ready():
                return self.reply(503, {"error": "textsync is not configured (KAVITA_URL/KAVITA_API_KEY)"})
            return self.reply(200, {"books": TEXTSYNC.recent_books()})
        # plugin self-update: version + file list, then each listed file
        if parts == ["plugin", "bridgesync", "manifest"]:
            m = plugin_manifest()
            device, version = q.get("device", [""])[0], q.get("version", [""])[0]
            if device in DEVICES:  # the plugin says who it is, so /status can show what each reader runs
                state[device]["plugin"] = {"version": version, "checked": time.strftime("%Y-%m-%dT%H:%M:%S")}
                state[device]["last_contact"] = state[device]["plugin"]["checked"]
                log.info("plugin check by %s: has %s, latest %s", device, version, m["version"])
            return self.reply(200, m)
        if len(parts) >= 3 and parts[:2] == ["plugin", "bridgesync"]:
            rel = "/".join(parts[2:])
            if rel not in {f["path"] for f in plugin_manifest()["files"]}:  # only listed files, no traversal
                return self.reply(404, {"error": "not found"})
            data = (PLUGIN_DIR / rel).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        # pull: the device fetches its own queue (works over VPN/NAT, unlike push)
        if len(parts) == 2 and parts[0] == "pull" and parts[1] in DEVICES:
            jobs = [{"job": p.name.split("__", 1)[0], "file": p.name.split("__", 1)[1], "size": p.stat().st_size}
                    for p in queued(parts[1])]
            state[parts[1]]["last_contact"] = time.strftime("%Y-%m-%dT%H:%M:%S")  # the reader itself is up (KOReader)
            log.info("pull check by %s from %s: %d queued", parts[1], self.client_address[0], len(jobs))
            return self.reply(200, {"jobs": jobs})
        if len(parts) == 3 and parts[0] == "pull" and parts[1] in DEVICES:
            path = job_path(parts[1], parts[2])
            if not path:
                return self.reply(404, {"error": "no such job", "status": job_status(parts[1], parts[2])})
            if not claim(parts[2], "pull"):
                return self.reply(409, {"error": "being pushed right now"})
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            with path.open("rb") as f:
                shutil.copyfileobj(f, self.wfile)
            return
        self.reply(404, {"error": "not found"})

    def do_DELETE(self):
        parts, _ = self.route()
        if len(parts) == 3 and parts[0] == "jobs" and parts[1] in DEVICES:
            code, status = cancel(parts[1], parts[2])
            return self.reply(code, {"job": parts[2], "status": status})
        self.reply(404, {"error": "not found"})

    def body_json(self):
        size = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(size) or b"{}") if size else {}

    def do_POST(self):
        parts, q = self.route()
        # paper -> EPUB: find an OCR'd page in the reader's EPUBs, then move Kavita's progress there
        if parts in (["locate"], ["locate", "apply"]):
            if not textsync_ready():
                return self.reply(503, {"error": "textsync is not configured (KAVITA_URL/KAVITA_API_KEY)"})
            try:
                b = self.body_json()
                if parts == ["locate"]:
                    return self.reply(200, TEXTSYNC.locate(b.get("text", ""), b.get("chapterId")))
                result = TEXTSYNC.apply(int(b["chapterId"]), int(b["pageNum"]), b.get("bookScrollId"))
                log.info("textsync: %s -> page %s %s", result["title"], b["pageNum"], b.get("bookScrollId"))
                return self.reply(200, result)
            except (KeyError, ValueError) as e:
                return self.reply(400, {"error": str(e)})
            except Exception as e:
                log.exception("textsync failed")
                return self.reply(502, {"error": f"Kavita: {e}"})
        if len(parts) == 4 and parts[0] == "jobs" and parts[1] in DEVICES and parts[3] == "resend":
            to = q.get("to", [parts[1]])[0]
            if to not in DEVICES:
                return self.reply(404, {"error": f"unknown device {to}"})
            job = resend(parts[1], parts[2], to)
            if not job:
                return self.reply(404, {"error": "no kept copy (older than SENT_KEEP_DAYS?)"})
            return self.reply(200, {"job": job, "device": to, "status": job_status(to, job)})
        # the plugin gave up on a download (user stopped it): free the job for push/pull right away
        if len(parts) == 4 and parts[0] == "pull" and parts[1] in DEVICES and parts[3] == "release":
            with claims_lock:
                if claims.get(parts[2], ("",))[0] == "pull":
                    claims.pop(parts[2])
            return self.reply(200, {"job": parts[2], "status": job_status(parts[1], parts[2])})
        # POST /pull/<device>/<job>/done once the file is safely on the device
        if len(parts) == 4 and parts[0] == "pull" and parts[1] in DEVICES and parts[3] == "done":
            path = job_path(parts[1], parts[2])
            if path:
                finish(parts[1], path, "pulled")
            return self.reply(200, {"job": parts[2], "status": job_status(parts[1], parts[2])})
        self.reply(404, {"error": "not found"})

    def do_PUT(self):
        parts, q = self.route()
        if len(parts) != 3 or parts[0] != "send":
            return self.reply(404, {"error": "use PUT /send/<device>/<filename>"})
        device, fname = parts[1], parts[2]
        if device not in DEVICES:
            return self.reply(404, {"error": f"unknown device {device}", "devices": list(DEVICES)})
        size = int(self.headers.get("Content-Length") or -1)
        if size < 0:
            return self.reply(411, {"error": "Content-Length required"})
        if size > MAX_BYTES:
            return self.reply(413, {"error": "file too large"})
        try:
            job = enqueue(device, fname, self.rfile.read(size))
        except ValueError as e:
            return self.reply(400, {"error": str(e)})
        deadline = time.time() + min(float(q.get("wait", ["0"])[0]), 120)
        while job_status(device, job) == "queued" and time.time() < deadline:
            time.sleep(0.5)
        self.reply(202, {"job": job, "device": device, "file": safe_name(fname), "status": job_status(device, job)})

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)


# ---- SMTP (Kavita) ----

class Smtp:
    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        if address.split("@")[0].lower() not in DEVICES:
            return "550 unknown device"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        msg = BytesParser(policy=policy.default).parsebytes(envelope.content)
        files = [(p.get_filename(), p.get_payload(decode=True)) for p in msg.iter_attachments() if p.get_filename()]
        if not files:
            return "554 no attachments"
        for rcpt in envelope.rcpt_tos:
            for name, data in files:
                enqueue(rcpt.split("@")[0].lower(), name, data)
        return "250 queued"


TEXTSYNC = None


def textsync_ready():
    return TEXTSYNC is not None


def main():
    global TEXTSYNC
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("mail.log").setLevel(logging.WARNING)  # aiosmtpd logs every SMTP command at INFO
    from aiosmtpd.controller import Controller
    for d in DEVICES.values():
        assert d["driver"] in DRIVERS, f"unknown driver {d['driver']}"
    DATA.mkdir(parents=True, exist_ok=True)
    for n in DEVICES:
        for p in qdir(n).glob(".*.part"):
            p.unlink()  # leftovers from a crash mid-enqueue
        first_try.update(p.name for p in queued(n))
    load_history()
    load_state()
    smtp = Controller(Smtp(), hostname="0.0.0.0", port=SMTP_PORT, server_hostname="ereader-bridge",
                      data_size_limit=int(MAX_BYTES * 1.4))  # base64 overhead
    smtp.start()
    threading.Thread(target=worker, daemon=True).start()
    if textsync.enabled():
        TEXTSYNC = textsync.TextSync(DATA)
        threading.Thread(target=TEXTSYNC.warm, daemon=True).start()
    log.info("devices=%s smtp=:%d http=:%d", list(DEVICES), SMTP_PORT, HTTP_PORT)
    ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Http).serve_forever()


if __name__ == "__main__":
    main()
