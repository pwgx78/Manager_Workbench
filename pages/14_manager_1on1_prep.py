"""
Manager 1:1 Prep (upward) — the user's 1:1 with their own manager.

Unlike the direct-report prep (page 13), this is an UPWARD prep. It is driven by
the Critical/Aged issues on the manager's PowerBI dashboard (ingested via a manual
Excel/CSV/paste export, since the dashboard is an interactive app.powerbi.com SPA
on Zebra's locked-down tenant), enriched from Jira + Outlook, and produces:
per-issue predicted questions from the manager with pre-armed answers, special-projects
status, team ops, and auto-extracted personal concerns.

HITL: it only proposes — nothing is sent or scheduled.
"""
import re
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

import config
import store
import jira_analysis as JA
import llm_prompts as P
from api_helpers import (
    clean_html,
    extract_text_from_bytes,
    fetch_jira_ticket_full,
    fetch_recent_inbox,
    get_calendar_events,
    search_emails_by_subject,
)

st.header(f"🧭 Manager 1:1 Prep ({config.MANAGER_NAME})")
st.caption(
    "Generate a data-backed agenda and predictive Q&A for your 1:1 with "
    f"{config.MANAGER_NAME}. Driven by the Critical/Aged issues you export from the "
    "dashboard, enriched from Jira and Outlook. Talking points only — nothing is sent."
)

client = st.session_state.get("gemini_client")
if client is None:
    st.error("Gemini client not initialized. Open the app from the main page (app.py).")
    st.stop()

OOO_KEYWORDS = ("ooo", "pto", "vto", "vacation", "out of office", "holiday")
JIRA_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
MAX_ISSUES = 20
CAL_HORIZON_DAYS = 14  # spec: calendar logistics for the next 2 weeks

cfg = config.load_config()
today = date.today()

# Saved "Manual Additions" — standing topics persisted across runs.
_topics_blob = store.load_json(config.MANAGER_TOPICS_KEY, default={})
saved_topics = _topics_blob.get("topics", "") if isinstance(_topics_blob, dict) else ""

# --------------------------------------------------------------------------- #
# Step 1 input — the PowerBI Critical/Aged export
# --------------------------------------------------------------------------- #
# The dashboard link is per-user (Settings → Identity) and optional, so the
# instruction has to stand on its own when no URL is configured.
_dashboard = config.POWERBI_DASHBOARD_URL
if _dashboard:
    st.markdown(
        f"**Dashboard:** [Open the {config.MANAGER_NAME} PowerBI report]({_dashboard}) "
        "→ export the *Critical Issues* and *Aged Issues* visuals to Excel/CSV (or copy "
        "the rows), then upload or paste below."
    )
else:
    st.markdown(
        "**Dashboard:** export the *Critical Issues* and *Aged Issues* visuals to "
        "Excel/CSV (or copy the rows), then upload or paste below. Add your "
        "dashboard link in **Settings → Identity** to get a shortcut here."
    )

up = st.file_uploader(
    "Critical/Aged issues export (xlsx / csv / txt / pdf / docx)",
    type=["xlsx", "csv", "txt", "pdf", "docx"],
)
pasted = st.text_area(
    "…or paste the Critical/Aged rows here",
    height=140,
    placeholder="Paste the dashboard rows (must include Jira/SPR keys, e.g. ECRT-1234).",
)

lookback = st.radio(
    "Email sift window",
    ["Past 7 days", "Past 14 days", "Past 30 days"],
    index=1,
    horizontal=True,
)
days = {"Past 7 days": 7, "Past 14 days": 14, "Past 30 days": 30}[lookback]
start = today - timedelta(days=days)
period_label = f"{start.isoformat()} → {today.isoformat()}"

