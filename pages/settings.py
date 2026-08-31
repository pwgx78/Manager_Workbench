"""
Settings — who you are, who your team is, where your data lives, and how the
app authenticates.

Four tabs, matching the way configuration is now split by ownership:

  Credentials — machine-local. Points the app at credential files stored
                ANYWHERE on disk; only the *paths* are saved (to machine.json),
                never the secrets, and they never travel with a profile.
  Identity    — profile data. Replaces the user/manager names that used to be
                hard-coded in config.py, which is what stopped anyone else from
                using the app.
  Team        — profile data. The directory fed to the LLM across the app.
  Profile     — the portability surface: switch profiles, and export or import a
                complete workbench as a single file.
"""
import os

import pandas as pd
import streamlit as st

import config
import db
import user_profile
from ms_auth import (
    ms_is_signed_in,
    ms_signed_in_account,
    ms_sign_out,
    begin_device_flow,
    complete_device_flow,
)

st.header("⚙️ Settings")

cfg = config.load_config()

# Mirrors the keys/labels in pages/3_exec_translator.py. Duplicated as a literal
# rather than imported: importing a Streamlit page module would execute it.
EXEC_AUDIENCES = [
    {"key": "sr_dir_eng", "label": "Senior Director, Engineering"},
    {"key": "bu_gm", "label": "Business Unit General Manager"},
    {"key": "svp_emc", "label": "SVP, EMC"},
]

# Credential registry — drives both the overview table and the edit form so
# the two never drift apart. Each entry maps a display label to the
# machine.json key that stores its file path.
CREDENTIALS = [
    {
        "key": "gcp_credentials_path",
        "label": "GCP service account (Vertex AI)",
        "help": "Service-account JSON used to authenticate the Gemini / Vertex client.",
        "placeholder": r"C:\Users\you\secure\its-compute-emc-zaip-p-...json",
    },
    {
        "key": "jira_pat_path",
        "label": "Jira PAT",
        "help": "Text file containing your Jira Personal Access Token.",
        "placeholder": r"C:\Users\you\secure\Jira_PAT.txt",
    },
    {
        "key": "confluence_pat_path",
        "label": "Confluence PAT",
        "help": "Text file containing your Confluence Personal Access Token.",
        "placeholder": r"C:\Users\you\secure\Confluence_PAT.txt",
    },
    {
        "key": "ms_client_secret_path",
        "label": "Microsoft client secret",
        "help": "Text file containing the Entra app-registration client "
                "secret. Defaults to ms_client_secret.txt in the app home.",
        "placeholder": r"C:\Users\you\AppData\Local\ManagerWorkbench\ms_client_secret.txt",
    },
]


def _status(path):
    if not path:
        return "⚪ Not set"
    return "🟢 Active" if os.path.exists(path) else "🔴 File not found"


tab_creds, tab_identity, tab_team, tab_profile = st.tabs(
    ["🔑 Credentials", "👤 Identity", "👥 Team", "💾 Profile"]
)

# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
with tab_creds:
    st.subheader("Active credentials")
    st.caption(
        "Each credential is loaded from a file path you control — the file can "
        "live anywhere on disk; only the path is saved, never the secret itself. "
        "These paths are **machine-local** (stored in `machine.json`) and are "
        "deliberately excluded from an exported profile."
    )

    # --- Overview: name / active status / file path in use ---------------- #
    rows = [
        {
            "Credential": c["label"],
            "Status": _status(cfg.get(c["key"], "")),
            "File path": cfg.get(c["key"], "") or "—",
        }
        for c in CREDENTIALS
    ]
    # Microsoft Graph authenticates via interactive device-code login rather
    # than a file path, so it has no editable row — but its live sign-in state
    # and token-cache location belong in the same overview.
    ms_signed_in = ms_is_signed_in()
    ms_account = ms_signed_in_account()
    rows.append(
        {
            "Credential": "Microsoft 365 (device-code login)",
            "Status": (
                f"🟢 Signed in — {ms_account}" if ms_signed_in and ms_account
                else "🟢 Signed in" if ms_signed_in
                else "⚪ Not signed in"
            ),
            "File path": user_profile.ms_token_cache_path(),
        }
    )
    st.dataframe(rows, width="stretch", hide_index=True)

    st.divider()

    # --- Microsoft 365 sign-in (device-code flow, fully in-app) ------------ #
    st.subheader("Microsoft 365 sign-in")
    st.caption(
        "Powers Outlook, Calendar, and Teams features. Sign in here once — the "
        "code and link appear below, so you never need the terminal."
    )
    if ms_signed_in:
        st.success(f"Signed in as {ms_account}." if ms_account else "Signed in.")
        if st.button("Sign out of Microsoft"):
            ms_sign_out()
            st.rerun()
    else:
        st.info("Not signed in.")
        if st.button("Sign in to Microsoft", type="primary"):
            try:
                flow = begin_device_flow()
            except Exception as e:
                st.error(f"Could not start sign-in: {e}")
            else:
                st.markdown(
                    "**1.** Open the Microsoft sign-in page, then **2.** enter this code:"
                )
                st.link_button("Open Microsoft sign-in", flow["verification_uri"])
                st.code(flow["user_code"], language=None)
                if flow.get("message"):
                    st.caption(flow["message"])
                with st.spinner("Waiting for you to finish signing in in your browser…"):
                    try:
                        complete_device_flow(flow)
                    except Exception as e:
                        st.error(f"Sign-in failed: {e}")
                    else:
                        st.success("Signed in to Microsoft.")
                        st.rerun()

    st.divider()

    # --- Edit paths ------------------------------------------------------- #
    with st.form("credential_paths"):
        st.subheader("Edit credential file paths")
        inputs = {
            c["key"]: st.text_input(
                c["label"],
                value=cfg.get(c["key"], ""),
                placeholder=c["placeholder"],
                help=c["help"],
            )
            for c in CREDENTIALS
        }
        submitted = st.form_submit_button("Save & Apply", type="primary")

    if submitted:
        # Validate before saving so the user gets immediate feedback.
        warnings = []
        for c in CREDENTIALS:
            path = inputs[c["key"]].strip()
            if path and not os.path.exists(path):
                warnings.append(f"{c['label']}: file not found at `{path}`")
            cfg[c["key"]] = path
        config.save_config(cfg)

        # Rebuild the cached Gemini client so new GCP credentials take effect.
        st.cache_resource.clear()

        if warnings:
            st.warning("Saved, but some paths don't resolve yet:\n- " + "\n- ".join(warnings))
        else:
            st.success("Credential paths saved and applied.")
        st.rerun()

    st.caption(
        "Microsoft Graph uses an interactive device-code login (no file path); "
        "its short-lived token is cached at the path shown above and refreshes "
        "automatically. The GCP, Jira, and Confluence credentials are read from "
        "the files you point to here."
    )

# --------------------------------------------------------------------------- #
# Identity — who you are and who you report to
# --------------------------------------------------------------------------- #
with tab_identity:
    st.subheader("Your identity")
    st.caption(
        "Used to attribute email action items to you, to decide what is being "
        "asked *of you* versus of someone else, and to address the 1:1 prep "
        "documents. This is per-profile data — it travels with your workbench."
    )

    identity = config.load_identity()
    if not config.identity_is_configured():
        st.info("Not set yet. Fill this in once and every page becomes personalized.")

    with st.form("identity_form"):
        col_you, col_mgr = st.columns(2)
        with col_you:
            st.markdown("**You**")
            user_name = st.text_input(
                "Your name",
                value=identity["user_name"] if config.identity_is_configured() else "",
                placeholder="Your first name",
                help="First name is enough — it is how the AI refers to you.",
            )
            user_email = st.text_input(
                "Your work email",
                value=identity["user_email"],
                placeholder="you@zebra.com",
                help="Used to match messages addressed to you when triaging the inbox.",
            )
        with col_mgr:
            st.markdown("**Your manager**")
            manager_name = st.text_input(
                "Manager's name",
                value=(
                    identity["manager_name"]
                    if identity["manager_name"] != "their manager" else ""
                ),
                placeholder="First and last name",
            )
            manager_email = st.text_input(
                "Manager's email",
                value=identity["manager_email"],
                placeholder="manager@zebra.com",
            )
            manager_title = st.text_input(
                "Manager's title / org (optional)",
                value=identity["manager_title"],
                placeholder="Director Engineering, ECRT",
                help=(
                    "Given to the AI so it pitches your 1:1 prep at the right "
                    "altitude. Leave blank and it simply won't assume one."
                ),
            )

        powerbi_url = st.text_input(
            "Manager dashboard URL (PowerBI)",
            value=identity["powerbi_dashboard_url"],
            placeholder="https://app.powerbi.com/Redirect?action=OpenReport&...",
            help=(
                "Opened as a convenience from the Manager 1:1 Prep page so you can "
                "export the Critical/Aged issues. Leave blank if you don't use one."
            ),
        )
        identity_saved = st.form_submit_button("Save identity", type="primary")

    if identity_saved:
        if not user_name.strip():
            st.error("Your name is required — the rest of the app writes in your voice.")
        else:
            config.save_identity(
                {
                    "user_name": user_name,
                    "user_email": user_email,
                    "manager_name": manager_name,
                    "manager_email": manager_email,
                    "manager_title": manager_title,
                    "powerbi_dashboard_url": powerbi_url,
                }
            )
            st.success("Identity saved.")
            st.rerun()

    st.divider()

    # Executive Translator audiences. The roles are fixed in the page; only WHO
    # sits in each one is per-user, so it is stored here rather than in source.
    st.subheader("Executive Translator audiences")
    st.caption(
        "Who sits in each leadership audience. Given to the AI so a brief is "
        "pitched at the actual people reading it. Optional — left blank, the "
        "translation targets the role alone."
    )
    saved_names = config.load_audience_names()
    with st.form("audience_names_form"):
        entered = {}
        for _aud in EXEC_AUDIENCES:
            entered[_aud["key"]] = st.text_input(
                _aud["label"],
                value=saved_names.get(_aud["key"], ""),
                placeholder="Comma-separated names",
                key=f"aud_{_aud['key']}",
            )
        audiences_saved = st.form_submit_button("Save audiences", type="primary")

    if audiences_saved:
        config.save_audience_names(entered)
        st.success("Audiences saved.")
        st.rerun()

