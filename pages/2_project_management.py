"""
PROJECT MANAGEMENT — the register that the rest of the app hangs off.

A PROJECT here sits at the altitude of a *Jira project* ("SPR"), not a Jira
issue ("SPR-12345"). Its job is holding the non-Jira work that has nowhere else
to live, and giving email, Jira items and tasks something durable to point at.

P1 scope (this file): the register itself — prefix setup, manual CRUD, aliases,
close/reopen, merge, and on-demand descriptions. Linking is manual only here;
email proposals arrive in P2/P3, at which point the Proposals tab below stops
being an honest zero.

Creation is deliberately a human act. The LLM never invents a project, only
ranks ones that already exist — that single rule is what keeps this register
from rotting back into a pile of free-text labels.
"""
import pandas as pd
import streamlit as st

import config
import llm_prompts as P
import project_db

st.header("📁 Project Management")

ENTITY_LABELS = {
    "email": "Emails",
    "conversation": "Threads",
    "jira": "Jira",
    "action": "Actions",
    "shipment": "Shipments",
}


def _toast_error(exc):
    """project_db raises ProjectError with a message written for the user, so
    surface it as-is rather than wrapping it in something vaguer."""
    st.error(str(exc))


# --------------------------------------------------------------------------- #
# First run — the id prefix, and seeding from the legacy Special Projects list
# --------------------------------------------------------------------------- #
locked = project_db.prefix_is_locked()

if not locked:
    st.info(
        "Set your project ID prefix before creating the first project. Every ID "
        "is minted from it (`FAB-001`, `FAB-002`, …), so it is locked as soon as "
        "a project exists."
    )
    with st.form("prefix_form"):
        prefix_input = st.text_input(
            "Project ID prefix",
            value=project_db.get_prefix(),
            max_chars=8,
            help="Letters and digits only. Kept short — it prefixes every ID.",
        )
        if st.form_submit_button("Set prefix", type="primary"):
            try:
                st.success(f"Prefix set to **{project_db.set_prefix(prefix_input)}**.")
                st.rerun()
            except project_db.ProjectError as exc:
                _toast_error(exc)
else:
    st.caption(
        f"Project ID prefix: **{project_db.get_prefix()}** — locked, because IDs "
        f"have already been minted from it."
    )

# The legacy special_projects list is the closest thing the app already had to a
# register, so it seeds this one rather than being retyped. Offered as a button
# rather than run automatically: the prefix must be chosen first, since it is
# baked into every ID the seeding mints.
_legacy = config.load_special_projects()
if _legacy and not project_db.has_absorbed_special_projects():
    with st.container(border=True):
        st.markdown(
            f"**Seed the register** — you have **{len(_legacy)}** entries in the "
            f"legacy Special Projects list. Import them as active projects, "
            f"keywords intact."
        )
        st.caption(", ".join(p["subject"] for p in _legacy))
        if st.button("Import Special Projects", type="primary"):
            result = project_db.absorb_special_projects()
            st.success(
                f"Imported {result['absorbed']} project(s)."
                + (
                    f" Skipped {result['skipped']} already in the register: "
                    f"{', '.join(result['skipped_names'])}."
                    if result["skipped"]
                    else ""
                )
            )
            st.rerun()

tab_register, tab_proposals = st.tabs(["📋 Register", "✅ Proposals"])

