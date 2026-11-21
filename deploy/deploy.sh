#!/bin/bash
# Version: v1 · created 2026-11-21 07:20 EST · updated 2026-11-21 07:20 EST
# Changes: v1 the deploy guard: ship only the pushed public-url, never drop a live feature, roll back.
# This file: /home/claude/dev/kanzo/deploy/deploy.sh (repo dwertz-at-blenko/kanzo, branch public-url)
# Session: b1475961-06d7-40b0-9eff-f2b47896b6bb · vps as claude · cwd /home/claude/dev
# Resume: ssh -t blenko-claude 'cd /home/claude/dev && claude --resume b1475961-06d7-40b0-9eff-f2b47896b6bb'
#
# The only way to ship Kanzo to the live board (kanzo.blenko.cloud). It ships the pushed tip of
# public-url, and refuses when the running board has commits that tip lacks, or when
# deploy/features.txt lost a feature the running board has. Then: cargo fmt, clippy and test,
# a rollback binary and a database copy, the release build, a restart, every feature check,
# the listeners and the edge; any failure after the build rolls back.
#
#   deploy/deploy.sh                       ship
#   deploy/deploy.sh --check               preflight only: changes nothing
#   deploy/deploy.sh --drop-feature NAME   allow NAME to leave features.txt (repeatable)
#
# Tests only: each constant below can be replaced by KANZO_DEPLOY_<NAME>, KANZO_DEPLOY_LIVE_COMMIT
# replaces the commit read from the running board, and, with KANZO_DEPLOY_DATA pointing away from
# the live data, KANZO_DEPLOY_SKIP_BUILD=1 skips the build and KANZO_DEPLOY_SKIP_SERVICE=1 the
# restart, listener and edge steps. deploy/test-deploy.sh proves the refusals.
set -euo pipefail