# --------------------------------------------------------------------------- #
# Team directory (core roster + extended team, each with a function)
# --------------------------------------------------------------------------- #
with tab_team:
    st.subheader("Team directory")
    st.caption(
        "Identify your core team and the important extended team members along with "
        "their function. This context is fed to the AI across the app — for delegation, "
        "triage, owner attribution, and drafting — so it understands who does what."
    )

    _MEMBER_FIELDS = ["name", "function", "email", "core_id"]

    def _members_df(records, fallback_names=None):
        records = records or [
            {"name": n, "function": "", "email": "", "core_id": ""}
            for n in (fallback_names or [])
        ]
        df = pd.DataFrame(records)
        # Ensure every column exists and is string-typed: legacy records lack
        # email/core_id, which would otherwise come back as all-NaN float columns
        # that st.column_config.TextColumn refuses to edit.
        for col in _MEMBER_FIELDS:
            if col not in df.columns:
                df[col] = ""
        return df[_MEMBER_FIELDS].fillna("").astype(str)

    _MEMBER_COLS = {
        "name": st.column_config.TextColumn("Name", width="medium"),
        "function": st.column_config.TextColumn("Function / Role", width="medium"),
        "email": st.column_config.TextColumn("Email", width="medium"),
        "core_id": st.column_config.TextColumn("Core ID", width="small"),
    }

    st.markdown("**Core team**")
    core_edit = st.data_editor(
        _members_df(cfg.get("team_members"), cfg.get("team_roster", config.DEFAULT_TEAM_ROSTER)),
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        key="team_core_editor",
        column_config=_MEMBER_COLS,
    )

    st.markdown("**Extended team / key partners**")
    st.caption("Cross-functional partners, leadership, suppliers — anyone whose role adds context.")
    ext_edit = st.data_editor(
        _members_df(cfg.get("extended_members")),
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        key="team_ext_editor",
        column_config=_MEMBER_COLS,
    )

    if st.button("Save team directory", type="primary"):
        def _clean(df):
            out = []
            for r in df.fillna("").to_dict("records"):
                name = str(r.get("name", "")).strip()
                if name:
                    out.append(
                        {
                            "name": name,
                            "function": str(r.get("function", "")).strip(),
                            "email": str(r.get("email", "")).strip(),
                            "core_id": str(r.get("core_id", "")).strip(),
                        }
                    )
            return out

        core = _clean(core_edit)
        cfg["team_members"] = core
        cfg["extended_members"] = _clean(ext_edit)
        # Keep the names-only roster in sync for existing consumers (SPR delegate
        # dropdown, OoO roster, etc.).
        cfg["team_roster"] = [m["name"] for m in core]
        config.save_config(cfg)
        st.success("Team directory saved.")
        st.rerun()

    preview = config.team_context_block(cfg)
    if preview:
        with st.expander("Preview the context the AI will see"):
            st.code(preview, language=None)

