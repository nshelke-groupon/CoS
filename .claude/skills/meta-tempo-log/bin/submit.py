#!/usr/bin/env python3
"""Idempotent Tempo worklog submitter.

Reads an approved draft from stdin (JSON), checks against existing Tempo
worklogs for the day, creates only the missing entries, and appends every
action to the audit CSV.

Input shape:
{
  "target_date": "2026-04-22",
  "account_id": "712020:...",
  "entries": [
    {"ticket_key": "SFDC-10201", "issue_id": 12345,
     "start": "08:00", "end": "09:00", "description": "..."}
  ],
  "dry_run": false,
  "ticket_caps": {
    "SFDC-10201": {"original_estimate_seconds": 28800, "logged_seconds": 18000}
  }
}

When `ticket_caps` is present, each entry is checked at submit time
against (estimate - logged - already_submitted_in_this_run). Entries
that would push the ticket over its Original Estimate are skipped with
reason "exceeds_estimate" and logged to the audit CSV. The skill is
expected to refresh `ticket_caps` immediately before calling submit so
the values reflect current Jira state.

Output shape:
{
  "submitted": [{"ticket_key": "...", "start": "...", "worklog_id": 789}],
  "skipped":   [{"ticket_key": "...", "start": "...", "reason": "duplicate"}],
  "failed":    [{"ticket_key": "...", "start": "...", "error": "..."}]
}
"""

import csv
import json
import os
import pathlib
import sys
from datetime import datetime

# Local import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tempo_api  # noqa: E402

SKILL_DIR = pathlib.Path(__file__).parent.parent
CONFIG_PATH = SKILL_DIR / "config.json"
LOCAL_CONFIG_PATH = SKILL_DIR / "config.local.json"


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    if LOCAL_CONFIG_PATH.exists():
        with open(LOCAL_CONFIG_PATH) as f:
            local = json.load(f)
        # Drop commentary keys starting with underscore (from the .example template)
        local = {k: v for k, v in local.items() if not k.startswith("_")}
        cfg = _deep_merge(cfg, local)
    if not cfg.get("atlassian_account_id") or not cfg.get("keychain", {}).get("account"):
        raise SystemExit(
            "Missing atlassian_account_id or keychain.account. "
            f"Copy {SKILL_DIR}/config.local.json.example to config.local.json "
            "and fill in your personal values."
        )
    return cfg


def expand(p: str) -> pathlib.Path:
    return pathlib.Path(os.path.expanduser(p))


def to_seconds(start: str, end: str) -> int:
    fmt = "%H:%M"
    s = datetime.strptime(start, fmt)
    e = datetime.strptime(end, fmt)
    return int((e - s).total_seconds())


def make_key(issue_id: int, start_date: str, start_time: str, seconds: int) -> tuple:
    return (int(issue_id), start_date, start_time[:5], int(seconds))


def existing_keys(worklogs: list) -> set:
    keys = set()
    for w in worklogs:
        issue_id = w.get("issue", {}).get("id") or w.get("issueId")
        start_date = w.get("startDate")
        start_time = (w.get("startTime") or "")[:5]
        seconds = w.get("timeSpentSeconds")
        if issue_id and start_date and start_time and seconds is not None:
            keys.add((int(issue_id), start_date, start_time, int(seconds)))
    return keys


def append_audit(audit_path: pathlib.Path, row: list) -> None:
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not audit_path.exists()
    with open(audit_path, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["timestamp", "action", "date", "ticket", "issue_id",
                        "start_time", "seconds", "worklog_id", "description", "note"])
        w.writerow(row)