REPO=${KANZO_DEPLOY_REPO:-/home/claude/dev/kanzo}
BRANCH=${KANZO_DEPLOY_BRANCH:-public-url}
DATA=${KANZO_DEPLOY_DATA:-/home/claude/.local/share/kanzo}
URL=${KANZO_DEPLOY_URL:-http://127.0.0.1:8111}
EDGE=${KANZO_DEPLOY_EDGE:-https://kanzo.blenko.cloud}
UNIT=${KANZO_DEPLOY_UNIT:-kanzo-web}

LIVE_DATA=/home/claude/.local/share/kanzo
BIN=$REPO/target/release/kanzo
FEATURES=deploy/features.txt
PORT=${URL##*:}
PORT=${PORT%%/*}
TMPROOT=${CLAUDE_JOB_DIR:-$HOME/scratch}/tmp
PATH=$HOME/.cargo/bin:$PATH
SKIP_BUILD=${KANZO_DEPLOY_SKIP_BUILD:-}
SKIP_SERVICE=${KANZO_DEPLOY_SKIP_SERVICE:-}

CHANGED=0 # 1 once the build may have replaced the binary: a failure from then on rolls back
HEAD= LIVE= ROLLBACK= LOG= WHY=
DROP=()

say() { printf 'ok  %s\n' "$*"; }
flag() { printf 'FAIL %s\n' "$*" >&2; }
fail() {
  flag "$@"
  if [ "$CHANGED" = 1 ]; then rollback; fi
  exit 1
}
g() { git -C "$REPO" "$@"; }

usage() {
  cat <<EOF
usage: deploy/deploy.sh [--check] [--drop-feature NAME]...
Ships the pushed tip of $BRANCH in $REPO to the board ($UNIT, $URL).
  --check               preflight only (branch, clean, pushed, live commit, features), changes nothing
  --drop-feature NAME   the one way to let a feature leave deploy/features.txt; repeatable
EOF
}

trim() {
  local s=$1
  s=${s#"${s%%[![:space:]]*}"}
  printf '%s' "${s%"${s##*[![:space:]]}"}"
}

# A features.txt on stdin as tab-separated name, path, status, text; a bad line fails.
features() {
  local line name path status text n=0
  while IFS= read -r line || [ -n "$line" ]; do
    n=$((n + 1))
    line=${line%$'\r'}
    case "$(trim "$line")" in '' | '#'*) continue ;; esac
    IFS='|' read -r name path status text <<<"$line"
    name=$(trim "$name") path=$(trim "${path:-}") status=$(trim "${status:-}") text=$(trim "${text:-}")
    if [ -z "$name" ] || [ "${path#/}" = "$path" ] || ! [[ $status =~ ^(![0-9]{3}|[0-9]{3}(,[0-9]{3})*)$ ]]; then
      flag "features: line $n is not 'name | /path | status | text': $line"
      return 1
    fi
    printf '%s\t%s\t%s\t%s\n' "$name" "$path" "$status" "$text"
  done
}

# Feature names only, without judging the lines: for an older features.txt.
feature_names() {
  local line
  while IFS= read -r line || [ -n "$line" ]; do
    line=$(trim "${line%%|*}")
    case "$line" in '' | '#'*) ;; *) printf '%s\n' "$line" ;; esac
  done
}

# The commit an /api/version reply on stdin names, `<sha>` or `<sha>-dirty`; empty when none.
commit_in() {
  local v c
  v=$(cat)
  c=$(sed -n 's/.*"commit":"\([0-9A-Za-z]*\)".*/\1/p' <<<"$v")
  [ -n "$c" ] || return 0
  case "$v" in *'"dirty":true'*) c=$c-dirty ;; esac
  printf '%s' "$c"
}

served_commit() { curl -s --max-time 3 "$URL/api/version" | commit_in || true; }

# The pid of the process listening on PORT (the kanzo binary, not the unit's main process).
listener_pid() {
  ss -ltnpH "sport = :$PORT" 2>/dev/null | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | head -n 1
}

stamp_of() {
  "$1" --version 2>/dev/null | sed -n 's/^kanzo [^ ]* (\(.*\))$/\1/p' || true
}

# a. Preflight.
preflight() {
  local who branch dirty err remote counts
  who=$(id -un)
  [ "$who" = claude ] || fail "user: running as $who, deploys run as claude"
  say "user claude"
  g rev-parse --git-dir >/dev/null 2>&1 || fail "repo: $REPO is not a git checkout"
  branch=$(g symbolic-ref --short -q HEAD) || branch="a detached HEAD"
  [ "$branch" = "$BRANCH" ] || fail "branch: $REPO is on $branch, deploys ship $BRANCH"
  say "$REPO is on $BRANCH"
  dirty=$(g status --porcelain --untracked-files=no)
  [ -z "$dirty" ] || fail "clean tree: $REPO has uncommitted changes:
$(sed 's/^/     /' <<<"$dirty")"
  say "tracked tree clean"
  err=$(g fetch --quiet origin 2>&1) || fail "fetch: git fetch origin failed: $(head -n 1 <<<"$err")"
  HEAD=$(g rev-parse HEAD)
  remote=$(g rev-parse -q --verify "origin/$BRANCH^{commit}") || fail "pushed: origin/$BRANCH does not exist"
  if [ "$HEAD" != "$remote" ]; then
    counts=$(g rev-list --left-right --count "origin/$BRANCH...HEAD")
    fail "pushed: HEAD ${HEAD:0:12} is not origin/$BRANCH ${remote:0:12} (${counts#*$'\t'} ahead, ${counts%$'\t'*} behind); only pushed commits ship"
  fi
  say "HEAD ${HEAD:0:12} is origin/$BRANCH"
}

# b. The commit the running board was built from.
live_commit() {
  local how reply code last
  if [ -n "${KANZO_DEPLOY_LIVE_COMMIT:-}" ]; then
    LIVE=$KANZO_DEPLOY_LIVE_COMMIT how=KANZO_DEPLOY_LIVE_COMMIT
  else
    reply=$(curl -s --max-time 5 -w '\n%{http_code}' "$URL/api/version") || true
    code=${reply##*$'\n'}
    case "$code" in
      200)
        LIVE=$(commit_in <<<"${reply%$'\n'*}") how=$URL/api/version
        [ -n "$LIVE" ] || fail "live commit: $URL/api/version does not name a commit"
        ;;
      404)
        last=$(tail -n 1 "$DATA/deployed" 2>/dev/null || true)
        LIVE=$(awk '{print $2}' <<<"$last") how=$DATA/deployed
        [ -n "$LIVE" ] || fail "live commit: the board predates /api/version and $DATA/deployed is empty;
     confirm what the board was built from, then run once with KANZO_DEPLOY_LIVE_COMMIT=<sha>"
        ;;
      *)
        fail "live commit: no board answers at $URL (HTTP ${code:-000}); start $UNIT, or confirm what it runs and set KANZO_DEPLOY_LIVE_COMMIT=<sha>"
        ;;
    esac
  fi
  case "$LIVE" in
    unknown) fail "live commit: the running board was built outside this script, from no known commit (unknown), so its source must be merged into $BRANCH first" ;;
    *-dirty) fail "live commit: the running board was built outside this script, from ${LIVE%-dirty} plus uncommitted changes, so its source must be committed and merged into $BRANCH first" ;;
  esac
  [[ $LIVE =~ ^[0-9a-f]{7,40}$ ]] || fail "live commit: '$LIVE' (from $how) is not a commit sha"
  g cat-file -e "$LIVE^{commit}" 2>/dev/null ||
    fail "live commit: the running board was built outside this script, from $LIVE, a commit $REPO does not have, so its source must be pushed and merged into $BRANCH first"
  LIVE=$(g rev-parse "$LIVE^{commit}")
  say "live commit ${LIVE:0:12} (from $how)"
}

