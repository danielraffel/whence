# Whence long-running PR capture handoff

Updated: `2026-07-17T19:20:36Z`

## Objective and completion gate

Original objective, in the user's framing: investigate why Whence did not catch Pulp PR #6195 and add labels, fix that root cause, then determine why PRs #6222, #6224, #6225, and #6226 were also delayed.

The #6195 race is fixed and deployed. Remaining completion gate: PRs created early by long-running or backgrounded `shipyard pr` / `pulp pr` commands must receive exact Whence provenance promptly, without waiting for the command or its supervised build to exit. The solution must preserve privacy flags, exact repo/branch/commit identity, existing direct-URL behavior, and the periodic sweep fallback; pass `python3 test_whence.py`, `python3 -m py_compile whence`, `git diff --check`, and adversarial review; merge to Whence `main`; deploy and verify the same commit on `m1`, `m3`, and `m5`.

## Repository boundaries and state

| Role | Absolute path on m3 | Branch | HEAD | Status | May edit? |
|---|---|---|---|---|---|
| Installed Whence | `/Users/danielraffel/.local/share/whence` | `main` | `77d7b6c9047a7e7708db04ae712a8eadbac64c45` | clean, equals `origin/main` | Do not develop here; keep it deployable |
| Handoff checkout | `/Volumes/Workshop/Code/whence-handoff-long-running-pr` | `handoff/long-running-pr-capture` | handoff commit recorded below | guide only | Reference only; create a new implementation worktree |
| Pulp evidence checkout | `/Volumes/Workshop/Code/pulp` | `feature/multi-plugin-bundle-cmake` | `f488609ef6156ea007d3460e35a3f548e7c8cd32` | dirty with unrelated edits and `UU core/render/src/gpu_compute.cpp` | **No touch** |
| Pulp remote baseline observed | `origin/main` | n/a | `3130dc97ff3de77c1ba6515f51633eb0f79a2542` | remote ref at capture time | Read-only evidence |

Pulp dirt also included modified docs/tools/planning plus untracked `.agents/skills/handoff/`, `.codex/`, `.trace-shots/`, and npm files. It belongs to other sessions and must not be repaired, stashed, committed, or cleaned by this work.

Whence remote: `https://github.com/danielraffel/whence`. Installed copies on `m1`, `m3`, and `m5` were verified at `77d7b6c9047a7e7708db04ae712a8eadbac64c45`.

State snapshot: `/private/tmp/whence-long-running-pr-handoff-state.json` (local to m3; SHA-256 reported in the closing handoff message).

## Completed and verified

- Root cause for #6195: hook ledgered the pushed branch 29 seconds before GitHub created the PR; the immediate sweep was too early, so the ten-minute timer labeled it 6m28s later.
- Fix merged as Whence PR #15, commit `77d7b6c9047a7e7708db04ae712a8eadbac64c45`: detached bounded retry, exact pushed commit identity, creation-window/fork/reused-branch protection, timeout cap, correct pushed source/cwd capture, and privacy propagation.
- The fix was deployed to `m1`, `m3`, and `m5`; full tests passed in the installed checkout.
- Follow-up live evidence:
  - Pulp #6222 created `17:46:30Z`, labeled `18:23:51Z`-`18:23:52Z` after its background `shipyard pr` returned.
  - Pulp #6224 created `17:49:37Z` on `m5`; its `shipyard pr --json` ran for about 1h25m and labels appeared at `19:14:10Z` / `19:14:18Z` only after return (`1·codex`, `2·m5`, `3·W1`).
  - Pulp #6225 created `17:52:58Z`, labeled `18:44:22Z` after later capture.
  - Pulp #6226 was ledgered by a standalone push and swept at `18:06:01Z`.
- The user's suspected m3 surface `9ECD1F0F-9D54-48EA-B232-5492721C5A16` was not the source. #6222/#6225/#6226 traced to m3 surface `39441E58-1F27-449C-BA8D-0399F9726E49`, Claude session `0289508d-5f2d-40d4-9f8c-96a18227ca7e`. #6224 traced to m5 Codex surface `24ACD3FF-978C-44C4-97CD-C18F49419261`.

