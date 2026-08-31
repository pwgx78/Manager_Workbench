"""
project_db.py — the PROJECT register: the app's shared spine.

A PROJECT here sits at the altitude of a *Jira project* ("SPR" = customer defect
resolution), NOT a Jira issue ("SPR-12345"). It is a container that Jira issues,
emails and tasks point *at*, and its real purpose is holding the non-Jira work
that has nowhere else to live.

A Jira issue must never become a project. That is enforced structurally, not by
convention: link() refuses to mint a project for entity_type='jira' (see the
`create_if_missing` guard below). The old `report_projects` table registers every
SPR ticket as a "project", which is exactly the anti-pattern this register exists
to avoid; that table is left alone and this one is protected.

Its tables live in the active profile's workbench.db alongside every other store
(see db.py), so they travel with the user rather than with the checkout. Schema
creation is driven by db.init_schema() at app start — deliberately NOT on import,
so the database path can change when the user switches profiles.

Tables:

  - projects       : the register itself. 'FAB-007' style ids, minted from a
                     user-chosen prefix plus a counter that never reuses a
                     number, so deleting FAB-007 does not hand its id to the
                     next project.
  - project_aliases: alternate names, so manual entry can be sloppy ('tc101' ->
                     'TC101') and so a merge or rename is a one-row update.
  - project_links  : one uniform link table for every entity type. Approval is a
                     state transition on the link row, not a separate table,
                     which is what makes a *rejected* proposal persist and stop
                     the same wrong project being proposed again.
"""
import re
import sqlite3
from datetime import date, datetime

import db

# profile_meta keys. The prefix is baked into every id ever minted, so it is
# editable only while the register is empty (see set_prefix).
PREFIX_KEY = "project_id_prefix"
COUNTER_KEY = "project_id_counter"
ABSORBED_KEY = "special_projects_absorbed"

DEFAULT_PREFIX = "PRJ"
ID_PAD = 3  # FAB-007. Past 999 the number simply grows a digit.

# Entity types a project can link to. 'email' is a single message, 'conversation'
# a whole thread; both exist because Page 0 works at both altitudes.
ENTITY_TYPES = ("email", "conversation", "jira", "action", "shipment")

# Flat set, max 3 per email — no primary/secondary. The cap counts CONFIRMED
# links only, so the model can always offer three candidates regardless of what
# is already confirmed.
MAX_CONFIRMED_PER_ENTITY = 3

STATES = ("proposed", "confirmed", "rejected")


class ProjectError(Exception):
    """Raised when a caller asks for something the register must refuse: a Jira
    key minting a project, a duplicate name, a locked prefix, a fourth confirmed
    project on one email."""


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
def init_db():
    """Create the project tables if they don't already exist."""
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            project_id          TEXT PRIMARY KEY,
            name                TEXT NOT NULL,
            description         TEXT,
            description_source  TEXT,
            description_updated TIMESTAMP,
            keywords            TEXT,
            status              TEXT NOT NULL DEFAULT 'active',
            start_date          DATE,
            close_date          DATE,
            owner               TEXT,
            jira_project_key    TEXT,
            created_at          TIMESTAMP,
            updated_at          TIMESTAMP
        )
        """
    )
    # Case/whitespace-insensitive uniqueness on the name, so "AI Gov" and
    # "ai  gov" cannot both exist. Enforced by the DB rather than by a
    # pre-check, which would race between two tabs.
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_name_norm "
        "ON projects (LOWER(TRIM(name)))"
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_projects_status ON projects (status)")
    # alias is stored NORMALIZED (see _normalize) and is the primary key, which
    # is what makes lookup a single indexed read and keeps aliases globally
    # unique across projects.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS project_aliases (
            alias      TEXT PRIMARY KEY,
            display    TEXT,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_aliases_project ON project_aliases (project_id)"
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS project_links (
            project_id  TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
            entity_type TEXT NOT NULL,
            entity_id   TEXT NOT NULL,
            state       TEXT NOT NULL DEFAULT 'proposed',
            confidence  REAL,
            rationale   TEXT,
            assigned_by TEXT,
            created_at  TIMESTAMP,
            updated_at  TIMESTAMP,
            UNIQUE(project_id, entity_type, entity_id)
        )
        """
    )
    # The two shapes every read uses: "what links to this project" and "what
    # projects does this entity have".
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_links_project ON project_links (project_id, state)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_links_entity "
        "ON project_links (entity_type, entity_id, state)"
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
# Normalization — one definition, used by both name and alias lookup
# --------------------------------------------------------------------------- #
def _normalize(text):
    """Fold a project name or alias to its lookup form: casefolded with every
    non-alphanumeric character removed.

    Separators are dropped rather than collapsed to a space, because that is
    what makes 'TC-101', 'TC 101' and 'tc101' one project instead of three —
    the exact sloppiness manual entry produces. The trade is that 'AI Gov' and
    'AIGov' also collide, which is wanted: it surfaces as a refusal at creation
    rather than as two half-populated registers.
    """
    return re.sub(r"[^0-9a-z]+", "", str(text or "").casefold())


