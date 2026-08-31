# ME Manager Agent — Documentation

A Human-in-the-Loop (HITL) desktop assistant for the ECRT Mechanical Engineering
manager. It integrates **Microsoft 365** (Outlook / Calendar / Teams), **Jira**,
**Confluence**, and **Google Vertex AI (Gemini)** behind a multi-page Streamlit UI.
The agent **drafts, summarizes, extracts, and proposes** — it never auto-sends mail,
posts to Teams, or closes tickets. Every outbound action requires an explicit click
and defaults to creating a draft.

---

## 1. Theory of Operations

### Architecture
- **UI / router:** `app.py` is the Streamlit entry point. It sets page config, builds
  one shared Gemini client, loads config fresh each run into `st.session_state`, and
  registers all pages with `st.navigation` grouped by role.
- **Pages:** each file in `pages/` is one tool/module. Pages read the shared client via
  `st.session_state["gemini_client"]` and stop with a friendly error if it's missing.
- **Shared libraries (project root):**
  - `config.py` — environment constants, the doc-store keys, the identity accessors,
    the merged config view (machine + profile), and the Gemini client factory.
  - `user_profile.py` — where data lives: the app home directory, machine-local
    settings, and profile create/switch/export/import.
  - `db.py` — the single SQLite database behind the active profile: connections,
    schema creation, the `doc_store` key/value table, and the one-time migration
    from the old repo-root layout.
  - `llm_prompts.py` — every Gemini prompt builder plus the `generate()` wrapper.
  - `api_helpers.py` — all outbound integrations (MS Graph, Jira, Confluence, carrier
    tracking) and file/text extraction helpers.
  - `ms_auth.py` — Microsoft sign-in (MSAL device-code flow, in-app).
  - `email_db.py` — SQLite store for the email tools (analysis cache, projects,
    dispositions, shipments).
  - `store.py` — load/save for the lighter JSON documents, backed by `doc_store`.
  - `ooo_logic.py` — pure (testable) reconciliation/coverage math for OoO.
  - `ui_placeholders.py` — shared "under construction" renderer for planned modules.

### How the AI is used
- **Provider:** Google **Gemini** on **Vertex AI** (model `gemini-2.5-pro`), created by
  `config.get_gemini_client()` and shared across pages.
- **Pattern:** each feature builds a prompt in `llm_prompts.py`, calls
  `P.generate(client, contents, temperature=...)`, and (for structured features) parses
  a single JSON object out of the reply. Image-heavy inputs are avoided — PDFs and other
  attachments are reduced to **text** before being sent, to stay within token limits.
- **Team context:** the team directory (names, functions, emails, IDs) is injected into
  the major prompts so the model understands "who's who" for ownership, delegation, and tone.

### Authentication & security
- **Microsoft 365:** MSAL **device-code** flow against the Zebra tenant app
  registration. Sign-in happens **in the app** (Settings → Credentials shows a code +
  link); the short-lived token is cached in `ms_token_cache.bin` **in the app home
  directory** — machine-local, never inside a profile — and refreshed silently.
- **Jira / Confluence:** Personal Access Tokens (PATs) read from files whose **paths**
  you set in Settings — the secrets never enter the project tree, and the paths are
  stored in machine-local `machine.json`, so they never travel with an exported profile.
- **Google Vertex AI:** a service-account JSON whose path you set in Settings.
- SSL verification is disabled on outbound requests to work behind the Zebra corporate
  proxy.

### Data storage — one portable file per user

Nothing is written into the repo. Everything a user accumulates lives in a single
SQLite database inside their **profile**, so a workbench can be backed up, moved to a
new machine, or handed to someone else by copying one file.

```
%LOCALAPPDATA%\ManagerWorkbench\        <- or $MANAGER_WORKBENCH_HOME
├── machine.json                        machine-local: active profile + credential PATHS
├── ms_token_cache.bin                  machine-local: Microsoft OAuth token
└── profiles/
    └── <profile_id>/
        └── workbench.db                THE portable artifact
```

**Machine-local vs. portable** is the organising rule. Secrets and the paths to them
describe *this computer*, so they stay in `machine.json` and are deliberately excluded
from an exported profile — which is what makes a profile safe to share.

`workbench.db` contains:

