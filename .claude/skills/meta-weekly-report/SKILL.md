---
name: meta-weekly-report
description: Auto-draft the weekly Asana 5/15 report subtask description from Jira/Tempo/GitHub/session activity. Finds this Friday's incomplete subtask under the recurring parent, aggregates work since last Friday's submission, compares against last week's stated priorities, and posts an HTML draft for review. User reviews and marks the subtask complete in Asana manually.
---

# meta-weekly-report

Drafts the weekly 5/15 report description for the user's Asana recurring subtask
and updates it on approval. Designed to be invoked manually on Friday or via a
scheduled routine.

## When to use

- User says: "draft 5/15", "weekly report", "meta-weekly-report", "5/15 for this week"
- User triggers via routine on Fridays (recommended: Friday 14:00 IST)
- User has just submitted Tempo for the week and wants the matching narrative

## Prerequisites

- Asana MCP authenticated (`mcp__asana__*`)
- Atlassian MCP authenticated (`mcp__atlassian__*`)
- GitHub access via `gh` CLI (already authed at the user level)
- Tempo helper available at `.claude/skills/meta-tempo-log/bin/tempo_api.py`
- Tempo token in macOS Keychain (`security find-generic-password -s tempo_api_token -a nshelke -w`)
- Config at `.claude/skills/meta-weekly-report/config.json`

## Modes

### Mode A — Default: "draft 5/15" or "/meta-weekly-report"

Auto-draft for the upcoming Friday's subtask. Show full HTML preview, ask
"Submit to Asana? (yes / edit / cancel)".

### Mode B — Specific date: "/meta-weekly-report 2026-05-08"

Target the subtask whose `due_on` matches the given Friday. Otherwise identical
to Mode A.

### Mode C — Dry run: "preview 5/15"

Render the draft but do NOT update Asana, even on `yes`. Useful for validation.

### Mode D — Scheduled / auto-submit: `/meta-weekly-report --scheduled`

Non-interactive invocation from launchd, cron, or any scripted context.
Identical workflow to Mode A through Step 11, then **skips Step 12 (the chat
approval gate) entirely** and goes straight to Step 13 (Asana submit).

**Detection signals (either one triggers Mode D):**
1. The user message contains the literal substring `--scheduled` or
   `--auto-submit` (case-sensitive).
2. The env var `META_WEEKLY_SCHEDULED=1` is set when claude was invoked.

**Guard:** Mode D only auto-submits if
`config.schedule.scheduled_mode_auto_submit` is `true`. If the flag is
detected but config has it `false`, save the rendered HTML to
`~/.local/share/meta-weekly-report/draft-<due_on>.html` and exit with a
"draft saved, not submitted" message instead of submitting.

**Output (Mode D):** 3-line stdout summary suitable for log files:
```
subtask: <gid>
modified_at: <ISO8601 timestamp>
url: https://app.asana.com/0/0/<gid>/f
```

## Workflow

### Step 1 — Locate this week's and last week's subtask

Call `mcp__asana__asana_get_task` with `task_id = config.parent_task_gid` and
`opt_fields = "subtasks.gid,subtasks.name,subtasks.due_on,subtasks.completed"`.

From the subtasks list:
- **Current subtask** = `completed=false` AND `due_on` is the upcoming Friday in
  config.timezone (or the Friday on/after today). If multiple, pick the
  earliest `due_on`.
- **Last subtask** = `completed=true` with the most recent `due_on` strictly
  before the current subtask's `due_on`.

Show the user which subtasks were matched:
```
Current → "5/15 - Name Surname - YYYY-MM-DD" (gid, due YYYY-MM-DD)
Previous → "5/15 - Name Surname - YYYY-MM-DD" (gid, due YYYY-MM-DD)
```

> ⚠ **Subtask name vs. due_on can drift.** Asana's recurrence often clones
> the previous subtask's name verbatim, so the upcoming-Friday subtask may
> still carry the *prior* week's date in its name (e.g., name says
> `... - 2026-04-24` but due_on is `2026-05-01`). **Always match on
> `due_on`, never on the name string.** When a mismatch is detected, surface
> a one-line note in the chat preview:
> `⚠ Subtask name "<name>" does not match due_on <date> — recommend renaming to "5/15 - <Full Name> - <due_on>" before submitting.`
> Do NOT auto-rename — leave the rename decision to the user.

