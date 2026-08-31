"""
Configuration, identity, and credential bootstrap for the ECRT ME Team
Management Agent.

Architecture note: this module used to own a dozen filesystem paths anchored to
PROJECT_ROOT, plus the user's name, email, and manager as source constants. Both
are gone. Configuration is now split three ways by *who owns it*:

  - Environment constants (below)  — same for everyone at Zebra; still code.
  - Machine-local settings         — credential FILE PATHS + the MSAL token
                                     cache. Live in machine.json next to the
                                     profiles; never travel. See user_profile.py.
  - Profile data                   — identity, team directory, and every tracker.
                                     Live in the active profile's workbench.db.
                                     See db.py.

PROJECT_ROOT survives only so the one-time migration can find the old files.

`load_config()` / `save_config()` keep their original signatures and present a
single merged dict, so callers (app.py, pages/settings.py) did not have to change
when the storage split underneath them.
"""
import os
import copy
import glob

from google import genai

import store
import user_profile

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# --- Credential filenames (dropped next to this file, auto-detected below) --- #
JIRA_PAT_FILENAME = "Jira_PAT.txt"
CONFLUENCE_PAT_FILENAME = "Confluence_PAT.txt"
MS_CLIENT_SECRET_FILENAME = "MS_Client_Secret.txt"

# --- Hard-coded environment constants (never vary per user) ------------------ #
JIRA_BASE_URL = "https://jira.zebra.com"
CONFLUENCE_DOMAIN = "https://confluence.zebra.com"

GCP_PROJECT = "its-compute-emc-zaip-p"
# Service-account key files are named "<project>-<key id>.json". Matched as a
# glob so the key id stays out of source: any key for the project is picked up.
GCP_CREDENTIALS_GLOB = f"{GCP_PROJECT}-*.json"
GCP_LOCATION = "us-central1"
MODEL_NAME = "gemini-2.5-pro"

# Aliases for compatibility with the original StaffMeetingBuilder baseline,
# which referenced PROJECT_ID / LOCATION / SERVICE_ACCOUNT_PATH directly.
PROJECT_ID = GCP_PROJECT
LOCATION = GCP_LOCATION

# --- Microsoft Graph (MSAL device-code flow) --------------------------------- #
# Uses the Manager Workbench app registration provisioned in the Zebra tenant,
# NOT the generic MS Graph PowerShell public client — the generic
# client/`common` tenant is blocked on Zebra's locked-down tenant.
# `.default` requests every delegated permission already consented to this app
# registration; whether Mail.Send / Calendars.Read / Chat.ReadWrite succeed
# depends on those permissions being granted to the app reg in Azure AD.
#
# The MSAL cache is keyed by client ID, so changing MS_CLIENT_ID silently
# invalidates any existing sign-in: get_ms_token() raises MSAuthRequired and the
# user signs in again via Settings -> Credentials (or `python ms_reauth.py`).
MS_CLIENT_ID = "4b318f75-0582-4881-a76e-6172a38a9d15"
MS_TENANT_ID = "4d3d260a-9c40-4306-8dac-0d64717039ec"
MS_AUTHORITY = f"https://login.microsoftonline.com/{MS_TENANT_ID}"
MS_SCOPES = ["https://graph.microsoft.com/.default"]

# --------------------------------------------------------------------------- #
# doc_store keys
#
# These were filesystem paths (EMAIL_ACTIONS_FILE = .../email_actions.json). They are
# now names of rows in the profile database, and are named *_KEY so no call site
# can mistake them for something that exists on disk.
# --------------------------------------------------------------------------- #
EMAIL_ACTIONS_KEY = "email_actions"       # Phase 0 — Email Action Tracker
OOO_SETTINGS_KEY = "ooo_settings"         # OoO — HR sender, coverage threshold
ONE_ON_ONE_KEY = "one_on_one_meetings"    # 1:1 — last prep date per report
MANAGER_PREP_KEY = "manager_prep_runs"    # Manager 1:1 — saved run history
MANAGER_TOPICS_KEY = "manager_manual_topics"  # Manager 1:1 — standing topics
SPECIAL_PROJECTS_KEY = "special_projects"  # Manager 1:1 — email-sift projects
IDENTITY_KEY = "identity"                 # who the user and their manager are
AUDIENCE_NAMES_KEY = "audience_names"     # Exec Translator — who each audience is
TEAM_KEY = "team"                         # core + extended team directory

