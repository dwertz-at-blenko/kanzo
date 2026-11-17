#!/bin/bash
# Version: v1 · created 2026-11-17 10:25 EST · updated 2026-11-17 10:25 EST
# Changes: v1 start the board under the vault runner so the cloud route's bearer reaches it,
#          with every other injected variable dropped before kanzo starts.
# This file: /home/claude/dev/kanzo/deploy/run-kanzo.sh (repo dwertz-at-blenko/kanzo, branch public-url)
# Session: b1475961-06d7-40b0-9eff-f2b47896b6bb · vps as claude · cwd /home/claude/dev
# Resume: ssh -t blenko-claude 'cd /home/claude/dev && claude --resume b1475961-06d7-40b0-9eff-f2b47896b6bb'
#
# ExecStart of kanzo-web.service. Two passes of this one file:
#   outer: load the vault machine token, then re-run this file under `bws run`.
#   inner: drop every environment variable except a short allowlist (KANZO_* included, so the
#          cloud bearer survives and nothing else from the vault does), then exec kanzo.
# The vault is optional: if it cannot be reached the board still starts, with the cloud route
# off (kanzo answers 404 on /mcp-remote when its bearer variable is unset).
set -euo pipefail
set +x

SELF=/home/claude/dev/kanzo/deploy/run-kanzo.sh
BIN=/home/claude/dev/kanzo/target/release/kanzo
DATA=/home/claude/.local/share/kanzo
PROJECT=9b09cc48-0a19-426c-b1fb-b4a2007f028a
BWS=/usr/local/bin/bws

if [ "${1:-}" = "--inner" ]; then
  for v in $(compgen -e); do
    case "$v" in
      HOME|PATH|USER|LOGNAME|LANG|LC_ALL|XDG_RUNTIME_DIR|KANZO_*) ;;
      *) unset "$v" 2>/dev/null || true ;;
    esac
  done
  exec "$BIN" --no-open --home "$DATA"
fi

if [ -r /home/claude/.secrets/bws.env ]; then
  set -a
  . /home/claude/.secrets/bws.env
  set +a
  if "$BWS" run --project-id "$PROJECT" -- true >/dev/null 2>&1; then
    exec "$BWS" run --project-id "$PROJECT" -- "$SELF" --inner
  fi
fi
echo "kanzo-web: vault not reachable, starting with the cloud route off" >&2
exec "$SELF" --inner