# c. Everything the board runs must be in HEAD.
ancestor() {
  if g merge-base --is-ancestor "$LIVE" HEAD; then
    say "live ${LIVE:0:12} is in HEAD"
    return
  fi
  local branches
  branches=$(g branch -a --contains "$LIVE" | sed 's/^[* ]*//')
  fail "ancestor: the running board has $(g rev-list --count "HEAD..$LIVE") commit(s) HEAD lacks; merge them into $BRANCH first:
$(g log --oneline -n 20 "HEAD..$LIVE" | sed 's/^/       /')
     branches that contain ${LIVE:0:12}:
$(sed 's/^/       /' <<<"${branches:-(none)}")"
}

# d. The list of live features cannot shrink, except by --drop-feature.
features_kept() {
  local now old name drop missing=() dropped
  now=$(g show "HEAD:$FEATURES" 2>/dev/null) || fail "features: $FEATURES is missing at HEAD"
  features <<<"$now" >/dev/null || fail "features: fix $FEATURES at HEAD"
  if ! old=$(g show "$LIVE:$FEATURES" 2>/dev/null); then
    say "features: none listed at ${LIVE:0:12}, nothing to keep"
    return
  fi
  now=$(feature_names <<<"$now")
  while IFS= read -r name; do
    [ -n "$name" ] || continue
    grep -qxF -- "$name" <<<"$now" && continue
    dropped=
    for drop in "${DROP[@]}"; do [ "$drop" = "$name" ] && dropped=1; done
    if [ -n "$dropped" ]; then say "features: dropping \"$name\" (--drop-feature)"; else missing+=("\"$name\""); fi
  done < <(feature_names <<<"$old")
  [ ${#missing[@]} -eq 0 ] ||
    fail "features: the running board has ${missing[*]} and $FEATURES at HEAD does not; restore the line, or pass --drop-feature if it is meant to go"
  say "features: every live feature is still listed"
}

# f, h. A command in REPO, its output in LOG; on failure the tail of it.
run() {
  local what=$1
  shift
  if (cd "$REPO" && "$@") >>"$LOG" 2>&1; then
    say "$what"
  else
    tail -n 20 "$LOG" | sed 's/^/     /' >&2
    fail "$what: full output in $LOG"
  fi
}

# g. A copy of the running binary and of the database, each read back.
safety_copies() {
  local pid src counts backup old
  pid=$(listener_pid)
  if [ -n "$pid" ]; then src=/proc/$pid/exe; else src=$BIN; fi
  mkdir -p "$DATA/rollback" "$DATA/backups"
  ROLLBACK=$DATA/rollback/kanzo-${LIVE:0:12}
  cp "$src" "$ROLLBACK.tmp" && chmod 755 "$ROLLBACK.tmp" && mv -f "$ROLLBACK.tmp" "$ROLLBACK" ||
    fail "rollback copy: cannot copy $src to $ROLLBACK"
  cmp -s "$src" "$ROLLBACK" || fail "rollback copy: $ROLLBACK differs from $src"
  say "rollback binary $ROLLBACK (from ${pid:+the running board, }$src)"
  find "$DATA/rollback" -maxdepth 1 -type f -name 'kanzo-*' ! -name '*.tmp' -printf '%T@ %p\n' |
    sort -rn | tail -n +4 | cut -d' ' -f2- | while IFS= read -r old; do rm -f -- "$old"; done

  backup=$DATA/backups/kanzo-before-deploy-$(date +%Y-%m-%d-%H%M)-${HEAD:0:12}.db
  [ ! -e "$backup" ] || backup=${backup%.db}-$(date +%S).db
  [ -f "$DATA/kanzo.db" ] || fail "database copy: there is no $DATA/kanzo.db"
  counts=$(
    python3 -I - "$DATA/kanzo.db" "$backup" 2>&1 <<'PY'
import sqlite3, sys
tables = ("tasks", "comments", "activity")
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=rw", uri=True, isolation_level=None, timeout=10)
src.execute("BEGIN")  # one snapshot for the counts and the copy
live = [src.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables]
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
src.execute("COMMIT")
dst.close()
copy = sqlite3.connect(f"file:{sys.argv[2]}?mode=ro", uri=True)
got = [copy.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables]
check = copy.execute("PRAGMA quick_check").fetchone()[0]
said = ", ".join(f"{t} {n}" for t, n in zip(tables, got))
if got != live or check != "ok":
    sys.exit(f"{sys.argv[2]} does not match: live {live}, copy {got}, check {check}")
print(said)
PY
  ) || fail "database copy: $(tail -n 1 <<<"$counts")"
  say "database copy $backup ($counts, same as live)"
}

# h. The release build, which must name HEAD.
build() {
  local stamp
  CHANGED=1
  run "cargo build --release" cargo build --release --locked
  stamp=$(stamp_of "$BIN")
  if [ "$stamp" != "${HEAD:0:12}" ]; then
    printf "note binary says '%s', not %s: touching build.rs, building again\n" "${stamp:-nothing}" "${HEAD:0:12}"
    touch "$REPO/build.rs"
    run "cargo build --release (again)" cargo build --release --locked
    stamp=$(stamp_of "$BIN")
    [ "$stamp" = "${HEAD:0:12}" ] || fail "binary: $BIN --version says '${stamp:-nothing}', not ${HEAD:0:12}"
  fi
  say "binary $BIN is ${HEAD:0:12}"
}

# i. Wait, at most 30 s, for `$1` to succeed; ends early when the unit stops being active.
wait_until() {
  local end=$((SECONDS + 30)) state
  while [ $SECONDS -lt $end ]; do
    state=$(systemctl --user is-active "$UNIT" 2>/dev/null || true)
    if [ "$state" != active ]; then
      WHY="$UNIT is ${state:-unknown}"
      return 1
    fi
    "$@" && return 0
    sleep 1
  done
  WHY="no answer within 30 s"
  return 1
}

serves_head() { [ "$(served_commit)" = "$HEAD" ]; }

restart() {
  CHANGED=1
  systemctl --user restart "$UNIT" || fail "restart: systemctl --user restart $UNIT failed"
  wait_until serves_head || fail "restart: $URL/api/version does not say ${HEAD:0:12} ($WHY)"
  say "$UNIT restarted, $URL serves ${HEAD:0:12}"
}

# j. Every line of features.txt against URL, each printed; 1 when any failed.
status_ok() {
  [ "$1" != 000 ] || return 1
  case "$2" in
    '!'*) [ "$1" != "${2#!}" ] ;;
    *) [[ ",$2," == *",$1,"* ]] ;;
  esac
}