# --- Shipping Sample Tracker (Email Action Identifier) ----------------------- #
# Public tracking URL templates per carrier ({tn} = tracking number). Used for the
# clickable "Track" link and best-effort status fetch.
CARRIER_TRACKING_URLS = {
    "fedex": "https://www.fedex.com/fedextrack/?trknbr={tn}",
    "ups": "https://www.ups.com/track?tracknum={tn}",
    "dhl": "https://www.dhl.com/us-en/home/tracking.html?tracking-id={tn}",
}
SHIPMENT_STATUS_MAX_AGE_HOURS = 24  # auto-refresh statuses older than this


def carrier_tracking_url(carrier, tracking_number):
    """Public tracking URL for a carrier+tracking number, or '' if unknown carrier."""
    if not carrier or not tracking_number:
        return ""
    template = CARRIER_TRACKING_URLS.get(carrier.strip().lower())
    return template.format(tn=str(tracking_number).strip()) if template else ""


# --- Phase 3: Jira State Tracking & Analysis --------------------------------- #
# Canonical ECRT engineering lifecycle phases (order matters for the stepper).
JIRA_PHASES = [
    "Testing / Investigation / Root Cause",
    "Re-Design",
    "Tooling / Manufacturing Fabrication",
    "Engineering & Qualification Testing",
    "EC & Release",
    "Manufacturing Cut-In",
]
# The "SPR Priority (Category)" shown in the 1:1 prep table is a custom Jira field.
# fetch_jira_ticket_full() returns it inside `extra_fields` labelled by display name;
# match any of these (case-insensitive) to read its value.
SPR_CATEGORY_FIELDS = ("Category", "customfield_21506")
# Logical sort order for the Category values (most→least severe). Unknown values
# sort after these. Adjust once the real category vocabulary is confirmed.
SPR_PRIORITY_ORDER = ["P1", "P2", "P3", "P4", "Critical", "High", "Medium", "Low"]

# --- Module F: PTO approval deep-links (HR systems are not API-integrated) --- #
PTO_APPROVAL_LINKS = {
    "US": "https://workday.zebra.com/pto/approvals",
    "Canada": "https://workday.zebra.com/pto/approvals?region=CA",
    "Taiwan": "https://workday.zebra.com/pto/approvals?region=TW",
}


# --------------------------------------------------------------------------- #
# Identity — profile data, served as module attributes
#
# Every consumer writes `config.USER_NAME` / `config.MANAGER_NAME` from inside a
# function (llm_prompts.py, pages/14_manager_1on1_prep.py). A PEP 562 module
# __getattr__ therefore lets identity become per-profile data without touching a
# single call site: the name is resolved from the active profile at the moment it
# is read, not frozen when this module is imported.
# --------------------------------------------------------------------------- #
IDENTITY_FIELDS = (
    "user_name",
    "user_email",
    "manager_name",
    "manager_email",
    "manager_title",
    "powerbi_dashboard_url",
)

# Neutral placeholders so an unconfigured profile renders sensibly instead of
# producing "You are 's assistant". app.py surfaces a Settings prompt when unset.
_IDENTITY_DEFAULTS = {
    "user_name": "the user",
    "user_email": "",
    "manager_name": "their manager",
    "manager_email": "",
    # Free text, e.g. "Director Engineering, ECRT". Blank by default: the
    # prompts drop the parenthetical entirely rather than assert a title we
    # do not know.
    "manager_title": "",
    "powerbi_dashboard_url": "",
}


def load_identity():
    """Return the active profile's identity dict, merged over neutral defaults."""
    identity = dict(_IDENTITY_DEFAULTS)
    stored = store.load_json(IDENTITY_KEY, {}) or {}
    if isinstance(stored, dict):
        identity.update(
            {k: v for k, v in stored.items() if k in _IDENTITY_DEFAULTS and v}
        )
    return identity


def save_identity(identity):
    """Persist identity to the active profile. Only known fields are kept."""
    store.save_json(
        IDENTITY_KEY,
        {k: str(identity.get(k, "") or "").strip() for k in IDENTITY_FIELDS},
    )


def identity_is_configured():
    """True once the user has told us who they are — drives the setup nudge."""
    stored = store.load_json(IDENTITY_KEY, {}) or {}
    return bool(isinstance(stored, dict) and str(stored.get("user_name", "")).strip())


