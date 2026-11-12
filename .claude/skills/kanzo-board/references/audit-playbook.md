# Board audit playbook

Reconciles the board with what actually happened: git history, session transcripts, memory files, running services. First run on the VPS after the deployment; then monthly, or whenever the board feels stale.

## 0. Safety

```bash
D=~/.local/share/kanzo
mkdir -p $D/backups && cp $D/kanzo.db $D/backups/kanzo-$(date +%F)-pre-audit.db
mkdir -p ~/dev/kanzo/docs/audit && curl -s http://127.0.0.1:8111/api/board > ~/dev/kanzo/docs/audit/board-before-$(date +%F).json
```

Never delete cards or projects during an audit. Everything is a create, a move, a comment or a merge.

## 1. Collect

```bash
python3 ~/dev/kanzo/tools/board-audit/collect.py \
  --api http://127.0.0.1:8111/api \
  --memory ~/.claude/memory \
  --sessions ~/.claude/projects \
  --repos ~/dev \
  --since 2026-08-01 \
  --out ~/dev/kanzo/docs/audit/inventory-$(date +%F)
```

It writes `inventory.md` (to read) and `inventory.json` (to script against), grouped by project:

- for each repo: its remote, commits since the date, unmerged branches, worktrees, TODO and FIXME counts, and the board project it maps to;
- for each session transcript: title or first prompt, last assistant text, pending lists from compaction summaries, dates;
- for each memory file: title, last change, lines that read like open items (TODO, pending, next, blocked, "Your move");
- the board as it is: projects, cards per status, cards in `doing` with no recent comment, `blocked` cards.

Add what the script cannot see: `systemctl --user list-units`, the Cloudflare site inventory, open PRs and issues (`gh` through `bws run`), anything Dave said in chat.

## 2. Decide

Read the inventory per project and write `plan.json`, a list of actions:

```json
[
  {"op": "create", "project": "kanzo", "title": "Merge upstream 0.4.2 into public-url", "status": "todo",
   "tags": ["deploy", "audit"], "human": false,
   "description": "**Why** ...\n\n**What**\n- ...\n\n**Done when**\n- [ ] ...\n\n**Links** ...",
   "source": "memory project_kanzo_blenko_cloud.md, 2026-11-07"},
  {"op": "move", "id": 42, "status": "done", "comment": "Shipped in 667f8ef (LCARS v0.2 toggle)", "source": "git log ~/dev/kanzo"},
  {"op": "comment", "id": 17, "comment": "Still blocked: waiting on the vendor's firmware", "tags_add": ["blocked"]},
  {"op": "merge", "id": 58, "into": 31, "comment": "duplicate of kanzo#31"}
]
```

Rules while deciding:

- One card per deliverable or decision, not per commit or per session.
- Shipped and verified in the window: `done`, with the closing comment naming the commit or URL.
- In flight (a session or branch touched it in the last 7 days): `doing`, with a comment on where it stands.
- Agreed next steps ("Your move" lists, pending tasks in summaries, Dave's asks): `todo`.
- Ideas, maybes, "later": `backlog`.
- Only Dave can act: `human`, and the comment says exactly what he needs to do.
- Every created or changed card gets the tag `audit` and a provenance comment.
- Match existing cards before creating: same project and a similar title means a move or a comment, not a new card.

## 3. Apply

```bash
python3 ~/dev/kanzo/tools/board-audit/apply.py --api http://127.0.0.1:8111/api --plan plan.json --dry-run
python3 ~/dev/kanzo/tools/board-audit/apply.py --api http://127.0.0.1:8111/api --plan plan.json --label "Board audit $(date +%F)"
```

`apply.py` is idempotent: a `create` whose project and title already exist becomes a no-op (or a comment, if the plan carries one), and every write leaves a provenance comment that starts with the label. Run it twice and the board is the same.

## 4. Report

Write `docs/audit/board-audit-<date>.md`: sources read, counts before and after per status, the changes per project, duplicates merged, open questions for Dave (also on the board as `human` cards tagged `audit`), and what could not be verified. Commit it with the plan and the inventory.

## 5. Keep it true

The audit fixes the past; the routine in `SKILL.md` and the hooks in `agent/hooks/` keep the future honest. If the same kind of drift shows up twice, fix the routine, not just the cards.