# --------------------------------------------------------------------------- #
# Register — create, browse, and drill into one project
# --------------------------------------------------------------------------- #
with tab_register:
    with st.expander("➕ New project", expanded=not locked):
        with st.form("new_project", clear_on_submit=True):
            col_name, col_owner = st.columns([2, 1])
            new_name = col_name.text_input(
                "Name", placeholder="e.g. TC101 Japan Post Quality Issue"
            )
            new_owner = col_owner.text_input("Owner (optional)")
            new_keywords = st.text_input(
                "Keywords (comma-separated, optional)",
                placeholder="AI Governance, AI committee",
                help="Used by the email sift to shortlist this project as a "
                "candidate. Costs nothing — it is plain string matching.",
            )
            col_jira, col_start = st.columns(2)
            new_jira = col_jira.text_input(
                "Jira project key (optional)",
                placeholder="SPR",
                help="The Jira PROJECT, e.g. SPR — never an issue key like "
                "SPR-12345. A Jira issue can be linked to this project; it can "
                "never become one.",
            )
            new_start = col_start.date_input("Start date", value=None)
            new_desc = st.text_area(
                "Description (optional)",
                placeholder="Leave blank and you can generate one later.",
                height=80,
            )
            if st.form_submit_button("Create project", type="primary"):
                try:
                    created = project_db.create_project(
                        new_name,
                        keywords=new_keywords,
                        description=new_desc,
                        owner=new_owner,
                        jira_project_key=new_jira,
                        start_date=new_start.isoformat() if new_start else None,
                    )
                    st.success(f"Created **{created}**.")
                    st.rerun()
                except project_db.ProjectError as exc:
                    _toast_error(exc)

    # --- Create from an existing email label ------------------------------- #
    # NOT a migration. The historical labels are deliberately left unmigrated
    # (186 of them, 71 holding a single email); this just makes manual entry
    # cheap for the handful that actually carry volume. Bare Jira keys are
    # filtered out by legacy_label_counts — they are the anti-pattern.
    with st.expander("📨 Create from an existing email label"):
        label_rows = project_db.legacy_label_counts()
        if not label_rows:
            st.caption(
                "No unregistered email labels with volume. Either the register "
                "already covers them, or no email has been analyzed yet."
            )
        else:
            st.caption(
                f"{len(label_rows)} label(s) from analyzed email that are not in "
                f"the register, busiest first. Creating one does NOT link its "
                f"email — use Backfill in the project's detail pane for that."
            )
            for row in label_rows[:15]:
                label_col, count_col, make_col = st.columns([5, 1, 2])
                label_col.write(row["label"])
                count_col.caption(f"{row['emails']} email(s)")
                if make_col.button(
                    "Create", key=f"fromlabel_{row['label']}"
                ):
                    try:
                        made = project_db.create_project(row["label"])
                        st.success(f"Created **{made}** from “{row['label']}”.")
                        st.rerun()
                    except project_db.ProjectError as exc:
                        _toast_error(exc)

    col_search, col_closed = st.columns([3, 1])
    search = col_search.text_input(
        "Filter by name or keyword", placeholder="Type to narrow the register"
    )
    show_closed = col_closed.toggle("Show closed", value=False)

    projects = project_db.list_projects(include_closed=show_closed, search=search)
    counts = project_db.link_counts("confirmed")

    if not projects:
        st.info(
            "No projects yet."
            if not search
            else f"Nothing in the register matches “{search}”."
        )
    else:
        rows = []
        for proj in projects:
            linked = counts.get(proj["project_id"], {})
            rows.append(
                {
                    "ID": proj["project_id"],
                    "Name": proj["name"],
                    "Status": proj["status"],
                    "Owner": proj["owner"] or "—",
                    "Start": proj["start_date"] or "—",
                    "Closed": proj["close_date"] or "—",
                    **{
                        label: linked.get(key, 0)
                        for key, label in ENTITY_LABELS.items()
                    },
                    "Linked": sum(linked.values()),
                }
            )
        st.dataframe(
            pd.DataFrame(rows), width="stretch", hide_index=True
        )
        st.caption(
            f"{len(projects)} project(s)"
            + (" including closed." if show_closed else " (active only).")
        )

        st.divider()

        # ------------------------------------------------------------------- #
        # Detail pane
        # ------------------------------------------------------------------- #
        st.subheader("Project detail")
        options = {f"{p['project_id']} — {p['name']}": p for p in projects}
        chosen_label = st.selectbox("Project", list(options))
        project = options[chosen_label]
        project_id = project["project_id"]

        if project["status"] == "closed":
            st.warning(
                f"Closed on {project['close_date']}. It is excluded from email "
                f"candidates until reopened."
            )

        # --- Description, generated on demand only ------------------------- #
        with st.container(border=True):
            st.markdown("**Description**")
            if project["description"]:
                st.write(project["description"])
                st.caption(
                    f"Source: {project['description_source'] or 'unknown'} · "
                    f"updated {project['description_updated'] or '—'}"
                )
            else:
                st.caption("No description yet.")

            user_written = project["description_source"] == "user"
            gen_col, warn_col = st.columns([1, 3])
            if gen_col.button(
                "✨ Generate description",
                disabled=user_written,
                help="Your own description is kept — clear it first to allow "
                "regeneration."
                if user_written
                else "One LLM call, only when you ask for it.",
            ):
                client = st.session_state.get("gemini_client")
                if client is None:
                    st.error(
                        "Gemini client not initialized. Open Settings and check "
                        "your GCP credentials."
                    )
                else:
                    links = project_db.links_for_project(project_id, state="confirmed")
                    linked_block = "\n".join(
                        f"- {row['entity_type']}: {row['entity_id']}" for row in links
                    )
                    with st.spinner("Describing the project…"):
                        text = P.generate(
                            client,
                            P.build_project_description_prompt(
                                project["name"],
                                keywords=project["keywords"] or "",
                                linked_block=linked_block,
                                team_context=config.team_context_block(),
                            ),
                            temperature=0.2,
                        )
                    if project_db.set_llm_description(project_id, (text or "").strip()):
                        st.success("Description generated.")
                        st.rerun()
                    else:
                        st.info("Left your own description in place.")
            if user_written:
                warn_col.caption(
                    "This description is yours, so generation is off. Editing it "
                    "below keeps it yours."
                )

        # --- Edit --------------------------------------------------------- #
        with st.expander("✏️ Edit project"):
            with st.form(f"edit_{project_id}"):
                edit_name = st.text_input("Name", value=project["name"])
                edit_owner = st.text_input("Owner", value=project["owner"] or "")
                edit_keywords = st.text_input(
                    "Keywords", value=project["keywords"] or ""
                )
                edit_jira = st.text_input(
                    "Jira project key", value=project["jira_project_key"] or ""
                )
                edit_desc = st.text_area(
                    "Description",
                    value=project["description"] or "",
                    height=110,
                    help="Saving here marks the description as yours, which "
                    "turns off regeneration.",
                )
                if st.form_submit_button("Save changes", type="primary"):
                    try:
                        fields = {
                            "name": edit_name,
                            "owner": edit_owner,
                            "keywords": edit_keywords,
                            "jira_project_key": edit_jira,
                        }
                        # Only pass description when it actually changed, so an
                        # untouched LLM description is not silently reclassified
                        # as user-authored.
                        if (edit_desc or "").strip() != (project["description"] or "").strip():
                            fields["description"] = edit_desc
                        project_db.update_project(project_id, **fields)
                        st.success("Saved.")
                        st.rerun()
                    except project_db.ProjectError as exc:
                        _toast_error(exc)

        # --- Aliases ------------------------------------------------------ #
        with st.expander("🏷️ Aliases"):
            st.caption(
                "Alternate names that resolve to this project, so manual entry "
                "can be sloppy. Matching ignores case, spaces and punctuation, "
                "so `TC-101`, `TC 101` and `tc101` are already the same thing — "
                "an alias is for genuinely different names."
            )
            existing = project_db.list_aliases(project_id)
            if existing:
                st.write(" · ".join(f"`{alias}`" for alias in existing))
            else:
                st.caption("None.")
            alias_col, remove_col = st.columns(2)
            with alias_col.form(f"alias_add_{project_id}", clear_on_submit=True):
                new_alias = st.text_input("Add alias")
                if st.form_submit_button("Add"):
                    try:
                        project_db.add_alias(project_id, new_alias)
                        st.rerun()
                    except project_db.ProjectError as exc:
                        _toast_error(exc)
            if existing:
                with remove_col.form(f"alias_del_{project_id}"):
                    drop = st.selectbox("Remove alias", existing)
                    if st.form_submit_button("Remove"):
                        project_db.remove_alias(drop)
                        st.rerun()

        # --- Linked items ------------------------------------------------- #
        with st.expander("🔗 Linked items"):
            links = project_db.links_for_project(project_id)
            if not links:
                st.caption(
                    "Nothing linked yet. Email linking arrives in P2; Jira "
                    "attachment is manual and arrives in P4."
                )
            else:
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "Type": row["entity_type"],
                                "Entity": row["entity_id"],
                                "State": row["state"],
                                "Confidence": row["confidence"],
                                "By": row["assigned_by"],
                                "Linked": row["created_at"],
                            }
                            for row in links
                        ]
                    ),
                    width="stretch",
                    hide_index=True,
                )

        # --- Backfill by keyword ------------------------------------------ #
        # The remedy for the one accepted consequence of never re-evaluating:
        # a project created after an email was analyzed can never be proposed
        # for it, because that analysis is frozen. This alias/keyword-matches
        # the project against historical email instead. Pure string matching —
        # no model, no re-evaluation.
        with st.expander("⏪ Backfill from historical email"):
            st.caption(
                "Matches this project's name, aliases and keywords against email "
                "already analyzed, and files the hits as proposals for you to "
                "approve. No LLM call — this is plain string matching."
            )
            if st.button("Find historical matches", key=f"bf_{project_id}"):
                st.session_state[f"bf_hits_{project_id}"] = (
                    project_db.backfill_candidates(project_id)
                )
            hits = st.session_state.get(f"bf_hits_{project_id}")
            if hits is None:
                pass
            elif not hits:
                st.info(
                    "No unlinked historical email matches this project's terms. "
                    "Adding keywords or an alias may widen it."
                )
            else:
                st.write(f"**{len(hits)}** matching email(s):")
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "Received": (row["received"] or "")[:10],
                                "Subject": row["subject"],
                                "Legacy label": row["legacy_label"] or "—",
                                "Matched on": ", ".join(row["matched"][:3]),
                            }
                            for row in hits
                        ]
                    ),
                    width="stretch",
                    hide_index=True,
                )
                if st.button(
                    f"Propose all {len(hits)}",
                    type="primary",
                    key=f"bf_apply_{project_id}",
                ):
                    n = project_db.backfill_link(
                        project_id, [row["message_id"] for row in hits]
                    )
                    st.session_state.pop(f"bf_hits_{project_id}", None)
                    st.success(
                        f"Filed {n} proposal(s). Approve them in the Proposals tab."
                    )
                    st.rerun()

        # --- Lifecycle ---------------------------------------------------- #
        with st.expander("🔄 Close, reopen, merge, delete"):
            if project["status"] == "active":
                close_col, close_btn = st.columns([2, 1])
                close_on = close_col.date_input(
                    "Close date", value=None, key=f"close_{project_id}"
                )
                st.caption(
                    "Closing exists to inactivate: a closed project stops being "
                    "offered for new email. There is no target date."
                )
                if close_btn.button("Close project", key=f"close_btn_{project_id}"):
                    project_db.close_project(
                        project_id, close_on.isoformat() if close_on else None
                    )
                    st.rerun()
            else:
                if st.button("Reopen project", key=f"reopen_{project_id}"):
                    project_db.reopen_project(project_id)
                    st.rerun()

            st.divider()
            others = [
                p for p in project_db.list_projects(include_closed=True)
                if p["project_id"] != project_id
            ]
            if others:
                merge_labels = {
                    f"{p['project_id']} — {p['name']}": p["project_id"] for p in others
                }
                merge_into = st.selectbox(
                    "Merge this project into", list(merge_labels), key=f"merge_{project_id}"
                )
                st.caption(
                    f"Moves every link from **{project_id}** to the target, keeps "
                    f"**{project['name']}** as an alias of it, then deletes "
                    f"**{project_id}**. Not reversible."
                )
                if st.button("Merge", key=f"merge_btn_{project_id}"):
                    try:
                        project_db.merge_projects(project_id, merge_labels[merge_into])
                        st.success(f"Merged into {merge_labels[merge_into]}.")
                        st.rerun()
                    except project_db.ProjectError as exc:
                        _toast_error(exc)

            st.divider()
            st.caption(
                "Deleting drops the project, its aliases and its links. The ID is "
                "never reissued. Closing is almost always what you want instead."
            )
            confirm = st.text_input(
                f"Type {project_id} to confirm deletion", key=f"del_{project_id}"
            )
            if st.button(
                "Delete permanently",
                disabled=confirm.strip().upper() != project_id.upper(),
                key=f"del_btn_{project_id}",
            ):
                project_db.delete_project(project_id)
                st.success(f"Deleted {project_id}.")
                st.rerun()

