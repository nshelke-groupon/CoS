#!/usr/bin/env python3
"""
JSM Operations on-call helper for the meta-sprint-bootstrap skill.

Wraps the small subset of the Atlassian JSM Operations REST API the skill
needs: discovering the team / schedule for the SFDC dev on-call rotation,
and fetching who is on-call across a date window.

Auth: Basic Auth using the user's Atlassian email + an API token. Token is
read from the macOS Keychain (service `atlassian_api_token`, account from
config.json) or from the `ATLASSIAN_API_TOKEN` env var.

Subcommands:
  discover                       List teams and schedules to help find IDs
  oncall   --schedule-id X       Who is on-call right now on schedule X
           --date YYYY-MM-DD     ... or on a specific date
  timeline --schedule-id X       Full participant list for a date window
           --from YYYY-MM-DD --to YYYY-MM-DD
  weekly   --schedule-id X       Resolve the two on-call participants for a
           --sprint-start YYYY-MM-DD     14-day sprint window: who covers
                                         days 0-6 (week 1) and days 7-13 (week 2).

All output is JSON to stdout. Errors go to stderr with a non-zero exit code.

Per-tenant URL detection:
  The Atlassian Cloud JSM Ops API base is well-known:
    https://api.atlassian.com/jsm/ops/api/{cloudId}/v1/...
  We pass cloudId via --cloud-id or read it from config.atlassian_cloud_id.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

SKILL_DIR = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = SKILL_DIR / "config.json"
DEFAULT_BASE = "https://api.atlassian.com/jsm/ops/api"


def load_config() -> dict:
    if CONFIG_PATH.is_file():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except json.JSONDecodeError as e:
            sys.stderr.write(f"WARN: config.json parse error: {e}\n")
    return {}


def get_token(cfg: dict) -> str:
    env = os.environ.get("ATLASSIAN_API_TOKEN")
    if env:
        return env
    keychain = cfg.get("keychain", {})
    service = keychain.get("service", "atlassian_api_token")
    account = keychain.get("account") or os.environ.get("USER", "")
    if not account:
        sys.exit(
            "ERROR: keychain.account not set in config.json and USER env "
            "is empty. Cannot read token from Keychain."
        )
    try:
        out = subprocess.check_output(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except subprocess.CalledProcessError:
        sys.exit(
            f"ERROR: no token found in Keychain (service={service}, "
            f"account={account}). Set ATLASSIAN_API_TOKEN env var, OR run:\n"
            f"  security add-generic-password -s {service} -a {account} -w "
            f"<your-atlassian-api-token>"
        )


def get_email(cfg: dict) -> str:
    email = os.environ.get("ATLASSIAN_EMAIL") or cfg.get("atlassian_email")
    if not email:
        sys.exit(
            "ERROR: atlassian_email not set in config.json and ATLASSIAN_EMAIL "
            "env is empty."
        )
    return email


def auth_header(email: str, token: str) -> str:
    raw = f"{email}:{token}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def request(method: str, url: str, headers: dict, body: bytes | None = None):
    """Returns whatever the response decodes to (dict or list)."""
    req = urllib.request.Request(url, method=method, headers=headers, data=body)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
            if not data:
                return {}
            return json.loads(data)
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        sys.stderr.write(
            f"HTTP {e.code} {e.reason} on {method} {url}\n{body_text}\n"
        )
        sys.exit(2)
    except urllib.error.URLError as e:
        sys.stderr.write(f"Network error on {method} {url}: {e.reason}\n")
        sys.exit(2)


def normalize_list(payload, list_keys=("values", "teams", "schedules", "data")):
    """Some Atlassian endpoints return a bare list, others wrap it in a dict.
    Return a list either way."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in list_keys:
            v = payload.get(k)
            if isinstance(v, list):
                return v
        return []
    return []


def api_url(cfg: dict, cloud_id_arg: str | None, path: str) -> str:
    cloud_id = cloud_id_arg or cfg.get("atlassian_cloud_id")
    if not cloud_id:
        sys.exit("ERROR: --cloud-id not given and atlassian_cloud_id missing in config.json")
    base = cfg.get("jsm_ops", {}).get("base_url", DEFAULT_BASE)
    return f"{base.rstrip('/')}/{cloud_id}/v1{path}"


