"""
jira_analysis.py — shared Jira ticket analysis.

Used by the Jira State Tracker page and the 1:1 Meeting Prep Assistant so the
deep-analysis + caching logic lives in one place. No Streamlit imports, so it is
reusable and unit-testable.
"""
import json

import config
import jira_db
import llm_prompts as P
from api_helpers import fetch_jira_ticket_full

# Bump when the analysis schema changes so older cached results auto-regenerate
# (v2 added the fishbone; v3 added the 5-Whys root cause + custom-field ingestion).
ANALYSIS_VERSION = 3


def parse_llm_json(text):
    """Strip markdown fences and parse the model's JSON object."""
    raw = (text or "").strip()
    if "```json" in raw:
        raw = raw.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in raw:
        raw = raw.split("```", 1)[1].split("```", 1)[0]
    return json.loads(raw)


def ticket_block(t):
    """Render a fetched ticket dict into the text block the prompt expects."""
    parts = [
        f"SUMMARY: {t.get('summary', '')}",
        f"JIRA STATUS FIELD: {t.get('status', '')}",
        f"ASSIGNEE: {t.get('assignee', '')}",
        f"CREATED: {t.get('created', '')}",
        "",
        "DESCRIPTION:",
        (t.get("description") or "")[:4000],
        "",
        "COMMENTS (oldest to newest):",
    ]
    for c in t.get("comments", []):
        parts.append(
            f"[{(c.get('created') or '')[:10]}] {c.get('author', 'Unknown')}: "
            f"{(c.get('body') or '')[:1500]}"
        )
    extra = t.get("extra_fields") or []
    if extra:
        parts.append("")
        parts.append("OTHER JIRA FIELDS (incl. custom fields like Root Cause Description):")
        for f in extra:
            parts.append(f"- {f.get('name', '')}: {str(f.get('value', ''))[:800]}")
    return "\n".join(parts)


def _delta_base_block(ticket, description_changed):
    """Compact ticket-identity reminder for the delta prompt. The description is
    only included when it changed since the last analysis (otherwise the prior
    analysis already reflects it)."""
    parts = [
        f"SUMMARY: {ticket.get('summary', '')}",
        f"JIRA STATUS FIELD: {ticket.get('status', '')}",
        f"ASSIGNEE: {ticket.get('assignee', '')}",
    ]
    if description_changed:
        parts += ["", "DESCRIPTION (CHANGED since last analysis):",
                  (ticket.get("description") or "")[:4000]]
    return "\n".join(parts)


def _delta_new_block(new_comments, changed_fields, attachments_text):
    """The delta payload: only the new comments, new/changed custom fields, and
    any attachment text — never the full history."""
    parts = []
    if new_comments:
        parts.append("NEW COMMENTS (oldest to newest):")
        for c in new_comments:
            parts.append(
                f"[{(c.get('created') or '')[:10]}] {c.get('author', 'Unknown')}: "
                f"{(c.get('body') or '')[:1500]}"
            )
    if changed_fields:
        parts.append("")
        parts.append("NEW / CHANGED JIRA FIELDS (incl. custom fields like Root Cause Description):")
        for f in changed_fields:
            parts.append(f"- {f.get('name', '')}: {str(f.get('value', ''))[:800]}")
    if attachments_text and attachments_text.strip():
        parts.append("")
        parts.append("ATTACHMENT CONTENT (meeting minutes, test/FA reports, etc.):")
        parts.append(attachments_text)
    if not parts:
        parts.append("(No new comments, fields, or attachments — re-confirm the prior analysis.)")
    return "\n".join(parts)


def get_or_analyze_ticket(client, key, team_context=None, force=False, attachments_text=""):
    """Fetch a Jira ticket and return (ticket_dict, analysis_dict).

    Backed by the SQLite store (jira_db). Behaviour:
      - No content change since the last analysis (same comments, description, and
        custom fields) and same ANALYSIS_VERSION → return the cached analysis,
        ignoring trivial Jira 'updated' bumps (labels, watchers, etc.).
      - New comments / changed fields / supplied attachments, with a usable prior
        analysis at the current version → DELTA update: send the prior analysis
        plus only the new content to the model.
      - No prior row, version mismatch (schema changed), or force=True → FULL
        rebuild from the entire ticket.
    The result + provenance is persisted either way."""
    ticket = fetch_jira_ticket_full(key)
    row = jira_db.get_spr(key)

    # Current provenance fingerprints.
    cur_desc_hash = jira_db.hash_text(ticket.get("description"))
    cur_fields_hash = jira_db.hash_extra_fields(ticket.get("extra_fields"))
    cur_comment_fps = jira_db.comment_fingerprints(ticket.get("comments"))
    cur_attach_ids = jira_db.attachment_ids(ticket.get("attachments"))

    version_match = bool(row) and row.get("analysis_version") == ANALYSIS_VERSION
    # Rows imported from the legacy JSON cache carry no fingerprints; fall back to
    # the old 'updated'-based freshness check for them so we don't needlessly
    # re-bill already-analyzed tickets on first view.
    has_provenance = bool(row and (row.get("seen_comments") or row.get("description_hash")))

    # What changed since the last analysis?
    if row and has_provenance:
        seen_comments = set(row.get("seen_comments") or [])
        new_comments = [
            c for c, fp in zip(ticket.get("comments", []), cur_comment_fps)
            if fp not in seen_comments
        ]
        description_changed = cur_desc_hash != row.get("description_hash")
        fields_changed = cur_fields_hash != row.get("extra_fields_hash")
        new_attachments = bool(set(cur_attach_ids) - set(row.get("seen_attachments") or []))
    else:
        new_comments, description_changed, fields_changed, new_attachments = [], True, True, True

    content_changed = bool(
        new_comments or description_changed or fields_changed or new_attachments
        or attachments_text
    )

    # --- Cache hit: nothing analyzable changed (provenanced rows), or a migrated
    # row whose Jira 'updated' is unchanged. ---
    if not force and version_match and not attachments_text:
        if has_provenance and not content_changed:
            return ticket, row["analysis"]
        if not has_provenance and row.get("updated") == ticket.get("updated"):
            return ticket, row["analysis"]

    if team_context is None:
        team_context = config.team_context_block()

    # --- Delta update: prior analysis at the current version + only new content.
    # Requires real provenance (a precise comment/field diff). ---
    if not force and version_match and has_provenance and row.get("analysis"):
        changed_fields = ticket.get("extra_fields") if fields_changed else []
        prompt = P.build_jira_state_delta_prompt(
            json.dumps(row["analysis"], indent=2, default=str),
            _delta_base_block(ticket, description_changed),
            _delta_new_block(new_comments, changed_fields, attachments_text),
            team_context=team_context,
        )
    else:
        # --- Full rebuild: no prior row, schema bump, or forced. ---
        prompt = P.build_jira_state_prompt(
            ticket_block(ticket), attachments_text, team_context=team_context
        )

    analysis = parse_llm_json(P.generate(client, prompt, temperature=0.2))

    jira_db.upsert_spr(
        key,
        {
            "summary": ticket.get("summary", ""),
            "status": ticket.get("status", ""),
            "updated": ticket.get("updated", ""),
            "analysis_version": ANALYSIS_VERSION,
            "analysis": analysis,
            "description_hash": cur_desc_hash,
            "extra_fields_hash": cur_fields_hash,
            "seen_comments": cur_comment_fps,
            "seen_attachments": cur_attach_ids,
        },
    )
    return ticket, analysis
