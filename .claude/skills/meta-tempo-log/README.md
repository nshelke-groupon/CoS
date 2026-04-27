# meta-tempo-log

Log time to Tempo Cloud from Claude Code.

## First-time setup

1. **Create your per-user config override**
   ```bash
   cd .claude/skills/meta-tempo-log/
   cp config.local.json.example config.local.json
   ```
   Then edit `config.local.json` and fill in:
   - `atlassian_account_id` — your Jira account ID. Get it from `https://groupondev.atlassian.net/rest/api/3/myself` (look for `accountId`), or via the Atlassian MCP tool `atlassianUserInfo`.
   - `keychain.account` — your macOS username (`whoami`).

   `config.local.json` is gitignored. Team defaults live in `config.json`; your local values are deep-merged on top at load time. Only override the fields listed in the example unless you have a personal reason (e.g. different exploration tickets).

2. **Get a Tempo API token**
   - In Jira, open the Tempo app → Settings → **API Integration** → **New Token**
   - Scope: worklog read + write

3. **Store the token in macOS Keychain** (substitute your macOS username for `-a`)
   ```bash
   security add-generic-password -s tempo_api_token -a $(whoami) -w <PASTE_TOKEN>
   ```
   (Fallback: `export TEMPO_API_TOKEN=...` in your shell rc file.)

4. **Create the local data directory**
   ```bash
   mkdir -p ~/.local/share/meta-tempo-log
   ```

5. **Verify token access**
   ```bash
   security find-generic-password -s tempo_api_token -a $(whoami) -w >/dev/null \
     && echo "token OK" || echo "token missing"
   ```

## Usage

Invoke the skill from any Claude Code session in the meta-repo:

```
/meta-tempo-log SFDC-10201 3h "Refined test standards"
/meta-tempo-log 2h                        # ticket inferred
/meta-tempo-log today                     # auto-draft for today
/meta-tempo-log yesterday                 # auto-draft for yesterday
/meta-tempo-log 2026-04-21                # auto-draft for specific date
/meta-tempo-log today dry-run             # preview, no writes
```

## What it does

- **Explicit mode** — submits one worklog for the ticket + duration you give
- **Context mode** — infers the ticket from your session or current branch
- **Auto-draft** — pulls Jira, Google Calendar, git commits, and session history, composes a draft that sums to 8h (or actual, whichever is higher), and asks you to confirm before writing
- Always shows the full draft before submission — no silent writes
- Never creates duplicate entries (idempotent by `issueId + startDate + startTime + seconds`)
- Appends every action to `~/.local/share/meta-tempo-log/audit.csv`

## Files

```
.claude/skills/meta-tempo-log/
├── SKILL.md              # Claude playbook
├── config.json           # Defaults: TZ, hours target, on-call rules, etc.
├── README.md             # This file
└── bin/
    ├── tempo_api.py      # Tempo REST wrapper (list/create/delete)
    ├── allocate.py       # Overlap + proportional-fill math, cap enforcement, 2h split
    ├── submit.py         # Idempotent submission + audit + Original Estimate hard block
    └── scan_sessions.py  # Cross-project session scanner: aggregates Jira mentions per IST day

~/.local/share/meta-tempo-log/
├── meeting-mappings.json # Learned recurring meeting → ticket map
└── audit.csv             # Every submit / skip / fail
```

## Manual script usage (without Claude)

The scripts work standalone for scripting or debugging:

```bash
# List worklogs for a date range
python3 .claude/skills/meta-tempo-log/bin/tempo_api.py list \
  --account-id 712020:5cdfd46b-1897-4c7a-8615-a1baf0c3b4b3 \
  --from 2026-04-22 --to 2026-04-22

# Create a single worklog
python3 .claude/skills/meta-tempo-log/bin/tempo_api.py create \
  --account-id 712020:5cdfd46b-1897-4c7a-8615-a1baf0c3b4b3 \
  --issue-id 12345 --seconds 3600 \
  --start-date 2026-04-22 --start-time 08:00:00 \
  --description "Explicit test"

# Run allocation math (reads JSON from stdin)
echo '{
  "target_date":"2026-04-22",
  "day_start_hour":8,"daily_hours_minimum":8,"min_bucket_minutes":15,
  "meetings":[{"start":"10:00","end":"11:00","ticket":"SFDC-9500"}],
  "activity_weights":{"SFDC-10201":5,"SFDC-10150":2},
  "existing_worklogs":[],
  "on_call_topup":null
}' | python3 .claude/skills/meta-tempo-log/bin/allocate.py
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Tempo token not found` | Add via Keychain command above, or set `TEMPO_API_TOKEN` env |
| `HTTP 401` on list/create | Token expired or missing worklog scope — regenerate in Tempo |
| `HTTP 403` on create | Account lacks permission to log against that project |
| `HTTP 404` on issueId | Ticket exists but not visible to your account, or wrong numeric ID |
| Duplicates skipped on rerun | Expected — `submit.py` is idempotent |

## What this skill does NOT do

- Edit existing worklogs (delete + recreate if you need to change one — use `tempo_api.py delete`)
- Bulk backfill across multiple days in one call (Phase 3)
- Report weekly totals or compliance (Phase 3)
- Write to native Jira worklog — only Tempo

## Approval gates

Tempo submissions fall under the meta-repo approval gates (5 and 8). The skill
never auto-submits: it always renders the full draft and waits for explicit
`yes` from the user.