| Table(s) | Purpose |
|----------|---------|
| `doc_store` | Key/value JSON documents: `identity`, `team`, `email_actions`, `ooo_settings`, `one_on_one_meetings`, `manager_prep_runs`, `manager_manual_topics`, `special_projects` |
| `thread_context`, `email_analysis_cache`, `email_projects`, `thread_summaries`, `project_themes`, `email_dispositions`, `conversation_dispositions`, `shipments` | Email tools (`email_db.py`) |
| `spr_analysis` | Delta-aware Jira analysis cache (`jira_db.py`) |
| `report_items`, `report_prep`, `report_projects`, `project_actions` | 1:1 Meeting Prep (`one_on_one_db.py`) |
| `profile_meta` | Schema version and migration provenance |

**Migration.** On first start after the upgrade, `db.migrate_legacy()` imports the old
repo-root files (6 JSON files + 3 `.db` files + `app_config.json`) into the active
profile, lifts credential paths into `machine.json`, and renames each original to
`*.migrated` rather than deleting it. It is guarded by `profile_meta.legacy_migrated`,
so it runs exactly once and can never overwrite newer in-profile data.

**Multiple profiles.** Settings → Profile lists every profile and lets you create,
switch, rename, delete, export, and import. Export uses SQLite `VACUUM INTO`, which
takes a consistent snapshot even while the app has the database open.

### Caching philosophy
- **Emails:** analysis is cached permanently per message id; re-analysis only happens via
  the "Bypass cache" toggle. Thread/project context accumulates over time.
- **Jira:** analysis cached per ticket, keyed by the ticket's `updated` time and a schema
  version (bumping the version auto-regenerates stale results).

---

## 2. Navigation (role-based)

The sidebar is organized by how the manager works, not by build phase:

- **👤 Individual Execution** — Email Action Identifier, Executive Translator
- **🛠️ Team Operations** — Timeline & Schedule Management*, Staff Meeting Builder,
  Jira State Tracker
- **👥 People Management** — OoO & FTO Management, Talent & HR Planning*, Individual
  Development Planning (IDP)*, Compensation Planning*
- **⚙️ Setup** — Settings

\* = placeholder ("under construction") — visible in the nav, not yet built.

---

## 3. Modules

### 📥 Email Action Identifier (`pages/0_email_actions.py`)
Pulls recent inbox mail and turns it into actionable, organized output. Tabs:

- **🔍 Identify Actions (conversation-based):** fetches mail for a timeframe
  (15 min … 24 hr, Today, or a **Custom** typed interval), analyzes each email, then
  **groups emails into conversations**. For each un-dispositioned thread it produces a
  consolidated summary, key points, pending tasks, and a single **suggested action**.
  You apply **one disposition to the whole conversation** — Add to Tracker, Read–No
  Action, Delegated, Follow-up later, or Ignore — which updates every email in the
  thread. A "Show all" toggle reveals already-handled conversations. ("Bypass cache"
  forces re-analysis.)
- **📋 Email Action Tracker:** a persistent, editable table of actions you've filed
  (Origin, Priority, Action, Owner, dates, Next Step, Suggested Response) with a Done
  checkbox that stamps the completion date on save.
- **🗂️ Projects & Themes:** emails are auto-tagged to a **project**; this tab lists
  projects with email counts and, on demand, synthesizes the **common themes / type of
  work** and flags repetitive work. Includes a rename/merge tool to consolidate labels.
- **📦 Shipments (Shipping Sample Tracker):** detects shipments in emails and tracks
  them in a table keyed (uniquely) by **tracking number** — duplicate emails merge
  rather than duplicate. Columns include Case, SPR (clickable Jira link), Sender, Date
  Sent, Contents, Carrier, Tracking (clickable link), and Shipping Status. Status is
  best-effort refreshed from the carrier's public tracking page (on open if stale, or
  via a Refresh button); a Save button commits manual edits.

### 🎯 Executive Translator (`pages/3_exec_translator.py`)
Translates deep ME engineering content for **three leadership audiences simultaneously**
(Senior Director Engineering; Business Unit GM; SVP EMC). Each audience gets a tailored
"What / So What / Now What" brief plus three **Likely Follow-Up Questions**, a per-
audience instruction box (e.g. "limit to 3 sentences"), and the option to **scrub
Outlook** for the subject to add email context. PDFs can be uploaded.

### 🏗️ Staff Meeting Builder (`pages/1_staff_meeting.py`)
Aggregates tagged Outlook emails (+ uploaded files) for a date range, dedupes against
last month's Confluence notes, optionally grounds in ZBRA earnings via Google Search,
applies privacy/tone rules, and compiles structured staff-meeting notes against your
agenda outline.

