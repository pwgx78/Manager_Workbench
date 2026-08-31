"""
jira_db.py — SQLite store for the delta-aware Jira State Tracker.

One row per SPR/ticket in `spr_analysis`, holding the latest LLM analysis (the
summary of record) plus the provenance needed to decide what changed since the
last run: hashes of the description and custom fields, and the fingerprints of
every comment / attachment already folded into the analysis. This lets
jira_analysis.get_or_analyze_ticket() re-analyze only the delta (new comments,
changed fields, new attachments) instead of rebuilding the whole analysis.

No Streamlit imports, so it stays reusable and unit-testable — same shape as
email_db.py. The table lives in the active user profile's workbench.db and is
created by db.init_schema() at app start, not on import, so the profile can be
switched at runtime. The one-time import of the legacy jira_state_cache.json now
lives in db.migrate_legacy().
"""
import sqlite3
import json
import hashlib
from datetime import datetime

import db


def init_db():
    """Create the spr_analysis table if it doesn't already exist."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS spr_analysis (
            ticket_key         TEXT PRIMARY KEY,
            summary            TEXT,
            status             TEXT,
            updated            TEXT,
            analysis_version   INTEGER,
            analysis_json      TEXT,
            description_hash   TEXT,
            extra_fields_hash  TEXT,
            seen_comments_json TEXT,
            seen_attach_json   TEXT,
            last_analyzed      TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


# --- Fingerprint helpers --------------------------------------------------- #
def _sha1(text):
    return hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()


def hash_text(text):
    """Stable hash of a free-text field (description, custom-field value)."""
    return _sha1(text or "")


def hash_extra_fields(extra_fields):
    """Order-independent hash of the list of {name, value} custom fields."""
    pairs = sorted(
        (str(f.get("name", "")), str(f.get("value", "")))
        for f in (extra_fields or [])
    )
    return _sha1(json.dumps(pairs, ensure_ascii=False))


def fingerprint_comment(c):
    """Stable identity for a comment: its Jira id when present, else a hash of
    its created/author/body so deltas still work if the id is missing."""
    cid = (c or {}).get("id")
    if cid:
        return str(cid)
    return _sha1(
        f"{c.get('created', '')}|{c.get('author', '')}|{(c.get('body') or '')[:80]}"
    )


def comment_fingerprints(comments):
    return [fingerprint_comment(c) for c in (comments or [])]


def attachment_ids(attachments):
    return [str(a.get("id")) for a in (attachments or []) if a.get("id") is not None]


# --- Row access ------------------------------------------------------------ #
def get_spr(ticket_key):
    """Return the stored row for `ticket_key` as a dict (json columns decoded),
    or None if the ticket has never been analyzed."""
    conn = db.connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM spr_analysis WHERE ticket_key = ?", (ticket_key,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "ticket_key": row["ticket_key"],
        "summary": row["summary"],
        "status": row["status"],
        "updated": row["updated"],
        "analysis_version": row["analysis_version"],
        "analysis": _loads(row["analysis_json"], {}),
        "description_hash": row["description_hash"],
        "extra_fields_hash": row["extra_fields_hash"],
        "seen_comments": _loads(row["seen_comments_json"], []),
        "seen_attachments": _loads(row["seen_attach_json"], []),
        "last_analyzed": row["last_analyzed"],
    }


def upsert_spr(ticket_key, record):
    """Insert or replace the analysis + provenance for `ticket_key`.

    `record` keys: summary, status, updated, analysis_version, analysis (dict),
    description_hash, extra_fields_hash, seen_comments (list), seen_attachments
    (list)."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO spr_analysis (
            ticket_key, summary, status, updated, analysis_version, analysis_json,
            description_hash, extra_fields_hash, seen_comments_json, seen_attach_json,
            last_analyzed
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticket_key) DO UPDATE SET
            summary = excluded.summary,
            status = excluded.status,
            updated = excluded.updated,
            analysis_version = excluded.analysis_version,
            analysis_json = excluded.analysis_json,
            description_hash = excluded.description_hash,
            extra_fields_hash = excluded.extra_fields_hash,
            seen_comments_json = excluded.seen_comments_json,
            seen_attach_json = excluded.seen_attach_json,
            last_analyzed = excluded.last_analyzed;
        """,
        (
            ticket_key,
            record.get("summary", ""),
            record.get("status", ""),
            record.get("updated", ""),
            record.get("analysis_version"),
            json.dumps(record.get("analysis", {}), default=str),
            record.get("description_hash", ""),
            record.get("extra_fields_hash", ""),
            json.dumps(record.get("seen_comments", [])),
            json.dumps(record.get("seen_attachments", [])),
            datetime.now(),
        ),
    )
    conn.commit()
    conn.close()


def _loads(text, default):
    try:
        return json.loads(text) if text else default
    except (ValueError, TypeError):
        return default