# --------------------------------------------------------------------------- #
# Special projects — user-configurable subject/keywords (persisted to JSON)
# --------------------------------------------------------------------------- #
special_projects = config.load_special_projects()
with st.expander("⚙️ Configure Special Projects (email sift terms)"):
    st.caption(
        "Each project's **Subject** is the primary Outlook subject search term and "
        "the label shown in the briefing. **Keywords** are optional, comma-separated "
        "additional subject terms. Saved to your profile."
    )
    sp_edit = st.data_editor(
        pd.DataFrame(special_projects or [{"subject": "", "keywords": ""}]),
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        key="special_projects_editor",
        column_config={
            "subject": st.column_config.TextColumn("Project / Subject", width="medium"),
            "keywords": st.column_config.TextColumn("Keywords (comma-separated)", width="large"),
        },
    )
    if st.button("Save special projects"):
        cleaned = []
        for r in sp_edit.fillna("").to_dict("records"):
            subject = str(r.get("subject", "")).strip()
            if subject:
                cleaned.append({"subject": subject, "keywords": str(r.get("keywords", "")).strip()})
        config.save_special_projects(cleaned)
        special_projects = cleaned
        st.success("Special projects saved.")
        st.rerun()

# --------------------------------------------------------------------------- #
# Standing / off-the-cuff topics — saved across runs (persisted to the profile)
# --------------------------------------------------------------------------- #
with st.expander("📝 Standing / Off-the-Cuff Topics (saved across runs)"):
    st.caption(
        f"Topics you always want to raise with {config.MANAGER_NAME} — one per line. "
        "Saved to your profile and pre-filled into the briefing's "
        "*Manual Additions* every run, so you never re-type them."
    )
    topics_edit = st.text_area(
        "Standing topics",
        value=saved_topics,
        key="manager_standing_topics",
        height=140,
        label_visibility="collapsed",
        placeholder="One topic per line, e.g.\n- Headcount for FY27\n- Lab space request",
    )
    if st.button("Save standing topics"):
        store.save_json(config.MANAGER_TOPICS_KEY, {"topics": topics_edit})
        saved_topics = topics_edit
        st.success("Standing topics saved.")
        st.rerun()

