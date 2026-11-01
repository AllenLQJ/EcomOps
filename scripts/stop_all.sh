#!/usr/bin/env bash
cd "$(dirname "$0")/.."
for f in .run/*.pid; do [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null; rm -f "$f"; done
[ "${DOWN_DB:-0}" = 1 ] && docker compose -f deploy/docker-compose.yml down
echo stopped
