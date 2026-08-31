"""
db.py — the single SQLite database behind a user profile.

Every table the app has ever used now lives in one `workbench.db` inside the
active profile (see user_profile.py). That is what makes a workbench portable:
one file to copy, back up, or hand over.

Two things this module owns that the old per-module `DATABASE_PATH` constants
could not:

  1. The path is resolved on EVERY connect(), not once at import. Switching
     profiles therefore takes effect immediately, without restarting Streamlit.
  2. Schema creation happens once, explicitly, from app.py — not as a side effect
     of importing email_db / jira_db / one_on_one_db.

It also provides `doc_store`, a key/value JSON table that replaces the eight
loose JSON files the app used to scatter through the repo.
"""
import json
import os
import sqlite3
from datetime import datetime

import user_profile

SCHEMA_VERSION = 1

# Legacy repo-root artifacts, migrated once into the profile database.
# Maps the old filename -> the doc_store key that now holds its contents.
# saydo_tasks.json / checkpoints.json are deliberately absent: the Say/Do Tracker
# and Resume Work modules were removed, so their data is not carried forward.
LEGACY_JSON_FILES = {
    "email_actions.json": "email_actions",
    "ooo_settings.json": "ooo_settings",
    "one_on_one_meetings.json": "one_on_one_meetings",
    "manager_one_on_one.json": "manager_prep_runs",
    "manager_manual_topics.json": "manager_manual_topics",
    "special_projects.json": "special_projects",
}

# Legacy SQLite databases and the tables to lift out of each.
LEGACY_DATABASES = {
    "email_context.db": [
        "thread_context", "email_analysis_cache", "email_projects",
        "thread_summaries", "project_themes", "email_dispositions",
        "conversation_dispositions", "shipments",
    ],
    "jira_state.db": ["spr_analysis"],
    "one_on_one_cache.db": [
        "report_items", "report_prep", "report_projects", "project_actions",
    ],
}


# --------------------------------------------------------------------------- #
# Connections
# --------------------------------------------------------------------------- #
def connect():
    """Open a connection to the ACTIVE profile's database.

    Callers open and close per operation (the pattern every *_db.py function
    already used); routing that through here is what lets the target file change
    at runtime. WAL keeps a long-running read from blocking a write, and the busy
    timeout absorbs the brief contention Streamlit's rerun model produces."""
    path = user_profile.db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
