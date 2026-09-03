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

        # --- Sub-projects -------------------------------------------------- #
        # The level between this project and a single item: one Jira issue, or
        # one email thread. Far thinner than a project on purpose — the key is
        # the identity, so there is no ID to mint and no lifecycle beyond
        # open/done.
        with st.expander("🧩 Sub-projects"):
            st.caption(
                "A Jira issue or an email thread within this project. Jira "
                "sub-projects register themselves when you link a ticket; "
                "subject-based ones are added by hand, since a mailbox has "
                "hundreds of subjects and auto-registering them would be noise."
            )
            subs = project_db.list_subprojects(project_id)
            sub_counts = project_db.subproject_counts(project_id)
            if not subs:
                st.caption("None yet.")
            else:
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "Kind": sub["kind"],
                                "Key": sub["label"] or sub["key"],
                                "Status": sub["status"],
                                "Added": sub["created_by"],
                                **{
                                    label: sub_counts.get(
                                        (sub["kind"], sub["key"]), {}
                                    ).get(entity, 0)
                                    for entity, label in ENTITY_LABELS.items()
                                },
                            }
                            for sub in subs
                        ]
                    ),
                    width="stretch",
                    hide_index=True,
                )

            add_col, manage_col = st.columns(2)
            with add_col.form(f"sub_add_{project_id}", clear_on_submit=True):
                st.markdown("**Add a sub-project**")
                sub_kind = st.selectbox(
                    "Type",
                    project_db.SUBPROJECT_KINDS,
                    format_func=lambda k: "Jira issue" if k == "jira" else "Email subject",
                    key=f"subkind_{project_id}",
                )
                sub_key = st.text_input(
                    "Jira key or email subject",
                    placeholder="SPR-60789",
                    key=f"subkey_{project_id}",
                )
                if st.form_submit_button("Add"):
                    try:
                        project_db.add_subproject(project_id, sub_kind, sub_key)
                        st.rerun()
                    except project_db.ProjectError as exc:
                        _toast_error(exc)

            if subs:
                with manage_col.form(f"sub_manage_{project_id}"):
                    st.markdown("**Change one**")
                    choices = {
                        f"{s['label'] or s['key']} ({s['status']})": s for s in subs
                    }
                    picked_label = st.selectbox(
                        "Sub-project", list(choices), key=f"subpick_{project_id}"
                    )
                    picked = choices[picked_label]
                    action = st.radio(
                        "Action",
                        ["Mark done", "Reopen", "Delete"],
                        horizontal=True,
                        key=f"subaction_{project_id}",
                    )
                    st.caption(
                        "Deleting a sub-project leaves its emails and tickets "
                        "linked to the project — they just stop being grouped "
                        "under it."
                    )
                    if st.form_submit_button("Apply"):
                        if action == "Delete":
                            project_db.delete_subproject(
                                project_id, picked["kind"], picked["key"]
                            )
                        else:
                            project_db.set_subproject_status(
                                project_id,
                                picked["kind"],
                                picked["key"],
                                "done" if action == "Mark done" else "open",
                            )
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

    # ---------------------------------------------------------------------- #
    # Danger zone — empty the whole register
    #
    # Sits inside a collapsed expander at the very bottom, below the per-project
    # controls, so it is never adjacent to anything routine. Two independent
    # confirmations are required, and the handler re-checks both: `disabled=`
    # only stops the click in the browser, so on an irreversible bulk delete it
    # is a presentational guard rather than a real one.
    # ---------------------------------------------------------------------- #
    total_projects = project_db.count_projects(include_closed=True)

    # An already-empty register hides the danger zone below, which would leave
    # no route to a renumber for anyone who wiped first and decided afterwards.
    if not total_projects and project_db.next_id_number() > 1:
        st.divider()
        with st.expander("🔢 Restart project numbering"):
            st.caption(
                f"The register is empty, but the next project would be "
                f"**{project_db.get_prefix()}-"
                f"{int(project_db.next_id_number()):03d}** because IDs are "
                f"normally never reissued. Restarting is safe here: no project "
                f"exists to collide with, and nothing is deleted — only future "
                f"numbering changes."
            )
            if st.button("Restart numbering at 001", key="renumber_btn"):
                try:
                    project_db.reset_id_counter()
                    st.success(
                        f"The next project will be {project_db.get_prefix()}-001."
                    )
                    st.rerun()
                except project_db.ProjectError as exc:
                    _toast_error(exc)

    if total_projects:
        st.divider()
        with st.expander("🚨 Danger zone — delete ALL projects"):
            st.warning(
                f"This deletes **all {total_projects} project(s)** — active and "
                f"closed — along with every alias and every link to email and "
                f"Jira. It cannot be undone."
            )
            st.caption(
                "The legacy Special Projects import will be offered again, "
                "since otherwise an emptied register has no route back to the "
                "rows that seeded it."
            )
            wipe_sure = st.checkbox(
                "Are you sure? This removes every project in the register.",
                key="wipe_sure",
            )
            wipe_renumber = st.checkbox(
                f"Also restart numbering at 001 "
                f"(next project would otherwise be {project_db.get_prefix()}-"
                f"{int(project_db.next_id_number()):03d})",
                key="wipe_renumber",
                help="Off by default: IDs are normally never reissued, so a "
                "number that has been used cannot later point at different "
                "work. Tick this only for a deliberate clean slate.",
            )
            wipe_typed = st.text_input(
                "Type `delete` to confirm",
                key="wipe_typed",
                placeholder="delete",
                disabled=not wipe_sure,
            )
            wipe_ok = wipe_sure and wipe_typed.strip().lower() == "delete"
            if (
                st.button(
                    f"Delete all {total_projects} project(s)",
                    disabled=not wipe_ok,
                    key="wipe_btn",
                )
                and wipe_ok
            ):
                removed = project_db.delete_all_projects()
                project_db.clear_absorbed_flag()
                note = ""
                if wipe_renumber:
                    # After the wipe, so the empty-register precondition holds.
                    project_db.reset_id_counter()
                    note = f" Numbering restarts at {project_db.get_prefix()}-001."
                st.success(f"Deleted all {removed} project(s).{note}")
                st.rerun()

