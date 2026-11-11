#!/usr/bin/env python3 -I
"""Prints a repo's Kanzo project and its open cards from a /api/board payload on stdin.
Used by kanzo-session-start.sh; prints nothing when the repo has no project."""
import json
import os
import sys

try:
    board = json.load(sys.stdin)
except Exception:
    sys.exit(0)
repo = os.environ.get("KANZO_REPO", "").lower()
project = next((p for p in board.get("projects", []) if repo in [r.lower() for r in p.get("repos", [])]), None)
if not project:
    sys.exit(0)
slug = project["slug"]
by = {}
for t in board.get("tasks", []):
    if t["project"] == slug and t["status"] != "done":
        by.setdefault(t["status"], []).append(t)
print(f'Kanzo: this repo is project "{project["name"]}" ({slug}), {project["open"]} open tasks.')
for status in ("doing", "review", "todo", "backlog"):
    tasks = by.get(status, [])
    for t in tasks[:8]:
        flags = [f for f in ("human" if t.get("human") else "", "blocked" if "blocked" in t.get("tags", []) else "") if f]
        print(f"  {status:8} {slug}#{t['id']} {t['title']}" + (f"  [{' '.join(flags)}]" if flags else ""))
    if len(tasks) > 8:
        print(f"  {status:8} and {len(tasks) - 8} more")
print("Take the card before the work (update_task to doing, or next_task), comment as you go, move it when done. Skill: kanzo-board.")
