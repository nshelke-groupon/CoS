---
name: meta-tempo-log
description: Log time to Tempo from Jira tickets. Supports explicit mode (ticket + hours), context mode (hours only, ticket inferred), and auto-draft from calendar + Jira activity + git + session history. Enforces an 8h/day minimum (floor, not cap) starting 08:00 IST with overlap checks. Always shows draft for confirmation before submission.
---

# meta-tempo-log

Log worklogs to Tempo Cloud for the current user. Wraps the Tempo REST API
(via local Python scripts) and reuses the atlassian + google-workspace MCPs
for reading Jira / Calendar data.

## When to use

- User says "log time", "log tempo", "add worklog", "tempo for today/yesterday"
- User gives a ticket + duration explicitly: "log 3h to SFDC-10201"
- User gives just duration: "log 2h" (ticket inferred from session/branch)
- User asks for an auto-draft: "draft tempo for yesterday"

## Prerequisites

- Tempo API token stored in macOS Keychain (`security add-generic-password -s tempo_api_token -a nshelke -w <token>`) or exported as `TEMPO_API_TOKEN`
- `~/.local/share/meta-tempo-log/` exists (the skill creates it on first submission)
- Config at `.claude/skills/meta-tempo-log/config.json` — atlassian_account_id, TZ, on-call rules

## Paths

- Skill dir: `.claude/skills/meta-tempo-log/`
- Scripts: `bin/tempo_api.py`, `bin/allocate.py`, `bin/submit.py`
- Config: `config.json` (loaded by scripts)
- Personal data: `~/.local/share/meta-tempo-log/meeting-mappings.json`, `audit.csv`

## Modes

### Mode A — Explicit: "log {duration} to {TICKET} [comment]"

1. Parse ticket key and duration (accept `3h`, `1h 30m`, `90m`, `1.5h`).
2. Resolve ticket to Jira issue **ID** (numeric) via atlassian MCP:
   `mcp__atlassian__getJiraIssue` → read `.id` field.
3. Pick start time:
   - Fetch existing worklogs for today: `python3 .claude/skills/meta-tempo-log/bin/tempo_api.py list --account-id <id> --from <today> --to <today>`
   - Find the earliest non-overlapping slot ≥ 08:00.
