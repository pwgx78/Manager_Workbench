"""
user_profile.py — where a user's data lives, and what stays behind on the machine.

Named `user_profile` rather than `profile` on purpose: the latter is a Python
stdlib module (the deterministic profiler), and the app root sits first on
sys.path, so a module named `profile.py` here would shadow it for every import
in the process — including inside third-party packages.

The app used to write all of its data into the repo folder itself, which meant a
user's workbench could not be backed up, moved, or handed to someone else, and a
second person could not use the app at all. This module introduces the split
that fixes that:

    <home>/                                <- $MANAGER_WORKBENCH_HOME, or
    ├── machine.json                          %LOCALAPPDATA%\\ManagerWorkbench
    │     active profile + credential FILE PATHS  (machine-local, never travels)
    ├── ms_token_cache.bin                    MSAL secret (machine-local)
    ├── ms_client_secret.txt                    app-registration secret (machine-local)
    └── profiles/
        └── <profile_id>/
            └── workbench.db               <- THE portable artifact

Everything a user accumulates — settings, roster, identity, trackers, and every
SQLite cache — lives in that one `workbench.db`. Copy it and you have moved the
whole workbench. Secrets are deliberately excluded so a profile is safe to share.

Imports nothing from `config` or `store`: those import *this*, and the cycle
would be unbreakable otherwise.
"""
import os
import json
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime

APP_DIR_NAME = "ManagerWorkbench"
HOME_ENV_VAR = "MANAGER_WORKBENCH_HOME"
DB_FILENAME = "workbench.db"
BUNDLE_SUFFIX = ".mwb"
MACHINE_FILENAME = "machine.json"
DEFAULT_PROFILE_ID = "default"

# Credential paths are machine-specific (they point at files on *this* disk), so
# they live in machine.json rather than in the portable profile.
MACHINE_KEYS = (
    "jira_pat_path",
    "confluence_pat_path",
    "gcp_credentials_path",
    "ms_client_secret_path",
)

DEFAULT_MACHINE = {
    "active_profile": DEFAULT_PROFILE_ID,
    "jira_pat_path": "",
    "confluence_pat_path": "",
    "gcp_credentials_path": "",
    "ms_client_secret_path": "",
}


# --------------------------------------------------------------------------- #
# Locations
# --------------------------------------------------------------------------- #
def home():
    """Root directory for all Manager Workbench data.

    Honours $MANAGER_WORKBENCH_HOME first — that override is what makes the
    clean-slate and second-profile tests possible without touching real data."""
    override = os.environ.get(HOME_ENV_VAR, "").strip()
    if override:
        return os.path.abspath(override)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return os.path.join(base, APP_DIR_NAME)
    return os.path.join(os.path.expanduser("~"), ".manager_workbench")


def profiles_root():
    return os.path.join(home(), "profiles")


def profile_dir(profile_id=None):
    return os.path.join(profiles_root(), profile_id or active_profile_id())


def db_path(profile_id=None):
    """Full path to a profile's workbench.db. Resolved on every call so that
    switching profiles takes effect without restarting the process."""
    return os.path.join(profile_dir(profile_id), DB_FILENAME)


def machine_file():
    return os.path.join(home(), MACHINE_FILENAME)


def ms_token_cache_path():
    """MSAL token cache — machine-local, outside every profile."""
    return os.path.join(home(), "ms_token_cache.bin")


def ensure_home():
    """Create the home + active profile directories and machine.json if absent.
    Safe to call on every app start."""
    os.makedirs(home(), exist_ok=True)
    os.makedirs(profiles_root(), exist_ok=True)
    machine = load_machine()
    pid = machine.get("active_profile") or DEFAULT_PROFILE_ID
    os.makedirs(profile_dir(pid), exist_ok=True)
    if not os.path.exists(machine_file()):
        save_machine(machine)
    return pid


