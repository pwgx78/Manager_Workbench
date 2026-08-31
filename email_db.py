"""
email_db.py — SQLite cache for the Phase 0 Email Action Identifier.

Ported from the EmailToJira project. Its tables live in the active user
profile's workbench.db alongside every other store (see db.py), so they travel
with the user rather than with the checkout. Schema creation is driven by
db.init_schema() at app start — deliberately NOT on import, so the database path
can change when the user switches profiles.

Tables:

  - thread_context       : rolling per-conversation context summary, so a new
                           email is analyzed with awareness of its thread.
  - email_analysis_cache : per-message LLM analysis, reused for 30 minutes to
                           avoid re-billing the same email on a re-fetch.
"""
import sqlite3
import json
import hashlib
from datetime import datetime, timedelta

import db


def init_db():
    """Create the cache tables if they don't already exist."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS thread_context (
            conversation_id TEXT PRIMARY KEY,
            thread_subject TEXT,
            context_summary TEXT,
            last_updated TIMESTAMP
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS email_analysis_cache (
            message_id TEXT PRIMARY KEY,
            analysis_result_json TEXT,
            last_analyzed TIMESTAMP
        )
        """
    )
    # Per-email project tag (Phase 0 Projects & Themes). One row per message.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS email_projects (
            message_id TEXT PRIMARY KEY,
            conversation_id TEXT,
            project TEXT,
            subject TEXT,
            received TEXT,
            summary TEXT,
            last_updated TIMESTAMP
        )
        """
    )
    # Consolidated per-conversation thread summary (the second analysis pass in
    # the Email Action Identifier). Cached so this expensive pass is incremental
    # and resumable: reused while a thread's message set is unchanged, keyed by a
    # fingerprint of its message ids. One row per conversation.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS thread_summaries (
            conversation_id TEXT PRIMARY KEY,
            msg_fingerprint TEXT,
            summary_json TEXT,
            last_updated TIMESTAMP
        )
        """
    )
    # Synthesized themes per project (cached so revisiting the tab is instant).
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS project_themes (
            project TEXT PRIMARY KEY,
            themes_json TEXT,
            last_synthesized TIMESTAMP
        )
        """
    )
    # Per-action-item disposition (tracked / read_no_action / delegated / ...),
    # so the triage status of every item is recallable across fetches.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS email_dispositions (
            item_id TEXT PRIMARY KEY,
            message_id TEXT,
            action TEXT,
            project TEXT,
            disposition TEXT,
            updated_at TIMESTAMP
        )
        """
    )
    # Conversation-level disposition (the Identify workflow operates per thread).
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_dispositions (
            conversation_id TEXT PRIMARY KEY,
            subject TEXT,
            disposition TEXT,
            updated_at TIMESTAMP
        )
        """
    )
    # Shipping Sample Tracker — one row per shipment, keyed (uniquely) by tracking
    # number so duplicate emails merge instead of inserting new rows.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS shipments (
            tracking_number TEXT PRIMARY KEY,
            carrier TEXT,
            associated_case TEXT,
            associated_spr TEXT,
            sender TEXT,
            date_sent TEXT,
            contents TEXT,
            shipping_status TEXT,
            message_id TEXT,
            status_checked TIMESTAMP,
            last_updated TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


# --- Thread context ------------------------------------------------------- #
def get_thread_context(conversation_id):
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT context_summary FROM thread_context WHERE conversation_id = ?",
        (conversation_id,),
    )
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None


def update_thread_context(conversation_id, subject, new_summary):
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO thread_context (conversation_id, thread_subject, context_summary, last_updated)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(conversation_id) DO UPDATE SET
            context_summary = excluded.context_summary,
            last_updated = excluded.last_updated;
        """,
        (conversation_id, subject, new_summary, datetime.now()),
    )
    conn.commit()
    conn.close()


# --- Per-message analysis cache ------------------------------------------- #
def get_cached_analysis(message_id):
    """Return the cached analysis for `message_id` if one exists, else None.

    Permanent cache — once an email is analyzed it is never re-analyzed unless
    the caller bypasses it (the 'Bypass cache' checkbox in
    pages/0_email_actions.py passes None to skip this lookup)."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT analysis_result_json FROM email_analysis_cache WHERE message_id = ?",
        (message_id,),
    )
    result = cursor.fetchone()
    conn.close()
    return json.loads(result[0]) if result else None


def update_cached_analysis(message_id, analysis_result_json):
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO email_analysis_cache (message_id, analysis_result_json, last_analyzed)
        VALUES (?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            analysis_result_json = excluded.analysis_result_json,
            last_analyzed = excluded.last_analyzed;
        """,
        (message_id, json.dumps(analysis_result_json), datetime.now()),
    )
    conn.commit()
    conn.close()