# --------------------------------------------------------------------------- #
# Generate
# --------------------------------------------------------------------------- #
if st.button("Generate Prep-Doc", type="primary"):
    export_text = (pasted or "").strip()
    if up is not None:
        try:
            export_text = (
                export_text + "\n" + extract_text_from_bytes(up.name, up.getvalue())
            ).strip()
        except Exception as e:
            st.warning(f"Could not read uploaded file: {e}")

    if not export_text:
        st.warning("Provide the Critical/Aged export (upload or paste) to continue.")
        st.stop()

    issues_lines, special_lines, pto_rows, cal_rows, personal_lines = [], [], [], [], []

    # Overall progress across the 5-step pipeline. Each step advances the bar to a
    # fixed checkpoint; the Jira step (the slow one) advances proportionally within
    # its 0.05 → 0.45 band as tickets are analyzed.
    overall = st.progress(0.0, text="Step 1/5 · Parsing the dashboard export…")

    # --- Step 1: extract Jira/SPR keys from the export ----------------------- #
    keys = list(dict.fromkeys(JIRA_KEY_RE.findall(export_text)))[:MAX_ISSUES]
    issues_lines.append("RAW EXPORT (Critical/Aged rows as provided):")
    issues_lines.append(export_text[:4000])
    overall.progress(0.05, text="Step 2/5 · Enriching issues from Jira…")

    # --- Step 2: Jira sift (cached/delta-aware; live refresh only on change) - #
    if keys:
        issues_lines.append("\nJIRA ENRICHMENT (per extracted key):")
        for i, key in enumerate(keys):
            overall.progress(
                0.05 + 0.40 * (i / len(keys)),
                text=f"Step 2/5 · Analyzing {key} ({i + 1}/{len(keys)})…",
            )
            try:
                _t, a = JA.get_or_analyze_ticket(client, key)
                nexts = "; ".join((a.get("open_next_steps") or [])[:2]) or "—"
                slips = a.get("schedule_slips") or []
                slip_note = f"{len(slips)} slip(s)" if slips else "no slips"
                issues_lines.append(
                    f"- [{key}] {_t.get('summary','')} | phase: {a.get('current_phase','?')} | "
                    f"status: {_t.get('status','')} | next: {nexts} | {slip_note}"
                )
            except Exception:
                # Fall back to the raw ticket text if deep analysis is unavailable.
                try:
                    issues_lines.append(f"- {fetch_jira_ticket_full(key).get('summary','')} [{key}]")
                except Exception as e:
                    issues_lines.append(f"- [{key}] (Jira lookup unavailable: {e})")
    else:
        st.info("No Jira/SPR keys found in the export — using the raw rows as-is.")

    # --- Step 4: special projects (configurable subject/keyword email sift) --- #
    overall.progress(0.45, text="Step 3/5 · Sifting email for special projects…")
    sdt = datetime(start.year, start.month, start.day)
    edt = datetime(today.year, today.month, today.day, 23, 59, 59)
    if not special_projects:
        st.info(
            "No special projects configured — skipping the special-projects email sift. "
            "Add topics under ⚙️ Configure Special Projects above."
        )
    for proj in special_projects:
        label = proj["subject"]
        # Search terms: the subject plus any comma-separated keywords.
        terms = [label] + [k.strip() for k in (proj.get("keywords") or "").split(",") if k.strip()]
        seen_ids = set()
        for term in terms:
            try:
                for msg in search_emails_by_subject(term, sdt, edt, max_messages=8):
                    mid = msg.get("id")
                    if mid in seen_ids:
                        continue
                    seen_ids.add(mid)
                    sender = (msg.get("sender", {}) or {}).get("emailAddress", {}) or {}
                    body = clean_html(msg.get("body", {}).get("content", ""))
                    special_lines.append(
                        f"[{label}] [{(msg.get('receivedDateTime','') or '')[:10]}] "
                        f"{sender.get('name','Unknown')} | {msg.get('subject','')}: {body[:400]}"
                    )
            except Exception as e:
                st.warning(f"Special-projects email sift skipped for '{term}': {e}")

    # --- Step 4: calendar — PTO/FTO + key meeting logistics (next 2 weeks) --- #
    overall.progress(0.60, text="Step 4/5 · Pulling team PTO & calendar logistics…")
    try:
        ev_start = datetime(today.year, today.month, today.day)
        ev_end = ev_start + timedelta(days=CAL_HORIZON_DAYS)
        for ev in get_calendar_events(ev_start, ev_end):
            subj = ev.get("subject", "") or ""
            s = (ev.get("start", {}) or {}).get("dateTime") or (ev.get("start", {}) or {}).get("date", "")
            e = (ev.get("end", {}) or {}).get("dateTime") or (ev.get("end", {}) or {}).get("date", "")
            org = (ev.get("organizer", {}) or {}).get("emailAddress", {}) or {}
            is_ooo = ev.get("showAs") == "oof" or any(k in subj.lower() for k in OOO_KEYWORDS)
            if is_ooo:
                pto_rows.append(f"- {subj or 'OoO'} ({org.get('name','')}): {s[:10]} → {e[:10]}")
            else:
                cal_rows.append(f"- {s[:16].replace('T',' ')} | {subj} (organizer: {org.get('name','')})")
    except Exception as e:
        st.warning(f"Calendar pull skipped: {e}")

    # --- Step 5: personal concerns (the user's own recent inbox) ------------- #
    overall.progress(0.75, text="Step 5/5 · Scanning inbox for personal concerns…")
    try:
        since_iso = sdt.strftime("%Y-%m-%dT00:00:00Z")
        for msg in fetch_recent_inbox(since_iso)[:60]:
            sender = (msg.get("sender", {}) or {}).get("emailAddress", {}) or {}
            body = clean_html(msg.get("body", {}).get("content", ""))
            personal_lines.append(
                f"[{(msg.get('receivedDateTime','') or '')[:10]}] {sender.get('name','Unknown')} | "
                f"{msg.get('subject','')}: {body[:300]}"
            )
    except Exception as e:
        st.warning(f"Inbox sift skipped: {e}")

    # --- Step 3 + synthesis -------------------------------------------------- #
    overall.progress(0.85, text="Synthesizing the 1:1 prep-doc…")
    try:
        sections = JA.parse_llm_json(
            P.generate(
                client,
                P.build_manager_prep_prompt(
                    "\n".join(issues_lines),
                    "\n".join(special_lines),
                    "\n".join(pto_rows),
                    "\n".join(cal_rows),
                    "\n".join(personal_lines),
                    period_label,
                    team_context=config.team_context_block(cfg),
                ),
                temperature=0.3,
            )
        )
        overall.progress(1.0, text="Done.")
        overall.empty()
        runs = store.load_json(config.MANAGER_PREP_KEY, default={})
        if not isinstance(runs, dict):
            runs = {}
        runs["last_run"] = today.isoformat()
        store.save_json(config.MANAGER_PREP_KEY, runs)
        st.session_state["manager_prep_doc"] = {
            "date": today.isoformat(),
            "period": period_label,
            "sections": sections,
            "pto": pto_rows,
            "calendar": cal_rows,
        }
    except Exception as e:
        overall.empty()
        st.error(f"Could not generate prep-doc: {e}")

