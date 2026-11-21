#!/bin/bash
# Version: v1 · created 2026-11-21 07:20 EST · updated 2026-11-21 07:20 EST
# Changes: v1 the refusals of deploy.sh --check, on a throwaway repo and data folder.
# This file: /home/claude/dev/kanzo/deploy/test-deploy.sh (repo dwertz-at-blenko/kanzo, branch public-url)
# Session: b1475961-06d7-40b0-9eff-f2b47896b6bb · vps as claude · cwd /home/claude/dev
# Resume: ssh -t blenko-claude 'cd /home/claude/dev && claude --resume b1475961-06d7-40b0-9eff-f2b47896b6bb'
#
# Proves deploy.sh's refusals without touching the live board: a throwaway repo with a
# public-url branch and a bare origin, a throwaway data folder, and `deploy.sh --check`
# pointed at them through the KANZO_DEPLOY_* overrides. Prints PASS n/n.
set -euo pipefail

DEPLOY=$(cd "$(dirname "$0")" && pwd)/deploy.sh
ROOT=${CLAUDE_JOB_DIR:-$HOME/scratch}/tmp
mkdir -p "$ROOT"
T=$(mktemp -d "$ROOT/kanzo-deploy-test.XXXXXX")
SERVER=
cleanup() {
  if [ -n "$SERVER" ]; then kill "$SERVER" 2>/dev/null || true; fi
  rm -rf "$T"
}
trap cleanup EXIT

r() { git -C "$T/repo" -c user.name=test -c user.email=test@example.invalid -c commit.gpgsign=false "$@"; }
commit() { r add -A && r commit -q -m "$1" && r rev-parse HEAD; }

git init -q --bare -b public-url "$T/origin.git"
git init -q -b public-url "$T/repo"
mkdir -p "$T/repo/deploy" "$T/data"
printf '# test features\nboard page | / | 200 | activity-dialog\nwarp stars | /stars.js | 200\n' >"$T/repo/deploy/features.txt"
echo one >"$T/repo/file.txt"
BASE=$(commit "chore: base")
r remote add origin "$T/origin.git"
r checkout -q -b side
echo side >"$T/repo/side.txt"
SIDE=$(commit "feat: side work never merged")
r push -q origin side
r checkout -q public-url
echo two >>"$T/repo/file.txt"
SECOND=$(commit "feat: second")
r push -q origin public-url