If no current incomplete subtask exists: stop and tell the user — Asana's
recurrence may not have generated this week's subtask yet.

### Step 2 — Define the activity window

```
window_start = previous_subtask.due_on + 1 day        (e.g., Sat after last submission)
window_end   = current_subtask.due_on                 (this Friday)
```

Both interpreted as IST calendar days. `today` may be ≤ `window_end`.

### Step 3 — Read last week's report

`mcp__asana__asana_get_task` with `task_id = previous_subtask.gid` and
`opt_fields = "html_notes"`. Parse the HTML to extract Section 3 (key
priorities). Pull every `[A-Z]+-\d+` ticket key out of Section 3 and keep the
prose lines too — those are what we'll cross-check.

### Step 4 — Aggregate this week's work

Run these in parallel:

#### 4a. Tempo worklogs in window

```
TOKEN=$(security find-generic-password -s tempo_api_token -a nshelke -w)
python3 .claude/skills/meta-tempo-log/bin/tempo_api.py list \
  --account-id <atlassian_account_id> \
  --from <window_start> --to <window_end>
```

Aggregate per `issue.id` → `{ticket_key, total_seconds, comments[]}`. Resolve
`issue.id` → ticket key by looking up via `mcp__atlassian__getJiraIssue` only if
the worklog response doesn't include `issue.key`.

The Tempo `description` field is the highest-quality source — it's what we
just wrote during `/meta-tempo-log`.

#### 4b. Jira activity in window

```jql
(assignee = currentUser()
 OR worklogAuthor = currentUser()
 OR commentedBy = currentUser()
 OR status changed by currentUser() DURING ("<window_start>", "<window_end+1>"))
AND updated >= "<window_start>" AND updated < "<window_end+1>"
```

Request **only** these fields: `summary, status, issuetype, parent,
customfield_10014, priority, updated, resolutiondate`. Do NOT request
`description`, do NOT use `expand`, do NOT add fields you don't strictly need.

> ⚠ **Token-limit guardrail.** A 7-day window for an active SFDC engineer
> commonly returns 15+ tickets. The Atlassian MCP returns full ADF for
> `description` and large blocks for `expand=*` — combined this can blow the
> 25K-token MCP cap. **Behaviour on token-limit error:** split the JQL into
> 5-ticket batches by adding `id in (...)` chunks, OR drop fields one at a
> time (resolutiondate first, then priority) and retry. Never read the
> overflow file the MCP saves — re-issue a smaller query instead.

Build per-ticket: `{key, summary, status, epic_key, epic_summary, type,
resolved_in_window: bool}`.

#### 4c. GitHub PRs in window — **OPTIONAL**

> Most Groupon SFDC repos live on **GitHub Enterprise Server**
> (`github.groupondev.com`), not github.com. The default `gh` CLI auth is
> against github.com, so `gh pr list` will return `[]` for GHES repos unless
> the user has run `gh auth login --hostname github.groupondev.com`.
>
> **Behaviour:**
> - Try the queries below.
> - If both repos return `[]` AND Tempo comments already mention specific PR
>   numbers (e.g., "PR #11187 merged", "PR #48 merged"), treat that as
>   sufficient — Tempo comments are the authoritative PR signal in this setup.
> - Do NOT fail or block on missing GHES auth. Skip the call and proceed.
> - Surface a one-line note in the chat preview ("⚠ GitHub PR aggregation
>   skipped — using Tempo comment PR mentions") so the user knows.

```bash
for repo in <config.github_repos>; do
  gh pr list -R "$repo" \
    --search "author:<config.github_user> updated:>=<window_start>" \
    --state all --json number,title,state,url,closedAt,mergedAt,createdAt,labels
done
```

Also `--search "reviewed-by:<github_user> updated:>=<window_start>"` for review
contributions.

Map PRs to tickets via the PR title's `[A-Z]+-\d+` token (or branch name if
fetched). Bucket per ticket: `{prs_authored, prs_merged, prs_reviewed}`.

