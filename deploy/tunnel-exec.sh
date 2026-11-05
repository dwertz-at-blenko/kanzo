#!/usr/bin/env bash
# Version: v1 · created 2026-11-05 18:55 EST · updated 2026-11-05 18:55 EST
# Changes: v1 initial: kanzo tunnel launcher.
# This file: /home/claude/dev/kanzo/deploy/tunnel-exec.sh
# Session: 08cf0c04-265c-4b64-8e18-d5679b1a650d · vps as claude · cwd /home/claude/dev
# Resume: ssh -t blenko-claude 'cd /home/claude/dev && claude --resume 08cf0c04-265c-4b64-8e18-d5679b1a650d'   (from the Mac)
# Launch cloudflared for tunnel kanzo (kanzo.blenko.cloud). Called by kanzo-tunnel.service.
#
# The token comes from the 0600 file cf-site.py --apply wrote. It goes through the
# environment, never argv: argv is world readable via `ps`, and cloudflared reads
# TUNNEL_TOKEN for `tunnel run`. `$(<file)` is a bash builtin read, no subprocess.
set -euo pipefail
set +x

CRED="$HOME/.cloudflared/kanzo.json"
BIN="$HOME/.local/bin/cloudflared"

[ -x "$BIN" ] || { echo "kanzo-tunnel: no cloudflared binary at $BIN" >&2; exit 1; }
[ -r "$CRED" ] || { echo "kanzo-tunnel: no run token at $CRED (run cf-site.sh --apply)" >&2; exit 1; }

TUNNEL_TOKEN="$(<"$CRED")"
export TUNNEL_TOKEN

exec "$BIN" --no-autoupdate tunnel run
