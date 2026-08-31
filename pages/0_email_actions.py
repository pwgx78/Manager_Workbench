"""
Phase 0 — Email Action Identifier.

Ports the EmailToJira analyzer: fetch inbox mail for a timeframe, analyze each
message in the context of its thread history (SQLite-cached), and surface
Eisenhower-prioritized action items. Extended for the ME Manager Agent with:
  - a suggested Next Step per action,
  - a Suggested Response (aware of whether the user was the one asked to act),
  - an "Add to Tracker" button that files the action into a running, user-managed
    Email Action Tracker (Origin / Date Assigned / Priority / Date Completed +
    a tick-to-complete checkbox and the suggested response).
"""
import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

import config
import store
import email_db
import llm_prompts as P
import project_db
from api_helpers import fetch_recent_inbox, clean_html, fetch_carrier_tracking_text

st.header("📥 Email Action Identifier")
st.caption(
    "Pull recent inbox mail, identify prioritized action items in thread context, "
    "and file the ones that matter into your Email Action Tracker."
)

client = st.session_state.get("gemini_client")
if client is None:
    st.error("Gemini client not initialized. Open the app from the main page (app.py).")
    st.stop()

TRACKER_COLUMNS = [
    "Completed",
    "Priority",
    "Origin",
    "Action",
    "Email Thread",
    "Owner",
    "Date Assigned",
    "Date Completed",
    "Next Step",
    "Suggested Response",
]

QUADRANT_BADGE = {
    "Urgent": "🔴 Urgent",
    "Critical": "🟠 Critical",
    "Delegate": "🟡 Delegate",
    "Delete": "⚪ Delete (FYI)",
}

# Disposition vocabulary (code, label). "" = Pending (undo).
DISPOSITIONS = [
    ("", "— Pending —"),
    ("tracked", "➕ Add to Tracker"),
    ("read_no_action", "✓ Read – No Action"),
    ("delegated", "👥 Delegated"),
    ("follow_up", "⏰ Follow-up later"),
    ("ignore", "🗑️ Ignore / Delete"),
]
DISP_CODES = [c for c, _ in DISPOSITIONS]
DISP_LABELS = [lbl for _, lbl in DISPOSITIONS]
DISP_LABEL = dict(DISPOSITIONS)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _iso_since(delta):
    """ISO-8601 UTC timestamp `delta` before now."""
    return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def _time_filter_iso(option):
    """Mirror EmailToJira's timeframe → ISO-8601 UTC start time."""
    windows = {
        "15 mins": timedelta(minutes=15),
        "30 mins": timedelta(minutes=30),
        "1 hr": timedelta(hours=1),
        "4 hrs": timedelta(hours=4),
        "8 hrs": timedelta(hours=8),
        "24 hrs": timedelta(hours=24),
    }
    if option in windows:
        return _iso_since(windows[option])
    # "Today" (and any fallback) → midnight UTC today.
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_llm_json(text):
    """Strip markdown fences and parse the model's JSON object."""
    raw = (text or "").strip()
    if "```json" in raw:
        raw = raw.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in raw:
        raw = raw.split("```", 1)[1].split("```", 1)[0]
    return json.loads(raw)


def _add_to_tracker(task):
    """Append one analyzed action to the persistent Email Action Tracker JSON.
    Skips if an identical action (same Action + Email Thread + Owner) is already
    tracked, so re-dispositioning an item doesn't create duplicate rows."""
    actions = store.load_json(config.EMAIL_ACTIONS_KEY, default=[])
    for a in actions:
        if (
            a.get("Action") == task.get("Action", "")
            and a.get("Email Thread") == task.get("Email Thread", "")
            and a.get("Owner") == task.get("Owner", "")
        ):
            return
    actions.append(
        {
            "Completed": False,
            "Priority": task.get("Priority", "Medium"),
            "Origin": task.get("Origin", "Email"),
            "Action": task.get("Action", ""),
            "Email Thread": task.get("Email Thread", ""),
            "Owner": task.get("Owner", ""),
            "Date Assigned": datetime.today().strftime("%Y-%m-%d"),
            "Date Completed": "",
            "Next Step": task.get("Next Step", ""),
            "Suggested Response": task.get("Suggested Response", ""),
        }
    )
    store.save_json(config.EMAIL_ACTIONS_KEY, actions)


def _project_links_for_messages(message_ids):
    """Project ids confirmed on ANY message of a conversation, and the count of
    messages each covers. A conversation is not itself the unit of linking here —
    links are per email, matching the dual-write — so partial coverage is real
    and worth showing rather than hiding."""
    coverage = {}
    for message_id in message_ids:
        for project in project_db.projects_for_entity("email", message_id):
            coverage[project["project_id"]] = coverage.get(project["project_id"], 0) + 1
    return coverage