check_features() {
  local list name path status text code body bad=0
  list=$(features <"$REPO/$FEATURES") || return 1
  mkdir -p "$TMPROOT"
  body=$(mktemp "$TMPROOT/kanzo-feature.XXXXXX")
  while IFS=$'\t' read -r name path status text; do
    code=$(curl -s -o "$body" -w '%{http_code}' --max-time 10 "$URL$path") || code=000
    if ! status_ok "$code" "$status"; then
      flag "feature $name: $path answered $code, wanted $status"
      bad=1
    elif [ -n "$text" ] && ! grep -qF -- "$text" "$body"; then
      flag "feature $name: $path ($code) does not contain $text"
      bad=1
    else
      say "feature $name: $path $code${text:+, has $text}"
    fi
  done <<<"$list"
  rm -f "$body"
  return $bad
}

check_listeners() {
  local addrs bad
  addrs=$(ss -ltnH "sport = :$PORT" | awk '{print $4}' | sed "s/:$PORT\$//")
  if [ -z "$addrs" ]; then
    flag "listeners: nothing listens on port $PORT"
    return 1
  fi
  bad=$(grep -vxF -e 127.0.0.1 -e '[::1]' <<<"$addrs" || true)
  if [ -n "$bad" ]; then
    flag "listeners: port $PORT is open on $(paste -sd ' ' <<<"$bad") (only 127.0.0.1 and [::1] are allowed)"
    return 1
  fi
  say "listeners on $PORT: $(paste -sd ' ' <<<"$addrs")"
}

