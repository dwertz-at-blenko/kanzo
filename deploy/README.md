# kanzo.blenko.cloud deploy

Kanzo (upstream github.com/emanuele-em/kanzo) on the dev VPS, restricted to Dave by Cloudflare Access.

| Piece | Value |
|---|---|
| URL | https://kanzo.blenko.cloud (Access: email dwertz@blenko.com only, 24h session) |
| Origin | 127.0.0.1:8111, user unit `kanzo-web.service` |
| Tunnel | `kanzo`, user unit `kanzo-tunnel.service`, token `~/.cloudflared/kanzo.json` (0600) |
| Binary | `~/dev/kanzo/target/release/kanzo`, built from the pushed tip of branch `public-url` by `deploy/deploy.sh` only (upstream 0.3.1 + the `url` setting); `kanzo --version` and `/api/version` name its commit |
| Data | `~/.local/share/kanzo` (`kanzo.db`, `config.toml` with `url = "https://kanzo.blenko.cloud"` and `theme = "skeletal"`) |
| Theme | Three looks, switched independently: Kanzo (stock), LCARS v0.2, Skeletal LCARS v0.1 (the Google Stitch design, the default). The pick is board-wide, kept in the database, set from the switcher at the bottom of the sidebar or Settings; `theme = "skeletal"` in `config.toml` only seeds a fresh database; `/?theme=<name>` previews one visit (`docs/themes/README.md`, `docs/lcars/README.md`) |
| Projects | one per active GitHub repo, seeded by `deploy/seed-projects.sh` (blenko-glass-company blue, dwertz-at-blenko purple; re-run any time, idempotent) |
| MCP | `https://kanzo.blenko.cloud/mcp` (behind Access); on the VPS `kanzo mcp --home ~/.local/share/kanzo` |

Install: `ln -sf ~/dev/kanzo/deploy/kanzo-web.service ~/dev/kanzo/deploy/kanzo-tunnel.service ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now kanzo-web kanzo-tunnel`

Update: merge into `public-url` (from upstream: `git fetch upstream && git merge upstream/main`), push, then deploy as below. The sidebar's one-click update is disabled in the unit (`KANZO_RELEASES_URL`) because it would install upstream's build, which 403s behind the tunnel until the `url` patch is merged upstream.

## Deploy

`deploy/deploy.sh` is the only way to ship; `--check` runs steps 1 to 3 and changes nothing. Never copy a binary into `target/release/` from a worktree, and never build `~/dev/kanzo` by hand: on 2026-11-17 a rebuild replaced a binary built from an unmerged branch, and the stars and the log's signing left the board for 30 hours.

1. Refuses unless `~/dev/kanzo` is on `public-url`, its tracked files are clean, and HEAD is `origin/public-url`.
2. Reads the running board's commit from `/api/version` (a board older than that: the last line of `~/.local/share/kanzo/deployed`) and refuses when the board has commits HEAD lacks, or was built outside the script (`unknown`, `-dirty`, a commit the repo lacks). First run, with neither: confirm what the board was built from, then `KANZO_DEPLOY_LIVE_COMMIT=<sha> deploy/deploy.sh`.
3. Refuses when `deploy/features.txt` lost a feature the running board's list has. A feature that ships adds its line in the same change; `--drop-feature "<name>"` is the one way to take one off.
4. `cargo fmt --check`, clippy and `cargo test`; a copy of the running binary in `~/.local/share/kanzo/rollback/` (newest 3 kept) and of the database in `~/.local/share/kanzo/backups/` (row counts compared); the release build, which must name HEAD; the restart; then every `features.txt` line, the listeners (loopback only) and the edge (302 to Access). Any failure from the build on rolls back to the copied binary and says what it restored.
5. Logs the deploy to `~/.local/share/kanzo/deployed`, and lists the agents' `kanzo mcp` processes still on the old binary: they keep old code until their session restarts, and leave their activity rows unsigned, which `tools/activity-sign/sign.py` signs.

`deploy/test-deploy.sh` proves the refusals on a throwaway repo, away from the live board.

## kanzo-dispatch

Dave comments on a card (unless he ticks "Do not send to agent") and a background Claude session picks it up; how it works is in `agent/dispatch/README.md`. It is a Python script run from this checkout, so it ships with a pull of `public-url`, not with `deploy.sh`, and it adds no HTTP route (no `features.txt` line).

| Piece | Value |
|---|---|
| Unit | `kanzo-dispatch.service` (user), `KillMode=process` so a restart never takes the claude daemon and its sessions down |
| Reads | `http://127.0.0.1:8111` API; `kanzo.db` read-only once, on the first run |
| Writes | the board's `/mcp` as `kanzo-dispatch`; state in `~/.local/share/kanzo/dispatch/state.json` |
| Secrets | none: no vault token, `claude` uses this user's login |

1. Dry run first: `agent/dispatch/kanzo_dispatch.py --once --dry-run` (writes nothing, prints what it would do).
2. Install: `ln -sf ~/dev/kanzo/deploy/kanzo-dispatch.service ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now kanzo-dispatch`
3. Log: `journalctl --user -u kanzo-dispatch -f`. After two start failures in a row it stops starting sessions until `systemctl --user restart kanzo-dispatch`.

---

> **Version:** v6 · created 2026-11-05 18:55 EST · updated 2026-11-21 13:26 EST
> **Changes:** v1 initial. v2 2026-11-07 LCARS theme on, seeded projects. v3 2026-11-07 theme toggle. v4 2026-11-12 three-way theme engine, Skeletal default, fork pushed to github.com/dwertz-at-blenko/kanzo (session 421baa4d). v5 2026-11-21 deploy only through deploy/deploy.sh: commit stamp, features.txt, rollback (session b1475961-06d7-40b0-9eff-f2b47896b6bb). v6 2026-11-21 kanzo-dispatch section (same session).
> **This file:** /home/claude/dev/kanzo/deploy/README.md
> **Session:** 08cf0c04-265c-4b64-8e18-d5679b1a650d · vps as claude · cwd /home/claude/dev
> **Resume:** ssh -t blenko-claude 'cd /home/claude/dev && claude --resume 08cf0c04-265c-4b64-8e18-d5679b1a650d'   (from the Mac)