def _render_project_controls(conv_id, subject, summary_text, message_ids):
    """Manual project linking for one conversation.

    Applies to every message in the thread, like the disposition control above
    it, and writes 'email' links so these counts are directly comparable with
    the ones the analyzer's dual-write produces.

    Candidates come from the keyword pre-filter — pure string matching, no model
    call — so this costs nothing to render. The LLM starts ranking candidates in
    P3; until then this is how email gets into the register.
    """
    if not message_ids:
        return
    coverage = _project_links_for_messages(message_ids)
    total = len(message_ids)

    st.markdown("**Projects**")
    if coverage:
        st.caption(
            " · ".join(
                f"`{pid}` ({n}/{total} email{'s' if n != 1 else ''})"
                for pid, n in sorted(coverage.items())
            )
        )
    else:
        st.caption("Not linked to any project yet.")

    shortlist = project_db.shortlist_for_text(
        f"{subject}\n{summary_text}", include_scores=True
    )
    active = project_db.list_projects()
    labels = {f"{p['project_id']} — {p['name']}": p["project_id"] for p in active}
    if shortlist:
        st.caption(
            "Suggested by keyword: "
            + ", ".join(
                f"**{p['name']}** ({', '.join(p['_matched'][:3])})" for p in shortlist[:3]
            )
        )

    chosen = st.multiselect(
        "Link this conversation to",
        list(labels),
        default=[label for label, pid in labels.items() if pid in coverage],
        key=f"projlink_{conv_id}",
        help="Suggested projects are listed first in the caption above. Max "
        f"{project_db.MAX_CONFIRMED_PER_ENTITY} confirmed projects per email.",
    )
    chosen_ids = {labels[label] for label in chosen}

    act_col, new_col = st.columns([1, 2])
    if act_col.button("Apply projects", key=f"projapply_{conv_id}"):
        errors = []
        for project_id in chosen_ids - set(coverage):
            for message_id in message_ids:
                try:
                    project_db.link(
                        project_id, "email", message_id, state="confirmed",
                        rationale=f"Linked by hand from the thread {subject!r}.",
                        assigned_by="user",
                    )
                except project_db.ProjectError as exc:
                    errors.append(str(exc))
                    break
        for project_id in set(coverage) - chosen_ids:
            for message_id in message_ids:
                project_db.unlink(project_id, "email", message_id)
        if errors:
            st.error(errors[0])
        else:
            st.rerun()

    with new_col.form(f"projnew_{conv_id}", clear_on_submit=True):
        new_name = st.text_input(
            "…or create a new project from this thread",
            placeholder=subject[:60],
            label_visibility="collapsed",
        )
        if st.form_submit_button("Create & link"):
            typed = (new_name or "").strip() or subject
            try:
                # resolve_name first, so typing the name of something that
                # already exists links it instead of erroring. Creation stays a
                # human act either way — the LLM never reaches this path.
                existing = project_db.resolve_name(typed)
                project_id = existing or project_db.create_project(typed)
                for message_id in message_ids:
                    project_db.link(
                        project_id, "email", message_id, state="confirmed",
                        rationale=f"Created from the thread {subject!r}.",
                        assigned_by="user",
                    )
                st.success(
                    f"{'Linked to' if existing else 'Created'} {project_id}."
                )
                st.rerun()
            except project_db.ProjectError as exc:
                st.error(str(exc))


tab_identify, tab_tracker, tab_projects, tab_ship = st.tabs(
    ["🔍 Identify Actions", "📋 Email Action Tracker", "🗂️ Projects & Themes", "📦 Shipments"]
)

