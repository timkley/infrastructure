# Agent Notes

## Mistakes

- When aligning Docker Compose naming in this repository, inspect both `template.yml` and the rendered Compose output before changing names. Service keys follow generic names such as `app`, `db`, and `broker`; visible prefixed container names should use hyphens, for example `immich-app`, not underscore names such as `immich_app`.

## Instruction Files

- Durable agent instructions belong in `AGENTS.md`.
- `CLAUDE.md` should be a symlink to `AGENTS.md`, without unique rules.