def fetch_all_schedules(cfg, cloud_id_arg: str | None, headers: dict) -> list:
    """Fetch every schedule across all paginated pages."""
    base_full = api_url(cfg, cloud_id_arg, "/schedules")
    base_root = base_full.rsplit("/v1/", 1)[0]
    url = base_full
    out = []
    while url:
        page = request("GET", url, headers)
        items = page.get("values", []) if isinstance(page, dict) else []
        out.extend(items)
        nxt = (page.get("links") or {}).get("next") if isinstance(page, dict) else None
        url = (base_root + nxt) if nxt else None
    return out


def cmd_discover(args, cfg, headers):
    """List teams the user can see, plus the schedules belonging to each.

    Schedules are paginated (~25/page across hundreds total in groupondev),
    so we fetch them all once and bucket by team_id client-side.
    """
    teams_raw = request("GET", api_url(cfg, args.cloud_id, "/teams"), headers)
    team_list = normalize_list(teams_raw)
    schedules = fetch_all_schedules(cfg, args.cloud_id, headers)
    by_team: dict[str, list] = {}
    for s in schedules:
        by_team.setdefault(s.get("teamId") or "", []).append(s)
    out = {"teams": []}
    for t in team_list:
        team_id = t.get("teamId") or t.get("id")
        team_name = t.get("teamName") or t.get("name") or t.get("displayName")
        out["teams"].append({
            "team_id": team_id,
            "team_name": team_name,
            "schedules": [
                {"id": s.get("id"), "name": s.get("name"),
                 "timezone": s.get("timezone"), "enabled": s.get("enabled")}
                for s in by_team.get(team_id, [])
            ],
        })
    print(json.dumps(out, indent=2))


def cmd_raw_teams(args, cfg, headers):
    """Debug helper: dump the raw /teams response so we can see field shape."""
    teams_raw = request("GET", api_url(cfg, args.cloud_id, "/teams"), headers)
    print(json.dumps(teams_raw, indent=2))


def to_iso_datetime(d: str) -> str:
    """Accept either YYYY-MM-DD or full ISO; always return full ISO 8601 UTC.

    The JSM Ops API rejects bare YYYY-MM-DD; it wants e.g. 2026-05-08T00:00:00Z.
    """
    if "T" in d:
        return d
    return f"{d}T00:00:00Z"


def jira_user_lookup(cfg: dict, headers: dict, account_id: str) -> dict:
    """Resolve a Jira accountId to {name, email} via the standard Jira REST API.

    The on-call endpoint returns only accountId; for the report we need the
    display name and email. Uses the user's Jira tenant (groupondev), NOT
    the JSM Ops API host.
    """
    base = cfg.get("jira_base_url") or "https://groupondev.atlassian.net"
    url = f"{base.rstrip('/')}/rest/api/3/user?" + urllib.parse.urlencode(
        {"accountId": account_id}
    )
    try:
        data = request("GET", url, headers)
    except SystemExit:
        return {"accountId": account_id, "name": None, "email": None}
    return {
        "accountId": account_id,
        "name": data.get("displayName"),
        "email": data.get("emailAddress"),
        "active": data.get("active"),
    }


def fetch_oncall_for_datetime(cfg, cloud_id_arg, headers, schedule_id: str,
                              dt_iso: str) -> list:
    """Fetch on-call participants for a specific moment, with name/email
    resolved.  Returns a list of resolved-user dicts (typically 1 element)."""
    path = f"/schedules/{schedule_id}/on-calls?" + urllib.parse.urlencode(
        {"date": dt_iso}
    )
    data = request("GET", api_url(cfg, cloud_id_arg, path), headers)
    participants = data.get("onCallParticipants", []) if isinstance(data, dict) else []
    resolved = []
    for p in participants:
        acc_id = p.get("id") or p.get("accountId")
        if not acc_id:
            continue
        if p.get("type") == "user":
            info = jira_user_lookup(cfg, headers, acc_id)
        else:
            # team / escalation / schedule reference - leave unresolved
            info = {"accountId": acc_id, "name": None, "email": None,
                    "type": p.get("type")}
        resolved.append(info)
    return resolved