def main() -> None:
    req = json.load(sys.stdin)
    cfg = load_config()
    token = tempo_api.get_token(
        cfg["keychain"]["service"], cfg["keychain"]["account"]
    )
    audit_path = expand(cfg["paths"]["audit"])
    account_id = req.get("account_id") or cfg["atlassian_account_id"]
    target_date = req["target_date"]
    dry_run = bool(req.get("dry_run", False))

    existing = tempo_api.list_worklogs(token, account_id, target_date, target_date)
    existing = [w for w in existing if _author_matches(w, account_id)]
    seen = existing_keys(existing)

    # Per-ticket cap state — track seconds about to be added in this run so
    # we don't double-count when the same ticket has multiple entries.
    ticket_caps = req.get("ticket_caps") or {}
    pending_seconds: dict = {}

    submitted, skipped, failed = [], [], []
    now = datetime.now().isoformat(timespec="seconds")

    for entry in req["entries"]:
        seconds = to_seconds(entry["start"], entry["end"])
        start_time_full = entry["start"] + ":00"
        key = make_key(entry["issue_id"], target_date, start_time_full, seconds)

        if key in seen:
            skipped.append({
                "ticket_key": entry["ticket_key"],
                "start": entry["start"],
                "reason": "duplicate",
            })
            append_audit(audit_path, [now, "skip", target_date, entry["ticket_key"],
                                      entry["issue_id"], entry["start"], seconds,
                                      "", entry.get("description", ""), "duplicate"])
            continue

        # Hard-block on Original Estimate cap. The skill is expected to have
        # refreshed `ticket_caps` immediately before calling submit; this is
        # the last-line-of-defense check that catches any drift between draft
        # approval and submit (e.g., user took a long time to confirm and
        # someone else logged time on the same ticket in between).
        cap = ticket_caps.get(entry["ticket_key"])
        if cap and cap.get("original_estimate_seconds") is not None:
            est = int(cap["original_estimate_seconds"])
            already_logged = int(cap.get("logged_seconds") or 0)
            already_pending = pending_seconds.get(entry["ticket_key"], 0)
            projected = already_logged + already_pending + seconds
            if projected > est:
                reason = (
                    f"exceeds_estimate "
                    f"(est={est}s, logged={already_logged}s, "
                    f"pending={already_pending}s, this={seconds}s)"
                )
                skipped.append({
                    "ticket_key": entry["ticket_key"],
                    "start": entry["start"],
                    "reason": "exceeds_estimate",
                    "estimate_seconds": est,
                    "logged_seconds": already_logged,
                })
                append_audit(audit_path, [now, "skip", target_date, entry["ticket_key"],
                                          entry["issue_id"], entry["start"], seconds,
                                          "", entry.get("description", ""), reason])
                continue

        if dry_run:
            submitted.append({
                "ticket_key": entry["ticket_key"],
                "start": entry["start"],
                "worklog_id": None,
                "dry_run": True,
            })
            continue

        try:
            resp = tempo_api.create_worklog(
                token, account_id, int(entry["issue_id"]), seconds,
                target_date, start_time_full, entry.get("description", "") or "",
            )
            worklog_id = resp.get("tempoWorklogId") or resp.get("id")
            submitted.append({
                "ticket_key": entry["ticket_key"],
                "start": entry["start"],
                "worklog_id": worklog_id,
            })
            append_audit(audit_path, [now, "create", target_date, entry["ticket_key"],
                                      entry["issue_id"], entry["start"], seconds,
                                      worklog_id, entry.get("description", ""), ""])
            seen.add(key)
            pending_seconds[entry["ticket_key"]] = (
                pending_seconds.get(entry["ticket_key"], 0) + seconds
            )
        except SystemExit:
            raise
        except Exception as e:
            failed.append({
                "ticket_key": entry["ticket_key"],
                "start": entry["start"],
                "error": str(e),
            })
            append_audit(audit_path, [now, "fail", target_date, entry["ticket_key"],
                                      entry["issue_id"], entry["start"], seconds,
                                      "", entry.get("description", ""), str(e)])

    out = {"submitted": submitted, "skipped": skipped, "failed": failed,
           "dry_run": dry_run, "target_date": target_date}
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _author_matches(worklog: dict, account_id: str) -> bool:
    author = worklog.get("author", {})
    return (author.get("accountId") == account_id
            or worklog.get("authorAccountId") == account_id)


if __name__ == "__main__":
    main()
