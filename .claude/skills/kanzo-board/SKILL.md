---
name: kanzo-board
description: How agents keep Dave's Kanzo board (kanzo.blenko.cloud, 83 repo projects) authoritative - taking, creating, moving and closing cards through the kanzo MCP tools, the card conventions (titles, statuses, tags, human flag, provenance comments), the start and end of session routine, and the audit playbook that reconciles the board with git history, session transcripts and memory. Use at the start of any coding session on a repo with a Kanzo project, whenever work is taken, finished or discovered, and when asked to audit, reconcile, clean up or report on the board.
---

# Managing the Kanzo board

The board is the one place where Dave and every agent see what is happening. It is authoritative only if every agent writes to it: take the card before the work, comment as you go, move it when you finish, and create the card when you discover work. The hooks in `agent/hooks/` remind you; this skill says how.

## The tools

Through the `kanzo` MCP server (`kanzo mcp`, or `https://kanzo.blenko.cloud/mcp` behind Access on the VPS; `http://127.0.0.1:8111/mcp` on the VPS itself):

| Tool | Use it to |
| --- | --- |
| `list_projects` | find the project for this repo (projects are linked to GitHub `owner/repo`; the agent is matched by the git remote) |
| `list_tasks` | see the board for a project, or filter with `tag:`, `status:`, `is:human`, `-status:done` |
| `get_task` | read a card with its comments before touching it |
| `next_task` | take the top `todo` card: it moves to `doing` |
| `create_task` | add a card; pass `status` for backlog or done, `tags`, `human`, `url` |
| `update_task` | change title, description, status, tags, human, url, project |
| `add_comment` | log progress, decisions, blockers, and the closing summary |
| `create_project`, `link_repo` | only for real work that has no project yet |

Scripts and hooks use the same operations over the HTTP API (`/api/board`, `/api/tasks`, `/api/tasks/{id}/comments`); `tools/board-audit/apply.py` wraps them.

## Routine

**Starting a session in a repo**

1. Find the project (`list_projects`, match the remote). No project: ask before creating one, unless the repo is clearly new work of Dave's.
2. Read `doing` and `todo` for it (`list_tasks`). If the user's request matches a card, take that card: `update_task` to `doing`, or `next_task` when they said "take the next task".
3. If the request matches nothing, create the card first (title in the conventions below, status `doing`), then work. Never work on something the board does not show.
4. Write the card's key to the repo's marker so the stop hook can check on you: `echo kanzo#12 > "$(git rev-parse --git-dir)/kanzo-task"`.

**While working**

- One comment per meaningful step: what changed, what you decided, what is blocked and on whom. Short, present tense, with the commit, PR or file.
- A blocker: add the tag `blocked`, say in a comment what unblocks it, and if only Dave can unblock it, set `human`.
- Work that turns up on the way goes on its own card (`todo`, or `backlog` if it is an idea), linked from the comment, never into the current card's scope.

**Finishing**

1. Closing comment: what shipped, how it was verified (tests, screenshot, URL), what is left if anything.
2. Move the card: `done` when shipped and verified; `review` when Dave must look (set `human` and say what to look at); back to `todo` with a comment if you stopped early.
3. Set `url` to the PR or the page when there is one.
4. Remove the marker: `rm -f "$(git rev-parse --git-dir)/kanzo-task"`.

**Never**

- Take a `human` card, a `backlog` card, or a card that `waits_on` unfinished work.
- Delete cards or projects. Merge duplicates instead: move the content into the older card and close the newer with the comment `duplicate of kanzo#N`.
- Leave a card in `doing` with no comment in the last 7 days.
- Rewrite history on a card: comments are the log, append to them.

## Card conventions

The full rules are in `references/card-conventions.md`. The short form:

| Field | Rule |
| --- | --- |
| Title | imperative, specific, under 80 characters, no trailing period: "Add the theme switcher to the sidebar", not "Theme stuff" |
| Status | `backlog` parked idea, agents leave it; `todo` agreed and ready; `doing` in flight now, one owner; `review` needs Dave's eyes; `done` shipped and verified |
| Human | on only when just Dave can do it: approvals, credentials, purchases, physical actions, pushes he gated |
| Tags | lowercase; area tags (`infra`, `security`, `ui`, `theme`, `docs`, `mcp`, `deploy`, `data`, `ops`, `network`, `cameras`, `pricing`); alert tags `blocked`, `urgent`, `p0`, `p1`, `bug`; origin tags `agent`, `audit` |
| Description | Markdown: **Why** (one line), **What** (bullets), **Done when** (checkable), **Links** (session, commit, PR, URL, memory file) |
| Comments | provenance first (`Board audit 2026-11-11: created from <source>`), then the log, then the closing summary |
| URL | the PR, the deployed page, or the doc |

## Audit playbook

For reconciling the board with reality (first time, or monthly): `references/audit-playbook.md`. In short: back up the database, dump the board, collect evidence with `tools/board-audit/collect.py` (memory files, session transcripts, git logs, running services), write a plan of creates, moves and merges, review it, apply it with `tools/board-audit/apply.py` (idempotent, provenance comments), write the report to `docs/audit/`, and leave the open questions for Dave as `human` cards tagged `audit`.

## The board itself

| | |
| --- | --- |
| URL | https://kanzo.blenko.cloud (Cloudflare Access, Dave only) |
| On the VPS | `http://127.0.0.1:8111`, data in `~/.local/share/kanzo`, units `kanzo-web` and `kanzo-tunnel`, binary from `~/dev/kanzo` branch `public-url` |
| Projects | one per active repo in `blenko-glass-company` (blue) and `dwertz-at-blenko` (purple), seeded by `deploy/seed-projects.sh` on the VPS; add a project by hand only for work with no repo |
| Columns | backlog, todo, doing, review, done |
| Themes | Kanzo, LCARS, Skeletal (switcher at the bottom of the sidebar; the pick is saved on the board) |