# --------------------------------------------------------------------------- #
# Output — spec markdown structure
# --------------------------------------------------------------------------- #
doc = st.session_state.get("manager_prep_doc")
if doc:
    s = doc["sections"]
    st.divider()
    st.subheader(f"📊 1:1 Prep: {config.USER_NAME} & {config.MANAGER_NAME} — {doc['date']}")
    st.caption(f"Email window: {doc['period']}")

    def _copy(body):
        with st.expander("📋 Copy"):
            st.code(body or "_None._", language="markdown")

    # 1. Critical & Aged Issues -------------------------------------------- #
    st.markdown("### 1. Critical & Aged Issues (Dashboard Review)")
    issues = s.get("critical_issues") or []
    if isinstance(issues, list) and issues:
        issue_md_parts = []
        for it in issues:
            block = (
                f"**{it.get('name','Issue')}**: "
                f"{it.get('status','—')} | Blocker: {it.get('blocker','—')} | "
                f"{it.get('updates','—')}\n\n"
                f"- **Predicted Question:** _{it.get('predicted_question','—')}_\n"
                f"- **Your Data/Response:** {it.get('response','—')}"
            )
            st.markdown(block)
            st.markdown("")
            issue_md_parts.append(block)
        _copy("\n\n".join(issue_md_parts))
    else:
        st.markdown("_No critical/aged issues parsed._")

    # 2. Special Projects --------------------------------------------------- #
    st.markdown("### 2. Special Projects")
    st.markdown(s.get("special_projects") or "_None._")
    _copy(s.get("special_projects"))

    # 3. Team & Operations -------------------------------------------------- #
    st.markdown("### 3. Team & Operations")
    st.markdown(s.get("team_ops") or "_None._")
    _copy(s.get("team_ops"))

    # 4. Personal Development & Concerns ----------------------------------- #
    st.markdown("### 4. Personal Development & Concerns")
    st.markdown("**Suggested Topics (auto-extracted from email sift):**")
    st.markdown(s.get("personal_concerns") or "_None found._")
    st.markdown("**Manual Additions — your standing topics (edit under 📝 Standing / Off-the-Cuff Topics above):**")
    st.markdown(saved_topics or "_None saved yet._")
    extra = st.text_area(
        "Off-the-cuff items (this run only)",
        key="manager_extra",
        placeholder="Anything new to raise just for this meeting.",
        height=100,
    )
    # Merge the saved standing topics with this run's off-the-cuff additions.
    manual = "\n".join(t for t in [saved_topics, extra] if t and t.strip())

    # --- Copy the whole briefing ------------------------------------------ #
    issues_full = "\n\n".join(
        f"### {it.get('name','Issue')}\n"
        f"- Status: {it.get('status','—')}\n- Blocker: {it.get('blocker','—')}\n"
        f"- Updates: {it.get('updates','—')}\n"
        f"- Predicted Question: {it.get('predicted_question','—')}\n"
        f"- Your Data/Response: {it.get('response','—')}"
        for it in (issues if isinstance(issues, list) else [])
    ) or "_None._"
    full = (
        f"# 1:1 Prep: {config.USER_NAME} & {config.MANAGER_NAME} — {doc['date']}\n\n"
        f"## 1. Critical & Aged Issues\n{issues_full}\n\n"
        f"## 2. Special Projects\n{s.get('special_projects','')}\n\n"
        f"## 3. Team & Operations\n{s.get('team_ops','')}\n\n"
        f"## 4. Personal Development & Concerns\n{s.get('personal_concerns','')}\n\n"
        f"### Manual Additions\n{manual}\n"
    )
    st.divider()
    with st.expander("📋 Copy full briefing"):
        st.code(full, language="markdown")
