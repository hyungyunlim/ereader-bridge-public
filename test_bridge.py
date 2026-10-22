"""End-to-end self-check against fake BOOX / Moosnap servers. Run: python test_bridge.py"""
import json
import pathlib
import os
import smtplib
import socket
import tempfile
import threading
import time
import urllib.request
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


received = {"boox": [], "moosnap": []}


class FakeBoox(BaseHTTPRequestHandler):
    def do_GET(self):
        self.ok({"id": "AA:BB", "model": "Palma2"})

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path == "/api/storage/check/duplicate":
            names = json.loads(body)["selectedMap"]["Document"]["selectedList"]
            have = {n for n, _ in received["boox"]}
            return self.ok({"code": 0, "data": {"list": [n for n in names if n in have]}, "successful": True})
        assert self.headers["Content-Type"].startswith("multipart/form-data; boundary=")
        name = body.split(b'filename="')[1].split(b'"')[0].decode()
        received["boox"].append((name, body.split(b"\r\n\r\n", 2)[2].rsplit(b"\r\n--", 1)[0]))
        self.ok({"code": 0, "successful": True})

    def ok(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


class FakeMoosnap(FakeBoox):
    def do_GET(self):
        b = "<html><head><title>WiFi 传输</title></head></html>".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        assert self.path == "/test.php?action=addBook" and self.headers["Content-Type"] == "application/octet-stream"
        assert b'name="Upload"' in body and b'name="Filename"' in body
        received["moosnap"].append(body.split(b'filename="')[1].split(b'"')[0].decode())
        self.ok({"code": 0, "msg": "upload success"})


events = []


class FakeWebhook(BaseHTTPRequestHandler):
    def do_POST(self):
        events.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


def serve(handler, port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def get_json(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def put(url, data):
    req = urllib.request.Request(url, data=data, method="PUT")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def wait_for(cond, secs=10):
    end = time.time() + secs
    while time.time() < end:
        try:
            if cond():
                return True
        except OSError:
            pass
        time.sleep(0.2)
    return False


def demo():
    boox_port, moo_port, smtp_port, http_port, hook_port = (free_port() for _ in range(5))
    serve(FakeWebhook, hook_port)
    os.environ.update(
        DATA_DIR=tempfile.mkdtemp(), SCAN_SUBNET="127.0.0.1/32", POLL_SECONDS="0.5", SCAN_EVERY_SECONDS="0", PROBE_TIMEOUT_SECONDS="0.5", NOTIFY_URL=f"http://127.0.0.1:{hook_port}/hook",
        SMTP_PORT=str(smtp_port), HTTP_PORT=str(http_port), LOG_LEVEL="WARNING",
        DEVICES=json.dumps({"Palma2": {"driver": "boox", "id": "aa:bb", "host": "127.0.0.1", "port": boox_port},
                            "moosnap-neo": {"driver": "moosnap", "host": "127.0.0.1", "port": moo_port},
                            # wrong IP on purpose: must stay offline, not latch onto the Neo via a rescan
                            "moosnap-ocean": {"driver": "moosnap", "host": "127.0.0.2", "port": moo_port},
                            # home IP dead, VPN-style alternate address alive
                            "moosnap-vpn": {"driver": "moosnap", "host": "127.0.0.2", "alt_hosts": ["127.0.0.1"],
                                            "port": moo_port},
                            # push can never reach it (like Palma on VPN): only the device's own pull delivers
                            "pullbox": {"driver": "boox", "id": "zz", "host": "127.0.0.2", "port": boox_port}}))
    import bridge
    threading.Thread(target=bridge.main, daemon=True).start()
    base = f"http://127.0.0.1:{http_port}"
    assert wait_for(lambda: urllib.request.urlopen(base + "/health").status == 200)

    # 1. device offline: file waits in the queue
    r = put(f"{base}/send/palma2/{quote('파친코 1.epub')}?wait=1", b"EPUBDATA")
    assert r["status"] == "queued", r

    # 2. device comes online: queued file is delivered (Korean name intact)
    boox = serve(FakeBoox, boox_port)
    bridge.wake.set()
    assert wait_for(lambda: received["boox"] == [("파친코 1.epub", b"EPUBDATA")]), received
    assert wait_for(lambda: json.load(urllib.request.urlopen(f"{base}/jobs/palma2/{r['job']}"))["status"] == "delivered")

    # 3. same name again -> skipped as duplicate, not re-uploaded
    r = put(f"{base}/send/palma2/{quote('파친코 1.epub')}?wait=10", b"EPUBDATA")
    assert r["status"] == "duplicate" and len(received["boox"]) == 1, r

    # 4. moosnap driver (Chinese UI title); a Moosnap at the wrong IP is never matched to another one
    serve(FakeMoosnap, moo_port)
    r = put(f"{base}/send/moosnap-neo/book.txt?wait=10", b"hello")
    assert r["status"] == "delivered" and received["moosnap"] == ["book.txt"], r
    r = put(f"{base}/send/moosnap-ocean/other.txt?wait=3", b"x")
    assert r["status"] == "queued" and received["moosnap"] == ["book.txt"], r
    r = put(f"{base}/send/moosnap-vpn/vpn.txt?wait=10", b"v")
    assert r["status"] == "delivered" and received["moosnap"] == ["book.txt", "vpn.txt"], r

    # 5. SMTP from Kavita: attachment to known device, unknown device refused
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "kavita@bridge.local", "palma2@bridge.local", "Send to device"
    msg.set_content("body")
    msg.add_attachment(b"CBZDATA", maintype="application", subtype="zip", filename="원피스 v01.cbz")
    with smtplib.SMTP("127.0.0.1", smtp_port) as s:
        s.send_message(msg)
        try:
            s.sendmail("kavita@bridge.local", ["kindle@bridge.local"], "x")
            raise AssertionError("unknown device accepted")
        except smtplib.SMTPRecipientsRefused:
            pass
    assert wait_for(lambda: ("원피스 v01.cbz", b"CBZDATA") in received["boox"]), received

    # 6. path traversal in filename is neutralised
    r = put(f"{base}/send/moosnap-neo/..%2F..%2Fetc%2Fpasswd?wait=10", b"x")
    assert r["file"] == "passwd", r

    # 7. pull: device lists its queue, downloads, confirms; a pulled job is not pushed meanwhile
    r = put(f"{base}/send/pullbox/{quote('빵 1.epub')}?wait=1", b"PULLDATA")
    jobs = get_json(f"{base}/pull/pullbox")["jobs"]
    assert get_json(f"{base}/status")["devices"]["pullbox"]["last_contact"], "pull check must record contact"
    assert [(j["job"], j["file"], j["size"]) for j in jobs] == [(r["job"], "빵 1.epub", 8)], jobs
    assert urllib.request.urlopen(f"{base}/pull/pullbox/{r['job']}").read() == b"PULLDATA"
    assert not bridge.claim(r["job"], "push"), "push must not take a job that is being pulled"
    bridge.claims[r["job"]] = ("pull", time.time() - 10 ** 6)          # abandoned pull ...
    assert bridge.claim(r["job"], "push")                               # ... expires
    try:
        urllib.request.urlopen(f"{base}/pull/pullbox/{r['job']}")
        raise AssertionError("pull of a job held by push must be refused")
    except urllib.error.HTTPError as e:
        assert e.code == 409
    bridge.release(r["job"])
    done = json.load(urllib.request.urlopen(urllib.request.Request(f"{base}/pull/pullbox/{r['job']}/done", data=b"", method="POST")))
    assert done["status"] == "pulled" and get_json(f"{base}/pull/pullbox")["jobs"] == [], done
    try:
        urllib.request.urlopen(f"{base}/pull/pullbox/{r['job']}")
        raise AssertionError("finished job must be gone")
    except urllib.error.HTTPError as e:
        assert e.code == 404

    # 8. plugin self-update source: manifest matches the files, only listed files are served
    import hashlib, re
    plugin = pathlib.Path(__file__).parent / "koreader" / "bridgesync.koplugin"
    m = get_json(f"{base}/plugin/bridgesync/manifest?device=palma2&version=1.0")
    assert get_json(f"{base}/status")["devices"]["palma2"]["plugin"]["version"] == "1.0"
    assert m["version"] == re.search(r'version\s*=\s*"([^"]+)"', (plugin / "_meta.lua").read_text()).group(1), m
    assert sorted(f["path"] for f in m["files"]) == ["_meta.lua", "main.lua"], m
    for f in m["files"]:
        data = urllib.request.urlopen(f"{base}/plugin/bridgesync/{f['path']}").read()
        assert hashlib.sha256(data).hexdigest() == f["sha256"] and len(data) == f["size"], f
    try:
        urllib.request.urlopen(f"{base}/plugin/bridgesync/..%2F..%2Fbridge.py")
        raise AssertionError("unlisted path served")
    except urllib.error.HTTPError as e:
        assert e.code == 404

    # 9. history survives a restart (file is replayed newest-first)
    before = list(bridge.history)
    bridge.history.clear()
    bridge.load_history()
    assert list(bridge.history) == before and before[0]["status"] == "pulled", before[:2]

    # 10. webhook got queued/delivered/pulled events, not duplicates (not in NOTIFY_EVENTS)
    assert wait_for(lambda: {"event": "pulled", "device": "pullbox", "file": "빵 1.epub"} in events), events
    assert {"event": "delivered", "device": "moosnap-neo", "file": "book.txt"} in events, events
    assert {"event": "queued", "device": "palma2", "file": "파친코 1.epub"} in events, events
    assert not any(e["event"] == "duplicate" for e in events), events

    def send(method, url):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=b"" if method == "POST" else None, method=method)) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    # 11. delivered files are kept for a while and can be resent, also to another device
    kept = get_json(f"{base}/status")["devices"]["moosnap-neo"]["sent"]
    book = next(k for k in kept if k["file"] == "book.txt")
    code, r = send("POST", f"{base}/jobs/moosnap-neo/{book['job']}/resend?to=pullbox")
    assert code == 200 and r["status"] == "queued" and r["device"] == "pullbox", r
    assert [j["file"] for j in get_json(f"{base}/pull/pullbox")["jobs"]] == ["book.txt"]

    # 12. cancel: removes a queued job; refused while it is being handed out; unknown job is 404
    assert send("DELETE", f"{base}/jobs/pullbox/nope")[0] == 404
    urllib.request.urlopen(f"{base}/pull/pullbox/{r['job']}").read()          # device starts pulling ...
    assert send("DELETE", f"{base}/jobs/pullbox/{r['job']}")[0] == 409       # ... so cancel waits
    # 13. the plugin stopping a download releases its claim at once (no 15 min lock)
    assert send("POST", f"{base}/pull/pullbox/{r['job']}/release")[0] == 200
    code, c = send("DELETE", f"{base}/jobs/pullbox/{r['job']}")
    assert code == 200 and c["status"] == "cancelled" and get_json(f"{base}/pull/pullbox")["jobs"] == [], c
    assert get_json(f"{base}/jobs/pullbox/{r['job']}")["status"] == "cancelled"

    # 14. housekeeping: undelivered jobs expire, old kept copies are dropped
    r = put(f"{base}/send/pullbox/old.epub?wait=1", b"o")
    old = bridge.job_path("pullbox", r["job"])
    os.utime(old, (0, 0))
    for k in bridge.sent("moosnap-neo"):
        os.utime(k, (0, 0))
    bridge.housekeeping()
    assert get_json(f"{base}/jobs/pullbox/{r['job']}")["status"] == "expired"
    assert bridge.sent("moosnap-neo") == []

    # 16. device state (last contact, plugin version, host) survives a restart
    bridge.save_state()
    remembered = bridge.state["palma2"]["plugin"]
    bridge.state["palma2"].update(plugin=None, last_contact=None)
    bridge.load_state()
    assert bridge.state["palma2"]["plugin"] == remembered and bridge.state["pullbox"]["last_contact"]

    # 17. paper -> EPUB sync is off without Kavita settings (503), its page is still served
    assert send("POST", f"{base}/locate")[0] == 503
    assert "Paper book" in urllib.request.urlopen(base + "/sync").read().decode()

    # 15. queue page
    page = urllib.request.urlopen(base + "/").read().decode()
    assert "ereader-bridge" in page and "/status" in page

    status = json.load(urllib.request.urlopen(base + "/status"))
    assert status["devices"]["palma2"]["online"] and status["devices"]["palma2"]["queued"] == []
    boox.shutdown()
    print("ok: all checks passed")


if __name__ == "__main__":
    demo()
