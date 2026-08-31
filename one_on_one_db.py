"""
one_on_one_db.py — per-direct-report SQLite cache for the 1:1 Meeting Prep Assistant.

Its tables live in the active user profile's workbench.db (like email_db.py /
jira_db.py), so they travel with the user. Every time the prep page pulls from a
data source
(Jira / Outlook / the trackers / calendar), each item is upserted here keyed by a
deterministic natural key, so:

  - re-seen items refresh in place (no duplicates), and
  - genuinely new items accumulate across runs.

This lets a prep synthesize from ALL cached history for a report, not just the
current time window. The page's scope toggle chooses period-only vs all-history.

Two tables:
  - report_items : one row per gathered item (Jira ticket, email, action, PTO),
                   tagged with the direct report and its project (SPR#/name).
  - report_prep  : the last synthesized prep-doc per report, so revisiting the page
                   is instant.
"""
import sqlite3
import json
from datetime import datetime

import db


def init_db():
    """Create the cache tables if they don't already exist."""
    conn = db.connect()
    cursor = conn.cursor()
    # One row per gathered item for a direct report. `item_id` is a deterministic
    # natural key ("{report}|{source}|{natural_id}") so re-pulls refresh in place.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS report_items (
            item_id TEXT PRIMARY KEY,
            report TEXT,
            source TEXT,
            project TEXT,
            title TEXT,
            detail TEXT,
            item_date TEXT,
            status TEXT,
            raw_json TEXT,
            first_seen TIMESTAMP,
            last_seen TIMESTAMP
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_report_items_report ON report_items (report)"
    )
    # Last synthesized prep-doc per report (cached so revisiting is instant).
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS report_prep (
            report TEXT PRIMARY KEY,
            sections_json TEXT,
            scope TEXT,
            period TEXT,
            generated_at TIMESTAMP
        )
        """
    )
    # Curated, persistent project registry (survives across 1:1s). One row per
    # project the report is working: SPR (from Jira), Other (email-derived or
    # manually added), or Goal (imported from an appraisal doc). `status` lets a
    # project be closed without deleting its history.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS report_projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report TEXT,
            ptype TEXT,
            pkey TEXT,
            name TEXT,
            source TEXT,
            keywords TEXT,
            status TEXT,
            meta_json TEXT,
            created_at TIMESTAMP,
            closed_at TIMESTAMP,
            UNIQUE(report, ptype, pkey)
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_report_projects_report ON report_projects (report)"
    )
    # Persistent per-project action items. Added during a 1:1 and surfaced at the
    # next one with a Done flag (open -> done, with done_at timestamp).
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS project_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report TEXT,
            ptype TEXT,
            pkey TEXT,
            text TEXT,
            status TEXT,
            created_at TIMESTAMP,
            created_meeting TEXT,
            done_at TIMESTAMP
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_actions_proj "
        "ON project_actions (report, ptype, pkey)"
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
# report_items
# --------------------------------------------------------------------------- #
def _item_id(report, source, natural_id):
    return f"{report}|{source}|{natural_id}"


def upsert_item(report, source, natural_id, project, title, detail,
                item_date="", status="", raw=None):
    """Insert or refresh one cached item for a direct report.

    `natural_id` is a stable id within (report, source) — e.g. a Jira key, an
    Outlook message id, or a hash of the action text — so re-seeing the same item
    updates it rather than duplicating. `first_seen` is preserved across updates;
    `last_seen` advances each time the item is re-pulled."""
    conn = db.connect()
    cursor = conn.cursor()
    now = datetime.now()
    cursor.execute(
        """
        INSERT INTO report_items
            (item_id, report, source, project, title, detail, item_date, status,
             raw_json, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
            project = excluded.project,
            title = excluded.title,
            detail = excluded.detail,
            item_date = excluded.item_date,
            status = excluded.status,
            raw_json = excluded.raw_json,
            last_seen = excluded.last_seen;
        """,
        (
            _item_id(report, source, natural_id), report, source,
            project or "Unassigned", title or "", detail or "", item_date or "",
            status or "", json.dumps(raw or {}, default=str), now, now,
        ),
    )
    conn.commit()
    conn.close()


def get_items(report, since=None):
    """Return cached items for `report` as dicts, newest item_date first.
    If `since` (an ISO date string, e.g. '2026-07-01') is given, keep only items
    whose `item_date` is on/after it; items with no date are always kept (so
    undated action items aren't silently dropped from the period view)."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT source, project, title, detail, item_date, status "
        "FROM report_items WHERE report = ? ORDER BY item_date DESC",
        (report,),
    )
    rows = cursor.fetchall()
    conn.close()
    items = []
    for source, project, title, detail, item_date, status in rows:
        if since and item_date and item_date[:10] < since:
            continue
        items.append(
            {
                "source": source,
                "project": project or "Unassigned",
                "title": title or "",
                "detail": detail or "",
                "item_date": item_date or "",
                "status": status or "",
            }
        )
    return items


def distinct_projects(report):
    """Distinct project tags seen for a report (excluding 'Unassigned'/blank)."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT DISTINCT project FROM report_items "
        "WHERE report = ? AND project IS NOT NULL AND project != '' "
        "AND project != 'Unassigned' ORDER BY project",
        (report,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]


def jira_summaries(report):
    """Map {project_key: jira_summary} from cached Jira items for a report.

    Lets SPR project labels show the Jira Issue Summary alongside the key
    regardless of the synthesis scope — the summary persists in the cache even
    when the SPR's Jira ticket isn't in the current period's pull."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT project, title FROM report_items "
        "WHERE report = ? AND source = 'jira' AND title IS NOT NULL AND title != ''",
        (report,),
    )
    rows = cursor.fetchall()
    conn.close()
    return {p: t for p, t in rows if p}


