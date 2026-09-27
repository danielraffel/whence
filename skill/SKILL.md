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

For a non-cmux launcher, preserve its already-known recovery facts with
`WHENCE_TERMINAL_RUNTIME=herdr`, `WHENCE_TERMINAL_ADDRESS`,
`WHENCE_TERMINAL_INSTANCE`,
`WHENCE_TERMINAL_TAB`, `WHENCE_SESSION_ID`, `WHENCE_RESUME_COMMAND`, and
`WHENCE_RELAUNCH_COMMAND`. `WHENCE_TERMINAL_WORKSPACE` is optional. Resume and
relaunch values must be short argv-shaped commands with no shell syntax, paths,
endpoints, assignments, or credential flags. Terminal runtime (`cmux` or
`herdr`) is distinct from provider route (`direct` or `subrouter`). Whence only
records provenance; it does not authorize a route rebind or owner transfer.
Treat pane/surface refs as reusable addresses, never instance identity. Supply
`WHENCE_TERMINAL_INSTANCE` only when the terminal adapter proves a non-reusable
incarnation; otherwise omit it.

For Shipyard, pass the same durable identity with
`shipyard pr --workstream-id <id>`. Whence snapshots that literal flag before a
detached worker can outlive the shell. Stable launcher/route defaults and exact
repository overrides may live under `provenance` in the fleet-synced Whence
config; explicit launcher environment always wins. Otherwise whence derives
them from the process ancestry (a subrouter ancestor means route `subrouter`,
cmux's launch argv means launcher `cmux`) and falls back to a named value such
as `codex-cli` or `shell`. Malformed explicit values remain unresolved.

## Notes

- Works for any cmux agent via cmux's per-tab restore handle. Other launchers can
  supply the generic native-session and recovery variables above.
- If the repo authenticates with a GitHub App token, set
  `WHENCE_GH=ghapp` (or the appropriate CLI) before running.
- Preview first with no `--apply` to see the labels and footer it would add.
- A session launched with its own `CLAUDE_CONFIG_DIR` or `CODEX_HOME` gets the
  agent hook wired automatically the first time whence runs inside it (only
  after `--install-agent-hook` was run once on the machine). It prints one line,
  never rewrites a malformed or symlinked file, and respects
  `WHENCE_AUTOINSTALL=0`, `"agent_hook_autoinstall": false`, and a hook you
  removed by hand. `whence --self-heal <dir>` shows its verdict as JSON.
