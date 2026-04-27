#!/usr/bin/env python3
"""Allocate time into non-overlapping buckets for a single day.

Reads a draft request from stdin as JSON, writes an allocated plan to stdout.

Input shape:
{
  "target_date": "2026-04-22",
  "day_start_hour": 8,
  "daily_hours_minimum": 8,
  "min_bucket_minutes": 15,
  "meetings": [
    {"start": "10:00", "end": "11:00", "ticket": "SFDC-9500", "description": "..."}
  ],
  "activity_weights": {
    "SFDC-10201": 5,
    "SFDC-10150": 2
  },
  "existing_worklogs": [
    {"start": "08:00", "end": "09:00", "ticket": "SFDC-9999"}
  ],
  "on_call_topup": {"ticket": "SFDC-9876", "hours": 1.0},
  "pto": false,
  "max_entry_minutes": 120,
  "ticket_caps": {
    "SFDC-10182": {"original_estimate_seconds": 28800, "logged_seconds": 18000}
  }
}

Output shape:
{
  "entries": [
    {"start": "08:00", "end": "09:00", "duration_minutes": 60,
     "ticket": "SFDC-10201", "source": "activity", "description": null}
  ],
  "unaccounted_minutes": 0,
  "total_minutes": 480,
  "exceeds_minimum": false,
  "cap_warnings": [
    {"ticket": "SFDC-10182", "would_be_minutes": 540,
     "estimate_minutes": 480, "logged_minutes": 300, "trimmed_to_minutes": 180}
  ]
}

Note: `daily_hours_minimum` is a FLOOR. Fixed meetings are always preserved.
Activity is added only to reach the floor; if fixed slots already meet or
exceed the floor, total_minutes may equal or exceed daily_hours_minimum —
that is expected, not an error.

Source values: "meeting" | "existing" | "on_call" | "activity" | "pto"
Entries with source="existing" indicate slots already in Tempo (idempotency info).
"""

import json
import sys
from datetime import datetime, timedelta


def parse_hm(s: str) -> datetime:
    return datetime.strptime(s, "%H:%M")


def fmt_hm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def minutes_between(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 60)


def round_up_to_bucket(minutes: int, bucket: int) -> int:
    if minutes % bucket == 0:
        return minutes
    return minutes + (bucket - minutes % bucket)


def allocate(req: dict) -> dict:
    day_start = parse_hm(f"{req['day_start_hour']:02d}:00")
    # Accept `daily_hours_minimum` (preferred) or legacy `daily_hours_target`.
    min_hours = req.get("daily_hours_minimum", req.get("daily_hours_target"))
    min_minutes = int(min_hours * 60)
    bucket = req["min_bucket_minutes"]
    max_entry_minutes = int(req.get("max_entry_minutes") or 0)

    if req.get("pto"):
        plan = _pto_plan(day_start, min_minutes, req.get("pto_fallback_ticket", "ADMIN-4"))
        plan["cap_warnings"] = []
        if max_entry_minutes > 0:
            plan["entries"] = _split_long_entries(plan["entries"], max_entry_minutes)
        return plan

    meetings = sorted(
        [_parse_slot(m, "meeting") for m in req.get("meetings", [])],
        key=lambda x: x["start"],
    )
    existing = sorted(
        [_parse_slot(e, "existing") for e in req.get("existing_worklogs", [])],
        key=lambda x: x["start"],
    )

    fixed = _merge_fixed_slots(meetings, existing)
    fixed_minutes = sum(minutes_between(s["start"], s["end"]) for s in fixed)

    topup = req.get("on_call_topup") or {}
    topup_minutes = int(round((topup.get("hours") or 0) * 60))
    topup_ticket = topup.get("ticket")

    # Floor semantics: only PAD activity up to the daily minimum. If fixed
    # slots already meet or exceed the minimum, no activity is added but
    # no meetings are ever trimmed — the day can legitimately exceed 8h.
    activity_budget = max(0, min_minutes - fixed_minutes - topup_minutes)

    weights = dict(req.get("activity_weights") or {})
    ticket_caps = req.get("ticket_caps") or {}

    # Enforce per-ticket Original Estimate caps on the WEIGHTED budget. If the
    # user's drafted weight would push a ticket past its remaining estimate,
    # trim the weight to the remaining estimate and emit a warning. Spillover
    # (the trimmed minutes) reroutes to the next-highest-weighted ticket that
    # still has cap headroom; otherwise it stays unaccounted (surfaced as a
    # warning to the user, who can adjust manually before submission).
    cap_warnings: list[dict] = []
    weights, cap_warnings = _apply_ticket_caps(
        weights, activity_budget, ticket_caps, bucket
    )

    entries = list(fixed)

    if topup_minutes > 0 and topup_ticket:
        placed = _place_in_gaps(entries, topup_ticket, topup_minutes, bucket,
                                day_start, source="on_call")
        entries.extend(placed)
        entries.sort(key=lambda x: x["start"])

    if activity_budget > 0 and weights:
        buckets = _split_by_weight(weights, activity_budget, bucket)
        for ticket, mins in buckets:
            placed = _place_in_gaps(entries, ticket, mins, bucket,
                                    day_start, source="activity")
            entries.extend(placed)
            entries.sort(key=lambda x: x["start"])

    filled_minutes = sum(
        minutes_between(e["start"], e["end"])
        for e in entries if e["source"] != "existing"
    )
    unaccounted = max(0, min_minutes - filled_minutes - _minutes_of(existing))
    total = filled_minutes + _minutes_of(existing)

    serialized = [_serialize(e) for e in sorted(entries, key=lambda x: x["start"])]
    if max_entry_minutes > 0:
        serialized = _split_long_entries(serialized, max_entry_minutes)

    return {
        "entries": serialized,
        "unaccounted_minutes": unaccounted,
        "total_minutes": total,
        "exceeds_minimum": total > min_minutes,
        "cap_warnings": cap_warnings,
    }


