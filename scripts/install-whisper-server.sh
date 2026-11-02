#!/bin/bash
# Run whisper.cpp's server on this Mac (Apple Silicon GPU) as a login service, for audiobook sync.
# Docker on macOS can't use the GPU, so the bridge calls this from its container via host.docker.internal.
#   brew install whisper-cpp ffmpeg
#   scripts/install-whisper-server.sh            # downloads the model on first run
set -euo pipefail
MODEL_DIR="$HOME/whisper-models"
MODEL="$MODEL_DIR/ggml-large-v3-turbo-q5_0.bin"
PORT=8178
LABEL=org.ereader-bridge.whisper-server
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
BIN=$(command -v whisper-server || echo /opt/homebrew/bin/whisper-server)
[ -x "$BIN" ] || { echo "whisper-server not found: brew install whisper-cpp" >&2; exit 1; }

mkdir -p "$MODEL_DIR" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
[ -f "$MODEL" ] || curl -fL -o "$MODEL" https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$BIN</string>
    <string>-m</string><string>$MODEL</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>$PORT</string>
    <string>-l</string><string>auto</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/whisper-server.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/whisper-server.log</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
for _ in $(seq 1 30); do
    curl -sf "http://127.0.0.1:$PORT/" >/dev/null && { echo "whisper-server up on 127.0.0.1:$PORT"; exit 0; }
    sleep 1
done
echo "whisper-server did not start; see ~/Library/Logs/whisper-server.log" >&2
exit 1