4. Generate a comment:
   - If user provided one, use it.
   - Else infer from current session (what we've been working on) — write a single short human sentence, not raw context.
   - Else fall back to the Jira ticket summary.
5. Show the draft to the user (see **Draft format** below).
6. On approval, submit via `submit.py` (stdin JSON). Report the worklog ID.

### Mode B — Context: "log {duration}" (ticket inferred)

Same as Mode A, but ticket is inferred in this priority:
1. Last ticket key mentioned in current session (SFDC-XXXX, ADMIN-XXXX, etc.)
2. Current git branch name (`feat/SFDC-10201` → `SFDC-10201`)
3. Ask the user.

### Mode C — Auto-draft: "draft tempo for {today | yesterday | YYYY-MM-DD}"

1. **Resolve target date** (IST). "Yesterday" = calendar yesterday in IST.
2. **Gather data in parallel**:
   - **Calendar events** via `mcp__google-workspace__manage_calendar` — primary calendar, full day range, include description/location/responseStatus.
   - **Jira activity** via `mcp__atlassian__searchJiraIssuesUsingJql`:
     ```
     (assignee = currentUser() OR worklogAuthor = currentUser()
      OR commentedBy = currentUser() OR status changed by currentUser() DURING ("YYYY-MM-DD", "YYYY-MM-DD+1"))
     ```
     Also list issues you updated: `updated >= "YYYY-MM-DD" AND updated < "YYYY-MM-DD+1" AND (assignee = currentUser() OR reporter = currentUser())`.
   - **Git activity** via Bash:
     ```
     git -C /Users/nshelke/workspace/gso-sf-team-ai log --all --author=nshelke \
       --since="YYYY-MM-DD 00:00 IST" --until="YYYY-MM-DD+1 00:00 IST" \
       --pretty=format:'%h|%s|%ad' --date=iso
     ```
     Repeat for each submodule (sf/SFDC, gso/cs-api).
   - **Session history** — call `python3 .claude/skills/meta-tempo-log/bin/scan_sessions.py --date YYYY-MM-DD`. The helper walks **every** `~/.claude/projects/*/*.jsonl` (every Claude Code project on this machine, not just the current repo) and aggregates Jira ticket mentions whose message `timestamp` falls inside the target IST day. Returns `{ticket: {count, sessions, cwds}}` so you can see which repos discussed each ticket. Any local Claude Code project counts — sandboxes, personal repos, anywhere a `[A-Z]+-\d+` token appeared in conversation.
3. **PTO short-circuit**: if any calendar event matches `config.pto_patterns` and covers >= 4h of the day, build a PTO plan (8h on `pto_fallback_ticket`) and skip the rest of the flow.
4. **Extract meeting tickets** — calendar events are the **primary skeleton of the day**:
   - Do NOT filter events by time-of-day. There is no implicit "workday window" (08:00–16:00 or similar). The day is defined by the events, not by a notional office schedule. A 15-min meeting at 19:30 is still a loggable event.
   - Only skip when an event matches one of: declined response, `excluded_meeting_patterns`, all-day events that are location markers (e.g. "Home", "WFH") unless they match `pto_patterns`, or duration < `min_meeting_minutes`.
   - Extract ticket key via regex `[A-Z]+-\d+` in: summary → description → location. First match wins.
   - For recurring events (same `iCalUID` or recurrence), check `meeting-mappings.json` first.
   - Unknown tickets → mark the meeting with `ticket = null` and surface it inline in the draft under "Unresolved" — NEVER drop the event silently. Ask the user for the ticket before submission.
5. **Detect dev on-call ticket**:
   - JQL: `assignee = currentUser() AND sprint in openSprints() AND (summary ~ "dev on call" OR summary ~ "doc" OR summary ~ "on-call" OR description ~ "dev on call" OR description ~ "doc")`
   - If found AND target date is in `on_call.applies_on` AND total existing coverage on that ticket < `min_hours_per_weekday`: schedule `on_call_topup = {ticket, hours: diff}`.
6. **Build activity weights** for proportional fill:
   - Per ticket, count: commits × 3, Jira updates (status/comment/edit) × 1, PR events (create/comment/approve) × 2.
   - Include every Jira ticket mentioned in session transcripts (weight = 1 if no other signal).
   - **Generic / non-Jira work** — when session/commit signal clearly maps to exploration but no SFDC ticket is referenced, route to one of the `exploration_tickets` in `config.json`:
     - AI-specific exploration (skill building, MCP, AI automation, prompt engineering) → **SFDC-10142** (`ai_exploration`)
     - General exploration / KTLO (tooling cleanup, maintenance, admin chores) → **SFDC-10198** (`ktlo`)
     - Each exploration bucket is capped at `max_hours_per_day` (default 1h). If a day's signal would exceed the cap, trim to the cap and either spill overage to the most-weighted real ticket or surface as unresolved.
     - Never assign to an exploration ticket when a real SFDC/MTG/etc. ticket is already in scope for the same activity — exploration tickets are a fallback only.
7. **Fetch existing Tempo worklogs** for the date:
   `python3 .claude/skills/meta-tempo-log/bin/tempo_api.py list --account-id <id> --from <date> --to <date>`
   - Pass these into `allocate.py` as `existing_worklogs` — they become immutable fixed slots (`source: existing`), so the allocator will never place new activity over them.
   - Render them in the draft table with `source = existing` so the user sees exactly what is already in Tempo and what's being added on top. Total = existing + new; only new entries are submitted.
7.5. **Fetch Original Estimate + already-logged time** for every unique ticket in the plan (`activity_weights ∪ meetings ∪ on_call_topup`). For each ticket call `mcp__atlassian__getJiraIssue` with `fields=["timetracking"]` and read `.fields.timetracking.originalEstimateSeconds` (may be null) and `.fields.timetracking.timeSpentSeconds` (may be null — fall back to 0). Build `ticket_caps`:
   ```json
   {"SFDC-10182": {"original_estimate_seconds": 28800, "logged_seconds": 18000}}
   ```
   Tickets with no estimate set (`null`) are passed through with no cap and a soft warning in the draft (`(no estimate)`).
   - **Hard block:** This is the source of truth for the "total time per ticket ≤ Original Estimate" rule. The allocator will trim weights so the draft fits under each cap; `submit.py` re-checks at submit time as the last-line-of-defense.
   - Always **fetch fresh** at draft time — don't cache. Estimates can change while the user is reviewing the draft.
8. **Run allocate.py** with the assembled request (piped via stdin). Pass `ticket_caps` and `max_entry_minutes` (default 120 from config). Returns a non-overlapping plan with two new behaviors:
   - **Cap enforcement** — any ticket whose proposed allocation would exceed `(original_estimate_seconds - logged_seconds)` is trimmed; spillover routes to the next-highest-weighted ticket that has cap headroom (or stays unaccounted, surfaced as a warning). Trimmed tickets show up in `cap_warnings` in the output — render them in the draft.
   - **2h-per-entry split** — any single non-meeting / non-existing entry longer than `max_entry_minutes` (default 120) is split into back-to-back chunks ≤ 120 min. Same ticket, descriptions get a `(n/N)` suffix. Meetings are not split (a 3h calendar event = one 3h entry).
   - `daily_hours_minimum` is still a **floor, not a cap** — the day may exceed it when fixed meetings plus activity signals warrant. Do not trim meetings or valid activity buckets to stay under 8h. Activity fill only pads up to the floor; if fixed slots already reach or exceed the floor, no padding is added but nothing is removed either.
9. **Generate comments** per entry — aim for **4–5 lines**, explanatory, highlighting the actual work done AND outcomes. Never paste raw conversation content, but do synthesize concretely from commits, Jira updates, PR titles, and session context.
   - **Meetings**: meeting title on line 1; if substantive (grooming, planning, design review), a 2-3 line summary of what was decided or discussed when session context supports it. Short stand-ups can stay at 1 line.
   - **Activity buckets**: 4–5 lines covering (1) what was worked on, (2) key changes / decisions, (3) outcome or state reached (merged PR, committed fix, skill released), (4) any follow-ups. Example:
     ```
     Fixed sfdx-git-delta CI installation failure on groupon-runner-sets-xl.
     Switched from npx to `npm ci` against a committed package-lock.json so the
     binary resolves via ./node_modules/.bin/sgd without PATH corruption.
     Removed the dead install-delta-plugin workflow input and updated docs.
     Outcome: validate-pr.yml now green across 3 consecutive PR pushes; escalation
     paths captured in SFDC-10228 if the approach regresses.
     ```
   - **On-call top-up**: `"Dev on-call coverage"` on line 1, followed by **all Google Chat thread links where the user was @mentioned on the target date**, one per line. If Google Chat access is unavailable, ask the user to paste the thread URLs inline before submission — do NOT submit without them.
   - **PTO**: `"PTO / Out of office"`.
   - **Exploration tickets (SFDC-10142, SFDC-10198)**: 4–5 lines describing the specific exploration/KTLO activity, what was learned or improved, and any artifacts produced (skills, docs, configs).
10. **Resolve issue IDs** for every ticket in the plan via atlassian MCP in a single batch (reuse the already-fetched Jira issues where possible).
11. **Present draft** (see format below) and list any unresolved meetings inline. Always show existing Tempo entries as `existing` rows at the top of the table so the user can see total coverage (existing + new) at a glance. Never let the total exceed 24h; never let new activity overlap existing Tempo entries.
12. On approval → call `submit.py`. Report submitted / skipped / failed counts.

## Draft format

Render a markdown table. Example:

```
Draft for 2026-04-22 (IST) — target 8h, planned 8h 00m

| Time         | Ticket     | Duration | Source       | Comment                                  |
|--------------|------------|----------|--------------|------------------------------------------|
| 08:00-09:00  | SFDC-10201 | 1h 00m   | git+session  | Updated test-standards with Assert.*     |
| 09:00-10:00  | SFDC-9876  | 1h 00m   | on-call      | Dev on-call coverage                     |
| 10:00-11:00  | SFDC-9500  | 1h 00m   | meeting      | Sprint planning                          |
| 11:00-12:00  | SFDC-10150 | 1h 00m   | jira         | Reviewed PR #34 comments                 |
| 12:00-13:00  | —          | skipped  | lunch        | (not logged)                             |
| 13:00-14:00  | SFDC-10000 | 1h 00m   | meeting      | 1:1 with manager                         |
| 14:00-17:00  | SFDC-10201 | 3h 00m   | session+git  | Implemented Phase 1 tempo-log skill      |

⚠ Unresolved:
  • Meeting "Design sync" 14:00-14:30 has no ticket key — which ticket?
  • 0 gap minutes remain to allocate.

Existing Tempo entries for today: none.
```

Then ask: **"Submit N entries? (yes / edit / cancel)"**

If `edit`, accept freeform changes (ticket swaps, time adjustments, comment edits) and re-render.

## Submission

Build the `submit.py` stdin payload:

```json
{
  "target_date": "2026-04-22",
  "account_id": "<from config.atlassian_account_id>",
  "entries": [
    {
      "ticket_key": "SFDC-10201",
      "issue_id": 12345,
      "start": "08:00",
      "end": "09:00",
      "description": "Updated test-standards with Assert.* syntax"
    }
  ],
  "dry_run": false
}
```

Run: `cat payload.json | python3 .claude/skills/meta-tempo-log/bin/submit.py`

Report: total submitted, skipped (duplicates), failed (with reasons). Share
the audit CSV path once: `~/.local/share/meta-tempo-log/audit.csv`.

## Learning loop — meeting mappings

When the user resolves an unknown meeting ticket during draft review, and
the meeting is **recurring** (has an `iCalUID` that appeared in last 30 days):
ask "Remember 'Design sync' → SFDC-XXXX for next time? (yes/no)". If yes,
append to `~/.local/share/meta-tempo-log/meeting-mappings.json`:

```json
{
  "mappings": [
    {"iCalUID": "...", "title": "Design sync", "ticket": "SFDC-XXXX",
     "added": "2026-04-22", "hit_count": 1}
  ]
}
```

On subsequent runs, consult this map before prompting the user.

## Approval gate

This skill creates worklogs — under **Gate 5 (Salesforce)** / **Gate 8 (irreversible)**
of `approval-gates.md`. Every submission requires explicit `yes` from the user
after seeing the full draft. `--yes` / auto-submit flags are not supported.

A single batch of N entries counts as one approval. Do not re-ask per entry.

## Error handling

- **Tempo API rejects `issueId`**: user may lack permission on that project. Surface the HTTP body from stderr verbatim. Skip that entry, continue with others.
- **Overlap detected in final plan** (sanity check): abort submission, show the plan, tell the user which entries collide.
- **Calendar/Jira MCP not authenticated**: tell user to `/mcp` and retry. Don't proceed with partial data.
- **Duplicate detected at submit time**: `submit.py` reports it as `skipped`. This is normal on re-runs — show the count but don't treat as error.

## Dry-run

If the user says "dry run" or "preview only", set `dry_run: true` in the
submit payload. No API writes; `submitted` entries include `dry_run: true`
and the audit CSV is not touched.

## Output shape (end of skill run)

```
Submitted 6 entries to Tempo for 2026-04-22.
Skipped 0 duplicates. Failed 0.
Audit: ~/.local/share/meta-tempo-log/audit.csv
```
