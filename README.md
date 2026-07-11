# whence

**Trace every pull request back to the agent, machine, and terminal tab that made it — so you can jump straight back to the session that's still running.**

If you drive a lot of AI coding agents (Claude Code, Codex) across several
machines and a wall of [cmux](https://cmux.com) tabs, your PR queue turns into a
black box: every PR is opened by the same bot, and there's no way to tell *which
agent, on which machine, in which tab* pushed it. When something needs a
follow-up you have no idea which of your twelve open sessions to go ask.

`whence` fixes that. On every PR it stamps:

- **Color-coded queue labels** — the agent (any cmux agent — `claude`, `codex`,
  `opencode`, `gemini`, `cursor`, …), the machine (any name you choose), and the
  cmux **tab name** — so the queue is legible at a glance.
- **A visible "🔎 Provenance" footer** in the PR body with the **session id**,
  the exact **resume command** (`claude --resume …` / `codex resume …`), the
  restorable **`claude.ai/code` URL**, and a **jump-to-tab** command
  (`cmux surface focus …`).

![What whence adds: color-coded queue labels and a provenance footer that links back to the session](docs/hero.png)

## Why it exists

Multi-agent, multi-machine development is fast but disorienting. You kick off
work in one tab, switch to another, come back an hour later to a queue of PRs and
no memory of which was which. The one thing you always want is **"take me back to
the session that did this."** That requires four facts, captured at push time,
attached to the PR:

| Fact | Where it comes from |
|------|---------------------|
| Which agent | cmux `CMUX_AGENT_LAUNCH_KIND` (or `CLAUDECODE` / `CODEX_*`) |
| Which machine | a one-token `~/.config/whence/host-label` you set per machine |
| Which tab | `cmux workspace list` → the tab's human name |
| How to resume it | `cmux surface resume get` → the exact relaunch command, for **any** agent |

The clever bit: cmux already stores the exact restore command per tab, so Codex
and Claude are handled by the *same* code path — no agent-specific guessing.

## Setup & scope — install once, opt in per repo

`whence` is a single script, not a background service. **Nothing runs until you
run it** (or wire a hook). Setup is **once per machine**:

```bash
git clone https://github.com/danielraffel/whence     # put `whence` on your PATH
echo m3 > ~/.config/whence/host-label                 # name this machine (any string)
# also needs: python3, gh (authenticated), and cmux for the tab/session fields
```

After that it works in **every repo you run it in** — there is no per-repo
install. GitHub labels are per-repo, so the first time it stamps in a given repo
it auto-creates the `agent` / `machine` / `tab` labels there; you never make
labels by hand.

**Will it stamp all my repos, or just one?** Entirely up to how you wire the
auto-stamp — and you can scope it either way:

| You want… | Do this |
|-----------|---------|
| Stamp by hand, any repo | run `whence --apply` when you want |
| Auto-stamp **every** repo | a global shell alias or Claude Code hook (`~/.claude/settings.json`) running `whence --auto` |
| Auto-stamp **one** repo | a project hook in that repo's `.claude/settings.json` |
| Global hook, but **skip** some repos | `"repos": {"mode":"deny","list":["owner/secret"]}` in config |
| Global hook, but **only** certain repos | `"repos": {"mode":"allow","list":["owner/a","owner/b"]}` |

`whence --auto` is the hook-safe mode: it applies quietly and **exits without
touching anything** if the repo is excluded or the branch has no PR yet — so one
global hook is safe to leave on everywhere and gate per-repo from config.

## Use

```bash
# preview what would be stamped (no changes):
whence --pr 1234

# stamp it:
whence --pr 1234 --apply

# or let it find the PR for the current branch:
whence --apply
```

Re-running is idempotent — it replaces its own labels/footer, never piling up.

### Auto-stamp on every PR

Pick whichever fits your flow — the tool is just a script, so it drops into any
hook that fires around PR creation:

- **Shell wrapper** — alias it over your PR command so it always runs after:
  ```bash
  gpr() { gh pr create "$@" && python3 /path/to/whence --apply; }
  ```
- **Claude Code hook** — see [`examples/claude-code-hook.md`](examples/claude-code-hook.md).
- **Merge orchestrators** (e.g. Shipyard) — call it from the PR-open step so
  every agent's PR is stamped centrally. See [`examples/shipyard.md`](examples/shipyard.md).
- **As an agent skill** — [`skill/SKILL.md`](skill/SKILL.md) tells an agent to
  stamp its PR right after opening it.

## Configure what gets posted

Everything is optional and lives in `~/.config/whence/config.json` — no code edits:

```json
{
  "hide":   ["url", "stamped"],
  "colors": { "agent": "1f6feb", "host": "1a7f37", "tab": "8250df" }
}
```

- **Hide fields** — omit any of `agent, host, tab, session, resume, url, jump,
  relaunch, stamped`. A hidden field drops from both the labels and the footer.
  Quick per-run: `WHENCE_HIDE=url,session whence --apply`, or `--hide url,session`.
- **Colors** — any GitHub label hex, per category.
- **Machine label** — `WHENCE_HOST_LABEL` env wins over the `host-label` file.
- **`gh` binary** — `WHENCE_GH=ghapp` (or any `gh`-compatible CLI) for App-token auth.

## Any agent, any machine

The agent label is whatever cmux reports (`CMUX_AGENT_LAUNCH_KIND`), so it works
for every agent cmux launches — **claude, codex, grok, opencode, pi, omp, amp,
cursor, gemini, kiro, antigravity, rovodev, hermes-agent, copilot, codebuddy,
factory, qoder** — with no per-agent setup. The **resume** line is pretty-printed
for agents whose CLI syntax is known (claude, codex; easy to add more), and the
**relaunch** line (`cmux surface resume get`) is cmux's own restore command, which
is correct for *any* agent. Machine names are free-form — whatever token you drop
in `host-label`.

**No cmux?** You still get the `agent` + `machine` labels and whatever session
handle the agent exposes; cmux just adds the tab name and the universal resume.

## What it doesn't do

There's no clickable `cmux://` deep-link — cmux only registers its URL scheme
for auth — so "jump to tab" is a copy-paste `cmux surface focus <id>` command.
If cmux ships a workspace-open URL, this will use it.

## License

MIT © Daniel Raffel