# The Location carries a per-visit token: only its host is ever printed.
check_edge() {
  local head code host
  head=$(curl -sI --max-time 10 "$EDGE/" | tr -d '\r') || true
  code=$(awk 'NR == 1 {print $2}' <<<"$head")
  host=$(awk 'tolower($1) == "location:" {print $2; exit}' <<<"$head" | sed -n 's|^https://\([^/?#]*\).*|\1|p')
  if [ "$code" = 302 ] && [[ $host == *.cloudflareaccess.com ]]; then
    say "edge $EDGE: 302 to $host"
  else
    flag "edge: $EDGE/ answered ${code:-nothing}${host:+ to $host}, wanted 302 to Cloudflare Access"
    return 1
  fi
}

# k. Put the rollback binary back, restart, and prove the board runs it.
runs_rollback() {
  local pid
  [ "$(curl -s --max-time 3 "$URL/api/ping")" = kanzo ] || return 1
  pid=$(listener_pid)
  [ -n "$pid" ] && cmp -s "/proc/$pid/exe" "$ROLLBACK"
}

rollback() {
  CHANGED=0
  local loud="ROLLBACK FAILED, the board may be down or on the new build. The binary it ran before is
     $ROLLBACK (commit ${LIVE:0:12}). By hand: cp $ROLLBACK $BIN && systemctl --user restart $UNIT"
  printf 'ROLLBACK: restoring %s (commit %s) to %s\n' "$ROLLBACK" "${LIVE:0:12}" "$BIN" >&2
  if ! { [ -f "$ROLLBACK" ] && cp "$ROLLBACK" "$BIN.rollback" && mv -f "$BIN.rollback" "$BIN" && cmp -s "$ROLLBACK" "$BIN"; }; then
    flag "$loud"
    return
  fi
  if [ -n "$SKIP_SERVICE" ]; then
    say "rolled back the file $BIN to ${LIVE:0:12} (service steps skipped)"
    return
  fi
  if ! systemctl --user restart "$UNIT" || ! wait_until runs_rollback; then
    flag "${WHY:+$WHY: }$loud"
    return
  fi
  say "rolled back: $UNIT runs $ROLLBACK again (commit ${LIVE:0:12}), copied to $BIN"
}

