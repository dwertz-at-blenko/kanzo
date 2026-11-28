#!/bin/bash
# Version: v2 · created 2026-11-24 · updated 2026-11-28 15:12 EST
# Changes: v1 start kanzo-dispatch under the vault runner so the Web Push key reaches it, with
#          every other injected variable dropped first; modelled on run-kanzo.sh.
#          v2 2026-11-28 --stop: the unit's ExecStop. systemd stops only `bws` (KillMode=process),
#          and `bws run` does not pass the signal on, so the dispatcher outlived every restart.
# This file: /home/claude/dev/kanzo/deploy/run-dispatch.sh (repo dwertz-at-blenko/kanzo, branch public-url)
# Session: 01TaShkAPU6jiD9Z2rT831EH · vps as claude · cwd /home/claude/dev
#
# ExecStart of kanzo-dispatch.service; its arguments go to kanzo_dispatch.py. Two passes of this file:
#   outer: load the vault machine token, then re-run this file under `bws run`.
#   inner: drop every environment variable except a short allowlist, then exec the dispatcher.
#          KANZO_VAPID_PRIVATE_KEY survives; the dispatcher takes it out of its own environment
#          before anything else runs, so claude, tmux and herdr never inherit it.
# The vault is optional: if it cannot be reached the dispatcher still starts, with push off.
#   --stop: end the running dispatcher (the pid in its lock file), wait for it, nothing else.
set -euo pipefail
set +x

SELF=/home/claude/dev/kanzo/deploy/run-dispatch.sh
SCRIPT=/home/claude/dev/kanzo/agent/dispatch/kanzo_dispatch.py
PROJECT=9b09cc48-0a19-426c-b1fb-b4a2007f028a
BWS=/usr/local/bin/bws

if [ "${1:-}" = "--stop" ]; then
  lock="${KANZO_HOME:-$HOME/.local/share/kanzo}/dispatch/state.json.lock"
  pid=$(head -n 1 "$lock" 2>/dev/null | tr -cd '0-9') || true
  # Only a process that is this dispatcher: the pid in an old lock file may belong to anything now.
  if [ -n "${pid:-}" ] && tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "kanzo_dispatch.py"; then
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 50); do kill -0 "$pid" 2>/dev/null || exit 0; sleep 0.2; done
    kill -9 "$pid" 2>/dev/null || true
  fi
  exit 0
fi

if [ "${1:-}" = "--inner" ]; then
  shift
  # What the unit gave the dispatcher and its claude children before (names read from the running
  # unit on 2026-11-24), minus systemd's bookkeeping; KANZO_* minus the board's own bearer.
  for v in $(compgen -e); do
    case "$v" in
      KANZO_REMOTE_BEARER) unset "$v" 2>/dev/null || true ;;
      HOME|PATH|USER|LOGNAME|LANG|LC_ALL|SHELL|XDG_RUNTIME_DIR|XDG_DATA_DIRS|DBUS_SESSION_BUS_ADDRESS|SSH_AUTH_SOCK|JOURNAL_STREAM|INVOCATION_ID|KANZO_*) ;;
      *) unset "$v" 2>/dev/null || true ;;
    esac
  done
  exec /usr/bin/python3 "$SCRIPT" "$@"
fi

if [ -x "$BWS" ] && [ -r /home/claude/.secrets/bws.env ]; then
  set -a
  . /home/claude/.secrets/bws.env
  set +a
  if "$BWS" run --project-id "$PROJECT" -- true >/dev/null 2>&1; then
    exec "$BWS" run --project-id "$PROJECT" -- "$SELF" --inner "$@"
  fi
fi
echo "kanzo-dispatch: vault not reachable, starting with push off" >&2
exec "$SELF" --inner "$@"
