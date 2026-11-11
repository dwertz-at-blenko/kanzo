# Board audit tooling

Two standard-library Python scripts behind the audit playbook (`.claude/skills/kanzo-board/references/audit-playbook.md`).

| Script | Does | Writes to the board |
| --- | --- | --- |
| `collect.py` | reads the board, memory files, Claude Code session transcripts and git repos into `inventory.md` and `inventory.json`, grouped by project | no |
| `apply.py` | applies a reviewed `plan.json` of creates, moves, comments and merges, idempotently, each with a provenance comment | yes |

```bash
python3 tools/board-audit/collect.py --api http://127.0.0.1:8111/api --memory ~/.claude/memory \
    --sessions ~/.claude/projects --repos ~/dev --since 2026-08-01 --out docs/audit/inventory-2026-11-11
python3 tools/board-audit/apply.py --api http://127.0.0.1:8111/api --plan docs/audit/plan-2026-11-11.json --dry-run
python3 tools/board-audit/apply.py --api http://127.0.0.1:8111/api --plan docs/audit/plan-2026-11-11.json --label "Board audit 2026-11-11"
```

The plan is written by whoever reads the inventory (an agent, following the playbook), not by a script: deciding what is done, in flight, agreed or parked is judgement. Running `apply.py` twice changes nothing the second time. Nothing is ever deleted.
