"""P2 page integration, rendered for real via Streamlit's AppTest.

Page 0 needs a Gemini client to get past its st.stop() guard, so it gets a
dummy object — no analysis is triggered, only the already-cached read paths and
the new project controls are exercised.

Runs against a scratch profile so the live workbench is never touched.
"""
import os
import shutil
import tempfile

scratch = tempfile.mkdtemp(prefix="mwb_p2page_")
os.environ["MANAGER_WORKBENCH_HOME"] = scratch

import db  # noqa: E402
import email_db  # noqa: E402
import project_db  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

db.init_schema()
project_db.set_prefix("FAB")

# A register with two projects, and historical email that matches one of them.
ai = project_db.create_project("AI Gov", keywords="AI Governance, AI committee")
tc = project_db.create_project("TC101 Japan Post Quality Issue", keywords="Japan Post")

email_db.upsert_email_project(
    "m-1", "conv-1", "AI Gov", "AI committee charter review", "2026-08-20", "summary"
)
email_db.upsert_email_project(
    "m-2", "conv-2", "Colruyt TC22 Rework", "Rework line update", "2026-08-21", "summary"
)
email_db.upsert_email_project(
    "m-3", "conv-3", "Unassigned", "Japan Post asked about TC101", "2026-08-22", "summary"
)


def run(path, label, session=None):
    at = AppTest.from_file(path, default_timeout=60)
    at.session_state["gemini_client"] = session
    at.run()
    if at.exception:
        print(f"  FAIL {label}: {at.exception[0].value}")
        raise SystemExit(1)
    print(f"  PASS {label} rendered clean")
    return at


print("\n-- dual-write path (what the analyzer now does per email) --")
# Mirrors pages/0_email_actions.py: legacy label written, register link layered on.
email_db.upsert_email_project(
    "m-4", "conv-4", "AI Gov", "another AI Gov thread", "2026-08-23", "s"
)
assert project_db.link_legacy_label("AI Gov", "email", "m-4") == ai
assert project_db.link_counts("confirmed").get(ai, {}).get("email") == 1
assert email_db.get_project_for_message("m-4") == "AI Gov"
print("  PASS legacy label AND register link both written; neither replaces the other")

print("\n-- Project Management: create from existing label --")
at = run("pages/2_project_management.py", "project management")
offered = [
    row["label"] for row in project_db.legacy_label_counts()
]
assert "Colruyt TC22 Rework" in offered, offered
assert "AI Gov" not in offered, offered
print(f"  PASS unregistered labels offered ({offered}), registered ones excluded")

before = project_db.count_projects()
made = project_db.create_project("Colruyt TC22 Rework")
assert project_db.count_projects() == before + 1
assert "Colruyt TC22 Rework" not in [
    r["label"] for r in project_db.legacy_label_counts()
]
print("  PASS creating from a label removes it from the offer list")

print("\n-- Project Management: backfill by keyword --")
hits = project_db.backfill_candidates(tc)
assert [h["message_id"] for h in hits] == ["m-3"], hits
print(f"  PASS backfill matched the unassigned email that mentions TC101: {hits[0]['matched']}")
assert project_db.backfill_link(tc, ["m-3"]) == 1
pending = [p for p in project_db.pending_proposals() if p["project_id"] == tc]
assert len(pending) == 1, pending
print("  PASS filed as a proposal, not straight into the rollup")

at = run("pages/2_project_management.py", "project management with a proposal")
assert not any("Nothing awaiting approval" in str(i.value) for i in at.info)
print("  PASS Proposals tab now shows the pending link instead of zero")

print("\n-- Page 0 renders with the new project controls --")
at = run("pages/0_email_actions.py", "email actions", session=object())
frames = [d.value for d in at.dataframe]
register_frames = [f for f in frames if "ID" in getattr(f, "columns", [])]
assert register_frames, [list(getattr(f, "columns", [])) for f in frames]
reg = register_frames[0]
assert set(["ID", "Project", "Emails", "Keywords"]).issubset(set(reg.columns)), reg.columns
print(f"  PASS Projects & Themes shows the register rollup: {list(reg.columns)}")
assert any("Legacy free-text labels" in str(m.value) for m in at.markdown)
print("  PASS legacy label view still present — nothing regressed")
row = reg[reg["ID"] == ai]
assert int(row["Emails"].iloc[0]) == 1, row.to_dict()
print("  PASS rollup counts the dual-written link")

print("\n-- Page 0: the per-conversation project control --")
# The conversation expander (and so _render_project_controls) only renders when
# phase0_threads is populated, which normally requires a full analysis run. Seed
# it directly to exercise the control without calling the LLM.
thread = {
    "conv_id": "conv-3",
    "subject": "Japan Post asked about TC101",
    "count": 1,
    "latest": "2026-08-22",
    "message_ids": ["m-3"],
    "summary": {
        "summary": "Japan Post are chasing the TC101 quality issue.",
        "key_points": ["Quality escalation"],
        "pending_tasks": [],
        "suggested_action": "Reply with the RCA status",
        "suggested_disposition": "",
        "requested_of_me": True,
    },
}
at = AppTest.from_file("pages/0_email_actions.py", default_timeout=60)
at.session_state["gemini_client"] = object()
at.session_state["phase0_threads"] = [thread]
at.run()
if at.exception:
    print("  FAIL conversation expander:", at.exception[0].value)
    raise SystemExit(1)
print("  PASS conversation expander with project controls rendered clean")

link_widgets = [m for m in at.multiselect if m.key == "projlink_conv-3"]
assert link_widgets, [m.key for m in at.multiselect]
options = link_widgets[0].options
assert any(tc in opt for opt in options), options
print(f"  PASS project multiselect offered {len(options)} active projects")
assert any(
    "Suggested by keyword" in str(c.value) and "TC101" in str(c.value)
    for c in at.caption
), [c.value for c in at.caption if "Suggested" in str(c.value)]
print("  PASS keyword pre-filter surfaced TC101 as a suggestion for this thread")

# Drive the control for real: select the project, then apply it.
link_widgets[0].select([opt for opt in options if tc in opt][0]).run()
apply_btn = [b for b in at.button if b.key == "projapply_conv-3"]
assert apply_btn, [b.key for b in at.button]
apply_btn[0].click().run()
assert not at.exception, at.exception[0].value if at.exception else None
linked = [p["project_id"] for p in project_db.projects_for_entity("email", "m-3")]
assert tc in linked, linked
print(f"  PASS applying the control linked m-3 to {tc} as confirmed")

print("\n-- Page 13 renders and is project-set aware --")
project_db.link(ai, "email", "m-1", state="confirmed")
project_db.link(made, "email", "m-1", state="confirmed")
names = [p["name"] for p in project_db.projects_for_entity("email", "m-1")]
assert len(names) == 2, names
print(f"  PASS one email now resolves to {len(names)} projects: {names}")
at = run("pages/13_one_on_one_prep.py", "1:1 prep", session=object())

shutil.rmtree(scratch, ignore_errors=True)
print("\nP2 PAGE TESTS PASSED")