#### 4d. Session activity (cross-repo)

```
python3 .claude/skills/meta-tempo-log/bin/scan_sessions.py --date <YYYY-MM-DD>
```

Run once per day in the window and union the results. Use this as a soft
signal to pull in tickets that didn't get formal Jira/PR/Tempo activity but
were genuinely worked on (e.g., research, blocked work).

#### 4e. Skill / .claude/* changes (AI work signal)

```bash
git -C /Users/nshelke/workspace/CoS log \
  --author=<config.github_user> \
  --since="<window_start> 00:00 IST" --until="<window_end+1> 00:00 IST" \
  --pretty=format:'%h|%s|%ad' --date=iso \
  -- .claude/
```

Plus any uncommitted modifications to skill/agent/rule files in
`config.ai_skill_dirs` (via `git status --porcelain` filtered to those paths).

This feeds Section 4's "best use of AI" signal even on weeks where SFDC-10142
has no formal Jira activity.

### Step 5 — Resolve Epic grouping

For every unique ticket from Steps 4a-4d, ensure we know its Epic. If
`customfield_10014` (Epic Link) wasn't populated in Step 4b's response (e.g.,
ticket wasn't in the window's Jira activity but appeared in Tempo or session),
batch-fetch:

```
mcp__atlassian__searchJiraIssuesUsingJql with:
  jql: "key in (T1, T2, ...)"
  fields: ["summary", "status", "parent", "customfield_10014", "issuetype"]
```

Build the grouping map:
```
{
  "<EPIC-KEY>": {
    "epic_summary": "...",
    "tickets": [
      {key, summary, status, work_summary},
      ...
    ]
  }
}
```

**Drop tickets with no epic from Section 1 entirely.** Do NOT render a
synthetic "Other / Standalone" group. Section 1 should only show real
project work organised by Epic. Recurring meeting trackers (MTG-*), admin
buckets (ADMIN-*), OOO/holiday markers, and any other epic-less items are
noise for the manager-facing report and are fully excluded from Section 1.

Also skip every ticket in `config.exclude_meeting_only_tickets` regardless
of whether it has an epic, unless there's a Tempo comment or session signal
showing the user did substantive work on it (not just attended a meeting).

### Step 6 — Synthesize per-ticket work summary

For each ticket, write a 1-2 line plain-English update by combining (in this
order of preference):
1. The longest Tempo worklog comment authored by the user this week
2. The Jira status transition that happened in window (e.g., "Moved to Review",
   "Closed/Done")
3. The merged PR title (e.g., "PR #11187 merged")
4. Generic fallback: ticket summary + current status

Lead with concrete outcomes (deployed, merged, closed, status). Avoid
fabrication — if the only signal is a session mention, write something honest
like "Continued analysis / in-progress work" rather than inventing details.

### Step 7 — Cross-check last week's priorities

For each ticket key extracted from last week's Section 3:
- Look up its current status now (one batch JQL).
- Compare against the *target* described in last week's prose (e.g., "deploy to
  PROD", "complete spike", "fix and verify").
- Bucket as **done** if status is Done/Closed/Resolved AND resolved within the
  window, OR if a related PR was merged in window.
- Bucket as **not done** otherwise. Synthesize a brief reason from current
  status + most-recent comment if accessible (e.g., "still in Review — awaiting
  reviewer feedback", "deprioritized for service-delivery escalations",
  "blocked on dependency").

Record: `{done: [ticket_keys], not_done: [{ticket_key, last_week_target, current_status, reason}]}`.

### Step 8 — Pick next week's priorities (Section 3)

Build the candidate pool from these JQL queries (combine, dedupe, rank):

```jql
-- In-flight near completion
assignee = currentUser() AND sprint in openSprints()
AND status in ("In Progress", "Review", "QA", "UAT")
ORDER BY priority DESC, updated DESC

-- Carry-over items (not done last week)
key in (<not_done_keys_from_step_7>)

-- Top of the to-do list
assignee = currentUser() AND sprint in openSprints()
AND status = "To Do" AND priority in (Highest, High)
ORDER BY rank ASC
```

