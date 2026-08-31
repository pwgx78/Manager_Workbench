"""
ooo_logic.py — pure reconciliation & coverage logic for the OoO Management tool.

No Streamlit / network imports, so it is unit-testable in isolation. Works on
normalized OoO records (dicts) of the shape:

    {"member": str, "type": str, "start_date": "YYYY-MM-DD",
     "end_date": "YYYY-MM-DD", "details": str, "date_sent": "YYYY-MM-DD"?}

Matching is done at DAY granularity per member, which is what makes the edge
cases work: one multi-day calendar invite vs. several single-day system requests
(and vice versa) reconcile because we compare the union of covered days, not item
counts.
"""
from datetime import date, datetime, timedelta

_MAX_SPAN_DAYS = 370  # guard against bad/unbounded LLM date ranges


def parse_date(s):
    """Parse 'YYYY-MM-DD' (or an ISO datetime) to a date; None if unparseable."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s)[:10]).date()
    except (ValueError, TypeError):
        return None


def expand_dates(start, end):
    """Inclusive list of date objects from start..end. [] if invalid or reversed.
    Clamped to _MAX_SPAN_DAYS to stay safe against bad inputs."""
    s, e = parse_date(start), parse_date(end)
    if s is None and e is None:
        return []
    if s is None:
        s = e
    if e is None:
        e = s
    if e < s:
        return []
    if (e - s).days > _MAX_SPAN_DAYS:
        e = s + timedelta(days=_MAX_SPAN_DAYS)
    return [s + timedelta(days=i) for i in range((e - s).days + 1)]


def _item_days(item):
    return set(expand_dates(item.get("start_date"), item.get("end_date")))


def _fmt_range(item):
    s, e = item.get("start_date", ""), item.get("end_date", "")
    if not s and not e:
        return ""
    if s == e or not e:
        return s or e
    return f"{s} → {e}"


def _status(days, other_union, missing_label):
    """Match status from this item's day-set vs. the other source's day-union."""
    if not days:
        return "⚠️ No dates"
    covered = days & other_union
    if covered == days:
        return "✅ Matched"
    if covered:
        return f"⚠️ Partial ({len(covered)}/{len(days)} days)"
    return missing_label


def reconcile(system_requests, calendar_invites):
    """Reconcile ONE member's system requests against their calendar invites.
    Returns rows sorted by start date:
        {source, date_sent, type, dates, details, match_status}
    """
    sys_union = set()
    for r in system_requests:
        sys_union |= _item_days(r)
    cal_union = set()
    for c in calendar_invites:
        cal_union |= _item_days(c)

    rows = []
    for r in system_requests:
        rows.append(
            {
                "source": "System",
                "date_sent": r.get("date_sent", ""),
                "type": r.get("type", ""),
                "dates": _fmt_range(r),
                "details": r.get("details", ""),
                "match_status": _status(
                    _item_days(r), cal_union, "🟧 No calendar (request only)"
                ),
                "_start": r.get("start_date", ""),
            }
        )
    for c in calendar_invites:
        rows.append(
            {
                "source": "Calendar",
                "date_sent": c.get("date_sent", ""),
                "type": c.get("type", ""),
                "dates": _fmt_range(c),
                "details": c.get("details", ""),
                "match_status": _status(
                    _item_days(c), sys_union, "🟥 No approval (calendar only)"
                ),
                "_start": c.get("start_date", ""),
            }
        )
    rows.sort(key=lambda x: (x["_start"] or "9999", x["source"]))
    for x in rows:
        x.pop("_start", None)
    return rows


def coverage_by_day(items, start, end, weekdays_only=True):
    """Map each day in [start, end] to the set of members out that day. `items`
    is the combined system+calendar list (each with a 'member'); using a set per
    day means a member counted once even if present in both sources."""
    window = expand_dates(
        start.isoformat() if isinstance(start, date) else start,
        end.isoformat() if isinstance(end, date) else end,
    )
    window_set = set(window)
    coverage = {d: set() for d in window}
    for it in items:
        member = it.get("member") or "Other"
        if member == "Other":
            continue
        for d in _item_days(it):
            if d in window_set:
                if weekdays_only and d.weekday() >= 5:
                    continue
                coverage[d].add(member)
    return coverage


def thin_coverage(coverage, threshold):
    """Days where the number of members out meets/exceeds threshold.
    Returns [(date, sorted_members), ...] sorted by date."""
    flagged = [
        (d, sorted(members))
        for d, members in coverage.items()
        if len(members) >= threshold
    ]
    flagged.sort(key=lambda x: x[0])
    return flagged


def summarize_upcoming(items, start, end):
    """Per-member upcoming time-off summary within [start, end]:
        {member: {"days": int, "next": date|None, "ranges": [str, ...]}}
    `days` counts distinct calendar days; `ranges` are the formatted spans."""
    window = set(expand_dates(
        start.isoformat() if isinstance(start, date) else start,
        end.isoformat() if isinstance(end, date) else end,
    ))
    summary = {}
    for it in items:
        member = it.get("member") or "Other"
        days = _item_days(it) & window
        if not days:
            continue
        s = summary.setdefault(member, {"day_set": set(), "ranges": []})
        s["day_set"] |= days
        rng = _fmt_range(it)
        if rng and rng not in s["ranges"]:
            s["ranges"].append(rng)
    out = {}
    for member, s in summary.items():
        day_set = s["day_set"]
        out[member] = {
            "days": len(day_set),
            "next": min(day_set) if day_set else None,
            "ranges": s["ranges"],
        }
    return out
