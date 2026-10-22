"""Push a local directory tree onto a BOOX device via BOOX Drop, staged then renamed.

usage: python -I boox_push.py <host:port> <local_dir> <remote_parent> <final_name> [--probe]
"""
import json, os, sys, uuid, hashlib, urllib.request, urllib.parse, urllib.error

H, LOCAL, PARENT, FINAL = "http://" + sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
PROBE = "--probe" in sys.argv
STAGE = FINAL + "-installing"


def call(method, path, body=None, headers=None, timeout=60):
    req = urllib.request.Request(H + path, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def jpost(path, obj):
    return json.loads(call("POST", path, json.dumps(obj).encode(), {"Content-Type": "application/json"}))


def ls(d):
    q = urllib.parse.quote(json.dumps({"limit": 1000, "offset": 0, "dir": d, "refresh": True}))
    return {e["name"]: e for e in json.loads(call("GET", f"/api/storage?args={q}"))["data"]["list"]}


def mkdir(parent, name):
    if name not in ls(parent):
        r = jpost("/api/storage/directory", {"parent": parent, "name": name})
        assert r.get("successful") or r.get("code") == 0, r


def upload(remote_dir, path):
    b = uuid.uuid4().hex
    name = os.path.basename(path)
    body = (f'--{b}\r\nContent-Disposition: form-data; name="dir"\r\n\r\n{remote_dir}\r\n'
            f'--{b}\r\nContent-Disposition: form-data; name="sender"\r\n\r\nereader-bridge\r\n'
            f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
            f'Content-Type: application/octet-stream\r\n\r\n').encode() + open(path, "rb").read() + f"\r\n--{b}--\r\n".encode()
    r = json.loads(call("POST", "/api/storage/upload", body, {"Content-Type": f"multipart/form-data; boundary={b}"}, 300))
    assert r.get("successful") or r.get("code") == 0, r


def fetch(remote_path):
    return call("GET", f"/api/storage/file?args={urllib.parse.quote(remote_path)}&sender=ereader-bridge")


assert FINAL not in ls(PARENT), f"{PARENT}/{FINAL} already exists; refusing to overwrite"
mkdir(PARENT, STAGE)
root = f"{PARENT}/{STAGE}"
files = []
for d, dirs, fs in os.walk(LOCAL):
    rel = os.path.relpath(d, LOCAL)
    remote = root if rel == "." else f"{root}/{rel}"
    for sub in sorted(dirs):
        mkdir(remote, sub)
    files += [(remote, os.path.join(d, f)) for f in sorted(fs)]

if PROBE:  # one text + one binary file, then byte-compare
    files = [next(f for f in files if f[1].endswith("_meta.lua")), next(f for f in files if f[1].endswith(".ttf"))]

existing = {}
for i, (remote, path) in enumerate(files, 1):
    have = existing.setdefault(remote, ls(remote))
    e = have.get(os.path.basename(path))
    if not (e and e["size"] == os.path.getsize(path)):  # resume: skip files already pushed (e.g. by --probe)
        upload(remote, path)
    if i % 25 == 0 or i == len(files):
        print(f"uploaded {i}/{len(files)}", flush=True)

# verify every file: size via listing, bytes via sha256 of a download
bad = []
for remote, path in files:
    name = os.path.basename(path)
    local = open(path, "rb").read()
    if hashlib.sha256(fetch(f"{remote}/{name}")).digest() != hashlib.sha256(local).digest():
        bad.append(f"{remote}/{name}")
print("verified", len(files) - len(bad), "of", len(files), "bad:", bad[:5])
if bad or PROBE:
    sys.exit(1 if bad else 0)

r = jpost("/api/storage/rename", {"file": root, "name": FINAL})
assert r.get("successful"), r
print("renamed to", f"{PARENT}/{FINAL}")
