---
name: whence
description: >
  After opening a pull request, stamp it with provenance so it can be traced
  back to this agent/session/machine/terminal-tab. Run this whenever you open a
  PR (gh pr create, a merge orchestrator, etc.) so the human can later find the
  exact session that produced the PR and resume it.
requires:
  tools:
    - gh
  files:
    - whence
---

# whence

## When to use

Right after you open a pull request. The stamp attaches the originating agent,
machine, and cmux tab as labels, plus a footer with the session's resume command
and restore URL.

## How

```bash
# stamp the PR you just opened for the current branch:
python3 /path/to/whence --apply

# or a specific PR:
python3 /path/to/whence --pr <number> --apply
```

It reads the environment (cmux + agent session vars) and the host label file, so
there is nothing to pass — just run it in the same shell/session that opened the
PR. It is idempotent; re-running replaces the prior stamp.

## Notes

- Works for Claude Code and Codex — the resume command is sourced from cmux's
  own per-tab restore handle, so no agent-specific configuration is needed.
- If the repo authenticates with a GitHub App token, set
  `WHENCE_GH=ghapp` (or the appropriate CLI) before running.
- Preview first with no `--apply` to see the labels and footer it would add.
