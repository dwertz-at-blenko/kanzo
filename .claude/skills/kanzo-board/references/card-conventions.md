# Card conventions

A card is a unit of work or a decision that someone can pick up cold. If a stranger could not start from the card alone, it is not finished being written.

## Title

- Imperative and specific: "Rotate the Cloudflare tunnel token", "Decide whether to merge upstream 0.4.2".
- Under 80 characters. The project is already on the card: no "Kanzo:" prefix.
- No trailing period, no ticket numbers (the key is the number), no dates (the timestamps are on the card).
- One deliverable per card. "Fix X and also Y" is two cards.

## Status

| Status | Means | Agents |
| --- | --- | --- |
| `backlog` | an idea or parked work, not agreed | leave it alone |
| `todo` | agreed, ready to start, no unfinished dependency | may take it, top first |
| `doing` | in flight right now, one owner, a comment within 7 days | the owner updates it |
| `review` | the work is done and waits on Dave: a look, an approval, a decision | set `human`, say what to look at |
| `done` | shipped and verified, with a closing comment | read only |

A card that waits on another: say so in the description (`Waits on kanzo#12`) until the board has `waits_on` (upstream 0.4).

## Human

On when only Dave can do the next step: approve, pay, plug in, sign in, push something he gated, make a judgement that is his. Off again as soon as he has done it and an agent can continue. The human icon on a card is a request for his attention, so do not waste it.

## Tags

All lowercase, one word or a hyphenated word, comma separated in the API.

| Kind | Tags | Effect |
| --- | --- | --- |
| Area | `infra`, `security`, `ui`, `theme`, `docs`, `mcp`, `deploy`, `data`, `ops`, `network`, `cameras`, `pricing`, `shop`, `home` (add sparingly) | filter and group |
| Alert | `blocked`, `urgent`, `p0` (red); `p1`, `bug` (amber) | the LCARS and Skeletal themes colour and pulse the card |
| Origin | `agent` (an agent created it from discovered work), `audit` (the reconciliation pass created or changed it) | provenance at a glance |

`blocked` always comes with a comment naming the blocker. Remove it the moment it no longer holds.

## Description

Markdown, four short sections, in this order:

```
**Why** one line on the reason this matters.

**What**
- the pieces of work

**Done when**
- [ ] a checkable outcome
- [ ] another

**Links** session "kanzo-vps-deployment" (2026-11-07), commit 667f8ef, https://..., memory project_kanzo_blenko_cloud.md
```

Keep the description current: when scope changes, edit it and say so in a comment.

## Comments

- The first comment on a card an agent created says where it came from: `Created from session "<title>" (<date>): <one line>`, or for the audit: `Board audit 2026-11-11: created from <source>`.
- Progress comments: present tense, with the artefact (commit, PR, file, URL). One per step, not per minute.
- A status change gets a comment when the reason is not obvious: `review: needs Dave to approve the Access policy`, `todo: stopped, out of context; next step is ...`.
- The closing comment on `done`: what shipped, how it was verified, what was left out.
- Never delete a comment; correct it with a new one.

## URL

The pull request while it is open, the merged commit or the deployed page after. One URL; more go in Links.

## Projects

- One project per repo, slug = repo name (the seed used that), colour blue for `blenko-glass-company`, purple for `dwertz-at-blenko`.
- Work with no repo (the VPS itself, the home network, the shop cameras) gets a clearly named project: "VPS ops", "Home network", "Shop cameras". Ask before creating one.
- Never delete a project. A project whose repo is archived keeps its done cards.

## Hygiene, weekly

- `doing` cards without a comment in 7 days: comment, move to `todo`, or ask.
- `blocked` cards: is the blocker still real?
- `review` cards older than 7 days: ping Dave in the session summary.
- `done` cards with no closing comment: add one from the commit or PR.
- Duplicates: merge into the older card.