## Open gates

1. **Capture before a long-running PR command starts** — current shell wrappers call the real command first and stamp only after it exits (`whence` around `_hook_file_text`, currently lines 529-556).
   - Implement a pre-execution capture for `shipyard pr` and `pulp pr` that records repo, branch, exact HEAD, and live provenance, then starts the existing bounded targeted retry while the real command continues.
   - Pass signal: a controlled long-running fake PR command creates a matching PR after launch and receives the stamp before the fake command exits.
2. **Make compound-command fallback command-local** — `_cmd_cwd()` currently deliberately takes the last `cd`. Commands for #6222/#6225 changed into a worktree, ran `shipyard pr`, then changed back to `/Volumes/Workshop/Code/pulp`; a background PostToolUse payload contained only `Command running in background`, so outcome parsing had no repo/ref and fallback could select the wrong checkout.
   - Continue: add tests with multiple `cd` segments and associate the PR/push verb with its preceding effective cwd rather than globally taking the last cwd.
   - Pass signal: ledger key and captured HEAD identify the shipped worktree branch, not the final diagnostic cwd.
3. **Avoid diagnostic false positives** — regexes currently treat any Bash command text containing `shipyard pr`, `pulp pr`, or `git push` as an action. Investigation commands that grep or print those literals can overwrite unrelated ledger provenance.
   - Pass signal: quoted search/diagnostic literals do not trigger capture, while actual invocations still do.
4. **Closeout** — run tests and autoreview, open/merge a Whence PR, fast-forward the installed checkout, then `./whence --deploy` and verify identical HEADs on all three hosts.

## Build, launch, and test

```sh
git clone https://github.com/danielraffel/whence /tmp/whence-continuation
git -C /tmp/whence-continuation fetch origin handoff/long-running-pr-capture
git -C /tmp/whence-continuation show origin/handoff/long-running-pr-capture:docs/handoffs/2026-07-17-long-running-pr-capture.md
git -C /tmp/whence-continuation worktree add -b fix/pre-exec-pr-capture /tmp/whence-preexec origin/main
cd /tmp/whence-preexec
python3 test_whence.py
python3 -m py_compile whence
git diff --check
```

- App: none.
- Visual/interaction evidence: none required.
- Latest tests on clean `77d7b6c`: `python3 test_whence.py && python3 -m py_compile whence && git diff --check` — all pass.

## Decisions and hazards

- Do not broaden the existing retry by branch name alone. Commit SHA plus creation window is the identity boundary that prevents old PR, reused-branch, and fork collisions.
- Preserve effective `--hide`, `--no-labels`, and `--no-footer` settings in any detached worker.
- Do not stop or clean unrelated Pulp/Shipyard jobs. At handoff time other sessions had active `shipyard pr` processes on m5; they are not owned by this session.
- Do not use the dirty Pulp root as an implementation checkout.
- GitHub read-only/PR operations should prefer `ghapp` when available.

## First safe continuation step

```sh
git clone https://github.com/danielraffel/whence /tmp/whence-continuation && git -C /tmp/whence-continuation fetch origin handoff/long-running-pr-capture && git -C /tmp/whence-continuation show origin/handoff/long-running-pr-capture:docs/handoffs/2026-07-17-long-running-pr-capture.md
```

## Quick retire-safety confirmation

- This handoff session started no persistent build, watch, CI-monitor, or deployment process.
- All of this session's commands are terminal before close.
- The #6195 fix is merged and deployed; #6222/#6224/#6225/#6226 now have labels.
- The remaining pre-execution-capture fix is explicitly transferred by this guide and its remote handoff branch.
- Other sessions' dirty Pulp files and m5 Shipyard jobs are identified as external, preserved, and must not be stopped.
- Once the handoff branch and state snapshot verify, this session can close with zero loss.
