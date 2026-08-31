"""
ms_reauth.py — one-off out-of-band Microsoft sign-in to re-seed the MSAL cache.

Why this exists: the device-code flow has no redirect URI, so Entra decides the
client type from the app registration's `allowPublicClient` flag — which is
currently off, giving AADSTS7000218 at redemption. The auth-code + PKCE flow
DOES send a redirect URI, and a redirect URI registered under "Mobile and
desktop applications" makes the request public-client on its own, no secret and
no app-registration change required.

Run this once from a terminal. It writes the same cache the app reads, so a
success here means Settings -> Credentials shows "Signed in" with no code change:

    python ms_reauth.py

It reports the exact AADSTS code on failure, which tells you what IT would need
to change if this path is closed too.
"""
import urllib3

import ms_auth
from config import MS_SCOPES

urllib3.disable_warnings()

# `http://localhost` registered with no port lets Entra accept any loopback port,
# so try MSAL's default first; the explicit ports cover a pinned registration.
PORTS = [None, 8400, 8501, 3000]


def main():
    cache = ms_auth._load_cache()
    app = ms_auth._build_app(cache)

    for port in PORTS:
        label = f"port {port}" if port else "any free port"
        print(f"\n--- Trying auth-code + PKCE on {label} ---")
        kwargs = {"port": port} if port else {}
        try:
            result = app.acquire_token_interactive(MS_SCOPES, **kwargs)
        except Exception as e:
            print(f"  could not start local redirect listener: {e}")
            continue

        if "access_token" in result:
            ms_auth._save_cache(cache)
            print(f"\nSUCCESS — token cached at {__import__('user_profile').ms_token_cache_path()}")
            print("Open Settings -> Credentials; it should now show Signed in.")
            return 0

        error = result.get("error")
        desc = result.get("error_description", "")
        print(f"  FAILED [{error}] {desc[:300]}")
        # AADSTS50011 = this redirect URI is not registered; another port may be.
        # Anything else (e.g. 7000218) is not port-specific, so stop.
        if "AADSTS50011" not in desc:
            break

    print("\nNo interactive path succeeded. See the AADSTS code above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
