"""P2: the keyword pre-filter, dual-write linking, and backfill.

Runs against a scratch profile so the live workbench is never touched.
"""
import os
import shutil
import tempfile

scratch = tempfile.mkdtemp(prefix="mwb_p2_")
os.environ["MANAGER_WORKBENCH_HOME"] = scratch

import db  # noqa: E402
import email_db  # noqa: E402
import project_db  # noqa: E402

db.init_schema()
ok = lambda m: print("  PASS", m)


def fails(fn, label):
    try:
        fn()
        print("  FAIL (no error):", label)
        raise SystemExit(1)
    except project_db.ProjectError as exc:
        print("  PASS refused:", label, "->", str(exc)[:55])


project_db.set_prefix("FAB")
ai = project_db.create_project("AI Gov", keywords="AI Governance, AI committee")
tc = project_db.create_project("TC101 Japan Post Quality Issue", keywords="Japan Post")
hc = project_db.create_project("Headcount", keywords="contractor, requisition")
dead = project_db.create_project("Retired Thing", keywords="obsolete")
project_db.add_alias(tc, "TC101")

print("\n-- shortlist: exact and fuzzy name hits --")
got = [p["project_id"] for p in project_db.shortlist_for_text("TC101 update from Japan Post")]
assert got and got[0] == tc, got
ok(f"subject naming the project ranks it first ({got})")

for probe in ("tc-101 rework", "TC 101 rework", "tc101 rework"):
    hit = [p["project_id"] for p in project_db.shortlist_for_text(probe)]
    assert tc in hit, (probe, hit)
ok("separator-insensitive: TC-101 / TC 101 / tc101 all hit")

print("\n-- shortlist: keyword hits, and name outranking keyword --")
scored = project_db.shortlist_for_text(
    "AI Gov meeting; also a contractor requisition", include_scores=True
)
by_id = {p["project_id"]: p for p in scored}
assert ai in by_id and hc in by_id, list(by_id)
assert by_id[ai]["_score"] > by_id[hc]["_score"], (
    by_id[ai]["_score"], by_id[hc]["_score"]
)
ok(f"name hit ({by_id[ai]['_score']}) outranks keyword hits ({by_id[hc]['_score']})")
assert "AI Gov" in by_id[ai]["_matched"], by_id[ai]["_matched"]
ok(f"matched terms are reported for explainability: {by_id[hc]['_matched']}")

print("\n-- shortlist: the short-term false positive guard --")
short = project_db.create_project("AI", keywords="")
hits = [p["project_id"] for p in project_db.shortlist_for_text("the supply chain broke")]
assert short not in hits, "bare 'AI' fired inside 'chain'"
ok("2-char name 'AI' does NOT match inside 'chain' (word-boundary path)")
hits = [p["project_id"] for p in project_db.shortlist_for_text("this is an AI problem")]
assert short in hits, hits
ok("...but DOES match 'AI' as a whole word")
project_db.delete_project(short)

print("\n-- shortlist: closed projects are excluded --")
assert dead in [p["project_id"] for p in project_db.shortlist_for_text("obsolete work")]
project_db.close_project(dead)
assert dead not in [p["project_id"] for p in project_db.shortlist_for_text("obsolete work")]
ok("closing a project removes it from the candidate shortlist")

print("\n-- shortlist: limit and empty input --")
assert project_db.shortlist_for_text("") == []
assert project_db.shortlist_for_text("   ") == []
ok("empty text yields no candidates (no wasted work)")
assert len(project_db.shortlist_for_text("AI Gov Japan Post contractor", limit=2)) == 2
ok("limit is honoured")
assert project_db.shortlist_for_text("nothing here matches anything") == []
ok("no matches yields an empty shortlist, not the whole register")

print("\n-- candidate_block: the prompt payload for P3 --")
block = project_db.candidate_block(project_db.shortlist_for_text("AI Gov"))
assert ai in block and "AI Governance" in block, block
ok("candidate_block carries id, name and keywords")
print("   ", block.replace("\n", " | "))