def cached_message_ids(message_ids):
    """Return the subset of `message_ids` that already have a cached analysis.
    Used to show a resume summary ('N of M already analyzed') before a run."""
    ids = [m for m in message_ids if m]
    if not ids:
        return set()
    conn = db.connect()
    cursor = conn.cursor()
    found = set()
    # Chunk to stay well under SQLite's variable limit for very large fetches.
    for i in range(0, len(ids), 400):
        chunk = ids[i : i + 400]
        placeholders = ",".join("?" for _ in chunk)
        cursor.execute(
            f"SELECT message_id FROM email_analysis_cache WHERE message_id IN ({placeholders})",
            chunk,
        )
        found.update(r[0] for r in cursor.fetchall())
    conn.close()
    return found


# --- Thread summaries (second analysis pass — incremental / resumable) ---- #
def thread_fingerprint(message_ids):
    """Stable hash of a thread's message-id set, so a cached thread summary is
    reused only while the thread's membership is unchanged."""
    joined = ",".join(sorted(str(m) for m in message_ids if m))
    return hashlib.md5(joined.encode("utf-8")).hexdigest()


def get_thread_summary(conversation_id):
    """Return (summary_dict, msg_fingerprint) for a conversation, or (None, None)."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT summary_json, msg_fingerprint FROM thread_summaries WHERE conversation_id = ?",
        (conversation_id,),
    )
    result = cursor.fetchone()
    conn.close()
    if result:
        return json.loads(result[0]), result[1]
    return None, None


def update_thread_summary(conversation_id, msg_fingerprint, summary_dict):
    """Persist (commit immediately) a conversation's consolidated summary so the
    thread-summary pass resumes from the last completed conversation."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO thread_summaries (conversation_id, msg_fingerprint, summary_json, last_updated)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(conversation_id) DO UPDATE SET
            msg_fingerprint = excluded.msg_fingerprint,
            summary_json = excluded.summary_json,
            last_updated = excluded.last_updated;
        """,
        (conversation_id, msg_fingerprint, json.dumps(summary_dict), datetime.now()),
    )
    conn.commit()
    conn.close()


# --- Projects & Themes ---------------------------------------------------- #
def upsert_email_project(message_id, conversation_id, project, subject, received, summary):
    """Record (or update) the project tag for one analyzed email."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO email_projects
            (message_id, conversation_id, project, subject, received, summary, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            conversation_id = excluded.conversation_id,
            project = excluded.project,
            subject = excluded.subject,
            received = excluded.received,
            summary = excluded.summary,
            last_updated = excluded.last_updated;
        """,
        (message_id, conversation_id, project, subject, received, summary, datetime.now()),
    )
    conn.commit()
    conn.close()


def list_known_projects():
    """Return the distinct project names seen so far (excluding 'Unassigned'),
    so the analyzer can reuse exact names instead of fragmenting them."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT DISTINCT project FROM email_projects "
        "WHERE project IS NOT NULL AND project != '' AND project != 'Unassigned' "
        "ORDER BY project"
    )
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_project_counts():
    """Return [(project, email_count), ...] ordered by count desc."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT project, COUNT(*) FROM email_projects "
        "GROUP BY project ORDER BY COUNT(*) DESC, project"
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_emails_for_project(project):
    """Return [(subject, received, summary, message_id), ...] for a project,
    oldest first so synthesis reads the activity in order."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT subject, received, summary, message_id FROM email_projects "
        "WHERE project = ? ORDER BY received",
        (project,),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_project_for_message(message_id):
    """Return the stored project tag for one analyzed email, or None if the
    message was never tagged. Used by the 1:1 Meeting Prep Assistant to reuse the
    Phase-0 project tag when caching an email item for a direct report."""
    if not message_id:
        return None
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT project FROM email_projects WHERE message_id = ?", (message_id,)
    )
    result = cursor.fetchone()
    conn.close()
    if not result:
        return None
    project = result[0]
    return project if project and project != "Unassigned" else None


