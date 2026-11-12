#!/usr/bin/env python3
"""Applies a reviewed audit plan to the board, idempotently, with provenance comments.

  python3 apply.py --api http://127.0.0.1:8111/api --plan plan.json [--dry-run] [--label "Board audit 2026-11-11"]

plan.json is a list of actions:
  {"op": "create", "project": "kanzo", "title": "...", "status": "todo", "tags": ["deploy","audit"],
   "human": false, "description": "...", "url": "...", "source": "where this came from", "comment": "optional"}
  {"op": "move", "id": 42, "status": "done", "comment": "why", "tags_add": ["audit"], "human": true}
  {"op": "comment", "id": 17, "comment": "...", "tags_add": [...], "tags_remove": [...]}
  {"op": "merge", "id": 58, "into": 31, "comment": "duplicate of kanzo#31"}

Idempotent: a create whose project already has a card with the same title (case and
spaces aside) is skipped; a move to the status the card already has is skipped; a
comment whose text the card already carries is skipped; a merge of a card already done
is skipped. Every write leaves a comment that starts with the label. Nothing is deleted.
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.request


class Board:
    def __init__(self, api, dry, label):
        self.api, self.dry, self.label = api.rstrip("/"), dry, label
        self.log = []
        self.reload()

    def call(self, method, path, body=None):
        if self.dry and method != "GET":
            self.log.append(f"would {method} {path} {json.dumps(body) if body else ''}")
            return {}
        req = urllib.request.Request(f"{self.api}{path}", method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            raise SystemExit(f"{method} {path}: {e.code} {e.read().decode(errors='replace')}")

    def reload(self):
        board = self.call("GET", "/board?done=all")
        self.projects = {p["slug"]: p for p in board["projects"]}
        self.tasks = {t["id"]: t for t in board["tasks"]}

    @staticmethod
    def norm(s):
        return re.sub(r"\s+", " ", s.strip().lower().rstrip("."))

    def find(self, project, title):
        n = self.norm(title)
        return next((t for t in self.tasks.values() if t["project"] == project and self.norm(t["title"]) == n), None)

    def comments(self, task_id):
        return [c["body"] for c in self.call("GET", f"/tasks/{task_id}").get("comments", [])] if not self.dry else []

    def note(self, task_id, text, source=None):
        """Adds the provenance comment once; True when it was added now."""
        body = f"{self.label}: {text}" + (f"\n\nSource: {source}" if source else "")
        if body in self.comments(task_id):
            return False
        self.call("POST", f"/tasks/{task_id}/comments", {"author": "board-audit", "body": body})
        return True

    def patch(self, task_id, fields):
        if fields:
            self.call("PATCH", f"/tasks/{task_id}", fields)

    def tags_after(self, task, add=(), remove=()):
        tags = [t for t in task.get("tags", []) if t not in set(remove)]
        for t in add:
            if t not in tags:
                tags.append(t)
        return ",".join(tags)

    # ---- actions ----
    def create(self, a):
        project = a["project"]
        if project not in self.projects:
            raise SystemExit(f"no project {project!r}; create it first (and link its repo)")
        existing = self.find(project, a["title"])
        if existing:
            self.log.append(f"skip create (exists as {project}#{existing['id']}): {a['title']}")
            if a.get("comment"):
                self.note(existing["id"], a["comment"], a.get("source"))
            return
        t = self.call("POST", "/tasks", {"project": project, "title": a["title"], "description": a.get("description", "")})
        tid = t.get("id", "?")
        fields = {}
        if a.get("status") and a["status"] != "todo":
            fields["status"] = a["status"]
        if a.get("tags"):
            fields["tags"] = ",".join(dict.fromkeys(list(a["tags"]) + ["audit"]))
        else:
            fields["tags"] = "audit"
        if a.get("human"):
            fields["human"] = True
        if a.get("url"):
            fields["url"] = a["url"]
        self.patch(tid, fields)
        if tid != "?":
            self.note(tid, a.get("comment") or "created", a.get("source"))
        self.log.append(f"created {project}#{tid} [{a.get('status','todo')}]: {a['title']}")

    def move(self, a):
        t = self.tasks.get(a["id"])
        if not t:
            raise SystemExit(f"no card #{a['id']}")
        fields = {}
        if a.get("status") and a["status"] != t["status"]:
            fields["status"] = a["status"]
        if "human" in a and bool(a["human"]) != bool(t.get("human")):
            fields["human"] = bool(a["human"])
        if a.get("tags_add") or a.get("tags_remove"):
            tags = self.tags_after(t, a.get("tags_add", []) + ["audit"], a.get("tags_remove", []))
            if tags != ",".join(t.get("tags", [])):
                fields["tags"] = tags
        if a.get("url") and a["url"] != t.get("url"):
            fields["url"] = a["url"]
        if not fields:
            self.log.append(f"skip move (already {t['status']}): {t['project']}#{t['id']}")
            return
        self.patch(t["id"], fields)
        self.note(t["id"], a.get("comment") or f"{t['status']} -> {a.get('status', t['status'])}", a.get("source"))
        self.log.append(f"moved {t['project']}#{t['id']} {t['status']} -> {a.get('status', t['status'])}: {t['title']}")

    def comment(self, a):
        t = self.tasks.get(a["id"])
        if not t:
            raise SystemExit(f"no card #{a['id']}")
        if a.get("tags_add") or a.get("tags_remove"):
            tags = self.tags_after(t, a.get("tags_add", []), a.get("tags_remove", []))
            if tags != ",".join(t.get("tags", [])):
                self.patch(t["id"], {"tags": tags})
        if self.note(t["id"], a["comment"], a.get("source")):
            self.log.append(f"commented {t['project']}#{t['id']}: {a['comment'][:60]}")
        else:
            self.log.append(f"skip comment (already there): {t['project']}#{t['id']}")

    def merge(self, a):
        t, into = self.tasks.get(a["id"]), self.tasks.get(a["into"])
        if not t or not into:
            raise SystemExit(f"merge: no card #{a['id']} or #{a['into']}")
        if t["status"] == "done":
            self.log.append(f"skip merge (already done): {t['project']}#{t['id']}")
            return
        body = f"Merged from {t['project']}#{t['id']} \"{t['title']}\"" + (f":\n\n{t['description']}" if t.get("description") else "")
        self.note(into["id"], body, a.get("source"))
        if t.get("tags"):
            self.patch(into["id"], {"tags": self.tags_after(into, t["tags"])})
        self.patch(t["id"], {"status": "done", "tags": self.tags_after(t, ["audit", "duplicate"])})
        self.note(t["id"], a.get("comment") or f"duplicate of {into['project']}#{into['id']}", a.get("source"))
        self.log.append(f"merged {t['project']}#{t['id']} into {into['project']}#{into['id']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default="http://127.0.0.1:4242/api")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--label", default="Board audit")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    plan = json.load(open(a.plan))
    board = Board(a.api, a.dry_run, a.label)
    ops = {"create": board.create, "move": board.move, "comment": board.comment, "merge": board.merge}
    for i, action in enumerate(plan):
        op = action.get("op")
        if op not in ops:
            raise SystemExit(f"action {i}: unknown op {op!r}")
        ops[op](action)
        if not a.dry_run:
            board.reload()
    print("\n".join(board.log) or "nothing to do")
    print(f"{'dry run, ' if a.dry_run else ''}{len(plan)} actions, {sum(1 for l in board.log if not l.startswith(('skip', 'would')))} applied")


if __name__ == "__main__":
    main()