def cmd_oncall(args, cfg, headers):
    dt_iso = to_iso_datetime(args.date) if args.date else None
    if dt_iso:
        users = fetch_oncall_for_datetime(cfg, args.cloud_id, headers,
                                          args.schedule_id, dt_iso)
        print(json.dumps({"date": dt_iso, "on_call": users}, indent=2))
    else:
        # No date given - return the raw "on-call right now" response.
        path = f"/schedules/{args.schedule_id}/on-calls"
        data = request("GET", api_url(cfg, args.cloud_id, path), headers)
        # Try to enrich with name/email even on the no-date path.
        participants = data.get("onCallParticipants", []) if isinstance(data, dict) else []
        enriched = []
        for p in participants:
            if p.get("type") == "user" and p.get("id"):
                enriched.append(jira_user_lookup(cfg, headers, p["id"]))
            else:
                enriched.append(p)
        print(json.dumps({"on_call": enriched, "raw": data}, indent=2))


def cmd_timeline(args, cfg, headers):
    qs = {"startDate": to_iso_datetime(args.from_date),
          "endDate": to_iso_datetime(args.to_date)}
    path = f"/schedules/{args.schedule_id}/timeline?" + urllib.parse.urlencode(qs)
    data = request("GET", api_url(cfg, args.cloud_id, path), headers)
    print(json.dumps(data, indent=2))


def cmd_weekly(args, cfg, headers):
    """Resolve the 2 on-call participants for a 14-day sprint window.

    week1 = participant on duty at sprint_start (day 0, 00:00 UTC).
    week2 = participant on duty at sprint_start + 7 days (00:00 UTC).

    Returns name + email for each, resolved via Jira user lookup.
    """
    start = dt.date.fromisoformat(args.sprint_start)
    week2_start = start + dt.timedelta(days=7)

    def slot(d: dt.date, week_label: str) -> dict:
        users = fetch_oncall_for_datetime(
            cfg, args.cloud_id, headers, args.schedule_id,
            to_iso_datetime(d.isoformat()),
        )
        first = users[0] if users else {"name": None, "email": None,
                                        "accountId": None}
        return {
            "week": week_label,
            "date": d.isoformat(),
            "name": first.get("name"),
            "email": first.get("email"),
            "accountId": first.get("accountId"),
        }

    out = {
        "sprint_start": start.isoformat(),
        "week1": slot(start, "week1"),
        "week2": slot(week2_start, "week2"),
    }
    print(json.dumps(out, indent=2))


def main():
    p = argparse.ArgumentParser(prog="jsm_oncall.py")
    p.add_argument("--cloud-id", help="Atlassian cloud ID (else from config)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("discover", help="List teams and schedules to find IDs")

    sp = sub.add_parser("oncall", help="Who is on-call right now on a schedule")
    sp.add_argument("--schedule-id", required=True)
    sp.add_argument("--date", help="ISO date (default: now)")

    sp = sub.add_parser("timeline", help="On-call timeline for a window")
    sp.add_argument("--schedule-id", required=True)
    sp.add_argument("--from", dest="from_date", required=True)
    sp.add_argument("--to", dest="to_date", required=True)

    sp = sub.add_parser("weekly", help="Resolve week-1 and week-2 of a 14-day sprint")
    sp.add_argument("--schedule-id", required=True)
    sp.add_argument("--sprint-start", required=True, help="ISO date of week-1 day-0")

    args = p.parse_args()
    cfg = load_config()
    email = get_email(cfg)
    token = get_token(cfg)
    headers = {
        "Authorization": auth_header(email, token),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    {
        "discover": cmd_discover,
        "oncall": cmd_oncall,
        "timeline": cmd_timeline,
        "weekly": cmd_weekly,
    }[args.cmd](args, cfg, headers)


if __name__ == "__main__":
    main()