def get_project_themes(project):
    """Return (themes_dict, last_synthesized_str) for a project, or (None, None)."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT themes_json, last_synthesized FROM project_themes WHERE project = ?",
        (project,),
    )
    result = cursor.fetchone()
    conn.close()
    if result:
        return json.loads(result[0]), result[1]
    return None, None


def upsert_project_themes(project, themes_dict):
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO project_themes (project, themes_json, last_synthesized)
        VALUES (?, ?, ?)
        ON CONFLICT(project) DO UPDATE SET
            themes_json = excluded.themes_json,
            last_synthesized = excluded.last_synthesized;
        """,
        (project, json.dumps(themes_dict), datetime.now()),
    )
    conn.commit()
    conn.close()


def rename_project(old_name, new_name):
    """Merge/rename a project label across both tables. If `new_name` already
    has synthesized themes they win; the old themes row is dropped."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE email_projects SET project = ? WHERE project = ?", (new_name, old_name)
    )
    # Keep project_themes consistent: drop the old row (themes are re-synthesized
    # after a merge); leave any existing new_name themes untouched.
    cursor.execute("DELETE FROM project_themes WHERE project = ?", (old_name,))
    conn.commit()
    conn.close()


# --- Dispositions --------------------------------------------------------- #
def set_disposition(item_id, message_id, action, project, disposition):
    """Upsert the disposition status of a single action item."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO email_dispositions
            (item_id, message_id, action, project, disposition, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
            message_id = excluded.message_id,
            action = excluded.action,
            project = excluded.project,
            disposition = excluded.disposition,
            updated_at = excluded.updated_at;
        """,
        (item_id, message_id, action, project, disposition, datetime.now()),
    )
    conn.commit()
    conn.close()


def clear_disposition(item_id):
    """Remove an item's disposition, returning it to Pending."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM email_dispositions WHERE item_id = ?", (item_id,))
    conn.commit()
    conn.close()


def get_disposition_map(message_ids):
    """Return {item_id: disposition} for the given message ids. Empty dict if
    none are passed."""
    ids = list(message_ids)
    if not ids:
        return {}
    conn = db.connect()
    cursor = conn.cursor()
    placeholders = ",".join("?" for _ in ids)
    cursor.execute(
        f"SELECT item_id, disposition FROM email_dispositions "
        f"WHERE message_id IN ({placeholders})",
        ids,
    )
    rows = cursor.fetchall()
    conn.close()
    return {item_id: disp for item_id, disp in rows}


# --- Conversation-level dispositions -------------------------------------- #
def set_conversation_disposition(conversation_id, subject, disposition, message_ids=()):
    """Disposition an entire conversation, and propagate the tag to each of its
    messages so the status of all individual emails is updated too."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO conversation_dispositions (conversation_id, subject, disposition, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(conversation_id) DO UPDATE SET
            subject = excluded.subject,
            disposition = excluded.disposition,
            updated_at = excluded.updated_at;
        """,
        (conversation_id, subject, disposition, datetime.now()),
    )
    conn.commit()
    conn.close()
    for mid in message_ids:
        set_disposition(mid, mid, "(thread)", "", disposition)


def clear_conversation_disposition(conversation_id, message_ids=()):
    """Return a conversation (and its messages) to un-dispositioned."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM conversation_dispositions WHERE conversation_id = ?",
        (conversation_id,),
    )
    conn.commit()
    conn.close()
    for mid in message_ids:
        clear_disposition(mid)


def get_conversation_dispositions():
    """Return {conversation_id: disposition} for all dispositioned threads."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute("SELECT conversation_id, disposition FROM conversation_dispositions")
    rows = cursor.fetchall()
    conn.close()
    return {cid: disp for cid, disp in rows}


# --- Shipping Sample Tracker --------------------------------------------- #
SHIPMENT_COLUMNS = [
    "tracking_number", "carrier", "associated_case", "associated_spr", "sender",
    "date_sent", "contents", "shipping_status", "message_id",
    "status_checked", "last_updated",
]


