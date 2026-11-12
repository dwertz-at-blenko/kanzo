#!/usr/bin/env bash
# Claude Code Stop hook: when this repo has a marker naming the card being worked on
# (written by the agent when it took the card, see the kanzo-board skill), asks the
# agent to close the card out before it stops: a comment, a move, and the marker removed.
# It blocks only once per stop (Claude Code sets stop_hook_active on the retry).
set -u
input=$(cat)
if printf '%s' "$input" | grep -q '"stop_hook_active": *true'; then exit 0; fi
dir=$(git -C "${PWD}" rev-parse --git-dir 2>/dev/null) || exit 0
marker="$dir/kanzo-task"
[ -f "$marker" ] || exit 0
task=$(head -1 "$marker" | tr -d '[:space:]')
[ -n "$task" ] || exit 0
reason="Kanzo: this session took card $task and has not closed it out. Before stopping: add a comment on $task with what changed and how it was verified; move it (done, review with human on, or back to todo with the next step); set its url if there is a PR; then remove the marker: rm -f \"$marker\". If the card is finished already, just remove the marker."
printf '{"decision": "block", "reason": %s}\n' "$(printf '%s' "$reason" | python3 -I -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
exit 0
