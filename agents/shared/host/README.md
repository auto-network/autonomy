## Host Agent CLAUDE.md — Setup

This directory contains the global CLAUDE.md brief for Claude running natively on the host (not in a container). It composes with the project-level `CLAUDE.md` at the repo root via Claude Code's layered CLAUDE.md system. To activate it, symlink it into `~/.claude/` once from the repo root:

```bash
ln -sf "$(pwd)/agents/shared/host/CLAUDE.md" ~/.claude/CLAUDE.md
```

After that, every host Claude session picks up the brief automatically via Claude Code's global CLAUDE.md layer.