def jira_meta(report):
    """Map {ticket_key: payload} from cached Jira items for a report, where
    `payload` is the structured dict stored in `raw_json` at gather time
    (summary, jira_status, phase, priority, digest, accomplished, slips,
    next_steps). Powers the 1:1 SPR table under either scope without re-pulling."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT project, raw_json FROM report_items "
        "WHERE report = ? AND source = 'jira'",
        (report,),
    )
    rows = cursor.fetchall()
    conn.close()
    meta = {}
    for project, raw in rows:
        if not project:
            continue
        try:
            payload = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            payload = {}
        if isinstance(payload, dict):
            meta[project] = payload
    return meta


def source_counts(report):
    """Return ({source: count, ...}, last_seen_str) for the cache-status caption."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT source, COUNT(*) FROM report_items WHERE report = ? GROUP BY source",
        (report,),
    )
    counts = {src: n for src, n in cursor.fetchall()}
    cursor.execute(
        "SELECT MAX(last_seen) FROM report_items WHERE report = ?", (report,)
    )
    last = cursor.fetchone()
    conn.close()
    return counts, (str(last[0])[:19] if last and last[0] else "")


def clear_report(report):
    """Delete the gathered-item cache and saved prep-doc for one report. Does NOT
    touch the curated project registry or actions (those are managed explicitly via
    close/reopen so a cache refresh never discards persistent, hand-curated work)."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM report_items WHERE report = ?", (report,))
    cursor.execute("DELETE FROM report_prep WHERE report = ?", (report,))
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
# report_projects — the persistent, curated project registry
# --------------------------------------------------------------------------- #
def upsert_project(report, ptype, pkey, name, source, keywords="", meta=None):
    """Insert or refresh a project. On conflict, refresh name/source/keywords/meta
    but PRESERVE status + created_at, so a re-pull never reopens a closed project
    or resets its provenance timestamp."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO report_projects
            (report, ptype, pkey, name, source, keywords, status, meta_json,
             created_at, closed_at)
        VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, NULL)
        ON CONFLICT(report, ptype, pkey) DO UPDATE SET
            name = excluded.name,
            source = excluded.source,
            keywords = excluded.keywords,
            meta_json = excluded.meta_json;
        """,
        (
            report, ptype, pkey, name or pkey, source, keywords or "",
            json.dumps(meta or {}, default=str), datetime.now(),
        ),
    )
    conn.commit()
    conn.close()


def list_projects(report, ptype=None, include_closed=False):
    """Return project rows (as dicts, meta decoded) for a report, optionally
    filtered to one ptype, newest first."""
    conn = db.connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    sql = "SELECT * FROM report_projects WHERE report = ?"
    params = [report]
    if ptype:
        sql += " AND ptype = ?"
        params.append(ptype)
    if not include_closed:
        sql += " AND status = 'open'"
    sql += " ORDER BY created_at DESC"
    rows = cursor.execute(sql, params).fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            meta = json.loads(r["meta_json"]) if r["meta_json"] else {}
        except (ValueError, TypeError):
            meta = {}
        out.append(
            {
                "id": r["id"], "report": r["report"], "ptype": r["ptype"],
                "pkey": r["pkey"], "name": r["name"] or r["pkey"],
                "source": r["source"] or "", "keywords": r["keywords"] or "",
                "status": r["status"] or "open", "meta": meta if isinstance(meta, dict) else {},
                "created_at": str(r["created_at"] or "")[:19],
                "closed_at": str(r["closed_at"] or "")[:19],
            }
        )
    return out


def set_project_status(report, ptype, pkey, status):
    """Close (status='closed') or reopen (status='open') a project; stamps closed_at."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE report_projects SET status = ?, closed_at = ? "
        "WHERE report = ? AND ptype = ? AND pkey = ?",
        (status, datetime.now() if status == "closed" else None, report, ptype, pkey),
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
# project_actions — persistent per-project action items
# --------------------------------------------------------------------------- #
def add_action(report, ptype, pkey, text, meeting=""):
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO project_actions "
        "(report, ptype, pkey, text, status, created_at, created_meeting, done_at) "
        "VALUES (?, ?, ?, ?, 'open', ?, ?, NULL)",
        (report, ptype, pkey, text, datetime.now(), meeting or ""),
    )
    conn.commit()
    conn.close()


