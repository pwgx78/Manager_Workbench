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

import plotly.graph_objects as go

import config
import store
import email_db
import email_volume as EV
import llm_prompts as P
import project_db
from api_helpers import (
    fetch_recent_inbox,
    clean_html,
    fetch_carrier_tracking_text,
    fetch_mail_volume,
    fetch_mailbox_messages,
)

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

# How many active projects are offered to the model per email. The plan's ~8:
# big enough that the right project is nearly always present, small enough that
# prompt cost stays flat however large the register grows.
SHORTLIST_SIZE = 8

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


# --------------------------------------------------------------------------- #
# Mail volume chart
#
# Colors are categorical slots 1 and 2 (blue / orange) from the validated
# palette, with steps chosen per mode rather than flipped: dark mode is its own
# selection. The pair clears every gate in both modes — worst CVD Delta E 24.7
# light / 26.8 dark against an 8.0 target, normal-vision 33.6 / 31.8 against a
# 15.0 floor, and both above 3:1 contrast on their surface.
# --------------------------------------------------------------------------- #
SERIES_COLORS = {
    "light": {EV.RECEIVED: "#2a78d6", EV.SENT: "#eb6834"},
    "dark": {EV.RECEIVED: "#3987e5", EV.SENT: "#d95926"},
}
SERIES_LABEL = {EV.RECEIVED: "Received", EV.SENT: "Sent"}
# Grid and axis text: one step off the surface, recessive, never the data color.
CHROME = {
    "light": {"grid": "#e6e5e2", "text": "#52514e"},
    "dark": {"grid": "#33322f", "text": "#c3c2b7"},
}


def _theme_mode():
    """'light' or 'dark' for the viewer's Streamlit theme. Defaults to light
    when the theme is unknown (bare/headless runs report None)."""
    try:
        return "dark" if (st.context.theme or {}).get("type") == "dark" else "light"
    except Exception:
        return "light"


def _volume_figure(frame, mode, grain):
    """Clustered column chart of received vs sent per time bucket.

    One chart with two series rather than two charts: both are counts of email,
    so they share a scale honestly and the comparison is the point. (Two
    different scales would have to be two charts — a second y-axis would invent
    a relationship that isn't in the data.)

    The x axis is categorical, not temporal: clustered columns want evenly
    spaced slots, and a temporal axis would size February's column differently
    from January's.
    """
    colors = SERIES_COLORS[mode]
    chrome = CHROME[mode]
    buckets = list(dict.fromkeys(frame["bucket_label"]))

    figure = go.Figure()
    for direction in EV.DIRECTIONS:
        part = frame[frame["direction"] == direction]
        figure.add_bar(
            x=part["bucket_label"],
            y=part["count"],
            name=SERIES_LABEL[direction],
            marker=dict(color=colors[direction], cornerradius=4),
            # The tooltip carries the UNABBREVIATED bucket, so the terse axis
            # tick never leaves a value ambiguous.
            customdata=part["bucket_label"],
            hovertemplate=(
                f"<b>{SERIES_LABEL[direction]}</b><br>"
                "%{customdata}<br>%{y:,} email(s)<extra></extra>"
            ),
        )

    # Keep columns slim instead of letting them fill the slot. Plotly gaps are
    # fractions of the slot, not pixels, so cap the bar at roughly 24px by
    # solving for the gap at an assumed ~720px plot width: with two bars per
    # slot, bar_px = slot_px * (1 - bargap) / 2. Few buckets therefore get a
    # wide gap (air) rather than two fat blocks.
    slot_px = 720 / max(len(buckets), 1)
    bargap = 1 - (2 * 24) / slot_px if slot_px > 0 else 0.3
    figure.update_layout(
        barmode="group",
        bargap=min(0.85, max(0.2, bargap)),
        bargroupgap=0.08,  # the surface gap between the two clustered columns
        height=420,
        margin=dict(l=8, r=8, t=8, b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=chrome["text"]),
        legend=dict(
            orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0,
            title_text="",
        ),
        hovermode="closest",
    )
    # Terse tick text ("03 Aug") against the full bucket key underneath: the
    # filter row above already states the year, and full ISO dates rotated -45
    # were eating a quarter of the figure height. The full form stays in the
    # tooltip and the table.
    ticks = frame.drop_duplicates("bucket_label")
    figure.update_xaxes(
        type="category",
        categoryorder="array",
        categoryarray=buckets,
        tickmode="array",
        tickvals=ticks["bucket_label"],
        ticktext=ticks["tick_label"],
        showgrid=False,
        showline=True,
        linecolor=chrome["grid"],
        linewidth=1,
        tickangle=-45 if len(buckets) > 8 else 0,
        title_text="",
    )
    figure.update_yaxes(
        showgrid=True,
        gridcolor=chrome["grid"],
        gridwidth=1,
        griddash="solid",
        zeroline=False,
        rangemode="tozero",
        separatethousands=True,
        title_text="",
    )
    return figure