def init_schema():
    """Create every table in the active profile's database. Idempotent — safe on
    every app start. Imports the feature DB modules lazily to avoid an import
    cycle (they import this module for connect())."""
    conn = connect()
    cursor = conn.cursor()
    # Replaces the eight loose JSON files: one row per logical document.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS doc_store (
            key        TEXT PRIMARY KEY,
            value_json TEXT,
            updated_at TIMESTAMP
        )
        """
    )
    # Profile provenance: schema version, display name, migration flags.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS profile_meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    import email_db
    import jira_db
    import one_on_one_db
    import project_db

    email_db.init_db()
    jira_db.init_db()
    one_on_one_db.init_db()
    project_db.init_db()

    if get_meta("schema_version") is None:
        set_meta("schema_version", str(SCHEMA_VERSION))
        set_meta("created_at", user_profile.stamp())


# --------------------------------------------------------------------------- #
# profile_meta
# --------------------------------------------------------------------------- #
def get_meta(key, default=None):
    conn = connect()
    row = conn.execute("SELECT value FROM profile_meta WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default


def set_meta(key, value):
    conn = connect()
    conn.execute(
        "INSERT INTO profile_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
# doc_store — JSON documents keyed by name
# --------------------------------------------------------------------------- #
def get_doc(key, default=None):
    """Return the JSON document stored under `key`, or `default` if absent or
    unparseable. Never raises — a damaged row degrades to the default the same way
    the old file-missing path did."""
    conn = connect()
    row = conn.execute("SELECT value_json FROM doc_store WHERE key = ?", (key,)).fetchone()
    conn.close()
    if not row or row[0] is None:
        return default
    try:
        return json.loads(row[0])
    except (ValueError, TypeError) as e:
        print(f"[db] Corrupt doc_store entry '{key}': {e}")
        return default


def set_doc(key, value):
    """Insert or replace the JSON document under `key`."""
    conn = connect()
    conn.execute(
        """
        INSERT INTO doc_store (key, value_json, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value_json = excluded.value_json,
            updated_at = excluded.updated_at
        """,
        (key, json.dumps(value, default=str), datetime.now()),
    )
    conn.commit()
    conn.close()


def delete_doc(key):
    conn = connect()
    conn.execute("DELETE FROM doc_store WHERE key = ?", (key,))
    conn.commit()
    conn.close()


def list_docs():
    """Return [(key, updated_at)] for every stored document — powers the Profile
    tab's contents summary."""
    conn = connect()
    rows = conn.execute("SELECT key, updated_at FROM doc_store ORDER BY key").fetchall()
    conn.close()
    return [(k, str(u or "")[:19]) for k, u in rows]


def table_counts():
    """Return {table: row_count} for the whole profile database. Used by the
    Profile tab and by migration verification."""
    conn = connect()
    counts = {}
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall():
        counts[name] = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    conn.close()
    return counts


# --------------------------------------------------------------------------- #
# One-time migration from the repo-root layout
# --------------------------------------------------------------------------- #
def migrate_legacy(project_root):
    """Import the old repo-root data files into this profile, once.

    Guarded by profile_meta.legacy_migrated so it never runs twice and can never
    clobber newer in-profile data. Originals are renamed to *.migrated rather than
    deleted — if anything is wrong the source of truth is still on disk.

    Returns a summary dict (or None if it did not run) for logging."""
    if get_meta("legacy_migrated"):
        return None
    if not project_root or not os.path.isdir(project_root):
        return None

    summary = {"docs": {}, "tables": {}, "config": False}
    found_legacy = False

    # --- Loose JSON documents --------------------------------------------- #
    for filename, key in LEGACY_JSON_FILES.items():
        path = os.path.join(project_root, filename)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[db] Skipping unreadable {filename}: {e}")
            continue
        set_doc(key, data)
        summary["docs"][key] = len(data) if isinstance(data, (list, dict)) else 1
        found_legacy = True
        _retire(path)

    # --- app_config.json: team data -> profile, credential paths -> machine - #
    cfg_path = os.path.join(project_root, "app_config.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                legacy_cfg = json.load(f)
        except Exception as e:
            print(f"[db] Skipping unreadable app_config.json: {e}")
            legacy_cfg = None
        if isinstance(legacy_cfg, dict):
            set_doc(
                "team",
                {
                    "team_members": legacy_cfg.get("team_members", []),
                    "extended_members": legacy_cfg.get("extended_members", []),
                    "team_roster": legacy_cfg.get("team_roster", []),
                },
            )
            machine = user_profile.load_machine()
            for k in user_profile.MACHINE_KEYS:
                if legacy_cfg.get(k) and not machine.get(k):
                    machine[k] = legacy_cfg[k]
            user_profile.save_machine(machine)
            summary["config"] = True
            found_legacy = True
            _retire(cfg_path)
            _retire(cfg_path + ".bak")

    # --- SQLite caches ------------------------------------------------------ #
    for filename, tables in LEGACY_DATABASES.items():
        path = os.path.join(project_root, filename)
        if not os.path.exists(path):
            continue
        moved = _attach_and_copy(path, tables)
        summary["tables"].update(moved)
        found_legacy = True
        _retire(path)
        for sidecar in (path + "-wal", path + "-shm"):
            if os.path.exists(sidecar):
                try:
                    os.remove(sidecar)
                except OSError:
                    pass

    # --- Legacy flat Jira analysis cache (pre-dates jira_state.db) --------- #
    _migrate_jira_json_cache(project_root, summary)

    # Identity is never migrated: it is personal data, so it is not carried in
    # source. Every install — upgraded or fresh — sets it in Settings > Identity.

    set_meta("legacy_migrated", user_profile.stamp())
    set_meta("legacy_source", project_root)
    print(f"[db] Legacy migration complete: {summary}")
    return summary


def _attach_and_copy(legacy_path, tables):
    """ATTACH a legacy database and copy its tables in. INSERT OR IGNORE so an
    interrupted or repeated run cannot duplicate rows — every one of these tables
    has a primary key."""
    copied = {}
    conn = connect()
    try:
        conn.execute("ATTACH DATABASE ? AS legacy", (legacy_path,))
    except sqlite3.Error as e:
        print(f"[db] Could not attach {legacy_path}: {e}")
        conn.close()
        return copied
    try:
        present = {
            r[0] for r in conn.execute(
                "SELECT name FROM legacy.sqlite_master WHERE type='table'"
            )
        }
        for table in tables:
            if table not in present:
                continue
            before = conn.execute(f'SELECT COUNT(*) FROM main."{table}"').fetchone()[0]
            # Column-explicit so a legacy file with a narrower schema still copies.
            cols = [r[1] for r in conn.execute(f'PRAGMA legacy.table_info("{table}")')]
            main_cols = {r[1] for r in conn.execute(f'PRAGMA main.table_info("{table}")')}
            shared = [c for c in cols if c in main_cols]
            if not shared:
                continue
            collist = ", ".join(f'"{c}"' for c in shared)
            conn.execute(
                f'INSERT OR IGNORE INTO main."{table}" ({collist}) '
                f'SELECT {collist} FROM legacy."{table}"'
            )
            after = conn.execute(f'SELECT COUNT(*) FROM main."{table}"').fetchone()[0]
            copied[table] = after - before
        conn.commit()
    finally:
        conn.execute("DETACH DATABASE legacy")
        conn.close()
    return copied


def _migrate_jira_json_cache(project_root, summary):
    """Import the pre-SQLite jira_state_cache.json, if present and if the
    spr_analysis table is still empty.

    This supersedes jira_db.migrate_from_json_cache(), which used to run on every
    import. Rows imported here carry no comment/field provenance, so they serve
    the cached analysis while `updated` is unchanged; the first real change
    triggers one full rebuild, after which delta analysis takes over."""
    path = os.path.join(project_root, "jira_state_cache.json")
    if not os.path.exists(path):
        return
    conn = connect()
    already = conn.execute("SELECT COUNT(*) FROM spr_analysis").fetchone()[0]
    conn.close()
    if already:
        _retire(path)
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            cache = json.load(f)
    except Exception as e:
        print(f"[db] Skipping unreadable jira_state_cache.json: {e}")
        return
    if not isinstance(cache, dict) or not cache:
        return

    import jira_db

    n = 0
    for key, entry in cache.items():
        if not isinstance(entry, dict) or "analysis" not in entry:
            continue
        jira_db.upsert_spr(
            key,
            {
                "summary": (entry.get("analysis") or {}).get("digest", "")[:200],
                "status": "",
                "updated": entry.get("updated", ""),
                "analysis_version": entry.get("v"),
                "analysis": entry.get("analysis", {}),
                "description_hash": "",
                "extra_fields_hash": "",
                "seen_comments": [],
                "seen_attachments": [],
            },
        )
        n += 1
    summary["tables"]["spr_analysis (from json)"] = n
    _retire(path)


def _retire(path):
    """Rename a migrated file to *.migrated. Keeping it means a bad migration is
    always recoverable; leaving it in place would let a stale file be re-read."""
    if not os.path.exists(path):
        return
    target = path + ".migrated"
    try:
        if os.path.exists(target):
            os.remove(target)
        os.rename(path, target)
    except OSError as e:
        print(f"[db] Could not retire {path}: {e}")
