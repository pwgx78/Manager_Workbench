"""P3: the LLM ranks candidates, the human approves.

The load-bearing part is accept_candidates — the boundary where an invented
project id would otherwise become a database row. Decision 6 says the model
never invents a project, and "we asked it not to" is not an enforcement
mechanism, so the offered shortlist is treated as an allowlist.

Runs against a scratch profile so the live workbench is never touched.
"""
import os
import shutil
import tempfile

scratch = tempfile.mkdtemp(prefix="mwb_p3_")
os.environ["MANAGER_WORKBENCH_HOME"] = scratch

import db  # noqa: E402
import email_db  # noqa: E402
import llm_prompts as P  # noqa: E402
import project_db as PDB  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

db.init_schema()
ok = lambda m: print("  PASS", m)

PDB.set_prefix("FAB")
ai = PDB.create_project("AI Gov", keywords="AI Governance, AI committee")
tc = PDB.create_project("TC101 Japan Post", keywords="Japan Post")
hc = PDB.create_project("Headcount", keywords="contractor")
shortlist = PDB.shortlist_for_text("AI Gov and Japan Post and contractor")
assert {p["project_id"] for p in shortlist} == {ai, tc, hc}

print("\n-- the prompt only asks when there is something to rank --")
bare = P.build_email_action_prompt("hello", project_candidates="")
assert "project_candidates" not in bare
assert "CANDIDATE PROJECTS" not in bare
ok("no shortlist -> the task and schema are omitted entirely, not asked-and-ignored")

block = PDB.candidate_block(shortlist)
withc = P.build_email_action_prompt("hello", project_candidates=block)
assert "CANDIDATE PROJECTS" in withc and ai in withc
ok("with a shortlist -> the ids are named in the prompt")
assert "ONLY ids permitted" in withc
ok("the prompt states the ids are the only permitted values")
assert "EMPTY array if none genuinely fit" in withc
ok("an empty answer is explicitly framed as correct, not as failure")
assert "keyword coincidence" in withc
ok("the prompt warns the shortlist is crude and will contain non-fits")

# The candidate schema is a plain string interpolated into an f-string, so
# f-string brace escaping does NOT apply to it. Doubling its braces would show
# the model `[{{...}}]` — malformed JSON as the example of the JSON it must
# produce. This shipped broken once.
schema_line = [
    line for line in withc.splitlines()
    if "project_id" in line and "confidence" in line and line.strip().startswith("[")
]
assert schema_line, "the project_candidates schema example is missing"
assert "{{" not in schema_line[0] and "}}" not in schema_line[0], schema_line[0]
ok(f"the schema example is valid JSON, not brace-escaped: {schema_line[0][:46]}...")
import json as _json
_probe = schema_line[0].replace("<exact id from CANDIDATE PROJECTS>", "FAB-001").replace("<why>", "w")
assert isinstance(_json.loads(_probe), list)
ok("...and it actually parses as JSON")
assert P.MAX_PROJECT_CANDIDATES == PDB.MAX_CONFIRMED_PER_ENTITY == 3
ok("the cap in the prompt matches the cap the database enforces")

print("\n-- THE GATE: an invented id is dropped --")
raw = [
    {"project_id": "FAB-999", "confidence": 0.99, "rationale": "totally made up"},
    {"project_id": ai, "confidence": 0.8, "rationale": "AI committee named"},
]
accepted = PDB.accept_candidates(raw, shortlist)
assert [c["project_id"] for c in accepted] == [ai], accepted
ok("a confident but unlisted id is dropped; the real one survives")

# The nastier case: a real project that exists but was NOT offered.
other = PDB.create_project("Never Offered")
accepted = PDB.accept_candidates(
    [{"project_id": other, "confidence": 1.0, "rationale": "exists!"}], shortlist
)
assert accepted == [], accepted
ok("an id that EXISTS but was not offered is also dropped — allowlist, not existence check")

assert PDB.accept_candidates([{"project_id": ai}], []) == []
ok("an empty shortlist accepts nothing at all")
for junk in (None, "not a list", 42, [{"nope": 1}], ["string"], [None]):
    assert PDB.accept_candidates(junk, shortlist) == [], junk
ok("malformed model output yields no proposals rather than an exception")

print("\n-- ordering, de-duping and the cap --")
raw = [
    {"project_id": hc, "confidence": 0.2, "rationale": "c"},
    {"project_id": ai, "confidence": 0.9, "rationale": "a"},
    {"project_id": tc, "confidence": 0.5, "rationale": "b"},
]
assert [c["project_id"] for c in PDB.accept_candidates(raw, shortlist)] == [ai, tc, hc]
ok("sorted by confidence, best first")
dupes = [
    {"project_id": ai, "confidence": 0.9, "rationale": "first"},
    {"project_id": ai, "confidence": 0.4, "rationale": "again"},
]
assert len(PDB.accept_candidates(dupes, shortlist)) == 1
ok("a repeated id yields ONE proposal, not two")
big = [{"project_id": p["project_id"], "confidence": 0.5} for p in shortlist]
assert len(PDB.accept_candidates(big, shortlist, limit=2)) == 2
ok("the cap is enforced here, before anything reaches the database")

