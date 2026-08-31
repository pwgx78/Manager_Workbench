"""
ms_auth.py — Microsoft Graph authentication via MSAL device-code flow.

Token acquisition is split so the interactive login can happen IN the Streamlit
UI instead of blocking the server and printing a code to the terminal:

  - get_ms_token()       -> silent refresh from the cache only; never blocks.
                            Raises MSAuthRequired if interactive sign-in is needed.
  - begin_device_flow()  -> start a device-code flow (returns code + URL).
  - complete_device_flow -> block until the browser login finishes, save cache.

SSL verification is disabled on the requests session to work behind the Zebra
corporate proxy (mirrors the existing biweekly_automation / EmailToJira refs).
"""
import os

import msal
import requests

import user_profile
from config import (
    MS_CLIENT_ID,
    MS_AUTHORITY,
    MS_SCOPES,
)

# Requests session with SSL verification disabled for the Zebra corp proxy.
_session = requests.Session()
_session.verify = False


class MSAuthRequired(Exception):
    """Raised when no cached Microsoft token is available and interactive
    sign-in is needed. Callers should route the user to Settings → Credentials
    rather than triggering a terminal-blocking device-code prompt."""


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _load_cache():
    """Read the MSAL cache from the app's home directory.

    The cache holds a live refresh token, so it is deliberately machine-local —
    it lives beside `machine.json`, never inside a user profile. That is what
    makes an exported profile safe to copy or hand to someone else."""
    cache = msal.SerializableTokenCache()
    path = user_profile.ms_token_cache_path()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            cache.deserialize(f.read())
    return cache


def _save_cache(cache):
    if cache.has_state_changed:
        path = user_profile.ms_token_cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(cache.serialize())


def _build_app(cache):
    return msal.PublicClientApplication(
        MS_CLIENT_ID,
        authority=MS_AUTHORITY,
        token_cache=cache,
        http_client=_session,  # bypass SSL for the corp network proxy
    )


# --------------------------------------------------------------------------- #
# Token acquisition (silent only)
# --------------------------------------------------------------------------- #
def get_ms_token() -> str:
    """Return a valid Microsoft Graph access token from the cache (silent
    refresh only). Does NOT trigger interactive device-code login — that would
    block the Streamlit server and print a code to the terminal. Raises
    MSAuthRequired if no cached account/token can be refreshed, so the caller
    can direct the user to the in-app sign-in."""
    cache = _load_cache()
    app = _build_app(cache)
    accounts = app.get_accounts()
    result = None
    if accounts:
        result = app.acquire_token_silent(MS_SCOPES, account=accounts[0])
    _save_cache(cache)
    if result and "access_token" in result:
        return result["access_token"]
    raise MSAuthRequired(
        "Microsoft sign-in required. Open Settings → Credentials and click "
        "'Sign in to Microsoft'."
    )


# --------------------------------------------------------------------------- #
# Status helpers (for the Settings UI)
# --------------------------------------------------------------------------- #
def ms_signed_in_account() -> str:
    """Return the cached account's username/email, or '' if none."""
    cache = _load_cache()
    app = _build_app(cache)
    accounts = app.get_accounts()
    return accounts[0].get("username", "") if accounts else ""


def ms_is_signed_in() -> bool:
    """True if a cached account exists and a token can be refreshed silently."""
    try:
        get_ms_token()
        return True
    except Exception:
        return False


def ms_sign_out() -> None:
    """Remove the cached account/token so the next call requires a fresh login."""
    cache = _load_cache()
    app = _build_app(cache)
    for acct in app.get_accounts():
        app.remove_account(acct)
    _save_cache(cache)
    if os.path.exists(user_profile.ms_token_cache_path()):
        try:
            os.remove(user_profile.ms_token_cache_path())
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Interactive device-code flow (driven by the Streamlit UI)
# --------------------------------------------------------------------------- #
def begin_device_flow() -> dict:
    """Initiate a device-code flow and return the flow dict (contains
    'user_code', 'verification_uri', and a human-readable 'message'). Does not
    block — pass the returned dict to complete_device_flow() to finish."""
    cache = _load_cache()
    app = _build_app(cache)
    flow = app.initiate_device_flow(scopes=MS_SCOPES)
    if "user_code" not in flow:
        raise ValueError(
            f"Could not initiate device flow: {flow.get('error_description', flow)}"
        )
    return flow


def complete_device_flow(flow: dict) -> str:
    """Block until the user finishes the device-code login in their browser,
    then persist the token cache and return the access token. Polls until the
    flow is completed or expires (~15 min)."""
    cache = _load_cache()
    app = _build_app(cache)
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" in result:
        _save_cache(cache)
        return result["access_token"]
    error = result.get("error_description", "Unknown authentication error")
    raise Exception(f"Microsoft auth failed: {error}")