Rank candidates by:
1. Already in Review/QA → near-deployment is highest priority
2. Carry-over from last week's not-done (continuity matters)
3. In-Progress with recent activity
4. High-priority To Do items

Pick top `max_priorities` items, ensuring at least `min_priorities`. For each:
- Synthesize a one-line target describing the next concrete outcome (e.g.,
  "close out Review and merge", "complete spike with tech proposal",
  "verify against reference deal").
- Format: `<Ticket link> - <state-and-target>`.
- **Do NOT include ETA / day-of-week / dates.** Keep the line tight.

Surface the chosen list to the user before final render so they can swap items.

### Step 9 — Section 4 (Achievement + AI)

**Achievement:** the highest-impact ticket closed in window. Heuristic:
- Status moved to Done in window AND
- Has the largest `originalEstimateSeconds` among such tickets, OR
- Resolves a production deploy ticket (label `prod-deploy` or summary contains
  "PROD Deployment"), OR
- Closes a long-running spike/escalation

If multiple, list up to 2.

**AI section** (mandatory, never empty):
Pull from:
- Any ticket whose epic_key == `config.ai_epic_key` worked in window
- Skill / agent / rule changes from Step 4e
- Session activity in `config.ai_skill_dirs`

Render as 1-2 bullets describing what AI tooling/automation was built or used.
Examples:
- "Built `meta-tempo-log` skill — auto-drafts daily Tempo worklogs from
  Calendar + Jira + git + session signals, eliminating ~20 min/day of manual
  entry."
- "Used Claude Code to refactor X (saved ~Y hours)."

If genuinely no AI activity occurred: surface as a follow-up question — never
fabricate. The user needs to provide the AI line themselves.

### Step 10 — Section 5 (Suggestions + AI opportunity)

This is the hardest to auto-generate. Default behavior:
- Look at any ticket in window with status "Blocked", "Stalled", or with
  comments mentioning "manual", "tedious", "repeat" — flag as automation candidate.
- Look at recurring meeting tickets (MTG-*) where worklog total > 4h/week —
  signal of meeting-heavy week.
- Look at process tickets (admin/KTLO) with high time spent.

Render 1-2 bullets:
- A concrete improvement suggestion if signal is strong, OR
- A clearly labeled "AI opportunity" idea (e.g., "AI-assisted ticket grooming",
  "automated test class selection from PR diff", etc.)

