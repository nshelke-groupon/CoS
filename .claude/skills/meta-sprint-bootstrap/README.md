# meta-sprint-bootstrap

Auto-creates the recurring SFDC sprint-ceremony tickets for the upcoming
sprint. See `SKILL.md` for the workflow specification.

## What it does

Per sprint, it creates 14 tickets in the SFDC project, all assigned to the
upcoming sprint:

- **2 Dev On-Call tickets** (8h each) - assigned to the two developers
  on-call for the sprint window, looked up automatically from JSM
  Operations.
- **12 SFDC Support Case Management tickets** (8h each) - 6 for Srilakshmi K S
  and 6 for Utkarsh Pathak.

Always shows a preview, asks for `yes / edit / cancel` before any Jira
writes, and has an idempotency check so re-running is safe.

## One-time setup

### 1. Atlassian API token

Generate at https://id.atlassian.com/manage-profile/security/api-tokens
(label: "Claude Code - meta-sprint-bootstrap"). Then store in the macOS
Keychain:

```bash
security add-generic-password -s atlassian_api_token -a nshelke -w "<paste-token>"
```

Verify:
```bash
security find-generic-password -s atlassian_api_token -a nshelke -w
```

(Should print the token.)

### 2. Discover JSM Ops team and schedule IDs

Run the discovery helper to list teams and on-call schedules:

```bash
python3 .claude/skills/meta-sprint-bootstrap/bin/jsm_oncall.py discover
```

You'll see output like:

```json
{
  "teams": [
    {
      "team_id": "abc-123",
      "team_name": "SFDC Engineering",
      "schedules": [
        {"id": "sch-456", "name": "SFDC Dev On-Call", "timezone": "Asia/Kolkata"}
      ]
    }
  ]
}
```

Pick the schedule that matches the dev on-call rotation and put its `id`
into `config.json`'s `jsm_ops.schedule_id`. Same for the team's `team_id`.

### 3. Verify config

Open `.claude/skills/meta-sprint-bootstrap/config.json` and confirm:
- `atlassian_email` - your work email
- `atlassian_cloud_id` - already filled (groupondev tenant)
- `current_quarter.label` - matches the active quarter (e.g. `26Q2`)
- `current_quarter.ktlo_epic_key` and `operational_epic_key` - the two
  parent epics for this quarter
- `jira_board_id` - numeric ID of your SFDC scrum board (find it in any
  board URL: `.../boards/<NUMBER>`). Used in the final report's link.

## Usage

In Claude Code chat, on Wednesday or Thursday morning before sprint
planning:

```
/meta-sprint-bootstrap
```

The skill will:
1. Find the upcoming sprint on the SFDC board
2. Look up who's on-call for week 1 and week 2 of that sprint
3. Show a preview of all 14 tickets
4. Wait for your `yes` before creating

### Modes

| Command | Behavior |
|---|---|
| `/meta-sprint-bootstrap` | Default. Discover sprint + on-call, create 14 tickets after `yes`. |
| `/meta-sprint-bootstrap preview` | Build preview but never create (Mode C dry-run). |
| `/meta-sprint-bootstrap sprint=<id>` | Override auto-discovered sprint with a specific one. |
| `/meta-sprint-bootstrap discover` | Run JSM Ops discovery only (initial setup). |

## Quarterly maintenance

Once per quarter, when new KTLO and Operational epics are created in
Jira, update `config.json`:

```json
"current_quarter": {
  "label":                "26Q3",
  "label_short":          "Q3",
  "ktlo_epic_key":        "SFDC-XXXX",
  "operational_epic_key": "SFDC-YYYY"
}
```

The skill warns automatically if the upcoming sprint's quarter prefix
doesn't match `current_quarter.label`.

## Files

- `SKILL.md` - workflow spec (read by Claude on invocation)
- `config.json` - your settings, gitignored
- `config.json.example` - template, committed
- `bin/jsm_oncall.py` - JSM Operations API helper (Python, uses Keychain
  for the API token)
- `README.md` - this file

## Related skills

- `meta-tempo-log` - daily Tempo worklog automation. Uses Tempo Keychain
  token, separate from Atlassian token.
- `meta-weekly-report` - weekly Asana 5/15 report.
- `meta-sprint-split` - split oversized tickets in active sprint.
