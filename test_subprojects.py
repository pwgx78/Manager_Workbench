"""Sub-projects: the level between a PROJECT and a single item.

A project sits at the altitude of a Jira PROJECT ('SPR'); a sub-project at the
altitude of one Jira ISSUE ('SPR-60789') or one email thread. That is the gap
the Jira guard creates deliberately — an issue must never be a top-level
project, but it still needs a home.

Runs against a scratch profile so the live workbench is never touched.
"""
import os
import shutil
import tempfile

scratch = tempfile.mkdtemp(prefix="mwb_sub_")
os.environ["MANAGER_WORKBENCH_HOME"] = scratch

import db  # noqa: E402
import project_db as PDB  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

db.init_schema()
ok = lambda m: print("  PASS", m)


def fails(fn, label):
    try:
        fn()
        print("  FAIL (no error):", label)
        raise SystemExit(1)
    except PDB.ProjectError as exc:
        print("  PASS refused:", label, "->", str(exc)[:52])


PDB.set_prefix("FAB")
parent = PDB.create_project("Customer Facing Mechanical SPRs", keywords="Customer Facing")
other = PDB.create_project("Unrelated Work")

print("\n-- Jira key recognition is permissive on purpose --")
for good in ("SPR-60789", "spr-60789", "CBL-EC5X-USBC3A-01", "TC-1"):
    assert PDB.is_jira_key(good), good
    ok(f"{good!r} recognised as a key")
for bad in ("", "SPR", "just some words", "60789", "SPR 60789"):
    assert not PDB.is_jira_key(bad), bad
ok("plain words, a bare prefix and a bare number are not keys")

print("\n-- key normalisation --")
assert PDB.normalize_subproject_key(PDB.JIRA, "  spr-60789 ") == "SPR-60789"
ok("Jira keys upper-case and strip")
assert PDB.normalize_subproject_key(PDB.SUBJECT, "RE: FW: TC101 Issue") == "tc101 issue"
ok("reply/forward prefixes are stripped from subjects")
assert PDB.normalize_subproject_key(PDB.SUBJECT, "RE: RE: Re: TC101  Issue") == "tc101 issue"
ok("stacked prefixes and doubled spaces collapse to the same subject")
# Unlike a project NAME, a subject keeps its punctuation — it has to stay
# readable and two subjects differing only by punctuation are different threads.
assert PDB.normalize_subproject_key(PDB.SUBJECT, "TC-101: update") == "tc-101: update"
ok("subject punctuation is preserved, unlike the project-name fold")

print("\n-- manual creation, and what it refuses --")
key = PDB.add_subproject(parent, PDB.JIRA, "spr-60789")
assert key == "SPR-60789"
ok(f"added Jira sub-project {key}")
subj = PDB.add_subproject(parent, PDB.SUBJECT, "RE: Japan Post escalation")
assert subj == "japan post escalation"
ok(f"added subject sub-project {subj!r}")

fails(lambda: PDB.add_subproject(parent, PDB.JIRA, "not a key"), "non-key as a Jira sub")
fails(lambda: PDB.add_subproject(parent, "banana", "x"), "unknown kind")
fails(lambda: PDB.add_subproject(parent, PDB.JIRA, "   "), "blank key")
fails(lambda: PDB.add_subproject("FAB-999", PDB.JIRA, "SPR-1"), "no such parent")
ok("a sub-project cannot exist without a parent — the core invariant")

PDB.add_subproject(parent, PDB.JIRA, "SPR-60789")
assert len(PDB.list_subprojects(parent)) == 2
ok("re-adding the same key is idempotent, not a duplicate")

print("\n-- AUTO-CREATE when a Jira key is linked --")
before = {s["key"] for s in PDB.list_subprojects(parent)}
PDB.link(parent, "jira", "SPR-61086", state="confirmed")
after = {s["key"] for s in PDB.list_subprojects(parent)}
assert after - before == {"SPR-61086"}, after
ok("linking SPR-61086 registered a sub-project for it automatically")
auto = [s for s in PDB.list_subprojects(parent) if s["key"] == "SPR-61086"][0]
assert auto["created_by"] == "auto", auto
ok("...recorded as created_by='auto', so it is distinguishable from a manual one")
counts = PDB.subproject_counts(parent)
assert counts.get((PDB.JIRA, "SPR-61086"), {}).get("jira") == 1, counts
ok("the link is attributed to it, so the count rolls up")

