# ME Manager Agent

A **Human-in-the-Loop** orchestration tool for the ECRT ME Engineering Manager.
It integrates Jira, MS Graph (Outlook / Teams / Calendar), Confluence, and
Vertex AI (Gemini) behind a multi-page Streamlit UI. The agent **drafts,
summarizes, and proposes** — it never auto-sends mail, posts to Teams, or
closes Jira tickets. Every outbound action requires an explicit click and
defaults to creating an Outlook *draft*.

## Modules

| Page | Module | Purpose |
|------|--------|---------|
| Staff Meeting Builder | A | Pull tagged Outlook emails + files, dedupe vs. last month's Confluence notes, ZBRA earnings grounding, privacy/tone filtering. |
| Executive Translator | C | Reformat L3 technical artifacts into the "What / So What / Now What" (Impact / Risk / Timeline) leadership brief. |
| Admin & OOO Hub | F | Consolidate OOO/VTO/PTO into a daily availability digest; regional PTO approval deep-links. |

## Project layout

```
ecrt_agent/
├── app.py             # st.navigation router + profile/GCP/Gemini bootstrap
├── config.py          # environment constants, identity accessors, merged config view
├── user_profile.py    # where data lives: app home, machine settings, profile CRUD
├── db.py              # the profile's SQLite database: schema, doc_store, migration
├── store.py           # JSON document load/save (backed by doc_store)
├── email_db.py        # email caches      ─┐
├── jira_db.py         # Jira analysis      ├─ all tables live in ONE workbench.db
├── one_on_one_db.py   # 1:1 prep caches   ─┘
├── ms_auth.py         # get_ms_token() — MSAL device-code flow
├── api_helpers.py     # Graph (mail/teams/calendar), Jira, Confluence, file parsing
├── llm_prompts.py     # all Gemini prompts + generate() wrapper
├── pages/             # one file per module
└── requirements.txt
```

**No data is written into this folder.** Everything you accumulate lives in your
profile — see *Your data* below.

## Setup

1. **Install dependencies**
   ```powershell
   pip install -r requirements.txt
   ```

2. **Point the app at your credential files.** Store the secrets **outside** the
   app directory (e.g. a secured user-profile folder), then open the **Settings**
   page (⚙️ in the sidebar) and paste the absolute path to each:
   - GCP service-account JSON (Vertex AI)
   - Jira Personal Access Token file (single line)
   - Confluence Personal Access Token file (single line)

   Settings shows a 🟢/🔴 status for each path and saves only the *paths* to
   machine-local `machine.json` — the secrets themselves never enter the project
   tree, and the paths never travel with an exported profile.
   The Gemini client rebuilds automatically when you change the GCP path.

   *(Convenience fallback: if you do drop files named `Jira_PAT.txt`,
   `Confluence_PAT.txt`, or the GCP JSON in the project root, they are
   auto-detected — but the Settings page is the recommended workflow.)*

3. **Microsoft Graph** uses MSAL device-code login, driven from Settings →
   Credentials (no terminal needed). The token is cached in `ms_token_cache.bin`
   in your app home directory. It signs in against the dedicated Manager
   Workbench app registration set as `MS_CLIENT_ID` / `MS_TENANT_ID` in
   `config.py`; if IT reprovisions the app, update those two values — the cached
   token is keyed by client ID, so everyone simply signs in once more.

4. **Tell the app who you are.** Settings → **Identity**: your name and email, and
   your manager's. Nothing is personalized until this is filled in.

5. **Run**
   ```powershell
   streamlit run app.py
   ```

## Your data

Everything you accumulate — identity, team directory, trackers, and every cached
analysis — lives in **one SQLite file** inside your profile:

```
%LOCALAPPDATA%\ManagerWorkbench\        (override with $MANAGER_WORKBENCH_HOME)
├── machine.json                        active profile + credential paths (machine-local)
├── ms_token_cache.bin                  Microsoft token (machine-local)
└── profiles/<name>/workbench.db        <- copy this and you've moved your workbench
```

Credentials and the Microsoft token are deliberately **left behind** — a profile
carries no secrets, so it is safe to copy, back up, or hand to a colleague.

- **Move to a new machine:** Settings → Profile → *Download workbench bundle*, then
  *Import* on the other machine. Re-point the credential paths and sign in again.
- **Multiple profiles:** create and switch from the same tab; each is fully isolated.
- **Upgrading an existing install:** the first launch imports the old repo-root data
  files automatically and renames the originals to `*.migrated`. Nothing is deleted.

## Configuration notes

- **Team directory**: edit on the **Settings → Team** page. A new profile starts empty.
- **PTO approval links** (Module F): edit `PTO_APPROVAL_LINKS` in `config.py`
  with the real US / Canada / Taiwan approval URLs.
- **Gemini model**: `MODEL_NAME` in `config.py` (default `gemini-2.5-pro`).

## Human-in-the-Loop guardrails

- **No module currently sends anything.** Since the SPR & Escalation Handler and
  the Say/Do Tracker were removed, nothing in the app calls `send_mail()`,
  `create_mail_draft()`, or `post_teams_message()` — every remaining page reads,
  analyzes, and drafts on screen only.
- Those helpers remain in `api_helpers.py` for future modules. If one is wired up
  again, keep the original contract: `send_mail(..., draft=True)` so outbound mail
  lands as an Outlook **draft**, and Teams posts only from an explicit "Send" click.
- No module closes, transitions, or comments on Jira tickets automatically.
