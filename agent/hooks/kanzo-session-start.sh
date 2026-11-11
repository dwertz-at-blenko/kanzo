#!/usr/bin/env bash
# Claude Code SessionStart hook: tells the agent which Kanzo project this repo is, and
# what is in flight on it, so the session starts from the board. Prints nothing when
# the repo has no project or the board is not reachable. Configure KANZO_API when the
# board is not at http://127.0.0.1:4242 (on the VPS: http://127.0.0.1:8111/api).
set -u
API="${KANZO_API:-http://127.0.0.1:4242/api}"
here=$(cd "$(dirname "$0")" && pwd)
remote=$(git -C "$PWD" remote get-url origin 2>/dev/null) || exit 0
repo=$(printf '%s' "$remote" | sed -E 's#^(https?://[^/]+/|git@[^:]+:|ssh://git@[^/]+/)##; s#\.git$##; s#/$##')
[ -n "$repo" ] || exit 0
board=$(curl -s -m 3 "$API/board" 2>/dev/null) || exit 0
[ -n "$board" ] || exit 0
printf '%s' "$board" | KANZO_REPO="$repo" python3 -I "$here/kanzo-session-start.py"
exit 0
