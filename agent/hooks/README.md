# Hooks that keep the board honest

Three Claude Code hooks, to install in user scope on every machine whose agents work with the board (the VPS, the Mac, the PC). They read the board over its HTTP API and never write to it; the agent does the writing, through MCP.

| Hook | Event | Does |
| --- | --- | --- |
| `kanzo-session-start.sh` (with `kanzo-session-start.py`) | `SessionStart` | finds the project for the repo's `origin` remote and prints its open cards into the session's context, with the one-line routine |
| `kanzo-stop.sh` | `Stop` | if `$(git rev-parse --git-dir)/kanzo-task` names a card, blocks the stop once and asks the agent to comment, move and clear it |
| `kanzo-note.py` | `PostToolUse` | each time the session writes to the board through MCP, shows the user one line with Kanzo's own reply, such as `Kanzo: Updated kanzo#251, review -> done: Merge the stars branch` |

## Install

Copy the four files together somewhere stable (the fleet config on the VPS keeps them in `~/.local/share/claude-machine-setup/hooks/`; on a laptop `~/.claude/hooks/` is fine), then add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      { "matcher": "", "hooks": [{ "type": "command", "command": "~/.claude/hooks/kanzo-session-start.sh", "timeout": 10 }] }
    ],
    "Stop": [
      { "matcher": "", "hooks": [{ "type": "command", "command": "~/.claude/hooks/kanzo-stop.sh", "timeout": 5 }] }
    ],
    "PostToolUse": [
      { "matcher": "mcp__kanzo__.*", "hooks": [{ "type": "command", "command": "~/.claude/hooks/kanzo-note.py", "timeout": 5 }] }
    ]
  },
  "env": { "KANZO_API": "http://127.0.0.1:8111/api" }
}
```

`KANZO_API` is where the board answers from that machine: on the VPS `http://127.0.0.1:8111/api`; on a machine that has joined the VPS board with `kanzo join`, its own `http://127.0.0.1:4242/api`, which forwards; leave it out for a local board on 4242. Behind Cloudflare Access there is no unattended path from a laptop, so a laptop either joins over the VPN or runs its own synced board.

The skills (`.claude/skills/kanzo-board`, `.claude/skills/skeletal-lcars`) load from this repo when working in it; to have them everywhere, copy or symlink them into `~/.claude/skills/`.

## Try them

```bash
cd ~/dev/some-repo && KANZO_API=http://127.0.0.1:8111/api ~/.claude/hooks/kanzo-session-start.sh
echo kanzo#12 > "$(git rev-parse --git-dir)/kanzo-task" && echo '{}' | ~/.claude/hooks/kanzo-stop.sh; rm "$(git rev-parse --git-dir)/kanzo-task"
```
