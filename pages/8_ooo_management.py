"""
OoO Management — standalone tool to reconcile, summarize, and forecast team PTO.

Reconciles two sources that both arrive in the manager's mailbox/calendar:
  - System server emails: official HR approval requests (filtered by a known sender).
  - Calendar invites: informal OoO blocks (read from the manager's own calendar,
    attributed to a member via the organizer).

Cross-references the two at day granularity (so one multi-day invite reconciles
with several single-day requests and vice versa), shows a per-member match table,
summarizes upcoming PTO, and flags thin-coverage days.
"""
import json
from datetime import date, datetime, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

import config
import store
import ooo_logic
import llm_prompts as P
from api_helpers import fetch_ooo_requests, get_calendar_events

st.header("OoO & FTO Management")
st.caption(
    "Reconcile HR approval requests against calendar OoO invites, track upcoming "
    "PTO, and get thin-coverage alerts."
)

client = st.session_state.get("gemini_client")
if client is None:
    st.error("Gemini client not initialized. Open the app from the main page (app.py).")
    st.stop()

# Load fresh so roster/team edits in Settings are picked up immediately.
app_cfg = config.load_config()
roster = app_cfg.get("team_roster", config.DEFAULT_TEAM_ROSTER)
team_directory = config.team_context_block(app_cfg)

OOO_KEYWORDS = ("ooo", "pto", "vto", "vacation", "out of office", "holiday")
DEFAULT_SETTINGS = {"hr_sender": "", "coverage_threshold": 2}


def _settings():
    s = store.load_json(config.OOO_SETTINGS_KEY, default={})
    if not isinstance(s, dict):
        s = {}
    return {**DEFAULT_SETTINGS, **s}


def _parse_llm_json(text):
    raw = (text or "").strip()
    if "```json" in raw:
        raw = raw.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in raw:
        raw = raw.split("```", 1)[1].split("```", 1)[0]
    return json.loads(raw)


def _event_date(node):
    """Pull a YYYY-MM-DD from a Graph calendar start/end node (dateTime or date)."""
    return (node.get("dateTime") or node.get("date") or "")[:10]


settings = _settings()

# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
with st.expander("⚙️ Settings"):
    with st.form("ooo_settings"):
        hr_sender = st.text_input(
            "HR system email sender (name or address)",
            value=settings.get("hr_sender", ""),
            placeholder="e.g. notifications@workday.com or 'Workday'",
            help="Used to find the official approval-request emails. Leave blank to "
            "fall back to subject keywords (OOO/PTO/VTO).",
        )
        threshold = st.number_input(
            "Thin-coverage threshold (alert when this many members are out)",
            min_value=1,
            max_value=max(2, len(roster)),
            value=int(settings.get("coverage_threshold", 2)),
            step=1,
        )
        if st.form_submit_button("Save settings", type="primary"):
            store.save_json(
                config.OOO_SETTINGS_KEY,
                {"hr_sender": hr_sender.strip(), "coverage_threshold": int(threshold)},
            )
            st.success("Settings saved.")
            st.rerun()

# --------------------------------------------------------------------------- #
# Fetch + normalize
# --------------------------------------------------------------------------- #
st.subheader("Fetch window")
default_range = (date.today() - timedelta(days=7), date.today() + timedelta(days=30))
win = st.date_input("Look-back → look-ahead", value=default_range)

if st.button("🔄 Refresh data", type="primary"):
    if not isinstance(win, (list, tuple)) or len(win) != 2:
        st.warning("Pick both a start and end date.")
    else:
        start, end = win
        try:
            with st.spinner("Fetching approval emails and calendar invites…"):
                emails = fetch_ooo_requests(start, end, sender=settings.get("hr_sender", ""))
                start_dt = datetime(start.year, start.month, start.day)
                end_dt = datetime(end.year, end.month, end.day, 23, 59, 59)
                events = get_calendar_events(start_dt, end_dt)

            # Keep OoO-type calendar events only.
            ooo_events = [
                ev
                for ev in events
                if ev.get("showAs") == "oof"
                or any(k in (ev.get("subject", "") or "").lower() for k in OOO_KEYWORDS)
            ]

            emails_block = "\n".join(
                f"{e['id']} | {e['received'][:10]} | {e['sender_name']} | "
                f"{e['subject']} | {e['body'][:500]}"
                for e in emails
            )
            def _organizer(ev):
                addr = (ev.get("organizer", {}) or {}).get("emailAddress", {}) or {}
                return addr.get("name", "Unknown"), addr.get("address", "")

            calendar_block = "\n".join(
                "cal{} | {} <{}> | {} | {} | {}".format(
                    i,
                    *_organizer(ev),
                    ev.get("subject", ""),
                    _event_date(ev.get("start", {})),
                    _event_date(ev.get("end", {})),
                )
                for i, ev in enumerate(ooo_events)
            )

            with st.spinner("Normalizing records…"):
                text = P.generate(
                    client,
                    P.build_ooo_parse_prompt(
                        emails_block, calendar_block, roster, team_directory
                    ),
                    temperature=0.1,
                )
                parsed = _parse_llm_json(text)

            # Back-fill date_sent for system rows from the email received date.
            received_by_id = {e["id"]: e["received"][:10] for e in emails}
            for r in parsed.get("system_requests", []):
                r["date_sent"] = received_by_id.get(r.get("id"), "")

            st.session_state["ooo_data"] = {
                "system": parsed.get("system_requests", []),
                "calendar": parsed.get("calendar_invites", []),
                "start": start.isoformat(),
                "end": end.isoformat(),
            }
            st.success(
                f"Loaded {len(emails)} approval email(s) and {len(ooo_events)} calendar invite(s)."
            )
            if not settings.get("hr_sender"):
                st.info(
                    "No HR sender configured — used subject keywords. Set the sender "
                    "in ⚙️ Settings for more reliable matching."
                )
        except Exception as e:
            st.error(f"{e}")