# --------------------------------------------------------------------------- #
# Proposals — the P3 approval queue. Real, but nothing proposes into it yet.
# --------------------------------------------------------------------------- #
with tab_proposals:
    st.subheader("Pending project proposals")
    pending = project_db.pending_proposals()
    if not pending:
        st.info(
            "Nothing awaiting approval. The Email Action Identifier starts "
            "proposing projects in P3; until then, link items by hand from the "
            "Register tab."
        )
    else:
        st.caption(
            f"{len(pending)} proposal(s). Approving links the item; rejecting is "
            f"remembered, so the same wrong project is not proposed again."
        )
        for row in pending:
            with st.container(border=True):
                head, approve, reject = st.columns([4, 1, 1])
                head.markdown(
                    f"**{row['project_id']} — {row['name']}** · "
                    f"{row['entity_type']} `{row['entity_id']}`"
                )
                if row["confidence"] is not None:
                    head.caption(f"Confidence {row['confidence']:.2f}")
                if row["rationale"]:
                    head.caption(row["rationale"])
                key = f"{row['project_id']}_{row['entity_type']}_{row['entity_id']}"
                if approve.button("Approve", key=f"ok_{key}", type="primary"):
                    try:
                        project_db.set_link_state(
                            row["project_id"], row["entity_type"],
                            row["entity_id"], "confirmed",
                        )
                        st.rerun()
                    except project_db.ProjectError as exc:
                        _toast_error(exc)
                if reject.button("Reject", key=f"no_{key}"):
                    project_db.set_link_state(
                        row["project_id"], row["entity_type"],
                        row["entity_id"], "rejected",
                    )
                    st.rerun()
