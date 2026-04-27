#!/usr/bin/env python3
"""Scan Claude Code session transcripts across ALL local projects for Jira
ticket mentions on a given IST day.

Walks ~/.claude/projects/*/*.jsonl, parses each session line-by-line, and
for messages whose timestamp falls inside the target IST day extracts every
[A-Z]+-\\d+ token. Aggregates counts per ticket with per-cwd attribution.

Usage:
    scan_sessions.py --date YYYY-MM-DD [--tz Asia/Kolkata]

Output (stdout):
{
  "tickets": {
    "SFDC-10182": {"count": 47, "sessions": 3, "cwds": ["/path/a", "/path/b"]},
    ...
  },
  "session_count": 8,
  "messages_scanned": 1284,
  "projects_scanned": 13,
  "errors": [{"file": "...", "error": "..."}]
}
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
from datetime import datetime, timedelta, timezone

PROJECTS_ROOT = pathlib.Path.home() / ".claude" / "projects"
TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


def ist_offset() -> timezone:
    # Asia/Kolkata = UTC+5:30, no DST
    return timezone(timedelta(hours=5, minutes=30))


def day_window_utc(date_str: str) -> tuple[datetime, datetime]:
    """Return [start, end) in UTC for the IST calendar day."""
    ist = ist_offset()
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=ist)
    start = d.astimezone(timezone.utc)
    end = (d + timedelta(days=1)).astimezone(timezone.utc)
    return start, end


def parse_ts(raw) -> datetime | None:
    if not raw:
        return None
    if isinstance(raw, (int, float)):
        # epoch (seconds or ms)
        ts = raw / 1000.0 if raw > 1e12 else raw
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(raw, str):
        try:
            # ISO-8601, accept Z suffix
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def extract_text(msg: dict) -> str:
    """Pull every text-like string out of a session message."""
    parts = []
    content = msg.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for c in content:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict):
                for key in ("text", "input", "name", "command", "description"):
                    v = c.get(key)
                    if isinstance(v, str):
                        parts.append(v)
                    elif isinstance(v, dict):
                        parts.append(json.dumps(v))
    # Some session formats nest "message"
    inner = msg.get("message")
    if isinstance(inner, dict):
        parts.append(extract_text(inner))
    return " ".join(parts)


def scan_session(path: pathlib.Path, start_utc: datetime, end_utc: datetime
                 ) -> tuple[set[str], int, str | None]:
    """Return (tickets_in_window, messages_scanned, cwd_or_none)."""
    tickets: set[str] = set()
    msgs = 0
    cwd: str | None = None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if cwd is None:
                cwd = msg.get("cwd") or msg.get("workingDirectory")
            ts = parse_ts(
                msg.get("timestamp")
                or msg.get("createdAt")
                or msg.get("time")
            )
            if ts is None:
                continue
            if ts < start_utc or ts >= end_utc:
                continue
            msgs += 1
            text = extract_text(msg)
            for m in TICKET_RE.findall(text):
                tickets.add(m)
    return tickets, msgs, cwd


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="Target date YYYY-MM-DD (IST)")
    ap.add_argument("--tz", default="Asia/Kolkata",
                    help="Currently informational; IST is hardcoded")
    args = ap.parse_args()

    start_utc, end_utc = day_window_utc(args.date)

    if not PROJECTS_ROOT.is_dir():
        json.dump({"tickets": {}, "session_count": 0, "messages_scanned": 0,
                   "projects_scanned": 0, "errors": []}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    aggregate: dict[str, dict] = {}
    session_count = 0
    messages_scanned = 0
    errors: list[dict] = []
    projects_scanned = 0

    for project_dir in sorted(PROJECTS_ROOT.iterdir()):
        if not project_dir.is_dir():
            continue
        projects_scanned += 1
        for session_file in project_dir.glob("*.jsonl"):
            # Cheap mtime filter — if the file was last touched before the
            # IST day even started, no message in it can be in the window.
            try:
                mtime = datetime.fromtimestamp(session_file.stat().st_mtime,
                                               tz=timezone.utc)
            except OSError:
                continue
            if mtime < start_utc:
                continue
            try:
                tickets, msgs, cwd = scan_session(session_file, start_utc, end_utc)
            except Exception as e:
                errors.append({"file": str(session_file), "error": str(e)})
                continue
            if msgs == 0 and not tickets:
                continue
            session_count += 1
            messages_scanned += msgs
            for t in tickets:
                bucket = aggregate.setdefault(
                    t, {"count": 0, "sessions": 0, "cwds": []}
                )
                bucket["count"] += 1
                bucket["sessions"] += 1
                if cwd and cwd not in bucket["cwds"]:
                    bucket["cwds"].append(cwd)

    out = {
        "tickets": aggregate,
        "session_count": session_count,
        "messages_scanned": messages_scanned,
        "projects_scanned": projects_scanned,
        "errors": errors,
    }
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