# --------------------------------------------------------------------------- #
# machine.json  (atomic write — same tempfile + .bak + os.replace approach that
# app_config.json needed after an interrupted write once truncated it)
# --------------------------------------------------------------------------- #
def _atomic_write_json(path, data):
    dir_ = os.path.dirname(path) or "."
    os.makedirs(dir_, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dir_, prefix=".mw-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        if os.path.exists(path):
            try:
                shutil.copy2(path, path + ".bak")
            except OSError as e:
                print(f"[Profile] Could not write backup of {path}: {e}")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _read_json(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception as e:
        print(f"[Profile] Failed to read {path}: {e}")
        return None


def load_machine():
    """Machine-local settings, merged over defaults. Falls back to the .bak on a
    corrupt file rather than silently resetting credential paths."""
    machine = dict(DEFAULT_MACHINE)
    stored = _read_json(machine_file())
    if stored is None and os.path.exists(machine_file()):
        stored = _read_json(machine_file() + ".bak")
        if stored is not None:
            print("[Profile] Recovered machine.json from backup.")
    if isinstance(stored, dict):
        machine.update({k: v for k, v in stored.items() if k in DEFAULT_MACHINE})
    return machine


def save_machine(machine):
    _atomic_write_json(machine_file(), machine)


def active_profile_id():
    return load_machine().get("active_profile") or DEFAULT_PROFILE_ID


# --------------------------------------------------------------------------- #
# Profile management
# --------------------------------------------------------------------------- #
def slugify(name):
    """Filesystem-safe profile id derived from a display name."""
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", (name or "").strip()).strip("-.").lower()
    return slug or DEFAULT_PROFILE_ID


def list_profiles():
    """Return [{id, path, size_bytes, exists}] for every profile on disk,
    id-sorted. A directory with no workbench.db yet still counts (it is a freshly
    created, not-yet-populated profile)."""
    root = profiles_root()
    if not os.path.isdir(root):
        return []
    out = []
    for pid in sorted(os.listdir(root)):
        if not os.path.isdir(os.path.join(root, pid)):
            continue
        path = db_path(pid)
        out.append(
            {
                "id": pid,
                "path": path,
                "exists": os.path.exists(path),
                "size_bytes": os.path.getsize(path) if os.path.exists(path) else 0,
            }
        )
    return out


def create_profile(name):
    """Create an empty profile directory and return its id. Raises if it exists —
    creating over a populated profile would silently adopt someone else's data."""
    pid = slugify(name)
    target = profile_dir(pid)
    if os.path.isdir(target):
        raise ValueError(f"A profile named '{pid}' already exists.")
    os.makedirs(target, exist_ok=False)
    return pid


def switch_profile(profile_id):
    """Make `profile_id` active. The caller is responsible for clearing Streamlit
    caches and rerunning so the new profile's data is picked up."""
    if not os.path.isdir(profile_dir(profile_id)):
        raise ValueError(f"No such profile: {profile_id}")
    machine = load_machine()
    machine["active_profile"] = profile_id
    save_machine(machine)
    return profile_id


def rename_profile(profile_id, new_name):
    """Rename a profile directory, following the active pointer if needed."""
    new_id = slugify(new_name)
    if new_id == profile_id:
        return profile_id
    if os.path.isdir(profile_dir(new_id)):
        raise ValueError(f"A profile named '{new_id}' already exists.")
    os.rename(profile_dir(profile_id), profile_dir(new_id))
    machine = load_machine()
    if machine.get("active_profile") == profile_id:
        machine["active_profile"] = new_id
        save_machine(machine)
    return new_id


def delete_profile(profile_id):
    """Permanently delete a profile's data. Refuses to delete the active profile
    or the last remaining one, so the app can never be left with nowhere to write."""
    if profile_id == active_profile_id():
        raise ValueError("Switch to another profile before deleting this one.")
    if len(list_profiles()) <= 1:
        raise ValueError("Cannot delete the only profile.")
    shutil.rmtree(profile_dir(profile_id))


# --------------------------------------------------------------------------- #
# Export / import — the portability surface
# --------------------------------------------------------------------------- #
def export_profile(profile_id=None, dest=None):
    """Write a consistent snapshot of a profile's database to `dest` (default: a
    temp <id>.mwb) and return the path.

    Uses `VACUUM INTO` rather than a file copy: it produces a single defragmented
    file from a consistent read transaction, so it is safe even while the app has
    the database open in WAL mode (a plain copy could miss un-checkpointed pages)."""
    pid = profile_id or active_profile_id()
    src = db_path(pid)
    if not os.path.exists(src):
        raise ValueError(f"Profile '{pid}' has no data to export yet.")
    if dest is None:
        dest = os.path.join(tempfile.gettempdir(), f"{pid}{BUNDLE_SUFFIX}")
    if os.path.exists(dest):
        os.remove(dest)  # VACUUM INTO refuses to overwrite
    conn = sqlite3.connect(src)
    try:
        conn.execute("VACUUM INTO ?", (dest,))
    finally:
        conn.close()
    return dest


def import_profile(src, name):
    """Create a new profile from an exported bundle. Validates that the file is a
    real workbench database before adopting it, so a mistaken upload cannot leave
    a corrupt profile behind."""
    if not os.path.exists(src):
        raise ValueError(f"No such file: {src}")
    try:
        conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
    except sqlite3.DatabaseError as e:
        raise ValueError(f"Not a valid workbench database: {e}")
    if "profile_meta" not in tables or "doc_store" not in tables:
        raise ValueError(
            "That file is not a Manager Workbench export (missing profile_meta/doc_store)."
        )
    pid = create_profile(name)
    try:
        shutil.copy2(src, db_path(pid))
    except Exception:
        shutil.rmtree(profile_dir(pid), ignore_errors=True)
        raise
    return pid


# --------------------------------------------------------------------------- #
# One-time relocation of the machine-local token cache
# --------------------------------------------------------------------------- #
def adopt_legacy_token_cache(project_root):
    """Move a pre-existing ./ms_token_cache.bin out of the repo into the home dir
    so the upgrade doesn't force a fresh Microsoft sign-in. Returns True if moved."""
    legacy = os.path.join(project_root, "ms_token_cache.bin")
    target = ms_token_cache_path()
    if not os.path.exists(legacy) or os.path.exists(target):
        return False
    os.makedirs(home(), exist_ok=True)
    try:
        shutil.move(legacy, target)
        print(f"[Profile] Moved MSAL token cache -> {target}")
        return True
    except OSError as e:
        print(f"[Profile] Could not move token cache: {e}")
        return False


def stamp():
    return datetime.now().isoformat(timespec="seconds")
