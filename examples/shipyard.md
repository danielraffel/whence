# Stamp from a merge orchestrator (Shipyard-style)

If a single tool opens every PR for every agent, stamp there once and every PR
gets provenance for free — no per-agent hook.

The stamp needs the *agent's* environment (cmux vars, session ids), so it must
run in the process the agent launched, not a detached daemon. Two options:

1. **Post-open shell step.** Whatever wrapper the agent calls to open a PR runs
   the stamper immediately after, in the same shell:
   ```bash
   your-pr-command "$@" && python3 /path/to/whence --apply
   ```

2. **Native integration.** If you own the orchestrator, port the collection +
   render from `whence` into its PR-open path, gated behind a config
   flag and degrading gracefully when cmux/env are absent. Use the same GitHub
   client it already has to add labels and edit the body.

Either way: read the env at PR-open, emit the `agent` / `host` / `tab` labels and
the provenance footer, and make it idempotent (replace a prior stamp rather than
appending).