### 📊 Jira State Tracker (`pages/7_jira_state_tracker.py`)
Determines the **true** engineering state of SPR tickets from description, full
timestamped comment history, **all Jira fields** (including custom fields like *Root
Cause Description*), and **selected attachments** (text is stripped from PDFs/Office
files — images ignored — to control tokens). For each ticket it produces:
- the current **lifecycle phase** (6-stage stepper) with rationale;
- **root-cause** and **re-design** status;
- an interactive, color-coded **milestone timeline** with draggable labels;
- **schedule-slip** detection (dates restated across comments → days slipped);
- **open next steps** and **action-item audits** (comments + attachments);
- a **5-Whys Root Cause** section (flags "incomplete/insufficient" when the chain can't
  be completed);
- a **fishbone (Ishikawa)** diagram over the 6 M's (uninvestigated bones show "Not
  Investigated");
- a textual digest.
You pick tickets from a JQL/filter list (multi-select), and results are cached per ticket.

### 🌴 OoO & FTO Management (`pages/8_ooo_management.py`)
Reconciles two OoO sources that both arrive in the manager's mailbox/calendar: the HR
system **approval emails** (filtered by a configurable sender) and **calendar invites**
(attributed to a member via the organizer). Tabs:
- **Reconciliation:** pick a team member → table of requests vs. calendar with a **Match
  Status** (matched / partial / no-approval / no-calendar). Matching is done at day
  granularity, so multi-day-vs-single-day representations reconcile correctly.
- **Upcoming PTO Summary:** per-member upcoming days + a Gantt timeline.
- **Coverage Alerts:** flags days where N+ members are out (configurable threshold).
- **PTO Approvals:** deep links to the regional HR systems.

### Placeholders (under construction)
- **🗓️ Timeline & Schedule Management** — SPR timelines, schedule management, PO requests.
- **🌱 Talent & HR Planning** — talent/succession planning, career pathing.
- **🧭 Individual Development Planning (IDP)** — development goals & progress per report.
- **💰 Compensation Planning** — merit, compensation, performance reviews.

### ⚙️ Settings (`pages/settings.py`)
- **🔑 Credentials:** an at-a-glance table of every credential (GCP, Jira PAT,
  Confluence PAT, Microsoft 365) showing active status and the file path in use; edit
  the file paths; and the **in-app Microsoft sign-in** (device-code, no terminal needed).
  Machine-local — these never travel with a profile.
- **👤 Identity:** your name and email, your manager's name and email, and your PowerBI
  dashboard URL. These drive action attribution, "was this asked of me?", and the 1:1
  prep documents. Profile data.
- **👥 Team:** the **team directory** — core team and extended team/partners, each with
  Name, Function/Role, Email, and Core ID. This context is fed to the AI across the app.
- **💾 Profile:** the portability surface — the active profile's database path, size and
  contents; create / switch / rename / delete profiles; and **export** or **import** a
  complete workbench as a single `.mwb` file.

---

## 4. Running the app

```powershell
pip install -r requirements.txt
streamlit run app.py
```

First-time setup (in **Settings**):
1. Fill in **Identity** — your name and email, and your manager's. Nothing is
   personalized until this is set, and the sidebar nags you until it is.
2. Set the **GCP service-account JSON** path (enables the Gemini client).
3. Set the **Jira** and **Confluence** PAT file paths if you use those tools.
4. **Sign in to Microsoft** (Credentials tab) for any Outlook/Calendar/Teams feature.
5. Fill in the **Team** directory.

Steps 1 and 5 are profile data and come along if you export your workbench; steps 2–4
are machine-local and have to be redone on a new computer.

### Moving your workbench to another machine
1. Settings → **Profile** → **Download workbench bundle** (a `.mwb` file).
2. On the new machine, install and run the app, then Settings → Profile → **Import**.
3. Switch to the imported profile, re-point the credential paths, and sign in to
   Microsoft. Your identity, team, trackers, and every cached analysis are already there.

---

## 5. Notes & limitations
- **HITL everywhere:** the agent drafts and proposes; sending mail / posting to Teams is
  always an explicit, confirmed action and defaults to a draft.
- **Carrier status** is best-effort — FedEx/UPS/DHL pages are JavaScript apps, so the
  auto-status may read "Unknown"; the clickable tracking link always works.
- **"Daily" refreshes** (e.g. shipment status) happen on first visit each day plus a
  manual button — Streamlit has no always-on background scheduler.
- **Scanned/image-only PDFs** have no text layer to extract (OCR would be a separate add).
- **Drag-positioned** Jira timeline labels are view-only and reset on rerun.
