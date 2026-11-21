#!/usr/bin/env python3
# Version: v1 · created 2026-11-21 06:50 EST · updated 2026-11-21 07:28 EST
# Changes: v1 sign unsigned activity rows from session transcripts, board comments and the audit's provenance lines.
# This file: /home/claude/dev/kanzo/tools/activity-sign/sign.py (repo dwertz-at-blenko/kanzo, branch public-url)
# Session: b1475961-06d7-40b0-9eff-f2b47896b6bb · vps as claude · cwd /home/claude/dev
"""Sign activity log rows that no writer signed.

The triggers in src/activity.sql record every change, whoever makes it. The "who" is added by
the Kanzo that made the write, so a write from a Kanzo built before the log, or from another
tool, leaves `actor` empty and the log shows "unknown". This finds the evidence and fills it in:

  agent   a `mcp__kanzo__*` call in a Claude Code transcript, on the same card, of a matching
          kind, at the same moment. The label is the one that session's signed rows carry, or
          `claude-code (<folder the session started in>)` when it has none.
  board   a comment whose row in `comments` has the author "me" (only the board writes that),
          and a move or edit of the same card within a minute of it that no agent call explains.
          The second kind is an inference and is printed as such.
  audit   a card whose first comment is the board audit's provenance line ("Board audit ...")
          written within ten seconds of the card: tools/board-audit/apply.py made both. The
          card's creation is signed "board audit", and so is that comment's row, which the
          board had signed "me" because the script came in through the board's own API.

Rows with no evidence stay unsigned. Nothing is written without --apply, which first copies
the database to <home>/backups/. Run it again any time: it touches unsigned rows, and the
audit's comment rows signed "me", nothing else.

    tools/activity-sign/sign.py                 # show what it would sign
    tools/activity-sign/sign.py --apply         # back up, then sign
"""
import argparse
import collections
import datetime as dt
import glob
import json
import os
import re
import sqlite3
import sys

WRITES = {"create_task": {"created"}, "update_task": {"moved", "edited"}, "next_task": {"moved"},
          "add_comment": {"commented"}, "delete_task": {"deleted"}, "create_project": {"project created"}}
KEY = re.compile(r"[a-z0-9][a-z0-9._-]*#(\d+)")
SLACK = 3        # seconds between a transcript's clock and the row's
NEAR_BOARD = 60  # a move this soon after a board comment on the same card is the board's
NEAR_AUDIT = 10  # the audit writes its provenance comment right after the card
AUDIT = "board audit"


def stamp(text):
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def transcript_calls(root):
    """Every Kanzo write a session made: kind, when it started and ended, the cards it named."""
    calls, started_in = [], {}
    files = glob.glob(os.path.join(root, "*", "*.jsonl")) + glob.glob(os.path.join(root, "*", "*", "subagents", "*.jsonl"))
    # Sessions before their subagents, so a subagent can take its session's folder.
    for path in sorted(files, key=lambda p: ("/subagents/" in p, p)):
        sub = "/subagents/" in path
        session = path.split("/subagents/")[0].rsplit("/", 1)[-1] if sub else os.path.basename(path)[:-6]
        folder, pending = started_in.get(session), {}
        try:
            lines = open(path, errors="replace")
        except OSError:
            continue
        for line in lines:
            if folder is None and '"cwd"' in line:
                try:
                    folder = json.loads(line).get("cwd")
                except ValueError:
                    pass
                if folder and not sub:
                    started_in[session] = folder
            if "mcp__kanzo__" not in line and not pending:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            content = (entry.get("message") or {}).get("content")
            if not isinstance(content, list) or not entry.get("timestamp"):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                name = str(block.get("name", ""))
                if block.get("type") == "tool_use" and name.startswith("mcp__kanzo__"):
                    tool = name.rsplit("__", 1)[-1]
                    if tool in WRITES:
                        pending[block.get("id")] = {"tool": tool, "start": stamp(entry["timestamp"]),
                                                    "named": str((block.get("input") or {}).get("id", ""))}
                elif block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
                    call = pending.pop(block["tool_use_id"])
                    if block.get("is_error"):
                        continue
                    reply = text_of(block.get("content")).split("\n")[0]
                    call.update(end=stamp(entry["timestamp"]), session=session, folder=folder or started_in.get(session),
                                cards={int(n) for n in KEY.findall(reply + " " + call["named"])})
                    calls.append(call)
    return calls