If genuinely nothing: render `• Nothing to add` (matches the user's past style)
and let the user fill in during review.

### Step 11 — Render HTML

Match the format from the reference subtask `1210072209390194`. Structure:

```html
<body>
<strong>1) What happened this week?</strong>
<ul>
  <li><strong>{Epic Key} - {Epic Summary}</strong></li>
  <ul>
    <li><a href="https://groupondev.atlassian.net/browse/{TICKET}">{Ticket Summary}</a>: {1-2 line work summary}</li>
    <!-- repeat per ticket in this epic. NOTE on style:
         - Separator is a colon ":", NOT "==>" or "==&gt;".
         - Status statement is plain text - NOT wrapped in <em> or <strong>.
         - No em-dashes ("—") anywhere - use regular hyphens "-" or restructure
           sentences with periods/commas. Em-dashes are an AI tell. -->
  </ul>
  <!-- repeat per epic. Do NOT add an "Other / Standalone" group at the
       end - tickets without an epic are dropped from Section 1 entirely. -->
</ul>

<strong>2) What was planned and wasn't done?</strong>
<ul>
  <li>{One-line intro framing the misses (e.g., "Carried over from last week — could not complete due to X")}</li>
  <ul>
    <li><a href="...">{Ticket Title}</a> - {reason}</li>
  </ul>
</ul>
<!-- if everything from last week was done: render "<ul><li>Nothing — all priorities from last week were completed.</li></ul>" -->

<strong>3) What are the key priorities for next week (3-5 tasks)?</strong>
<ul>
  <li><a href="...">{Ticket Title}</a> - {state-and-target}</li>
  <!-- 3-5 items. NO "ETA - Day - DD/MM/YYYY" suffix. -->
</ul>

<strong>4) Top achievement/breakthrough (include best use of AI if nothing else)</strong>
<ul>
  <li>{Achievement 1 — short headline}</li>
  <ul>
    <li>{detail bullet — concrete outcome, scope, or impact}</li>
    <li>{detail bullet — additional context if relevant}</li>
  </ul>
  <li>{Achievement 2 — short headline (optional)}</li>
  <ul>
    <li>{detail bullet}</li>
  </ul>
  <li>{AI use case 1 — short headline (mandatory)}</li>
  <ul>
    <li>{detail bullet — what was built/used and the concrete artifact}</li>
    <li>{detail bullet — measurable benefit if any (e.g., time saved, errors avoided)}</li>
  </ul>
  <!-- Repeat the same parent-bullet + nested sub-bullets pattern for any
       additional AI use case. Each achievement and each AI use case gets its
       OWN parent bullet with its OWN nested <ul> of sub-bullets — never mix
       multiple achievements or AI use cases under one parent bullet. -->
</ul>

<strong>5) Suggestions and improvement ideas (include AI opportunity if nothing else)</strong>
<ul>
  <li>{Suggestion or AI opportunity}</li>
</ul>
<!-- or fallback: "• Nothing to add" outside <ul> if matching user's prior style -->
</body>
```

HTML rules (from Asana API):
- Allowed tags only: `<body>, <strong>, <em>, <u>, <s>, <code>, <ol>, <ul>,
  <li>, <a>, <blockquote>, <pre>, <h1>, <h2>, <hr/>, <img>`.
- Use `&gt;` not `>` inside text. Use `&amp;` for ampersands.
- Single root `<body>...</body>` element. Must be well-formed XML.
- Only `<a>` may have attributes (`href`, `data-asana-gid`).

**Content rules - what to OMIT from the rendered output:**
- **No hours.** Do not include per-ticket hours, per-epic totals, weekly
  totals, or any "Xh / Xh logged" text in any section. Hours are an internal
  ranking signal only. The manager-facing report should describe outcomes,
  not effort. If the user explicitly asks for hours later, surface them as a
  follow-up. Don't bake them into the default render.
- **No ETAs / dates in Section 3.** No "ETA - Day - DD/MM/YYYY", no
  "by Friday", no week numbers. Just `<a>Ticket</a> - <state-and-target>`.
- **No timezone or window dates** inside the report body. The window is
  metadata for the skill's own bookkeeping, not for the manager.
- **No "Render summary" / stats table** in the submitted HTML. That's a
  preview-only aid for chat. Never include it in `html_notes`.

**Style rules - typography to AVOID:**
- **No em-dashes ("—", U+2014).** They are an AI-writing tell. Replace every
  em-dash with either a regular hyphen "-" (with spaces around it when used
  as a clause separator) OR restructure the sentence with a period, comma,
  or colon. This applies to headers, ticket lines, and prose alike.
- **No italics.** Do NOT wrap status statements, achievements, or any other
  text in `<em>` or `<i>`. Plain text only. The earlier reference task style
  used `<em><strong>...</strong></em>` for status statements but the user has
  explicitly opted out.
- **No bold on status statements** (Section 1's "what was done" lines).
  Bold is reserved for Epic group headers and section headers only.
- **Separator is a colon, not "==&gt;" or "→".** In Section 1, the format is
  `<a>Ticket Link</a>: status statement`.
- **No decorative em-dash openers in achievements / AI use cases** (Section
  4). Don't write "Achievement Title — sub-clause." Either use a colon, a
  period, or restructure: "Achievement Title. Sub-clause."

### Step 12 — Show the draft

Render the HTML as readable markdown for the user (convert `<strong>` to bold,
`<a href>` to `[label](url)`, etc.). Show all 5 sections, the matched
subtasks, and the activity window.

Then ask:
```
Submit to Asana subtask {gid}? (yes / edit <change> / preview / cancel)
```

`edit` accepts freeform changes — re-render and re-ask. `preview` keeps
iterating without submitting. `cancel` exits without writing.

### Step 13 — Submit

`mcp__asana__asana_update_task` with:
```
task_id: <current_subtask.gid>
html_notes: <rendered HTML>
```

Confirm success: print the Asana URL
(`https://app.asana.com/0/0/{subtask_gid}/f`) and remind the user to mark the
subtask complete themselves.

## Format conventions (must match user's history)

Inspect any recent completed subtask (e.g., `1213911742146709`,
`1213830806836539`) to verify ongoing format. Key conventions:
- Section headers wrapped in `<strong>` (not `<h1>` or `<h2>`)
- Section 1: nested `<ul><ul>` with epic name in bold as a `<li>`, then nested
  ticket bullets
- Section 1 format: `<a>Ticket</a>: status statement` (plain text after the
  colon - NO `<em>`, NO `<strong>`, NO em-dash separator).
- Section 4 format: each achievement and each AI use case gets its own parent
  bullet (`<li>`) with a nested `<ul>` of 1-3 sub-bullets describing detail,
  scope, or impact. Never collapse multiple achievements into one bullet.
- Em-dashes ("—") are forbidden everywhere in the rendered output. Use
  regular hyphens "-" or restructure with periods/commas/colons.
- Section 3 format: `<a>Ticket</a> - <state-and-target>` (no ETA, no day-of-week, no dates)
- Section 5 sometimes uses literal `•` bullet character outside `<ul>`

The reference task 1210072209390194 is the canonical example.

## Approval gate

Per CoS approval-gates rules, behaviour depends on invocation mode:

**Manual / interactive mode** (user types `/meta-weekly-report` in chat):
- Show full draft, ask `yes / edit <change> / cancel`.
- Submit only after explicit `yes`.

**Scheduled / routine mode** (cron-triggered, no human in the session):
- If `config.schedule.scheduled_mode_auto_submit` is `true`: skip the chat
  approval gate and submit the draft directly to Asana.
- The Asana subtask remains **incomplete**. The user reviews the description
  in Asana and marks the subtask complete manually.
- Rationale: this skill writes to a non-final deliverable (the description
  field of an open subtask). The user's review point is opening the Asana
  task before marking it complete, not a chat review. Risk model differs
  from completing the subtask itself.
- If the flag is `false` (default for safety): save the rendered HTML to
  `~/.local/share/meta-weekly-report/draft-<due_on>.html` and emit a push
  notification asking the user to review in chat.

## Error handling

- **No incomplete subtask for upcoming Friday**: tell user, don't proceed.
- **Atlassian/Asana MCP unauthenticated**: tell user to `/mcp` and retry.
- **Tempo token missing**: tell user `security add-generic-password -s tempo_api_token -a nshelke -w <token>`.
- **Window has zero activity**: render an "OOO / no updates this week" draft
  and let user confirm before submitting.
- **Last week's report can't be parsed**: still render Sections 1, 3, 4, 5;
  put a note in Section 2 ("Could not parse last week's priorities — please
  fill in").

## Output shape (end of skill run)

```
Updated Asana subtask:
  https://app.asana.com/0/0/{subtask_gid}/f
  Title: 5/15 - Nirajkumar Shelke - YYYY-MM-DD

Sections rendered: 5
Window: YYYY-MM-DD → YYYY-MM-DD
Epics covered: N
Tickets surfaced: M
Reminder: mark the subtask complete in Asana when ready.
```

## Recommended scheduling

Add via `/schedule`:
```
Cadence: every Thursday 21:00 IST
Cron:    0 21 * * 4
Timezone: Asia/Kolkata
Command: /meta-weekly-report
Note: "Auto-drafts the 5/15 and writes the description directly to Asana
       via auto-submit. User reviews in Asana the next day and marks the
       subtask complete manually."
```

Thursday 21:00 IST runs the night before the Friday 17:00 IST (20:00 CET)
deadline, leaving ~20 hours for the user to open the Asana subtask, review,
edit if needed, and mark complete.

Auto-submit is gated by `config.schedule.scheduled_mode_auto_submit`. Set it
to `true` to enable the hands-off Thursday-night flow.