# --------------------------------------------------------------------------- #
# 1. Identify actions
# --------------------------------------------------------------------------- #
with tab_identify:
    col1, col2 = st.columns([1, 2])
    with col1:
        timeframe = st.selectbox(
            "Fetch emails from the last:",
            ["15 mins", "30 mins", "1 hr", "4 hrs", "8 hrs", "24 hrs", "Today", "Custom…"],
            index=5,
        )
        custom_amount, custom_unit = 2, "hours"
        if timeframe == "Custom…":
            cc1, cc2 = st.columns(2)
            custom_amount = cc1.number_input(
                "Amount", min_value=1, max_value=10000, value=2, step=1, key="custom_amount"
            )
            custom_unit = cc2.selectbox(
                "Unit", ["minutes", "hours", "days"], index=1, key="custom_unit"
            )
    with col2:
        st.write("")
        st.write("")
        bypass_cache = st.checkbox("Bypass cache and force re-analysis", value=False)

    if st.button("Fetch and Analyze Emails", type="primary"):
        try:
            if timeframe == "Custom…":
                since_iso = _iso_since(timedelta(**{custom_unit: int(custom_amount)}))
            else:
                since_iso = _time_filter_iso(timeframe)
            with st.spinner("Fetching inbox…"):
                messages = fetch_recent_inbox(since_iso)

            if not messages:
                st.warning("No emails found in that timeframe.")
                st.session_state["phase0_tasks"] = []
            else:
                all_tasks = []
                by_conv = {}
                n_cache = n_fresh = n_failed = 0
                known_projects = email_db.list_known_projects()
                team_ctx = config.team_context_block()

                # Resume visibility: how many of these are already analyzed (and so
                # loaded instantly from cache) vs. genuinely new. Each fresh result
                # is committed as it completes, so an interrupted run picks up here
                # instead of re-scanning from the start.
                already = set() if bypass_cache else email_db.cached_message_ids(
                    [m.get("id") for m in messages]
                )
                to_do = len(messages) - len(already)
                if already and not bypass_cache:
                    st.info(
                        f"▶ Resuming — {len(already)} of {len(messages)} already analyzed "
                        f"(loaded from cache); {to_do} left to analyze."
                    )

                progress = st.progress(0.0)
                status = st.empty()

                for idx, msg in enumerate(messages):
                    mid = msg.get("id")
                    cached = None if bypass_cache else email_db.get_cached_analysis(mid)
                    if cached:
                        status.text(f"⏭️ {idx + 1}/{len(messages)} · already analyzed (cached)")
                    else:
                        status.text(f"🧠 {idx + 1}/{len(messages)} · analyzing new email…")

                    if cached:
                        out = cached
                        source = "Cached"
                        n_cache += 1
                    else:
                        conv = msg.get("conversationId")
                        subject = msg.get("subject", "No Subject")
                        context = email_db.get_thread_context(conv)
                        sender = (
                            msg.get("sender", {}).get("emailAddress", {}).get("name", "Unknown")
                        )
                        to_field = ", ".join(
                            r["emailAddress"]["address"]
                            for r in msg.get("toRecipients", [])
                            if "emailAddress" in r
                        )
                        body = clean_html(msg.get("body", {}).get("content", ""))
                        content = (
                            f"From: {sender}\nTo: {to_field}\nSubject: {subject}\n\n{body}"
                        )
                        try:
                            text = P.generate(
                                client,
                                P.build_email_action_prompt(
                                    content,
                                    context,
                                    known_projects=known_projects,
                                    team_context=team_ctx,
                                ),
                                temperature=0.2,
                            )
                            out = _parse_llm_json(text)
                        except Exception:
                            n_failed += 1
                            progress.progress((idx + 1) / len(messages))
                            continue
                        source = "Fresh"
                        n_fresh += 1
                        email_db.update_cached_analysis(mid, out)
                        if out.get("new_context_summary"):
                            email_db.update_thread_context(
                                conv, subject, out["new_context_summary"]
                            )

                    # Record the project tag (both fresh and cached branches, so
                    # permanently-cached older emails backfill the projects view).
                    project = out.get("Project") or "Unassigned"
                    summary = out.get("new_context_summary") or " · ".join(
                        t.get("Action", "") for t in out.get("extracted_tasks", [])
                    )
                    email_db.upsert_email_project(
                        mid,
                        msg.get("conversationId"),
                        project,
                        msg.get("subject", "No Subject"),
                        msg.get("receivedDateTime", ""),
                        summary,
                    )
                    # Dual-write into the project register. Nothing regresses:
                    # the legacy label above is still the source for the Projects
                    # & Themes view across every historical email, and this only
                    # adds a link when the label IS a registered project. The
                    # LLM is not proposing here — an exact name/alias match is a
                    # deterministic identity, so it confirms directly.
                    project_db.link_legacy_label(project, "email", mid)

                    # Shipment detection: upsert (merge) by tracking number.
                    ship = out.get("shipment")
                    if isinstance(ship, dict) and str(ship.get("tracking_number", "")).strip():
                        email_db.upsert_shipment(ship, mid)

                    # Accumulate per-conversation snippets for thread consolidation.
                    conv_id = msg.get("conversationId") or mid
                    received = msg.get("receivedDateTime", "") or ""
                    snip_sender = (
                        msg.get("sender", {}).get("emailAddress", {}).get("name", "Unknown")
                    )
                    task_lines = "; ".join(
                        f"{t.get('Action', '')} (owner {t.get('Owner', '?')}, due {t.get('Due Date', '?')})"
                        for t in out.get("extracted_tasks", [])
                    ) or "no explicit tasks"
                    entry = by_conv.setdefault(
                        conv_id,
                        {
                            "subject": msg.get("subject", "No Subject"),
                            "message_ids": [],
                            "latest": "",
                            "snippets": [],
                        },
                    )
                    entry["message_ids"].append(mid)
                    if received > entry["latest"]:
                        entry["latest"] = received
                    entry["snippets"].append(
                        f"[{received[:10]}] {snip_sender}: "
                        f"{out.get('new_context_summary') or summary}\n  tasks: {task_lines}"
                    )

                    for j, t in enumerate(out.get("extracted_tasks", [])):
                        t["Analysis Source"] = source
                        t["_item_id"] = f"{mid}:{j}"
                        t["_message_id"] = mid
                        t["_project"] = project
                        all_tasks.append(t)
                    progress.progress((idx + 1) / len(messages))

                status.empty()
                st.session_state["phase0_tasks"] = all_tasks

                # Consolidate each un-dispositioned conversation into a thread summary.
                conv_disp = email_db.get_conversation_dispositions()
                threads = []
                tstatus = st.empty()
                for ci, (cid, info) in enumerate(by_conv.items()):
                    base = {
                        "conv_id": cid,
                        "subject": info["subject"],
                        "count": len(info["message_ids"]),
                        "latest": info["latest"],
                        "message_ids": info["message_ids"],
                        "summary": None,
                    }
                    if conv_disp.get(cid):
                        threads.append(base)  # already dispositioned — no fresh summary
                        continue
                    # Reuse a cached thread summary while the thread's message set is
                    # unchanged, so this pass is also incremental/resumable and only
                    # re-summarizes conversations that are new or have grown.
                    fp = email_db.thread_fingerprint(info["message_ids"])
                    cached_sum, cached_fp = email_db.get_thread_summary(cid)
                    if cached_sum and cached_fp == fp:
                        base["summary"] = cached_sum
                        threads.append(base)
                        continue
                    tstatus.text(f"Summarizing conversation {ci + 1}/{len(by_conv)}…")
                    block = "\n\n".join(info["snippets"])
                    try:
                        base["summary"] = _parse_llm_json(
                            P.generate(
                                client,
                                P.build_thread_action_prompt(block, team_ctx),
                                temperature=0.2,
                            )
                        )
                        # Commit per conversation so an interruption keeps progress.
                        email_db.update_thread_summary(cid, fp, base["summary"])
                    except Exception:
                        base["summary"] = {
                            "summary": "\n".join(info["snippets"]),
                            "key_points": [],
                            "pending_tasks": [],
                            "suggested_action": "",
                            "suggested_disposition": "",
                            "requested_of_me": False,
                        }
                    threads.append(base)
                tstatus.empty()
                st.session_state["phase0_threads"] = threads

                msg_bits = [
                    f"{len(messages)} emails",
                    f"{len(by_conv)} conversations",
                    f"{n_cache} cached",
                    f"{n_fresh} fresh",
                ]
                if n_failed:
                    msg_bits.append(f"{n_failed} skipped (parse error)")
                st.success("Analysis complete — " + ", ".join(msg_bits) + ".")
        except Exception as e:
            st.error(f"{e}")

    # --- Conversations (un-dispositioned threads) ------------------------- #
    threads = st.session_state.get("phase0_threads")
    if threads is not None:
        st.divider()
        if not threads:
            st.info("No conversations found in the selected emails.")
        else:
            conv_disp = email_db.get_conversation_dispositions()
            pending = [c for c in threads if not conv_disp.get(c["conv_id"])]
            done_count = len(threads) - len(pending)

            show_all = st.checkbox("Show all (including dispositioned)", value=False)
            st.markdown(
                f"### {len(pending)} pending conversation(s) · {done_count} dispositioned"
            )

            visible = threads if show_all else pending
            visible = sorted(visible, key=lambda c: c.get("latest", ""), reverse=True)
            if not visible:
                st.success("All conversations dispositioned — toggle 'Show all' to review them.")

            for c in visible:
                conv_id = c["conv_id"]
                current = conv_disp.get(conv_id, "")
                s = c.get("summary") or {}
                title = (
                    f"{c['subject']} — {c['count']} email(s) · {(c.get('latest', '') or '')[:10]}"
                )
                with st.expander(title, expanded=not current):
                    if current:
                        st.caption(f"Status: {DISP_LABEL.get(current, current)}")
                    if s.get("summary"):
                        st.markdown(s["summary"])
                    if s.get("key_points"):
                        st.markdown("**Key points**")
                        for k in s["key_points"]:
                            st.markdown(f"- {k}")
                    if s.get("pending_tasks"):
                        st.markdown("**Pending tasks**")
                        for p in s["pending_tasks"]:
                            st.markdown(f"- {p}")
                    if s.get("suggested_action"):
                        st.markdown(f"**Suggested action:** {s['suggested_action']}")
                    if s.get("requested_of_me"):
                        st.caption("✅ You were asked to act on this thread.")

                    sugg = s.get("suggested_disposition")
                    if sugg in DISP_CODES and sugg and sugg != current:
                        st.caption(f"Suggested disposition: {DISP_LABEL.get(sugg, sugg)}")

                    # Default to the stored tag (never auto-apply the suggestion).
                    default_code = current if current in DISP_CODES else ""
                    chosen = st.selectbox(
                        "Disposition (applies to the whole conversation)",
                        DISP_LABELS,
                        index=DISP_CODES.index(default_code),
                        key=f"convdisp_{conv_id}",
                    )
                    new_code = DISP_CODES[DISP_LABELS.index(chosen)]
                    if new_code != current:
                        if new_code == "":
                            email_db.clear_conversation_disposition(conv_id, c["message_ids"])
                        else:
                            if new_code == "tracked":
                                _add_to_tracker(
                                    {
                                        "Action": s.get("suggested_action", "") or c["subject"],
                                        "Priority": "Medium",
                                        "Origin": "Email",
                                        "Email Thread": c["subject"],
                                        "Owner": "",
                                        "Next Step": s.get("suggested_action", ""),
                                        "Suggested Response": "",
                                    }
                                )
                            email_db.set_conversation_disposition(
                                conv_id, c["subject"], new_code, c["message_ids"]
                            )
                        st.rerun()

                    st.divider()
                    _render_project_controls(
                        conv_id,
                        c["subject"],
                        " ".join(
                            str(part)
                            for part in (
                                s.get("summary", ""),
                                " ".join(s.get("key_points", []) or []),
                            )
                        ),
                        c["message_ids"],
                    )