def explain(rows, calls, comments):
    """For each row: (the one session whose call explains it or None, True when the board's comment)."""
    out = {}
    for rid, at, actor, action, task_id, _project in rows:
        when = stamp(at)
        sessions = {c["session"] for c in calls if action in WRITES[c["tool"]]
                    and c["start"] - SLACK <= when <= c["end"] + SLACK and (task_id in c["cards"] if task_id else True)}
        by_board = action == "commented" and not sessions and any(
            author == "me" and abs(stamp(made) - when) <= SLACK for author, made in comments.get(task_id, []))
        out[rid] = (next(iter(sessions)) if len(sessions) == 1 else None, by_board)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--home", default=os.environ.get("KANZO_HOME", os.path.expanduser("~/.local/share/kanzo")))
    ap.add_argument("--transcripts", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--apply", action="store_true", help="back up the database, then sign")
    ap.add_argument("--verbose", action="store_true", help="list the audit's rows too")
    args = ap.parse_args()
    db = os.path.join(args.home, "kanzo.db")

    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    rows = conn.execute("SELECT id, at, actor, action, task_id, project FROM activity ORDER BY id").fetchall()
    comments = collections.defaultdict(list)
    first = {}   # task -> when the audit wrote its provenance line, when that is the card's first comment
    for task_id, author, made, body in conn.execute("SELECT task_id, author, created_at, body FROM comments ORDER BY created_at, id"):
        if task_id not in first:
            first[task_id] = stamp(made) if body.startswith("Board audit") else None
        comments[task_id].append((author, made))
    conn.close()

    calls = transcript_calls(args.transcripts)
    why = explain(rows, calls, comments)
    folder_of = {c["session"]: c["folder"] for c in calls}

    # The label each session's own signed rows carry.
    label = collections.defaultdict(collections.Counter)
    for rid, _at, actor, *_ in rows:
        session = why[rid][0]
        if session and actor and actor not in ("board", "me"):
            label[session][actor] += 1

    signs, board_at = [], {}   # (row id, actor, how), task -> time of the board's last comment
    for rid, at, actor, action, task_id, project in rows:
        session, by_board = why[rid]
        if actor == "board" or by_board:
            board_at[task_id] = stamp(at)
        audit = first.get(task_id)
        if actor == "me" and action == "commented" and audit is not None and abs(stamp(at) - audit) <= SLACK:
            signs.append((rid, AUDIT, "the audit's provenance comment, was signed me"))
        if actor is not None:
            continue
        if action == "created" and audit is not None and 0 <= audit - stamp(at) <= NEAR_AUDIT:
            signs.append((rid, AUDIT, "card made by the audit: its provenance comment follows within 10 s"))
        elif session:
            if label[session]:
                signs.append((rid, label[session].most_common(1)[0][0], "agent call, session's own label"))
            elif folder_of.get(session):
                name = os.path.basename(folder_of[session].rstrip("/"))
                signs.append((rid, "claude-code (%s)" % name, "agent call, folder the session started in"))
        elif by_board:
            signs.append((rid, "board", "comment written by the board"))
        elif action in ("moved", "edited") and stamp(at) - board_at.get(task_id, -1e18) <= NEAR_BOARD:
            signs.append((rid, "board", "INFERRED: same card within a minute of the board's comment"))

    by_id = {r[0]: r for r in rows}
    unsigned = [r for r in rows if r[2] is None]
    how = collections.Counter(h for _, _, h in signs)
    print("activity rows: %d, unsigned: %d, Kanzo calls in transcripts: %d" % (len(rows), len(unsigned), len(calls)))
    for rid, actor, reason in signs:
        _, at, _, action, task_id, project = by_id[rid]
        if actor != AUDIT or args.verbose:
            print("  %5d  %s  %-9s %s#%s  ->  %s  [%s]" % (rid, at, action, project, task_id, actor, reason))
    print("would sign %d:" % len(signs) if not args.apply else "signing %d:" % len(signs))
    for reason, n in how.most_common():
        print("  %4d  %s" % (n, reason))
    left = collections.Counter(r[3] for r in unsigned if r[0] not in {s[0] for s in signs})
    print("left unsigned (no evidence): %d %s" % (sum(left.values()), dict(left)))
    if not args.apply or not signs:
        return 0

    backups = os.path.join(args.home, "backups")
    os.makedirs(backups, exist_ok=True)
    copy = os.path.join(backups, "kanzo-before-activity-sign-%s.db" % dt.datetime.now().strftime("%Y-%m-%d-%H%M"))
    src = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    dst = sqlite3.connect(copy)
    src.backup(dst)
    dst.close()
    src.close()
    kept = sqlite3.connect("file:%s?mode=ro" % copy, uri=True).execute("SELECT count(*) FROM activity").fetchone()[0]
    if kept != len(rows):
        print("backup has %d activity rows, the board has %d: nothing signed" % (kept, len(rows)), file=sys.stderr)
        return 1
    print("backup: %s (%d activity rows)" % (copy, kept))

    conn = sqlite3.connect(db, timeout=10)
    conn.execute("BEGIN IMMEDIATE")
    done = sum(conn.execute("UPDATE activity SET actor = ? WHERE id = ? AND (actor IS NULL OR (actor = 'me' AND ? = ?))",
                            (actor, rid, actor, AUDIT)).rowcount for rid, actor, _ in signs)
    conn.commit()
    still = conn.execute("SELECT count(*) FROM activity WHERE actor IS NULL").fetchone()[0]
    conn.close()
    print("signed %d rows; unsigned now: %d" % (done, still))
    return 0


if __name__ == "__main__":
    sys.exit(main())
