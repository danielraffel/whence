#!/usr/bin/env python3
"""Tests for the attribution whence can't afford to get wrong.

`parse_outcome` is the load-bearing piece: it reads the repo/branch/PR out of a
command's own OUTPUT, which is the only cwd-independent signal available to an
agent hook. Everything else (which PR gets stamped, which ledger key a push files
under) rides on it, and a miss is SILENT — the PR just never gets a label. So the
real-world output shapes are pinned here.

Run: python3 test_whence.py
"""
import importlib.util
import pathlib
import sys

_src = (pathlib.Path(__file__).parent / "whence").read_text().split("def main(")[0]
w = importlib.util.module_from_spec(importlib.util.spec_from_loader("whence", loader=None))
exec(_src, w.__dict__)  # noqa: S102 — the script is not importable as a module (no .py)


# (name, tool_response, expected (slug, branch, pr))
OUTCOMES = [
    # ── a PR was opened: the URL names the repo and the number outright ──
    ("gh pr create",
     {"stdout": "https://github.com/danielraffel/pulp/pull/6081\n"},
     ("danielraffel/pulp", "", "6081")),
    ("orchestrator prints the URL in prose",
     {"stdout": "✓ opened PR: https://github.com/danielraffel/pulp/pull/6088 (feature/x)\n"},
     ("danielraffel/pulp", "", "6088")),
    ("tool_response as a bare string",
     "https://github.com/danielraffel/whence/pull/7\n",
     ("danielraffel/whence", "", "7")),

    # ── a push: no PR yet (a daemon may open one later), but repo+branch are named,
    #    which is exactly what the ledger needs so the sweep can find it ──
    ("push, new branch (remote suggests a PR)",
     {"stderr": "remote: Create a pull request for 'fix/x' on GitHub by visiting:\n"
                "remote:      https://github.com/danielraffel/pulp/pull/new/fix/x\n"
                "To github.com:danielraffel/pulp.git\n"
                " * [new branch]      fix/x -> fix/x\n"},
     ("danielraffel/pulp", "fix/x", "")),
    ("push, existing branch (scp-style remote, no user)",
     {"stderr": "To github.com:danielraffel/pulp-planning.git\n"
                "   a1b2c3d..e4f5a6b  main -> main\n"},
     ("danielraffel/pulp-planning", "main", "")),
    ("push, git@ remote",
     {"stderr": "To git@github.com:danielraffel/pulp.git\n"
                "   111aaaa..222bbbb  fix/z -> fix/z\n"},
     ("danielraffel/pulp", "fix/z", "")),
    ("push, https remote",
     {"stderr": "To https://github.com/danielraffel/tartci.git\n"
                "   1111111..2222222  feature/y -> feature/y\n"},
     ("danielraffel/tartci", "feature/y", "")),
    ("force-push (trailing note after the refspec)",
     {"stderr": "To github.com:danielraffel/pulp.git\n"
                " + 999ffff...888eeee  fix/w -> fix/w (forced update)\n"},
     ("danielraffel/pulp", "fix/w", "")),
    ("push to a fully-qualified dest ref",
     {"stderr": "To github.com:danielraffel/pulp.git\n"
                "   111aaaa..222bbbb  HEAD -> refs/heads/fix/q\n"},
     ("danielraffel/pulp", "fix/q", "")),

    # ── nothing happened / not ours: stay silent rather than guess ──
    ("no-op push", {"stdout": "Everything up-to-date\n"}, ("", "", "")),
    ("a push to some other forge is not ours",
     {"stderr": "To git@gitlab.com:acme/thing.git\n   111..222  main -> main\n"},
     ("", "", "")),
    ("empty response", {}, ("", "", "")),
]


# How the worktree ACTUALLY gets named in a real command. A backgrounded
# `shipyard pr` returns no output to parse and the hook's cwd is the session root,
# so this `cd` is the only thing naming the worktree — and a miss here is a PR
# with no label. Measured against ~330 real `shipyard pr` commands from a year of
# transcripts: the old `cd X &&`-prefix-only parser read 46% of them, these forms
# are the other half.
def cwd_cases(tmp):
    return [
        ("cd X && cmd", f"cd {tmp} && shipyard pr", tmp),
        ("cd on its own line", f"cd {tmp}\nshipyard pr --base main", tmp),
        ("cd via a variable set in the same command", f'WT={tmp}\ncd "$WT"\nshipyard pr', tmp),
        ("${BRACED} variable", f'WT={tmp}\ncd "${{WT}}" && shipyard pr', tmp),
        ("quoted path", f"cd '{tmp}' && shipyard pr", tmp),
        ("git -C, no cd at all", f"git -C {tmp} push origin HEAD", tmp),
        ("the LAST cd wins", f"cd /tmp && echo hi\ncd {tmp} && shipyard pr", tmp),
        ("env prefix before the tool", f"cd {tmp} && PULP_SKIP_DIFF_COVER=1 shipyard pr", tmp),
        ("no cd — caller falls back to its own cwd", "shipyard pr --help", None),
        ("a path that doesn't exist tells us nothing",
         "cd /nonexistent/wt-xyz && shipyard pr", None),
    ]