# --------------------------------------------------------------------------- #
# 2. Email Action Tracker
# --------------------------------------------------------------------------- #
with tab_tracker:
    st.subheader("Email Action Tracker")
    st.caption(
        "Your running list. Tick **Done** to mark an item complete (its completion "
        "date is filled in on save). Add or remove rows manually as needed."
    )

    records = store.load_json(config.EMAIL_ACTIONS_KEY, default=[])
    df = pd.DataFrame(records, columns=TRACKER_COLUMNS)

    # --- Sort controls (data_editor has no click-to-sort headers) --------- #
    sc1, sc2 = st.columns([2, 1])
    sort_col = sc1.selectbox(
        "Sort by", TRACKER_COLUMNS, key="phase0_tracker_sort_col"
    )
    sort_dir = sc2.radio(
        "Order", ["Ascending", "Descending"], horizontal=True,
        key="phase0_tracker_sort_dir",
    )
    if not df.empty:
        ascending = sort_dir == "Ascending"
        if sort_col == "Priority":
            # Logical High → Medium → Low ordering rather than alphabetical.
            order = pd.Categorical(
                df["Priority"], categories=["High", "Medium", "Low"], ordered=True
            )
            df = df.assign(_prio=order).sort_values(
                "_prio", ascending=ascending, kind="stable"
            ).drop(columns="_prio")
        else:
            df = df.sort_values(
                sort_col, ascending=ascending, kind="stable",
                na_position="last", key=lambda s: s.astype(str).str.lower(),
            )
        df = df.reset_index(drop=True)

    # --- Per-column filters ----------------------------------------------- #
    with st.expander("🔎 Filter rows"):
        hide_done = st.checkbox(
            "Hide completed (Done) actions", value=False,
            key="phase0_tracker_hide_done",
        )
        cat_options = {
            "Priority": ["High", "Medium", "Low"],
            "Origin": ["Email", "MS Teams", "Meeting", "Chat", "Other"],
        }
        text_cols = [
            "Action", "Email Thread", "Owner",
            "Date Assigned", "Date Completed", "Next Step", "Suggested Response",
        ]
        fcols = st.columns(3)
        col_filters = {}
        for i, col in enumerate(cat_options):
            col_filters[col] = fcols[i % 3].multiselect(
                col, cat_options[col], key=f"phase0_tracker_f_{col}"
            )
        for j, col in enumerate(text_cols):
            col_filters[col] = fcols[(len(cat_options) + j) % 3].text_input(
                f"{col} contains", key=f"phase0_tracker_f_{col}"
            ).strip()

    mask = pd.Series(True, index=df.index)
    if hide_done:
        mask &= ~df["Completed"].fillna(False).astype(bool)
    for col, sel in col_filters.items():
        if not sel:
            continue
        if isinstance(sel, list):
            mask &= df[col].isin(sel)
        else:
            mask &= df[col].astype(str).str.lower().str.contains(
                sel.lower(), na=False, regex=False
            )

    # Rows removed by filters are kept aside and re-saved untouched, so editing
    # a filtered view never drops the hidden rows.
    hidden_df = df[~mask]
    visible_df = df[mask].reset_index(drop=True)
    if len(hidden_df):
        st.caption(
            f"Showing {len(visible_df)} of {len(df)} row(s) · "
            f"{len(hidden_df)} hidden by filters (preserved on save)."
        )

    # New editor instance whenever sort/filter changes, so stale cell edits
    # can't be re-applied to a different row after the view shifts.
    filter_sig = "|".join(
        [str(hide_done)] + [f"{k}={col_filters[k]}" for k in col_filters]
    )
    editor_key = f"phase0_tracker_editor::{sort_col}::{sort_dir}::{filter_sig}"

    edited = st.data_editor(
        visible_df,
        num_rows="dynamic",
        width="stretch",
        hide_index=True,
        key=editor_key,
        column_config={
            "Completed": st.column_config.CheckboxColumn("Done", default=False),
            "Priority": st.column_config.SelectboxColumn(
                "Priority", options=["High", "Medium", "Low"]
            ),
            "Origin": st.column_config.SelectboxColumn(
                "Origin", options=["Email", "MS Teams", "Meeting", "Chat", "Other"]
            ),
            "Action": st.column_config.TextColumn("Action", width="large"),
            "Email Thread": st.column_config.TextColumn("Email Thread"),
            "Owner": st.column_config.TextColumn("Owner"),
            "Date Assigned": st.column_config.TextColumn("Date Assigned"),
            "Date Completed": st.column_config.TextColumn(
                "Date Completed", help="Auto-filled when you mark an item done and save."
            ),
            "Next Step": st.column_config.TextColumn("Next Step", width="medium"),
            "Suggested Response": st.column_config.TextColumn(
                "Suggested Response", width="large"
            ),
        },
    )

    if st.button("💾 Save Tracker", type="primary"):
        today = datetime.today().strftime("%Y-%m-%d")
        rows = []
        # Visible (possibly edited) rows first, then filtered-out rows untouched.
        preserved = hidden_df.fillna("").to_dict("records")
        for r in edited.fillna("").to_dict("records") + preserved:
            r["Completed"] = bool(r.get("Completed"))
            # Stamp / clear the completion date based on the checkbox.
            if r["Completed"] and not str(r.get("Date Completed", "")).strip():
                r["Date Completed"] = today
            elif not r["Completed"]:
                r["Date Completed"] = ""
            rows.append(r)
        store.save_json(config.EMAIL_ACTIONS_KEY, rows)
        msg = f"Saved {len(rows)} item(s)."
        if preserved:
            msg += f" ({len(preserved)} filtered-out row(s) preserved.)"
        st.success(msg)
        st.rerun()

