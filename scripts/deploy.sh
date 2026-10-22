#!/bin/bash
# Update a git-pull deployment and rebuild:  scripts/deploy.sh
# From another machine:  ssh <server> '<checkout>/scripts/deploy.sh'
set -euo pipefail
export PATH=/usr/local/bin:/opt/homebrew/bin:$PATH
cd "$(dirname "$0")/.."

before=$(git rev-parse --short HEAD)
git pull --ff-only
after=$(git rev-parse --short HEAD)
[ "$before" = "$after" ] && echo "already at $after" || git log --oneline "$before..$after"

docker compose up -d --build
for _ in $(seq 1 20); do
    curl -sf localhost:8086/health >/dev/null && { echo "ereader-bridge up at $after"; exit 0; }
    sleep 2
done
echo "ereader-bridge did not become healthy" >&2
docker compose logs --tail=30
exit 1