def _combine_unique(old, new):
    """Union of two comma-separated lists, order-preserving."""
    seen, out = set(), []
    for tok in [t.strip() for t in (old or "").split(",")] + [
        t.strip() for t in (new or "").split(",")
    ]:
        if tok and tok.lower() not in seen:
            seen.add(tok.lower())
            out.append(tok)
    return ", ".join(out)


def get_shipment(tracking_number):
    conn = db.connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM shipments WHERE tracking_number = ?", (tracking_number,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def list_shipments():
    conn = db.connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM shipments ORDER BY last_updated DESC")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def upsert_shipment(data, message_id=""):
    """Insert a shipment, or MERGE into the existing row with the same tracking
    number: fill empty scalar fields, combine SPRs, append new contents. No-op if
    `tracking_number` is empty."""
    tn = str(data.get("tracking_number", "")).strip()
    if not tn:
        return
    existing = get_shipment(tn)
    now = datetime.now()
    conn = db.connect()
    cursor = conn.cursor()
    if existing is None:
        cursor.execute(
            """
            INSERT INTO shipments
                (tracking_number, carrier, associated_case, associated_spr, sender,
                 date_sent, contents, shipping_status, message_id, status_checked, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tn,
                str(data.get("carrier", "")).strip(),
                str(data.get("associated_case", "")).strip(),
                str(data.get("associated_spr", "")).strip(),
                str(data.get("sender", "")).strip(),
                str(data.get("date_sent", "")).strip(),
                str(data.get("contents", "")).strip(),
                "",  # shipping_status — filled by the carrier refresh
                message_id,
                None,
                now,
            ),
        )
    else:
        def fill(field):  # keep existing if present, else take new
            return existing.get(field) or str(data.get(field, "")).strip()

        merged_spr = _combine_unique(existing.get("associated_spr"), data.get("associated_spr", ""))
        new_contents = str(data.get("contents", "")).strip()
        old_contents = existing.get("contents") or ""
        if new_contents and new_contents.lower() not in old_contents.lower():
            contents = f"{old_contents}; {new_contents}".strip("; ")
        else:
            contents = old_contents
        cursor.execute(
            """
            UPDATE shipments SET
                carrier = ?, associated_case = ?, associated_spr = ?, sender = ?,
                date_sent = ?, contents = ?, message_id = ?, last_updated = ?
            WHERE tracking_number = ?
            """,
            (
                fill("carrier"), fill("associated_case"), merged_spr, fill("sender"),
                fill("date_sent"), contents, message_id or existing.get("message_id"),
                now, tn,
            ),
        )
    conn.commit()
    conn.close()


def update_shipment_status(tracking_number, status):
    conn = db.connect()
    cursor = conn.cursor()
    now = datetime.now()
    cursor.execute(
        "UPDATE shipments SET shipping_status = ?, status_checked = ?, last_updated = ? "
        "WHERE tracking_number = ?",
        (status, now, now, tracking_number),
    )
    conn.commit()
    conn.close()


def save_shipment_row(row):
    """Persist manual edits for one shipment (keyed by tracking_number)."""
    tn = str(row.get("tracking_number", "")).strip()
    if not tn:
        return
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE shipments SET
            carrier = ?, associated_case = ?, associated_spr = ?, sender = ?,
            date_sent = ?, contents = ?, shipping_status = ?, last_updated = ?
        WHERE tracking_number = ?
        """,
        (
            str(row.get("carrier", "")).strip(),
            str(row.get("associated_case", "")).strip(),
            str(row.get("associated_spr", "")).strip(),
            str(row.get("sender", "")).strip(),
            str(row.get("date_sent", "")).strip(),
            str(row.get("contents", "")).strip(),
            str(row.get("shipping_status", "")).strip(),
            datetime.now(),
            tn,
        ),
    )
    conn.commit()
    conn.close()


def shipments_needing_refresh(max_age_hours=24):
    """Return [(tracking_number, carrier), ...] whose status is unchecked or stale."""
    cutoff = datetime.now() - timedelta(hours=max_age_hours)
    out = []
    for s in list_shipments():
        checked = s.get("status_checked")
        if not checked:
            out.append((s["tracking_number"], s.get("carrier", "")))
            continue
        try:
            if datetime.fromisoformat(str(checked)) < cutoff:
                out.append((s["tracking_number"], s.get("carrier", "")))
        except ValueError:
            out.append((s["tracking_number"], s.get("carrier", "")))
    return out
