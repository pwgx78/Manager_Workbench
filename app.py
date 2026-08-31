"""
ECRT ME Team Management Agent — multi-page Streamlit router.

Run with:  streamlit run app.py

Responsibilities:
  - Set page config once and draw the shared banner (app name + source
    build stamp, so it is obvious which copy of the code is running).
  - Open the active user profile: create its database, apply the schema, and run
    the one-time migration of the old repo-root data files into it.
  - Bootstrap GCP credentials + a single shared Gemini client (session_state).
  - Register all module pages under the role-based navigation groups
    (Individual Execution / Team Operations / People Management / Setup).

Human-in-the-Loop philosophy: the agent drafts, summarizes, and presents
Approve/Edit/Send buttons. It never auto-sends mail, posts to Teams, or closes
Jira tickets.
"""
import datetime as dt
import glob
import os

import streamlit as st

import config
import db
import user_profile

st.set_page_config(page_title="ME Manager Agent", layout="wide")


APP_NAME = "ME Manager Agent"
APP_TAGLINE = "ECRT ME Team Management Agent"


def _source_build_stamp():
    """Newest mtime across the app's Python sources, as (build_number, datetime).

    The build number is a sortable YYYYMMDD.HHMM of that mtime. Deliberately
    uncached: it exists so the banner proves which copy of the code the browser
    is actually talking to, and a cached value would defeat that. Cost is a few
    dozen stat() calls per rerun.
    """
    newest = 0.0
    for pattern in ("*.py", os.path.join("pages", "*.py")):
        for path in glob.glob(os.path.join(config.PROJECT_ROOT, pattern)):
            newest = max(newest, os.path.getmtime(path))
    stamp = dt.datetime.fromtimestamp(newest or dt.datetime.now().timestamp())
    return stamp.strftime("%Y%m%d.%H%M"), stamp


def _render_banner():
    """Gradient header shown above every page: app name + source build stamp."""
    build, stamp = _source_build_stamp()
    st.markdown(
        f"""
        <div style="
            display:flex; align-items:center; justify-content:space-between;
            gap:1rem; flex-wrap:wrap;
            padding:0.9rem 1.4rem; margin-bottom:1.2rem;
            border-radius:12px;
            background:linear-gradient(100deg,#0F2F4F 0%,#1E5B8C 55%,#2E8FBF 100%);
            box-shadow:0 2px 10px rgba(0,0,0,0.18);">
          <div style="min-width:0;">
            <div style="font-size:1.55rem; font-weight:700; color:#FFFFFF;
                        letter-spacing:0.4px; line-height:1.2;">
              &#128295; {APP_NAME}
            </div>
            <div style="font-size:0.8rem; color:#BFD9EC; margin-top:0.15rem;">
              {APP_TAGLINE}
            </div>
          </div>
          <div style="text-align:right; white-space:nowrap;">
            <div style="font-size:0.68rem; text-transform:uppercase;
                        letter-spacing:1.2px; color:#BFD9EC;">
              Source build
            </div>
            <div style="font-family:'Consolas','Courier New',monospace;
                        font-size:1.25rem; font-weight:700; color:#FFFFFF;">
              {build}
            </div>
            <div style="font-size:0.68rem; color:#BFD9EC;">
              updated {stamp.strftime('%a %d %b %Y, %H:%M')}
            </div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


@st.cache_resource(show_spinner=False)
def _bootstrap_profile(profile_id):
    """Prepare the active profile's database exactly once per profile.

    Keyed on `profile_id` so switching profiles re-runs it against the new
    database. Schema creation lives here rather than at module import so the
    target file can change at runtime."""
    user_profile.ensure_home()
    user_profile.adopt_legacy_token_cache(config.PROJECT_ROOT)
    db.init_schema()
    db.migrate_legacy(config.PROJECT_ROOT)
    # ASCII only: this goes to the console, which is cp1252 on a default Windows
    # terminal and raises UnicodeEncodeError on anything outside it.
    print(f"[App] Active profile: {profile_id} -> {user_profile.db_path()}")
    return True


@st.cache_resource(show_spinner=False)
def _bootstrap_client():
    """Initialize GCP auth and create one Gemini client for the whole app.
    Cached so every page shares the same client."""
    cfg = config.load_config()
    auth_ok = config.initialize_gcp_auth(cfg.get("gcp_credentials_path", ""))
    if not auth_ok:
        return None, cfg
    return config.get_gemini_client(), cfg


# Open the profile before anything reads config or data — every store below
# resolves through the active profile's database.
_profile_id = user_profile.ensure_home()
_bootstrap_profile(_profile_id)

client, _ = _bootstrap_client()
# Load config fresh each run (not the cached bootstrap copy) so roster/team edits
# made in Settings propagate to every page immediately.
cfg = config.load_config()
st.session_state["gemini_client"] = client
st.session_state["app_config"] = cfg

with st.sidebar:
    st.title("ME Manager Agent 🛠️")
    if client is None:
        st.error(
            "GCP credentials not configured. Open **Settings** to paste the path "
            "to your service-account JSON."
        )
    else:
        st.success("Gemini client ready.")
    if not config.identity_is_configured():
        st.warning(
            "Tell the app who you are — open **Settings → Identity**. Until then "
            "drafts and 1:1 prep won't be personalized."
        )
    st.caption(f"Profile: **{_profile_id}**")
    st.caption("Human-in-the-Loop: the agent drafts; you approve & send.")

pages = {
    "📁 Project Management": [
        st.Page("pages/2_project_management.py", title="Project Register", icon="📁"),
    ],
    "👤 Individual Execution": [
        st.Page("pages/0_email_actions.py", title="Email Action Identifier", icon="📥"),
        st.Page("pages/3_exec_translator.py", title="Executive Translator", icon="🎯"),
    ],
    "🛠️ Team Operations": [
        st.Page("pages/9_timeline_schedule.py", title="Timeline & Schedule Management", icon="🗓️"),
        st.Page("pages/1_staff_meeting.py", title="Staff Meeting Builder", icon="🏗️"),
        st.Page("pages/7_jira_state_tracker.py", title="Jira State Tracker", icon="📊"),
    ],
    "👥 People Management": [
        st.Page("pages/8_ooo_management.py", title="OoO & FTO Management", icon="🌴"),
        st.Page("pages/13_one_on_one_prep.py", title="1:1 Meeting Prep Assistant", icon="🤝"),
        st.Page("pages/14_manager_1on1_prep.py", title="Manager 1:1 Prep", icon="🧭"),
        st.Page("pages/10_talent_hr.py", title="Talent & HR Planning", icon="🌱"),
        st.Page("pages/12_idp.py", title="Individual Development Planning (IDP)", icon="🧭"),
        st.Page("pages/11_compensation.py", title="Compensation Planning", icon="💰"),
    ],
    "⚙️ Setup": [
        st.Page("pages/settings.py", title="Settings", icon="⚙️"),
    ],
}

# Rendered before .run() so the banner sits above every page's content.
_render_banner()
st.navigation(pages).run()