# l. Record the deploy, and name the processes still on an old binary.
record() {
  local line=
  line="$(date -Iseconds) $HEAD $LIVE"
  printf '%s\n' "$line" >>"$DATA/deployed"
  [ "$(tail -n 1 "$DATA/deployed")" = "$line" ] || fail "record: $DATA/deployed does not end with this deploy"
  say "recorded in $DATA/deployed"
}

old_processes() {
  local exe pid ppid list=() n=0
  for exe in /proc/[0-9]*/exe; do
    [ "$(readlink "$exe" 2>/dev/null)" = "$BIN (deleted)" ] || continue
    pid=${exe#/proc/} pid=${pid%/exe}
    ppid=$(ps -o ppid= -p "$pid" | tr -d ' ') || continue
    list+=("pid $pid started $(ps -o lstart= -p "$pid" || true), parent $ppid in $(readlink "/proc/$ppid/cwd" 2>/dev/null || echo unknown)")
    n=$((n + 1))
  done
  if [ $n -eq 0 ]; then
    say "no process still runs an old kanzo binary"
    return
  fi
  printf 'note %s process(es) still run an old kanzo binary, until their agent session restarts:\n' "$n"
  printf '       %s\n' "${list[@]}"
  printf '     tools/activity-sign/sign.py signs the activity rows they leave unsigned.\n'
}

main() {
  local check=0 bad=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --check) check=1 ;;
      --drop-feature)
        [ $# -ge 2 ] || { usage >&2; exit 2; }
        DROP+=("$2")
        shift
        ;;
      --drop-feature=*) DROP+=("${1#*=}") ;;
      -h | --help) usage; exit 0 ;;
      *) usage >&2; exit 2 ;;
    esac
    shift
  done
  if [ -n "$SKIP_BUILD$SKIP_SERVICE" ] && [ "$(realpath -m "$DATA")" = "$LIVE_DATA" ]; then
    fail "KANZO_DEPLOY_SKIP_* is for tests; it needs KANZO_DEPLOY_DATA pointing away from $LIVE_DATA"
  fi
  trap 'fail "interrupted"' INT TERM

  preflight
  live_commit
  ancestor
  features_kept
  if [ $check = 1 ]; then
    say "check passed: ${HEAD:0:12} can replace ${LIVE:0:12}"
    exit 0
  fi

  exec 9>>"$DATA/deploy.lock"
  flock -n 9 || fail "lock: another deploy is running ($DATA/deploy.lock)"
  LOG=$HOME/scratch/kanzo-deploy-$(date +%Y-%m-%d-%H%M%S).log
  mkdir -p "$(dirname "$LOG")"
  run "cargo fmt --check" cargo fmt --check
  run "cargo clippy" cargo clippy --all-targets --locked -- -D warnings
  run "cargo test" cargo test --locked
  safety_copies
  if [ -n "$SKIP_BUILD" ]; then say "build skipped (KANZO_DEPLOY_SKIP_BUILD, tests only)"; else build; fi
  if [ -n "$SKIP_SERVICE" ]; then say "restart skipped (KANZO_DEPLOY_SKIP_SERVICE, tests only)"; else restart; fi
  check_features || bad=1
  if [ -z "$SKIP_SERVICE" ]; then
    check_listeners || bad=1
    check_edge || bad=1
  fi
  [ $bad = 0 ] || fail "checks: the new build failed the checks above"
  CHANGED=0
  if [ -n "$SKIP_SERVICE" ]; then
    say "not recorded in $DATA/deployed: nothing was restarted (tests only)"
  else
    record
    old_processes
  fi
  say "deployed ${HEAD:0:12} (was ${LIVE:0:12})"
}

# Sourced (to reuse a check), it only defines; run, it deploys.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