def _spaced(text):
    """Like _normalize but keeping word boundaries, so a short term can be
    matched with \\b anchors instead of as a bare substring."""
    return " ".join(re.sub(r"[^0-9a-z]+", " ", str(text or "").casefold()).split())


# A collapsed term shorter than this is matched on word boundaries rather than
# as a substring. 'AI' inside 'chain' is the failure being avoided.
MIN_SUBSTRING_TERM = 4

# Relative weight of the thing that matched. A name hit is worth more than a
# keyword hit, so a project named in the subject outranks one that merely shares
# a keyword with the body.
WEIGHT_NAME = 3.0
WEIGHT_ALIAS = 2.0
WEIGHT_KEYWORD = 1.0


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _today():
    return date.today().isoformat()


def _row_to_dict(row, columns):
    return dict(zip(columns, row))


PROJECT_COLUMNS = (
    "project_id", "name", "description", "description_source",
    "description_updated", "keywords", "status", "start_date", "close_date",
    "owner", "jira_project_key", "created_at", "updated_at",
)


# --------------------------------------------------------------------------- #
# ID prefix and minting
# --------------------------------------------------------------------------- #
def get_prefix():
    """The configured project-id prefix, e.g. 'FAB'."""
    return db.get_meta(PREFIX_KEY, DEFAULT_PREFIX)


def prefix_is_locked():
    """True once any project exists. The prefix is part of every id already
    minted, so changing it afterwards would orphan them."""
    return count_projects(include_closed=True) > 0


def set_prefix(prefix):
    """Set the id prefix. Refused once the register is non-empty."""
    cleaned = re.sub(r"[^A-Za-z0-9]", "", str(prefix or "")).upper()
    if not cleaned:
        raise ProjectError("Prefix must contain at least one letter or digit.")
    if len(cleaned) > 8:
        raise ProjectError("Prefix must be 8 characters or fewer.")
    if prefix_is_locked() and cleaned != get_prefix():
        raise ProjectError(
            "The prefix is baked into every project id already minted, so it "
            "cannot be changed once projects exist."
        )
    db.set_meta(PREFIX_KEY, cleaned)
    return cleaned


def mint_project_id():
    """Return the next unused project id, e.g. 'FAB-007'.

    The counter lives in profile_meta and only ever moves forward, so deleting
    FAB-007 never hands its id to the next project. The uniqueness loop covers
    the one case a counter alone cannot: a database restored from an export
    whose counter trails its rows."""
    prefix = get_prefix()
    counter = int(db.get_meta(COUNTER_KEY, "0") or 0)
    conn = db.connect()
    try:
        while True:
            counter += 1
            candidate = f"{prefix}-{counter:0{ID_PAD}d}"
            taken = conn.execute(
                "SELECT 1 FROM projects WHERE project_id = ?", (candidate,)
            ).fetchone()
            if not taken:
                break
    finally:
        conn.close()
    db.set_meta(COUNTER_KEY, counter)
    return candidate


