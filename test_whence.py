#!/usr/bin/env python3
"""Tests for the attribution whence can't afford to get wrong.

`parse_outcome` is the load-bearing piece: it reads the repo/branch/PR out of a
command's own OUTPUT, which is the only cwd-independent signal available to an
agent hook. Everything else (which PR gets stamped, which ledger key a push files
under) rides on it, and a miss is SILENT — the PR just never gets a label. So the
real-world output shapes are pinned here.

Run: python3 test_whence.py
"""
import datetime
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
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


def self_heal_checks() -> int:
    """Each check drives self_heal_agent_hooks against real temp dirs."""
    import io, threading
    bad = []

    def check(name, ok, detail=""):
        if ok:
            print(f"ok    self-heal: {name}")
        else:
            bad.append(name)
            print(f"FAIL  self-heal: {name} {detail}")

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(os.path.realpath(tmp))
        home, cfgdir = root / "home", root / "home" / ".config" / "whence"
        cfgdir.mkdir(parents=True)
        (home / ".claude").mkdir()
        pr_hook = cfgdir / "pr-hook.sh"
        pr_hook.write_text("#!/bin/sh\n")

        def proxy(name, settings=None, marker=".claude.json"):
            d = home / ".router" / "claude-proxy" / name
            d.mkdir(parents=True)
            (d / marker).write_text("{}")
            if settings is not None:
                (d / "settings.json").write_text(settings)
            return d

        def run(env, cfg=None, top=""):
            notes = io.StringIO()
            full = {"HOME": str(home), **env}
            patches = [mock.patch.object(w, "CFG_DIR", cfgdir),
                       mock.patch.object(w, "PR_HOOK", pr_hook),
                       mock.patch.object(w, "SELF_HEAL_STATE", cfgdir / "self-heal.json"),
                       mock.patch.object(w, "SELF_HEAL_LOG", cfgdir / "self-heal.jsonl"),
                       mock.patch.dict(os.environ, full, clear=True),
                       mock.patch("sys.stderr", notes)]
            for pt in patches: pt.start()
            try:
                return w.self_heal_agent_hooks(cfg or {}, top), notes.getvalue()
            finally:
                for pt in reversed(patches): pt.stop()

        def claude(d, **extra):
            return {"CLAUDECODE": "1", "CLAUDE_CONFIG_DIR": str(d), **extra}

        def wired(f):
            return w._hook_wired(json.loads(f.read_text()))

        # 1. unwired dir: installed once, unknown keys + mode kept; second run is a
        #    pure cache hit: no write, no process spawn.
        d = proxy("fresh", json.dumps({"model": "x", "permissions": {"allow": ["Bash(ls)"]},
                                       "hooks": {"Stop": [{"hooks": []}]}}))
        f = d / "settings.json"
        f.chmod(0o600)
        ino0 = f.stat().st_ino
        r1, n1 = run(claude(d))
        doc = json.loads(f.read_text())
        check("unwired dir is wired once by atomic rename, keys and mode kept",
              r1[0]["action"] == "installed" and wired(f) and doc["model"] == "x"
              and doc["permissions"] == {"allow": ["Bash(ls)"]} and "Stop" in doc["hooks"]
              and (f.stat().st_mode & 0o777) == 0o600 and f.stat().st_ino != ino0
              and n1.count("\n") == 1
              and "wired the claude PR hook" in n1
              and not list(d.glob(".settings.json.whence-*")), f"{r1} {n1!r}")
        sig = w._file_sig(f)
        spawned = []
        real_popen = subprocess.Popen
        def popen(*a, **k):
            spawned.append(a)
            return real_popen(*a, **k)
        t0 = time.perf_counter()
        with mock.patch.object(subprocess, "Popen", popen):
            hits = [run(claude(d)) for _ in range(50)]
        per_run = (time.perf_counter() - t0) / 50
        check(f"steady state is a cache hit: no write, no spawn ({per_run*1e3:.2f} ms/run)",
              all(r[0]["cache_hit"] and r[0]["action"] == "none" and not n for r, n in hits)
              and w._file_sig(f) == sig and not spawned, f"{hits[0]} spawned={spawned}")

        # 2. a dir that is already wired is never written.
        d2 = proxy("wired", json.dumps({"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "/elsewhere/pr-hook.sh"}]}]}}))
        sig2 = w._file_sig(d2 / "settings.json")
        r2, n2 = run(claude(d2))
        check("wired dir untouched", r2[0]["verdict"] == "wired" and not n2
              and w._file_sig(d2 / "settings.json") == sig2, f"{r2}")

        # 3. skipped: not an agent session, never opted in, .whence-off, opt-outs.
        d3 = proxy("skips", "{}")
        f3, sig3 = d3 / "settings.json", w._file_sig(d3 / "settings.json")
        repo = root / "repo"; repo.mkdir(); (repo / ".whence-off").write_text("")
        r_shell, _ = run({"CLAUDE_CONFIG_DIR": str(d3)})
        r_off, _ = run(claude(d3), top=str(repo))
        r_env, _ = run(claude(d3, WHENCE_AUTOINSTALL="0"))
        r_cfg, _ = run(claude(d3), cfg={"agent_hook_autoinstall": False})
        d3b = proxy("nohooks", json.dumps({"disableAllHooks": True}))
        sig3b = w._file_sig(d3b / "settings.json")
        r_dis, _ = run(claude(d3b))
        pr_hook.rename(cfgdir / "pr-hook.off")
        r_noopt, _ = run(claude(d3))
        (cfgdir / "pr-hook.off").rename(pr_hook)
        check("skipped outside an agent, without opt-in, in a .whence-off repo, when opted out or hooks are disabled",
              r_shell == [] and r_off == [] and r_noopt == []
              and r_env[0]["verdict"] == r_cfg[0]["verdict"] == "opted-out"
              and r_dis[0]["verdict"] == "opted-out" and w._file_sig(d3b / "settings.json") == sig3b
              and w._file_sig(f3) == sig3, f"{r_shell} {r_off} {r_env} {r_cfg} {r_noopt} {r_dis}")

        # 4. malformed settings: never overwritten, one note, then silent.
        d4 = proxy("broken", '{"model": "x", ')
        before = (d4 / "settings.json").read_bytes()
        r4, n4 = run(claude(d4))
        r4b, n4b = run(claude(d4))
        check("malformed settings.json left untouched with one note",
              r4[0]["verdict"] == "malformed" and (d4 / "settings.json").read_bytes() == before
              and n4.count("\n") == 1 and "malformed" in n4 and r4b[0]["cache_hit"] and not n4b,
              f"{r4} {n4!r} {r4b} {n4b!r}")

        # 5. owner saves between whence's read and its rename: whence drops its write.
        d5 = proxy("race", json.dumps({"model": "x"}))
        f5 = d5 / "settings.json"
        real_doc = w._hook_doc
        def racing(target):
            out = real_doc(target)
            if pathlib.Path(target) == f5:
                f5.write_text(json.dumps({"model": "owner-saved", "extra": 1}))
            return out
        with mock.patch.object(w, "_hook_doc", racing):
            r5, _ = run(claude(d5))
        check("a save made during the write wins; whence retries later",
              r5[0]["verdict"] == "changed"
              and json.loads(f5.read_text()) == {"model": "owner-saved", "extra": 1}
              and not list(d5.glob(".settings.json.whence-*")), f"{r5}")
        r5b, _ = run(claude(d5))
        check("the retry then wires it", r5b[0]["action"] == "installed" and wired(f5)
              and json.loads(f5.read_text())["extra"] == 1, f"{r5b}")

        # 6. concurrent whence runs + the owner rewriting: the file always parses.
        d6 = proxy("hammer", json.dumps({"n": 0}))
        f6 = d6 / "settings.json"
        stop, errors = threading.Event(), []
        def owner():
            i = 0
            while not stop.is_set():
                i += 1
                cur = json.loads(f6.read_text())
                cur["n"] = i
                tmpf = d6 / f".owner-{i}"
                tmpf.write_text(json.dumps(cur)); os.replace(tmpf, f6)
        def healer():
            for _ in range(40):
                try:
                    w._wire_posttooluse(f6, "Bash", {}, only_if_missing=True,
                                        follow_symlinks=False)
                except w.HookFileError as e:
                    if e.kind != "changed": errors.append(e.kind)
                except Exception as e:
                    errors.append(repr(e))
        with mock.patch.object(w, "PR_HOOK", pr_hook):
            th = [threading.Thread(target=owner)] + [threading.Thread(target=healer) for _ in range(6)]
            for t in th: t.start()
            for t in th[1:]: t.join()
            stop.set(); th[0].join()
        final = json.loads(f6.read_text())
        entries = [x for e in final.get("hooks", {}).get("PostToolUse", [])
                   for x in e["hooks"] if x["command"].endswith("pr-hook.sh")]
        check("concurrent writers never corrupt the file",
              not errors and isinstance(final.get("n"), int) and len(entries) <= 1
              and not list(d6.glob(".settings.json.whence-*")), f"{errors} {final}")

        # 7. safety: symlinked file, symlinked dir, outside $HOME, no agent files.
        target = root / "dotfiles-settings.json"; target.write_text("{}")
        d7 = proxy("linkfile")
        (d7 / "settings.json").symlink_to(target)
        r7, _ = run(claude(d7))
        linkdir = home / ".router" / "claude-proxy" / "linkdir"
        linkdir.symlink_to(d3)
        r7b, _ = run(claude(linkdir))
        outside = root / "outside"; outside.mkdir(); (outside / ".claude.json").write_text("{}")
        r7c, _ = run(claude(outside))
        outside_untouched = not (outside / "settings.json").exists()
        r7d, _ = run(claude(outside, WHENCE_CLAUDE_CONFIG_DIRS=str(root / "out*")))
        bare = home / "not-a-config"; bare.mkdir()
        r7e, _ = run(claude(bare))
        check("never writes through a symlink, outside a recognized dir, or into a non-config dir",
              r7[0]["verdict"] == "refused" and (d7 / "settings.json").is_symlink()
              and target.read_text() == "{}"
              and r7b[0].get("why") == "symlinked dir" and w._file_sig(f3) == sig3
              and r7c[0].get("why", "").startswith("outside") and outside_untouched
              and r7d[0]["action"] == "installed"
              and r7e[0].get("why") == "no claude config files" and not (bare / "settings.json").exists(),
              f"{r7} {r7b} {r7c} {r7d} {r7e}")

        # 8. a hook the user removed stays removed until an explicit install.
        doc = json.loads(f.read_text()); doc["hooks"].pop("PostToolUse"); f.write_text(json.dumps(doc))
        r8, n8 = run(claude(d))
        r8b, n8b = run(claude(d))
        with mock.patch.object(w, "CFG_DIR", cfgdir), mock.patch.object(w, "PR_HOOK", pr_hook), \
             mock.patch.object(w, "SELF_HEAL_STATE", cfgdir / "self-heal.json"), \
             mock.patch.object(w, "_write_pr_hook", lambda: pr_hook), \
             mock.patch.object(w, "claude_settings_files", lambda: [f]), \
             mock.patch("sys.stdout", io.StringIO()):
            w.install_agent_hook("claude")
        r8c, _ = run(claude(d))
        check("a removed hook is an opt-out until --install-agent-hook",
              r8[0]["verdict"] == "removed" and "removed" in n8 and r8b[0]["cache_hit"] and not n8b
              and r8c[0]["verdict"] == "wired", f"{r8} {r8b} {r8c}")

        # 9. read-only config dir: one note, no retry storm, file unchanged.
        d9 = proxy("readonly", "{}")
        d9.chmod(0o555)
        try:
            r9, n9 = run(claude(d9))
            r9b, n9b = run(claude(d9))
        finally:
            d9.chmod(0o755)
        check("read-only dir fails once with a note, then backs off",
              r9[0]["verdict"] == "failed" and n9.count("\n") == 1 and r9b[0]["cache_hit"]
              and not n9b and (d9 / "settings.json").read_text() == "{}", f"{r9} {n9!r} {r9b}")

        # 10. Codex: its own home, matcher and timeout.
        ch = home / "codex-alt"; ch.mkdir(); (ch / "config.toml").write_text("")
        r10, _ = run({"CODEX_THREAD_ID": "t1", "CODEX_HOME": str(ch)})
        hj = json.loads((ch / "hooks.json").read_text()) if (ch / "hooks.json").exists() else {}
        e10 = (hj.get("hooks", {}).get("PostToolUse") or [{}])[0]
        check("codex session wires $CODEX_HOME/hooks.json",
              r10[0]["action"] == "installed" and e10.get("matcher", "").startswith("Bash|Shell")
              and e10["hooks"][0].get("timeout") == 20000, f"{r10} {hj}")

        # 11. an internal error becomes one note; nothing propagates.
        with mock.patch.object(w, "_session_agent_dirs", side_effect=RuntimeError("boom")):
            r11, n11 = run(claude(d))
        check("internal error is swallowed with one note",
              r11[0]["verdict"] == "error" and n11.count("\n") == 1 and "boom" in n11, f"{r11} {n11!r}")

        # 12. the explicit install refuses a malformed file too (it used to clobber it).
        before = (d4 / "settings.json").read_bytes()
        out = io.StringIO()
        with mock.patch.object(w, "CFG_DIR", cfgdir), mock.patch.object(w, "PR_HOOK", pr_hook), \
             mock.patch.object(w, "SELF_HEAL_STATE", cfgdir / "self-heal.json"), \
             mock.patch.object(w, "_write_pr_hook", lambda: pr_hook), \
             mock.patch.object(w, "claude_settings_files", lambda: [d4 / "settings.json"]), \
             mock.patch("sys.stdout", out):
            rc12 = w.install_agent_hook("claude")
        check("--install-agent-hook never overwrites a malformed file",
              rc12 == 1 and (d4 / "settings.json").read_bytes() == before
              and "NOT wired" in out.getvalue(), f"rc={rc12} {out.getvalue()!r}")

    # 14. the real entry points reach it: --pre-exec (via preexec_capture) and the
    #     CLI diagnostic, run as a fresh process with its own HOME.
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(os.path.realpath(tmp))
        repo = root / "repo"; repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                        "https://github.com/o/r.git"], check=True)
        calls = []
        with mock.patch.object(w, "self_heal_agent_hooks", lambda cfg, top="": calls.append(top)), \
             mock.patch.object(w, "collect", return_value={}), \
             mock.patch.object(w, "sanitize_path", return_value=""), \
             mock.patch.object(w, "ledger_record", return_value=""):
            w.preexec_capture(str(repo), {"repos": {}}, True, True)
        home = root / "home"; (home / ".config" / "whence").mkdir(parents=True)
        (home / ".config" / "whence" / "pr-hook.sh").write_text("#!/bin/sh\n")
        cdir = home / ".claude"; cdir.mkdir(); (cdir / "projects").mkdir()
        env = {"HOME": str(home), "PATH": os.environ["PATH"], "CLAUDECODE": "1"}
        script = pathlib.Path(__file__).parent / "whence"
        outs = [subprocess.run([sys.executable, str(script), "--self-heal", str(repo)], env=env,
                               capture_output=True, text=True, timeout=30) for _ in range(2)]
        res = [json.loads(o.stdout or "[]") for o in outs]
        # --auto (the post-command wrapper and the agent hook) heals too, and a
        # PR lookup that finds nothing still exits 0.
        cdir2 = home / "proxy-b"; cdir2.mkdir(); (cdir2 / ".claude.json").write_text("{}")
        fake_gh = root / "fake-gh"; fake_gh.write_text("#!/bin/sh\nexit 1\n"); fake_gh.chmod(0o755)
        auto = subprocess.run([sys.executable, str(script), "--auto"], cwd=str(repo),
                              env={**env, "CLAUDE_CONFIG_DIR": str(cdir2), "WHENCE_GH": str(fake_gh)},
                              capture_output=True, text=True, timeout=60)
        auto_ok = (auto.returncode == 0 and (cdir2 / "settings.json").exists()
                   and w._hook_wired(json.loads((cdir2 / "settings.json").read_text())))
        check("--pre-exec, --auto and the CLI run the self-heal (install, then cache hit)",
              calls == [os.path.realpath(repo)]
              and res[0] and res[0][0]["action"] == "installed" and "wired the claude" in outs[0].stderr
              and res[1] and res[1][0]["cache_hit"] and not outs[1].stderr
              and w._hook_wired(json.loads((cdir / "settings.json").read_text())) and auto_ok,
              f"calls={calls} res={res} err={[o.stderr for o in outs]} auto={auto.returncode} {auto.stderr[-300:]!r}")

    # 13. the shell wrapper: the notice reaches the caller's stderr, whence's own
    #     noise does not, and the wrapped command's exit status survives a failing whence.
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        bindir = root / "bin"; bindir.mkdir()
        (bindir / "whence").write_text(
            '#!/bin/sh\n[ -n "$WHENCE_NOTICE_FD" ] && eval "echo NOTICE >&$WHENCE_NOTICE_FD"\n'
            'echo noise >&2\nexit 3\n')
        (bindir / "gh").write_text('#!/bin/sh\nexit "${FAKE_RC:-0}"\n')
        (bindir / "git").write_text('#!/bin/sh\nexit "${FAKE_RC:-0}"\n')
        for f in bindir.iterdir(): f.chmod(0o755)
        hook_file = root / "hook.sh"; hook_file.write_text(w._hook_file_text()[0])
        env = {**os.environ, "ZDOTDIR": str(root), "PATH": f"{bindir}:/usr/bin:/bin", "FAKE_RC": "7"}
        runs = []
        for shell in (["zsh", "-fc"], ["bash", "--noprofile", "--norc", "-c"]):
            for cmd in ("gh pr create --fill", "git push"):
                r = subprocess.run([*shell, f'. "{hook_file}"; {cmd}'], env=env,
                                   capture_output=True, text=True, timeout=10)
                runs.append((shell[0], cmd, r.returncode, r.stderr))
        check("wrapper keeps the exit status and shows only the notice line",
              all(rc == 7 and err.strip() == "NOTICE" for _, _, rc, err in runs), f"{runs}")
    return 1 if bad else 0


def main() -> int:
    failed = 0

    # A timeout owns the complete subprocess tree. Plant a TERM-resistant
    # grandchild that inherits Whence's capture pipes: the helper must return on
    # schedule and leave no process behind. A normal command is the positive
    # control proving the same instrument can report success.
    control = w.sh("sh", "-c", "printf control", timeout=1)
    with tempfile.TemporaryDirectory() as tmp:
        child_pid = pathlib.Path(tmp) / "child.pid"
        script = (
            "sh -c 'trap \"\" HUP TERM; echo $$ > \"$1\"; "
            "while :; do sleep 1; done' sh \"$1\" & wait"
        )
        started = time.monotonic()
        timed = w.sh("sh", "-c", script, "sh", str(child_pid), timeout=0.1)
        elapsed = time.monotonic() - started
        planted_pid = int(child_pid.read_text()) if child_pid.exists() else 0
        alive = False
        for _ in range(20):
            alive = bool(planted_pid and subprocess.run(
                ["ps", "-p", str(planted_pid)], capture_output=True).returncode == 0)
            if not alive:
                break
            time.sleep(0.025)
    if (control.returncode != 0 or control.stdout != "control"
            or timed.returncode != 124 or elapsed > 1.0 or alive):
        failed += 1
        print(f"FAIL  process-tree timeout: control={control!r} timed={timed!r} "
              f"elapsed={elapsed:.3f} child_alive={alive}")
    else:
        print("ok    process-tree timeout: TERM/KILL reaps a pipe-holding grandchild")

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
            w.github_call(app, "api", "--method", "POST",
                          "repos/danielraffel/whence/issues/24/labels",
                          repo="danielraffel/whence")
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
            app, "api", "--method", "POST",
            "repos/danielraffel/pulp-planning/issues/24/labels",
            "-f", "labels[]=1·codex", repo="danielraffel/pulp-planning")
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

    # Shipyard's privileged ghapp grammar permits `api`, not `label create` or
    # `pr edit`. Exercise the complete provenance write path twice against a
    # stateful fake: every mutation must use the supported API surface and the
    # second application must perform no writes.
    class StatefulGitHub:
        def __init__(self):
            self.repo_labels = {"1·codex": "FFFFFF"}
            self.issue_labels = {"1·claude"}
            self.body = "<sub>stamped forged</sub>\nInitial body\n"
            self.mutations = []
            self.calls = []

        def __call__(self, *args, **kwargs):
            argv = list(args[1:])
            self.calls.append(tuple(argv))
            if argv[:2] == ["repo", "view"]:
                return subprocess.CompletedProcess(args, 0, "danielraffel/whence\n", "")
            if argv[:2] == ["pr", "view"]:
                if "body" in argv:
                    return subprocess.CompletedProcess(args, 0, self.body, "")
                return subprocess.CompletedProcess(
                    args, 0, "\n".join(sorted(self.issue_labels)), "")
            if argv and argv[0] == "api":
                endpoint = next((x for x in argv if x.startswith("repos/")), "")
                if "--paginate" in argv:
                    payload = [{"name": n, "color": c}
                               for n, c in sorted(self.repo_labels.items())]
                    return subprocess.CompletedProcess(args, 0, json.dumps([payload]), "")
                method = argv[argv.index("--method") + 1]
                self.mutations.append(tuple(argv))
                fields = [argv[i + 1] for i, value in enumerate(argv[:-1]) if value == "-f"]
                if method == "POST" and endpoint == "repos/danielraffel/whence/labels":
                    values = dict(field.split("=", 1) for field in fields)
                    self.repo_labels[values["name"]] = values["color"]
                elif method == "POST" and endpoint.endswith("/issues/24/labels"):
                    self.issue_labels.update(
                        field.split("=", 1)[1] for field in fields
                        if field.startswith("labels[]="))
                elif method == "DELETE" and "/issues/24/labels/" in endpoint:
                    self.issue_labels.discard(w.urllib.parse.unquote(endpoint.rsplit("/", 1)[1]))
                elif method == "PATCH" and "/labels/" in endpoint:
                    old_name = w.urllib.parse.unquote(endpoint.rsplit("/", 1)[1])
                    values = dict(field.split("=", 1) for field in fields)
                    self.repo_labels.pop(old_name, None)
                    self.repo_labels[values["new_name"]] = values["color"]
                elif method == "PATCH" and endpoint.endswith("/issues/24"):
                    body_file = argv[argv.index("--input") + 1]
                    payload = pathlib.Path(body_file).read_bytes().decode("utf-8")
                    self.body = json.loads(payload)["body"]
                else:
                    return subprocess.CompletedProcess(args, 1, "", f"unexpected API call: {argv!r}")
                return subprocess.CompletedProcess(args, 0, "{}", "")
            return subprocess.CompletedProcess(args, 1, "", f"unsupported call: {argv!r}")

    stateful = StatefulGitHub()
    stamp_cfg = {
        "hide": set(), "colors": w.DEFAULT_COLORS, "label_maxlen": 24,
        "order_labels": True, "audit_log": False,
    }
    stamp_prov = {field: "" for field in w.FIELDS}
    stamp_prov.update({"agent": "codex", "host": "m5", "stamped": "now"})
    with mock.patch.dict(w.os.environ, {"WHENCE_GH": app}, clear=True), \
         mock.patch.object(w, "GH", app), mock.patch.object(w, "sh", side_effect=stateful):
        first_names = w.apply_stamp(
            "24", stamp_prov, stamp_cfg, True, True,
            repo="danielraffel/whence", source="test")
        first_mutation_count = len(stateful.mutations)
        first_body = stateful.body
        stamp_prov["stamped"] = "later"
        second_names = w.apply_stamp(
            "24", stamp_prov, stamp_cfg, True, True,
            repo="", source="test")
        replay_mutation_count = len(stateful.mutations)
        replay_body = stateful.body
        stamp_prov.update({"agent": "claude", "stamped": "later"})
        third_names = w.apply_stamp(
            "24", stamp_prov, stamp_cfg, True, True,
            repo="danielraffel/whence", source="test")
    supported_grammar_only = all(
        call[0] == "api" or call[:2] in (("pr", "view"), ("repo", "view"))
        for call in stateful.calls)
    if (first_names != ["1·codex", "2·m5"] or second_names != first_names
            or third_names != ["1·claude", "2·m5"]
            or stateful.issue_labels != set(third_names)
            or stateful.repo_labels != {"1·codex": "1f6feb", "1·claude": "1f6feb",
                                        "2·m5": "1a7f37"}
            or w.prior_stamp(first_body) != "now" or w.prior_stamp(stateful.body) != "later"
            or replay_body != first_body or replay_mutation_count != first_mutation_count
            or len(stateful.mutations) == replay_mutation_count
            or not supported_grammar_only):
        failed += 1
        print(f"FAIL  ghapp provenance API: names={first_names!r}/{second_names!r} "
              f"labels={stateful.issue_labels!r} repo_labels={stateful.repo_labels!r} "
              f"mutations={stateful.mutations!r}")
    else:
        print("ok    ghapp provenance API covers create/recolor/remove/body/resolve and replay")

    label_pages = lambda labels: subprocess.CompletedProcess(
        [], 0, json.dumps([[{"name": n, "color": c} for n, c in labels.items()]]), "")
    with mock.patch.object(w, "github_call", side_effect=[
            label_pages({}), RuntimeError("concurrent create"),
            label_pages({"1·codex": "1f6feb"})]):
        try:
            w.ensure_repo_labels(app, "danielraffel/whence", [("1·codex", "1F6FEB")])
        except RuntimeError:
            create_race_converged = False
        else:
            create_race_converged = True
    with mock.patch.object(w, "github_call", side_effect=[
            RuntimeError("concurrent removal"),
            subprocess.CompletedProcess([], 0, "", "")]):
        try:
            w.remove_issue_label(app, "danielraffel/whence", "24", "1·claude")
        except RuntimeError:
            remove_race_converged = False
        else:
            remove_race_converged = True
    with mock.patch.object(w, "github_call", side_effect=[
            RuntimeError("permission denied"),
            subprocess.CompletedProcess([], 0, "1·claude\n", "")]):
        try:
            w.remove_issue_label(app, "danielraffel/whence", "24", "1·claude")
        except RuntimeError:
            removal_denial_failed = True
        else:
            removal_denial_failed = False
    if not create_race_converged or not remove_race_converged or not removal_denial_failed:
        failed += 1
        print(f"FAIL  API race fences: create={create_race_converged} "
              f"remove={remove_race_converged} denial={removal_denial_failed}")
    else:
        print("ok    API races converge only after authoritative state readback")

    with mock.patch.object(
            w, "github_call",
            return_value=subprocess.CompletedProcess([], 0, "not-json", "")):
        try:
            w.ensure_repo_labels(app, "danielraffel/whence", [("1·codex", "1f6feb")])
        except RuntimeError:
            malformed_labels_failed = True
        else:
            malformed_labels_failed = False
    if not malformed_labels_failed:
        failed += 1
        print("FAIL  malformed repository-label read did not fail closed")
    else:
        print("ok    malformed repository-label read fails closed before mutation")

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
            ("portable recovery context",
             "WHENCE_AGENT=qwen WHENCE_TERMINAL_RUNTIME=herdr WHENCE_TERMINAL_ADDRESS=pane:42 "
             "WHENCE_TERMINAL_INSTANCE=herdr-run-8f7c "
             "WHENCE_TERMINAL_WORKSPACE='Fix queue' WHENCE_TERMINAL_TAB='Fix queue' "
             "WHENCE_SESSION_ID=qwen-session-42 "
             "WHENCE_RESUME_COMMAND='qwen resume qwen-session-42' "
             "WHENCE_RELAUNCH_COMMAND='herdr attach pane:42' shipyard pr",
             {"agent": "qwen", "terminal": "herdr", "terminal_address": "pane:42",
              "terminal_instance": "herdr-run-8f7c",
              "workspace": "Fix queue", "tab": "Fix queue",
              "session": "qwen-session-42", "resume": "qwen resume qwen-session-42",
              "relaunch": "herdr attach pane:42"}),
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

    same_terminal_move = w._prov_better(
        {"tab": "New pane name", "workspace": "w2", "agent": "qwen",
         "origin_state": "known", "terminal": "herdr", "terminal_instance": "herdr-run-1",
         "session": "qwen-1"},
        {"tab": "Old pane name", "workspace": "w1", "agent": "qwen",
         "origin_state": "known", "terminal": "herdr", "terminal_instance": "herdr-run-1",
         "session": "qwen-1"},
        healcfg)
    different_terminal_name_match = w._prov_better(
        {"tab": "Same name", "workspace": "", "agent": "qwen",
         "origin_state": "known", "terminal": "herdr", "terminal_instance": "herdr-run-2",
         "session": "qwen-2"},
        {"tab": "Same name", "workspace": "", "agent": "qwen",
         "origin_state": "known", "terminal": "herdr", "terminal_instance": "herdr-run-1",
         "session": "qwen-1"},
        healcfg)
    hidden_display_move = w._prov_better(
        {"tab": "New", "workspace": "w2", "agent": "qwen", "origin_state": "known",
         "terminal": "herdr", "terminal_instance": "herdr-run-1", "session": "qwen-1"},
        {"agent": "qwen", "origin_state": "known", "terminal": "herdr",
         "terminal_instance": "herdr-run-1", "session": "qwen-1"},
        {**healcfg, "hide": {"tab", "workspace"}})
    reused_address_without_instance = w._prov_better(
        {"tab": "New", "agent": "qwen", "origin_state": "known", "terminal": "herdr",
         "terminal_address": "pane:42", "terminal_instance": ""},
        {"tab": "Old", "agent": "qwen", "origin_state": "known", "terminal": "herdr",
         "terminal_address": "pane:42", "terminal_instance": ""}, healcfg)
    degraded_same_instance = w._prov_better(
        {"tab": "", "workspace": "", "agent": "qwen", "origin_state": "lookup_failed",
         "terminal": "herdr", "terminal_instance": "herdr-run-1", "session": "qwen-1"},
        {"tab": "Useful name", "workspace": "w1", "agent": "qwen", "origin_state": "known",
         "terminal": "herdr", "terminal_instance": "herdr-run-1", "session": "qwen-1"}, healcfg)
    different_native_session = w._prov_better(
        {"tab": "New", "agent": "qwen", "origin_state": "known", "terminal": "herdr",
         "terminal_instance": "herdr-run-1", "session": "qwen-2"},
        {"tab": "Old", "agent": "qwen", "origin_state": "known", "terminal": "herdr",
         "terminal_instance": "herdr-run-1", "session": "qwen-1"}, healcfg)
    if (not same_terminal_move or different_terminal_name_match or hidden_display_move
            or reused_address_without_instance or degraded_same_instance
            or different_native_session):
        failed += 1
        print(f"FAIL  terminal identity healing: same={same_terminal_move} "
              f"different={different_terminal_name_match} hidden={hidden_display_move} "
              f"reused_address={reused_address_without_instance} degraded={degraded_same_instance} "
              f"different_session={different_native_session}")
    else:
        print("ok    terminal identity heals same-instance display moves, never similar-name identity")

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

    # Explicit recovery commands cross the public PR boundary. Preserve simple
    # argv exactly, but reject shell syntax, paths, endpoints, and credentials.
    recovery_commands = [
        ("qwen resume qwen-session-42", "qwen resume qwen-session-42"),
        ("herdr attach pane:42", "herdr attach pane:42"),
        ("qwen resume s; curl bad.invalid", ""),
        ("/private/bin/qwen resume s", ""),
        ("qwen --api-key hunter2 resume s", ""),
        ("env TOKEN=hunter2 qwen resume s", ""),
        ("qwen resume https://router.invalid/s", ""),
    ]
    for src, want in recovery_commands:
        got = w.safe_public_command(src)
        if got != want:
            failed += 1; print(f"FAIL  safe_public_command({src!r})={got!r} want={want!r}")
        else: print(f"ok    safe_public_command({src!r}) -> {got!r}")

    if (w.terminal_runtime("cmux") != "cmux" or w.terminal_runtime("herdr") != "herdr"
            or w.terminal_runtime("subrouter") != ""
            or w.terminal_instance_id("pane:42") != ""
            or w.terminal_instance_id("pane:abc") != ""
            or w.terminal_instance_id("surface:123e4567-e89b-12d3-a456-426614174000") != ""):
        failed += 1; print("FAIL  terminal runtime must be cmux or herdr, never provider route")
    else: print("ok    terminal runtime is distinct from provider routing")
    display_cases = {
        "Fix queue (pane:42)": "Fix queue (pane:42)",
        "Fix `x` ![probe](//attacker.invalid/p)": "",
        "[link](https://attacker.invalid)": "",
        "Fix | table": "",
    }
    for src, want in display_cases.items():
        got = w.safe_public_display(src)
        if got != want:
            failed += 1; print(f"FAIL  safe_public_display({src!r})={got!r} want={want!r}")
        else: print(f"ok    safe_public_display({src!r}) -> {got!r}")

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
        absent["workstream"] == "" and absent["launcher"] == "shell"
        and absent["route"] == "shell"
        and configured["workstream"] == "SY-LF-2026-08-20"
        and configured["launcher"] == "cmux" and configured["route"] == "shipyard-daemon"
        and malformed["launcher"] == "cmux" and malformed["route"] == "shell"
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

    # A launcher outside cmux can supply portable native-session and terminal
    # recovery facts without teaching Whence agent-specific resume syntax.
    portable_env = {
        "WHENCE_AGENT": "qwen", "WHENCE_HOST_LABEL": "m1",
        "WHENCE_TERMINAL_RUNTIME": "herdr", "WHENCE_TERMINAL_ADDRESS": "pane:42",
        "WHENCE_TERMINAL_INSTANCE": "herdr-run-8f7c",
        "WHENCE_TERMINAL_WORKSPACE": "Fix queue", "WHENCE_TERMINAL_TAB": "Fix queue",
        "WHENCE_SESSION_ID": "qwen-session-42",
        "WHENCE_RESUME_COMMAND": "qwen resume qwen-session-42",
        "WHENCE_RELAUNCH_COMMAND": "herdr attach pane:42",
        "WHENCE_ROUTE": "subrouter", "WHENCE_ROUTER": "m3",
    }
    with mock.patch.dict(_os.environ, portable_env, clear=True), \
         mock.patch.object(w, "cmux_workspace", return_value=""), \
         mock.patch.object(w, "cmux_tab_title", return_value=("", "")):
        portable = w.collect({"denylist": [], "hide": set()})
    portable_ft = w.footer(portable, {"hide": set()}, [])
    portable_marker = w.prior_prov(portable_ft)
    required_portable = {
        "agent": "qwen", "terminal": "herdr", "terminal_address": "pane:42",
        "terminal_instance": "herdr-run-8f7c",
        "workspace": "", "tab": "Fix queue",
        "session": "qwen-session-42", "resume": "qwen resume qwen-session-42",
        "relaunch": "herdr attach pane:42", "route": "subrouter", "router": "m3",
    }
    hidden_marker = w.prior_prov(w.footer(
        portable, {"hide": {"session", "resume", "relaunch"}}, []))
    if (any(portable.get(k) != v for k, v in required_portable.items())
            or any(portable_marker.get(k) != v for k, v in required_portable.items() if v)
            or "workspace" in portable_marker
            or any(k in hidden_marker for k in ("session", "resume", "relaunch"))):
        failed += 1
        print(f"FAIL  portable recovery provenance: p={portable!r} marker={portable_marker!r} "
              f"hidden={hidden_marker!r}")
    else:
        print("ok    portable recovery provenance is public-safe, machine-readable, and hide-aware")

    comment_name = dict(portable, tab="Investigate alpha--beta")
    comment_marker = w.footer(comment_name, {"hide": set()}, [])
    if ("alpha--beta" not in w.prior_prov(comment_marker).get("tab", "")
            or "alpha--beta" in comment_marker.splitlines()[0]):
        failed += 1
        print(f"FAIL  marker HTML-comment escaping: {comment_marker.splitlines()[0]!r}")
    else:
        print("ok    machine-readable marker round-trips HTML-comment-sensitive names")

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
                     "origin_state": "known", "session": "session-original",
                     "resume": "claude --resume session-original",
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
                               launcher="daemon", route="queue", agent="unknown",
                               tab="", origin_state="unresolved", session="",
                               resume="", goals="")
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
            or recorded["p"].get("agent") != "claude"
            or recorded["p"].get("tab") != "Deferred PR"
            or recorded["p"].get("origin_state") != "known"
            or recorded["p"].get("session") != "session-original"
            or recorded["p"].get("resume") != "claude --resume session-original"
            or not recorded.get("provenance_locked")
            or recorded.get("revision") != 3):
        failed += 1
        print(f"FAIL  ledger capture: key={recorded_key!r} head={recorded_head!r} goal={recorded_goal!r}")
    else:
        print("ok    ledger capture: same-HEAD delayed worker preserves all known provenance")

    # A later synchronous pre-exec is an explicit provenance recapture. It may
    # replace a stale locked owner for the same immutable HEAD, but an unlocked
    # delayed observation or an unresolved/name-only capture cannot revert it.
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = pathlib.Path(tmp) / "ledger.json"
        branch = "fix/recaptured"
        capture_key = f"danielraffel/pulp#{branch}"
        old = {f: "" for f in w.FIELDS}
        old.update({"agent": "qwen", "tab": "Old pane", "workspace": "old-ws",
                    "origin_state": "known", "terminal": "herdr",
                    "terminal_address": "pane:42", "terminal_instance": "herdr-run-old",
                    "session": "qwen-old", "resume": "qwen resume qwen-old",
                    "relaunch": "herdr attach pane:42", "workstream": "GEN-14",
                    "launcher": "herdr", "route": "direct",
                    "goals": "https://example.com/goal-one"})
        new = dict(old, tab="New pane", workspace="new-ws", terminal_address="pane:99",
                   terminal_instance="herdr-run-new", session="qwen-new",
                   resume="qwen resume qwen-new", relaunch="herdr attach pane:99",
                   workstream="", launcher="unresolved", route="subrouter", router="m3",
                   goals="https://example.com/goal-two")
        moved = dict(new, tab="Renamed pane", workspace="moved-ws", resume="", relaunch="")
        unresolved = dict(old, agent="unresolved", origin_state="unresolved", session="",
                          terminal_address="", terminal_instance="")
        with mock.patch.object(w, "LEDGER", ledger_path):
            w.ledger_record("", old, "danielraffel/pulp", branch, "origin/main",
                            str(pathlib.Path.cwd()), lock_provenance=True)
            w.ledger_record("", new, "danielraffel/pulp", branch, "origin/main",
                            str(pathlib.Path.cwd()), lock_provenance=True)
            w.ledger_record("", moved, "danielraffel/pulp", branch, "origin/main",
                            str(pathlib.Path.cwd()), lock_provenance=True)
            w.ledger_record("", old, "danielraffel/pulp", branch, "origin/main",
                            str(pathlib.Path.cwd()), lock_provenance=False)
            w.ledger_record("", unresolved, "danielraffel/pulp", branch, "origin/main",
                            str(pathlib.Path.cwd()), lock_provenance=True)
            recaptured = json.loads(ledger_path.read_text())[capture_key]
            no_resume_branch = "fix/recaptured-no-resume"
            no_resume_key = f"danielraffel/pulp#{no_resume_branch}"
            w.ledger_record("", old, "danielraffel/pulp", no_resume_branch, "origin/main",
                            str(pathlib.Path.cwd()), lock_provenance=True)
            no_resume = dict(new, resume="", relaunch="")
            w.ledger_record("", no_resume, "danielraffel/pulp", no_resume_branch, "origin/main",
                            str(pathlib.Path.cwd()), lock_provenance=True)
            recaptured_no_resume = json.loads(ledger_path.read_text())[no_resume_key]
    if any(recaptured["p"].get(k) != v for k, v in {
            "agent": "qwen", "tab": "Renamed pane", "workspace": "moved-ws",
            "terminal": "herdr", "terminal_address": "pane:99",
            "terminal_instance": "herdr-run-new", "session": "qwen-new",
            "resume": "qwen resume qwen-new", "relaunch": "herdr attach pane:99",
            "workstream": "GEN-14", "launcher": "herdr", "route": "subrouter",
            "router": "m3",
            "goals": "https://example.com/goal-one\nhttps://example.com/goal-two"}.items()
            or recaptured_no_resume["p"].get("resume")
            or recaptured_no_resume["p"].get("relaunch")):
        failed += 1
        print(f"FAIL  locked provenance recapture boundary: {recaptured!r}")
    else:
        print("ok    locked provenance recapture replaces only from a complete synchronous capture")

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

    # The revision mismatch above must survive process boundaries. On the next
    # timer pass, known-but-different provenance is republished even though the
    # normal healing policy intentionally ignores mere known -> known renames.
    second_stamped = []
    old_body = w.footer(stamped_p, sweep_cfg, [])
    second_prs = subprocess.CompletedProcess([], 0, json.dumps([
        {"number": 41, "body": old_body, "headRefOid": "same-head"},
    ]), "")
    with tempfile.TemporaryDirectory() as tmp:
        second_ledger = pathlib.Path(tmp) / "ledger.json"
        second_ledger.write_text(json.dumps({sweep_key: interleaved}))
        with mock.patch.object(w, "LEDGER", second_ledger), \
             mock.patch.object(w, "_now_epoch", return_value=103), \
             mock.patch.object(w, "github_client_for_repo", return_value="ghapp"), \
             mock.patch.object(w, "sh", return_value=second_prs), \
             mock.patch.object(w, "apply_stamp",
                               side_effect=lambda pr, p, *a, **k: second_stamped.append((pr, dict(p)))):
            second_count = w.sweep(sweep_cfg)
        second_record = json.loads(second_ledger.read_text())[sweep_key]
    if (second_count != 1 or len(second_stamped) != 1
            or second_stamped[0][1].get("workstream") != "SY-LF-NEWER"
            or second_stamped[0][1].get("tab") != "Newer"
            or second_record.get("published_revision") != second_record.get("revision")
            or not second_record.get("done")
            or second_record.get("publication_claim")):
        failed += 1
        print(f"FAIL  durable next-sweep revision: count={second_count} "
              f"stamped={second_stamped!r} rec={second_record!r}")
    else:
        print("ok    durable next-sweep revision: newer known context is republished before done")

    # A large pending ledger is processed in bounded, fair slices. Successful
    # rows become terminal and subsequent timer passes resume after the durable
    # cursor until every row has had its turn.
    with tempfile.TemporaryDirectory() as tmp:
        fair_ledger = pathlib.Path(tmp) / "ledger.json"
        fair_rows = {}
        for index in range(5):
            fair_rows[f"example/repo#branch-{index}"] = {
                "p": {f: "" for f in w.FIELDS}, "ts": 100,
                "head": f"head-{index}", "revision": 1,
            }
        fair_ledger.write_text(json.dumps(fair_rows))
        queried_keys = []

        def fair_query(*args, **kwargs):
            branch = args[args.index("--head") + 1]
            key = f"example/repo#{branch}"
            queried_keys.append(key)
            head = fair_rows[key]["head"]
            return subprocess.CompletedProcess(
                [], 0, json.dumps([{"number": len(queried_keys), "body": "",
                                    "headRefOid": head}]), "")

        def fair_publish(key, *args, **kwargs):
            with w._ledger_lock():
                current = w._load_ledger()
                current[key]["done"] = True
                w._write_ledger(current)
            return True

        with mock.patch.object(w, "LEDGER", fair_ledger), \
             mock.patch.object(w, "_now_epoch", return_value=101), \
             mock.patch.object(w, "github_client_for_repo", return_value="ghapp"), \
             mock.patch.object(w, "sh", side_effect=fair_query), \
             mock.patch.object(w, "_publish_ledger_pr", side_effect=fair_publish):
            fair_counts = [w.sweep(sweep_cfg, max_items=2, max_seconds=30)
                           for _ in range(3)]
        final_fair = json.loads(fair_ledger.read_text())
        cursor = fair_ledger.with_name(fair_ledger.name + ".sweep-cursor").read_text().strip()
    expected_fair = list(sorted(fair_rows))
    if (fair_counts != [2, 2, 1] or queried_keys != expected_fair
            or not all(rec.get("done") for rec in final_fair.values())
            or cursor != expected_fair[-1]):
        failed += 1
        print(f"FAIL  bounded fair sweep: counts={fair_counts} queries={queried_keys} "
              f"cursor={cursor!r} final={final_fair!r}")
    else:
        print("ok    bounded fair sweep: repeated timer passes drain every row once")

    # The wall-clock budget is independent of the item budget. Once exhausted,
    # the current row remains pending and the cursor advances so it cannot starve
    # later rows forever.
    with tempfile.TemporaryDirectory() as tmp:
        budget_ledger = pathlib.Path(tmp) / "ledger.json"
        budget_ledger.write_text(json.dumps(fair_rows))
        with mock.patch.object(w, "LEDGER", budget_ledger), \
             mock.patch.object(w, "_now_epoch", return_value=101), \
             mock.patch.object(w, "github_client_for_repo", return_value="ghapp"), \
             mock.patch.object(w, "sh", return_value=subprocess.CompletedProcess([], 0, "[]", "")) as budget_sh, \
             mock.patch.object(w.time, "monotonic", side_effect=[0, 0, 0, 2]):
            budget_count = w.sweep(sweep_cfg, max_items=10, max_seconds=1)
        budget_cursor = budget_ledger.with_name(
            budget_ledger.name + ".sweep-cursor").read_text().strip()
    if budget_count != 0 or budget_sh.call_count != 1 or budget_cursor != expected_fair[0]:
        failed += 1
        print(f"FAIL  sweep wall budget: count={budget_count} calls={budget_sh.call_count} "
              f"cursor={budget_cursor!r}")
    else:
        print("ok    sweep wall budget: pending work survives for the next fair pass")

    # A targeted retry may already own a branch publication lock. The global
    # sweep must skip that row within its deadline instead of waiting behind the
    # live owner. A separate whole-sweep lock also prevents concurrent cursor
    # writers from regressing one another.
    lock_holder = (
        "import fcntl,pathlib,sys,time; "
        "p=pathlib.Path(sys.argv[1]); m=pathlib.Path(sys.argv[2]); "
        "f=p.open('a+'); fcntl.flock(f.fileno(),fcntl.LOCK_EX); "
        "m.write_text('held'); time.sleep(float(sys.argv[3]))"
    )
    with tempfile.TemporaryDirectory() as tmp:
        locked_ledger = pathlib.Path(tmp) / "ledger.json"
        locked_key = "example/repo#locked"
        locked_ledger.write_text(json.dumps({locked_key: {
            "p": {f: "" for f in w.FIELDS}, "ts": 100,
            "head": "locked-head", "revision": 1,
        }}))
        digest = w.hashlib.sha256(locked_key.encode()).hexdigest()
        publication_path = locked_ledger.with_name(
            f"{locked_ledger.name}.{digest}.publish.lock")
        publication_marker = pathlib.Path(tmp) / "publication-held"
        owner = subprocess.Popen([
            sys.executable, "-c", lock_holder, str(publication_path),
            str(publication_marker), "1.2",
        ])
        for _ in range(100):
            if publication_marker.exists(): break
            time.sleep(0.01)
        publication_acquired = publication_marker.exists()
        matching = subprocess.CompletedProcess([], 0, json.dumps([
            {"number": 1, "body": "", "headRefOid": "locked-head"}]), "")
        started = time.monotonic()
        with mock.patch.object(w, "LEDGER", locked_ledger), \
             mock.patch.object(w, "_now_epoch", return_value=101), \
             mock.patch.object(w, "github_client_for_repo", return_value="ghapp"), \
             mock.patch.object(w, "sh", return_value=matching):
            locked_count = w.sweep(sweep_cfg, max_items=1, max_seconds=0.6)
        locked_elapsed = time.monotonic() - started
        owner.wait(timeout=3)

        sweep_path = locked_ledger.with_name(locked_ledger.name + ".sweep.lock")
        sweep_marker = pathlib.Path(tmp) / "sweep-held"
        sweep_owner = subprocess.Popen([
            sys.executable, "-c", lock_holder, str(sweep_path), str(sweep_marker), "0.5",
        ])
        for _ in range(100):
            if sweep_marker.exists(): break
            time.sleep(0.01)
        cursor_path = locked_ledger.with_name(locked_ledger.name + ".sweep-cursor")
        cursor_before = cursor_path.read_text() if cursor_path.exists() else ""
        started = time.monotonic()
        with mock.patch.object(w, "LEDGER", locked_ledger), \
             mock.patch.object(w, "sh") as overlap_sh:
            overlap_count = w.sweep(sweep_cfg, max_items=1, max_seconds=0.6)
        overlap_elapsed = time.monotonic() - started
        cursor_after = cursor_path.read_text() if cursor_path.exists() else ""
        sweep_owner.wait(timeout=2)
    if (not publication_acquired or locked_count != 0 or locked_elapsed > 1.0
            or overlap_count != 0 or overlap_elapsed > 0.2 or overlap_sh.called
            or cursor_before != cursor_after):
        failed += 1
        print(f"FAIL  sweep lock deadlines: publication={locked_elapsed:.3f}s "
              f"overlap={overlap_elapsed:.3f}s counts={locked_count}/{overlap_count} "
              f"cursor_changed={cursor_before != cursor_after}")
    else:
        print("ok    sweep lock deadlines: live owners cannot wedge or regress a pass")

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

    # Neither live cmux resolution nor GitHub publication may run beneath the
    # single process-wide ledger flock. Prove a second process can acquire the
    # exact lock during both callbacks.
    with tempfile.TemporaryDirectory() as tmp:
        free_ledger = pathlib.Path(tmp) / "ledger.json"
        free_key = "danielraffel/pulp#fix/lock-free"
        free_p = {f: "" for f in w.FIELDS}
        free_p.update({"agent": "codex", "tab": "Lock free",
                       "origin_state": "known"})
        free_ledger.write_text(json.dumps({free_key: {
            "p": free_p, "ts": 100, "head": "lock-free-head", "revision": 1,
        }}))
        callback_acquired = []

        def acquire_during(label):
            marker = pathlib.Path(tmp) / label
            child = subprocess.Popen([
                sys.executable, "-c", child_code,
                str(free_ledger) + ".lock", str(marker),
            ])
            child.wait(timeout=2)
            callback_acquired.append(child.returncode == 0 and marker.exists())

        def lock_free_best(record, cfg, body=""):
            acquire_during("cmux-acquired")
            return dict(record["p"]), ""

        def lock_free_apply(*args, **kwargs):
            acquire_during("github-acquired")
            return []

        with mock.patch.object(w, "LEDGER", free_ledger), \
             mock.patch.object(w, "_now_epoch", return_value=101), \
             mock.patch.object(w, "_best_provenance", side_effect=lock_free_best), \
             mock.patch.object(w, "apply_stamp", side_effect=lock_free_apply):
            free_published = w._publish_ledger_pr(
                free_key, "lock-free-head", "99", "", sweep_cfg, "sweep:new")
    if not free_published or callback_acquired != [True, True]:
        failed += 1
        print(f"FAIL  publication lock scope: published={free_published} "
              f"callbacks={callback_acquired!r}")
    else:
        print("ok    publication lock scope: cmux and GitHub callbacks never hold global flock")

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
    if deadline_sh.call_count != 1 or deadline_sleep.call_count:
        failed += 1
        print(f"FAIL  retry deadline: queries={deadline_sh.call_count} sleeps={deadline_sleep.call_args_list}")
    else:
        print("ok    retry deadline: exhausted request budget cannot add a sleep")

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
            "  nohup sh -c 'while [ ! -f \"$WHENCE_FAKE_STATE/pr-created\" ]; do "
            "sleep 0.01; done; touch \"$WHENCE_FAKE_STATE/stamped\"' "
            "</dev/null >/dev/null 2>&1 &\n"
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
        if (wrapped_tools != list(w.PR_TOOLS) or not all(
                f"{tool}()" in hook_text for tool in w.PR_TOOLS)
                or "__whence_sweep" in hook_text or "--sweep" in hook_text):
            failed += 1
            print(f"FAIL  deterministic wrappers: tools={wrapped_tools}")
        else:
            print("ok    shell wrapper generation: no interruptible global sweep remains")
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
                        and not (state / "swept").exists()
                        and not (state / "recollected").exists()
                        and (state / "context").read_text().strip()
                        == "SY-LF-2026-08-20|cmux|direct")
        if not lifecycle_ok:
            failed += 1
            print(f"FAIL  long-running wrapper: rc={driven.returncode} out={driven.stdout!r} err={driven.stderr!r}")
        else:
            print("ok    long-running wrapper: exact retry stamps without a global post sweep")
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
                   and (state / "stamped").exists() and not (state / "swept").exists()
                   and not (state / "recollected").exists())
        if not bash_ok:
            failed += 1
            print(f"FAIL  bash wrapper: rc={bash_run.returncode} state={[p.name for p in state.iterdir()]}")
        else:
            print("ok    shell wrapper: absolute-path bypass works in zsh and bash")

    # Drive the production PostToolUse entrypoint, not merely helper functions.
    # A push records its ledger row and returns without invoking the fake GitHub
    # client; the launchd timer owns global sweep work.
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        home, bindir = root / "home", root / "bin"
        cfg_dir = home / ".config" / "whence"
        cfg_dir.mkdir(parents=True); bindir.mkdir()
        (cfg_dir / ".last-selfupdate").write_text(str(int(time.time())))
        gh_marker = root / "gh-called"
        fake_gh = bindir / "gh"
        fake_gh.write_text(
            "#!/bin/sh\n"
            f"touch '{gh_marker}'\n"
            "sleep 2\n"
            "exit 1\n"
        )
        fake_gh.chmod(0o755)
        payload = {
            "tool_input": {"command": "git push"},
            "tool_response": {"stderr":
                "To github.com:danielraffel/whence.git\n"
                " * [new branch] fix/hook -> fix/hook\n"},
            "cwd": str(pathlib.Path(__file__).parent),
            "session_id": "hook-nonblocking-test",
            "transcript_path": str(home / ".codex" / "session.jsonl"),
        }
        env = dict(__import__("os").environ)
        env.update({"HOME": str(home), "PATH": f"{bindir}:/usr/bin:/bin"})
        for inherited in list(env):
            if inherited.startswith("CMUX_") or inherited.startswith("WHENCE_"):
                env.pop(inherited, None)
        started = time.monotonic()
        hook_run = subprocess.run(
            [sys.executable, str(pathlib.Path(__file__).parent / "whence"), "--hook"],
            input=json.dumps(payload), env=env, capture_output=True, text=True,
            timeout=1.5,
        )
        hook_elapsed = time.monotonic() - started
        hook_ledger = cfg_dir / "branch-ledger.json"
        hook_rows = json.loads(hook_ledger.read_text()) if hook_ledger.exists() else {}
        gh_called = gh_marker.exists()
    if (hook_run.returncode != 0 or hook_elapsed > 1.0 or gh_called
            or "danielraffel/whence#fix/hook" not in hook_rows):
        failed += 1
        print(f"FAIL  PostToolUse nonblocking: rc={hook_run.returncode} "
              f"elapsed={hook_elapsed:.3f} gh_called={gh_called} rows={hook_rows!r}")
    else:
        print("ok    PostToolUse: records push and returns without a global sweep")

    # ── Regression: an orchestrator commits on top of the captured HEAD ──
    # The shell wrapper captures HEAD before `shipyard pr` runs; Shipyard then
    # adds `chore: bump versions` and opens the PR at that new commit. The exact
    # HEAD match therefore never fired and the PR stayed unlabelled forever
    # (danielraffel/Shipyard#616, #612, #607). Drive the real ledger_record and
    # real git ancestry; only the GitHub client is faked.
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp) / "repo"
        repo.mkdir()
        def git(*a):
            return subprocess.run(["git", "-C", str(repo), *a], check=True,
                                  capture_output=True, text=True).stdout.strip()
        git("init", "-q", "-b", "feat/x")
        git("config", "user.email", "t@example.com"); git("config", "user.name", "t")
        git("remote", "add", "origin", "https://github.com/example/repo.git")
        git("commit", "-q", "--allow-empty", "-m", "base")
        # A same-named branch reused for different work shares history with the
        # capture but does not contain the captured commit.
        git("checkout", "-q", "-b", "other")
        git("commit", "-q", "--allow-empty", "-m", "unrelated")
        unrelated = git("rev-parse", "HEAD")
        git("checkout", "-q", "feat/x")
        git("commit", "-q", "--allow-empty", "-m", "work")
        captured = git("rev-parse", "HEAD")
        anc_ledger = pathlib.Path(tmp) / "ledger.json"
        anc_p = {f: "" for f in w.FIELDS}
        anc_p.update({"agent": "claude", "host": "m3", "tab": "T", "origin_state": "known",
                      "launcher": "cmux", "route": "direct", "session": "s-1"})
        anc_cfg = {"labels": True, "footer": True, "hide": set(),
                   "colors": dict(w.DEFAULT_COLORS), "label_maxlen": 24,
                   "denylist": [], "redact_placeholder": "(redacted)"}
        real_sh = w.sh
        with mock.patch.object(w, "LEDGER", anc_ledger), \
             mock.patch.object(w, "_now_epoch", return_value=1_790_000_000):
            key = w.ledger_record("", anc_p, "", "", "HEAD", str(repo), lock_provenance=True)
        git("commit", "-q", "--allow-empty", "-m", "chore: bump versions")
        bumped = git("rev-parse", "HEAD")
        rec = json.loads(anc_ledger.read_text())[key]
        git_dir_recorded = (os.path.realpath(rec.get("git_dir", ""))
                            == os.path.realpath(repo / ".git"))
        after = "2026-09-21T14:20:00Z"   # > ts (2026-09-21T14:13:20Z)
        before = "2026-09-21T12:00:00Z"
        def run_case(prs, runner):
            stamped = []
            def fake_sh(*args, **kwargs):
                if args and args[0] == "fakegh":
                    return subprocess.CompletedProcess(args, 0, json.dumps(prs), "")
                return real_sh(*args, **kwargs)
            anc_ledger.write_text(json.dumps({key: rec}))
            with mock.patch.object(w, "LEDGER", anc_ledger), \
                 mock.patch.object(w, "_now_epoch", return_value=1_790_000_100), \
                 mock.patch.object(w, "github_client_for_repo", return_value="fakegh"), \
                 mock.patch.object(w, "sh", side_effect=fake_sh), \
                 mock.patch.object(w, "_best_provenance", side_effect=lambda r, c, b="": (r["p"], "")), \
                 mock.patch.object(w, "apply_stamp",
                                   side_effect=lambda pr, p, *a, **k: stamped.append(pr)), \
                 mock.patch.object(w.time, "sleep"):
                if runner == "retry":
                    w.retry_pending_pr(key, anc_cfg, attempts=1, delay=0)
                else:
                    w.sweep(anc_cfg)
            return stamped
        def pr(n, head, created):
            return {"number": n, "body": "", "headRefOid": head, "createdAt": created}
        results = {}
        for runner in ("retry", "sweep"):
            results[(runner, "bumped")] = run_case([pr(616, bumped, after)], runner)
            results[(runner, "unrelated")] = run_case([pr(700, unrelated, after)], runner)
            results[(runner, "old-pr")] = run_case([pr(500, bumped, before)], runner)
            results[(runner, "exact-wins")] = run_case(
                [pr(801, bumped, after), pr(802, captured, after)], runner)
    anc_ok = (key == "example/repo#feat/x"
              and git_dir_recorded
              and all(results[(r, "bumped")] == ["616"] for r in ("retry", "sweep"))
              and all(results[(r, "unrelated")] == [] for r in ("retry", "sweep"))
              and all(results[(r, "old-pr")] == [] for r in ("retry", "sweep"))
              and all(results[(r, "exact-wins")] == ["802"] for r in ("retry", "sweep")))
    if not anc_ok:
        failed += 1
        print(f"FAIL  descendant PR head: key={key} git_dir={rec.get('git_dir')!r} {results}")
    else:
        print("ok    orchestrator bump commit: PR descending from the capture is stamped; "
              "unrelated, older, and non-exact-when-exact-exists are not")

    # ── Regression: the captured commit is rewritten before the PR opens ──
    # `shipyard pr --skip-skill-update ...` AMENDS the captured HEAD to add a
    # `Skill-Update:` trailer, then commits `chore: bump versions` and opens the
    # PR there (danielraffel/Shipyard#621). The captured commit is no longer an
    # ancestor of the PR head, so the descent rule above cannot match; neither
    # can it after an agent rebases onto a newer base. Real git, fake GitHub.
    T0 = 1_790_000_000                       # 2026-09-21T14:13:20Z
    def iso(epoch):
        return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
    rw_cfg = {"labels": True, "footer": True, "hide": set(),
              "colors": dict(w.DEFAULT_COLORS), "label_maxlen": 24,
              "denylist": [], "redact_placeholder": "(redacted)"}
    def rw_prov(session, tab="T"):
        p = {f: "" for f in w.FIELDS}
        p.update({"agent": "claude", "host": "m3", "tab": tab, "origin_state": "known",
                  "launcher": "cmux", "route": "direct", "session": session})
        return p
    real_sh_rw = w.sh
    def rw_run(ledger, key, prs, runner):
        stamped = []
        rec = json.loads(ledger.read_text())[key]
        def fake_sh(*args, **kwargs):
            if args and args[0] == "fakegh":
                return subprocess.CompletedProcess(args, 0, json.dumps(prs), "")
            return real_sh_rw(*args, **kwargs)
        saved = ledger.read_text()
        with mock.patch.object(w, "LEDGER", ledger), \
             mock.patch.object(w, "_now_epoch", return_value=T0 + 100), \
             mock.patch.object(w, "github_client_for_repo", return_value="fakegh"), \
             mock.patch.object(w, "sh", side_effect=fake_sh), \
             mock.patch.object(w, "_best_provenance", side_effect=lambda r, c, b="": (r["p"], "")), \
             mock.patch.object(w, "apply_stamp",
                               side_effect=lambda pr, p, *a, **k: stamped.append(pr)), \
             mock.patch.object(w.time, "sleep"):
            if runner == "retry":
                w.retry_pending_pr(key, rw_cfg, attempts=1, delay=0)
            else:
                w.sweep(rw_cfg)
        ledger.write_text(saved)
        return stamped
    def both(ledger, key, prs):
        return {r: rw_run(ledger, key, prs, r) for r in ("retry", "sweep")}
    def rw_pr(n, head, created):
        return {"number": n, "body": "", "headRefOid": head, "createdAt": iso(created)}
    rw = {}
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp) / "repo"
        repo.mkdir()
        def git(*a):
            return subprocess.run(["git", "-C", str(repo), *a], check=True,
                                  capture_output=True, text=True).stdout.strip()
        def change(name, text, msg):
            (repo / name).write_text(text)
            git("add", name)
            git("commit", "-q", "-m", msg)
            return git("rev-parse", "HEAD")
        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@example.com"); git("config", "user.name", "t")
        git("remote", "add", "origin", "https://github.com/example/repo.git")
        base = change("base.txt", "base\n", "base")
        git("update-ref", "refs/remotes/origin/main", base)
        ledger = pathlib.Path(tmp) / "ledger.json"
        def capture(branch_key_branch, session, lock=True, now=T0, tab="T"):
            with mock.patch.object(w, "LEDGER", ledger), \
                 mock.patch.object(w, "_now_epoch", return_value=now):
                return w.ledger_record("", rw_prov(session, tab), "", "", "HEAD",
                                       str(repo), lock_provenance=lock)

        # 1. The #621 shape: capture, amend (trailer only), bump, open.
        git("checkout", "-q", "-b", "fix/amend", base)
        captured = change("fix.txt", "fix\n", "fix: the thing")
        ledger.write_text("{}")
        k_amend = capture("fix/amend", "s-1")
        git("commit", "-q", "--amend", "-m", "fix: the thing\n\nSkill-Update: skip skill=ci")
        amended = git("rev-parse", "HEAD")
        bumped = change("VERSION", "2\n", "chore: bump versions")
        rw["amend"] = both(ledger, k_amend, [rw_pr(621, bumped, T0 + 60)])
        # 1b. The same amended PR opened BEFORE the capture is older work.
        rw["older"] = both(ledger, k_amend, [rw_pr(620, bumped, T0 - 3600)])
        # 1c. ... and one opened long after the capture is out of the window.
        rw["late"] = both(ledger, k_amend,
                          [rw_pr(622, bumped, T0 + getattr(w, "CAPTURE_MATCH_WINDOW", 86400) + 60)])
        # 1d. An exact-head PR still wins over the rewritten one.
        rw["exact"] = both(ledger, k_amend, [rw_pr(901, bumped, T0 + 60),
                                             rw_pr(902, captured, T0 + 60)])
        # 1e. Same branch name, different change: patch-ids differ.
        git("checkout", "-q", "-b", "reuse", base)
        other = change("fix.txt", "something else\n", "fix: the thing")
        rw["unrelated"] = both(ledger, k_amend, [rw_pr(700, other, T0 + 60)])

        # 1f. Same diff, different author date (an independent commit of the
        #     same change), is not this capture's commit.
        git("checkout", "-q", "-b", "reuse2", base)
        (repo / "fix.txt").write_text("fix\n"); git("add", "fix.txt")
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "fix: the thing"],
                       check=True, capture_output=True,
                       env={**os.environ, "GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z"})
        same_diff = git("rev-parse", "HEAD")
        rw["other-author"] = both(ledger, k_amend, [rw_pr(701, same_diff, T0 + 60)])
        # 1g. The captured change reached main through other work; a later PR
        #     rebased onto that main carries it only as main history.
        git("checkout", "-q", "-b", "landed", base)
        git("cherry-pick", captured)
        landed_main = git("rev-parse", "HEAD")
        saved_main = git("rev-parse", "refs/remotes/origin/main")
        git("update-ref", "refs/remotes/origin/main", landed_main)
        later = change("later.txt", "later\n", "feat: later work")
        rw["via-main"] = both(ledger, k_amend, [rw_pr(702, later, T0 + 60)])
        git("update-ref", "refs/remotes/origin/main", saved_main)

        # 2. Rebase onto a newer main (origin/main moved, and it is excluded).
        git("checkout", "-q", "main")
        new_main = change("main2.txt", "main moved\n", "main: unrelated work")
        git("update-ref", "refs/remotes/origin/main", new_main)
        git("checkout", "-q", "-b", "fix/rebase", base)
        change("r.txt", "rebased\n", "fix: rebased work")
        ledger.write_text("{}")
        k_rebase = capture("fix/rebase", "s-1")
        git("rebase", "-q", "main")
        rebased = change("VERSION", "3\n", "chore: bump versions")
        rw["rebase"] = both(ledger, k_rebase, [rw_pr(631, rebased, T0 + 60)])

        # 3. Another session captures the SAME head of this branch name: two
        #    provenances claim it, so the rewritten PR is refused, not guessed.
        git("checkout", "-q", "-b", "fix/claimed", base)
        change("c.txt", "claimed\n", "fix: claimed")
        ledger.write_text("{}")
        k_claim = capture("fix/claimed", "s-1")
        capture("fix/claimed", "s-2", now=T0 + 1)
        claim_rec = json.loads(ledger.read_text())[k_claim]
        git("commit", "-q", "--amend", "-m", "fix: claimed\n\nSkill-Update: skip")
        claimed_pr = change("VERSION", "4\n", "chore: bump versions")
        rw["two-sessions"] = both(ledger, k_claim, [rw_pr(641, claimed_pr, T0 + 60)])

        # 4. A capture with NO session at a different head starts a fresh
        #    claim: the earlier head it replaced cannot vouch for a PR.
        git("checkout", "-q", "-b", "fix/replaced", base)
        change("d.txt", "first\n", "fix: first claim")
        ledger.write_text("{}")
        k_repl = capture("fix/replaced", "s-1")
        git("commit", "-q", "--amend", "-m", "fix: first claim\n\nSkill-Update: skip")
        replaced_pr = change("VERSION", "5\n", "chore: bump versions")
        git("checkout", "-q", "-b", "elsewhere", base)
        change("e.txt", "other work\n", "other work")
        git("branch", "-f", "fix/replaced", "HEAD")
        git("checkout", "-q", "fix/replaced")
        capture("fix/replaced", "", lock=False, now=T0 + 2)
        rw["replaced"] = both(ledger, k_repl, [rw_pr(651, replaced_pr, T0 + 60)])

        # 5. The same session re-captures at a NEW head: the head moves, the
        #    lock keeps identity, and a PR built on the EARLIER head still
        #    matches because it descends from a recorded head.
        git("checkout", "-q", "-b", "fix/moving", base)
        first_head = change("m.txt", "one\n", "fix: step one")
        ledger.write_text("{}")
        k_move = capture("fix/moving", "s-1", tab="Named tab")
        second_head = change("m2.txt", "two\n", "fix: step two")
        capture("fix/moving", "s-1", lock=False, now=T0 + 5, tab="surface:9")
        move_rec = json.loads(ledger.read_text())[k_move]
        git("checkout", "-q", "-b", "side", first_head)
        side_pr = change("VERSION", "6\n", "chore: bump versions")
        rw["earlier-head"] = both(ledger, k_move, [rw_pr(661, side_pr, T0 + 60)])

    want = {
        "amend": ["621"], "older": [], "late": [], "exact": ["902"], "unrelated": [],
        "other-author": [], "via-main": [],
        "rebase": ["631"], "two-sessions": [], "replaced": [], "earlier-head": ["661"],
    }
    rw_bad = {case: got for case, got in rw.items()
              if got != {"retry": want[case], "sweep": want[case]}}
    move_ok = (move_rec.get("head") == second_head
               and [h["head"] for h in move_rec.get("heads", [])] == [first_head, second_head]
               and move_rec.get("provenance_locked") is True
               and move_rec["p"].get("tab") == "Named tab")
    claim_ok = claim_rec.get("claimants") == ["s-1", "s-2"]
    if rw_bad or not move_ok or not claim_ok or amended == captured:
        failed += 1
        print(f"FAIL  rewritten capture: bad={rw_bad} move_rec={move_rec if not move_ok else 'ok'} "
              f"claimants={claim_rec.get('claimants')}")
    else:
        print("ok    rewritten capture: amend and rebase stamp via patch-id; a new head "
              "keeps the lock and its history; two sessions, a replaced claim, an older "
              "or late PR, and a different change do not; exact still wins")

    # ── Launcher/route resolution: no `5·unresolved` from a real session ──
    # `subrouter codex` scrubs CMUX_* from Codex's environment, so on m5 every
    # Codex PR stamped launcher/route `unresolved` and no tab. And the route was
    # derived only by hook.sh, so the Python agent-hook path stamped
    # `unresolved` over a correct `5·direct` (danielraffel/tartci#260) -- and
    # `direct` itself was false for `sr claude`, whose cmux launch argv carries
    # no subrouter marker. Derivation now lives in collect(), from the process
    # ancestry, and recovers the stripped cmux variables from an ancestor.
    SURF = "BF748855-B51E-4E48-9F43-EED652E88D07"
    WS = "16963A89-DE79-43ED-A22F-4885AB7E999A"
    OTHER = "AAAAAAAA-B51E-4E48-9F43-EED652E88D07"
    subrouter_codex = [
        (900, "/bin/zsh -lc gh pr create --fill"),
        (901, '/Users/u/.local/bin/codex -c model_provider="subrouter" -c x=1'),
        (902, "/Users/u/bin/subrouter codex -c model_providers.subrouter.supports_websockets=false"),
        (903, "-/bin/zsh"), (904, "/usr/bin/login -flp u /bin/zsh"),
        (905, "/Applications/cmux.app/Contents/MacOS/cmux"),
    ]
    ancestor_env = {903: {"CMUX_SURFACE_ID": SURF, "CMUX_WORKSPACE_ID": WS},
                    902: {"CMUX_SURFACE_ID": SURF, "CMUX_WORKSPACE_ID": WS},
                    904: {"CMUX_SURFACE_ID": OTHER, "CMUX_WORKSPACE_ID": OTHER}}
    tab_calls = []
    def launch_collect(env, ancestry, envs=None):
        tab_calls.clear()
        def fake_env(pid, names):
            return {k: v for k, v in (envs or {}).get(pid, {}).items() if k in names}
        with mock.patch.dict(_os.environ, env, clear=True), \
             mock.patch.object(w, "_process_ancestry", return_value=ancestry), \
             mock.patch.object(w, "_process_env", side_effect=fake_env), \
             mock.patch.object(w, "host_label", return_value="m5"), \
             mock.patch.object(w, "cmux_workspace", side_effect=lambda ws, s="": ws[:4]), \
             mock.patch.object(w, "cmux_tab_title",
                               side_effect=lambda s: (tab_calls.append(s) or ("Fix queue", "") if s else ("", ""))), \
             mock.patch.object(w, "sh", return_value=subprocess.CompletedProcess([], 1, "", "")):
            return w.collect({"denylist": [], "hide": set()}, "Generous-Corp/pulp")
    codex_env = {"CODEX_SESSION_ID": "019a-codex", "SUBROUTER_CODEX_LAUNCHER": "subrouter",
                 "__CFBundleIdentifier": "com.cmuxterm.app"}
    lc = {}
    lc["m5-codex"] = launch_collect(codex_env, subrouter_codex, ancestor_env)
    with tempfile.TemporaryDirectory() as tmp:
        bindir = pathlib.Path(tmp)
        real = bindir / "subrouter"; real.write_text("#!/bin/sh\n"); real.chmod(0o755)
        (bindir / "sr").symlink_to(real)
        argv_b64 = __import__("base64").b64encode(b"/Users/u/.local/bin/claude\0").decode()
        claude_env = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "cl-1",
                      "CMUX_SURFACE_ID": SURF, "CMUX_AGENT_LAUNCH_ARGV_B64": argv_b64,
                      "PATH": str(bindir)}
        sr_chain = [(800, "/bin/zsh -c git push"),
                    (801, "/Users/u/.local/bin/claude --session-id cl-1 --settings /tmp/cmux-claude-settings.x"),
                    (802, "sr claude"), (803, "-/bin/zsh")]
        lc["m3-sr-claude"] = launch_collect(claude_env, sr_chain)
        lc["cmux-direct"] = launch_collect(claude_env, sr_chain[:2] + [(803, "-/bin/zsh")])
        lc["explicit"] = launch_collect({**claude_env, "WHENCE_ROUTE": "shipyard-daemon",
                                         "WHENCE_LAUNCHER": "herdr"}, sr_chain)
    lc["bare-codex"] = launch_collect({"CODEX_SESSION_ID": "c2"},
                                      [(700, "/bin/zsh -lc x"), (701, "/usr/local/bin/codex")])
    lc["no-agent"] = launch_collect({}, [(600, "/bin/bash")])
    got = {k: (v["launcher"], v["route"]) for k, v in lc.items()}
    want = {"m5-codex": ("subrouter", "subrouter"), "m3-sr-claude": ("cmux", "subrouter"),
            "cmux-direct": ("cmux", "direct"), "explicit": ("herdr", "shipyard-daemon"),
            "bare-codex": ("codex-cli", "codex-cli"), "no-agent": ("shell", "shell")}
    m5 = lc["m5-codex"]
    recovered = (m5["agent"] == "codex" and m5["terminal"] == "cmux"
                 and m5["terminal_address"] == SURF and m5["tab"] == "Fix queue"
                 and m5["origin_state"] == "known" and m5["workspace"] == WS[:4])
    unresolved_labels = [k for k, v in lc.items()
                         if any(n.startswith("5·") and n.endswith("unresolved")
                                for n, _ in w.labels_for(
                             v, {"hide": set(), "colors": dict(w.DEFAULT_COLORS),
                                 "label_maxlen": 24, "denylist": [],
                                 "redact_placeholder": "(redacted)"}))]
    heal = (w._prov_better({"route": "subrouter", "launcher": "cmux"},
                           {"route": "codex-cli", "launcher": "codex-cli"}, {"hide": set()})
            and not w._prov_better({"route": "codex-cli", "launcher": "codex-cli"},
                                   {"route": "subrouter", "launcher": "cmux"}, {"hide": set()}))
    hook_text = w._hook_file_text()[0]
    no_export = "WHENCE_ROUTE=" not in hook_text and "WHENCE_LAUNCHER=" not in hook_text
    if got != want or not recovered or unresolved_labels or not heal or not no_export:
        failed += 1
        print(f"FAIL  launch derivation: got={got} recovered={recovered} m5={ {k: m5[k] for k in ('agent','terminal','terminal_address','tab','origin_state','workspace')} } "
              f"unresolved={unresolved_labels} heal={heal} no_export={no_export}")
    else:
        print("ok    launch derivation: subrouter codex (scrubbed env) and sr claude route "
              "subrouter, nearest ancestor restores the cmux surface, explicit wins, "
              "fallbacks are named, and no label reads unresolved")

    # The two process readers against real processes, not mocks.
    # macOS hides the environment of platform (SIP) binaries such as /bin/zsh or
    # /usr/bin/python3 from `ps eww`; user-installed ones (subrouter, codex, a
    # framework Python) are readable. PATH= in the output is the control that
    # this interpreter is readable at all.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             env={**os.environ, "CMUX_SURFACE_ID": SURF,
                                  "CMUX_WORKSPACE_ID": "not-a-uuid"})
    try:
        time.sleep(0.3)
        readable = (pathlib.Path(f"/proc/{child.pid}/environ").exists()
                    or "PATH=" in w.sh("ps", "eww", "-o", "command=", "-p",
                                       str(child.pid)).stdout)
        read = w._process_env(child.pid, ["CMUX_SURFACE_ID", "CMUX_WORKSPACE_ID"])
    finally:
        child.kill(); child.wait()
    chain = w._process_ancestry()
    if not readable:
        print(f"SKIP  process env reader: {sys.executable} is a platform binary whose "
              "environment ps does not show")
        read = {"CMUX_SURFACE_ID": SURF}
    if read != {"CMUX_SURFACE_ID": SURF} or not chain or chain[0][0] != os.getppid():
        failed += 1
        print(f"FAIL  process readers: env={read} chain_head={chain[:1]} ppid={os.getppid()}")
    else:
        print("ok    process readers: another process's env is read shape-checked; "
              "the ancestry starts at our parent")

    # ── Launchers hide the PR command from a function wrapper ──
    # `timeout 900 shipyard pr` (how agents, subagents especially, bound a long
    # orchestrator) execs the real binary, so the `shipyard` function never ran
    # and nothing was captured. Drive the generated hook in zsh and bash.
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        bindir, state = root / "bin", root / "state"
        bindir.mkdir(); state.mkdir()
        (bindir / "whence").write_text(
            f"#!/bin/sh\necho \"$1\" >> '{state}/calls'\n")
        # A launcher that, like timeout/nohup/env, runs its command by exec.
        launcher_body = ("#!/bin/sh\n"
                         "while [ $# -gt 0 ]; do case \"$1\" in "
                         "-s|-k|-u) shift 2 ;; -*|*=*|[0-9]*) shift ;; *) break ;; esac; done\n"
                         "exec \"$@\"\n")
        for name in ("timeout", "nohup", "env"):
            (bindir / name).write_text(launcher_body)
        for tool in ("shipyard", "gh", "grep"):
            (bindir / tool).write_text(
                f"#!/bin/sh\necho \"{tool} $*\" >> '{state}/ran'\n"
                "[ \"$FAKE_RC\" ] && exit \"$FAKE_RC\"; exit 0\n")
        for f in bindir.iterdir():
            f.chmod(0o755)
        hook_file = root / "hook.sh"
        hook_file.write_text(w._hook_file_text()[0])
        cases = [
            ("timeout 900 shipyard pr --base main", "--pre-exec", 0),
            ("X=1 timeout -s KILL 5m shipyard pr", "--pre-exec", 0),
            ("nohup env PULP_SKIP_DIFF_COVER=1 shipyard pr --base main", "--pre-exec", 0),
            ("timeout 900 gh pr create --fill", "--auto", 0),
            ("timeout 5 grep shipyard pr", "", 0),
            ("timeout 900 shipyard pr --help", "", 0),
            ("nohup gh pr list", "", 0),
            ("env", "", 0),
            ("FAKE_RC=7 timeout 900 shipyard pr", "--pre-exec", 7),
        ]
        launcher_fail = []
        env = dict(os.environ)
        env.update({"ZDOTDIR": str(root), "PATH": f"{bindir}:/usr/bin:/bin"})
        for shell in (["zsh", "-fc"], ["bash", "--noprofile", "--norc", "-c"]):
            for cmd, want, want_rc in cases:
                for leftover in ("calls", "ran"):
                    try: (state / leftover).unlink()
                    except FileNotFoundError: pass
                run = subprocess.run([*shell, f'. "{hook_file}"; {cmd} >/dev/null'],
                                     env=env, capture_output=True, text=True, timeout=10)
                calls = (state / "calls").read_text().split() if (state / "calls").exists() else []
                ran = (state / "ran").exists() or cmd == "env"
                if calls != ([want] if want else []) or run.returncode != want_rc or not ran:
                    launcher_fail.append((shell[0], cmd, calls, run.returncode, run.stderr[-200:]))
    if launcher_fail:
        failed += 1
        print(f"FAIL  launcher wrappers: {launcher_fail}")
    else:
        print("ok    launcher wrappers: timeout/nohup/env capture the PR command they exec, "
              "ignore non-PR commands, and preserve the exit status")

    # ── Claude sessions launched with their own CLAUDE_CONFIG_DIR ──
    with tempfile.TemporaryDirectory() as tmp:
        home = pathlib.Path(tmp)
        (home / ".claude").mkdir()
        proxy_a = home / "session-config"   # outside the configured glob
        proxy_b = home / ".router" / "claude-proxy" / "bbb"
        proxy_a.mkdir(parents=True); proxy_b.mkdir(parents=True)
        (home / ".router" / "claude-proxy" / "aaa").mkdir()
        with mock.patch.dict(os.environ, {
                "HOME": str(home), "CLAUDE_CONFIG_DIR": str(proxy_a),
                "WHENCE_CLAUDE_CONFIG_DIRS": "~/.router/claude-proxy/*:~/missing/*"}):
            files = w.claude_settings_files()
            for f in files:
                w._wire_posttooluse(f, "Bash", {})
        wired = [json.loads(f.read_text())["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
                 for f in files]
    expected_files = [home / ".claude" / "settings.json", proxy_a / "settings.json",
                      home / ".router" / "claude-proxy" / "aaa" / "settings.json",
                      proxy_b / "settings.json"]
    if files != expected_files or not all(c.endswith("pr-hook.sh") for c in wired):
        failed += 1
        print(f"FAIL  claude config dirs: files={files} wired={wired}")
    else:
        print("ok    agent hook: wired into ~/.claude, CLAUDE_CONFIG_DIR, and configured dirs once each")

    # ── Self-heal: wire the hook into the config dir the session actually reads ──
    failed += self_heal_checks()

    # ── A missed stamp is detectable ──
    listing = json.dumps([
        {"number": 1, "headRefName": "a", "body": "x\n<!-- whence {} -->f<!-- /whence -->", "state": "MERGED",
         "author": {"login": "bot"}, "labels": []},
        {"number": 2, "headRefName": "b", "body": "", "state": "OPEN",
         "author": {"login": "bot"}, "labels": []},
        {"number": 3, "headRefName": "c", "body": "", "state": "MERGED",
         "author": {"login": "bot"}, "labels": [{"name": "1·claude"}]},
    ])
    with tempfile.TemporaryDirectory() as tmp:
        led = pathlib.Path(tmp) / "ledger.json"
        led.write_text(json.dumps({"o/r#b": {"p": {}, "ts": 1, "head": "h"}}))
        out = __import__("io").StringIO()
        with mock.patch.object(w, "LEDGER", led), \
             mock.patch.object(w, "github_client_for_repo", return_value="fakegh"), \
             mock.patch.object(w, "github_call",
                               return_value=subprocess.CompletedProcess([], 0, listing, "")), \
             mock.patch("sys.stdout", out), mock.patch("sys.stderr", __import__("io").StringIO()):
            un_rc = w.unstamped("o/r", 10)
    un_lines = out.getvalue().splitlines()
    if (un_rc != 1 or len(un_lines) != 2 or "ledger: pending sweep" not in un_lines[0]
            or "no capture on this host" not in un_lines[1]
            or "footer missing" not in un_lines[1]):
        failed += 1
        print(f"FAIL  --unstamped: rc={un_rc} lines={un_lines}")
    else:
        print("ok    --unstamped: lists unstamped PRs with their ledger state, exit 1")

    print(f"\n{'ALL PASS' if not failed else f'{failed} FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
