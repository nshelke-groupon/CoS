#!/usr/bin/env python3
"""Tempo REST wrapper.

CLI:
  tempo_api.py list   --account-id <id> --from YYYY-MM-DD --to YYYY-MM-DD
  tempo_api.py create --account-id <id> --issue-id <N> --seconds <N> \
                      --start-date YYYY-MM-DD --start-time HH:MM:SS \
                      [--description "..."]
  tempo_api.py delete --worklog-id <id>

All successful responses print JSON to stdout. Errors print to stderr, exit 1.
Token is read from macOS Keychain (service=tempo_api_token, account=nshelke)
or from env TEMPO_API_TOKEN as fallback.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

API_BASE = "https://api.tempo.io/4"


def get_token(service: str = "tempo_api_token", account: str = "nshelke") -> str:
    env_token = os.environ.get("TEMPO_API_TOKEN")
    if env_token:
        return env_token
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        sys.stderr.write(
            "ERROR: Tempo token not found. Set TEMPO_API_TOKEN env var or add to Keychain:\n"
            f"  security add-generic-password -s {service} -a {account} -w <token>\n"
        )
        sys.exit(1)


def http(method: str, path: str, token: str, body: dict | None = None) -> dict:
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        sys.stderr.write(f"ERROR: Tempo {method} {path} → HTTP {e.code}\n{detail}\n")
        sys.exit(1)


def list_worklogs(token: str, account_id: str, from_date: str, to_date: str) -> list:
    path = f"/worklogs/user/{account_id}?from={from_date}&to={to_date}&limit=1000"
    results = []
    while path:
        page = http("GET", path, token)
        results.extend(page.get("results", []))
        next_link = page.get("metadata", {}).get("next")
        if not next_link:
            break
        path = next_link.replace(API_BASE, "")
    return results


def create_worklog(token: str, account_id: str, issue_id: int, seconds: int,
                   start_date: str, start_time: str, description: str) -> dict:
    body = {
        "authorAccountId": account_id,
        "issueId": issue_id,
        "timeSpentSeconds": seconds,
        "startDate": start_date,
        "startTime": start_time,
        "description": description or "",
    }
    return http("POST", "/worklogs", token, body)


def delete_worklog(token: str, worklog_id: int) -> None:
    http("DELETE", f"/worklogs/{worklog_id}", token)


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("list")
    lp.add_argument("--account-id", required=True)
    lp.add_argument("--from", dest="from_date", required=True)
    lp.add_argument("--to", dest="to_date", required=True)

    cp = sub.add_parser("create")
    cp.add_argument("--account-id", required=True)
    cp.add_argument("--issue-id", type=int, required=True)
    cp.add_argument("--seconds", type=int, required=True)
    cp.add_argument("--start-date", required=True)
    cp.add_argument("--start-time", required=True)
    cp.add_argument("--description", default="")

    dp = sub.add_parser("delete")
    dp.add_argument("--worklog-id", type=int, required=True)

    args = p.parse_args()
    token = get_token()

    if args.cmd == "list":
        out = list_worklogs(token, args.account_id, args.from_date, args.to_date)
    elif args.cmd == "create":
        out = create_worklog(token, args.account_id, args.issue_id, args.seconds,
                             args.start_date, args.start_time, args.description)
    elif args.cmd == "delete":
        delete_worklog(token, args.worklog_id)
        out = {"deleted": args.worklog_id}
    else:
        p.error(f"unknown command: {args.cmd}")
        return

    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
