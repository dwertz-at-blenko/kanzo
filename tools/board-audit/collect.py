#!/usr/bin/env python3
"""Collects the evidence a board audit needs, read-only, into one inventory.

Reads the board (its API), memory files, Claude Code session transcripts and git
repos, and writes <out>/inventory.md to read and <out>/inventory.json to script
against, grouped by board project where a repo can be matched.

  python3 collect.py --api http://127.0.0.1:8111/api --memory ~/.claude/memory \
      --sessions ~/.claude/projects --repos ~/dev --since 2026-08-01 --out docs/audit/inventory

Standard library only. Nothing is written to the board.
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.request

OPEN_ITEM = re.compile(r"\b(TODO|FIXME|pending|next step|blocked|awaiting|your move|not started|open:)\b", re.I)


def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.load(r)
    except Exception as e:  # the board may be down; the rest of the inventory is still useful
        print(f"warning: {url}: {e}", file=sys.stderr)
        return None


def run(cmd, cwd):
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return ""


def repo_key(remote):
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", remote.strip())
    return m.group(1).lower() if m else None


def collect_repos(root, since):
    repos = []
    if not root or not os.path.isdir(root):
        return repos
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isdir(os.path.join(path, ".git")):
            continue
        remote = run(["git", "remote", "get-url", "origin"], path).strip()
        log = run(["git", "log", f"--since={since}", "--date=short", "--pretty=%h%x09%ad%x09%s", "--no-merges"], path)
        commits = [dict(zip(("hash", "date", "subject"), l.split("\t", 2))) for l in log.splitlines() if "\t" in l]
        branches = [b.strip().lstrip("* ") for b in run(["git", "branch", "--no-merged", "HEAD"], path).splitlines() if b.strip()]
        worktrees = [l.split()[0] for l in run(["git", "worktree", "list"], path).splitlines()[1:]]
        status = run(["git", "status", "--porcelain"], path)
        todos = run(["git", "grep", "-c", "-E", r"\b(TODO|FIXME)\b", "--", ":!*.lock", ":!*.min.*"], path)
        todo_count = sum(int(l.rsplit(":", 1)[1]) for l in todos.splitlines() if ":" in l)
        repos.append({
            "path": path, "remote": remote, "repo": repo_key(remote) if remote else None,
            "last_commit": commits[0]["date"] if commits else None, "commits_since": len(commits),
            "commits": commits[:40], "unmerged_branches": branches, "worktrees": worktrees,
            "dirty_files": len(status.splitlines()), "todo_fixme": todo_count,
        })
    return repos


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def collect_sessions(root, since):
    sessions = []
    if not root or not os.path.isdir(root):
        return sessions
    since_ts = dt.datetime.fromisoformat(since).timestamp()
    for dirpath, _, files in os.walk(root):
        for f in files:
            if not f.endswith(".jsonl"):
                continue
            path = os.path.join(dirpath, f)
            if os.path.getmtime(path) < since_ts:
                continue
            first_user, last_assistant, summary, cwd, pending, first_ts, last_ts = None, None, None, None, [], None, None
            try:
                with open(path, errors="replace") as fh:
                    for line in fh:
                        try:
                            ev = json.loads(line)
                        except Exception:
                            continue
                        if ev.get("type") == "summary":
                            summary = ev.get("summary")
                        msg = ev.get("message") or {}
                        role, text = msg.get("role"), text_of(msg.get("content"))
                        ts = ev.get("timestamp")
                        cwd = cwd or ev.get("cwd")
                        if role == "user" and text and not text.startswith("<"):
                            first_ts = first_ts or ts
                            if first_user is None:
                                first_user = text[:300]
                            if "Pending Tasks" in text or "Your move" in text:
                                pending.append(text[text.find("Pending Tasks") if "Pending Tasks" in text else text.find("Your move"):][:800])
                        if role == "assistant" and text:
                            last_assistant, last_ts = text[-600:], ts
            except Exception:
                continue
            if first_user or summary:
                sessions.append({"file": path, "title": summary, "cwd": cwd, "first_prompt": first_user, "last_reply": last_assistant,
                                 "pending": pending[-2:], "started": first_ts, "ended": last_ts})
    sessions.sort(key=lambda s: s.get("ended") or "", reverse=True)
    return sessions


def collect_memory(root):
    notes = []
    if not root or not os.path.isdir(root):
        return notes
    for dirpath, _, files in os.walk(root):
        for f in sorted(files):
            if not f.endswith(".md"):
                continue
            path = os.path.join(dirpath, f)
            try:
                text = open(path, errors="replace").read()
            except Exception:
                continue
            title = next((l.lstrip("# ").strip() for l in text.splitlines() if l.startswith("#")), f)
            desc = re.search(r"^description:\s*(.+)$", text, re.M)
            items = [l.strip()[:200] for l in text.splitlines() if OPEN_ITEM.search(l)][:12]
            notes.append({"file": path, "title": title, "description": desc.group(1).strip() if desc else None,
                          "updated": dt.date.fromtimestamp(os.path.getmtime(path)).isoformat(), "open_items": items})
    notes.sort(key=lambda n: n["updated"], reverse=True)
    return notes


def collect_board(api):
    board = fetch(f"{api}/board?done=all") if api else None
    if not board:
        return None
    now = dt.datetime.now(dt.timezone.utc)
    stale = []
    for t in board.get("tasks", []):
        if t["status"] == "doing":
            age = (now - dt.datetime.fromisoformat(t["updated_at"].replace("Z", "+00:00"))).days
            if age >= 7:
                stale.append({"id": t["id"], "project": t["project"], "title": t["title"], "days": age})
    by_status = {}
    for t in board.get("tasks", []):
        by_status[t["status"]] = by_status.get(t["status"], 0) + 1
    return {"projects": board.get("projects", []), "tasks": board.get("tasks", []), "by_status": by_status, "stale_doing": stale,
            "blocked": [t for t in board.get("tasks", []) if "blocked" in t.get("tags", []) and t["status"] != "done"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default="http://127.0.0.1:4242/api")
    ap.add_argument("--memory")
    ap.add_argument("--sessions")
    ap.add_argument("--repos")
    ap.add_argument("--since", default=(dt.date.today() - dt.timedelta(days=90)).isoformat())
    ap.add_argument("--out", required=True, help="directory for inventory.md and inventory.json")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    inv = {"generated": dt.datetime.now().isoformat(timespec="seconds"), "since": a.since,
           "board": collect_board(a.api), "repos": collect_repos(a.repos, a.since),
           "sessions": collect_sessions(a.sessions, a.since), "memory": collect_memory(a.memory)}

    # Map repos to projects by the linked GitHub repo.
    projects = {p["slug"]: p for p in (inv["board"] or {}).get("projects", [])}
    by_repo = {r.lower(): p["slug"] for p in projects.values() for r in p.get("repos", [])}
    for r in inv["repos"]:
        r["project"] = by_repo.get(r["repo"] or "")
    for s in inv["sessions"]:
        r = next((r for r in inv["repos"] if s.get("cwd") and s["cwd"].startswith(r["path"])), None)
        s["project"] = r["project"] if r else None

    with open(os.path.join(a.out, "inventory.json"), "w") as f:
        json.dump(inv, f, indent=1)

    L = [f"# Board audit inventory, {inv['generated']} (since {a.since})", ""]
    b = inv["board"]
    if b:
        L += ["## The board now", "", "| Status | Cards |", "| --- | --- |"]
        L += [f"| {s} | {n} |" for s, n in sorted(b["by_status"].items())]
        L += ["", f"{len(b['projects'])} projects, {len(b['tasks'])} cards. "
              f"{len(b['stale_doing'])} in doing untouched for 7+ days, {len(b['blocked'])} blocked.", ""]
        for t in b["stale_doing"]:
            L.append(f"- stale doing: {t['project']}#{t['id']} {t['title']} ({t['days']} days)")
        for t in b["blocked"]:
            L.append(f"- blocked: {t['project']}#{t['id']} {t['title']}")
        L.append("")
    else:
        L += ["## The board now", "", "Not reachable at " + a.api, ""]

    L += ["## Repos", ""]
    for r in inv["repos"]:
        proj = f" -> project `{r['project']}`" if r.get("project") else " -> no project"
        L.append(f"### {os.path.basename(r['path'])} ({r['repo'] or 'no remote'}){proj}")
        L.append(f"{r['commits_since']} commits since {a.since}, last {r['last_commit']}, "
                 f"{len(r['unmerged_branches'])} unmerged branches, {len(r['worktrees'])} worktrees, "
                 f"{r['dirty_files']} dirty files, {r['todo_fixme']} TODO/FIXME")
        for c in r["commits"][:12]:
            L.append(f"- {c['date']} {c['hash']} {c['subject']}")
        for br in r["unmerged_branches"][:10]:
            L.append(f"- unmerged branch: {br}")
        L.append("")

    L += ["## Sessions", ""]
    for s in inv["sessions"]:
        L.append(f"### {s.get('title') or (s.get('first_prompt') or '')[:80]}")
        L.append(f"{s.get('started','?')[:10]} to {(s.get('ended') or '?')[:10]}, cwd {s.get('cwd')}, project `{s.get('project')}`")
        if s.get("first_prompt"):
            L.append("- asked: " + s["first_prompt"].replace("\n", " ")[:300])
        if s.get("last_reply"):
            L.append("- ended with: " + s["last_reply"].replace("\n", " ")[-300:])
        for p in s.get("pending", []):
            L.append("- pending: " + p.replace("\n", " ")[:500])
        L.append("")

    L += ["## Memory", ""]
    for n in inv["memory"]:
        L.append(f"### {n['title']} ({os.path.basename(n['file'])}, {n['updated']})")
        if n.get("description"):
            L.append(n["description"])
        for i in n["open_items"]:
            L.append(f"- {i}")
        L.append("")
    with open(os.path.join(a.out, "inventory.md"), "w") as f:
        f.write("\n".join(L))
    print(f"wrote {a.out}/inventory.md and inventory.json: {len(inv['repos'])} repos, {len(inv['sessions'])} sessions, "
          f"{len(inv['memory'])} memory files, board {'read' if b else 'not reachable'}")


if __name__ == "__main__":
    main()
