# kanzo-dispatch

Hand a card to Claude by commenting on it. `kanzo_dispatch.py` watches the board and runs one
background Claude Code session per card.

## What a comment does

| Comment by Dave (author `me`) | Result |
|---|---|
| Any comment, card has no session (a leading `@claude` is optional and dropped) | Starts a session for the card and comments "Picked up" with the app link and the folder |
| Any comment, card has a session (done or not) | Typed into that same session; if it is gone, a new one starts from the card's history |
| `@claude stop` | `claude stop` on the card's session, then a comment saying so |
| Starts with `[note]` (the board's "Do not send to agent" box adds it) | Nothing: no session starts and an attached session is not told |

Comments by anyone else (agents, the dispatcher itself) never start or reach a session.

## Where the session runs

- Project linked to a repo (`owner/name`) and `<dev-root>/<name>` exists: there.
- Project with no repo: `<dev-root>`.
- Linked but no checkout on this box: a comment says so, the card is set for the human, nothing starts.

The session gets a brief built from the card: key, title, description, every comment in order,
the ask, the link, and the rules (own the card through the kanzo MCP tools, set doing, comment
progress, final answer in a comment, done only when Done when is verified, otherwise review plus
human, questions as a comment with human set, standing guardrails). Its name starts with the card
key, like `api#12 Fix the login`, so `claude attach api#12` finds it.

## Limits and the watchdog

- At most `--max` (3) sessions working at once. Extra asks get a "Queued" comment and start when one finishes.
- Never two sessions for one card.
- A follow-up for a busy session waits; after 30 minutes the card says it did not get through and is set for the human.
- A session that is gone, or will not take a follow-up, is stopped and replaced by a new one built from the full card.
- A session that ends, goes idle or waits on a permission with nothing written to the card since
  pickup (or since the last follow-up): a comment with the app link, and human set.
- `claude` fails to start: a comment with the short error, human set. Two failures in a row for the
  same cause: no more starts until the dispatcher restarts (logged, and each later ask is told so).

## How it talks to the board and to Claude

| Step | How |
|---|---|
| New comments | `GET /api/activity` (rows after the last seen), then `GET /api/tasks/{id}` |
| Folder | `GET /api/board?project=<slug>` for the project's repos |
| Its comments and the human flag | MCP on `/mcp` (`add_comment` with author `kanzo-dispatch`, `update_task`). The HTTP API signs every comment `me`, so it cannot be used |
| First run | Reads `max(id)` from `comments` in `kanzo.db`, read-only, once. Nothing older is ever acted on |
| Start | `claude --bg --name "<key> <title>" "<brief>"` in the folder; the short id comes from its output |
| Find | `claude agents --json --all`; `~/.claude/jobs/<id>/state.json` for the session id and app link |
| Follow-up | `claude attach <id>` in a private tmux: wait for the prompt, type the text as one line, Enter, check the transcript, Ctrl+Z |
| Stop | `claude stop <id>` |

`claude` and `tmux` get the dispatcher's environment minus anything named like a token, secret,
password, credential or key.

## State

`~/.local/share/kanzo/dispatch/state.json` (`--state`), written atomically: the last activity and
comment ids seen, the card to session map, and the queue. A comment id is saved as seen before the
dispatcher acts on it, and a start is recorded before `claude` runs: a crash can drop an ask, but
never start twice. A start cut off by a crash is adopted if its session shows up by name, otherwise
the card is told to ask again.

## Run

    agent/dispatch/kanzo_dispatch.py --once --dry-run   # what it would do; writes nothing
    agent/dispatch/kanzo_dispatch.py --loop --interval 5
    python3 agent/dispatch/test_dispatch.py             # throwaway board on 127.0.0.1:8198, fake claude

The service is `deploy/kanzo-dispatch.service` (see `deploy/README.md`).

---

> **Version:** v1 · created 2026-11-21 13:26 EST · updated 2026-11-21 13:26 EST
> **Changes:** v1 initial.
> **This file:** /home/claude/dev/kanzo/agent/dispatch/README.md (repo dwertz-at-blenko/kanzo, branch public-url)
> **Session:** b1475961-06d7-40b0-9eff-f2b47896b6bb · vps as claude · cwd /home/claude/dev
