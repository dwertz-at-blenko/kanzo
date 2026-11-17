#!/usr/bin/env python3
# Version: v4 · created 2026-11-17 03:36 EST · updated 2026-11-17 12:33 EST
# Changes: v4 the agent no longer repeats the line, the status bar is enough (Dave 2026-11-17). v3 the line is also saved for the status bar (Dave: "stick in the status bar"). v2 the agent also receives the line and repeats it, because the note alone was hidden from Dave (session b1475961). v1 note to the user on every board write.
# This file: /home/claude/dev/kanzo/agent/hooks/kanzo-note.py
# Session: 1434c01e-24f9-406f-856d-7015b5dd7b69 · vps as claude · cwd /home/claude/dev · slug unified-imagining-globe · name "kanzo setup"
# Resume: ssh -t blenko-claude 'cd /home/claude/dev && claude --resume 1434c01e-24f9-406f-856d-7015b5dd7b69'   (from the Mac)
"""Claude Code PostToolUse hook: when the session changes the board through a Kanzo MCP
tool, shows the user one line saying what changed, for example

    Kanzo: Updated kanzo#251, review -> done: Merge the stars branch

The line is Kanzo's own reply to the tool call, so it says what happened, not what was
asked. Reads (list_tasks, get_task, list_projects) and failed calls print nothing. The
hook never touches the board.
"""
import json
import os
import sys
import time

WRITES = ("create_task", "update_task", "add_comment", "next_task", "delete_task", "create_project", "link_repo")
QUIET = ("Nothing in todo", "internal error", "Pass `project`", "Cannot tell the project")


def reply_text(response):
    """The text of a tool reply, whichever shape Claude Code hands over."""
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        if response.get("isError") or response.get("is_error"):
            return ""
        response = response.get("content", response.get("text", ""))
        if isinstance(response, str):
            return response
    if isinstance(response, list):
        return "\n".join(b.get("text", "") for b in response if isinstance(b, dict))
    return ""


def note(tool, args, text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or any(q in lines[0] for q in QUIET):
        return ""
    out = lines[0]
    if tool == "next_task" and len(lines) > 1:  # "Moved to doing:" then the card
        out = "Took %s (%s)" % (lines[1], lines[0].rstrip(":").lower())
    if tool == "add_comment":
        body = " ".join(str(args.get("body", "")).split())
        if body:
            out = "%s %s" % (out, body[:100] + ("..." if len(body) > 100 else ""))
    return "Kanzo: " + out[:240]


def save(session_id, line):
    """Leaves the line where the status bar can read it: ~/.claude/state/kanzo/<session>."""
    sid = "".join(c for c in str(session_id or "") if c.isalnum() or c in "-_")[:80]
    if not sid:
        return
    try:
        folder = os.path.join(os.path.expanduser("~"), ".claude", "state", "kanzo")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, sid), "w") as f:
            f.write("%d|%s\n" % (time.time(), line))
    except OSError:
        pass


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    name = str(event.get("tool_name", ""))
    if not name.startswith("mcp__kanzo__"):
        return
    tool = name.rsplit("__", 1)[-1]
    if tool not in WRITES:
        return
    line = note(tool, event.get("tool_input") or {}, reply_text(event.get("tool_response")))
    if line:
        save(event.get("session_id"), line)
        print(json.dumps({"systemMessage": line}))


if __name__ == "__main__":
    main()