data = st.session_state.get("ooo_data")
if not data:
    st.info("Set your window and click **Refresh data** to begin.")
    st.stop()

system_all = data["system"]
calendar_all = data["calendar"]
all_items = system_all + calendar_all
members = sorted({i.get("member", "Other") for i in all_items} | set(roster))
today = date.today()
win_end = ooo_logic.parse_date(data["end"]) or (today + timedelta(days=30))

tab_recon, tab_summary, tab_alerts, tab_pto = st.tabs(
    ["🔄 Reconciliation", "📅 Upcoming PTO Summary", "🚨 Coverage Alerts", "✅ PTO Approvals"]
)

# --------------------------------------------------------------------------- #
# 1. Reconciliation
# --------------------------------------------------------------------------- #
with tab_recon:
    member = st.selectbox("Team member", members)
    m_sys = [r for r in system_all if r.get("member") == member]
    m_cal = [c for c in calendar_all if c.get("member") == member]
    rows = ooo_logic.reconcile(m_sys, m_cal)
    if not rows:
        st.info(f"No OoO records for {member} in this window.")
    else:
        df = pd.DataFrame(rows).rename(
            columns={
                "source": "Source",
                "date_sent": "Date sent",
                "type": "Type",
                "dates": "Dates",
                "details": "Request details",
                "match_status": "Match Status",
            }
        )[["Source", "Date sent", "Type", "Dates", "Request details", "Match Status"]]
        st.dataframe(df, width="stretch", hide_index=True)

        unmatched = [r for r in rows if "No " in r["match_status"]]
        if unmatched:
            st.warning(f"{len(unmatched)} item(s) without a matched pair — review above.")

# --------------------------------------------------------------------------- #
# 2. Upcoming PTO Summary
# --------------------------------------------------------------------------- #
with tab_summary:
    st.caption(f"Upcoming time off from {today.isoformat()} through {win_end.isoformat()}.")
    summary = ooo_logic.summarize_upcoming(all_items, today, win_end)
    if not summary:
        st.info("No upcoming time off in this window.")
    else:
        srows = [
            {
                "Member": m,
                "Upcoming days": v["days"],
                "Next absence": v["next"].isoformat() if v["next"] else "—",
                "Ranges": "; ".join(v["ranges"]),
            }
            for m, v in sorted(summary.items())
        ]
        st.dataframe(pd.DataFrame(srows), width="stretch", hide_index=True)

        gantt = []
        for it in all_items:
            s = ooo_logic.parse_date(it.get("start_date"))
            e = ooo_logic.parse_date(it.get("end_date")) or s
            if not s or e < today:
                continue
            gantt.append(
                {
                    "Member": it.get("member", "Other"),
                    "Start": s.isoformat(),
                    "Finish": (e + timedelta(days=1)).isoformat(),
                    "Type": it.get("type", ""),
                }
            )
        if gantt:
            fig = px.timeline(
                pd.DataFrame(gantt),
                x_start="Start",
                x_end="Finish",
                y="Member",
                color="Type",
            )
            fig.update_yaxes(autorange="reversed")
            fig.update_layout(height=360, margin=dict(l=10, r=10, t=30, b=10))
            st.plotly_chart(fig, use_container_width=True)

# --------------------------------------------------------------------------- #
# 3. Coverage Alerts
# --------------------------------------------------------------------------- #
with tab_alerts:
    threshold = st.number_input(
        "Alert when this many members are out on a day",
        min_value=1,
        max_value=max(2, len(roster)),
        value=int(settings.get("coverage_threshold", 2)),
        step=1,
    )
    coverage = ooo_logic.coverage_by_day(all_items, today, win_end, weekdays_only=True)
    flagged = ooo_logic.thin_coverage(coverage, threshold)

    if flagged:
        st.error(f"⚠️ {len(flagged)} day(s) at or above the threshold of {threshold}.")
        st.dataframe(
            pd.DataFrame(
                [
                    {"Date": d.isoformat(), "# Out": len(m), "Members": ", ".join(m)}
                    for d, m in flagged
                ]
            ),
            width="stretch",
            hide_index=True,
        )
    else:
        st.success(f"No days with {threshold}+ members out (weekdays) in this window.")

    cov_rows = [
        {"Date": d.isoformat(), "Out": len(m)} for d, m in sorted(coverage.items())
    ]
    if cov_rows:
        fig = px.bar(pd.DataFrame(cov_rows), x="Date", y="Out")
        fig.add_hline(y=threshold, line_dash="dash", line_color="red")
        fig.update_layout(height=300, margin=dict(l=10, r=10, t=30, b=10))
        st.plotly_chart(fig, use_container_width=True)

# --------------------------------------------------------------------------- #
# 4. PTO Approvals (moved from Module F)
# --------------------------------------------------------------------------- #
with tab_pto:
    st.subheader("Pending PTO approvals")
    st.caption("Direct deep-links to the regional HR approval systems.")
    cols = st.columns(len(config.PTO_APPROVAL_LINKS))
    for col, (region, url) in zip(cols, config.PTO_APPROVAL_LINKS.items()):
        col.link_button(f"{region} approvals", url, width="stretch")
    st.caption(
        "These HR systems are not API-integrated; links open the approval queue "
        "for each region (US, Canada, Taiwan)."
    )
