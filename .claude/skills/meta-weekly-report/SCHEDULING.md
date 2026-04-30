# Scheduling guide for meta-weekly-report

**Current state:** the skill runs manually — invoke `/meta-weekly-report` in
Claude Code each week, review the draft, type `yes` to submit. Zero
infrastructure.

This doc captures what we evaluated and how to re-enable scheduling later.

---

## Decision tree

```
                Do you want unattended Thursday automation?
                              |
              ┌───────────────┴───────────────┐
              No                             Yes
              |                               |
        Stay manual                Is your Mac on most
       (current state)             Thursday evenings?
                                          |
                              ┌───────────┴───────────┐
                              Yes                     No
                              |                        |
                       Local launchd            Remote routine
                       (full data)             (degraded data)
```

---

## Option A: Local launchd (full data, requires Mac on)

**When this is right**
- Your Mac is on or in light sleep most Thursday evenings (~9pm IST)
- You want full report quality (Tempo comments + Jira + sessions + GHES PRs)
- You're OK with launchd catching up on Friday morning if Thursday was missed

**What to set up**

1. Wrapper script at `~/bin/run-meta-weekly-report.sh`. Sets PATH, cd's to
   CoS, invokes claude. See git history for the prior version (commit
   reference: search for `run-meta-weekly-report`).

2. LaunchAgent plist at
   `~/Library/LaunchAgents/com.nshelke.meta-weekly-report.plist`. Fires
   Thursday 21:00 local time via `StartCalendarInterval` with
   `Weekday: 4, Hour: 21, Minute: 0`.

3. Set `config.json` `schedule.scheduled_mode_auto_submit: true` so the
   skill skips the chat approval gate when invoked with `--scheduled`.

4. Bootstrap the agent:
   ```
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.nshelke.meta-weekly-report.plist
   ```

5. Verify:
   ```
   launchctl list | grep meta-weekly-report
   ```

**Known blocker (must solve first)**

When we tried this on 2026-05-01, `claude --print --dangerously-skip-permissions
"/meta-weekly-report --scheduled"` produced no visible output. Either claude
hangs waiting for input, MCP setup, or permission, OR it exits silently
because the slash command isn't being parsed in `--print` mode.

**Diagnostics to run before scheduling:**

```bash
# A. Does --print work at all?
cd ~/workspace/CoS
claude --print "What is 2+2?"

# B. Does --print see your skills?
claude --print "List the skills available to you that start with meta-"

# C. Direct skill invocation
claude --print "/meta-weekly-report --scheduled" 2>&1 | head -100

# D. With debug
claude --print --debug "api,mcp" "/meta-weekly-report --scheduled" 2>&1 | tail -100
```

If A works but C doesn't, the slash-command-in-print path isn't supported —
phrase the prompt as natural language:
`claude --print "Run the meta-weekly-report skill in scheduled auto-submit mode"`

If anything hangs, try without `--dangerously-skip-permissions`. That flag
may interfere with MCP loading despite not being documented to.

**Sleep behavior**
- Mac awake or in light sleep at 21:00 → fires
- Mac asleep on battery + lid closed → skipped, **launchd catches up on next wake**
- Mac fully shut down → skipped entirely, no catch-up
- Optional: `sudo pmset repeat wake R 20:55:00` wakes the Mac at 20:55 every
  weekday so the 21:00 launchd job has a 5-minute lead. Plugged-in Macs only.

---

## Option B: Remote routine (degraded data, fully unattended)

**When this is right**
- You travel often or your Mac is unpredictable on Thursday nights
- You're OK with thinner reports (Tempo, sessions, GHES PRs unavailable)
- You're willing to do the claude.ai connector setup

**Setup steps**

1. Connect Atlassian Rovo at https://claude.ai/customize/connectors. Grant
   Jira read scope. Note the connector UUID.

2. Verify Asana connector still has write scope (currently does).

3. Push CoS repo to github.com (the routine clones it). Make sure latest
   skill is committed.

4. Adapt the skill for "remote-degraded" mode:
   - Skip Step 4a (Tempo) — no token in remote sandbox, no MCP exists
   - Skip Step 4c (GHES PRs) — no auth for `github.groupondev.com`
   - Skip Step 4d (sessions) — `~/.claude/projects/` is local-only
   - Keep Steps 4b (Jira via Rovo MCP), 4e (git log of `.claude/`), and the
     full Asana read/write
   - Note in the rendered report: "Sources: Jira + Asana + .claude/ git log"

5. Create the routine (cron `30 15 * * 4` = Thursday 15:30 UTC = 21:00 IST):
   ```
   /schedule
   ```
   Then attach the Asana + Atlassian Rovo connectors via the routine's
   `mcp_connections` field.

6. Set `config.json` `schedule.scheduled_mode_auto_submit: true`.

**Report quality with this setup**

Approximately 50-65% of local-quality reports. The big losses are:
- No per-ticket Tempo comments (which were the strongest signal in the 2026-04-30 dry run)
- No cross-repo session signals (lose tickets that were worked on but had no formal Jira/Tempo activity)
- No GHES PR data

**To recover Tempo data without local launchd**

Option B+: modify `meta-tempo-log/bin/submit.py` to also write the day's
audit data to `data/tempo-week-YYYY-WW.csv` in the CoS repo and commit/push
it after each successful submit. Then add a Step 4a-remote in
meta-weekly-report that reads this CSV instead of calling Tempo API. This
gets you back to ~85% report quality at the cost of having your weekly
Tempo activity visible in the repo's git history.

---

## What we ruled out

| Mechanism | Why not |
|---|---|
| `CronCreate` (Claude Code in-session cron) | Requires Claude session running at fire time; recurring jobs auto-expire after 7 days |
| `/loop` skill | Same constraint — only fires while session is open |
| GitHub Actions | Has secrets API, but requires rewriting the skill as a Python script (~200 lines) and loses chat ergonomics. Heavy for a single weekly task. |
| `.env` file in repo | Either committed (security risk) or gitignored (routine can't see it). Encrypted-at-rest needs a decryption key, which is the same problem one level deeper. |

---

## Skill plumbing already in place

The skill code already supports scheduled mode — only the OS-level scheduler
is missing. Specifically:

- `SKILL.md` Mode D section explains `--scheduled` flag detection
- `config.json` has `schedule.scheduled_mode_auto_submit` flag (currently
  `false`)
- Approval gate logic in SKILL.md branches on the flag

To re-enable, the only changes needed are:
- Set `scheduled_mode_auto_submit: true` in `config.json`
- Set up the OS-level scheduler (launchd plist + wrapper, or remote routine)

---

## Quick-reference commands

```bash
# Manual invocation (current default workflow)
# In Claude Code chat or terminal:
/meta-weekly-report

# Preview without submitting
preview 5/15

# After scheduling is set up — run the scheduled job on demand
launchctl kickstart -k gui/$(id -u)/com.nshelke.meta-weekly-report

# Stop the schedule without deleting files
launchctl bootout gui/$(id -u)/com.nshelke.meta-weekly-report

# See what scheduled jobs are loaded
launchctl list | grep meta-weekly-report

# View latest run logs (after schedule is active)
tail -f ~/Library/Logs/meta-weekly-report.log
```
