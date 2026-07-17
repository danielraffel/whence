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
import json
import pathlib
import subprocess
import sys
import tempfile
from unittest import mock

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
        ("the action's preceding cd wins", f"cd /tmp && echo hi\ncd {tmp} && shipyard pr", tmp),
        ("a later diagnostic cd is ignored", f"cd {tmp} && shipyard pr; cd /tmp && git status", tmp),
        ("a quoted diagnostic is skipped before the action",
         f'cd /tmp && rg "shipyard pr" whence; cd {tmp} && shipyard pr; cd /tmp', tmp),
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

    ref_payload = {
        "tool_response": {
            "stderr": "To github.com:danielraffel/pulp.git\n"
                      "   111aaaa..222bbbb  other-local -> fix/deferred\n"
        }
    }
    if w.pushed_source_ref(ref_payload) != "other-local":
        failed += 1
        print("FAIL  pushed_source_ref did not preserve the local source ref")
    else:
        print("ok    pushed_source_ref preserves local source != remote branch")

    with tempfile.TemporaryDirectory() as tmp:
        for name, cmd, want in cwd_cases(tmp):
            got = w._cmd_cwd(cmd)
            if got != want:
                failed += 1
                print(f"FAIL  _cmd_cwd: {name}\n      got={got!r}\n      want={want!r}")
            else:
                print(f"ok    _cmd_cwd: {name}")

        action_cases = [
            ("shipyard", f"cd {tmp} && shipyard pr --base main; cd /tmp", (True, True, tmp)),
            ("pulp", f"env PULP_X=1 pulp pr", (True, True, None)),
            ("gh", "command gh pr create --fill", (True, False, None)),
            ("git -C", f"git -C {tmp} push origin HEAD", (False, True, tmp)),
            ("quoted search", 'rg "shipyard pr|git push" whence', (False, False, None)),
            ("unquoted echo", "echo shipyard pr", (False, False, None)),
            ("git diagnostic", "git log -S 'git push' -- whence", (False, False, None)),
            ("quoted multiline", 'printf "shipyard pr\\ngit push\\n"', (False, False, None)),
            ("heredoc diagnostic",
             "python3 - <<'PY'\nprint('shipyard pr')\nprint('git push')\nPY\n",
             (False, False, None)),
            ("shell comment", "echo done # shipyard pr; git push\n", (False, False, None)),
            ("orchestrator help", "shipyard pr --help", (False, False, None)),
            ("PR dry-run", "gh pr create --dry-run", (False, False, None)),
        ]
        for name, cmd, want in action_cases:
            got = w._command_action(cmd)
            if got != want:
                failed += 1
                print(f"FAIL  command action: {name}: got={got!r} want={want!r}")
            else:
                print(f"ok    command action: {name}")

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

    # sanitize_path: strip the private home prefix, keep the folder; scrub denied.
    import os as _os
    home=_os.path.expanduser("~")
    pcases=[(home+"/Code/pulp","~/Code/pulp"),(home,"~"),("/tmp/x","/tmp/x")]
    for src,want in pcases:
        got=w.sanitize_path(src,{"denylist":[]})
        if got!=want:
            failed+=1; print(f"FAIL  sanitize_path({src!r})={got!r} want {want!r}")
        else: print(f"ok    sanitize_path -> {got!r}")
    if w.sanitize_path(home+"/Code/pulp-acme-port",{"denylist":["acme"]}).count("acme"):
        failed+=1; print("FAIL  sanitize_path did not scrub denied term")
    else: print("ok    sanitize_path scrubs denied term")

    # footer: commands are fenced code blocks (GitHub copy button); table present.
    fcfg={"hide":set(),"colors":w.DEFAULT_COLORS,"label_maxlen":24,"denylist":[],"redact_placeholder":"(redacted)"}
    fp={f:"" for f in w.FIELDS}
    fp.update({"agent":"claude","host":"m5","tab":"Fix caret","path":"~/Code/pulp",
               "resume":"claude --resume abc","jump":"cmux surface focus X","stamped":"t"})
    ft=w.footer(fp,fcfg,["claude","m5"])
    if "| **Agent** |" not in ft or "```\nclaude --resume abc\n```" not in ft or "**Directory**" not in ft:
        failed+=1; print("FAIL  footer table/copy/directory not rendered")
    else: print("ok    footer: table + fenced copy blocks + directory")

    # order_labels: prefixes force the queue's ALPHABETICAL sort into role order.
    lp={f:"" for f in w.FIELDS}; lp.update({"agent":"claude","host":"m5","workspace":"w1","tab":"Fix caret"})
    lbase={"hide":set(),"colors":w.DEFAULT_COLORS,"label_maxlen":24}
    on=[n for n,_ in w.labels_for(lp,{**lbase,"order_labels":True})]
    off=[n for n,_ in w.labels_for(lp,{**lbase,"order_labels":False})]
    if off!=["claude","m5","w1","Fix caret"] or on!=["1\u00b7claude","2\u00b7m5","3\u00b7w1","4\u00b7Fix caret"] or sorted(on)!=on:
        failed+=1; print(f"FAIL  order_labels: off={off} on={on} sorted={sorted(on)}")
    else: print("ok    order_labels: prefixed names sort into agent/host/workspace/tab")

    # A backgrounded orchestrator can return before its PR exists. The live hook
    # must launch a targeted retry instead of leaving the PR to the 10-minute
    # global sweep. PR #6195 was ledgered 29 seconds before GitHub created it.
    key = "danielraffel/pulp#fix/deferred"
    rec = {"p": {f: "" for f in w.FIELDS}, "ts": 1784270822, "head": "new-head"}
    rec["p"].update({"agent": "claude", "host": "m3", "tab": "Deferred PR"})
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = pathlib.Path(tmp) / "ledger.json"
        with mock.patch.object(w, "LEDGER", ledger_path), \
             mock.patch.object(w, "_now_epoch", return_value=1):
            recorded_key = w.ledger_record(
                "", rec["p"], "danielraffel/pulp", "fix/deferred",
                "origin/main", str(pathlib.Path.cwd()),
            )
            recorded_head = json.loads(ledger_path.read_text())[key]["head"]
    expected_head = subprocess.run(
        ["git", "rev-parse", "origin/main"], check=True, capture_output=True, text=True
    ).stdout.strip()
    if recorded_key != key or recorded_head != expected_head:
        failed += 1
        print(f"FAIL  ledger capture: key={recorded_key!r} head={recorded_head!r}")
    else:
        print("ok    ledger capture: deferred retry receives branch + HEAD identity")

    responses = iter([
        subprocess.CompletedProcess(
            [], 0,
            '[{"number":14,"body":"","createdAt":"2026-07-17T06:46:30Z",'
            '"headRefOid":"old-head"},'
            '{"number":15,"body":"","createdAt":"2026-07-17T06:46:55Z",'
            '"headRefOid":"new-head"},'
            '{"number":16,"body":"","createdAt":"2026-07-17T06:47:20Z",'
            '"headRefOid":"fork-head"}]',
            "",
        ),
        subprocess.CompletedProcess(
            [], 0,
            '[{"number":6195,"body":"","createdAt":"2026-07-17T06:47:31Z",'
            '"headRefOid":"new-head"}]',
            "",
        ),
    ])
    applied = []
    queries = []
    def fake_pr_list(*args, **kwargs):
        queries.append((args, kwargs))
        return next(responses)
    publication_cfg = {"labels": False, "footer": True}
    with mock.patch.object(w, "_load_ledger", return_value={key: rec}), \
         mock.patch.object(w, "sh", side_effect=fake_pr_list), \
         mock.patch.object(w, "apply_stamp", side_effect=lambda *a, **k: applied.append((a, k))), \
         mock.patch.object(w.time, "sleep") as sleep:
        rc = w.retry_pending_pr(key, publication_cfg, attempts=2, delay=0.01)
    policy_kept = (len(applied) == 1 and applied[0][0][0] == "6195"
                   and applied[0][0][3:5] == (False, True))
    timeouts_bounded = all(0 < q[1].get("timeout", 0) <= 5 for q in queries)
    if rc != 0 or not policy_kept or not timeouts_bounded or applied[0][1].get("repo") != "danielraffel/pulp" or sleep.call_count != 1:
        failed += 1
        print(f"FAIL  deferred retry: rc={rc} applied={applied} sleeps={sleep.call_count}")
    else:
        print("ok    deferred retry: exact HEAD appears later; old/fork PRs ignored")

    empty = subprocess.CompletedProcess([], 0, "[]", "")
    with mock.patch.object(w, "_load_ledger", return_value={key: rec}), \
         mock.patch.object(w, "sh", return_value=empty) as deadline_sh, \
         mock.patch.object(w.time, "monotonic", side_effect=[0, 0, 119, 121]), \
         mock.patch.object(w.time, "sleep") as deadline_sleep:
        w.retry_pending_pr(key, publication_cfg, attempts=24, delay=5, max_wait=120)
    if deadline_sh.call_count != 1 or deadline_sleep.call_args_list != [mock.call(1)]:
        failed += 1
        print(f"FAIL  retry deadline: queries={deadline_sh.call_count} sleeps={deadline_sleep.call_args_list}")
    else:
        print("ok    retry deadline: request + sleep cannot exceed two-minute budget")

    retry_cfg = {"hide": {"session", "url"}}
    with mock.patch.object(w, "_spawn_retry") as spawn:
        scheduled = w.maybe_retry_deferred_pr(True, "", key, retry_cfg, False, True)
        skipped_named = w.maybe_retry_deferred_pr(True, "6195", key, retry_cfg, False, True)
        skipped_push = w.maybe_retry_deferred_pr(False, "", key, retry_cfg, False, True)
    expected_spawn = [mock.call(key, retry_cfg, False, True)]
    if not scheduled or skipped_named or skipped_push or spawn.call_args_list != expected_spawn:
        failed += 1
        print(f"FAIL  deferred retry scheduling: scheduled={scheduled} named={skipped_named} push={skipped_push} calls={spawn.call_args_list}")
    else:
        print("ok    deferred retry scheduling: only unnamed PR-producing hooks spawn it")

    with mock.patch.object(w.subprocess, "Popen") as popen:
        spawned = w._spawn_retry(key, retry_cfg, False, True)
    popen_kwargs = popen.call_args.kwargs if popen.call_args else {}
    popen_args = popen.call_args.args[0] if popen.call_args else []
    forwards_policy = (popen_args[:4][-2:] == ["--retry-key", key]
                       and popen_args[popen_args.index("--hide") + 1] == "session,url"
                       and "--no-labels" in popen_args and "--no-footer" not in popen_args)
    if not spawned or not forwards_policy or not popen_kwargs.get("start_new_session"):
        failed += 1
        print(f"FAIL  detached retry process: spawned={spawned} args={popen_args} kwargs={popen_kwargs}")
    else:
        print("ok    detached retry process: worker receives the ledger key + privacy policy")

    # Pre-exec capture happens before a long-running orchestrator starts. It
    # records the exact current HEAD and forwards effective publication/privacy
    # policy to the detached worker without waiting for GitHub.
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "fix/preexec", str(repo)], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Whence Test"], check=True)
        (repo / "tracked").write_text("exact head\n")
        subprocess.run(["git", "-C", str(repo), "add", "tracked"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "test"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                        "git@github.com:danielraffel/preexec-test.git"], check=True)
        ledger_path = pathlib.Path(tmp) / "ledger.json"
        pp = {f: "" for f in w.FIELDS}
        pp.update({"agent": "codex", "host": "m5", "tab": "Long PR"})
        pcfg = {"hide": {"session", "url"}, "labels": False, "footer": True,
                "repos": {"mode": "all", "list": []}, "denylist": []}
        with mock.patch.object(w, "LEDGER", ledger_path), \
             mock.patch.object(w, "collect", return_value=pp), \
             mock.patch.object(w, "_spawn_retry", return_value=True) as pre_spawn, \
             mock.patch.object(w, "_now_epoch", return_value=1784317000):
            pre_key = w.preexec_capture(str(repo), pcfg, False, True)
        pre_rec = json.loads(ledger_path.read_text()).get(pre_key, {}) if pre_key else {}
        current_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True
        ).stdout.strip()
        pre_ok = (pre_key == "danielraffel/preexec-test#fix/preexec"
                  and pre_rec.get("head") == current_head
                  and pre_rec.get("p", {}).get("path", "").endswith("/repo")
                  and pre_spawn.call_args_list == [mock.call(pre_key, pcfg, False, True)])
        if not pre_ok:
            failed += 1
            print(f"FAIL  pre-exec capture: key={pre_key!r} rec={pre_rec} calls={pre_spawn.call_args_list}")
        else:
            print("ok    pre-exec capture: exact HEAD + privacy policy recorded before launch")

        (repo / ".whence-off").write_text("")
        with mock.patch.object(w, "LEDGER", ledger_path), \
             mock.patch.object(w, "collect", return_value=pp), \
             mock.patch.object(w, "_spawn_retry") as disabled_spawn:
            disabled_key = w.preexec_capture(str(repo), pcfg, False, True)
        if disabled_key or disabled_spawn.called:
            failed += 1
            print(f"FAIL  pre-exec repo opt-out: key={disabled_key!r} calls={disabled_spawn.call_args_list}")
        else:
            print("ok    pre-exec capture: .whence-off remains authoritative")

    # Drive the generated shell wrapper with fake commands. The fake PR appears
    # only after shipyard starts, and shipyard refuses to exit until the worker
    # launched by --pre-exec has stamped it. A post-exit-only hook deadlocks/fails.
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        bindir, state = root / "bin", root / "state"
        bindir.mkdir(); state.mkdir()
        fake_whence = bindir / "whence"
        fake_shipyard = bindir / "shipyard"
        fake_whence.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = --pre-exec ]; then\n"
            "  touch \"$WHENCE_FAKE_STATE/preexec\"\n"
            "  (while [ ! -f \"$WHENCE_FAKE_STATE/pr-created\" ]; do sleep 0.01; done; "
            "touch \"$WHENCE_FAKE_STATE/stamped\") >/dev/null 2>&1 &\n"
            "elif [ \"$1\" = --sweep ]; then\n"
            "  touch \"$WHENCE_FAKE_STATE/swept\"\n"
            "elif [ \"$1\" = --auto ]; then\n"
            "  touch \"$WHENCE_FAKE_STATE/recollected\"\n"
            "fi\n"
            "exit 0\n"
        )
        fake_shipyard.write_text(
            "#!/bin/sh\n"
            "if [ \"$2\" = --help ]; then touch \"$WHENCE_FAKE_STATE/help\"; exit 0; fi\n"
            "touch \"$WHENCE_FAKE_STATE/started\"\n"
            "sleep 0.05\n"
            "touch \"$WHENCE_FAKE_STATE/pr-created\"\n"
            "i=0; while [ ! -f \"$WHENCE_FAKE_STATE/stamped\" ] && [ $i -lt 100 ]; do "
            "sleep 0.01; i=$((i+1)); done\n"
            "test -f \"$WHENCE_FAKE_STATE/stamped\"\n"
        )
        fake_whence.chmod(0o755); fake_shipyard.chmod(0o755)
        # Generation must not depend on this non-interactive process's PATH:
        # deploy over SSH often cannot see user-installed shipyard/pulp yet the
        # resulting hook is sourced later from an interactive shell that can.
        with mock.patch("shutil.which", return_value=None):
            hook_text, wrapped_tools = w._hook_file_text()
        if wrapped_tools != list(w.PR_TOOLS) or not all(
                f"{tool}()" in hook_text for tool in w.PR_TOOLS):
            failed += 1
            print(f"FAIL  deterministic wrappers: tools={wrapped_tools}")
        else:
            print("ok    shell wrapper generation: reduced deploy PATH cannot drop tools")
        hook_file = root / "hook.sh"
        hook_file.write_text(hook_text)
        env = dict(**__import__("os").environ)
        env.update({"PATH": f"{bindir}:{env.get('PATH', '')}", "WHENCE_FAKE_STATE": str(state)})
        driven = subprocess.run(
            ["zsh", "-fc", f'source "{hook_file}"; shipyard pr'],
            env=env, capture_output=True, text=True, timeout=5,
        )
        lifecycle_ok = (driven.returncode == 0 and (state / "preexec").exists()
                        and (state / "started").exists() and (state / "stamped").exists()
                        and (state / "swept").exists()
                        and not (state / "recollected").exists())
        if not lifecycle_ok:
            failed += 1
            print(f"FAIL  long-running wrapper: rc={driven.returncode} out={driven.stdout!r} err={driven.stderr!r}")
        else:
            print("ok    long-running wrapper: pre-exec stamp kept; post path sweeps ledger")
        for name in ("preexec", "stamped", "pr-created", "started", "swept", "recollected"):
            try: (state / name).unlink()
            except FileNotFoundError: pass
        help_run = subprocess.run(
            ["zsh", "-fc", f'source "{hook_file}"; shipyard pr --help'],
            env=env, capture_output=True, text=True, timeout=5,
        )
        help_ok = (help_run.returncode == 0 and (state / "help").exists()
                   and not any((state / name).exists()
                               for name in ("preexec", "stamped", "swept", "recollected")))
        if not help_ok:
            failed += 1
            print(f"FAIL  wrapper diagnostic: rc={help_run.returncode} state={[p.name for p in state.iterdir()]}")
        else:
            print("ok    shell wrapper: help diagnostic does not capture or stamp")

    print(f"\n{'ALL PASS' if not failed else f'{failed} FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
