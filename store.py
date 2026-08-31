"""
store.py — persistence for the app's loose JSON documents.

Historically these were files in the repo root; they are now rows in the
`doc_store` table of the active profile's workbench.db, so they travel with the
user instead of with the checkout. See db.py / user_profile.py.

The function signatures are unchanged on purpose — every page already calls
`store.load_json(...)` / `store.save_json(...)`, and only the meaning of the
first argument moved: it is now a document KEY (e.g. "email_actions") rather than
a filesystem path. The `config.*_KEY` constants supply those names.
"""
import db


def load_json(key, default=None):
    """Load the document stored under `key`, returning `default` (or []) when it
    has never been written."""
    if default is None:
        default = []
    return db.get_doc(key, default)


def save_json(key, data):
    """Persist `data` as the document under `key`."""
    db.set_doc(key, data)
