"""Static checks over every tracked Python file.

Exists because of a real escape: `fetch_mailbox_messages` was added to
api_helpers and called from pages/0_email_actions.py but never imported there.
Nothing caught it — the call sits inside a button handler, and the test suite
cannot click that button because doing so would hit the live mailbox. It failed
in the user's face instead.

Undefined names are exactly what a static pass finds for free, so this closes
that gap for every handler the tests cannot reach.

    python test_lint.py

Requires pyflakes (pip install pyflakes).
"""
import subprocess
import sys

# Categories that are real defects rather than style opinions. An undefined name
# is always a latent crash; an unused local is usually a leftover from a
# deletion (which is how it was found here).
FATAL = ("undefined name", "unable to detect undefined names")
WARN = ("assigned to but never used", "imported but unused", "f-string is missing")


def tracked_python_files():
    out = subprocess.run(
        ["git", "ls-files", "*.py"], capture_output=True, text=True, check=True
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def main():
    files = tracked_python_files()
    print(f"pyflakes over {len(files)} tracked file(s)\n")
    result = subprocess.run(
        [sys.executable, "-m", "pyflakes", *files], capture_output=True, text=True
    )
    if "No module named pyflakes" in result.stderr:
        print("SKIP: pyflakes is not installed (pip install pyflakes)")
        return 0

    lines = [
        line for line in (result.stdout + result.stderr).splitlines() if line.strip()
    ]
    fatal = [line for line in lines if any(k in line.lower() for k in FATAL)]
    warn = [
        line
        for line in lines
        if line not in fatal and any(k in line.lower() for k in WARN)
    ]
    other = [line for line in lines if line not in fatal and line not in warn]

    for label, group in (("FATAL", fatal), ("WARN", warn), ("OTHER", other)):
        for line in group:
            print(f"  {label}  {line}")

    if fatal:
        print(f"\nFAILED: {len(fatal)} undefined name(s) — these crash at runtime.")
        return 1
    print(
        f"\nLINT PASSED — no undefined names."
        + (f" ({len(warn) + len(other)} non-fatal note(s).)" if warn or other else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