N=0 P=0
# expect pass|refuse "<text the output must contain>" "<case>" [VAR=value...] [-- deploy.sh args]
expect() {
  local want=$1 text=$2 name=$3 envs=() out status=0
  shift 3
  while [ $# -gt 0 ] && [ "$1" != -- ]; do envs+=("$1") && shift; done
  [ $# -eq 0 ] || shift
  out=$(env KANZO_DEPLOY_REPO="$T/repo" KANZO_DEPLOY_DATA="$T/data" KANZO_DEPLOY_URL=http://127.0.0.1:9 \
    KANZO_DEPLOY_EDGE=http://127.0.0.1:9 KANZO_DEPLOY_UNIT=kanzo-deploy-test "${envs[@]}" \
    "$DEPLOY" --check "$@" 2>&1) || status=$?
  N=$((N + 1))
  if { [ "$want" = pass ] && [ $status -eq 0 ]; } || { [ "$want" = refuse ] && [ $status -eq 1 ]; }; then
    if grep -qF -- "$text" <<<"$out"; then
      P=$((P + 1))
      echo "PASS $name"
      return
    fi
  fi
  echo "FAIL $name (exit $status, wanted $want with: $text)"
  sed 's/^/    /' <<<"$out"
}

# 1. The board runs an ancestor of HEAD, or HEAD itself.
expect pass "check passed: ${SECOND:0:12} can replace ${BASE:0:12}" "live is an ancestor" KANZO_DEPLOY_LIVE_COMMIT="$BASE"
expect pass "check passed" "live is HEAD, by short sha" KANZO_DEPLOY_LIVE_COMMIT="${SECOND:0:7}"

# 2. The board runs a commit from a branch that was never merged.
expect refuse "feat: side work never merged" "live is on an unmerged branch: names the commit" KANZO_DEPLOY_LIVE_COMMIT="$SIDE"
expect refuse "remotes/origin/side" "live is on an unmerged branch: names the branch" KANZO_DEPLOY_LIVE_COMMIT="$SIDE"

# 3. The board was built outside this script.
expect refuse "from no known commit (unknown)" "live is unknown" KANZO_DEPLOY_LIVE_COMMIT=unknown
expect refuse "from $BASE plus uncommitted changes" "live is dirty" KANZO_DEPLOY_LIVE_COMMIT="$BASE-dirty"
expect refuse "a commit $T/repo does not have" "live is a commit this repo lacks" \
  KANZO_DEPLOY_LIVE_COMMIT=0123456789abcdef0123456789abcdef01234567
expect refuse "is not a commit sha" "live is not a sha" KANZO_DEPLOY_LIVE_COMMIT=HEAD

# 5. Uncommitted changes to a tracked file; untracked files are fine.
echo dirty >>"$T/repo/file.txt"
expect refuse "uncommitted changes" "dirty tree" KANZO_DEPLOY_LIVE_COMMIT="$BASE"
r checkout -q -- file.txt
echo scratch >"$T/repo/untracked.txt"
expect pass "check passed" "untracked file only" KANZO_DEPLOY_LIVE_COMMIT="$BASE"
rm "$T/repo/untracked.txt"

# 6. HEAD not pushed, and the wrong branch.
echo three >>"$T/repo/file.txt"
THIRD=$(commit "feat: third")
expect refuse "is not origin/public-url" "HEAD not pushed" KANZO_DEPLOY_LIVE_COMMIT="$BASE"
r push -q origin public-url
expect pass "check passed" "HEAD pushed" KANZO_DEPLOY_LIVE_COMMIT="$SECOND"
r checkout -q side
expect refuse "is on side, deploys ship public-url" "wrong branch" KANZO_DEPLOY_LIVE_COMMIT="$BASE"
r checkout -q public-url

# 4. A feature line removed: refused, unless named with --drop-feature; a bad line is refused.
printf '# test features\nboard page | / | 200 | activity-dialog\n' >"$T/repo/deploy/features.txt"
DROPPED=$(commit "feat: drop the stars")
r push -q origin public-url
expect refuse '"warp stars"' "a feature removed" KANZO_DEPLOY_LIVE_COMMIT="$THIRD"
expect pass 'dropping "warp stars"' "a feature removed with --drop-feature" \
  KANZO_DEPLOY_LIVE_COMMIT="$THIRD" -- --drop-feature "warp stars"
expect refuse '"warp stars"' "--drop-feature names another feature" \
  KANZO_DEPLOY_LIVE_COMMIT="$THIRD" -- --drop-feature "board page"
printf 'board page | / | ok\n' >"$T/repo/deploy/features.txt"
commit "chore: break the list" >/dev/null
r push -q origin public-url
expect refuse "line 1 is not" "a bad features line" KANZO_DEPLOY_LIVE_COMMIT="$DROPPED"
printf '# test features\nboard page | / | 200 | activity-dialog\nwarp stars | /stars.js | 200\n' >"$T/repo/deploy/features.txt"
commit "feat: the stars are back" >/dev/null
r push -q origin public-url

# b. The live commit read from the board, or from DATA/deployed when the board predates /api/version.
python3 -I -c '
import http.server, os, sys
class Board(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = os.path.join(sys.argv[1], "version.json")
        if self.path != "/api/version" or not os.path.exists(path):
            return self.send_error(404)
        body = open(path, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
server = http.server.HTTPServer(("127.0.0.1", 0), Board)
with open(os.path.join(sys.argv[1], "port.tmp"), "w") as f:
    f.write(str(server.server_port))
os.rename(os.path.join(sys.argv[1], "port.tmp"), os.path.join(sys.argv[1], "port"))
server.serve_forever()
' "$T" &
SERVER=$!
for _ in $(seq 50); do [ -s "$T/port" ] && break || sleep 0.1; done
BOARD=http://127.0.0.1:$(cat "$T/port")
expect refuse "run once with KANZO_DEPLOY_LIVE_COMMIT=<sha>" "board predates /api/version, no record" \
  KANZO_DEPLOY_URL="$BOARD"
printf '2026-11-20T10:00:00-05:00 %s %s\n' "$BASE" "$BASE" >"$T/data/deployed"
expect pass "live commit ${BASE:0:12} (from $T/data/deployed)" "board predates /api/version, recorded" \
  KANZO_DEPLOY_URL="$BOARD"
printf '{"commit":"%s","dirty":false,"version":"0.3.1"}' "$SECOND" >"$T/version.json"
expect pass "live commit ${SECOND:0:12} (from $BOARD/api/version)" "board names its commit" KANZO_DEPLOY_URL="$BOARD"
printf '{"commit":"%s","dirty":true,"version":"0.3.1"}' "$SECOND" >"$T/version.json"
expect refuse "plus uncommitted changes" "board names a dirty commit" KANZO_DEPLOY_URL="$BOARD"
expect refuse "no board answers at http://127.0.0.1:9" "no board answers" KANZO_DEPLOY_URL=http://127.0.0.1:9

# The test-only skips never point at the live data.
expect refuse "KANZO_DEPLOY_SKIP_* is for tests" "skips refused on the live data" \
  KANZO_DEPLOY_DATA=/home/claude/.local/share/kanzo KANZO_DEPLOY_SKIP_SERVICE=1 KANZO_DEPLOY_LIVE_COMMIT="$BASE"

[ $P -eq $N ] && echo "PASS $P/$N" || { echo "FAIL $((N - P)) of $N"; exit 1; }
