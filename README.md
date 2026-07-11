# whence

*Whence came this pull request?*

**Trace every PR back to the agent, machine, and terminal tab that made it — so you can jump straight back to the session that's still running.**

If you drive a lot of AI coding agents (Claude Code, Codex) across several
machines and a wall of [cmux](https://cmux.com) tabs, your PR queue turns into a
black box: every PR is opened by the same bot, and there's no way to tell *which
agent, on which machine, in which tab* pushed it. When something needs a
follow-up you have no idea which of your twelve open sessions to go ask.

`whence` fixes that. On every PR it stamps:

- **Color-coded queue labels** — the agent (any cmux agent — `claude`, `codex`,
  `opencode`, `gemini`, `cursor`, …), the machine (any name you choose), the cmux
  **workspace** name, and the **tab** title — so the queue is legible at a glance.
  (Two tabs sharing a title get the cmux `surface:N` ref appended so you can still
  tell them apart.)
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
whence --setup --host m3                               # name this machine + install the every-PR hook
# also needs: python3, a GitHub CLI (gh or ghapp), and cmux for the tab/session fields
```

`whence --setup` is the guided one-shot: it names the machine, writes the config
(auto-selecting `gh` or `ghapp` based on what's installed), installs the
every-future-PR hook, and prints exactly **which PR commands it will catch** on
this machine. Prefer to do it in pieces? `whence --init` (config),
`echo m3 > ~/.config/whence/host-label` (name), `whence --install-hook` (hook)
are the same steps by hand.

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
| Turn it off in **one repo**, instantly | drop an empty `.whence-off` file in that repo's root (`touch .whence-off`) — gitignore it to keep it private, or commit it to disable for everyone |
| Global hook, but **skip** some repos | `"repos": {"mode":"deny","list":["owner/secret"]}` in config |
| Global hook, but **only** certain repos | `"repos": {"mode":"allow","list":["owner/a","owner/b"]}` |

So there are two independent off-switches for repos: a **`.whence-off` file in the
repo** (local, instant, no config) and the **`repos` list in your global config**
(central). Either one turns it off.

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

There are two triggers; they're idempotent, so use either or both.

**Reliable — your agent's own hook.** An agent hook fires *in the agent's
process*, so it always runs regardless of how the agent shells out. It's the
dependable choice for "every PR this agent opens gets stamped." One command wires
it:

```bash
whence --install-agent-hook          # Claude (global) + Codex (this repo)
whence --install-agent-hook claude   # just Claude
whence --install-agent-hook codex    # just Codex, in the current repo
```

It writes a small `pr-hook.sh` and registers a `PostToolUse` hook that runs
`whence --hook` — which reads the tool call, and stamps only when the command
actually opened a PR (`gh`/`ghapp pr create`, `shipyard pr`, `pulp pr`). **Claude**
is global (`~/.claude/settings.json`). **Codex** only reads each repo's own
`.codex/hooks.json` and gates hooks behind a trust prompt, so run the `codex`
variant inside each repo you want covered — Codex will ask you to trust it once.
(cmux also wires Claude's hooks automatically.) See
[`examples/claude-code-hook.md`](examples/claude-code-hook.md) for the manual form.

**Best-effort, zero per-agent config — the shell hook.**

```bash
whence --install-hook          # once per machine
```

This wraps the *command that opens the PR* with a small **shell function**
(written to `~/.config/whence/hook.sh`, sourced from `~/.zshenv`) that stamps the
branch's PR right after. It covers **every common way a PR gets opened** —
`gh pr create`, `ghapp pr create`, `shipyard pr`, `pulp pr` (whichever you have)
— with no per-agent config and no Shipyard dependency. A function beats `PATH`
(so a `.zshrc` PATH reorder can't shadow it) and calls the real tool via
`command` (no recursion, no double-stamp).

Its limit, honestly: it only fires in shells that load your init file, so it
catches PRs you open in a normal terminal and in agents whose command shell
sources `~/.zshenv` — but an agent that runs commands in a bare shell
(`bash -c`, no rc) won't trigger it. That's why the agent hook above is the
reliable path; the shell hook is a convenient net for everything else. Over-firing
is harmless (whence no-ops when the branch has no PR). Uninstall with
`whence --uninstall-hook`.

> **zsh note:** `whence` is a zsh builtin (like `which`), so at an interactive
> zsh prompt run **`command whence …`** (or the full path) to reach this tool.
> The hooks already do this internally; it only matters when you type it by hand.

Why a command shim and not "an agent hook": each agent's hook system is a
*different* config, and most of cmux's agents have no PR-time hook at all. The PR
command is the one layer they all share. The shim runs in the agent's own shell,
so the session/tab it captures is real; it never blocks or changes the PR
command's result; and because stamping is idempotent, it's safe even if a second
mechanism (below) also fires.

```bash
whence --print-hook            # see exactly what it would install, write nothing
```

The functions live in `~/.config/whence/hook.sh`, sourced by a clearly-marked
block in `~/.zshenv` (and `~/.bashrc` if present). `whence --uninstall-hook`
removes both.

**Keep a tab's PRs current.** whence captures the tab title at PR-open. If you
rename a tab later and want its already-open PRs updated, run `whence --resync`
from that tab — it finds this tab's open PRs (by the surface id already in their
footers — no tracking, no daemon) and re-stamps them with the current name.

**Other ways** (optional; they coexist with the shim — stamping is idempotent):

- **Claude Code / Codex hook** — if you'd rather trigger from the agent. See
  [`examples/claude-code-hook.md`](examples/claude-code-hook.md).
- **Merge orchestrators** (e.g. Shipyard) — stamp centrally at the PR-open step.
  See [`examples/shipyard.md`](examples/shipyard.md).
- **As an agent skill** — [`skill/SKILL.md`](skill/SKILL.md) tells an agent to
  stamp its PR right after opening it.

## Configure anything — one file, every knob

Everything is on by default; turn off whatever you want. Run **`whence --init`**
to drop a fully-populated config at `~/.config/whence/config.json`, then flip
values. **`whence --show`** prints the file's location and your effective
settings any time (so you never have to wonder where it lives).

```json
{
  "fields": {
    "agent": true, "host": true, "workspace": true, "tab": true,
    "session": true, "resume": true, "url": true,
    "jump": true, "relaunch": true, "stamped": true
  },
  "labels": true,
  "footer": true,
  "colors": { "agent": "1f6feb", "host": "1a7f37", "workspace": "bf8700", "tab": "8250df" },
  "repos": { "mode": "all", "list": [] },
  "gh": "gh",
  "label_maxlen": 24
}
```

Long tab/workspace names are clipped to `label_maxlen` chars (default 24) with a
`…` on the **queue label** only — the **footer always keeps the full name**. Set
it shorter or longer to taste.

| To drop… | Set |
|----------|-----|
| the **machine name** | `"fields": { "host": false }` |
| the **agent name** | `"fields": { "agent": false }` |
| the tab / session / url / any field | `"fields": { "url": false }`, … |
| the **whole PR-description footer** (keep labels) | `"footer": false` |
| the **labels** (keep footer) | `"labels": false` |

A field set false disappears from **both** the labels and the footer. Prefer a
one-off? `--hide host,agent`, `--no-footer`, `--no-labels`, or
`WHENCE_HIDE=url,session` do the same without editing the file.

**Colors:** any GitHub label hex, per category. **Machine label:**
`WHENCE_HOST_LABEL` env beats the `host-label` file. **`gh` binary:** set
`"gh": "ghapp"` in config (or `WHENCE_GH=ghapp`) to authenticate as a GitHub
App instead of the shared personal token.

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
