---
name: meta-sprint-bootstrap
description: Auto-create the recurring SFDC sprint-ceremony tickets for the upcoming sprint. Resolves the next Jira sprint, looks up the dev on-call rotation from JSM Operations for the 14-day sprint window, and creates 2 Dev On-Call tickets plus 12 SFDC support case management tickets (6 per admin) all assigned to the upcoming sprint. Always shows a preview and waits for explicit approval before any Jira writes.
---

# meta-sprint-bootstrap

Automates the static sprint-ceremony ticket creation that the SFDC team
manually does at the start of every 2-week sprint. Per sprint:

- 2 Dev On-Call tickets, 8h each, assigned to the two devs on-call for the
  upcoming sprint window (looked up from JSM Operations).
- 12 SFDC Support Case Management tickets, 8h each: 6 for Srilakshmi K S
  and 6 for Utkarsh Pathak.

All 14 tickets are added directly to the upcoming Jira sprint.

## When to use

- User says: "bootstrap sprint", "create sprint tickets", "/meta-sprint-bootstrap"
- Run on Wednesday or Thursday morning, before sprint planning on Thursday.
- Idempotency: re-running detects already-created tickets for the same sprint
  and skips them (see Step 7).

## Prerequisites

- Atlassian MCP authenticated (`mcp__atlassian__*`)
- Atlassian API token in macOS Keychain
  (`security add-generic-password -s atlassian_api_token -a nshelke -w <token>`)
  or `ATLASSIAN_API_TOKEN` env var.
- `config.json` filled in. On first run, the on-call `team_id` and
  `schedule_id` will be empty: the workflow detects this and runs the
  discover step before proceeding.

## Modes

### Mode A — Default: `/meta-sprint-bootstrap`

Auto-discover next sprint, look up on-call, build the 14 tickets, show a
preview, ask for approval, create on `yes`.

### Mode B — Specific sprint: `/meta-sprint-bootstrap sprint=<id>`

Override the auto-discovered sprint with an explicit sprint ID. Useful for
back-filling a missed sprint or re-running for a specific upcoming one.

### Mode C — Dry-run: `/meta-sprint-bootstrap preview`

Build the preview but never call `mcp__atlassian__createJiraIssue`, even
on `yes`. Use to validate the on-call lookup and naming before committing.

### Mode D — Discover only: `/meta-sprint-bootstrap discover`

Run only the JSM Ops discovery (list teams + schedules + their IDs) and
print to chat. Used at first-time setup to populate `config.json`'s
`jsm_ops.team_id` and `jsm_ops.schedule_id`.

## Workflow

### Step 1 — First-run discovery (skip if config already populated)

If `config.jsm_ops.schedule_id` is empty:

```bash
python3 .claude/skills/meta-sprint-bootstrap/bin/jsm_oncall.py discover
```

Show the user the list of teams + schedules. Ask which one tracks the
SFDC dev on-call rotation. On answer, write `team_id` and `schedule_id`
into `config.json` and continue.

If `config.atlassian_email` looks unset, prompt the user to confirm or
update before continuing.

### Step 2 — Resolve next sprint

Hit the Jira Agile API directly (the JQL `futureSprints()` approach
returns ticket-level sprint references, not the sprint metadata we need):

```bash
GET https://groupondev.atlassian.net/rest/agile/1.0/board/<board_id>/sprint?state=active
GET https://groupondev.atlassian.net/rest/agile/1.0/board/<board_id>/sprint?state=future&maxResults=100
```

Auth: same `ATLASSIAN_API_TOKEN` Keychain entry used by `jsm_oncall.py`,
Basic Auth with `atlassian_email`.

**Selection logic:**
1. From `state=active` find the current sprint. Parse its name with regex
   `\[(\d{2}Q\d)\] SFDC (\d+)(?:st|nd|rd|th) Sprint` to extract
   `current_quarter` and `current_ordinal`.
2. From `state=future` find sprints whose name matches the same regex.
   Most "future" sprints on the board are NOT time-bound (they are backlog
   buckets like "Pending Review", "Ready for Prioritization: High"). Only
   pick the ones whose name matches `[QQYY] SFDC <N>(st|nd|rd|th) Sprint`.
3. Pick the future sprint with `ordinal = current_ordinal + 1`. That is
   the target.
4. If no such sprint exists: stop with "no upcoming sprint found - sprint
   planning hasn't created Sprint <N+1> on the board yet."

**Date inference (important):** Jira does NOT populate `startDate` /
`endDate` on future sprints until the sprint is actually started during
planning. So target sprint's `startDate` will usually be missing. Compute:

