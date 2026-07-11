# Auto-stamp from a Claude Code hook

Claude Code fires hooks around tool calls. The simplest reliable trigger is a
**PostToolUse** hook on `Bash` that notices a PR-creating command and stamps the
branch's PR afterward.

Add to `~/.claude/settings.json` (or a project `.claude/settings.json`):

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "if grep -qiE 'gh pr create|shipyard pr|pulp pr' <<<\"$CLAUDE_TOOL_INPUT\"; then python3 ~/code/whence/whence --apply >/dev/null 2>&1 || true; fi"
          }
        ]
      }
    ]
  }
}
```

Notes:
- The `|| true` keeps a stamping hiccup from ever failing your workflow.
- Runs in the same session, so it inherits all the cmux/agent env it needs.
- For Codex, wire the equivalent in your Codex hook config; the script itself is
  agent-agnostic.