# --------------------------------------------------------------------------- #
# Profile — the portability surface
# --------------------------------------------------------------------------- #
with tab_profile:
    active = user_profile.active_profile_id()
    st.subheader("Your workbench profile")
    st.caption(
        "Everything you accumulate — identity, team, trackers, and every cached "
        "analysis — lives in a single database file. Copy that one file and you "
        "have moved your entire workbench. Credentials and your Microsoft sign-in "
        "stay on this machine, so a profile is safe to share."
    )

    db_file = user_profile.db_path()
    size_mb = os.path.getsize(db_file) / (1024 * 1024) if os.path.exists(db_file) else 0
    col_a, col_b = st.columns([1, 2])
    col_a.metric("Active profile", active)
    col_a.metric("Size on disk", f"{size_mb:.1f} MB")
    col_b.markdown("**Database**")
    col_b.code(db_file, language=None)
    col_b.caption(f"All profiles live under `{user_profile.profiles_root()}`")

    with st.expander("What's inside this profile"):
        counts = db.table_counts()
        st.dataframe(
            [{"Table": t, "Rows": n} for t, n in sorted(counts.items()) if n],
            width="stretch",
            hide_index=True,
        )

    st.divider()

    # --- Switch / create -------------------------------------------------- #
    st.subheader("Switch profile")
    profiles = user_profile.list_profiles()
    ids = [p["id"] for p in profiles] or [active]
    chosen = st.selectbox(
        "Profile", ids, index=ids.index(active) if active in ids else 0
    )
    col_switch, col_new = st.columns(2)
    if col_switch.button(
        "Switch to this profile", disabled=(chosen == active), width="stretch"
    ):
        user_profile.switch_profile(chosen)
        # The Gemini client and the profile bootstrap are both cached resources
        # keyed to the old profile — drop them so the new database is opened.
        st.cache_resource.clear()
        st.rerun()

    with col_new.popover("Create a new profile", width="stretch"):
        new_name = st.text_input("Name", placeholder="chris-personal", key="new_profile")
        if st.button("Create", type="primary", key="do_create_profile"):
            try:
                pid = user_profile.create_profile(new_name)
            except ValueError as e:
                st.error(str(e))
            else:
                st.success(f"Created profile '{pid}'. Switch to it above to start using it.")
                st.rerun()

    st.divider()

    # --- Export / import --------------------------------------------------- #
    st.subheader("Move this workbench")
    col_exp, col_imp = st.columns(2)

    with col_exp:
        st.markdown("**Export**")
        st.caption(
            "Writes a consistent snapshot of the active profile — safe to run "
            "while the app is in use."
        )
        if os.path.exists(db_file):
            try:
                bundle = user_profile.export_profile(active)
                with open(bundle, "rb") as f:
                    st.download_button(
                        "Download workbench bundle",
                        data=f.read(),
                        file_name=f"{active}{user_profile.BUNDLE_SUFFIX}",
                        mime="application/octet-stream",
                        width="stretch",
                    )
            except Exception as e:
                st.error(f"Export failed: {e}")
        else:
            st.info("Nothing to export yet.")

    with col_imp:
        st.markdown("**Import**")
        st.caption(
            "Loads a bundle into a NEW profile — your current data is never "
            "overwritten."
        )
        uploaded = st.file_uploader(
            "Workbench bundle",
            type=[user_profile.BUNDLE_SUFFIX.lstrip("."), "db"],
            label_visibility="collapsed",
        )
        import_name = st.text_input("Name for the imported profile", placeholder="imported")
        if st.button("Import", disabled=not (uploaded and import_name.strip())):
            tmp_path = os.path.join(user_profile.home(), "_import.tmp")
            try:
                with open(tmp_path, "wb") as f:
                    f.write(uploaded.getbuffer())
                pid = user_profile.import_profile(tmp_path, import_name)
            except Exception as e:
                st.error(f"Import failed: {e}")
            else:
                st.success(f"Imported as profile '{pid}'. Switch to it above.")
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

    st.divider()

    # --- Delete ------------------------------------------------------------ #
    with st.expander("⚠️ Delete a profile"):
        st.caption(
            "Permanently deletes that profile's data. You cannot delete the "
            "profile you are using, or the last one remaining."
        )
        others = [p["id"] for p in profiles if p["id"] != active]
        if not others:
            st.info("No other profiles to delete.")
        else:
            victim = st.selectbox("Profile to delete", others, key="delete_profile")
            confirm = st.text_input(
                f"Type **{victim}** to confirm", key="delete_confirm"
            )
            if st.button("Delete permanently", disabled=(confirm != victim)):
                try:
                    user_profile.delete_profile(victim)
                except ValueError as e:
                    st.error(str(e))
                else:
                    st.success(f"Deleted profile '{victim}'.")
                    st.rerun()
