# kanzo-dispatch

Hand a card to Claude by commenting on it. Your comment goes to whoever is working the card; if
nobody is, a new agent starts. `kanzo_dispatch.py` watches the board and never puts two agents on
one card.

## What a comment does

| Comment by Dave (author `me`) | Result |
|---|---|
| Any comment, card has no session, and a live session already works the card | Handed to that session (see "Who works the card"); the card gets one comment, "Sent to the session already on this card", with its name and app link. Later comments go there too |
| Any comment, card has no session and nobody works it (a leading `@claude` is optional and dropped) | Starts a session for the card and comments "Picked up" with the app link and the folder |
| Any comment, card has a session (done or not) | Typed into that same session; if it is gone, it goes to whoever else works the card, else the card's earlier session is resumed, else a new one starts from the card's history |
| Any comment, the dispatcher's own session for the card ended or was stopped | `claude --bg --resume <session id> <comment>` in its folder, so it keeps the conversation; a "Resumed" comment. If the resume fails, a new session starts from the full card |
| `@claude start` | Starts an agent with the card as the brief. A live session already on the card: one comment naming it, nothing starts |
| Owner found but not reachable (no herdr pane, or delivery failed twice) | No second agent. A comment says the session is on the card and could not be reached, `@claude new` starts a separate one; human set |
| `@claude new ...` | Skips the search and starts a separate session, never a resume. A session the dispatcher started for the card is stopped first; any other is left alone |
| `@claude stop` | Dispatcher's own session: `claude stop`, then a comment. A session it did not start: only detached from the card (never stopped), and the next comment starts a new agent. A follow-up still waiting for the session is marked failed |
| Starts with `[note]` (the board's "Do not send to agent" box adds it) | Nothing: no session starts and an attached session is not told |
| New comments on more than `--burst` (5) cards in one pass | Nothing starts and nothing is typed in: each comment is marked `held` in status.json, one log line, no card comments. A later comment on one of those cards behaves normally |

Comments by anyone else (agents, the dispatcher itself) never start or reach a session.

When a comment that started or reached a session has pictures, the typed line ends with "(N
picture(s) attached to this comment: get_task on the card lists the file paths, read them.)". No
paths are typed.

## Nudge, then flag

Dave reads only the card, so a session that answers only in its own terminal has not answered.
After a comment is started or delivered, when the session's turn ends (not working, at least 20 s
after delivery) and no later comment on the card comes from anyone other than `me` or
`kanzo-dispatch`, the dispatcher types one line into the session:

    Card <key>: Dave has no answer on the card yet and he reads only the card. Post your answer or
    your question as a card comment now with the kanzo tools, and set the card status.

If the next turn also ends without an answer: the comment "The agent finished without answering
here. Open the session to see what it did: <link>. Comment again to retry.", human set, card state
`silent`. At most one nudge per delivered comment, never into a working or blocked session. A card
moved to review or done counts as answered. Adopted sessions get the nudge too, and the flag while
they are alive.

## Sessions that wait on Dave

A background session that ends its turn needing Dave is listed by `claude agents --json --all` as
`blocked`, usually with no process, and its job file (`~/.claude/jobs/<id>/state.json`) holds
`needs` (one line), `updatedAt` and sometimes `suggestedReply`. The dispatcher puts that on the board:

1. Episode: (session id, `updatedAt`). One ask per episode, once it has been blocked `--ask-after`
   seconds (120; from `updatedAt`, else from when the dispatcher first saw it). The job file's state
   decides when it has one, since the session list is read at most every 15 s.
2. The card, in this order: the card the dispatcher holds for the session; the card of the
   session's earlier ask; the open card the session last wrote to through Kanzo; else a new card
   `Answer: <session name>` (doing, human, tags `agent,ask`) in the project whose repo matches the
   session's folder (a worktree under it too), else in `--ask-project` (`vps-ops`). That project
   missing: a log line, no ask, a retry a minute later.
3. One comment as `kanzo-dispatch`, and the human flag (so the Inbox and the push see it):

       **<session name> waits on you.**

       <needs, 700 chars at most>

       Suggested reply: <when the job has one>

       Answer in a comment here and it goes to that session. [Open in Claude](<link>)

