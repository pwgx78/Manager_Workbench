"""
Phase 3 — ECRT Jira State Tracking & Analysis Agent.

Parses a Jira ticket's description, timestamped comment history, and selected
attachments to determine the TRUE engineering phase, root-cause / re-design
status, forward-looking milestones, schedule slips, and the conclusion status of
action items — then renders an interactive color-coded milestone timeline plus a
textual digest. Standard Jira status fields don't capture this nuance, so the
analysis supplements/overrides them.

Flow: list tickets via JQL/saved-filter → check off multiple → analyze → per
ticket, review the digest/timeline and optionally fold selected attachments into
a deeper re-analysis. Results are cached per ticket (keyed by Jira 'updated').
"""
import textwrap

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import config
import jira_analysis as JA
import jira_db
from api_helpers import (
    search_jira,
    download_jira_attachment,
    extract_text_from_bytes,
)

st.header("📊 Jira State Tracker")
st.caption(
    "Extract the true engineering state of ECRT tickets — phase, root cause, "
    "schedule slips, and action-item status — with an interactive milestone timeline."
)

client = st.session_state.get("gemini_client")
if client is None:
    st.error("Gemini client not initialized. Open the app from the main page (app.py).")
    st.stop()

# Generous safety guard on download size only (text extraction is cheap, so large
# files are still processed — we just avoid pathological multi-hundred-MB pulls).
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024
# Cap the extracted text per attachment fed to the model (keeps tokens bounded).
ATTACH_TEXT_CHARS = 15000