print("\n-- auto-create is narrow: only jira, only real keys --")
PDB.link(parent, "email", "m-1", state="confirmed")
assert not [s for s in PDB.list_subprojects(parent) if s["key"] == "m-1"]
ok("linking an EMAIL creates no sub-project (704 subjects would be noise)")
PDB.link(parent, "jira", "not-a-key-at-all", state="confirmed")
assert not [s for s in PDB.list_subprojects(parent) if "NOT-A-KEY" in s["key"].upper()]
ok("a jira link whose id is not key-shaped creates nothing")

print("\n-- attributing an existing link by hand --")
PDB.attribute_link(parent, "email", "m-1", PDB.SUBJECT, "RE: Japan Post escalation")
counts = PDB.subproject_counts(parent)
assert counts.get((PDB.SUBJECT, "japan post escalation"), {}).get("email") == 1, counts
ok("an email link rolls up under a subject sub-project")
PDB.attribute_link(parent, "email", "m-1", None, None)
assert (PDB.SUBJECT, "japan post escalation") not in PDB.subproject_counts(parent)
ok("attribution can be cleared back to parent-only")

print("\n-- re-linking must not wipe a hand-set attribution --")
PDB.attribute_link(parent, "email", "m-1", PDB.SUBJECT, "japan post escalation")
PDB.link(parent, "email", "m-1", state="confirmed", assigned_by="user")
counts = PDB.subproject_counts(parent)
assert counts.get((PDB.SUBJECT, "japan post escalation"), {}).get("email") == 1, counts
ok("re-linking the same email preserves the sub-project it was filed under")

print("\n-- status --")
PDB.set_subproject_status(parent, PDB.JIRA, "SPR-60789", "done")
assert [s["status"] for s in PDB.list_subprojects(parent) if s["key"] == "SPR-60789"] == ["done"]
ok("marked done")
assert "SPR-60789" not in {s["key"] for s in PDB.list_subprojects(parent, include_done=False)}
ok("done sub-projects drop out of the open-only view")
PDB.set_subproject_status(parent, PDB.JIRA, "SPR-60789", "open")
assert "SPR-60789" in {s["key"] for s in PDB.list_subprojects(parent, include_done=False)}
ok("reopened")
fails(lambda: PDB.set_subproject_status(parent, PDB.JIRA, "SPR-60789", "cancelled"), "bad status")

print("\n-- deleting a sub-project keeps its items on the project --")
PDB.attribute_link(parent, "jira", "SPR-61086", PDB.JIRA, "SPR-61086")
PDB.delete_subproject(parent, PDB.JIRA, "SPR-61086")
assert "SPR-61086" not in {s["key"] for s in PDB.list_subprojects(parent)}
still_linked = [
    row for row in PDB.links_for_project(parent) if row["entity_id"] == "SPR-61086"
]
assert len(still_linked) == 1, still_linked
ok("the ticket is still linked to the project, just no longer grouped")

print("\n-- sub-projects are scoped to their parent --")
PDB.add_subproject(other, PDB.JIRA, "SPR-60789")
assert len(PDB.list_subprojects(other)) == 1
assert "SPR-60789" in {s["key"] for s in PDB.list_subprojects(parent)}
ok("the same Jira key can be a sub-project of two projects independently")

print("\n-- deleting the parent takes its sub-projects with it --")
PDB.delete_project(other)
conn = db.connect()
left = conn.execute("SELECT COUNT(*) FROM subprojects WHERE project_id = ?", (other,)).fetchone()[0]
conn.close()
assert left == 0, left
ok("no orphaned sub-projects after the parent is deleted")

print("\n-- delete_all_projects clears sub-projects too --")
PDB.delete_all_projects()
conn = db.connect()
assert conn.execute("SELECT COUNT(*) FROM subprojects").fetchone()[0] == 0
conn.close()
ok("register wipe leaves no sub-projects behind")

print("\n-- the page renders with sub-projects present --")
p = PDB.create_project("Customer Facing Mechanical SPRs", keywords="SPR")
PDB.link(p, "jira", "SPR-60789", state="confirmed")
PDB.add_subproject(p, PDB.SUBJECT, "TC101 Japan Post")
at = AppTest.from_file("pages/2_project_management.py", default_timeout=60)
at.session_state["gemini_client"] = None
at.run()
assert not at.exception, at.exception[0].value
ok("project management page renders clean")
frames = [d.value for d in at.dataframe if "Kind" in getattr(d.value, "columns", [])]
assert frames, [list(getattr(d.value, "columns", [])) for d in at.dataframe]
table = frames[0]
assert set(table["Key"]) == {"SPR-60789", "TC101 Japan Post"}, table["Key"].tolist()
ok(f"the sub-projects table shows both: {table['Key'].tolist()}")

shutil.rmtree(scratch, ignore_errors=True)
print("\nSUB-PROJECT TESTS PASSED")
