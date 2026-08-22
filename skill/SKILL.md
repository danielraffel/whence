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

If the work has a committed goal or planning document, include its durable URL:

```bash
python3 /path/to/whence --pr <number> \
  --goal https://github.com/owner/planning/blob/main/path/to/goal.md --apply
```

Repeat `--goal` for multiple documents. Prefer a committed default-branch URL;
never publish a local filesystem path.

It reads the environment (cmux + agent session vars) and the host label file, so
there is nothing to pass — just run it in the same shell/session that opened the
PR. It is idempotent; re-running replaces the prior stamp.

When a durable workstream or router is in use, preserve these launcher-provided
environment variables through the PR command: `WHENCE_WORKSTREAM_ID`,
`WHENCE_LAUNCHER`, `WHENCE_ROUTE`, and `WHENCE_ROUTER`. Never put an account
identity, credential, URL, private path, or transcript in those fields.

For Shipyard, pass the same durable identity with
`shipyard pr --workstream-id <id>`. Whence snapshots that literal flag before a
detached worker can outlive the shell. Stable launcher/route defaults and exact
repository overrides may live under `provenance` in the fleet-synced Whence
config; explicit launcher environment always wins. Missing or malformed values
remain unresolved rather than being inferred.

## Notes

- Works for Claude Code and Codex — the resume command is sourced from cmux's
  own per-tab restore handle, so no agent-specific configuration is needed.
- If the repo authenticates with a GitHub App token, set
  `WHENCE_GH=ghapp` (or the appropriate CLI) before running.
- Preview first with no `--apply` to see the labels and footer it would add.
