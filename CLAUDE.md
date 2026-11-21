# Kanzo, Dave's fork

Upstream is [emanuele-em/kanzo](https://github.com/emanuele-em/kanzo); this fork adds themes, the board-management skill and the audit tooling. The live board is https://kanzo.blenko.cloud, served from the VPS by `/home/claude/dev/kanzo` (branch `public-url`, which also carries the `url` setting and the LCARS theme; see `docs/themes/README.md` for merging it here).

## Before a change

- Read `.github/CONTRIBUTING.md`: light, few tokens for agents, plain web, small commits.
- Run what CI runs: `cargo fmt --check`, `cargo clippy --all-targets --locked -- -D warnings`, `cargo test --locked`.
- A board change: try it in a browser in all three themes (`/?theme=kanzo|lcars|skeletal`), light and dark for the stock one, and at 390px.

## Themes

`src/index.html` plus `src/skeletal.css` and `src/lcars.css`, each scoped under `html[data-theme=...]`; the pick is a board setting. How it fits together, and how to add a theme: `docs/themes/README.md`. The design system for the Skeletal look: the `skeletal-lcars` skill in `.claude/skills/`.

## The board

Agents working in this or any repo of Dave's keep the board honest: `.claude/skills/kanzo-board/SKILL.md` (routine and conventions), `agent/hooks/` (SessionStart and Stop hooks), `tools/board-audit/` (collect and apply).

## Deploying to the VPS

Only with `deploy/deploy.sh`, on the VPS, from a pushed `public-url` (`--check` first; `deploy/README.md` says what it checks). Never copy a binary into `target/release/` from a worktree, and never build the live checkout by hand. A feature that ships adds its line to `deploy/features.txt` in the same change; taking one off the board takes `--drop-feature`. Why: on 2026-11-17 a rebuild replaced a binary built from an unmerged branch, and the stars and the log's signing left the board for 30 hours. `docs/ops/` has the handoffs.
