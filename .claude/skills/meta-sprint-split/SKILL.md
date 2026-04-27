---
name: meta-sprint-split
description: Split oversized Jira tickets in the active SFDC sprint into smaller actionable tickets. Finds the active SFDC sprint, scans tickets assigned to the current user, and splits any with Original Estimate > 8h into balanced pieces (each ≤ 8h) while keeping cumulative estimate identical. Creates new standalone tickets (not subtasks), copies key fields, links new tickets back to the primary, and posts a short reference comment on the primary.
---

# meta-sprint-split

Split oversized Jira tickets in the active SFDC sprint into smaller actionable tickets, each ≤ 8h, preserving the cumulative original estimate.

## When to use

- User says "split my sprint tickets", "split oversized tickets", "split sprint", "break down sprint tickets"
- User invokes `/meta-sprint-split`
- User pastes a ticket list and asks to split the ones > 8h

---

## Core rules

- **8h cap** — every ticket after split must have Original Estimate ≤ 8h.
- **Preserve total** — sum of split pieces (including the primary after its estimate is reduced) must equal the primary's original estimate exactly. No rounding loss.
- **Leave small tickets alone** — tickets with estimate ≤ 8h or with no estimate are not touched.
- **Not subtasks** — new tickets are standalone issues of the same `issuetype` as the primary, not Sub-tasks.
- **Assigned to current user** — new tickets inherit the primary's assignee; skill only operates on tickets assigned to the invoking user.
- **Same sprint** — new tickets are added to the same active sprint as the primary.
- **Primary keeps its ID** — the primary is updated in place (estimate reduced), and N−1 new tickets are created. The primary always takes the first chunk.

---

## Prerequisites

- Atlassian MCP authenticated (`mcp__atlassian__*` tools available). If not, run `/meta-doctor` first.
- User's Atlassian `accountId` known — skill reads it from `.claude/skills/meta-tempo-log/config.json` (`atlassian_account_id`) if present, otherwise calls `mcp__atlassian__atlassianUserInfo` to resolve.

---

## Flow

### Step 1 — Resolve Cloud ID and user

- Call `mcp__atlassian__getAccessibleAtlassianResources` once. Pick the `groupondev.atlassian.net` resource and cache its `id` as `cloudId` for all subsequent calls.
- Read `atlassian_account_id` from `meta-tempo-log/config.json`, or resolve via `mcp__atlassian__atlassianUserInfo`.

### Step 2 — Find the active SFDC sprint

Run JQL via `mcp__atlassian__searchJiraIssuesUsingJql` to discover the active sprint ID:

```
project = SFDC AND sprint in openSprints()
```