def __getattr__(name):
    """Serve USER_NAME / USER_EMAIL / MANAGER_NAME / MANAGER_EMAIL /
    POWERBI_DASHBOARD_URL from the active profile (PEP 562).

    Only reached for names absent from module globals, so it costs nothing for
    ordinary constants."""
    key = name.lower()
    if key in _IDENTITY_DEFAULTS:
        return load_identity()[key]
    if name == "MS_TOKEN_CACHE":  # kept for callers that still ask config
        return user_profile.ms_token_cache_path()
    raise AttributeError(f"module 'config' has no attribute {name!r}")


# --------------------------------------------------------------------------- #
# Special projects (Manager 1:1 email sift)
# --------------------------------------------------------------------------- #
DEFAULT_SPECIAL_PROJECTS = []


def load_special_projects():
    """Load the configured special projects (list of {subject, keywords}) from the
    active profile. Returns an empty list when nothing has been saved (no
    hard-coded topics). Drops blank/invalid rows and normalizes fields to strings."""
    projects = store.load_json(SPECIAL_PROJECTS_KEY, DEFAULT_SPECIAL_PROJECTS)
    if not isinstance(projects, list):
        projects = DEFAULT_SPECIAL_PROJECTS
    out = []
    for p in projects:
        if not isinstance(p, dict):
            continue
        subject = str(p.get("subject", "")).strip()
        if subject:
            out.append({"subject": subject, "keywords": str(p.get("keywords", "")).strip()})
    return out


def save_special_projects(projects):
    """Persist the special-projects list to the active profile."""
    store.save_json(SPECIAL_PROJECTS_KEY, projects)


# --------------------------------------------------------------------------- #
# Executive Translator audience names
#
# WHO each leadership audience is, is personal data about third parties, so it
# lives in the profile and never in source. The audiences themselves (role and
# focus) are generic and stay in pages/3_exec_translator.py.
# --------------------------------------------------------------------------- #
def load_audience_names():
    """Return {audience_key: "Name, Name"} from the active profile. Empty dict
    when unset — the prompt then omits the names entirely rather than inventing
    an audience, the same way a blank manager_title is handled."""
    names = store.load_json(AUDIENCE_NAMES_KEY, {}) or {}
    if not isinstance(names, dict):
        return {}
    return {str(k): str(v).strip() for k, v in names.items() if str(v).strip()}


def save_audience_names(names):
    """Persist the audience-name map to the active profile."""
    store.save_json(
        AUDIENCE_NAMES_KEY,
        {str(k): str(v or "").strip() for k, v in (names or {}).items()},
    )


# --------------------------------------------------------------------------- #
# Team directory + credential paths (the merged config view)
# --------------------------------------------------------------------------- #
# A fresh profile starts with NO roster. Seeding one manager's reports into every
# other manager's install was one of the things that made the app un-shareable.
DEFAULT_TEAM_ROSTER = []
DEFAULT_TEAM_MEMBERS = []

DEFAULT_CONFIG = {
    "jira_pat_path": "",
    "confluence_pat_path": "",
    "gcp_credentials_path": "",
    "ms_client_secret_path": "",
    "team_roster": DEFAULT_TEAM_ROSTER,
    "team_members": DEFAULT_TEAM_MEMBERS,
    "extended_members": [],
}

# Which keys of the merged view belong to which store.
_PROFILE_KEYS = ("team_roster", "team_members", "extended_members")


def _auto_detect_credentials(machine):
    """Fill credential paths from the project root when the configured path is
    empty or points to a missing file. Lets the user drop the known credential
    files next to the app and have them picked up with no manual setup.
    Returns True if any path was changed."""
    candidates = {
        "jira_pat_path": JIRA_PAT_FILENAME,
        "confluence_pat_path": CONFLUENCE_PAT_FILENAME,
        "gcp_credentials_path": GCP_CREDENTIALS_GLOB,
        "ms_client_secret_path": MS_CLIENT_SECRET_FILENAME,
    }
    changed = False
    for key, pattern in candidates.items():
        path = machine.get(key, "")
        if not path or not os.path.exists(path):
            # glob() covers the plain filenames too: with no wildcard it simply
            # returns the one path if it exists. Sorted so a project with more
            # than one key file on disk resolves deterministically.
            matches = sorted(glob.glob(os.path.join(PROJECT_ROOT, pattern)))
            if matches and machine.get(key, "") != matches[0]:
                machine[key] = matches[0]
                changed = True
    return changed