4. The card is attached to the session like an adopted one (never stopped, outside `--max`, `@claude
   stop` only detaches). Dave's comment there goes to it: typed in when its process is idle with the
   input box free; `claude --bg --resume <session id> <follow-up>` when it has no process (receipt
   `delivered`, no card comment); never typed into a permission prompt or a question dialog (receipt
   `waiting`, its note says to open the app). A blocked session is never nudged.
5. The episode ends when the session no longer waits or leaves the list: the human flag goes off
   when the dispatcher set it, and `POST /api/inbox/clear` takes the card out of the Inbox (best
   effort: a 404 is ignored). When an agent other than the dispatcher wrote the card's newest
   comment, both are left alone: it is an answer Dave has not seen. Nothing is posted.
6. At most 3 new asks per pass. A later block (new `updatedAt`) comments again on the same card.
   `--no-asks` turns it off; `--dry-run` writes nothing.

state.json keeps `asks` (`{session id: {task, comment, since, at, flagged, created}}`; `flagged` and
`created` say whether the dispatcher set the flag or made the card) and `ask_cards` (each session's
ask card, so a later episode finds it; dropped after 7 days).

## Who works the card

1. Candidates: `claude agents --json --all` items that are live (listed with a pid and a status,
   and the process exists). Exited background items have no status.
2. A candidate works the card when its transcript (`~/.claude/projects/*/<sessionId>.jsonl`, plus
   its `subagents/`) holds a kanzo MCP write on that card that did not fail: `update_task` or
   `add_comment` with the card in `id`, or `create_task` / `next_task` whose reply names it. Reads
   (`get_task`, `list_tasks`) do not count. Lines without `mcp__kanzo__` are skipped before any
   JSON parsing.
3. The owner is the candidate with the most recent such write. A session `@claude stop` detached
   from a card is never picked for that card again.
4. Background owner: attached to the card like a session the dispatcher started (short id,
   session id, app link) and the comment goes in with `claude attach`. Interactive owner: the
   comment goes in with `herdr agent prompt <pane> <text>`; the pane is found again before every
   delivery (by session id, else by terminal title = session name).
5. The text typed in is one line starting `Dave commented on card <key> (<title>): ...`, since an
   adopted session may work several cards.
6. An adopted session is outside the `--max` cap and the watchdog: it was not started for the
   card, so only a failed delivery and the nudge apply. It is never stopped or resumed.

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
- Never two sessions for one card, counting sessions Dave opened himself (terminal, app, Slack).
- A follow-up for a busy session waits; after 30 minutes the card says it did not get through and is set for the human.
- A session that is gone is resumed with the comment; one that will not take a follow-up is stopped
  and replaced by a new one built from the full card.
- A session that goes idle without answering: the nudge, then the flag (above). A session that
  ends or waits on a permission with nothing written to the card since pickup (or since the last
  follow-up): a comment with the app link, and human set.
- A session the dispatcher started, on a card that is done, idle for `--idle-stop` seconds (3600;
  0 turns it off) with nothing waiting: `claude stop`, no card comment. The next comment resumes it.
- `claude` fails to start: a comment with the short error, human set. Two failures in a row for the
  same cause: no more starts until the dispatcher restarts (logged, and each later ask is told so).

## How it talks to the board and to Claude

| Step | How |
|---|---|
| New comments | `GET /api/activity` (rows after the last seen), then `GET /api/tasks/{id}` |
| Folder | `GET /api/board?project=<slug>` for the project's repos |
| Its comments and the human flag | MCP on `/mcp` (`add_comment` with author `kanzo-dispatch`, `update_task`). The HTTP API signs every comment `me`, so it cannot be used |
| First run | Reads `max(id)` from `comments` in `kanzo.db`, read-only, once. Nothing older is ever acted on |
| Done cards | One read-only query on `kanzo.db` (`--db`): `SELECT id FROM tasks WHERE status = 'done' AND id IN (...)`. Cheaper than `GET /api/board?project=all&done=all`, which returns every card with its description |
| Start | `claude --bg --name "<key> <title>" "<brief>"` in the folder; the short id comes from its output |
| Resume | `claude --bg --resume <session id> "<follow-up>"` in the session's original folder |
| Find | `claude agents --json --all`; `~/.claude/jobs/<id>/state.json` for the session id and app link |
| Owner | the live sessions' transcripts under `~/.claude/projects/`, read-only and incremental: each file is read from where the last pass stopped |
| Follow-up, background session | `claude attach <id>` in a private tmux: wait for the prompt, type the text as one line, Enter, check the transcript, Ctrl+Z |
| Follow-up, terminal session | `herdr agent list` for the pane, then `herdr agent prompt <pane> <text>` (`--herdr`, default `~/.local/bin/herdr`); waits while the pane is working or blocked |
| Stop | `claude stop <id>`, only ever on a session the dispatcher started |
| Ask card | `GET /api/board?project=all` for the projects' repos, `POST /api/tasks`, then `PATCH /api/tasks/{id}` (status, human, tags); only when a waiting session has no card |
| Inbox clear | `POST /api/inbox/clear` with `{"tasks": [id]}` when an ask ends; errors (404 included) logged at debug level |