def main() -> int:
    failed = 0
    for name, tr, want in OUTCOMES:
        got = w.parse_outcome({"tool_response": tr})
        if got != want:
            failed += 1
            print(f"FAIL  {name}\n      got={got}\n      want={want}")
        else:
            print(f"ok    {name}")

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        for name, cmd, want in cwd_cases(tmp):
            got = w._cmd_cwd(cmd)
            if got != want:
                failed += 1
                print(f"FAIL  _cmd_cwd: {name}\n      got={got!r}\n      want={want!r}")
            else:
                print(f"ok    _cmd_cwd: {name}")

    # Denylist redaction: a cmux tab/workspace title with a forbidden name must
    # never reach a label OR the public footer. cmux gives us no way to rename a
    # tab, so redaction at publish time is the only enforcement.
    cfg = {"denylist": ["acme", "widgetworks", "codename-zephyr"],
           "redact_placeholder": "(redacted)", "hide": set(),
           "colors": w.DEFAULT_COLORS, "label_maxlen": 24}
    deny_cases = [
        ("clean title is not denied", "Investigate denormal ODR", False),
        ("VST3 is allowed (not on the list)", "VST3 bus arrangement", False),
        ("denied term anywhere in the title", "Port the Acme reverb", True),
        ("case-insensitive", "acme param mapping", True),
        ("substring: a longer word that contains a denied term", "regen Acmelab project", True),
        ("multi-word denied term", "WidgetWorks VST3 quirk", True),
        ("codename", "codename-Zephyr graphics port", True),
    ]
    for name, title, want_denied in deny_cases:
        got = bool(w.denied(title, cfg))
        if got != want_denied:
            failed += 1
            print(f"FAIL  denied: {name}: got={got} want={want_denied}")
        else:
            print(f"ok    denied: {name}")

    # redact() scrubs the denied TERM but keeps the readable rest of the name.
    pr = {"tab": "Improve Acme import", "workspace": "widgetworks-quirks",
          "agent": "claude", "host": "m5"}
    hit = w.redact(pr, cfg, surface_id="")
    blob = (pr["tab"] + " " + pr["workspace"]).lower()
    leaked = [t for t in cfg["denylist"] if t in blob]
    label_leaks = [n for n, _ in w.labels_for(pr, cfg) if w.denied(n, cfg)]
    kept_word = "improve" in pr["tab"].lower() and "import" in pr["tab"].lower()
    if leaked or label_leaks or set(hit) != {"tab", "workspace"} or not kept_word:
        failed += 1
        print(f"FAIL  redact: tab={pr['tab']!r} leaked={leaked} label_leaks={label_leaks} hit={hit}")
    else:
        print(f"ok    redact: denied term cut, name kept -> tab={pr['tab']!r}")

    # scrub_denied specifics: keep the surrounding words, tidy the gap.
    for src, want in [("Improve JUCE import", "Improve import"),
                      ("JUCE", ""), ("steinberg VST3 quirk", "VST3 quirk")]:
        # use the real fleet-style terms for this sub-check
        c2 = {"denylist": ["juce", "steinberg"], "redact_placeholder": "(redacted)"}
        got = w.scrub_denied(src, c2)
        if got != want:
            failed += 1
            print(f"FAIL  scrub_denied({src!r}) = {got!r} want {want!r}")
        else:
            print(f"ok    scrub_denied({src!r}) -> {got!r}")

    # self-heal helpers: a ref/blank/unknown stamp is degraded; a named one is good.
    healcfg = {"redact_placeholder": "(redacted)"}
    checks = [
        ("named+agent is good", {"tab": "Fix caret", "agent": "claude"}, True),
        ("blank tab is degraded", {"tab": "", "agent": "claude"}, False),
        ("ref tab is degraded", {"tab": "surface:26", "agent": "claude"}, False),
        ("unknown agent is degraded", {"tab": "Fix caret", "agent": "unknown"}, False),
    ]
    for name, prov, good in checks:
        if w._prov_good(prov, healcfg) != good:
            failed += 1
            print(f"FAIL  _prov_good: {name}")
        else:
            print(f"ok    _prov_good: {name}")

    # _prov_better upgrades a degraded stamp but never chases a rename.
    better = w._prov_better({"tab": "Real name", "agent": "codex"},
                            {"tab": "surface:26", "agent": "unknown"}, healcfg)
    rename = w._prov_better({"tab": "New name", "agent": "claude"},
                            {"tab": "Old name", "agent": "claude"}, healcfg)
    if not better or rename:
        failed += 1
        print(f"FAIL  _prov_better: upgrade={better} (want True)  rename={rename} (want False)")
    else:
        print("ok    _prov_better: heals degraded, ignores renames")

    # A workspace cmux auto-titled is just some tab's name wearing a workspace
    # label — the bug that put two tab-looking labels on one PR. No id, no label.
    if w.cmux_workspace("") != "":
        failed += 1
        print("FAIL  cmux_workspace('') must be empty")
    else:
        print("ok    cmux_workspace('') is empty")

    print(f"\n{'ALL PASS' if not failed else f'{failed} FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