def load_config():
    """Return the merged configuration: machine-local credential paths plus the
    active profile's team directory.

    Same shape and signature as the original file-backed version, so pages that
    do `cfg = config.load_config()` needed no change. Auto-detected credential
    paths are written back to machine.json (only when they actually changed)."""
    config = copy.deepcopy(DEFAULT_CONFIG)

    machine = user_profile.load_machine()
    if _auto_detect_credentials(machine):
        user_profile.save_machine(machine)
    for key in user_profile.MACHINE_KEYS:
        config[key] = machine.get(key, "")

    team = store.load_json(TEAM_KEY, {}) or {}
    if isinstance(team, dict):
        for key in _PROFILE_KEYS:
            value = team.get(key)
            if isinstance(value, list):
                config[key] = value

    # Keep the names-only roster consistent with the structured directory, which
    # is what the SPR delegate dropdown and OoO roster read.
    if config["team_members"] and not config["team_roster"]:
        config["team_roster"] = [
            m.get("name", "") for m in config["team_members"] if m.get("name")
        ]

    return config


def save_config(config_data):
    """Split a merged config dict back to its two homes: credential paths to
    machine.json, team directory to the active profile's database."""
    machine = user_profile.load_machine()
    touched_machine = False
    for key in user_profile.MACHINE_KEYS:
        if key in config_data:
            machine[key] = config_data.get(key, "")
            touched_machine = True
    if touched_machine:
        user_profile.save_machine(machine)

    if any(k in config_data for k in _PROFILE_KEYS):
        team = store.load_json(TEAM_KEY, {}) or {}
        if not isinstance(team, dict):
            team = {}
        for key in _PROFILE_KEYS:
            if key in config_data:
                team[key] = config_data[key]
        store.save_json(TEAM_KEY, team)


def read_token_file(path):
    """Read a single-line PAT/token from a file. Returns '' if missing."""
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        print(f"[Config] Failed to read token file {path}: {e}")
        return ""


def ms_client_secret():
    """Return the Microsoft app-registration client secret, or '' if unset.

    Resolution order, most specific first:
      1. MS_CLIENT_SECRET environment variable (CI / one-off overrides).
      2. The file at machine.json's `ms_client_secret_path`.
      3. ms_client_secret.txt in the app home, beside the MSAL token cache.

    The value is a live credential, so like the PATs it is never stored in code
    or in a profile — only machine-local, and read fresh on each call so a
    rotated secret takes effect without restarting the process."""
    env = os.environ.get("MS_CLIENT_SECRET", "").strip()
    if env:
        return env

    path = user_profile.load_machine().get("ms_client_secret_path", "")
    secret = read_token_file(path)
    if secret:
        return secret

    return read_token_file(
        os.path.join(user_profile.home(), "ms_client_secret.txt")
    )


def initialize_gcp_auth(credentials_path):
    """Set GOOGLE_APPLICATION_CREDENTIALS if a valid path is provided.
    Returns True on success."""
    if not credentials_path:
        return False
    if not os.path.exists(credentials_path):
        print(f"[Auth] GCP credentials file not found: {credentials_path}")
        return False
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_path
    return True


def get_gemini_client(project=GCP_PROJECT, location=GCP_LOCATION):
    """Create a Vertex AI Gemini client."""
    return genai.Client(vertexai=True, project=project, location=location)


def team_context_block(cfg=None):
    """Render the team directory (core + extended members with their functions)
    as a compact text block for injecting into LLM prompts. Pass a loaded `cfg`
    to avoid a disk read; otherwise it loads config. Returns "" if empty."""
    if cfg is None:
        cfg = load_config()
    core = cfg.get("team_members") or [
        {"name": n, "function": ""} for n in cfg.get("team_roster", [])
    ]
    extended = cfg.get("extended_members") or []

    def _fmt(members):
        out = []
        for m in members:
            name = (m.get("name") or "").strip()
            if not name:
                continue
            line = name
            fn = (m.get("function") or "").strip()
            if fn:
                line += f" — {fn}"
            meta = []
            email = (m.get("email") or "").strip()
            if email:
                meta.append(email)
            cid = (m.get("core_id") or "").strip()
            if cid:
                meta.append(f"ID {cid}")
            if meta:
                line += f" ({', '.join(meta)})"
            out.append(f"- {line}")
        return out

    lines = []
    core_lines = _fmt(core)
    ext_lines = _fmt(extended)
    if core_lines:
        lines.append("Core team:")
        lines.extend(core_lines)
    if ext_lines:
        lines.append("Extended team / key partners:")
        lines.extend(ext_lines)
    return "\n".join(lines)