- Fetch only 1 issue, with `fields: ["customfield_10105"]` (Sprint field ID in Groupon's Jira — verified). From that issue's `customfield_10105` array, pick the entry where `state == "active"` and extract `id` and `name`.
- If no active sprint exists, abort with a clear message.
- If more than one sprint is active on a single board, list them and ask the user which one.

### Step 3 — Gather candidate tickets

JQL:

```
project = SFDC
  AND sprint = <active_sprint_id>
  AND assignee = currentUser()
  AND statusCategory != Done
  AND issuetype != Sub-task
  AND originalEstimate > 8h
```

Fetch fields: `summary, issuetype, status, priority, labels, components, fixVersions, description, assignee, timeoriginalestimate, customfield_10105` (Sprint), `customfield_12000` (Epic Link — legacy string), and `parent` (modern Epic parent — preferred).

If zero candidates: tell the user "Nothing to split — all assigned sprint tickets are ≤ 8h." and exit cleanly.

### Step 4 — Discover available link types

Call `mcp__atlassian__getIssueLinkTypes` once. Prefer, in order:

1. A link type whose `name` or `inward`/`outward` contains "Split" (e.g. "Split To" / "Split From")
2. `Relates` (inward: "relates to", outward: "relates to")

Cache the chosen link type `name` for use in Step 7. If neither exists, fall back to whichever generic link type is available and surface the choice in the plan.

### Step 5 — Compute the split plan per ticket

For each candidate, read `timeoriginalestimate` (seconds) and convert to integer hours (`hours = round(seconds / 3600)`; reject if rounding would produce a non-integer mismatch > 0.25h — in that case, surface the ticket in the plan with a warning and skip it).

Split algorithm (balanced, integer hours, minimum piece count):

```
count = ceil(total_hours / 8)
base  = total_hours // count
extra = total_hours - (base * count)
pieces = [base + 1] * extra + [base] * (count - extra)
```

Examples:

| Original | Count | Pieces |
| :-- | :-: | :-- |
| 9h | 2 | 5 + 4 |
| 12h | 2 | 6 + 6 |
| 16h | 2 | 8 + 8 |
| 17h | 3 | 6 + 6 + 5 |
| 20h | 3 | 7 + 7 + 6 |
| 25h | 4 | 7 + 6 + 6 + 6 |

Primary retains the **first** piece. All remaining pieces become new tickets.

### Step 6 — Present the plan (MANDATORY approval gate)

Render a markdown table:

```
Active sprint: "<Sprint name>" (id <sprint_id>)
Tickets to split: <N>

| Primary | Original | Pieces | Plan |
| :-- | :-: | :-: | :-- |
| SFDC-1234 (Story) "Fix BillingCountry pipeline" | 12h | 2 | primary → 6h, +1 new ticket (6h) |
| SFDC-1301 (Task) "Migrate trigger A to handler" | 17h | 3 | primary → 6h, +2 new tickets (6h, 5h) |

Link type: "Relates" (new → primary, "relates to")
Fields copied to new tickets: summary (prefixed), issuetype, priority, labels, components, fixVersions, description, assignee, sprint, epic link
Fields NOT touched: story points, attachments, comments, due date
```

Also list any skipped tickets with reason (no estimate, fractional hours, status = Done, etc.).

Ask: **"Proceed with this split plan? (yes / edit / cancel)"**

- `edit` — accept freeform adjustments (change a split shape, exclude a ticket, use a different link type) and re-render.
- `cancel` — exit without writing.
- Only `yes` proceeds to Step 7.

A single `yes` approves the entire batch per the root Approval Gates policy (Gate 3 Jira writes + Gate 4 task creation). Do not ask per-ticket.

### Step 7 — Execute the split (per primary ticket)

For each approved primary, in order:

1. **Create N−1 new tickets** via `mcp__atlassian__createJiraIssue`:
   - `projectKey: SFDC`
   - `issueTypeName: <primary's issuetype name>`
   - `summary: "[<PRIMARY_KEY>] : <primary's original summary>"`
   - `description: <copied from primary>`
   - `assignee: <primary's assignee accountId>` (= current user)
   - `priority: <primary's priority name>` (if set)
   - `labels: <primary's labels>` (if any)
   - `components: <primary's components>` (if any)
   - `fixVersions: <primary's fixVersions>` (if any)
   - `customfield_10105: <active sprint id>` (Sprint — add to same sprint as primary)
   - **Epic / Parent** — if `parent` is set on primary, copy `parent: { key: "<PRIMARY_EPIC_KEY>" }` on the new ticket. As a fallback (or additionally for robustness), also set `customfield_12000: "<PRIMARY_EPIC_KEY>"` (legacy Epic Link string) — Groupon's tenant populates both.
   - `timeTracking.originalEstimate: "<piece>h"`

2. **Reduce primary's original estimate** to the first piece via `mcp__atlassian__editJiraIssue`:
   - `fields.timetracking.originalEstimate: "<first_piece>h"`

3. **Create issue links** — for each newly created ticket, call `mcp__atlassian__createIssueLink` with the cached link type:
   - `inwardIssue: <new_key>`
   - `outwardIssue: <primary_key>`
   - `type: { name: "<chosen link type>" }` (e.g. "Relates")
   
   If the chosen link type is "Split To / Split From", set direction so outward is primary → inward is new (primary "splits to" new).

4. **Post reference comment on primary** via `mcp__atlassian__addCommentToJiraIssue`, `contentFormat: "markdown"`, single short line:

   ```
   Split into: [NEW-1], [NEW-2]
   ```

   Where each `[NEW-N]` is a Jira smart-link (just the key, e.g. `SFDC-12345`) — Jira auto-renders these as issue links. Do NOT add descriptive text beyond this line.

If any step fails on a ticket, abort that ticket's remaining steps (leave partial state only if creation succeeded but link failed — surface it for the user to clean up). Continue with the next primary.

### Step 8 — Report

```
Split plan complete.
- 2 primaries processed
- 3 new tickets created: SFDC-12345, SFDC-12346, SFDC-12347
- 2 primaries' estimates reduced
- 3 issue links created
- 2 reference comments posted on primaries

Skipped (with reason):
- SFDC-1200: no Original Estimate set
- SFDC-1298: status = Done (manually resolved before sprint end)
```

Include URLs to the active-sprint board view if easily derivable.

---

## Edge cases

- **Primary has active worklogs** — do NOT reset `timeSpent`, only edit `originalEstimate`. Tempo worklogs on the primary stay attached to the primary.
- **Primary has subtasks** — skill does not touch subtasks. Leave them attached to the primary; note this in the report.
- **Sprint is closing today / mid-closure** — if `sprint.state` transitions mid-run, abort after current primary and warn.
- **No link type matches** — if neither "Split" nor "Relates" is available, stop and show `mcp__atlassian__getIssueLinkTypes` output for the user to pick.
- **Custom-field IDs differ across tenants** — Groupon uses `customfield_10105` (Sprint) and `customfield_12000` (Epic Link) + native `parent` for Epic. If `mcp__atlassian__getJiraIssue` returns a `names` map (when `expand: "names"`), discover the real IDs at runtime by searching for entries `"Sprint"` and `"Epic Link"` and substitute. Do NOT silently skip Epic/Sprint copy — if neither field resolves, abort and surface the tenant mismatch so the user can update the skill.
- **Permissions** — if `createJiraIssue` returns 403, surface the full API error and abort. Do not silently skip.

---

## Never do

- Never use `Sub-task` as the issue type for split tickets — always match the primary's type.
- Never touch tickets where `timeoriginalestimate` is null or ≤ 28800 seconds (8h).
- Never split a ticket whose status is already in the Done category.
- Never batch-approve in Step 6 — always show the plan, always wait for explicit `yes`.
- Never copy or re-post attachments/comments from the primary to new tickets.
- Never reduce primary estimate without also creating the new tickets — if ticket creation fails, do not touch the primary.
- Never post more than one reference comment per primary.

---

## Approval gate

This skill creates tickets, edits estimates, creates issue links, and posts comments — all under **Gate 3 (Jira)** and **Gate 4 (task creation)** of `approval-gates.md`. A single `yes` on the Step 6 plan approves the full batch. Do not re-ask per ticket.

Dry-run mode: if the user says "dry run", "preview", or "show only", execute Steps 1–6 and then exit without writing. No Jira mutations, no audit.

---

## Auto-invocation rules

Invoke this skill when the user's intent matches one of:

- "split my sprint tickets" / "split oversized tickets" / "split tickets > 8h"
- "break down sprint tickets" / "chunk sprint work"
- `/meta-sprint-split`

Do NOT invoke for:

- Tickets outside the active SFDC sprint (require user to be explicit)
- Tickets not assigned to the current user
- Subtask breakdown requests (different workflow — sub-tasks are a Jira-native operation, not what this skill does)