def list_actions(report, ptype, pkey):
    """Return actions for one project, open first then done, each a dict."""
    conn = db.connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    rows = cursor.execute(
        "SELECT * FROM project_actions WHERE report = ? AND ptype = ? AND pkey = ? "
        "ORDER BY (status = 'done'), created_at",
        (report, ptype, pkey),
    ).fetchall()
    conn.close()
    return [
        {
            "id": r["id"], "text": r["text"] or "", "status": r["status"] or "open",
            "created_meeting": r["created_meeting"] or "",
            "created_at": str(r["created_at"] or "")[:19],
            "done_at": str(r["done_at"] or "")[:19],
        }
        for r in rows
    ]


def set_action_status(action_id, status):
    """Mark an action done (stamps done_at) or reopen it (clears done_at)."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE project_actions SET status = ?, done_at = ? WHERE id = ?",
        (status, datetime.now() if status == "done" else None, action_id),
    )
    conn.commit()
    conn.close()


def delete_action(action_id):
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM project_actions WHERE id = ?", (action_id,))
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
# report_prep — cached synthesized doc per report
# --------------------------------------------------------------------------- #
def save_prep(report, sections, scope, period):
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO report_prep (report, sections_json, scope, period, generated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(report) DO UPDATE SET
            sections_json = excluded.sections_json,
            scope = excluded.scope,
            period = excluded.period,
            generated_at = excluded.generated_at;
        """,
        (report, json.dumps(sections, default=str), scope, period, datetime.now()),
    )
    conn.commit()
    conn.close()


def get_prep(report):
    """Return (sections_dict, scope, period, generated_at_str) or (None, ...)."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT sections_json, scope, period, generated_at FROM report_prep "
        "WHERE report = ?",
        (report,),
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None, "", "", ""
    return json.loads(row[0]), row[1] or "", row[2] or "", str(row[3] or "")[:19]