# --------------------------------------------------------------------------- #
# Proposals — the P3 approval queue. Real, but nothing proposes into it yet.
# --------------------------------------------------------------------------- #
with tab_proposals:
    st.subheader("Pending project proposals")
    pending = project_db.pending_proposals()
    if not pending:
        st.info(
            "Nothing awaiting approval. The Email Action Identifier proposes "
            "projects as it analyzes mail — run it from the Email Action "
            "Identifier page, or link items by hand from the Register tab."
        )
    else:
        st.caption(
            f"{len(pending)} proposal(s) from the email analyzer. Approving links "
            f"the item; **rejecting is remembered**, so the same wrong project is "
            f"never proposed for that item again — which is why rejecting is "
            f"worth doing rather than just leaving it."
        )

        # Filters above the queue, so a big batch can be worked through one
        # project or one confidence band at a time.
        project_names = sorted({f"{p['project_id']} — {p['name']}" for p in pending})
        f_proj, f_conf = st.columns([2, 1])
        only_projects = f_proj.multiselect(
            "Limit to project(s)", project_names, key="prop_filter_project"
        )
        min_conf = f_conf.slider(
            "Minimum confidence", 0.0, 1.0, 0.0, 0.05, key="prop_filter_conf"
        )
        chosen_ids = {label.split(" — ")[0] for label in only_projects}
        visible = [
            row
            for row in pending
            if (not chosen_ids or row["project_id"] in chosen_ids)
            and (row["confidence"] or 0.0) >= min_conf
        ]
        if len(visible) != len(pending):
            st.caption(f"Showing {len(visible)} of {len(pending)}.")

        if not visible:
            st.info("No proposals match those filters.")
        else:
            # One editable Decision column beats a pair of buttons per row: a
            # batch of eighty proposals is a table to work down, not eighty
            # separate interactions. "Leave" is the default so nothing is
            # decided by accident.
            queue = pd.DataFrame(
                [
                    {
                        "Decision": "Leave",
                        "Project": f"{row['project_id']} — {row['name']}",
                        "Type": row["entity_type"],
                        "Item": row["entity_label"],
                        "Confidence": row["confidence"],
                        "Why": row["rationale"],
                        "_pid": row["project_id"],
                        "_etype": row["entity_type"],
                        "_eid": row["entity_id"],
                    }
                    for row in visible
                ]
            )
            edited = st.data_editor(
                queue,
                width="stretch",
                hide_index=True,
                key="prop_editor",
                column_config={
                    "Decision": st.column_config.SelectboxColumn(
                        "Decision",
                        options=["Leave", "Approve", "Reject"],
                        required=True,
                        width="small",
                    ),
                    "Confidence": st.column_config.NumberColumn(
                        "Conf.", format="%.2f", width="small"
                    ),
                    "Item": st.column_config.TextColumn("Item", width="large"),
                    "Why": st.column_config.TextColumn("Why", width="medium"),
                    "_pid": None,
                    "_etype": None,
                    "_eid": None,
                },
                disabled=["Project", "Type", "Item", "Confidence", "Why"],
            )

            STATE = {"Approve": "confirmed", "Reject": "rejected"}
            decisions = [
                (r["_pid"], r["_etype"], r["_eid"], STATE[r["Decision"]])
                for r in edited.to_dict("records")
                if r["Decision"] in STATE
            ]
            apply_col, all_col = st.columns([1, 2])
            if apply_col.button(
                f"Apply {len(decisions)} decision(s)",
                type="primary",
                disabled=not decisions,
                key="prop_apply",
            ) and decisions:
                applied, errors = project_db.decide_proposals(decisions)
                for message in errors[:3]:
                    st.error(message)
                if applied:
                    st.success(f"Applied {applied} decision(s).")
                if not errors:
                    st.rerun()

            with all_col.expander(f"Decide all {len(visible)} shown at once"):
                st.caption(
                    "Applies to the filtered view above, not the whole queue. "
                    "Approving in bulk can hit the "
                    f"{project_db.MAX_CONFIRMED_PER_ENTITY}-project cap on an "
                    "item; anything refused is reported rather than dropped."
                )
                bulk_a, bulk_r = st.columns(2)
                if bulk_a.button("Approve all shown", key="prop_all_ok"):
                    applied, errors = project_db.decide_proposals(
                        [(r["project_id"], r["entity_type"], r["entity_id"], "confirmed")
                         for r in visible]
                    )
                    for message in errors[:5]:
                        st.error(message)
                    st.success(f"Approved {applied}.")
                    if not errors:
                        st.rerun()
                if bulk_r.button("Reject all shown", key="prop_all_no"):
                    applied, _ = project_db.decide_proposals(
                        [(r["project_id"], r["entity_type"], r["entity_id"], "rejected")
                         for r in visible]
                    )
                    st.success(f"Rejected {applied}.")
                    st.rerun()