# --------------------------------------------------------------------------- #
# 3. Projects & Themes
# --------------------------------------------------------------------------- #
with tab_projects:
    st.subheader("Projects & Themes")
    st.caption(
        "Emails are tagged with a project as they're analyzed. Pick a project to "
        "synthesize its common themes, the type of work requested, and whether "
        "work is repetitive vs. prior emails."
    )

    # --- Register rollup ------------------------------------------------- #
    # The PROJECT register, counted by linked email. Shown above the legacy
    # label view rather than replacing it: the labels still cover every
    # historical email, and cutting over would blank most of this tab.
    register_counts = project_db.link_counts("confirmed")
    register_rows = [
        {
            "ID": proj["project_id"],
            "Project": proj["name"],
            "Emails": register_counts.get(proj["project_id"], {}).get("email", 0),
            "Keywords": proj["keywords"] or "—",
        }
        for proj in project_db.list_projects()
    ]
    if register_rows:
        st.markdown("**Project register**")
        st.dataframe(
            pd.DataFrame(sorted(register_rows, key=lambda r: -r["Emails"])),
            width="stretch",
            hide_index=True,
        )
        pending_n = len(project_db.pending_proposals())
        if pending_n:
            st.caption(
                f"{pending_n} project link(s) awaiting approval — review them in "
                f"**Project Management → Proposals**."
            )
        st.caption(
            "Managed in **Project Management**. Emails link here as they are "
            "analyzed, whenever their label matches a registered project."
        )
        st.divider()
    else:
        st.info(
            "No projects in the register yet — create some in **Project "
            "Management** and analyzed email will start rolling up here."
        )
        st.divider()

    st.markdown("**Legacy free-text labels**")
    counts = email_db.get_project_counts()
    if not counts:
        st.info("No projects yet — analyze emails in the **🔍 Identify Actions** tab first.")
    else:
        # --- Consolidate fragmented names --------------------------------- #
        known = email_db.list_known_projects()
        if known:
            with st.expander("✏️ Rename / merge project names"):
                c1, c2 = st.columns(2)
                old_name = c1.selectbox("Rename this project", known, key="proj_rename_old")
                new_name = c2.text_input(
                    "To (existing or new name)", key="proj_rename_new"
                ).strip()
                if st.button("Apply rename / merge", disabled=not new_name):
                    email_db.rename_project(old_name, new_name)
                    st.success(f"Renamed '{old_name}' → '{new_name}'.")
                    st.rerun()

        # --- Project list with counts ------------------------------------- #
        st.dataframe(
            pd.DataFrame(counts, columns=["Project", "# Emails"]),
            width="stretch",
            hide_index=True,
        )

        # --- Synthesize themes for a project ------------------------------ #
        project_names = [p for p, _ in counts]
        chosen = st.selectbox("Project to analyze", project_names, key="proj_choice")
        if st.button("🧠 Synthesize themes", type="primary"):
            emails = email_db.get_emails_for_project(chosen)
            if not emails:
                st.warning("No emails recorded for this project.")
            else:
                emails_block = "\n".join(
                    f"- {subj} · {(recv or '')[:10]} · {(summ or '').strip()}"
                    for subj, recv, summ, _mid in emails
                )
                prior_dict, _ = email_db.get_project_themes(chosen)
                prior_themes = json.dumps(prior_dict) if prior_dict else ""
                try:
                    with st.spinner(f"Synthesizing themes for {chosen}…"):
                        text = P.generate(
                            client,
                            P.build_project_themes_prompt(chosen, emails_block, prior_themes),
                            temperature=0.2,
                        )
                        result = _parse_llm_json(text)
                    email_db.upsert_project_themes(chosen, result)
                    st.rerun()
                except Exception as e:
                    st.warning(f"Could not synthesize themes: {e}")

        # --- Display saved themes for the chosen project ------------------ #
        themes, last_synth = email_db.get_project_themes(chosen)
        if not themes:
            st.info("No themes synthesized yet for this project — click **Synthesize themes**.")
        else:
            st.divider()
            if themes.get("project_summary"):
                st.markdown(f"**{chosen}** — {themes['project_summary']}")
            if last_synth:
                st.caption(f"Last synthesized: {str(last_synth)[:19]}")
            for theme in themes.get("themes", []):
                with st.container(border=True):
                    note = (theme.get("repetition_note") or "").lower()
                    badge = "🔁 Repetitive" if ("repeat" in note or "recurr" in note) else "🆕 New"
                    head, tag = st.columns([4, 1])
                    head.markdown(f"**{theme.get('theme', '(theme)')}**")
                    tag.markdown(badge)
                    st.caption(f"Type of work: {theme.get('type_of_work', '—')}")
                    examples = theme.get("example_emails") or []
                    if examples:
                        st.caption("Examples: " + "; ".join(str(e) for e in examples))
                    st.markdown(theme.get("repetition_note", ""))

