# VPS handoff, 2026-11-11: push the fork, audit the board, deploy the themes

For the Claude session on the VPS (`kanzo-vps-deployment`, the one that deployed kanzo.blenko.cloud and built LCARS v0.2). Written by the cloud session that built this branch, which cannot reach the VPS: no SSH, no Remote Control folder, and the board sits behind Cloudflare Access. Paste this file's text into that session, or point it at this file once the branch is merged.

Dave's request, quoted:

> 1) You recently deployed Kanzo to my dev VPS. It looks great with the LCARS theme, but now I want you to go through as much project history and sessions as you can find on the VPS and ensure that all Cards are established and correct. Moving forward, you'll be in charge of managing those, so determine what skills/hooks/etc you will need to do so effectively. Dig deep here and ensure this kanzo board is authoritative and effective.
> 2) I have used Google Stitch to generate a new theme / layout. Your LCARS is good, but I think I like the style of this one better. Previously I asked to have a theme selected installed. Ensure that sticks, and that the original default Kanzo theme is available, your original LCARS is available, and this new one from google stitch is available to be switched back and forth independently.

Follow your own guardrails; where a step needs Dave's word, say so rather than skip it.

## A. Push the fork to GitHub (small, first)

`/home/claude/dev/kanzo` has no remote. Dave created https://github.com/dwertz-at-blenko/kanzo (it holds upstream v0.3.0 plus this branch).

```bash
cd /home/claude/dev/kanzo
git remote add origin https://github.com/dwertz-at-blenko/kanzo
# through your bws-wrapped gh for auth; no force, do not touch main
git push -u origin public-url lcars-theme
```

Then record in a comment on the PR for `claude/eloquent-ptolemy-cl1te6` (or tell Dave): the heads pushed, Cargo.toml version, the non-secret `config.toml`, the columns on in Settings.

## B. Merge the themes and deploy

Branch `claude/eloquent-ptolemy-cl1te6` (based on upstream v0.3.1, same as `public-url`) adds the three-way theme engine, the Skeletal LCARS theme, the skills, hooks and audit tooling. Merge it into `public-url`:

```bash
cd /home/claude/dev/kanzo && git fetch origin
git checkout -b themes-merge public-url && git merge origin/claude/eloquent-ptolemy-cl1te6
```

Resolve as in `docs/themes/README.md`, "Merging the VPS fork's LCARS theme":

| File | Take |
| --- | --- |
| `src/lcars.css` | yours (v0.2); then set `LCARS_VERSION = "0.2"` in `src/web.rs` and make sure the first line of lcars.css reads `/* LCARS theme v0.2 */` and it has `--theme-version: "0.2"` |
| `src/fonts/antonio.woff2` | this branch's (variable weight, covers Bold) |
| `src/theme.js`, `src/index.html`, theme parts of `src/web.rs` and `src/app.js` | this branch's (the three-way switcher and the board-wide setting replace the v0.2 footer toggle and `kanzo.theme` in localStorage) |
| `url` setting (`src/home.rs` Config, `Guard` in `src/web.rs`), `deploy/`, `docs/lcars/` | yours |

Then:

```bash
export PATH="$HOME/.cargo/bin:$PATH"
cargo fmt --check && cargo clippy --all-targets --locked -- -D warnings && cargo test --locked
# a throwaway instance on 8112 with a copy of the DB, as you did for v0.2; check / for data-theme,
# /?theme=lcars shows LCARS v0.2 in the version line, /?theme=skeletal the Skeletal look, the switcher saves
git checkout public-url && git merge --ff-only themes-merge
cargo build --release && systemctl --user restart kanzo-web && systemctl --user is-active kanzo-web kanzo-tunnel
curl -s http://127.0.0.1:8111/ | grep -o 'data-theme="[a-z]*"\|kanzo-themes" content="[^"]*"'
curl -s -o /dev/null -w '%{http_code}\n' https://kanzo.blenko.cloud/
git push origin public-url
```

Set the default: Dave prefers the Stitch look, so `curl -s -X PATCH -H 'content-type: application/json' -d '{"theme":"skeletal"}' http://127.0.0.1:8111/api/settings`, and put `theme = "skeletal"` in `config.toml` so a fresh database starts there too. The pick is kept in the database from then on; every browser follows it.

Update `deploy/README.md` (theme row, version v4) and your memory file.

## C. Make the board authoritative

Everything is in `.claude/skills/kanzo-board/` (routine, conventions, `references/audit-playbook.md`) and `tools/board-audit/`. The short version:

1. Back up: `cp ~/.local/share/kanzo/kanzo.db ~/.local/share/kanzo/backups/kanzo-$(date +%F)-pre-audit.db`; dump `/api/board` to `docs/audit/`.
2. Collect: `python3 tools/board-audit/collect.py --api http://127.0.0.1:8111/api --memory ~/.claude/memory --sessions ~/.claude/projects --repos ~/dev --since 2026-08-01 --out docs/audit/inventory-$(date +%F)`. Add what it cannot see: `systemctl --user list-units`, the cf-site inventory in the blenko-cloud-publish skill, `vps-services.md`, open PRs and issues through `bws run ... gh`.
3. Decide, per project, following the conventions: done for shipped and verified work (one card per deliverable, closing comment with commit or URL), doing for what a session or branch touched in the last 7 days, todo for agreed next steps ("Your move" lists, pending tasks in summaries), backlog for ideas, human for what only Dave can do. Tag everything you create or change `audit`. Never delete; merge duplicates. Keep the 83 seeded projects; add a project only for real work with no repo ("VPS ops", "Home network", "Shop cameras").
4. Apply: `apply.py --dry-run`, read it, then `apply.py --label "Board audit $(date +%F)"`.
5. Report: `docs/audit/board-audit-$(date +%F).md`, with the open questions for Dave also on the board as human cards tagged `audit`. Commit the inventory, the plan and the report on `public-url`, push.

## D. Keep it that way

Install the hooks in user scope on the VPS (and declare them in `claude-machine-setup`'s fleet config, as you did for the MCP server): `agent/hooks/README.md`. Copy or symlink `.claude/skills/kanzo-board` and `.claude/skills/skeletal-lcars` into `~/.claude/skills/` so they load in every repo. The Mac and the PC cannot reach the board unattended through Access; a `kanzo join` over the VPN, or a synced board, is the way there, and is a decision for Dave.

## E. Report back

A comment on the PR, or a line to Dave: what was pushed, the theme now live and its version line, counts per status before and after the audit, the open questions.
