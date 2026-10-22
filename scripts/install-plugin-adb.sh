#!/bin/bash
# First-time install of bridgesync on an Android reader over adb (later versions update themselves).
#   scripts/install-plugin-adb.sh <adb-serial> <bridge-url> <bridge-device-name> <receive-dir>
#   e.g. scripts/install-plugin-adb.sh 0123456789ABCDEF http://192.168.1.10:8086 moosnap /storage/emulated/0/Books
set -euo pipefail
serial=$1 url=$2 device=$3 dir=$4
export ANDROID_SERIAL=$serial
src="$(cd "$(dirname "$0")/.." && pwd)/koreader/bridgesync.koplugin"
plugins=/sdcard/koreader/plugins
settings=/sdcard/koreader/settings/bridgesync.lua

# KOReader rewrites its settings on exit; stop it first (HOME first so the open book's position is saved)
adb shell input keyevent KEYCODE_HOME; sleep 3
adb shell am force-stop org.koreader.launcher; sleep 1
if adb shell pidof org.koreader.launcher >/dev/null; then echo "KOReader still running" >&2; exit 1; fi

adb shell rm -rf "$plugins/bridgesync.koplugin-installing"
adb push "$src" "$plugins/bridgesync.koplugin-installing" >/dev/null
local_sha=$(cd "$src" && find . -type f -exec shasum -a 256 {} + | sort -k2)
remote_sha=$(adb shell "cd $plugins/bridgesync.koplugin-installing && find . -type f -exec sha256sum {} +" | tr -d '\r' | sort -k2)
[ "$local_sha" = "$remote_sha" ] || { echo "hash mismatch after push" >&2; exit 1; }
adb shell "rm -rf $plugins/bridgesync.koplugin && mv $plugins/bridgesync.koplugin-installing $plugins/bridgesync.koplugin"

if ! adb shell "[ -f $settings ]"; then
    tmp=$(mktemp)
    printf 'return {\n    ["url"] = "%s",\n    ["device"] = "%s",\n    ["dir"] = "%s",\n}\n' "$url" "$device" "$dir" > "$tmp"
    adb push "$tmp" "$settings" >/dev/null && rm -f "$tmp"
fi
adb shell am start -n org.koreader.launcher/.MainActivity >/dev/null
echo "bridgesync installed for $device; KOReader restarted"
