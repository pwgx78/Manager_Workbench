"""
1:1 Meeting Prep Assistant.

A persistent, managed briefing for a direct report, organized into three sections
of project cards — SPR Projects (from Jira), Other Projects (email-derived or
manually added), and Goal Projects (imported from a goal-appraisal doc). Each
project carries a running action list that persists across meetings (open → done),
and can be closed. Data is gathered from Jira, Outlook, the Email Action Tracker,
and the calendar.
HITL: it only proposes — nothing is sent or scheduled.
"""
import hashlib
import re
from datetime import date, datetime, timedelta

import streamlit as st

import config
import store
import email_db
import one_on_one_db as OODB
import jira_analysis as JA
import llm_prompts as P
from api_helpers import (
    search_jira,
    fetch_emails_for_person,
    get_calendar_events,
    search_emails_by_subject,
    extract_text_from_bytes,
    clean_html,
)

st.header("🤝 1:1 Meeting Prep Assistant")
st.caption(
    "A persistent 1:1 briefing for a direct report: SPR, Other, and Goal project "
    "cards with running action items. Discussion topics only — nothing is sent."
)

client = st.session_state.get("gemini_client")
if client is None:
    st.error("Gemini client not initialized. Open the app from the main page (app.py).")
    st.stop()

OOO_KEYWORDS = ("ooo", "pto", "vto", "vacation", "out of office", "holiday")
MAX_TICKETS = 25
JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _nid(*parts):
    """Stable short natural-id from arbitrary text parts (for action items)."""
    return hashlib.md5("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


def _slug(text):
    """Stable slug used as a project key for non-SPR (Other) projects."""
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return s or _nid(text)


def _category(ticket):
    """Read the custom 'Category' priority field from a full Jira ticket's
    extra_fields (matched by display name / field id, case-insensitive)."""
    wanted = {s.strip().lower() for s in config.SPR_CATEGORY_FIELDS}
    for f in ticket.get("extra_fields") or []:
        if str(f.get("name", "")).strip().lower() in wanted:
            return str(f.get("value", "")).strip()
    return ""


def _jira_payload(ticket, analysis):
    """Structured per-SPR payload cached in raw_json and shown on the SPR card."""
    ms = analysis.get("milestones") or []
    accomplished = "; ".join(m.get("label", "") for m in ms if m.get("type") == "past")
    slips = "; ".join(
        f"{s.get('milestone', '')}: {s.get('original_date', '?')}→{s.get('new_date', '?')} "
        f"({s.get('slip_days', '?')}d)"
        for s in (analysis.get("schedule_slips") or [])
    )
    return {
        "summary": ticket.get("summary", ""),
        "jira_status": ticket.get("status", ""),
        "phase": analysis.get("current_phase", ""),
        "priority": _category(ticket),
        "digest": analysis.get("digest", ""),
        "accomplished": accomplished,
        "slips": slips,
        "next_steps": "; ".join(analysis.get("open_next_steps") or []),
    }


def _priority_rank(value):
    order = config.SPR_PRIORITY_ORDER
    rank = {v.strip().lower(): i for i, v in enumerate(order)}
    return rank.get(str(value).strip().lower(), len(order))


def _phase_rank(phase):
    pr = {p.strip().lower(): i for i, p in enumerate(config.JIRA_PHASES)}
    ph = str(phase).strip().lower()
    return next((i for k, i in pr.items() if ph.startswith(k)), len(pr))


def _sort_projects(projects, key, ascending, meta):
    """Sort a list of project dicts by the chosen section key. Category/State use
    logical orderings (SPR_PRIORITY_ORDER / JIRA_PHASES); others alphabetical."""
    def keyfn(p):
        m = meta.get(p["pkey"], {})
        if key == "Priority (Category)":
            return _priority_rank(m.get("priority", ""))
        if key == "Jira State":
            return _phase_rank(m.get("phase", ""))
        if key == "Category":
            return str(p["meta"].get("category", "")).lower()
        if key == "Assessment":
            v = str(p["meta"].get("assessment", "")).replace("%", "").strip()
            try:
                return -float(v)
            except ValueError:
                return 1e9
        if key == "Source":
            return str(p.get("source", "")).lower()
        if key in ("Index", "SPR Number"):
            return str(p.get("pkey", "")).lower()
        return str(p.get("name", "")).lower()

    return sorted(projects, key=keyfn, reverse=not ascending)


def _render_actions(report, ptype, pkey):
    """Running per-project action list: open items with a Done checkbox, done items
    greyed with their completion date, plus an add form. Persists across 1:1s."""
    acts = OODB.list_actions(report, ptype, pkey)
    st.markdown("**Actions**")
    if not acts:
        st.caption("No actions yet.")
    for a in acts:
        c1, c2 = st.columns([0.07, 0.93])
        is_done = a["status"] == "done"
        checked = c1.checkbox(
            "done", value=is_done, label_visibility="collapsed",
            key=f"ad_{ptype}_{pkey}_{a['id']}",
        )
        if checked and not is_done:
            OODB.set_action_status(a["id"], "done")
            st.rerun()
        if not checked and is_done:
            OODB.set_action_status(a["id"], "open")
            st.rerun()
        if is_done:
            c2.markdown(f"~~{a['text']}~~ · ✅ {a['done_at'][:10]}")
        else:
            added = f" · added {a['created_meeting']}" if a["created_meeting"] else ""
            c2.markdown(f"{a['text']}{added}")
    with st.form(f"addact_{ptype}_{pkey}", clear_on_submit=True):
        fc1, fc2 = st.columns([0.85, 0.15])
        txt = fc1.text_input(
            "New action", label_visibility="collapsed", placeholder="Add an action…"
        )
        if fc2.form_submit_button("➕ Add") and txt.strip():
            OODB.add_action(report, ptype, pkey, txt.strip(), meeting=date.today().isoformat())
            st.rerun()


def _close_button(report, ptype, pkey):
    if st.button("✔️ Close project", key=f"close_{ptype}_{pkey}"):
        OODB.set_project_status(report, ptype, pkey, "closed")
        st.rerun()


cfg = config.load_config()
members = cfg.get("team_members") or [
    {"name": n, "function": "", "email": "", "core_id": ""}
    for n in cfg.get("team_roster", config.DEFAULT_TEAM_ROSTER)
]
members = [m for m in members if (m.get("name") or "").strip()]

if not members:
    st.info("No team members configured. Add your direct reports in Settings → 👥 Team.")
    st.stop()

# --------------------------------------------------------------------------- #
# Selectors
# --------------------------------------------------------------------------- #
names = [m["name"] for m in members]
member_name = st.selectbox("Direct report", names)
member = next(m for m in members if m["name"] == member_name)
email = (member.get("email") or "").strip()

period_choice = st.radio(
    "Period",
    ["Past 7 days", "Past 14 days", "Past 30 days", "Since last meeting", "Custom"],
    horizontal=True,
)

today = date.today()
meetings = store.load_json(config.ONE_ON_ONE_KEY, default={})
if not isinstance(meetings, dict):
    meetings = {}

start = today - timedelta(days=7)
if period_choice == "Past 14 days":
    start = today - timedelta(days=14)
elif period_choice == "Past 30 days":
    start = today - timedelta(days=30)
elif period_choice == "Since last meeting":
    last = meetings.get(member_name)
    start = date.fromisoformat(last) if last else today - timedelta(days=7)
    st.caption(f"Last prep: {last}" if last else "No prior prep recorded — using the past 7 days.")
elif period_choice == "Custom":
    rng = st.date_input("Custom range", value=(today - timedelta(days=7), today))
    if isinstance(rng, (list, tuple)) and len(rng) == 2:
        start, today = rng

window_days = max(1, (today - start).days)
period_label = f"{start.isoformat()} → {today.isoformat()}"
sdt = datetime(start.year, start.month, start.day)
edt = datetime(today.year, today.month, today.day, 23, 59, 59)

if not email:
    st.warning(
        f"No email on file for {member_name} — Jira and Outlook sections will be skipped. "
        "Add it in Settings → 👥 Team."
    )

# --------------------------------------------------------------------------- #
# Manage: import goals + add a project
# --------------------------------------------------------------------------- #
with st.expander("📥 Import Goal Appraisal → Goal Projects"):
    st.caption(
        "Upload the member's goal-appraisal doc (PDF/Excel/Word). Each leaf sub-goal "
        "becomes a Goal Project you can attach actions to. Re-upload anytime to refresh."
    )
    up = st.file_uploader(
        "Appraisal file", type=["pdf", "xlsx", "docx", "csv", "txt"], key="goal_up"
    )
    if st.button("Import goals", disabled=up is None):
        try:
            text = extract_text_from_bytes(up.name, up.getvalue())
            with st.spinner("Parsing goals…"):
                goals = JA.parse_llm_json(
                    P.generate(client, P.build_goal_appraisal_prompt(text), temperature=0.1)
                )
            n = 0
            for g in goals if isinstance(goals, list) else []:
                idx = str(g.get("index", "")).strip()
                if not idx:
                    continue
                OODB.upsert_project(
                    member_name, "goal", idx,
                    (g.get("goal", "") or idx)[:80], f"appraisal:{up.name}",
                    meta={
                        "category": g.get("category", ""),
                        "goal": g.get("goal", ""),
                        "remarks": g.get("remarks", ""),
                        "assessment": g.get("assessment", ""),
                    },
                )
                n += 1
            st.success(f"Imported {n} goal(s) for {member_name}.")
            st.rerun()
        except Exception as e:
            st.error(f"Could not parse goals: {e}")

with st.expander("➕ Add a project (persists across 1:1s)"):
    st.caption(
        "Add a project by name. Optional comma-separated keywords are used to scrub "
        "matching email into this project on future Generates."
    )
    with st.form("add_proj", clear_on_submit=True):
        pn = st.text_input("Project name")
        pk = st.text_input("Scrub keywords (comma-separated, optional)")
        if st.form_submit_button("Add project") and pn.strip():
            OODB.upsert_project(
                member_name, "other", _slug(pn.strip()), pn.strip(), "manual",
                keywords=pk.strip(),
            )
            st.success(f"Added project '{pn.strip()}'.")
            st.rerun()

# --------------------------------------------------------------------------- #
# Cache status
# --------------------------------------------------------------------------- #
_counts, _last_seen = OODB.source_counts(member_name)
if _counts:
    summary = " · ".join(f"{n} {src}" for src, n in sorted(_counts.items()))
    cap, btn = st.columns([4, 1])
    cap.caption(f"🗄️ Cached for {member_name}: {summary} · last updated {_last_seen or '—'}")
    if btn.button("🗑️ Clear data cache", help="Clears gathered items only; keeps projects & actions"):
        OODB.clear_report(member_name)
        st.rerun()
else:
    st.caption(f"🗄️ No gathered data yet for {member_name} — Generate to pull from the sources.")

# --------------------------------------------------------------------------- #
# Generate — pull the sources, refresh the cache, and populate the registry
# --------------------------------------------------------------------------- #
if st.button("Generate Prep-Doc", type="primary"):
    # --- Jira (analyze active + done through the shared cached analysis) ---- #
    if email:
        try:
            active = search_jira(
                f'assignee = "{email}" AND statusCategory != Done ORDER BY updated DESC',
                max_results=MAX_TICKETS,
            )
        except Exception as e:
            active = []
            st.warning(f"Jira active pull skipped: {e}")
        try:
            done = search_jira(
                f'assignee = "{email}" AND statusCategory = Done AND updated >= -{window_days}d '
                "ORDER BY updated DESC",
                max_results=MAX_TICKETS,
            )
        except Exception:
            done = []

        seen_keys = set()
        tickets = [t for t in active + done if not (t.get("key") in seen_keys or seen_keys.add(t.get("key")))]
        if tickets:
            prog = st.progress(0.0)
            for i, iss in enumerate(tickets):
                key = iss.get("key")
                try:
                    with st.spinner(f"Analyzing {key}…"):
                        _t, a = JA.get_or_analyze_ticket(client, key)
                    payload = _jira_payload(_t, a)
                    detail = f"[{key}] {payload['summary']} | {payload['phase']} ({payload['jira_status']})"
                except Exception:
                    payload = {"summary": iss.get("summary", ""), "jira_status": iss.get("status", "")}
                    detail = f"[{key}] {iss.get('summary','')} (analysis unavailable)"
                OODB.upsert_item(
                    member_name, "jira", key, key, payload.get("summary", ""), detail,
                    item_date=iss.get("updated", ""),
                    status=payload.get("jira_status", ""), raw=payload,
                )
                # Register/refresh the SPR project (status preserved if closed).
                OODB.upsert_project(member_name, "spr", key, payload.get("summary", "") or key, "jira")
                prog.progress((i + 1) / len(tickets))
            prog.empty()

    # --- Emails involving the member (reuse the Phase-0 project tag) -------- #
    if email:
        try:
            for m in fetch_emails_for_person(email, sdt, edt):
                mid = m.get("id")
                project = email_db.get_project_for_message(mid) or "Unassigned"
                OODB.upsert_item(
                    member_name, "email", mid or _nid(m.get("subject"), m.get("received")),
                    project, m.get("subject", ""),
                    f"{m['sender_name']} | {m['subject']}: {m['body'][:400]}",
                    item_date=m.get("received", ""),
                )
        except Exception as e:
            st.warning(f"Outlook pull skipped: {e}")

    # --- Manual projects: scrub email by keyword and tag it to the project -- #
    if email:
        for proj in OODB.list_projects(member_name, "other"):
            if proj["source"] != "manual" or not proj["keywords"]:
                continue
            terms = [t.strip() for t in proj["keywords"].split(",") if t.strip()]
            for term in terms:
                try:
                    for msg in search_emails_by_subject(term, sdt, edt, max_messages=8):
                        mid = msg.get("id")
                        sender = (msg.get("sender", {}) or {}).get("emailAddress", {}) or {}
                        body = clean_html(msg.get("body", {}).get("content", ""))
                        OODB.upsert_item(
                            member_name, "email", mid or _nid(term, msg.get("subject")),
                            proj["name"], msg.get("subject", ""),
                            f"{sender.get('name','Unknown')} | {msg.get('subject','')}: {body[:400]}",
                            item_date=msg.get("receivedDateTime", ""),
                        )
                except Exception as e:
                    st.warning(f"Email scrub skipped for '{term}': {e}")

    # --- Open actions assigned to the member ------------------------------- #
    name_l = member_name.lower()
    email_l = email.lower()
    for r in store.load_json(config.EMAIL_ACTIONS_KEY, default=[]) or []:
        owner = str(r.get("Owner", "")).lower()
        if r.get("Completed"):
            continue
        if name_l in owner or (email_l and email_l in owner):
            action = r.get("Action", "")
            thread = r.get("Email Thread", "")
            OODB.upsert_item(
                member_name, "action", _nid(action, thread), "Unassigned", action,
                f"{action} (priority {r.get('Priority','?')}, thread: {thread})",
                status=str(r.get("Priority", "")),
            )

    # --- Upcoming PTO from the calendar ------------------------------------ #
    try:
        ev_start = datetime(today.year, today.month, today.day)
        ev_end = ev_start + timedelta(days=28)
        first = name_l.split()[0] if name_l else ""
        for ev in get_calendar_events(ev_start, ev_end):
            org = (ev.get("organizer", {}) or {}).get("emailAddress", {}) or {}
            oname = (org.get("name", "") or "").lower()
            oaddr = (org.get("address", "") or "").lower()
            who = (email_l and email_l == oaddr) or (first and first in oname) or (name_l in oname)
            is_ooo = ev.get("showAs") == "oof" or any(
                k in (ev.get("subject", "") or "").lower() for k in OOO_KEYWORDS
            )
            if who and is_ooo:
                s = (ev.get("start", {}) or {}).get("dateTime") or (ev.get("start", {}) or {}).get("date", "")
                e = (ev.get("end", {}) or {}).get("dateTime") or (ev.get("end", {}) or {}).get("date", "")
                OODB.upsert_item(
                    member_name, "pto", _nid(ev.get("subject"), s), "Unassigned",
                    ev.get("subject", "OoO"),
                    f"- {ev.get('subject','OoO')}: {s[:10]} → {e[:10]}", item_date=s[:10],
                )
    except Exception as e:
        st.warning(f"Calendar pull skipped: {e}")

    # --- Register non-SPR (Other) projects from gathered items ------------- #
    meta_now = OODB.jira_meta(member_name)
    existing = {(x["ptype"], x["pkey"]) for x in OODB.list_projects(member_name, include_closed=True)}
    for p in {it["project"] for it in OODB.get_items(member_name) if it["source"] != "pto"}:
        if not p:
            continue
        if JIRA_KEY_RE.match(p):
            if ("spr", p) not in existing:
                OODB.upsert_project(member_name, "spr", p, (meta_now.get(p, {}) or {}).get("summary") or p, "jira")
        elif p == "Unassigned":
            if ("other", "unassigned") not in existing:
                OODB.upsert_project(member_name, "other", "unassigned", "Unassigned (untagged items)", "untagged")
        else:
            if ("other", _slug(p)) not in existing:
                OODB.upsert_project(member_name, "other", _slug(p), p, "email request")

    meetings[member_name] = today.isoformat()
    store.save_json(config.ONE_ON_ONE_KEY, meetings)
    st.success("Prep-doc refreshed.")

# --------------------------------------------------------------------------- #
# Output — three sortable card sections (rendered from the persistent registry)
# --------------------------------------------------------------------------- #
st.divider()
st.subheader(f"1:1 Briefing — {member_name}")

meta = OODB.jira_meta(member_name)
item_snippets = {}
for it in OODB.get_items(member_name):
    if it["source"] in ("email", "action"):
        item_snippets.setdefault(it["project"] or "Unassigned", []).append(it["detail"])


def _section_header(title, key_prefix, sort_options, with_closed_toggle=True):
    st.markdown(f"### {title}")
    cols = st.columns([2, 1, 1] if with_closed_toggle else [2, 1])
    sort_key = cols[0].selectbox("Sort by", sort_options, key=f"{key_prefix}_sort")
    ascending = cols[1].radio(
        "Order", ["Ascending", "Descending"], horizontal=True, key=f"{key_prefix}_dir"
    ) == "Ascending"
    show_closed = cols[2].checkbox("Show closed", key=f"{key_prefix}_closed") if with_closed_toggle else False
    return sort_key, ascending, show_closed


def _render_spr_card(p):
    m = meta.get(p["pkey"], {})
    phase, status = m.get("phase", ""), m.get("jira_status", "")
    state = f"{phase} ({status})".strip() if (phase or status) else (status or "—")
    prio = m.get("priority", "") or "—"
    with st.expander(f"{p['pkey']} — {p['name']}  ·  [{prio}]  ·  {state}"):
        st.markdown(f"[Open {p['pkey']} in Jira ↗]({config.JIRA_BASE_URL.rstrip('/')}/browse/{p['pkey']})")
        st.markdown(f"**Investigation Summary**\n\n{m.get('digest') or '_None._'}")
        st.markdown(f"**Recently Accomplished**\n\n{m.get('accomplished') or '_None._'}")
        st.markdown(f"**Roadblocks / Slips**\n\n{m.get('slips') or '_None._'}")
        st.markdown(f"**Next Steps**\n\n{m.get('next_steps') or '_None._'}")
        st.divider()
        _render_actions(member_name, "spr", p["pkey"])
        if p["status"] == "closed":
            if st.button("↩️ Reopen", key=f"reopen_spr_{p['pkey']}"):
                OODB.set_project_status(member_name, "spr", p["pkey"], "open")
                st.rerun()
        else:
            _close_button(member_name, "spr", p["pkey"])


# --- SPR Projects (split into Open / Closed sub-sections) -------------------- #
sort_key, ascending, _ = _section_header(
    "🗂️ SPR Projects", "spr", ["Priority (Category)", "Jira State", "SPR Number"],
    with_closed_toggle=False,
)
all_spr = _sort_projects(
    OODB.list_projects(member_name, "spr", include_closed=True), sort_key, ascending, meta
)
open_spr = [p for p in all_spr if p["status"] != "closed"]
closed_spr = [p for p in all_spr if p["status"] == "closed"]

st.markdown(f"#### Open Projects ({len(open_spr)})")
if not open_spr:
    st.info("No open SPR projects — Generate to pull the member's assigned tickets.")
for p in open_spr:
    _render_spr_card(p)

st.markdown(f"#### Closed Projects ({len(closed_spr)})")
if not closed_spr:
    st.caption("No closed SPR projects.")
for p in closed_spr:
    _render_spr_card(p)

# --- Other Projects ---------------------------------------------------------- #
sort_key, ascending, show_closed = _section_header(
    "🗂️ Other Projects", "other", ["Source", "Name"]
)
other_projects = _sort_projects(
    OODB.list_projects(member_name, "other", include_closed=show_closed), sort_key, ascending, meta
)
if not other_projects:
    st.info("No other projects yet — add one above, or Generate to pick up email-tagged projects.")
for p in other_projects:
    closed_tag = " · 🔒 closed" if p["status"] == "closed" else ""
    with st.expander(f"{p['name']}  ·  ({p['source'] or 'manual'}){closed_tag}"):
        st.caption(f"Source: {p['source'] or 'manual'}")
        if p["keywords"]:
            st.caption(f"Scrub keywords: {p['keywords']}")
        snip_key = "Unassigned" if p["pkey"] == "unassigned" else p["name"]
        snips = item_snippets.get(snip_key, [])
        if snips:
            st.markdown("**Recent items**")
            for s in snips[:8]:
                st.markdown(f"- {s}")
        st.divider()
        _render_actions(member_name, "other", p["pkey"])
        if p["status"] == "closed":
            if st.button("↩️ Reopen", key=f"reopen_other_{p['pkey']}"):
                OODB.set_project_status(member_name, "other", p["pkey"], "open")
                st.rerun()
        else:
            _close_button(member_name, "other", p["pkey"])

# --- Goal Projects (grouped by the leading index number, e.g. 5.1.4 → "5") --- #
def _goal_group(pkey):
    """Top-level group for a goal index: the part before the first dot ('5.1.4' → '5')."""
    head = str(pkey).split(".", 1)[0].strip()
    return head or "?"


def _render_goal_card(p):
    gm = p["meta"]
    assess = gm.get("assessment", "")
    closed_tag = " · 🔒 closed" if p["status"] == "closed" else ""
    goal_text = gm.get("goal", "") or p["name"]
    title = (
        f"{p['pkey']} - {gm.get('category', '') or 'Goal'} - "
        f"Goal: {goal_text} - {assess or '—'}{closed_tag}"
    )
    with st.expander(title):
        st.markdown(f"**Goal:** {gm.get('goal','') or p['name']}")
        if gm.get("remarks"):
            st.markdown(f"**Remarks:** {gm['remarks']}")
        if assess:
            st.caption(f"Appraisal: {assess}")
        st.divider()
        _render_actions(member_name, "goal", p["pkey"])
        if p["status"] == "closed":
            if st.button("↩️ Reopen", key=f"reopen_goal_{p['pkey']}"):
                OODB.set_project_status(member_name, "goal", p["pkey"], "open")
                st.rerun()
        else:
            _close_button(member_name, "goal", p["pkey"])


sort_key, ascending, show_closed = _section_header(
    "🎯 Goal Projects", "goal", ["Category", "Assessment", "Index"]
)
goal_projects = _sort_projects(
    OODB.list_projects(member_name, "goal", include_closed=show_closed), sort_key, ascending, meta
)
if not goal_projects:
    st.info("No goal projects yet — import a goal-appraisal doc above.")
else:
    # Group by the leading index number; group headings ordered numerically.
    groups = {}
    for p in goal_projects:
        groups.setdefault(_goal_group(p["pkey"]), []).append(p)

    def _group_order(g):
        return (0, int(g)) if g.isdigit() else (1, g)

    for g in sorted(groups, key=_group_order):
        members_in_group = groups[g]
        # If every goal in the group shares one category, show it in the heading.
        cats = {p["meta"].get("category", "") for p in members_in_group if p["meta"].get("category")}
        label = f"Goal {g}" + (f" — {next(iter(cats))}" if len(cats) == 1 else "")
        st.markdown(f"#### {label} ({len(members_in_group)})")
        for p in members_in_group:
            _render_goal_card(p)

# --------------------------------------------------------------------------- #
# Standing sections + copy
# --------------------------------------------------------------------------- #
st.divider()
st.markdown("#### 🌱 Development & Recognition")
dev_note = st.text_area(
    "Your observations (manual)",
    key=f"dev_{member_name}",
    placeholder="Note coaching points, recognition, or growth topics to raise.",
    height=120,
)

st.markdown("#### 🗓️ Upcoming Time Off (next 4 weeks)")
pto_lines = [it["detail"] for it in OODB.get_items(member_name) if it["source"] == "pto"]
pto_md = "\n".join(pto_lines) if pto_lines else "_None found._"
st.markdown(pto_md)


def _section_md(title, projects, body_fn):
    parts = [f"## {title}"]
    for p in projects:
        parts.append(f"### {p['name']}")
        parts.append(body_fn(p))
        acts = OODB.list_actions(member_name, p["ptype"], p["pkey"])
        if acts:
            parts.append("Actions:")
            for a in acts:
                mark = "x" if a["status"] == "done" else " "
                parts.append(f"- [{mark}] {a['text']}")
    return "\n".join(parts)


def _spr_body(p):
    m = meta.get(p["pkey"], {})
    return (
        f"- SPR: {p['pkey']} ({config.JIRA_BASE_URL.rstrip('/')}/browse/{p['pkey']})\n"
        f"- Priority: {m.get('priority','—')} | State: {m.get('phase','')} ({m.get('jira_status','')})\n"
        f"- Investigation: {m.get('digest','')}\n"
        f"- Recently Accomplished: {m.get('accomplished','')}\n"
        f"- Roadblocks/Slips: {m.get('slips','')}\n"
        f"- Next Steps: {m.get('next_steps','')}"
    )


full = "\n\n".join(
    [
        f"# 1:1 Briefing — {member_name}",
        _section_md("SPR Projects", all_spr, _spr_body),
        _section_md("Other Projects", other_projects, lambda p: f"- Source: {p['source']}"),
        _section_md(
            "Goal Projects", goal_projects,
            lambda p: f"- {p['meta'].get('goal','')} (assessment {p['meta'].get('assessment','—')})",
        ),
        f"## Development & Recognition\n{dev_note}",
        f"## Upcoming Time Off\n{pto_md}",
    ]
)
with st.expander("📋 Copy full briefing"):
    st.code(full, language="markdown")