```
target.startDate = current.endDate          (Sprint 3 begins when Sprint 2 ends)
target.endDate   = target.startDate + 14 days
```

**Final values used downstream:**
- `target.id` (numeric — set as the sprint custom field on each new ticket)
- `target.name` (used for the quarter / ordinal parsing)
- `target.startDate` (passed to `jsm_oncall.py weekly --sprint-start`)
- `target.endDate` (used in the preview header for the user)

**Sanity check the quarter:** `current_quarter` from the active sprint
name should equal `config.current_quarter.label`. If they differ (e.g.
config says `26Q2` but active sprint name says `26Q3`), warn loudly and
ask the user to update the config first. Do NOT proceed silently — the
parent epic linkage would be wrong.

### Step 3 — Look up dev on-call for the sprint window

```bash
python3 .claude/skills/meta-sprint-bootstrap/bin/jsm_oncall.py weekly \
  --schedule-id <config.jsm_ops.schedule_id> \
  --sprint-start <sprint.startDate as YYYY-MM-DD>
```

This returns a JSON blob with `week1` and `week2`, each containing `name`
and `email` of the on-call participant for that week.

Resolve each email to a Jira account ID using
`mcp__atlassian__lookupJiraAccountId`. If the email lookup returns nothing,
fall back to `mcp__atlassian__searchJiraIssuesUsingJql` with `assignee =
"<email>"` and read `assignee.accountId` from the first result.

If both weeks resolve to the same dev: use the `-1` and `-2` suffix from
`config.name_formats.dev_oncall_split_suffix` to disambiguate the two
tickets (matches the SFDC-9503/SFDC-9504 historical pattern).

Print to chat:
```
On-call for upcoming sprint <sprint.name>:
  Week 1 (<startDate> to <startDate+7d>): <Dev A> (<email>)
  Week 2 (<startDate+7d> to <endDate>): <Dev B> (<email>)
```

If user objects, allow override via the `edit` flow in Step 6.

### Step 4 — Resolve admin account IDs (cache in config)

If any `admins[].account_id` is empty, look up via
`mcp__atlassian__lookupJiraAccountId` and write back into config.json so
subsequent runs skip the lookup.

### Step 5 — Validate parent epics

Verify both epics in `config.current_quarter` exist and are in the
configured project. Use `mcp__atlassian__getJiraIssue` for each:
- `ktlo_epic_key` (parent for dev on-call tickets)
- `operational_epic_key` (parent for admin tickets)

Sanity check: epic summary should contain `config.current_quarter.label`.
If it doesn't (e.g. config still says `26Q2` but the epic's quarter is
`26Q3`), warn the user clearly — quarter rollover.

### Step 6 — Build and preview the 14 ticket payloads

Generate the payloads in this order:

#### A. Dev On-Call (2 tickets)

For each week (1, 2):
```
{
  "summary":  fmt(config.name_formats.dev_oncall, {
    quarter_label:  config.current_quarter.label,
    sprint_ordinal: <"3rd" derived from sprint_number>,
    dev_name:       <on-call dev's display name>
  }) + (same_dev ? fmt(dev_oncall_split_suffix, {week}) : ""),
  "description":   fmt(config.description_formats.dev_oncall, {
    week:           <1 or 2>,
    quarter_label:  config.current_quarter.label,
    sprint_ordinal: <"3rd">
  }),
  "issuetype":     "Task",
  "parent":        config.current_quarter.ktlo_epic_key,
  "assignee":      <dev account_id>,
  "originalEstimate": "8h",
  "labels":        config.labels.dev_oncall,
  "sprint":        <next_sprint.id>
}
```

#### B. SFDC Support Case Management (12 tickets)

For each admin in `config.admins`, for `ticket_index` in 1..6:
```
{
  "summary": fmt(config.name_formats.admin, {
    quarter_label_short: config.current_quarter.label_short,
    sprint_number:       <sprint_number>,
    ticket_index:        <ticket_index>
  }),
  "description": fmt(config.description_formats.admin, {
    quarter_label:    config.current_quarter.label,
    sprint_ordinal:   <"3rd">,
    sprint_window:    "<sprint.startDate> to <sprint.endDate>",
    ticket_index:     <ticket_index>,
    tickets_per_admin: config.tickets_per_admin_per_sprint,
    admin_name:       <admin display name>
  }),
  "issuetype":     "Task",
  "parent":        config.current_quarter.operational_epic_key,
  "assignee":      <admin account_id>,
  "originalEstimate": "8h",
  "labels":        config.labels.admin,
  "sprint":        <next_sprint.id>
}
```

Render the preview as a markdown table grouped by category:

```
Sprint: <sprint.name>  (id: <sprint.id>, <startDate> -> <endDate>)
Quarter epics:
  KTLO:        <ktlo_epic_key>  <ktlo_epic_summary>
  Operational: <operational_epic_key>  <operational_epic_summary>

DEV ON-CALL (2 tickets)
| # | Summary                                                      | Assignee    | OE | Sprint |
| 1 | [26Q2] Dev On-Call - SFDC 3rd Sprint - Alice                 | alice@...   | 8h | <id>   |
| 2 | [26Q2] Dev On-Call - SFDC 3rd Sprint - Bob                   | bob@...     | 8h | <id>   |

ADMIN SUPPORT CASE MANAGEMENT (12 tickets)
| # | Summary                                            | Assignee  | OE |
| 3 | Q2' Salesforce support cases management S3/1       | sriks@... | 8h |
| ...
| 14| Q2' Salesforce support cases management S3/6       | upathak@..| 8h |

Create 14 tickets in <project>?  (yes / edit / cancel)
```

`edit` accepts free-form changes (e.g. "swap week 1 to bob@", "use epic
SFDC-XXXX for admins"); re-render and re-ask. `cancel` exits cleanly.

### Step 7 — Idempotency check (run before any creates)

Before creating, query Jira for any existing ticket in the upcoming sprint
that matches our naming pattern:

```jql
project = SFDC AND sprint = <next_sprint.id> AND (
  summary ~ "Dev On-Call - SFDC <sprint_ordinal> Sprint"
  OR summary ~ "Salesforce support cases management S<sprint_number>"
)
```

For each existing ticket: skip the corresponding payload, log
`[skip] <expected-summary> already exists as <existing-key>`.

### Step 8 — Create tickets

For each remaining payload, call `mcp__atlassian__createJiraIssue`. The
sprint custom field name is tenant-specific (typically
`customfield_10020`). Discover via `mcp__atlassian__getJiraIssueTypeMetaWithFields`
on first run if needed.

Process serially (not parallel) so any failure is easy to diagnose. On
failure for a single ticket, log the error and continue with the rest.

### Step 9 — Report

Print summary:
```
Created N / Skipped M / Failed K tickets for sprint <sprint.name>.

Created:
  SFDC-XXXX  [26Q2] Dev On-Call - SFDC 3rd Sprint - Alice
  ...

Skipped (already exist):
  SFDC-YYYY  Q2' Salesforce support cases management S3/1 (assignee Utkarsh)

Failed:
  - <expected summary>: <error reason>

Sprint board: https://groupondev.atlassian.net/jira/software/c/projects/SFDC/boards/<board_id>
```

## Approval gate

Per CoS approval-gates rules:
- Creating Jira tickets is a write to a deliverable visible to the team.
- Always show the full 14-ticket preview before any create call.
- One `yes` covers the whole batch; do not re-ask per ticket.
- Idempotency check (Step 7) acts as a second safety net.
- Mode C (`preview`) is available to dry-run without any writes.

## Error handling

- **Missing API token**: tell user to set
  `security add-generic-password -s atlassian_api_token -a nshelke -w <token>`,
  exit. Do NOT prompt for the token in chat.
- **JSM Ops API returns 401/403**: token has insufficient scope. Tell user
  to regenerate at id.atlassian.com with full Atlassian Cloud scopes.
- **No future sprint on board**: stop early; user needs to create the
  upcoming sprint on the SFDC scrum board first.
- **Quarter label mismatch in config vs sprint name**: warn loudly, ask
  the user to update `config.current_quarter` first. Do NOT proceed
  silently — wrong epic linkage.
- **Email -> account_id lookup fails**: fall back to JQL `assignee =
  "<email>"` first-result trick. If still nothing: ask user to paste the
  account_id manually.
- **Rate-limited by Atlassian API**: backoff and retry once. If still
  failing, abort with clear message.

## Output shape (end of skill run)

Same shape as `meta-tempo-log` for consistency:

```
Bootstrapped sprint <sprint.name> (id <sprint.id>):
  Created: 14 / Skipped: 0 / Failed: 0
  Sprint board: <url>

Tickets:
  SFDC-XXXX  [26Q2] Dev On-Call - SFDC 3rd Sprint - <name>
  ...
```

## Quarterly maintenance

Once per quarter (when the new KTLO and Operational epics are created),
update `config.json`:

```json
"current_quarter": {
  "label":                 "26Q3",
  "label_short":           "Q3",
  "ktlo_epic_key":         "SFDC-XXXX",
  "operational_epic_key":  "SFDC-YYYY"
}
```

Skill warns automatically (Step 5) if the sprint name's quarter doesn't
match the config — that's the trigger to update.
