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

    # A configured GitHub App remains the preferred client. It may fall back to
    # ambient user auth only when GitHub says the App cannot access this exact
    # repository and ambient gh proves that it can. Never turn a transient or
    # unrelated auth failure into a surprising identity switch.
    app = "/Users/test/.local/bin/ghapp"
    ambient = "/opt/homebrew/bin/gh"

    def client_case(name, responses, want, *, explicit=False, fallback=ambient):
        nonlocal failed
        calls = []

        def fake_sh(*args, **kwargs):
            calls.append(args)
            return responses[len(calls) - 1]

        w._GH_CLIENT_CACHE.clear()
        with mock.patch.object(w, "sh", side_effect=fake_sh), \
             mock.patch.object(w.shutil, "which", return_value=fallback):
            got = w.github_client_for_repo(
                app, "danielraffel/pulp-planning", explicit=explicit)
        if got != want:
            failed += 1
            print(f"FAIL  GitHub client: {name}: got={got!r} want={want!r} calls={calls!r}")
        else:
            print(f"ok    GitHub client: {name}")
        return calls

    ok_repo = subprocess.CompletedProcess([], 0, "danielraffel/pulp-planning\n", "")
    missing_app = subprocess.CompletedProcess(
        [], 1, "", "GraphQL: Could not resolve to a Repository with the name 'danielraffel/pulp-planning'.")
    inaccessible_app = subprocess.CompletedProcess(
        [], 1, "", "GraphQL: Resource not accessible by integration")
    network_error = subprocess.CompletedProcess([], 1, "", "error connecting to api.github.com")
    wrong_repo = subprocess.CompletedProcess([], 0, "somebody/other-repo\n", "")

    calls = client_case("accessible App stays selected", [ok_repo], app)
    if len(calls) != 1:
        failed += 1; print(f"FAIL  accessible App unexpectedly probed ambient gh: {calls!r}")
    client_case("missing App installation uses verified ambient gh",
                [missing_app, ok_repo], ambient)
    client_case("integration access denial uses verified ambient gh",
                [inaccessible_app, ok_repo], ambient)
    calls = client_case("explicit client never falls back", [], app, explicit=True)
    if calls:
        failed += 1; print(f"FAIL  explicit client was unexpectedly probed: {calls!r}")
    calls = client_case("network errors fail closed on configured client",
                        [network_error], app)
    if len(calls) != 1:
        failed += 1; print(f"FAIL  network failure unexpectedly probed ambient gh: {calls!r}")
    client_case("ambient gh must prove the exact repository",
                [missing_app, wrong_repo], app)
    client_case("missing ambient gh leaves configured client selected",
                [missing_app], app, fallback=None)

    rejected = subprocess.CompletedProcess([], 1, "", "Resource not accessible by integration")
    with mock.patch.object(w, "sh", return_value=rejected):
        try:
            w.github_call(app, "pr", "edit", "24")
        except RuntimeError as exc:
            checked_failure = "Resource not accessible by integration" in str(exc)
        else:
            checked_failure = False
    if not checked_failure:
        failed += 1
        print("FAIL  GitHub mutation rejection was reported as success")
    else:
        print("ok    GitHub mutation rejection fails visibly")

    mutation_calls = []
    mutation_responses = iter([
        rejected,
        ok_repo,
        subprocess.CompletedProcess([], 0, "updated\n", ""),
    ])
    def mutation_sh(*args, **kwargs):
        mutation_calls.append(args)
        return next(mutation_responses)
    w._GH_CLIENT_CACHE.clear()
    with mock.patch.object(w, "sh", side_effect=mutation_sh), \
         mock.patch.object(w.shutil, "which", return_value=ambient):
        retried = w.github_call(
            app, "pr", "edit", "24", "--repo", "danielraffel/pulp-planning",
            "--add-label", "1·codex")
    retried_with_ambient = (
        retried.returncode == 0 and len(mutation_calls) == 3
        and mutation_calls[0][0] == app
        and mutation_calls[1][0] == ambient
        and mutation_calls[2][0] == ambient)
    if not retried_with_ambient:
        failed += 1
        print(f"FAIL  read-only App mutation fallback: {mutation_calls!r}")
    else:
        print("ok    read-only App mutation retries through verified ambient gh")

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
            ("nested background shell",
             f"nohup bash -lc 'cd {tmp} && exec shipyard pr --base main' >/tmp/ship.log 2>&1 &",
             (True, True, tmp)),
            ("detached nested background shell",
             f"setsid nohup bash -lc 'cd {tmp} && exec shipyard pr --base main' >/tmp/ship.log 2>&1 &",
             (True, True, tmp)),
            ("command in a variable",
             f"GHAPP=~/.local/bin/ghapp; cd {tmp}; $GHAPP pr create --fill",
             (True, False, tmp)),
            ("timeout wrapper",
             f"cd {tmp}; PULP_SKIP_DIFF_COVER=1 timeout 420 shipyard pr --base main",
             (True, True, tmp)),
            ("timeout wrapper with valued options",
             f"cd {tmp}; timeout --signal TERM --kill-after=5 420 shipyard pr --base main",
             (True, True, tmp)),
            ("git -C", f"git -C {tmp} push origin HEAD", (False, True, tmp)),
            ("quoted search", 'rg "shipyard pr|git push" whence', (False, False, None)),
            ("quoted search in nested shell",
             'bash -lc \'rg "shipyard pr" whence\'', (False, False, None)),
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

        context_cases = [
            ("explicit handoff",
             "shipyard pr --workstream-id SY-LF-2026-08-20",
             {"workstream": "SY-LF-2026-08-20"}),
            ("equals handoff through wrappers",
             "nohup env WHENCE_LAUNCHER=cmux WHENCE_ROUTE=direct "
             "shipyard pr --workstream-id=SY-LF-P1 &",
             {"workstream": "SY-LF-P1", "launcher": "cmux", "route": "direct"}),
            ("nested delayed worker",
             "WHENCE_LAUNCHER=cmux setsid bash -lc "
             "'exec shipyard pr --workstream-id SY-LF-P2' &",
             {"workstream": "SY-LF-P2", "launcher": "cmux"}),
            ("outer context survives nested command without inner flag",
             "WHENCE_LAUNCHER=cmux WHENCE_ROUTE=direct nohup bash -lc "
             "'exec shipyard pr' &",
             {"launcher": "cmux", "route": "direct"}),
            ("diagnostic literal is not context",
             "rg 'shipyard pr --workstream-id WRONG' .",
             {}),
            ("malformed explicit value fails closed",
             "WHENCE_ROUTE=https://router.invalid shipyard pr --workstream-id=/private/path",
             {"workstream": "", "route": ""}),
        ]
        for name, cmd, want in context_cases:
            got = w.command_provenance_context(cmd)
            if got != want:
                failed += 1
                print(f"FAIL  command provenance: {name}: got={got!r} want={want!r}")
            else:
                print(f"ok    command provenance: {name}")

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
        ("named+agent is good", {"tab": "Fix caret", "agent": "claude", "origin_state": "known"}, True),
        ("blank tab is degraded", {"tab": "", "agent": "claude", "origin_state": "unnamed"}, False),
        ("ref tab is degraded", {"tab": "surface:26", "agent": "claude", "origin_state": "lookup_failed"}, False),
        ("unknown agent is degraded", {"tab": "Fix caret", "agent": "unknown", "origin_state": "unresolved"}, False),
        ("automation needs no tab", {"tab": "", "agent": "automation", "origin_state": "automation"}, True),
        ("external needs no tab", {"tab": "", "agent": "external", "origin_state": "external"}, True),
    ]
    for name, prov, good in checks:
        if w._prov_good(prov, healcfg) != good:
            failed += 1
            print(f"FAIL  _prov_good: {name}")
        else:
            print(f"ok    _prov_good: {name}")

    # _prov_better upgrades degraded/missing provenance but never chases a rename.
    better = w._prov_better({"tab": "Real name", "agent": "codex", "origin_state": "known"},
                            {"tab": "surface:26", "agent": "unknown", "origin_state": "lookup_failed"}, healcfg)
    rename = w._prov_better({"tab": "New name", "agent": "claude", "origin_state": "known"},
                            {"tab": "Old name", "agent": "claude", "origin_state": "known"}, healcfg)
    goal_upgrade = w._prov_better(
        {"tab": "Old name", "agent": "claude", "goals": "https://example.com/goal"},
        {"tab": "Old name", "agent": "claude", "goals": ""}, healcfg)
    if not better or rename or not goal_upgrade:
        failed += 1
        print(f"FAIL  _prov_better: upgrade={better} rename={rename} goal={goal_upgrade}")
    else:
        print("ok    _prov_better: heals degraded/goals, ignores renames")

    # A workspace cmux auto-titled is just some tab's name wearing a workspace
    # label — the bug that put two tab-looking labels on one PR. No id, no label.
    if w.cmux_workspace("") != "":
        failed += 1
        print("FAIL  cmux_workspace('') must be empty")
    else:
        print("ok    cmux_workspace('') is empty")

    # Agent hooks can lose CMUX_WORKSPACE_ID while retaining the stable surface
    # UUID. Recover a deliberately named workspace from pane membership; do not
    # invent a label for an auto-titled workspace.
    ws_list = subprocess.CompletedProcess([], 0, json.dumps({"workspaces": [
        {"id": "named", "has_custom_title": True, "custom_title": "w1"},
        {"id": "auto", "has_custom_title": False, "custom_title": None},
    ]}), "")
    named_panes = subprocess.CompletedProcess([], 0, json.dumps({"panes": [
        {"surface_ids": ["SURFACE-NAMED"]}
    ]}), "")
    auto_panes = subprocess.CompletedProcess([], 0, json.dumps({"panes": [
        {"surface_ids": ["SURFACE-AUTO"]}
    ]}), "")
    def fake_cmux_workspace(*args, **kwargs):
        if args[2] == "workspace.list":
            return ws_list
        params = json.loads(args[3])
        return named_panes if params["workspace_id"] == "named" else auto_panes
    with mock.patch.object(w, "sh", side_effect=fake_cmux_workspace):
        recovered_named = w.cmux_workspace("", "SURFACE-NAMED")
        recovered_auto = w.cmux_workspace("", "SURFACE-AUTO")
    if recovered_named != "w1" or recovered_auto != "":
        failed += 1
        print(f"FAIL  workspace recovery: named={recovered_named!r} auto={recovered_auto!r}")
    else:
        print("ok    workspace recovery: surface membership restores named workspace only")

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

    # Goal documents are durable provenance, not transient PR-body prose. Keep
    # only safe HTTP(S) URLs, deduplicate them, and render each as a link.
    goals = w.normalize_goals([
        "https://github.com/acme/planning/blob/main/goal.md",
        "https://github.com/acme/planning/blob/main/goal.md",
        "javascript:alert(1)",
        "https://example.com/second goal",
        "https://example.com/second",
    ])
    gp = {f: "" for f in w.FIELDS}
    gp.update({"agent": "codex", "goals": goals, "stamped": "t"})
    gft = w.footer(gp, {"hide": set()}, ["codex"])
    if (goals.splitlines() != ["https://github.com/acme/planning/blob/main/goal.md",
                               "https://example.com/second"]
            or "| **Goal docs** | <https://github.com/acme/planning/blob/main/goal.md><br><https://example.com/second> |" not in gft
            or "javascript:" in gft):
        failed += 1
        print(f"FAIL  goals: normalized={goals!r} footer={gft!r}")
    else:
        print("ok    goals: safe durable URLs normalized + linked in provenance")

    # order_labels: prefixes force the queue's ALPHABETICAL sort into role order.
    lp={f:"" for f in w.FIELDS}; lp.update({"agent":"claude","host":"m5","workspace":"w1","tab":"Fix caret","route":"subrouter"})
    lbase={"hide":set(),"colors":w.DEFAULT_COLORS,"label_maxlen":24}
    on=[n for n,_ in w.labels_for(lp,{**lbase,"order_labels":True})]
    off=[n for n,_ in w.labels_for(lp,{**lbase,"order_labels":False})]
    if off!=["claude","m5","w1","Fix caret","subrouter"] or on!=["1\u00b7claude","2\u00b7m5","3\u00b7w1","4\u00b7Fix caret","5\u00b7subrouter"] or sorted(on)!=on:
        failed+=1; print(f"FAIL  order_labels: off={off} on={on} sorted={sorted(on)}")
    else: print("ok    order_labels: prefixed names sort into agent/host/workspace/tab/route")

    # Route/workstream identifiers cross a public boundary. Credentials, URLs,
    # emails, paths, and query strings must fail closed instead of being stamped.
    safe_ids = {
        "agent-workstream-continuity-20260813": "agent-workstream-continuity-20260813",
        "subrouter-cli": "subrouter-cli", "m3": "m3", "direct": "direct",
        "https://m3:31415": "", "person@example.com": "", "/Users/me/key": "",
        "subrouter?token=secret": "",
    }
    for src, want in safe_ids.items():
        got = w.stable_identifier(src)
        if got != want:
            failed += 1; print(f"FAIL  stable_identifier({src!r})={got!r} want={want!r}")
        else: print(f"ok    stable_identifier({src!r}) -> {got!r}")

    provenance_cfg = {
        "denylist": [], "hide": set(),
        "provenance": {
            "default": {"launcher": "cmux", "route": "direct"},
            "repositories": {
                "Generous-Corp/pulp": {
                    "workstream": "SY-LF-2026-08-20", "route": "shipyard-daemon"
                },
                "Generous-Corp/bad": {"route": "https://router.invalid"},
            },
        },
    }
    collect_patches = (
        mock.patch.object(w, "host_label", return_value="m3"),
        mock.patch.object(w, "cmux_workspace", return_value=""),
        mock.patch.object(w, "cmux_tab_title", return_value=("", "")),
        mock.patch.object(w, "sh", return_value=subprocess.CompletedProcess([], 1, "", "")),
    )
    with mock.patch.dict(_os.environ, {}, clear=True), collect_patches[0], \
         collect_patches[1], collect_patches[2], collect_patches[3]:
        absent = w.collect({"denylist": [], "hide": set()}, "Generous-Corp/pulp")
        configured = w.collect(provenance_cfg, "Generous-Corp/pulp")
        malformed = w.collect(provenance_cfg, "Generous-Corp/bad")
    with mock.patch.dict(_os.environ, {
            "WHENCE_WORKSTREAM_ID": "SY-LF-P3", "WHENCE_LAUNCHER": "cmux-continue-session",
            "WHENCE_ROUTE": "subrouter", "WHENCE_ROUTER": "m5",
         }, clear=True), mock.patch.object(w, "host_label", return_value="m3"), \
         mock.patch.object(w, "cmux_workspace", return_value=""), \
         mock.patch.object(w, "cmux_tab_title", return_value=("", "")), \
         mock.patch.object(w, "sh", return_value=subprocess.CompletedProcess([], 1, "", "")):
        inherited = w.collect(provenance_cfg, "Generous-Corp/pulp")
    with mock.patch.dict(_os.environ, {"WHENCE_ROUTE": "https://bad.invalid"}, clear=True), \
         mock.patch.object(w, "host_label", return_value="m3"), \
         mock.patch.object(w, "cmux_workspace", return_value=""), \
         mock.patch.object(w, "cmux_tab_title", return_value=("", "")), \
         mock.patch.object(w, "sh", return_value=subprocess.CompletedProcess([], 1, "", "")):
        invalid_inherited = w.collect(provenance_cfg, "Generous-Corp/pulp")
    context_ok = (
        absent["workstream"] == "" and absent["launcher"] == "unresolved"
        and absent["route"] == "unresolved"
        and configured["workstream"] == "SY-LF-2026-08-20"
        and configured["launcher"] == "cmux" and configured["route"] == "shipyard-daemon"
        and malformed["launcher"] == "cmux" and malformed["route"] == "unresolved"
        and inherited["workstream"] == "SY-LF-P3"
        and inherited["launcher"] == "cmux-continue-session"
        and inherited["route"] == "subrouter" and inherited["router"] == "m5"
        and invalid_inherited["route"] == "unresolved")
    if not context_ok:
        failed += 1
        print(f"FAIL  configured/inherited provenance: absent={absent!r} configured={configured!r} "
              f"malformed={malformed!r} inherited={inherited!r} invalid={invalid_inherited!r}")
    else:
        print("ok    configured/inherited provenance: explicit precedence + fail-closed absence")

    # The context policy uses Whence's existing offline-rejoin channel. Pulling
    # a newer fleet snapshot must preserve the host-local GitHub client while
    # applying the provenance block exactly once.
    with tempfile.TemporaryDirectory() as tmp:
        sync_root = pathlib.Path(tmp)
        backup = sync_root / "config-repo"
        local_config = sync_root / "config.json"
        (backup / ".git").mkdir(parents=True)
        fleet_provenance = provenance_cfg["provenance"]
        (backup / "config.json").write_text(json.dumps({
            "provenance": fleet_provenance, "labels": True,
        }))
        local_config.write_text(json.dumps({"gh": "ghapp", "labels": False}))
        with mock.patch.object(w, "BACKUP_DIR", backup), \
             mock.patch.object(w, "CONFIG_FILE", local_config), \
             mock.patch.object(w, "_git", return_value=subprocess.CompletedProcess([], 1, "", "offline")):
            failed_pull = w.pull_config()
            after_failure = json.loads(local_config.read_text())
        with mock.patch.object(w, "BACKUP_DIR", backup), \
             mock.patch.object(w, "CONFIG_FILE", local_config), \
             mock.patch.object(w, "_git", return_value=subprocess.CompletedProcess([], 0, "", "")):
            first_pull = w.pull_config()
            second_pull = w.pull_config()
        synced = json.loads(local_config.read_text())
    if (failed_pull or after_failure != {"gh": "ghapp", "labels": False}
            or not first_pull or second_pull or synced.get("gh") != "ghapp"
            or synced.get("provenance") != fleet_provenance or synced.get("labels") is not True):
        failed += 1
        print(f"FAIL  offline config rejoin: failed={failed_pull} after={after_failure!r} "
              f"first={first_pull} second={second_pull} synced={synced!r}")
    else:
        print("ok    offline config rejoin: failed pull is inert; successful pull converges once")

    stale_policy = {"labels": True, "footer": True, "gh": "gh-old"}
    fresh_policy = {"labels": False, "footer": True, "gh": "gh-new"}
    with mock.patch.object(w, "GH", "gh"), \
         mock.patch.object(w, "load_config", side_effect=[stale_policy, fresh_policy]), \
         mock.patch.object(w, "pull_config", return_value=True):
        refreshed_policy, refreshed_changed = w._config_for_sweep("")
        refreshed_gh = w.GH
    if (not refreshed_changed or refreshed_policy != fresh_policy or refreshed_gh != "gh-new"):
        failed += 1
        print(f"FAIL  refreshed sweep policy: changed={refreshed_changed} "
              f"cfg={refreshed_policy!r} gh={refreshed_gh!r}")
    else:
        print("ok    refreshed sweep policy: successful pull reloads config before stamping")

    # A launcher supplies identity and route independently. The exact values
    # survive collection and appear in the machine-readable tag plus footer.
    env = {
        "WHENCE_AGENT": "codex", "WHENCE_HOST_LABEL": "m5",
        "WHENCE_WORKSTREAM_ID": "agent-workstream-continuity-20260813",
        "WHENCE_LAUNCHER": "cmux-continue-session", "WHENCE_ROUTE": "subrouter",
        "WHENCE_ROUTER": "m3", "CMUX_SURFACE_ID": "SURFACE",
    }
    with mock.patch.dict(_os.environ, env, clear=True), \
         mock.patch.object(w, "cmux_workspace", return_value=""), \
         mock.patch.object(w, "cmux_tab_title", return_value=("Linear work #3", "")), \
         mock.patch.object(w, "sh", return_value=subprocess.CompletedProcess([], 1, "", "")):
        routed = w.collect({"denylist": [], "hide": set()})
    routed_ft = w.footer(routed, {"hide": set()}, [])
    required = {
        "agent": "codex", "workstream": "agent-workstream-continuity-20260813",
        "launcher": "cmux-continue-session", "route": "subrouter", "router": "m3",
        "origin_state": "known",
    }
    if (any(routed.get(k) != v for k, v in required.items())
            or "| **Route** | `subrouter` |" not in routed_ft
            or '"workstream": "agent-workstream-continuity-20260813"' not in routed_ft):
        failed += 1; print(f"FAIL  explicit routed provenance: {routed!r} footer={routed_ft!r}")
    else: print("ok    explicit routed provenance keeps agent separate from route/router")

    # Canonical numbered classes converge even when the old footer omitted a
    # duplicate. Unrelated labels are never touched.
    stale = w.stale_labels(
        ["1·claude", "1·codex", "2·m5", "5·direct", "bug"],
        ["1·claude"], {"1·codex", "2·m5", "5·subrouter"},
        {"order_labels": True})
    if stale != {"1·claude", "5·direct"}:
        failed += 1; print(f"FAIL  canonical label convergence: {stale}")
    else: print("ok    canonical label convergence removes duplicate/stale classes only")
    converged = w.numbered_labels_converged(
        ["1·codex", "2·m5", "5·subrouter", "bug"],
        {"1·codex", "2·m5", "5·subrouter"}, {"order_labels": True})
    duplicated = w.numbered_labels_converged(
        ["1·claude", "1·codex", "2·m5", "5·subrouter"],
        {"1·codex", "2·m5", "5·subrouter"}, {"order_labels": True})
    if not converged or duplicated:
        failed += 1; print(f"FAIL  numbered convergence verification: good={converged} duplicate={duplicated}")
    else: print("ok    numbered convergence verification rejects duplicate class members")

    # A retry may fill missing routing data, but a degraded later environment
    # must not overwrite already-known metadata from the originating launcher.
    known_route = {"tab": "Fix", "agent": "codex", "origin_state": "known",
                   "route": "subrouter", "router": "m3", "launcher": "cmux"}
    degraded_route = {"tab": "Fix", "agent": "codex", "origin_state": "lookup_failed",
                      "route": "unresolved", "router": "", "launcher": "unresolved"}
    missing_route = {**known_route, "route": "unresolved", "router": "", "launcher": "unresolved"}
    if w._prov_better(degraded_route, known_route, healcfg) or not w._prov_better(known_route, missing_route, healcfg):
        failed += 1; print("FAIL  route provenance upgrade ordering")
    else: print("ok    route provenance fills missing data but never degrades known data")

    # A backgrounded orchestrator can return before its PR exists. The live hook
    # must launch a targeted retry instead of leaving the PR to the 10-minute
    # global sweep. PR #6195 was ledgered 29 seconds before GitHub created it.
    key = "danielraffel/pulp#fix/deferred"
    rec = {"p": {f: "" for f in w.FIELDS}, "ts": 1784270822, "head": "new-head"}
    rec["p"].update({"agent": "claude", "host": "m3", "tab": "Deferred PR",
                     "workstream": "SY-LF-2026-08-20", "launcher": "cmux",
                     "route": "direct",
                     "goals": "https://github.com/acme/planning/blob/main/goal.md"})
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = pathlib.Path(tmp) / "ledger.json"
        with mock.patch.object(w, "LEDGER", ledger_path), \
             mock.patch.object(w, "_now_epoch", return_value=1):
            unlocked = dict(rec["p"], workstream="SY-LF-STALE",
                            launcher="daemon", route="queue")
            w.ledger_record("", unlocked, "danielraffel/pulp", "fix/deferred",
                            "origin/main", str(pathlib.Path.cwd()))
            recorded_key = w.ledger_record(
                "", rec["p"], "danielraffel/pulp", "fix/deferred",
                "origin/main", str(pathlib.Path.cwd()), lock_provenance=True,
            )
            conflicting = dict(rec["p"], workstream="SY-LF-WRONG",
                               launcher="daemon", route="queue")
            w.ledger_record("", conflicting, "danielraffel/pulp", "fix/deferred",
                            "origin/main", str(pathlib.Path.cwd()))
            recorded = json.loads(ledger_path.read_text())[key]
            recorded_head = recorded["head"]
            recorded_goal = recorded["p"].get("goals")
    expected_head = subprocess.run(
        ["git", "rev-parse", "origin/main"], check=True, capture_output=True, text=True
    ).stdout.strip()
    if (recorded_key != key or recorded_head != expected_head
            or recorded_goal != rec["p"]["goals"]
            or recorded["p"].get("workstream") != "SY-LF-2026-08-20"
            or recorded["p"].get("launcher") != "cmux"
            or recorded["p"].get("route") != "direct"
            or not recorded.get("provenance_locked")
            or recorded.get("revision") != 3):
        failed += 1
        print(f"FAIL  ledger capture: key={recorded_key!r} head={recorded_head!r} goal={recorded_goal!r}")
    else:
        print("ok    ledger capture: same-HEAD delayed worker cannot replace pre-exec provenance")

    # Force a pre-exec write after sweep has read the old record but before it
    # commits. Sweep may mark that same HEAD done, but it must neither stamp the
    # force-pushed branch-reuse PR nor overwrite the interleaved locked context.
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = pathlib.Path(tmp) / "ledger.json"
        sweep_key = "danielraffel/pulp#fix/reused"
        old_p = {f: "" for f in w.FIELDS}
        old_p.update({"agent": "codex", "host": "m3", "tab": "Old",
                      "origin_state": "known", "launcher": "daemon", "route": "queue"})
        ledger_path.write_text(json.dumps({sweep_key: {
            "p": old_p, "ts": 100, "head": "same-head", "provenance_locked": False,
        }}))
        locked_p = dict(old_p, tab="Launcher", workstream="SY-LF-LOCKED",
                        launcher="cmux", route="direct")
        newer_p = dict(locked_p, tab="Newer", workstream="SY-LF-NEWER")
        queried = []
        stamped = []

        def sweep_query(*args, **kwargs):
            queried.append(args)
            # Interleave a legitimate pre-exec revision after sweep's branch
            # snapshot but before its locked publication preflight.
            w.ledger_record("", locked_p, "danielraffel/pulp", "fix/reused",
                            "same-head", str(pathlib.Path.cwd()), lock_provenance=True)
            return subprocess.CompletedProcess([], 0, json.dumps([
                {"number": 40, "body": "", "headRefOid": "force-pushed-head"},
                {"number": 41, "body": "", "headRefOid": "same-head"},
            ]), "")

        def interleaved_stamp(pr, provenance, *args, **kwargs):
            stamped.append((pr, dict(provenance)))
            # Force a hostile non-locking write during the external mutation.
            # The post-mutation revision check must not mark it done.
            latest = w._load_ledger()
            latest[sweep_key] = {
                "p": newer_p, "ts": 102, "head": "same-head",
                "provenance_locked": True,
                "revision": latest[sweep_key]["revision"] + 1,
            }
            w._write_ledger(latest)

        sweep_cfg = {
            "labels": False, "footer": False, "hide": set(),
            "colors": dict(w.DEFAULT_COLORS), "label_maxlen": 24,
            "denylist": [], "redact_placeholder": "(redacted)",
        }
        with mock.patch.object(w, "LEDGER", ledger_path), \
             mock.patch.object(w, "_now_epoch", return_value=101), \
             mock.patch.object(w, "_git", return_value=subprocess.CompletedProcess([], 0, "same-head\n", "")), \
             mock.patch.object(w, "github_client_for_repo", return_value="ghapp"), \
             mock.patch.object(w, "sh", side_effect=sweep_query), \
             mock.patch.object(w, "apply_stamp", side_effect=interleaved_stamp):
            sweep_count = w.sweep(sweep_cfg)
        interleaved = json.loads(ledger_path.read_text())[sweep_key]
    query_fields = queried[0][queried[0].index("--json") + 1] if queried else ""
    stamped_pr = stamped[0][0] if stamped else ""
    stamped_p = stamped[0][1] if stamped else {}
    if (sweep_count != 1 or stamped_pr != "41" or "headRefOid" not in query_fields
            or stamped_p.get("workstream") != "SY-LF-LOCKED"
            or stamped_p.get("launcher") != "cmux" or stamped_p.get("route") != "direct"
            or not interleaved.get("provenance_locked")
            or interleaved.get("p", {}).get("workstream") != "SY-LF-NEWER"
            or interleaved.get("p", {}).get("launcher") != "cmux"
            or interleaved.get("p", {}).get("route") != "direct"
            or interleaved.get("done")):
        failed += 1
        print(f"FAIL  sweep HEAD/race guard: count={sweep_count} stamped={stamped} "
              f"query={query_fields!r} rec={interleaved!r}")
    else:
        print("ok    sweep revision fence: latest context published; newer revision not done")

    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = pathlib.Path(tmp) / "ledger.json"
        acquired = pathlib.Path(tmp) / "child-acquired"
        child_code = (
            "import fcntl,pathlib,sys; "
            "f=open(sys.argv[1],'a+'); fcntl.flock(f.fileno(),fcntl.LOCK_EX); "
            "pathlib.Path(sys.argv[2]).write_text('acquired')"
        )
        with mock.patch.object(w, "LEDGER", ledger_path):
            with w._ledger_lock():
                child = subprocess.Popen(
                    [sys.executable, "-c", child_code,
                     str(ledger_path) + ".lock", str(acquired)]
                )
                w.time.sleep(0.1)
                child_blocked = child.poll() is None and not acquired.exists()
            child.wait(timeout=5)
        child_completed = child.returncode == 0 and acquired.exists()
    if not child_blocked or not child_completed:
        failed += 1
        print(f"FAIL  process ledger lock: blocked={child_blocked} "
              f"completed={child_completed} rc={child.returncode}")
    else:
        print("ok    process ledger lock: concurrent writer blocks until atomic update completes")

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
        response = next(responses)
        if len(queries) == 2:
            latest = w._load_ledger()
            latest[key] = {**latest[key], "p": rec["p"], "revision": 2,
                           "provenance_locked": True}
            w._write_ledger(latest)
        return response
    publication_cfg = {
        "labels": False, "footer": True, "hide": set(),
        "colors": dict(w.DEFAULT_COLORS), "label_maxlen": 24,
        "denylist": [], "redact_placeholder": "(redacted)",
    }
    with tempfile.TemporaryDirectory() as tmp:
        retry_ledger = pathlib.Path(tmp) / "ledger.json"
        retry_rec = json.loads(json.dumps(rec))
        retry_rec.update({"revision": 1, "provenance_locked": False})
        retry_rec["p"].update({"workstream": "SY-LF-OLD", "launcher": "daemon", "route": "queue"})
        retry_ledger.write_text(json.dumps({key: retry_rec}))
        with mock.patch.object(w, "LEDGER", retry_ledger), \
             mock.patch.object(w, "sh", side_effect=fake_pr_list), \
             mock.patch.object(w, "apply_stamp", side_effect=lambda *a, **k: applied.append((a, k))), \
             mock.patch.object(w.time, "sleep") as sleep:
            rc = w.retry_pending_pr(key, publication_cfg, attempts=2, delay=0.01)
    policy_kept = (len(applied) == 1 and applied[0][0][0] == "6195"
                   and applied[0][0][1].get("workstream") == "SY-LF-2026-08-20"
                   and applied[0][0][1].get("launcher") == "cmux"
                   and applied[0][0][1].get("route") == "direct"
                   and applied[0][0][3:5] == (False, True))
    timeouts_bounded = all(0 < q[1].get("timeout", 0) <= 5 for q in queries)
    if rc != 0 or not policy_kept or not timeouts_bounded or applied[0][1].get("repo") != "danielraffel/pulp" or sleep.call_count != 1:
        failed += 1
        print(f"FAIL  deferred retry: rc={rc} applied={applied} sleeps={sleep.call_count}")
    else:
        print("ok    deferred retry fence: exact HEAD uses latest same-HEAD revision")

    empty = subprocess.CompletedProcess([], 0, "[]", "")
    with tempfile.TemporaryDirectory() as tmp:
        deadline_ledger = pathlib.Path(tmp) / "ledger.json"
        deadline_ledger.write_text(json.dumps({key: rec}))
        with mock.patch.object(w, "LEDGER", deadline_ledger), \
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
                  and pre_rec.get("provenance_locked") is True
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
            "  printf '%s|%s|%s\\n' \"$WHENCE_WORKSTREAM_ID\" \"$WHENCE_LAUNCHER\" \"$WHENCE_ROUTE\" "
            "> \"$WHENCE_FAKE_STATE/context\"\n"
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
        # Isolate zsh startup from the machine's real ~/.zshenv. Once Whence is
        # installed, that file sources the live hook and may rewrite PATH ahead
        # of our fake binaries, turning this hermetic lifecycle test into a real
        # network sweep (and a timeout). ZDOTDIR keeps the production hook out;
        # the generated hook under test is sourced explicitly below.
        env.update({"WHENCE_FAKE_STATE": str(state), "ZDOTDIR": str(root)})
        late_path = f'PATH="{bindir}:$PATH"'
        driven = subprocess.run(
            ["zsh", "-fc", f'source "{hook_file}"; {late_path}; '
             'WHENCE_LAUNCHER=cmux WHENCE_ROUTE=direct '
             'shipyard pr --workstream-id SY-LF-2026-08-20'],
            env=env, capture_output=True, text=True, timeout=5,
        )
        lifecycle_ok = (driven.returncode == 0 and (state / "preexec").exists()
                        and (state / "started").exists() and (state / "stamped").exists()
                        and (state / "swept").exists()
                        and not (state / "recollected").exists()
                        and (state / "context").read_text().strip()
                        == "SY-LF-2026-08-20|cmux|direct")
        if not lifecycle_ok:
            failed += 1
            print(f"FAIL  long-running wrapper: rc={driven.returncode} out={driven.stdout!r} err={driven.stderr!r}")
        else:
            print("ok    long-running wrapper: pre-exec stamp kept; post path sweeps ledger")
        for name in ("preexec", "stamped", "pr-created", "started", "swept", "recollected", "context"):
            try: (state / name).unlink()
            except FileNotFoundError: pass
        help_run = subprocess.run(
            ["zsh", "-fc", f'source "{hook_file}"; {late_path}; shipyard pr --help'],
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
        for name in ("help", "preexec", "stamped", "pr-created", "started", "swept", "recollected"):
            try: (state / name).unlink()
            except FileNotFoundError: pass
        bash_run = subprocess.run(
            ["bash", "--noprofile", "--norc", "-c",
             f'source "{hook_file}"; {late_path}; shipyard pr'],
            env=env, capture_output=True, text=True, timeout=5,
        )
        bash_ok = (bash_run.returncode == 0 and (state / "preexec").exists()
                   and (state / "stamped").exists() and (state / "swept").exists()
                   and not (state / "recollected").exists())
        if not bash_ok:
            failed += 1
            print(f"FAIL  bash wrapper: rc={bash_run.returncode} state={[p.name for p in state.iterdir()]}")
        else:
            print("ok    shell wrapper: absolute-path bypass works in zsh and bash")

    print(f"\n{'ALL PASS' if not failed else f'{failed} FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