# --------------------------------------------------------------------------- #
# Projects — CRUD
# --------------------------------------------------------------------------- #
def create_project(
    name,
    keywords="",
    description="",
    owner="",
    jira_project_key="",
    start_date=None,
    aliases=(),
):
    """Create a project and return its id.

    Creation is always a human act — the LLM only ever ranks projects that
    already exist. That is the single rule that stops the register rotting back
    into a pile of free-text labels."""
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ProjectError("A project needs a name.")

    existing = resolve_name(clean_name)
    if existing:
        raise ProjectError(
            f"{clean_name} already exists as {existing} — link to that instead, "
            f"or pick another name."
        )

    described = str(description or "").strip()
    project_id = mint_project_id()
    stamp = _now()
    conn = db.connect()
    try:
        conn.execute(
            """
            INSERT INTO projects (
                project_id, name, description, description_source,
                description_updated, keywords, status, start_date, close_date,
                owner, jira_project_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, NULL, ?, ?, ?, ?)
            """,
            (
                project_id,
                clean_name,
                described,
                "user" if described else None,
                stamp if described else None,
                str(keywords or "").strip(),
                start_date or _today(),
                str(owner or "").strip(),
                str(jira_project_key or "").strip().upper(),
                stamp,
                stamp,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.close()
        raise ProjectError(f"Could not create {clean_name}: {exc}") from exc
    conn.close()

    for alias in aliases:
        add_alias(project_id, alias)
    return project_id


def get_project(project_id):
    """One project as a dict, or None."""
    conn = db.connect()
    row = conn.execute(
        f"SELECT {', '.join(PROJECT_COLUMNS)} FROM projects WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    conn.close()
    return _row_to_dict(row, PROJECT_COLUMNS) if row else None


def list_projects(include_closed=False, search=""):
    """Projects as dicts, active first then newest.

    `include_closed=False` is the default everywhere on purpose: a closed
    project must never be offered as a candidate for new email."""
    where, params = [], []
    if not include_closed:
        where.append("status = 'active'")
    if str(search or "").strip():
        where.append("(LOWER(name) LIKE ? OR LOWER(IFNULL(keywords, '')) LIKE ?)")
        needle = f"%{search.strip().casefold()}%"
        params += [needle, needle]
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    conn = db.connect()
    rows = conn.execute(
        f"SELECT {', '.join(PROJECT_COLUMNS)} FROM projects {clause} "
        f"ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, created_at DESC",
        params,
    ).fetchall()
    conn.close()
    return [_row_to_dict(r, PROJECT_COLUMNS) for r in rows]


def count_projects(include_closed=False):
    clause = "" if include_closed else "WHERE status = 'active'"
    conn = db.connect()
    n = conn.execute(f"SELECT COUNT(*) FROM projects {clause}").fetchone()[0]
    conn.close()
    return int(n)


# Fields a caller may update. status and close_date are deliberately absent:
# they move only through close_project / reopen_project, so the two can never
# drift out of step.
UPDATABLE = (
    "name", "description", "keywords", "owner", "jira_project_key", "start_date",
)


def update_project(project_id, **fields):
    """Update whitelisted fields. Setting `description` marks it user-authored,
    which locks out LLM regeneration — the user always wins."""
    unknown = set(fields) - set(UPDATABLE)
    if unknown:
        raise ProjectError(f"Not updatable: {', '.join(sorted(unknown))}")
    if not fields:
        return

    if "name" in fields:
        clean = str(fields["name"] or "").strip()
        if not clean:
            raise ProjectError("A project needs a name.")
        clash = resolve_name(clean)
        if clash and clash != project_id:
            raise ProjectError(f"{clean} is already {clash}.")
        fields["name"] = clean

    sets = [f"{key} = ?" for key in fields]
    params = [fields[key] for key in fields]
    if "description" in fields:
        sets += ["description_source = 'user'", "description_updated = ?"]
        params.append(_now())
    sets.append("updated_at = ?")
    params.append(_now())
    params.append(project_id)

    conn = db.connect()
    try:
        conn.execute(
            f"UPDATE projects SET {', '.join(sets)} WHERE project_id = ?", params
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.close()
        raise ProjectError(str(exc)) from exc
    conn.close()


def set_llm_description(project_id, description):
    """Store a generated description, but never over a user-written one.
    Returns False when it declined."""
    current = get_project(project_id)
    if current and current.get("description_source") == "user":
        return False
    stamp = _now()
    conn = db.connect()
    conn.execute(
        "UPDATE projects SET description = ?, description_source = 'llm', "
        "description_updated = ?, updated_at = ? WHERE project_id = ?",
        (str(description or "").strip(), stamp, stamp, project_id),
    )
    conn.commit()
    conn.close()
    return True


def close_project(project_id, close_date=None):
    """Close a project. Dropping out of the candidate list is the point: it is
    what stops the LLM filing new email into dead work. There is no target
    date — close_date exists to inactivate, not to plan."""
    conn = db.connect()
    conn.execute(
        "UPDATE projects SET status = 'closed', close_date = ?, updated_at = ? "
        "WHERE project_id = ?",
        (close_date or _today(), _now(), project_id),
    )
    conn.commit()
    conn.close()


def reopen_project(project_id):
    """One click, no friction — this is a personal tool."""
    conn = db.connect()
    conn.execute(
        "UPDATE projects SET status = 'active', close_date = NULL, updated_at = ? "
        "WHERE project_id = ?",
        (_now(), project_id),
    )
    conn.commit()
    conn.close()


def delete_project(project_id):
    """Hard delete, cascading to aliases and links. The id is never recycled."""
    conn = db.connect()
    conn.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
# Aliases
#
# Their original job was migrating a pile of free-text labels. With that
# migration dropped, they earn their keep two other ways: normalizing sloppy
# manual entry, and making a merge or rename a one-row update.
# --------------------------------------------------------------------------- #
def add_alias(project_id, alias):
    """Point an alternate name at a project. Silently ignores an alias that
    already points at THIS project; refuses one owned by another."""
    normalized = _normalize(alias)
    if not normalized:
        return False
    owner = resolve_name(alias)
    if owner == project_id:
        return True
    if owner:
        raise ProjectError(f"{alias} already resolves to {owner}.")
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO project_aliases (alias, display, project_id) VALUES (?, ?, ?)",
            (normalized, str(alias).strip(), project_id),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.close()
        raise ProjectError(str(exc)) from exc
    conn.close()
    return True


def remove_alias(alias):
    conn = db.connect()
    conn.execute("DELETE FROM project_aliases WHERE alias = ?", (_normalize(alias),))
    conn.commit()
    conn.close()


def list_aliases(project_id):
    """Display forms of a project's aliases."""
    conn = db.connect()
    rows = conn.execute(
        "SELECT IFNULL(display, alias) FROM project_aliases WHERE project_id = ? "
        "ORDER BY alias",
        (project_id,),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def resolve_name(text):
    """Resolve a typed name to a project_id, or None.

    Checks the project id itself, then names, then aliases — all normalized, so
    'tc101', 'TC-101' and 'TC 101' land on the same project. This is what backs
    manual selection: found means link it, not-found means offer to create."""
    raw = str(text or "").strip()
    if not raw:
        return None
    normalized = _normalize(raw)
    if not normalized:
        return None
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT project_id FROM projects WHERE UPPER(project_id) = ?",
            (raw.upper(),),
        ).fetchone()
        if row:
            return row[0]
        # Normalizing in Python rather than SQL: the fold is punctuation-aware,
        # which LOWER(TRIM()) is not.
        for pid, name in conn.execute("SELECT project_id, name FROM projects"):
            if _normalize(name) == normalized:
                return pid
        row = conn.execute(
            "SELECT project_id FROM project_aliases WHERE alias = ?", (normalized,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def merge_projects(source_id, target_id):
    """Fold `source_id` into `target_id`: move its links, keep its name as an
    alias of the target, then delete it. Per 4.2 this is why aliases exist."""
    if source_id == target_id:
        raise ProjectError("Cannot merge a project into itself.")
    source, target = get_project(source_id), get_project(target_id)
    if not source or not target:
        raise ProjectError("Both projects must exist to merge.")

    conn = db.connect()
    # INSERT OR IGNORE, not UPDATE: the target may already link the same entity,
    # and UNIQUE(project_id, entity_type, entity_id) would reject the collision.
    conn.execute(
        """
        INSERT OR IGNORE INTO project_links (
            project_id, entity_type, entity_id, state, confidence, rationale,
            assigned_by, created_at, updated_at
        )
        SELECT ?, entity_type, entity_id, state, confidence, rationale,
               assigned_by, created_at, updated_at
        FROM project_links WHERE project_id = ?
        """,
        (target_id, source_id),
    )
    conn.execute("DELETE FROM project_links WHERE project_id = ?", (source_id,))
    conn.execute(
        "UPDATE project_aliases SET project_id = ? WHERE project_id = ?",
        (target_id, source_id),
    )
    conn.commit()
    conn.close()

    # Delete BEFORE aliasing, not after: while the source still exists its own
    # name resolves to itself, so add_alias would see the name as taken and
    # refuse. The alias rows were re-pointed at the target above, so the
    # cascade on delete does not take them with it.
    delete_project(source_id)

    # The source's name becomes an alias, so anything that referred to it by
    # name still resolves after the merge. A genuine clash with another
    # project's alias is possible and non-fatal — the merge itself is done.
    try:
        add_alias(target_id, source["name"])
    except ProjectError:
        pass


# --------------------------------------------------------------------------- #
# Links — one uniform table for every entity type
# --------------------------------------------------------------------------- #
def link(
    project_id,
    entity_type,
    entity_id,
    state="proposed",
    confidence=None,
    rationale="",
    assigned_by="user",
    create_if_missing=False,
):
    """Link an entity to a project.

    THE JIRA GUARD (plan 4.3): `create_if_missing` is forced False for
    entity_type='jira', so a Jira key can never mint a projects row. This is
    structural rather than conventional because the old report_projects table
    proves convention does not hold — it registered every SPR ticket as a
    "project".

    `create_if_missing` is a hook for later phases; P1 has no caller that sets
    it, and for 'jira' it can never take effect.
    """
    if entity_type not in ENTITY_TYPES:
        raise ProjectError(f"Unknown entity_type {entity_type!r}.")
    if state not in STATES:
        raise ProjectError(f"Unknown state {state!r}.")
    if entity_type == "jira":
        create_if_missing = False

    if not get_project(project_id):
        if not create_if_missing:
            raise ProjectError(
                f"{project_id} does not exist, and a {entity_type} link may not "
                f"create one."
            )
        raise ProjectError(
            "create_if_missing has no implementation yet — projects are created "
            "by a human, in Project Management."
        )

    if state == "confirmed":
        already = confirmed_project_ids(entity_type, entity_id)
        if project_id not in already and len(already) >= MAX_CONFIRMED_PER_ENTITY:
            raise ProjectError(
                f"That {entity_type} already has {MAX_CONFIRMED_PER_ENTITY} "
                f"confirmed projects, which is the maximum."
            )

    stamp = _now()
    conn = db.connect()
    conn.execute(
        """
        INSERT INTO project_links (
            project_id, entity_type, entity_id, state, confidence, rationale,
            assigned_by, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project_id, entity_type, entity_id) DO UPDATE SET
            state       = excluded.state,
            confidence  = excluded.confidence,
            rationale   = excluded.rationale,
            assigned_by = excluded.assigned_by,
            updated_at  = excluded.updated_at
        """,
        (
            project_id, entity_type, str(entity_id), state,
            confidence, str(rationale or ""), assigned_by, stamp, stamp,
        ),
    )
    conn.commit()
    conn.close()
    return True


def unlink(project_id, entity_type, entity_id):
    conn = db.connect()
    conn.execute(
        "DELETE FROM project_links WHERE project_id = ? AND entity_type = ? "
        "AND entity_id = ?",
        (project_id, entity_type, str(entity_id)),
    )
    conn.commit()
    conn.close()


def set_link_state(project_id, entity_type, entity_id, state):
    """Approve or reject a proposal. Rejection is stored, not deleted — that is
    exactly how the same wrong project stops being proposed again (plan 4.4)."""
    if state not in STATES:
        raise ProjectError(f"Unknown state {state!r}.")
    if state == "confirmed":
        return link(
            project_id, entity_type, entity_id, state="confirmed", assigned_by="user"
        )
    conn = db.connect()
    conn.execute(
        "UPDATE project_links SET state = ?, assigned_by = 'user', updated_at = ? "
        "WHERE project_id = ? AND entity_type = ? AND entity_id = ?",
        (state, _now(), project_id, entity_type, str(entity_id)),
    )
    conn.commit()
    conn.close()
    return True


def confirmed_project_ids(entity_type, entity_id):
    """Project ids confirmed for one entity."""
    conn = db.connect()
    rows = conn.execute(
        "SELECT project_id FROM project_links WHERE entity_type = ? "
        "AND entity_id = ? AND state = 'confirmed' ORDER BY project_id",
        (entity_type, str(entity_id)),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def links_for_project(project_id, entity_type=None, state=None):
    """Link rows for a project, newest first."""
    where, params = ["project_id = ?"], [project_id]
    if entity_type:
        where.append("entity_type = ?")
        params.append(entity_type)
    if state:
        where.append("state = ?")
        params.append(state)
    columns = (
        "project_id", "entity_type", "entity_id", "state", "confidence",
        "rationale", "assigned_by", "created_at", "updated_at",
    )
    conn = db.connect()
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM project_links "
        f"WHERE {' AND '.join(where)} ORDER BY created_at DESC",
        params,
    ).fetchall()
    conn.close()
    return [_row_to_dict(r, columns) for r in rows]


def link_counts(state="confirmed"):
    """{project_id: {entity_type: n}} in one query, so the register table does
    not fan out into a query per row."""
    conn = db.connect()
    rows = conn.execute(
        "SELECT project_id, entity_type, COUNT(*) FROM project_links "
        "WHERE state = ? GROUP BY project_id, entity_type",
        (state,),
    ).fetchall()
    conn.close()
    counts = {}
    for project_id, entity_type, total in rows:
        counts.setdefault(project_id, {})[entity_type] = int(total)
    return counts


def pending_proposals():
    """Every link still awaiting a human decision — the P3 approval queue. Read
    here in P1 so the module can show an honest zero."""
    conn = db.connect()
    rows = conn.execute(
        """
        SELECT l.project_id, p.name, l.entity_type, l.entity_id, l.confidence,
               l.rationale, l.created_at
        FROM project_links l JOIN projects p ON p.project_id = l.project_id
        WHERE l.state = 'proposed'
        ORDER BY l.confidence DESC, l.created_at DESC
        """
    ).fetchall()
    conn.close()
    columns = (
        "project_id", "name", "entity_type", "entity_id", "confidence",
        "rationale", "created_at",
    )
    return [_row_to_dict(r, columns) for r in rows]


# --------------------------------------------------------------------------- #
# Absorbing the legacy special_projects list
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# The keyword pre-filter — pure string matching, NO model
#
# This is what keeps the project feature at zero extra LLM calls. Ranking rides
# in the email-analysis prompt that already runs (P3), and this narrows the
# active register to a shortlist first, so prompt cost stays FLAT as the register
# grows. Without it, cost would scale linearly with project count: 40 projects x
# 60 emails means paying for the whole roster sixty times.
# --------------------------------------------------------------------------- #
def _match_terms(project, aliases):
    """(term, weight) pairs to match one project against text."""
    terms = [(project["name"], WEIGHT_NAME)]
    terms += [(alias, WEIGHT_ALIAS) for alias in aliases]
    terms += [
        (kw.strip(), WEIGHT_KEYWORD)
        for kw in str(project.get("keywords") or "").split(",")
        if kw.strip()
    ]
    return terms


def _term_hits(term, collapsed_text, spaced_text):
    """True when `term` appears in the text. Long terms match as a substring of
    the collapsed form, so 'TC-101' finds 'tc101'; short ones need word
    boundaries, so 'AI' does not fire on 'chain'."""
    collapsed_term = _normalize(term)
    if not collapsed_term:
        return False
    if len(collapsed_term) >= MIN_SUBSTRING_TERM:
        return collapsed_term in collapsed_text
    return re.search(rf"\b{re.escape(_spaced(term))}\b", spaced_text) is not None


def shortlist_for_text(text, limit=8, include_scores=False):
    """Rank ACTIVE projects against arbitrary text (an email subject + body).

    Pure SQL and string matching — no model is called, so this is free and can
    run on every email. Closed projects are excluded, which is the whole reason
    close_date exists: dead work stops being offered.

    Returns the top `limit` projects, best first. With include_scores=True each
    dict carries `_score` and `_matched` (the terms that fired), which is what
    the UI shows so a shortlist is explainable rather than magic.
    """
    haystack = str(text or "")
    if not haystack.strip():
        return []
    collapsed_text = _normalize(haystack)
    spaced_text = _spaced(haystack)

    conn = db.connect()
    rows = conn.execute(
        f"SELECT {', '.join(PROJECT_COLUMNS)} FROM projects WHERE status = 'active'"
    ).fetchall()
    alias_rows = conn.execute(
        "SELECT project_id, IFNULL(display, alias) FROM project_aliases"
    ).fetchall()
    conn.close()

    aliases_by_project = {}
    for project_id, alias in alias_rows:
        aliases_by_project.setdefault(project_id, []).append(alias)

    scored = []
    for row in rows:
        project = _row_to_dict(row, PROJECT_COLUMNS)
        score, matched = 0.0, []
        for term, weight in _match_terms(
            project, aliases_by_project.get(project["project_id"], [])
        ):
            if _term_hits(term, collapsed_text, spaced_text):
                score += weight
                matched.append(term)
        if score > 0:
            if include_scores:
                project["_score"] = score
                project["_matched"] = matched
            scored.append((score, project["name"], project))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [project for _score, _name, project in scored[:limit]]


def candidate_block(projects):
    """Render a shortlist as prompt text. Lives here rather than in llm_prompts
    so the id/name/keyword shape is defined next to the schema that produces it.
    Consumed by the email-analysis prompt in P3."""
    lines = []
    for project in projects:
        keywords = str(project.get("keywords") or "").strip()
        line = f"- {project['project_id']}: {project['name']}"
        if keywords:
            line += f" (keywords: {keywords})"
        lines.append(line)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Entity-side reads — "what projects does this email belong to"
# --------------------------------------------------------------------------- #
def projects_for_entity(entity_type, entity_id, states=("confirmed",)):
    """Full project rows linked to one entity, with the link state attached.

    Returns a LIST, not a single value: an email may sit in up to three
    projects, which is exactly what email_projects.message_id being a PRIMARY
    KEY cannot express."""
    if not entity_id:
        return []
    placeholders = ", ".join("?" for _ in states)
    conn = db.connect()
    rows = conn.execute(
        f"""
        SELECT {', '.join('p.' + c for c in PROJECT_COLUMNS)}, l.state, l.confidence
        FROM project_links l JOIN projects p ON p.project_id = l.project_id
        WHERE l.entity_type = ? AND l.entity_id = ? AND l.state IN ({placeholders})
        ORDER BY p.name
        """,
        [entity_type, str(entity_id), *states],
    ).fetchall()
    conn.close()
    columns = (*PROJECT_COLUMNS, "link_state", "link_confidence")
    return [_row_to_dict(r, columns) for r in rows]


def entity_ids_for_project(project_id, entity_type, states=("confirmed",)):
    """The entity ids of one type linked to a project."""
    placeholders = ", ".join("?" for _ in states)
    conn = db.connect()
    rows = conn.execute(
        f"SELECT entity_id FROM project_links WHERE project_id = ? "
        f"AND entity_type = ? AND state IN ({placeholders})",
        [project_id, entity_type, *states],
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def link_legacy_label(label, entity_type, entity_id):
    """Link an entity to whatever project its legacy free-text label resolves to.

    The dual-write path for already-analyzed email (plan 6.3: the legacy label
    keeps being written and is a free, strong signal). Confirmed rather than
    proposed on purpose: this is a deterministic name/alias identity, not a model
    judgement — 'AI Gov' IS project FAB-001. LLM ranking, which is a judgement,
    still arrives as a proposal in P3.

    Returns the project_id linked, or None if the label matches nothing.
    """
    if not label or str(label).strip().lower() == "unassigned":
        return None
    project_id = resolve_name(label)
    if not project_id:
        return None
    project = get_project(project_id)
    if not project or project["status"] != "active":
        return None
    try:
        link(
            project_id,
            entity_type,
            entity_id,
            state="confirmed",
            confidence=1.0,
            rationale=f"Legacy label {label!r} matches this project exactly.",
            assigned_by="auto",
        )
    except ProjectError:
        # The cap is already full, or the row is otherwise refused. Not fatal:
        # the legacy label is still written by the caller either way.
        return None
    return project_id


# --------------------------------------------------------------------------- #
# Backfill by keyword — the remedy for the one accepted consequence
#
# Analyses are never recomputed (decision 10), so a project created AFTER an
# email was analyzed can never appear as a proposal for it: already-analyzed mail
# is frozen. This is the agreed way out — alias/keyword-match a new project
# against historical email and offer the hits as proposals. Pure string
# matching, no model, no re-evaluation.
#
# These read email_projects directly. Reaching across to another module's table
# for a read is the point of decision 1 (one database, separate modules): a join
# like this is impossible across ATTACHed files.
# --------------------------------------------------------------------------- #
def backfill_candidates(project_id, limit=200):
    """Historical emails that match a project's name, aliases or keywords and
    are not already linked to it. Newest first."""
    project = get_project(project_id)
    if not project:
        return []
    terms = _match_terms(project, list_aliases(project_id))
    if not terms:
        return []
    linked = set(entity_ids_for_project(project_id, "email", states=STATES))

    conn = db.connect()
    rows = conn.execute(
        "SELECT message_id, subject, received, summary, project FROM email_projects "
        "ORDER BY received DESC"
    ).fetchall()
    conn.close()

    hits = []
    for message_id, subject, received, summary, legacy_label in rows:
        if message_id in linked:
            continue
        haystack = " ".join(str(x or "") for x in (subject, summary, legacy_label))
        collapsed, spaced = _normalize(haystack), _spaced(haystack)
        matched = [
            term for term, _weight in terms if _term_hits(term, collapsed, spaced)
        ]
        if matched:
            hits.append(
                {
                    "message_id": message_id,
                    "subject": subject,
                    "received": received,
                    "legacy_label": legacy_label,
                    "matched": matched,
                }
            )
        if len(hits) >= limit:
            break
    return hits


def backfill_link(project_id, message_ids, state="proposed"):
    """Link a batch of historical emails to a project. Defaults to `proposed`,
    not `confirmed`: a keyword hit is a suggestion, unlike the exact label
    identity that link_legacy_label handles."""
    linked = 0
    for message_id in message_ids:
        try:
            link(
                project_id,
                "email",
                message_id,
                state=state,
                rationale="Keyword backfill against historical email.",
                assigned_by="auto",
            )
            linked += 1
        except ProjectError:
            continue
    return linked


def legacy_label_counts(limit=40, exclude_registered=True):
    """Top legacy free-text email labels by count, for the 'create from existing
    label' shortcut. NOT a migration — it just makes manual entry cheap for the
    labels that actually carry volume.

    Jira-key-shaped labels are dropped: about 21 of them exist, and they are
    precisely the anti-pattern this register refuses. 'Unassigned' is dropped
    too, being the absence of a label rather than one.
    """
    conn = db.connect()
    rows = conn.execute(
        "SELECT project, COUNT(*) FROM email_projects "
        "WHERE project IS NOT NULL AND TRIM(project) != '' "
        "AND project != 'Unassigned' "
        "GROUP BY project ORDER BY COUNT(*) DESC, project"
    ).fetchall()
    conn.close()

    out = []
    for label, total in rows:
        # A label that IS a bare Jira issue key, e.g. 'SPR-60789'. A descriptive
        # label that merely mentions one ('SPR-61086 TC53e Trigger Issue') is
        # kept: that names real work.
        if re.fullmatch(r"[A-Za-z]{2,6}-\d+", str(label).strip()):
            continue
        registered = resolve_name(label)
        if exclude_registered and registered:
            continue
        out.append(
            {"label": label, "emails": int(total), "project_id": registered}
        )
        if len(out) >= limit:
            break
    return out


def has_absorbed_special_projects():
    """True once the legacy list has been imported for this profile."""
    return bool(db.get_meta(ABSORBED_KEY))


def absorb_special_projects():
    """Turn the legacy special_projects rows into the first active projects,
    keywords intact. Runs once per profile, flagged in profile_meta.

    Those rows were the closest thing the app already had to a project register,
    which is why they seed this one rather than being retyped. The legacy list is
    left in place; pages/14 stops reading it in P4.
    """
    if db.get_meta(ABSORBED_KEY):
        return {"absorbed": 0, "skipped": 0, "already_done": True}

    import config

    created, skipped = [], []
    for row in config.load_special_projects():
        name = str(row.get("subject", "")).strip()
        if not name:
            continue
        if resolve_name(name):
            skipped.append(name)
            continue
        try:
            created.append(
                create_project(name, keywords=str(row.get("keywords", "")).strip())
            )
        except ProjectError:
            skipped.append(name)

    db.set_meta(ABSORBED_KEY, _now())
    return {
        "absorbed": len(created),
        "skipped": len(skipped),
        "already_done": False,
        "ids": created,
        "skipped_names": skipped,
    }