print("\n-- confidence is coerced, never trusted --")
weird = [
    {"project_id": ai, "confidence": 7.5},
    {"project_id": tc, "confidence": -3},
    {"project_id": hc, "confidence": "not a number"},
]
got = {c["project_id"]: c["confidence"] for c in PDB.accept_candidates(weird, shortlist)}
assert got[ai] == 1.0 and got[tc] == 0.0 and got[hc] == 0.0, got
ok(f"out-of-range and non-numeric confidence clamped to 0.0-1.0: {got}")

print("\n-- proposals land as 'proposed', awaiting a human --")
accepted = PDB.accept_candidates(
    [{"project_id": ai, "confidence": 0.8, "rationale": "AI committee"}], shortlist
)
assert PDB.propose_candidates("email", "m-1", accepted) == 1
rows = PDB.links_for_project(ai, state="proposed")
assert len(rows) == 1 and rows[0]["assigned_by"] == "llm", rows
ok("filed as state='proposed', assigned_by='llm'")
assert PDB.projects_for_entity("email", "m-1") == []
ok("a proposal does NOT count as a link until approved")

print("\n-- a decision the user already made is never reopened --")
PDB.set_link_state(ai, "email", "m-1", "rejected")
assert PDB.propose_candidates("email", "m-1", accepted) == 0
ok("a REJECTED project is not re-proposed for the same email")
PDB.link(tc, "email", "m-2", state="confirmed")
accepted_tc = PDB.accept_candidates(
    [{"project_id": tc, "confidence": 0.9}], shortlist
)
assert PDB.propose_candidates("email", "m-2", accepted_tc) == 0
ok("an already-CONFIRMED project is not proposed again either")

print("\n-- the approval queue is readable --")
email_db.upsert_email_project(
    "m-3", "c-3", "Unassigned", "Japan Post chasing TC101 RCA", "2026-09-01", "s"
)
PDB.propose_candidates(
    "email", "m-3",
    PDB.accept_candidates([{"project_id": tc, "confidence": 0.7, "rationale": "TC101 named"}], shortlist),
)
queued = [row for row in PDB.pending_proposals() if row["entity_id"] == "m-3"]
assert len(queued) == 1, queued
assert queued[0]["entity_label"] == "Japan Post chasing TC101 RCA", queued[0]
ok("the queue shows the SUBJECT, not the raw Graph message id")
PDB.propose_candidates(
    "email", "no-subject-on-record",
    PDB.accept_candidates([{"project_id": hc, "confidence": 0.3}], shortlist),
)
fallback = [r for r in PDB.pending_proposals() if r["entity_id"] == "no-subject-on-record"]
assert fallback[0]["entity_label"] == "no-subject-on-record"
ok("...falling back to the id when no subject is on record")

print("\n-- bulk decisions, and refusals reported not swallowed --")
for i in range(4):
    p = PDB.create_project(f"Filler {i}")
    PDB.propose_candidates(
        "email", "m-bulk",
        [{"project_id": p, "confidence": 0.5, "rationale": "x"}],
    )
pending_bulk = [r for r in PDB.pending_proposals() if r["entity_id"] == "m-bulk"]
assert len(pending_bulk) == 4, len(pending_bulk)
applied, errors = PDB.decide_proposals(
    [(r["project_id"], r["entity_type"], r["entity_id"], "confirmed") for r in pending_bulk]
)
assert applied == 3, applied
assert len(errors) == 1 and "maximum" in errors[0], errors
ok(f"approving 4 on one email confirms 3 and REPORTS the refusal: {errors[0][:56]}...")

applied, errors = PDB.decide_proposals([("FAB-000", "email", "x", "rejected")])
ok(f"a decision on a nonexistent link is harmless (applied={applied}, errors={len(errors)})")

print("\n-- the queue renders, with filters and a bulk control --")
at = AppTest.from_file("pages/2_project_management.py", default_timeout=60)
at.session_state["gemini_client"] = None
at.run()
assert not at.exception, at.exception[0].value
ok("project management page renders clean with proposals pending")
# st.data_editor surfaces as a 'dataframe' element in AppTest, so identify it
# by its Decision column rather than by widget type.
queues = [
    d.value for d in at.dataframe if "Decision" in getattr(d.value, "columns", [])
]
assert queues, [list(getattr(d.value, "columns", [])) for d in at.dataframe]
queue_table = queues[0]
ok("the queue is one table with a Decision column, not a pair of buttons per row")
for column in ("Project", "Type", "Item", "Confidence", "Why"):
    assert column in queue_table.columns, (column, list(queue_table.columns))
ok(f"columns give enough to judge on: {[c for c in queue_table.columns if not c.startswith('_')]}")
assert set(queue_table["Decision"]) == {"Leave"}
ok("every row defaults to 'Leave', so nothing is decided by accident")
assert any("Japan Post chasing TC101 RCA" == v for v in queue_table["Item"])
ok("the Item column carries the readable subject")
assert [s for s in at.slider if s.key == "prop_filter_conf"]
assert [m for m in at.multiselect if m.key == "prop_filter_project"]
ok("confidence and project filters are present above the queue")
assert [b for b in at.button if b.key == "prop_all_ok"]
ok("a decide-all-shown control exists for large batches")

shutil.rmtree(scratch, ignore_errors=True)
print("\nP3 TESTS PASSED")
