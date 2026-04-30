# meta-weekly-report

Auto-drafts the weekly Asana 5/15 report subtask description from Jira / Tempo
/ GitHub / session activity, then updates Asana on approval.

See `SKILL.md` for the workflow specification.

## Quick start

1. **Configure** — copy `config.json.example` to `config.json`, fill in your
   Asana parent task GID, Jira cloud ID, account ID, and AI epic key.
2. **Verify MCPs** — Asana and Atlassian MCPs must be authenticated (`/mcp`).
3. **Verify Tempo** — token must be in macOS Keychain
   (`security add-generic-password -s tempo_api_token -a <user> -w <token>`).
4. **Run** — invoke `/meta-weekly-report` (or just say "draft 5/15") on Friday.
5. **Review the draft** — the skill renders all 5 sections in chat with the
   matched subtask GIDs and activity window.
6. **Submit** — reply `yes` to write to Asana. Then mark the subtask complete
   in Asana yourself.

## Recommended schedule

```
Cadence: every Friday 14:00 IST
Command: /meta-weekly-report
```

3 hours of buffer before the 17:00 IST (20:00 CET) deadline.

## Architecture

This skill is **MCP-first** — no Python aggregation scripts. The skill
prompt instructs Claude to:
- Use Asana MCP for parent/subtask discovery and the final write
- Use Atlassian MCP (one batch JQL) for the Jira activity window
- Use `gh` CLI for GitHub PRs
- Use the existing `meta-tempo-log/bin/tempo_api.py` for Tempo
- Use the existing `meta-tempo-log/bin/scan_sessions.py` for cross-repo
  session signal

The only state the skill keeps in `~/.local/share/` is none — Asana itself
is the source of truth (subtask history is persistent).

## Files

- `SKILL.md` — workflow specification (read by Claude when skill is invoked)
- `config.json` — your personal config (gitignored)
- `config.json.example` — template (committed)
- `README.md` — this file
