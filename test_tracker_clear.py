"""The 'Clear action register' button on the Email Action Tracker tab.

Runs against a scratch profile so the live workbench is never touched.
"""
import os
import shutil
import tempfile

scratch = tempfile.mkdtemp(prefix="mwb_clear_")
os.environ["MANAGER_WORKBENCH_HOME"] = scratch

import config  # noqa: E402
import db  # noqa: E402
import store  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

db.init_schema()


def seed(n=3):
    store.save_json(
        config.EMAIL_ACTIONS_KEY,
        [
            {
                "Completed": i == 0,
                "Priority": ["High", "Medium", "Low"][i % 3],
                "Origin": "Email",
                "Action": f"Action number {i}",
                "Email Thread": f"Thread {i}",
                "Owner": "someone" if i else "",
                "Date Assigned": "2026-09-01",
                "Date Completed": "",
                "Next Step": "",
                "Suggested Response": "",
            }
            for i in range(n)
        ],
    )


def run():
    at = AppTest.from_file("pages/0_email_actions.py", default_timeout=60)
    at.session_state["gemini_client"] = object()
    at.run()
    assert not at.exception, at.exception[0].value
    return at


def widget(at, kind, key):
    found = [w for w in getattr(at, kind) if w.key == key]
    assert found, f"{kind} {key!r} not found: {[w.key for w in getattr(at, kind)]}"
    return found[0]


print("\n-- the control is present and gated --")
seed(3)
at = run()
assert len(store.load_json(config.EMAIL_ACTIONS_KEY, default=[])) == 3
clear = widget(at, "button", "phase0_tracker_clear")
assert clear.disabled, "Clear button is enabled before the box is ticked"
print("  PASS Clear button renders DISABLED until the confirmation is ticked")

# AppTest has no typed accessor for download_button, so go through get(). The
# proto id is the key with a hash prefix, hence endswith rather than equality.
exports = [
    w for w in at.get("download_button")
    if str(w.proto.id).endswith("phase0_tracker_export")
]
assert exports, [w.proto.id for w in at.get("download_button")]
print("  PASS an export-first download button is offered alongside it")

print("\n-- clicking while un-ticked must not clear --")
# Streamlit blocks a disabled widget in the browser; assert the data survives a
# click regardless, so the guard does not rest on the UI alone.
widget(at, "button", "phase0_tracker_clear").click().run()
assert len(store.load_json(config.EMAIL_ACTIONS_KEY, default=[])) == 3
print("  PASS register untouched (still 3 items)")

print("\n-- tick, then clear --")
at = run()
widget(at, "checkbox", "phase0_tracker_clear_confirm").check().run()
assert not widget(at, "button", "phase0_tracker_clear").disabled
print("  PASS ticking the box enables the button")
widget(at, "button", "phase0_tracker_clear").click().run()
assert store.load_json(config.EMAIL_ACTIONS_KEY, default=[]) == []
print("  PASS register cleared to empty")

print("\n-- empty state --")
at = run()
assert any("already empty" in str(c.value) for c in at.caption)
print("  PASS says 'already empty' rather than offering a pointless button")
assert not [b for b in at.button if b.key == "phase0_tracker_clear"]
print("  PASS no Clear button rendered when there is nothing to clear")

print("\n-- clears rows hidden by a filter, not just the visible view --")
seed(3)
at = run()
# Hide two of the three rows by filtering Priority, then clear.
widget(at, "multiselect", "phase0_tracker_f_Priority").select("High").run()
assert any(
    "hidden by filters" in str(c.value) for c in at.caption
), [c.value for c in at.caption]
print("  PASS filter active, and the page reports rows hidden by it")
widget(at, "checkbox", "phase0_tracker_clear_confirm").check().run()
widget(at, "button", "phase0_tracker_clear").click().run()
remaining = store.load_json(config.EMAIL_ACTIONS_KEY, default=[])
assert remaining == [], remaining
print("  PASS all 3 cleared, including the 2 hidden by the filter")

print("\n-- Save still preserves filtered-out rows (no regression) --")
seed(3)
at = run()
widget(at, "multiselect", "phase0_tracker_f_Priority").select("High").run()
# The Save button predates this work and has no key, so match it by label.
save = [b for b in at.button if "Save Tracker" in str(b.label)]
assert save, [str(b.label) for b in at.button]
save[0].click().run()
assert len(store.load_json(config.EMAIL_ACTIONS_KEY, default=[])) == 3
print("  PASS saving a filtered view still keeps all 3 rows")

shutil.rmtree(scratch, ignore_errors=True)
print("\nTRACKER CLEAR TESTS PASSED")