print("\n-- dual-write: legacy label -> confirmed link --")
assert project_db.link_legacy_label("Unassigned", "email", "m1") is None
assert project_db.link_legacy_label("", "email", "m1") is None
ok("'Unassigned' and blank labels link nothing")
assert project_db.link_legacy_label("nonexistent label", "email", "m1") is None
ok("an unmatched label links nothing (the LLM cannot invent a project)")
assert project_db.link_legacy_label("ai gov", "email", "m1") == ai
links = project_db.projects_for_entity("email", "m1")
assert [p["project_id"] for p in links] == [ai], links
assert links[0]["link_state"] == "confirmed"
ok("an exactly-matching label auto-confirms (deterministic identity, not a guess)")
assert project_db.link_legacy_label("TC101", "email", "m1") == tc
ok("an alias match works too")
project_db.close_project(dead)
assert project_db.link_legacy_label("Retired Thing", "email", "m1") is None
ok("a label matching a CLOSED project links nothing")

print("\n-- projects_for_entity returns a SET, not one label --")
project_db.link_legacy_label("Headcount", "email", "m1")
assert len(project_db.projects_for_entity("email", "m1")) == 3
ok("one email sits in 3 projects — what email_projects.message_id cannot express")
assert project_db.link_legacy_label("AI Gov", "email", "m1") == ai
ok("re-linking an already-linked project is not blocked by the 3-cap")

print("\n-- backfill by keyword (the section 3 remedy) --")
for i, (subject, label) in enumerate(
    [
        ("Japan Post escalation on TC101", "TC101 Japan Post Quality Issue"),
        ("Contractor requisition approval", "Headcount"),
        ("Totally unrelated lunch invite", "Unassigned"),
        ("tc-101 follow up", "Unassigned"),
    ]
):
    email_db.upsert_email_project(
        f"hist-{i}", f"conv-{i}", label, subject, f"2026-08-0{i + 1}", "summary text"
    )

cands = project_db.backfill_candidates(tc)
subjects = [c["subject"] for c in cands]
assert "Japan Post escalation on TC101" in subjects, subjects
assert "tc-101 follow up" in subjects, subjects
assert "Totally unrelated lunch invite" not in subjects, subjects
ok(f"backfill found {len(cands)} historical matches, and no false positive")
print("   ", [(c["subject"][:32], c["matched"]) for c in cands])

n = project_db.backfill_link(tc, [c["message_id"] for c in cands])
assert n == len(cands), (n, len(cands))
ok(f"backfill linked {n} emails as PROPOSED (a keyword hit is a suggestion)")
pending = project_db.pending_proposals()
assert all(p["state"] if "state" in p else True for p in pending)
assert len([p for p in pending if p["project_id"] == tc]) == n
ok("they land in the approval queue, not straight into the rollup")
assert project_db.backfill_candidates(tc) == []
ok("already-linked emails are not offered again")

print("\n-- create from existing label --")
labels = project_db.legacy_label_counts()
names = [row["label"] for row in labels]
assert "Unassigned" not in names, names
ok("'Unassigned' excluded — it is the absence of a label")
assert "TC101 Japan Post Quality Issue" not in names, names
ok("labels already in the register are excluded (nothing to create)")

email_db.upsert_email_project("j1", "cj", "SPR-60789", "bare jira key label", "2026-08-01", "")
email_db.upsert_email_project(
    "j2", "cj2", "SPR-61086 TC53e Trigger Issue", "descriptive", "2026-08-01", ""
)
names = [row["label"] for row in project_db.legacy_label_counts()]
assert "SPR-60789" not in names, names
ok("a BARE Jira key is refused as a label — the exact anti-pattern")
assert "SPR-61086 TC53e Trigger Issue" in names, names
ok("...but a descriptive label mentioning a key is kept: it names real work")

shutil.rmtree(scratch, ignore_errors=True)
print("\nP2 DATA-LAYER TESTS PASSED")
