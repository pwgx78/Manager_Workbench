"""Exercise project_db against a scratch profile, not the real one."""
import os, tempfile, shutil
scratch = tempfile.mkdtemp(prefix="mwb_test_")
os.environ["MANAGER_WORKBENCH_HOME"] = scratch

import db, project_db

db.init_schema()
ok = lambda m: print("  PASS", m)

def fails(fn, label):
    try:
        fn(); print("  FAIL (no error):", label); return False
    except project_db.ProjectError as e:
        print("  PASS refused:", label, "->", str(e)[:60]); return True

print("\n-- prefix --")
assert project_db.get_prefix() == "PRJ", project_db.get_prefix()
project_db.set_prefix("fab-")           # cleaned + uppercased
assert project_db.get_prefix() == "FAB"
ok("prefix cleaned to FAB")
assert not project_db.prefix_is_locked(); ok("unlocked while empty")

print("\n-- minting --")
a = project_db.create_project("AI Gov", keywords="AI Governance, AI committee")
b = project_db.create_project("TC101 Japan Post Quality Issue")
assert a == "FAB-001" and b == "FAB-002", (a, b)
ok(f"minted {a}, {b} (3-digit pad)")
assert project_db.prefix_is_locked(); ok("locked once projects exist")
fails(lambda: project_db.set_prefix("XYZ"), "prefix change after lock")

print("\n-- no id reuse after delete --")
project_db.delete_project(b)
c = project_db.create_project("Headcount", keywords="contractor, requisition")
assert c == "FAB-003", c
ok("deleted FAB-002 -> next is FAB-003, not reused")

print("\n-- normalized resolution --")
for probe in ("AI Gov", "ai  gov", "AI-GOV", "  ai gov  ", "AIGov", "FAB-001", "fab-001"):
    assert project_db.resolve_name(probe) == a, probe
ok("name/id resolve case+punctuation insensitive")
assert project_db.resolve_name("nope") is None; ok("unknown name -> None")
fails(lambda: project_db.create_project("ai gov"), "duplicate name (normalized)")

print("\n-- aliases --")
project_db.add_alias(a, "AIG")
assert project_db.resolve_name("a.i.g") == a; ok("alias resolves, normalized")
assert project_db.resolve_name("TC-101") == project_db.resolve_name("tc101") is None
ok("separator-insensitive fold: TC-101 == tc101 (both unknown here)")
fails(lambda: project_db.add_alias(c, "AIG"), "alias owned by another project")
assert project_db.add_alias(a, "AIG"); ok("re-adding own alias is a no-op")

print("\n-- THE JIRA GUARD (4.3) --")
fails(lambda: project_db.link("FAB-999", "jira", "SPR-60789", create_if_missing=True),
      "jira link with create_if_missing=True cannot mint a project")
fails(lambda: project_db.link("FAB-999", "email", "m1", create_if_missing=False),
      "link to nonexistent project")
project_db.link(a, "jira", "SPR-60789", state="confirmed")
ok("jira CAN link to an existing project")

print("\n-- max 3 confirmed per entity (decision 7) --")
d = project_db.create_project("Resilliency")
e = project_db.create_project("Goals")
for p in (a, c, d):
    project_db.link(p, "email", "msg-1", state="confirmed")
assert len(project_db.confirmed_project_ids("email", "msg-1")) == 3
fails(lambda: project_db.link(e, "email", "msg-1", state="confirmed"), "4th confirmed")
project_db.link(e, "email", "msg-1", state="proposed")
ok("4th PROPOSED is allowed (cap counts confirmed only)")
project_db.link(a, "email", "msg-1", state="confirmed")
ok("re-confirming an existing link is not blocked by the cap")

print("\n-- rejection persists (4.4) --")
project_db.set_link_state(e, "email", "msg-1", "rejected")
rows = project_db.links_for_project(e, state="rejected")
assert len(rows) == 1, rows
ok("rejected link is retained, not deleted")

print("\n-- description: user wins --")
project_db.update_project(d, description="Hand written.")
assert project_db.set_llm_description(d, "LLM text") is False
assert project_db.get_project(d)["description"] == "Hand written."
ok("set_llm_description declines over a user description")
assert project_db.set_llm_description(e, "LLM text") is True
assert project_db.get_project(e)["description_source"] == "llm"
ok("llm description accepted where none was user-written")

print("\n-- close / reopen --")
project_db.close_project(d)
assert project_db.get_project(d)["status"] == "closed"
assert d not in [p["project_id"] for p in project_db.list_projects()]
ok("closed project drops out of the active candidate list")
assert d in [p["project_id"] for p in project_db.list_projects(include_closed=True)]
project_db.reopen_project(d)
assert project_db.get_project(d)["close_date"] is None
ok("reopen clears close_date")

print("\n-- merge --")
project_db.link(e, "shipment", "TRACK1", state="confirmed")
project_db.merge_projects(e, a)
assert project_db.get_project(e) is None
assert project_db.resolve_name("Goals") == a
assert "TRACK1" in [l["entity_id"] for l in project_db.links_for_project(a)]
ok("merge moved links, kept the name as an alias, deleted the source")

print("\n-- counts / proposals --")
print("  link_counts:", project_db.link_counts())
print("  pending:", [(p['project_id'], p['entity_type']) for p in project_db.pending_proposals()])

print("\n-- absorb special_projects --")
import config
config.save_special_projects([
    {"subject": "AI Gov", "keywords": "dup"},          # collides -> skipped
    {"subject": "Core Compliance", "keywords": ""},
    {"subject": "", "keywords": "blank -> ignored"},
])
r1 = project_db.absorb_special_projects()
print("  first run:", {k: r1[k] for k in ("absorbed","skipped","already_done")}, r1["skipped_names"])
assert r1["absorbed"] == 1 and r1["skipped_names"] == ["AI Gov"]
r2 = project_db.absorb_special_projects()
assert r2["already_done"] and r2["absorbed"] == 0
ok("absorb is idempotent and skips name collisions")

print("\n-- overflow past 999 --")
db.set_meta(project_db.COUNTER_KEY, 999)
big = project_db.create_project("Overflow test")
assert big == "FAB-1000", big
ok("graceful overflow: FAB-1000")

shutil.rmtree(scratch, ignore_errors=True)
print("\nALL TESTS PASSED")