def _apply_ticket_caps(weights: dict, activity_budget: int,
                       ticket_caps: dict, bucket: int) -> tuple[dict, list[dict]]:
    """Trim activity weights to fit per-ticket Original Estimate caps.

    A cap entry is {"original_estimate_seconds": N, "logged_seconds": M}.
    Remaining headroom = (N - M) seconds. When the proportional split would
    allocate more than the remaining headroom to a ticket, we cap that
    ticket's weight share (in minutes) and emit a warning. Trimmed minutes
    spill over to the highest-weighted uncapped ticket, if any.
    """
    if not weights or not ticket_caps or activity_budget <= 0:
        return weights, []

    total_weight = sum(weights.values())
    if total_weight <= 0:
        return weights, []

    warnings: list[dict] = []
    proposed: dict[str, int] = {}
    for t, w in weights.items():
        proposed[t] = int(activity_budget * (w / total_weight))

    spillover = 0
    capped_tickets: set[str] = set()
    for t, mins in list(proposed.items()):
        cap = ticket_caps.get(t)
        if not cap:
            continue
        est = cap.get("original_estimate_seconds")
        if est is None:
            continue
        logged = cap.get("logged_seconds") or 0
        remaining_minutes = max(0, (est - logged) // 60)
        if mins > remaining_minutes:
            spillover += (mins - remaining_minutes)
            warnings.append({
                "ticket": t,
                "would_be_minutes": mins + (logged // 60),
                "estimate_minutes": est // 60,
                "logged_minutes": logged // 60,
                "trimmed_to_minutes": remaining_minutes,
            })
            proposed[t] = remaining_minutes
            capped_tickets.add(t)

    if spillover > 0:
        # Distribute spillover to uncapped tickets in descending weight order.
        uncapped = sorted(
            [(t, w) for t, w in weights.items() if t not in capped_tickets],
            key=lambda x: -x[1],
        )
        for t, _ in uncapped:
            cap = ticket_caps.get(t) or {}
            est = cap.get("original_estimate_seconds")
            if est is not None:
                logged = cap.get("logged_seconds") or 0
                room_for_t = max(0, (est - logged) // 60 - proposed[t])
                if room_for_t <= 0:
                    continue
                give = min(spillover, room_for_t)
            else:
                give = spillover
            proposed[t] = proposed.get(t, 0) + give
            spillover -= give
            if spillover <= 0:
                break

    # Convert minutes back to weights for downstream allocator (it expects
    # weights, not minutes). Pass minutes-as-weights since their relative
    # proportions are now exactly what we want.
    return {t: max(0, m) for t, m in proposed.items() if m > 0}, warnings


def _split_long_entries(entries: list, max_entry_minutes: int) -> list:
    """Split any non-meeting / non-existing entry > max_entry_minutes into
    back-to-back chunks of <= max_entry_minutes. Descriptions get an "(n/N)"
    suffix; ticket and source carry over.
    """
    out: list = []
    for e in entries:
        dur = e.get("duration_minutes")
        src = e.get("source")
        if (
            dur is None
            or dur <= max_entry_minutes
            or src in ("meeting", "existing")
        ):
            out.append(e)
            continue
        # Number of chunks = ceil(dur / max)
        n_chunks = (dur + max_entry_minutes - 1) // max_entry_minutes
        start_dt = parse_hm(e["start"])
        remaining = dur
        for i in range(n_chunks):
            take = min(max_entry_minutes, remaining)
            chunk_end = start_dt + timedelta(minutes=take)
            base_desc = e.get("description") or ""
            chunked_desc = (
                f"{base_desc} ({i + 1}/{n_chunks})".lstrip().strip()
                if base_desc else f"({i + 1}/{n_chunks})"
            )
            out.append({
                "start": fmt_hm(start_dt),
                "end": fmt_hm(chunk_end),
                "duration_minutes": take,
                "ticket": e.get("ticket"),
                "source": src,
                "description": chunked_desc,
            })
            start_dt = chunk_end
            remaining -= take
    return out


def _pto_plan(day_start: datetime, min_minutes: int, ticket: str) -> dict:
    end = day_start + timedelta(minutes=min_minutes)
    return {
        "entries": [{
            "start": fmt_hm(day_start),
            "end": fmt_hm(end),
            "duration_minutes": min_minutes,
            "ticket": ticket,
            "source": "pto",
            "description": "PTO / Out of office",
        }],
        "unaccounted_minutes": 0,
        "total_minutes": min_minutes,
        "exceeds_minimum": False,
    }


def _parse_slot(s: dict, source: str) -> dict:
    return {
        "start": parse_hm(s["start"]),
        "end": parse_hm(s["end"]),
        "ticket": s.get("ticket"),
        "source": source,
        "description": s.get("description"),
    }


def _merge_fixed_slots(meetings: list, existing: list) -> list:
    # Meetings and existing entries are immutable. Sort them; if they collide,
    # shift meetings later (existing worklogs win — idempotency). Downstream gap
    # placement ignores any area covered by fixed slots.
    fixed = list(existing)
    for m in meetings:
        m_start, m_end = m["start"], m["end"]
        for e in fixed:
            if _overlaps(m_start, m_end, e["start"], e["end"]):
                m_end = m_end + (e["end"] - m_start)
                m_start = e["end"]
        if m_end > m_start:
            m["start"], m["end"] = m_start, m_end
            fixed.append(m)
    fixed.sort(key=lambda x: x["start"])
    return fixed


def _overlaps(a_start: datetime, a_end: datetime,
              b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def _split_by_weight(weights: dict, total_minutes: int, bucket: int) -> list:
    total_weight = sum(weights.values())
    if total_weight <= 0:
        top = max(weights.items(), key=lambda x: x[1])[0] if weights else None
        return [(top, total_minutes)] if top else []
    raw = [(t, total_minutes * (w / total_weight)) for t, w in weights.items()]
    rounded = [(t, max(bucket, bucket * round(m / bucket))) for t, m in raw]
    drift = sum(m for _, m in rounded) - total_minutes
    if drift != 0 and rounded:
        rounded.sort(key=lambda x: -x[1])
        t, m = rounded[0]
        rounded[0] = (t, max(bucket, m - drift))
    return [(t, m) for t, m in rounded if m > 0]


def _place_in_gaps(placed: list, ticket: str, minutes_needed: int, bucket: int,
                   day_start: datetime, source: str) -> list:
    # Build gap list between placed entries (sorted), starting at day_start.
    placed_sorted = sorted(placed, key=lambda x: x["start"])
    cursor = day_start
    gaps = []
    for e in placed_sorted:
        if e["start"] > cursor:
            gaps.append((cursor, e["start"]))
        cursor = max(cursor, e["end"])
    gaps.append((cursor, cursor + timedelta(hours=16)))  # open tail

    result = []
    remaining = minutes_needed
    for gap_start, gap_end in gaps:
        if remaining <= 0:
            break
        avail = minutes_between(gap_start, gap_end)
        if avail < bucket:
            continue
        take = min(remaining, avail)
        take = (take // bucket) * bucket
        if take < bucket:
            take = bucket if avail >= bucket else 0
        if take == 0:
            continue
        entry_start = gap_start
        entry_end = gap_start + timedelta(minutes=take)
        result.append({
            "start": entry_start,
            "end": entry_end,
            "ticket": ticket,
            "source": source,
            "description": None,
        })
        remaining -= take
    return result


def _minutes_of(slots: list) -> int:
    return sum(minutes_between(s["start"], s["end"]) for s in slots)


def _serialize(e: dict) -> dict:
    return {
        "start": fmt_hm(e["start"]),
        "end": fmt_hm(e["end"]),
        "duration_minutes": minutes_between(e["start"], e["end"]),
        "ticket": e["ticket"],
        "source": e["source"],
        "description": e.get("description"),
    }


def main() -> None:
    req = json.load(sys.stdin)
    plan = allocate(req)
    json.dump(plan, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