`claude`, `tmux` and `herdr` get the dispatcher's environment minus anything named like a token,
secret, password, credential, API or private key, or VAPID. The push key is also taken out of the
dispatcher's own environment before anything else runs (see Push).

## State

`~/.local/share/kanzo/dispatch/state.json` (`--state`), written atomically: the last activity and
comment ids seen, the card to session map (adopted sessions marked, with their kind and pane),
the sessions detached from each card, and the queue. A comment id is saved as seen before the
dispatcher acts on it, and a start is recorded before `claude` runs: a crash can drop an ask, but
never start twice. A start cut off by a crash is adopted if its session shows up by name, otherwise
the card is told to ask again. Per card it also keeps `receipts`: the newest 20 handled comments
with their state, a sticky card state (`failed`, `unreachable`, `silent`) and, for a session the
dispatcher started, what a resume needs (session id, name, folder, link). A card untouched for 7
days is dropped.

## What the board shows

Every pass (`--interval`) the dispatcher rewrites `status.json` beside `state.json` (`--status`),
mode 0600, atomically. The board reads it; the shape is fixed by a contract the two share:

| Field | What |
|---|---|
| `updated` | Heartbeat, rewritten every pass even when nothing changed. Older than 30 s: the watcher is down |
| `rev` | Bumped only when anything besides `updated` changed; carried across restarts |
| `interval`, `max` | The loop interval and the `--max` cap |
| `sessions` | Every live Claude session on the box, plus background sessions that wait on Dave with no process: `id` (short), `session_id`, `name`, `kind`, `state` (`working`, `idle`, `blocked`, `ended`), `detail` (160 chars; the `needs` line for a waiting session), `folder`, `started`, `tokens`, `link`, `origin` (`dispatch`, `adopted`, `other`), `cards` it owns (a waiting session with no process: only its ask card) |
| `cards` | Keyed by task id: `session`, `origin`, `state` (`queued`, `starting`, `working`, `idle`, `blocked`, `ended`, `unreachable`, `silent`, `failed`), `since`, `note`, and `comments`: a receipt per handled comment (`started`, `delivered`, `waiting`, `queued`, `failed`, `held`, `command`) with `at` and `note` |
| `queue` | Starts waiting for a free slot, oldest first: `task`, `comment`, `at` |
| `push_key` | The Web Push public key (uncompressed P-256 point, base64url, no padding); "" when push is off. The board hands it to the page at `GET /api/push` |
| `push` | `on`, `subscriptions` (browsers it would send to), `last_sent` (ISO or ""), `last_error` (host and code only). Never an endpoint |
| `asks` | `on` (false with `--no-asks`), `open` (episodes with a posted ask). A card with an open ask has `state` `blocked` and `note` the `needs` line. Another tool reads `asks.on` with `push.on`, `push.subscriptions` and `updated` to tell whether Kanzo handles waiting-on-Dave notices: keep these names |

A card's owner is the dispatcher's attached session, else the live session with the latest kanzo
write on it; a done card shows no owner unless the dispatcher has a session on it. The session list
is read at most every 15 s just for this file (sooner when the dispatcher needed it anyway); the
job file (`~/.claude/jobs/<id>/state.json`) is read every pass for `detail`, `tokens` and `link`;
transcripts are read incrementally, at most 64 MiB per pass, so a restart catches up over a few
passes. `[note]` comments never appear. "Answered" is not in the file: the board works it out.

## Push