tab_identify, tab_tracker, tab_projects, tab_volume, tab_ship = st.tabs(
    [
        "🔍 Identify Actions",
        "📋 Email Action Tracker",
        "🗂️ Projects & Themes",
        "📈 Volume",
        "📦 Shipments",
    ]
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
                n_cache = n_fresh = n_failed = n_proposed = 0
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
                        # Keyword pre-filter, then the SAME call that already
                        # runs. No second pass, so the register costs zero extra
                        # LLM calls; the shortlist keeps prompt size flat as the
                        # register grows instead of scaling with project count.
                        shortlist = project_db.shortlist_for_text(
                            f"{subject}\n{body}", limit=SHORTLIST_SIZE
                        )
                        try:
                            text = P.generate(
                                client,
                                P.build_email_action_prompt(
                                    content,
                                    context,
                                    known_projects=known_projects,
                                    team_context=team_ctx,
                                    project_candidates=project_db.candidate_block(
                                        shortlist
                                    ),
                                ),
                                temperature=0.2,
                            )
                            out = _parse_llm_json(text)
                            # Remember which ids were OFFERED, so the validation
                            # allowlist survives into the cached analysis. A
                            # cached result replayed later must be checked
                            # against the same shortlist it was produced from,
                            # not against whatever the register holds today.
                            out["_project_shortlist"] = [
                                p["project_id"] for p in shortlist
                            ]
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

                    # Proposals from the model's ranking. Runs on the cached
                    # branch too, so a re-run replays proposals for free
                    # instead of re-billing the email — decision 10 says an
                    # analysis is never recomputed, and this respects that.
                    #
                    # accept_candidates is the boundary where an invented id
                    # would otherwise become a row: the offered shortlist is
                    # the allowlist, and anything outside it is dropped.
                    accepted = project_db.accept_candidates(
                        out.get("project_candidates"),
                        out.get("_project_shortlist") or [],
                    )
                    if accepted:
                        n_proposed += project_db.propose_candidates(
                            "email", mid, accepted
                        )

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
                if n_proposed:
                    msg_bits.append(f"{n_proposed} project proposal(s) to approve")
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

    # --- Clear the whole register ----------------------------------------- #
    # Deliberately ALL rows, not the filtered view: "clear the register" meaning
    # "clear the seven rows you can currently see" would be a nasty surprise.
    # The save path above is the one that respects filters.
    with st.expander("🗑️ Clear action register"):
        if df.empty:
            st.caption("The register is already empty.")
        else:
            st.caption(
                f"Deletes all **{len(df)}** item(s) from the tracker, including "
                f"any hidden by the filters above. This cannot be undone — the "
                f"tracker is hand-curated, so download a copy first if in doubt."
            )
            st.download_button(
                "⬇️ Download a copy (CSV)",
                data=df.to_csv(index=False).encode("utf-8"),
                file_name=f"email_action_tracker_{datetime.today():%Y-%m-%d}.csv",
                mime="text/csv",
                key="phase0_tracker_export",
            )
            st.caption(
                "Conversation dispositions are separate and are left alone, so a "
                "thread already marked *Add to Tracker* stays marked. Re-select "
                "that disposition to file its action again."
            )
            confirmed = st.checkbox(
                f"Yes, delete all {len(df)} item(s)",
                key="phase0_tracker_clear_confirm",
            )
            # `and confirmed` is not redundant with `disabled=`. The disabled
            # flag only stops the click in the browser; the handler must refuse
            # on its own too, or the guard on an irreversible delete is purely
            # presentational.
            if (
                st.button(
                    "Clear register",
                    disabled=not confirmed,
                    key="phase0_tracker_clear",
                )
                and confirmed
            ):
                store.save_json(config.EMAIL_ACTIONS_KEY, [])
                st.success(f"Cleared {len(df)} item(s) from the tracker.")
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


# --------------------------------------------------------------------------- #
# 4. Volume
# --------------------------------------------------------------------------- #
with tab_volume:
    st.subheader("Mail volume over time")
    st.caption(
        "How much arrives versus how much goes out. **Received** counts your "
        "whole mailbox — Inbox, Archive, Deleted Items and any folder your "
        "rules file into, however deeply nested — so cloud-side rules don't "
        "hide mail from the count. Junk, Drafts and Teams chat history are not "
        "counted. **Sent** is Sent Items."
    )

    _tz = "UTC"
    try:
        _tz = st.context.timezone or "UTC"
    except Exception:
        pass
    _mode = _theme_mode()

    # One filter row above everything it scopes, so the tiles, the chart and the
    # table all describe the same slice.
    f1, f2, f3 = st.columns(3)
    vol_start = f1.date_input(
        "From", value=datetime.today().date() - timedelta(days=29), key="vol_start"
    )
    vol_end = f2.date_input("To", value=datetime.today().date(), key="vol_end")
    grain = f3.selectbox(
        "Group by",
        list(EV.GRAINS),
        index=list(EV.GRAINS).index(EV.DEFAULT_GRAIN),
        key="vol_grain",
    )
    # `To` is inclusive to the user; the data layer takes an exclusive end.
    vol_end_exclusive = vol_end + timedelta(days=1)

    cov = EV.coverage()
    stored = cov[EV.RECEIVED]["count"] + cov[EV.SENT]["count"]

    # Rows gathered before received mail was read mailbox-wide undercount, and
    # coverage() cannot tell which definition produced which row — so blending
    # them would quietly mix an accurate month with a short one. Rebuilding is
    # just a Graph pull, so offer the unambiguous fix.
    if stored and not EV.scope_is_current():
        st.warning(
            "This stored data was gathered when only the **Inbox** counted as "
            "received, so anything your rules filed elsewhere is missing and "
            "the received numbers are too low. Clear it and fetch again to "
            "correct them."
        )
        if st.button("Clear and start again", key="vol_rescope"):
            EV.clear()
            st.rerun()

    fetch_col, stored_col = st.columns([1, 3])
    if fetch_col.button("🔄 Fetch this range", type="primary", key="vol_fetch"):
        if vol_start > vol_end:
            st.error("`From` is after `To` — nothing to fetch.")
        else:
            progress = st.empty()
            window = (
                f"{vol_start.isoformat()}T00:00:00Z",
                f"{vol_end_exclusive.isoformat()}T00:00:00Z",
            )
            try:
                progress.caption("Identifying your mail folders…")
                ids = EV.folder_ids()

                # Received: the whole mailbox, classified by parent folder.
                progress.caption("Fetching received mail…")
                inbound = fetch_mailbox_messages(
                    EV.TIMESTAMP_FIELD[EV.RECEIVED],
                    *window,
                    page_cb=lambda n: progress.caption(
                        f"Fetching received mail… {n:,} so far"
                    ),
                )
                n_recv, n_ignored, _n_sent_skipped = EV.upsert_mailbox_messages(
                    inbound, ids
                )

                # Sent: its own pass, because only Sent Items carries the
                # sentDateTime that a sent message should be dated by.
                progress.caption("Fetching sent mail…")
                outbound = fetch_mail_volume(
                    EV.SENT_FOLDER,
                    EV.TIMESTAMP_FIELD[EV.SENT],
                    *window,
                    page_cb=lambda n: progress.caption(
                        f"Fetching sent mail… {n:,} so far"
                    ),
                )
                n_sent = EV.upsert_messages(outbound, EV.SENT)

                EV.mark_scope()
                progress.empty()
                st.success(
                    f"Stored {n_recv:,} received and {n_sent:,} sent."
                    + (
                        f" Skipped {n_ignored:,} in junk, drafts and chat history."
                        if n_ignored
                        else ""
                    )
                    + " Re-fetching is safe."
                )
                st.rerun()
            except Exception as e:
                progress.empty()
                st.error(f"Could not fetch mail volume: {e}")

    if stored:
        with stored_col.expander("Stored data"):
            for direction in EV.DIRECTIONS:
                info = cov[direction]
                st.caption(
                    f"**{SERIES_LABEL[direction]}**: {info['count']:,} message(s)"
                    + (
                        f" · {(info['first'] or '')[:10]} to {(info['last'] or '')[:10]}"
                        if info["count"]
                        else ""
                    )
                )
            st.caption(
                "Counts are read from this stored copy rather than from Outlook, "
                "so the chart is instant. Fetch again to extend or refresh a "
                "range — it is idempotent, so nothing double-counts."
            )
            _ids = EV.cached_folder_ids()
            if _ids:
                st.caption(
                    "Counted as received: the whole mailbox except junk, drafts, "
                    "outbox and chat history — including "
                    + ", ".join(EV.counted_folder_names(_ids))
                    + ". Deleted Items is purged by retention over time, so the "
                    "deep past will drift downward."
                )
            if st.button("Clear stored volume data", key="vol_clear"):
                EV.clear()
                st.rerun()

    if not stored:
        st.info(
            "No volume data yet — pick a range and press **Fetch this range**. "
            "Only timestamps, subjects and senders are pulled, never message "
            "bodies, so a wide range is quick."
        )
    else:
        frame = None
        try:
            frame = EV.series(
                start=vol_start.isoformat(),
                end=vol_end_exclusive.isoformat(),
                grain=grain,
                tz=_tz,
            )
        except EV.TooManyBuckets as e:
            st.warning(str(e))

        if frame is not None and frame.empty:
            st.info(
                "Nothing stored for that range yet — press **Fetch this range**."
            )
        elif frame is not None:
            counts = EV.totals(
                start=vol_start.isoformat(), end=vol_end_exclusive.isoformat(), tz=_tz
            )
            m1, m2, m3 = st.columns(3)
            m1.metric("Received", f"{counts[EV.RECEIVED]:,}")
            m2.metric("Sent", f"{counts[EV.SENT]:,}")
            m3.metric(
                "Received per sent",
                f"{counts[EV.RECEIVED] / counts[EV.SENT]:.1f}×"
                if counts[EV.SENT]
                else "—",
                help="How many arrive for every one you send.",
            )

            n_buckets = frame["bucket_label"].nunique()
            if n_buckets > EV.BUSY_BUCKETS:
                st.caption(
                    f"{n_buckets:,} columns — readable, but crowded. A coarser "
                    f"**Group by** will make the shape clearer."
                )
            st.plotly_chart(
                _volume_figure(frame, _mode, grain),
                width="stretch",
                key=f"vol_chart_{grain}_{n_buckets}",
            )
            st.caption(f"Times shown in **{_tz}**.")

            # Table view: every value the chart encodes, reachable without
            # relying on hover or on telling two colours apart.
            with st.expander("🔢 Table view"):
                wide = (
                    frame.pivot_table(
                        index="bucket_label",
                        columns="direction",
                        values="count",
                        aggfunc="sum",
                        fill_value=0,
                    )
                    .rename(columns=SERIES_LABEL)
                    .reset_index()
                    .rename(columns={"bucket_label": grain})
                )
                for column in SERIES_LABEL.values():
                    if column not in wide.columns:
                        wide[column] = 0
                wide["Total"] = wide[list(SERIES_LABEL.values())].sum(axis=1)
                st.dataframe(wide, width="stretch", hide_index=True)
                st.download_button(
                    "⬇️ Download CSV",
                    data=wide.to_csv(index=False).encode("utf-8"),
                    file_name=f"mail_volume_by_{grain.lower()}.csv",
                    mime="text/csv",
                    key="vol_export",
                )


# --------------------------------------------------------------------------- #
# 5. Shipments
# --------------------------------------------------------------------------- #
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