PHASE_COLORS = {"past": "#2e9e4f", "future": "#3b7dd8", "slipped": "#d6336c"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _analyze(key, force=False, selected_attachments=None):
    """Strip text from any selected attachments, then run the shared (cached)
    Jira analysis and return the analysis dict."""
    attachments_text = ""
    for att in selected_attachments or []:
        if att.get("size", 0) and att["size"] > MAX_ATTACHMENT_BYTES:
            st.warning(
                f"Skipped {att['filename']} — exceeds the "
                f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB download limit."
            )
            continue
        data = download_jira_attachment(att["content_url"])
        txt = extract_text_from_bytes(att["filename"], data)
        if txt and txt.strip():
            attachments_text += f"\n\n--- {att['filename']} ---\n{txt[:ATTACH_TEXT_CHARS]}"
        else:
            st.warning(
                f"No extractable text from {att['filename']} "
                "(unsupported type or image-only / scanned) — skipped."
            )

    ticket, analysis = JA.get_or_analyze_ticket(
        client, key, force=force, attachments_text=attachments_text
    )
    st.session_state.setdefault("jira_tickets", {})[key] = ticket
    return analysis


def _render_phase_stepper(current):
    cols = st.columns(len(config.JIRA_PHASES))
    cur = (current or "").lower()
    for col, ph in zip(cols, config.JIRA_PHASES):
        active = ph.lower() == cur or (cur and (cur in ph.lower() or ph.lower() in cur))
        if active:
            col.markdown(f"**🟢 {ph}**")
        else:
            col.caption(ph)


def _render_timeline(milestones, chart_key):
    rows, undated = [], []
    for m in milestones or []:
        d = pd.to_datetime(m.get("date", ""), errors="coerce")
        if pd.isna(d):
            undated.append(m)
        else:
            rows.append(
                {
                    "date": d,
                    "type": (m.get("type") or "future").lower(),
                    "label": m.get("label", ""),
                    "source": m.get("source", ""),
                }
            )
    if rows:
        # Everything on ONE horizontal line (y=0), colored by type. Labels are
        # draggable annotations (Plotly marker text isn't draggable) so the user
        # can pull apart any that overlap on the initial render.
        rows.sort(key=lambda r: r["date"])

        fig = go.Figure()
        for typ in ["past", "future", "slipped"]:
            sub = [r for r in rows if r["type"] == typ]
            if not sub:
                continue
            fig.add_trace(
                go.Scatter(
                    x=[r["date"] for r in sub],
                    y=[0] * len(sub),
                    mode="markers",
                    marker=dict(size=14, color=PHASE_COLORS.get(typ, "#888")),
                    name=typ.capitalize(),
                    customdata=[[r["label"], r["source"]] for r in sub],
                    hovertemplate=(
                        "<b>%{customdata[0]}</b><br>%{x|%Y-%m-%d}<br>%{customdata[1]}<extra></extra>"
                    ),
                )
            )

        # Visible baseline for the timeline.
        fig.add_hline(y=0, line_width=2, line_color="#888888", layer="below")

        # Staggered initial heights so adjacent labels don't all stack at once.
        ay_cycle = [-45, -80, -115, -150]
        for i, r in enumerate(rows):
            label = r["label"] or "(milestone)"
            if len(label) > 48:
                label = label[:47] + "…"
            fig.add_annotation(
                x=r["date"],
                y=0,
                text=label,
                showarrow=True,
                arrowhead=2,
                arrowwidth=1,
                ax=0,
                ay=ay_cycle[i % len(ay_cycle)],
                font=dict(size=11),
                bgcolor="rgba(255,255,255,0.85)",
                bordercolor=PHASE_COLORS.get(r["type"], "#888"),
                borderwidth=1,
            )

        fig.update_layout(
            height=460,
            margin=dict(l=10, r=20, t=60, b=10),
            xaxis_title="Date",
            yaxis_title="",
            legend_title="Milestone",
        )
        fig.update_yaxes(visible=False, range=[-1.5, 1.5])
        st.plotly_chart(
            fig,
            use_container_width=True,
            key=chart_key,
            config={"edits": {"annotationTail": True, "annotationPosition": True}},
        )
        st.caption("Tip: drag any milestone label to reposition it if labels overlap.")
    else:
        st.caption("No dated milestones to plot.")

    if undated:
        with st.expander(f"Undated milestones ({len(undated)})"):
            for m in undated:
                st.markdown(f"- **{m.get('label', '')}** — {m.get('source', '')}")


# Fishbone (Ishikawa) categories — the manufacturing "6 M's", 3 ribs above the
# spine and 3 below.
FISHBONE_TOP = ["Manpower", "Machine", "Material"]
FISHBONE_BOTTOM = ["Method", "Measurement", "Maintenance"]


def _render_fishbone(fishbone, fallback_problem, chart_key):
    """Draw a fishbone / Ishikawa diagram: spine + head (problem) + six 6-M ribs.
    Categories with no causes are labelled 'Not Investigated'."""
    fishbone = fishbone or {}
    problem = (fishbone.get("problem") or fallback_problem or "Problem").strip()
    categories = fishbone.get("categories") or {}

    fig = go.Figure()
    # Spine + fish head (problem box on the right).
    fig.add_shape(type="line", x0=0.6, y0=0, x1=8.4, y1=0, line=dict(color="#333333", width=3))
    fig.add_shape(
        type="rect", x0=8.5, y0=-1.2, x1=11.0, y1=1.2,
        line=dict(color="#c0392b", width=2), fillcolor="rgba(192,57,43,0.08)",
    )
    # Wrap the problem text so it stays inside the red head box (≈2.5 x-units
    # wide) instead of overflowing on a single line. Cap the number of lines so
    # a very long problem statement can't grow taller than the box; the full
    # text is always available on hover (see the head hover marker below).
    head_lines = textwrap.wrap(problem, width=26) or ["Problem"]
    if len(head_lines) > 6:
        head_lines = head_lines[:6]
        head_lines[-1] = head_lines[-1][:23].rstrip() + "…"
    head = "<br>".join(head_lines)
    fig.add_annotation(
        x=9.75, y=0, text=f"<b>Problem</b><br>{head}", showarrow=False,
        font=dict(size=10), align="center",
    )
    # Invisible hover marker over the head box → full, untruncated problem text.
    fig.add_trace(go.Scatter(
        x=[9.75], y=[0], mode="markers",
        marker=dict(size=60, opacity=0, symbol="square"),
        hoverinfo="text", hovertext="<br>".join(textwrap.wrap(problem, width=60)),
        showlegend=False,
    ))

    anchors = [2.6, 4.8, 7.0]  # where each rib meets the spine

    def _draw(cat, anchor_x, top):
        sign = 1 if top else -1
        tip_x, tip_y = anchor_x - 1.4, sign * 2.4
        fig.add_shape(
            type="line", x0=anchor_x, y0=0, x1=tip_x, y1=tip_y,
            line=dict(color="#555555", width=2),
        )
        causes = categories.get(cat) or []
        investigated = bool(causes)
        box = "#2e7d32" if investigated else "#9e9e9e"
        fig.add_annotation(
            x=tip_x, y=tip_y + sign * 0.12, text=f"<b>{cat}</b>", showarrow=False,
            font=dict(size=12, color="white"), bgcolor=box, bordercolor=box, borderpad=4,
            yanchor="bottom" if top else "top",
        )
        if investigated:
            shown = [c if len(c) <= 40 else c[:39] + "…" for c in causes[:5]]
            extra = len(causes) - len(shown)
            text = "<br>".join(f"• {c}" for c in shown)
            if extra > 0:
                text += f"<br>… (+{extra} more)"
            color = "#222222"
            # Full, untruncated list for the hover tooltip — each cause wrapped
            # so long items don't run off the screen edge.
            hover = "<br>".join(
                "• " + "<br>&nbsp;&nbsp;".join(textwrap.wrap(c, width=60))
                for c in causes
            )
        else:
            text = "<i>Not Investigated</i>"
            color = "#9e9e9e"
            hover = f"<b>{cat}</b>: Not Investigated"
        y_text = tip_y + sign * 0.95
        fig.add_annotation(
            x=tip_x, y=y_text, text=text, showarrow=False,
            font=dict(size=10, color=color), align="left",
            xanchor="center", yanchor="bottom" if top else "top",
            bgcolor="rgba(255,255,255,0.85)",
        )
        # Invisible hover marker over the cause block → hover the box to expand
        # the (otherwise clipped) text into a full, untruncated tooltip.
        fig.add_trace(go.Scatter(
            x=[tip_x], y=[y_text + sign * 0.6], mode="markers",
            marker=dict(size=46, opacity=0, symbol="square"),
            hoverinfo="text", hovertext=f"<b>{cat}</b><br>{hover}",
            showlegend=False,
        ))

    for cat, ax in zip(FISHBONE_TOP, anchors):
        _draw(cat, ax, True)
    for cat, ax in zip(FISHBONE_BOTTOM, anchors):
        _draw(cat, ax, False)

    fig.update_xaxes(visible=False, range=[0, 11.4])
    fig.update_yaxes(visible=False, range=[-5, 5])
    fig.update_layout(
        height=540, margin=dict(l=10, r=10, t=20, b=10),
        showlegend=False, plot_bgcolor="white",
        hovermode="closest",
        hoverlabel=dict(bgcolor="white", bordercolor="#555555", align="left"),
    )
    st.plotly_chart(fig, use_container_width=True, key=chart_key)


def _render_analysis(key, analysis, ticket):
    _render_phase_stepper(analysis.get("current_phase"))
    st.caption(
        f"Phase rationale: {analysis.get('phase_rationale', '—')}  ·  "
        f"Root cause: **{analysis.get('root_cause_status', '?')}**  ·  "
        f"Re-design: **{analysis.get('redesign_status', '?')}**"
    )
    if analysis.get("root_cause_detail"):
        st.caption(f"Root cause detail: {analysis['root_cause_detail']}")

    row = jira_db.get_spr(key)
    if row and row.get("last_analyzed"):
        when = str(row["last_analyzed"])[:19]
        n_comments = len(row.get("seen_comments") or [])
        st.caption(
            f"🗄️ Cached summary last analyzed {when} · {n_comments} comment(s) "
            "folded in. Subsequent runs analyze only new activity (delta)."
        )

    st.markdown("#### 📅 Milestone timeline")
    _render_timeline(analysis.get("milestones"), chart_key=f"tl_{key}")

    td = analysis.get("target_dates", {}) or {}
    c1, c2, c3 = st.columns(3)
    c1.metric("Mfg change target", td.get("manufacturing_change") or "—")
    c2.metric("Customer delivery", td.get("customer_delivery") or "—")
    c3.metric("Overall completion", td.get("overall_completion") or "—")

    st.markdown("#### 📝 Digest")
    st.markdown(analysis.get("digest", "—"))

    st.markdown("#### 🧩 Root Cause (5 Whys)")
    rc = analysis.get("root_cause_5whys") or {}
    rc_status = (rc.get("status") or "").lower()
    whys = rc.get("whys") or []
    if rc.get("problem"):
        st.caption(f"Problem: {rc['problem']}")
    if whys:
        for i, w in enumerate(whys, 1):
            st.markdown(f"**Why {i}?** → {w}")
    else:
        st.caption("No 5-Whys chain could be derived.")
    if rc_status == "complete" and rc.get("root_cause"):
        st.success(f"✅ Root cause: {rc['root_cause']}")
    elif rc_status == "incomplete":
        st.warning("⚠️ Root cause incomplete — the 5-Whys chain stops short of a verified root cause.")
        if rc.get("root_cause"):
            st.caption(f"Best current hypothesis: {rc['root_cause']}")
    elif rc_status == "insufficient":
        st.warning("⚠️ Root cause insufficient — not enough information in the ticket to derive it.")
    elif rc.get("root_cause"):
        st.info(f"Root cause: {rc['root_cause']}")
    else:
        st.warning("⚠️ Root cause incomplete or insufficient.")

    st.markdown("#### 🐟 Root-Cause Fishbone")
    _render_fishbone(
        analysis.get("fishbone"),
        (ticket or {}).get("summary", ""),
        chart_key=f"fishbone_{key}",
    )

    slips = analysis.get("schedule_slips") or []
    st.markdown("#### ⏳ Schedule slips")
    if slips:
        st.dataframe(pd.DataFrame(slips), width="stretch", hide_index=True)
    else:
        st.caption("No schedule slips detected.")

    steps = analysis.get("open_next_steps") or []
    st.markdown("#### ✅ Open next steps")
    if steps:
        for s in steps:
            st.markdown(f"- {s}")
    else:
        st.caption("None stated.")

    audit = analysis.get("comment_action_audit") or []
    st.markdown("#### 🔎 Comment action audit")
    if audit:
        st.dataframe(pd.DataFrame(audit), width="stretch", hide_index=True)
    else:
        st.caption("No commitments found in comments.")

    att_actions = analysis.get("attachment_actions") or []
    if att_actions:
        st.markdown("#### 📎 Attachment actions")
        st.dataframe(pd.DataFrame(att_actions), width="stretch", hide_index=True)

    # --- Attachment selection + deeper re-analysis ------------------------ #
    st.markdown("#### 📎 Attachments")
    attachments = (ticket or {}).get("attachments", [])
    if not attachments:
        st.caption("No attachments on this ticket.")
    else:
        selected = []
        for att in attachments:
            size_kb = round((att.get("size") or 0) / 1024)
            if st.checkbox(
                f"{att['filename']} · {size_kb} KB",
                key=f"att_{key}_{att['id']}",
            ):
                selected.append(att)
        if st.button(
            "Analyze with selected attachments",
            key=f"reatt_{key}",
            disabled=not selected,
        ):
            try:
                with st.spinner(f"Analyzing {key} with {len(selected)} attachment(s)…"):
                    st.session_state["jira_results"][key] = _analyze(
                        key, selected_attachments=selected
                    )
                st.rerun()
            except Exception as e:
                st.error(f"{e}")

    if st.button("🔄 Re-analyze (comments only)", key=f"re_{key}"):
        try:
            with st.spinner(f"Re-analyzing {key}…"):
                st.session_state["jira_results"][key] = _analyze(key, force=True)
            st.rerun()
        except Exception as e:
            st.error(f"{e}")


# --------------------------------------------------------------------------- #
# 1. Select tickets
# --------------------------------------------------------------------------- #
st.subheader("1. Select tickets")
jql = st.text_input("JQL / saved filter", value='filter = "ECRT_ME_Disposition"')
if st.button("List tickets"):
    try:
        with st.spinner("Querying Jira…"):
            st.session_state["jira_list"] = search_jira(jql, max_results=100)
        st.session_state.pop("jira_results", None)
    except Exception as e:
        st.error(f"{e}")

jira_list = st.session_state.get("jira_list")
if jira_list:
    df = pd.DataFrame(jira_list)
    df.insert(0, "Analyze", False)
    edited = st.data_editor(
        df,
        hide_index=True,
        width="stretch",
        key="jira_select",
        column_config={"Analyze": st.column_config.CheckboxColumn("Analyze")},
        disabled=["key", "summary", "status", "assignee", "updated"],
    )
    selected_keys = [r["key"] for r in edited.to_dict("records") if r.get("Analyze")]

    if st.button(
        f"Analyze selected ({len(selected_keys)})",
        type="primary",
        disabled=not selected_keys,
    ):
        results = st.session_state.setdefault("jira_results", {})
        progress = st.progress(0.0)
        for i, key in enumerate(selected_keys):
            try:
                with st.spinner(f"Analyzing {key}…"):
                    results[key] = _analyze(key)
            except Exception as e:
                st.error(f"{key}: {e}")
            progress.progress((i + 1) / len(selected_keys))
        st.session_state["jira_results"] = results

# --------------------------------------------------------------------------- #
# 2. Results
# --------------------------------------------------------------------------- #
results = st.session_state.get("jira_results")
if results:
    st.divider()
    st.subheader("2. Ticket analyses")
    tickets = st.session_state.get("jira_tickets", {})
    for n, (key, analysis) in enumerate(results.items()):
        ticket = tickets.get(key, {})
        title = f"{key} — {ticket.get('summary', '')}"
        with st.expander(title, expanded=(n == 0)):
            _render_analysis(key, analysis, ticket)