The board can notify a phone when an agent answers, with the board closed. The page subscribes the
browser (Settings, Notifications) and the board keeps the subscription in its `meta` table
(`push_subscriptions`, at most 10, only `https://` endpoints on FCM, Mozilla, Windows or Apple push
hosts). Each pass the dispatcher reads `GET /api/inbox` once: a card new in `waiting`, or whose last
comment moved, is news. The first pass after a start only records. Then every browser is owed one
push; a round sends at most one push per browser, at most every 30 s, in a thread with a 10 s
timeout, so several answers collapse into one push and a slow push service never holds up a pass.

| What | How |
|---|---|
| Key | `KANZO_VAPID_PRIVATE_KEY` from the vault (`deploy/run-dispatch.sh`): base64 of the SEC1 DER of a P-256 private key, one line. Taken out of `os.environ` before anything else runs, never logged, never written. Missing or unreadable: push is off, one log line, everything else works |
| Make a key | In your own terminal: `openssl ecparam -genkey -name prime256v1 -noout -outform DER \| base64 -w0`, then paste the line into the vault web app as `KANZO_VAPID_PRIVATE_KEY` in the dispatcher's project. A new key means every browser switches notifications on again |
| Signing | ES256 through the system `cryptography` package, imported only when a key is set: the one import outside the standard library. Missing: push is off, one log line |
| Subscriptions | Read-only from `kanzo.db` (`--db`), never over HTTP: no route returns an endpoint. The board has no loopback-only route it could trust, because the public URL arrives through cloudflared on loopback too |
| What leaves the box | One empty POST per browser to its push service: `Authorization: vapid t=<JWT>, k=<public key>`, `TTL: 3600`, `Urgency: normal`, `Topic: kanzo-inbox`, `Content-Length: 0`, no body. JWT: `aud` the push service origin, `exp` 12 h (reused until an hour before), `sub` `https://kanzo.blenko.cloud`. Nothing about a card: the phone's service worker reads the inbox itself, with its own login |
| 404 or 410 | The browser is gone: a hash of its endpoint goes to `push_dead` in state.json (newest 50) and it is skipped from then on. The board's row stays until the page unsubscribes or 10 newer browsers push it out |
| 429, 5xx, no answer | That browser waits 60 s, doubling to an hour (or `Retry-After`), then gets the push it is owed; owed pushes expire after an hour |
| Off | `--no-push`, or no key |

The phone: when the board's login (Cloudflare Access, 24 h) has expired, the worker cannot read the
inbox and shows "Kanzo: an agent answered"; a tap opens the Inbox, which asks for the login.

## Run

    agent/dispatch/kanzo_dispatch.py --once --dry-run   # what it would do; writes nothing
    agent/dispatch/kanzo_dispatch.py --loop --interval 5
    agent/dispatch/kanzo_dispatch.py --loop --burst 5 --idle-stop 3600   # the defaults
    agent/dispatch/kanzo_dispatch.py --loop --no-push   # no Web Push even with the key set
    agent/dispatch/kanzo_dispatch.py --loop --ask-after 120 --ask-project vps-ops   # the asks defaults
    agent/dispatch/kanzo_dispatch.py --loop --no-asks   # leave waiting sessions off the board
    python3 agent/dispatch/test_dispatch.py             # throwaway board on 127.0.0.1:8198, fake claude, herdr and push service
    KANZO_TEST_PORT=8196 python3 -W error::ResourceWarning agent/dispatch/test_dispatch.py   # another port

The service is `deploy/kanzo-dispatch.service` (see `deploy/README.md`).

---

> **Version:** v5 · created 2026-11-21 13:26 EST · updated 2026-11-28
> **Changes:** v5 2026-11-28 "Sessions that wait on Dave": a blocked background session's ask on its card, his answer resumes or reaches it; `asks` in status.json; flags `--ask-after`, `--ask-project`, `--no-asks`. v4 2026-11-24 Push: content-free Web Push when an agent answers, `push_key` and `push` in status.json, `--no-push`. v3 2026-11-24 status.json for the board ("What the board shows"); `@claude start`; nudge, then flag; burst guard; resume an ended session; idle stop on done cards; pictures in a follow-up; flags `--status`, `--burst`, `--idle-stop`. v2 2026-11-23 a comment goes to the live session already on the card; `@claude new`; stop only detaches an adopted session. v1 initial.
> **This file:** /home/claude/dev/kanzo/agent/dispatch/README.md (repo dwertz-at-blenko/kanzo, branch public-url)
> **Session:** b1475961-06d7-40b0-9eff-f2b47896b6bb · vps as claude · cwd /home/claude/dev