# --------------------------------------------------------------------------- #
# 4. Shipments
# --------------------------------------------------------------------------- #
SHIP_CARRIERS = ["FedEx", "UPS", "DHL", "Other"]
SHIP_COLS = [
    "Associated Case", "Associated SPR", "SPR ↗", "Sender", "Date Sent", "Contents",
    "Carrier", "Tracking Number", "Track ↗", "Shipping Status", "Last Updated",
]


def _norm_carrier(c):
    return {"fedex": "FedEx", "ups": "UPS", "dhl": "DHL"}.get((c or "").strip().lower(), "Other")


def _refresh_shipment_statuses(pairs):
    """pairs: [(tracking_number, carrier), ...]. Best-effort status refresh."""
    for tn, carrier in pairs:
        text, _url = fetch_carrier_tracking_text(carrier, tn)
        status = "Unknown — see tracking link"
        if text and text.strip():
            try:
                out = _parse_llm_json(
                    P.generate(
                        client,
                        P.build_shipping_status_prompt(carrier, tn, text),
                        temperature=0.1,
                    )
                )
                status = (out.get("status") or status).strip()
            except Exception:
                pass
        email_db.update_shipment_status(tn, status)


with tab_ship:
    st.subheader("Shipping Sample Tracker")
    st.caption(
        "Shipments detected in analyzed emails. Status refreshes on open (if older "
        "than a day) and on demand. Carrier pages are JavaScript-heavy, so use the "
        "Track ↗ link if a status reads 'Unknown'."
    )

    shipments = email_db.list_shipments()

    # Auto-refresh stale statuses once per session.
    if shipments and not st.session_state.get("ship_autorefresh_done"):
        stale = email_db.shipments_needing_refresh(config.SHIPMENT_STATUS_MAX_AGE_HOURS)
        if stale:
            with st.spinner(f"Refreshing {len(stale)} shipment status(es)…"):
                _refresh_shipment_statuses(stale)
            shipments = email_db.list_shipments()
        st.session_state["ship_autorefresh_done"] = True

    if not shipments:
        st.info(
            "No shipments yet — analyze emails that mention a shipment in the "
            "🔍 Identify Actions tab."
        )
    else:
        if st.button("🔄 Refresh statuses"):
            with st.spinner("Refreshing all shipment statuses…"):
                _refresh_shipment_statuses(
                    [(s["tracking_number"], s.get("carrier", "")) for s in shipments]
                )
            st.rerun()

        jira = config.JIRA_BASE_URL.rstrip("/")
        rows = []
        for s in shipments:
            spr_first = (s.get("associated_spr") or "").split(",")[0].strip()
            rows.append(
                {
                    "Associated Case": s.get("associated_case", ""),
                    "Associated SPR": s.get("associated_spr", ""),
                    "SPR ↗": f"{jira}/browse/{spr_first}" if spr_first else "",
                    "Sender": s.get("sender", ""),
                    "Date Sent": s.get("date_sent", ""),
                    "Contents": s.get("contents", ""),
                    "Carrier": _norm_carrier(s.get("carrier", "")),
                    "Tracking Number": s.get("tracking_number", ""),
                    "Track ↗": config.carrier_tracking_url(
                        s.get("carrier", ""), s.get("tracking_number", "")
                    ),
                    "Shipping Status": s.get("shipping_status", ""),
                    "Last Updated": (str(s.get("last_updated") or ""))[:19],
                }
            )
        df = pd.DataFrame(rows, columns=SHIP_COLS).fillna("").astype(str)

        edited = st.data_editor(
            df,
            width="stretch",
            hide_index=True,
            key="ship_editor",
            column_config={
                "Associated Case": st.column_config.TextColumn("Associated Case"),
                "Associated SPR": st.column_config.TextColumn("Associated SPR"),
                "SPR ↗": st.column_config.LinkColumn(
                    "SPR ↗", display_text=r"/browse/(.+)$", disabled=True
                ),
                "Sender": st.column_config.TextColumn("Sender"),
                "Date Sent": st.column_config.TextColumn("Date Sent"),
                "Contents": st.column_config.TextColumn("Contents", width="large"),
                "Carrier": st.column_config.SelectboxColumn("Carrier", options=SHIP_CARRIERS),
                "Tracking Number": st.column_config.TextColumn("Tracking Number", disabled=True),
                "Track ↗": st.column_config.LinkColumn(
                    "Track ↗", display_text="Track", disabled=True
                ),
                "Shipping Status": st.column_config.TextColumn("Shipping Status", width="medium"),
                "Last Updated": st.column_config.TextColumn("Last Updated", disabled=True),
            },
        )

        if st.button("💾 Save", type="primary", key="ship_save"):
            for r in edited.fillna("").to_dict("records"):
                email_db.save_shipment_row(
                    {
                        "tracking_number": r.get("Tracking Number", ""),
                        "carrier": r.get("Carrier", ""),
                        "associated_case": r.get("Associated Case", ""),
                        "associated_spr": r.get("Associated SPR", ""),
                        "sender": r.get("Sender", ""),
                        "date_sent": r.get("Date Sent", ""),
                        "contents": r.get("Contents", ""),
                        "shipping_status": r.get("Shipping Status", ""),
                    }
                )
            st.success("Saved.")
            st.rerun()
