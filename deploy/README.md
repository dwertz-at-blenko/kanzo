# kanzo.blenko.cloud deploy

Kanzo (upstream github.com/emanuele-em/kanzo) on the dev VPS, restricted to Dave by Cloudflare Access.

| Piece | Value |
|---|---|
| URL | https://kanzo.blenko.cloud (Access: email dwertz@blenko.com only, 24h session) |
| Origin | 127.0.0.1:8111, user unit `kanzo-web.service` |
| Tunnel | `kanzo`, user unit `kanzo-tunnel.service`, token `~/.cloudflared/kanzo.json` (0600) |
| Binary | `~/dev/kanzo/target/release/kanzo`, built from branch `public-url` (upstream 0.3.1 + the `url` setting) |
| Data | `~/.local/share/kanzo` (`kanzo.db`, `config.toml` with `url = "https://kanzo.blenko.cloud"` and `theme = "lcars"`) |
| Theme | LCARS theme (branch `lcars-theme`, merged; `docs/lcars/README.md`); `/?theme=kanzo` shows the stock look for one visit |
| Projects | one per active GitHub repo, seeded by `deploy/seed-projects.sh` (blenko-glass-company blue, dwertz-at-blenko purple; re-run any time, idempotent) |
| MCP | `https://kanzo.blenko.cloud/mcp` (behind Access); on the VPS `kanzo mcp --home ~/.local/share/kanzo` |

Install: `ln -sf ~/dev/kanzo/deploy/kanzo-web.service ~/dev/kanzo/deploy/kanzo-tunnel.service ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now kanzo-web kanzo-tunnel`

Update: `git fetch upstream && git merge upstream/main`, `cargo build --release`, `systemctl --user restart kanzo-web`. The sidebar's one-click update is disabled in the unit (`KANZO_RELEASES_URL`) because it would install upstream's build, which 403s behind the tunnel until the `url` patch is merged upstream.

---

> **Version:** v2 · created 2026-11-05 18:55 EST · updated 2026-11-07 12:07 EST
> **Changes:** v1 initial. v2 2026-11-07 LCARS theme on, seeded projects.
> **This file:** /home/claude/dev/kanzo/deploy/README.md
> **Session:** 08cf0c04-265c-4b64-8e18-d5679b1a650d · vps as claude · cwd /home/claude/dev
> **Resume:** ssh -t blenko-claude 'cd /home/claude/dev && claude --resume 08cf0c04-265c-4b64-8e18-d5679b1a650d'   (from the Mac)
