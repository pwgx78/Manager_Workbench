"""The 'delete ALL projects' control in Project Management.

Driven through the real UI, because the guard on an irreversible bulk delete is
the whole point: an earlier version of a similar control relied on `disabled=`
alone, which only stops the click in the browser, and a test that clicked
through it wiped the data.

Runs against a scratch profile so the live workbench is never touched.
"""
import os
import shutil
import tempfile

scratch = tempfile.mkdtemp(prefix="mwb_wipe_")
os.environ["MANAGER_WORKBENCH_HOME"] = scratch

import config  # noqa: E402
import db  # noqa: E402
import project_db as PDB  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

db.init_schema()
ok = lambda m: print("  PASS", m)


def seed():
    """Three projects — one closed — with an alias and a link, so the wipe has
    something in every table to remove."""
    PDB.delete_all_projects()
    a = PDB.create_project("AI Gov", keywords="AI Governance")
    b = PDB.create_project("TC101 Japan Post", keywords="Japan Post")
    c = PDB.create_project("Retired Work")
    PDB.add_alias(a, "AIG")
    PDB.link(a, "email", "m-1", state="confirmed")
    PDB.link(b, "email", "m-2", state="proposed")
    PDB.close_project(c)
    return a, b, c


def run(label):
    at = AppTest.from_file("pages/2_project_management.py", default_timeout=60)
    at.session_state["gemini_client"] = None
    at.run()
    if at.exception:
        print(f"  FAIL {label}: {at.exception[0].value}")
        raise SystemExit(1)
    return at


def widget(at, kind, key):
    found = [w for w in getattr(at, kind) if w.key == key]
    assert found, f"{kind} {key!r} missing: {[w.key for w in getattr(at, kind)]}"
    return found[0]


PDB.set_prefix("FAB")

print("\n-- hidden when there is nothing to delete --")
PDB.delete_all_projects()
at = run("empty register")
assert not [b for b in at.button if b.key == "wipe_btn"]
ok("no delete-all button when the register is empty")

print("\n-- present, and double-gated, once projects exist --")
a, b, c = seed()
assert PDB.count_projects(include_closed=True) == 3
at = run("populated register")
assert widget(at, "button", "wipe_btn").disabled
ok("button starts DISABLED")
assert widget(at, "text_input", "wipe_typed").disabled
ok("the type-to-confirm box is disabled until 'Are you sure' is ticked")

print("\n-- neither confirmation alone is enough --")
at = run("tick only")
widget(at, "checkbox", "wipe_sure").check().run()
assert not widget(at, "text_input", "wipe_typed").disabled
ok("ticking the box enables the text box")
assert widget(at, "button", "wipe_btn").disabled
ok("...but the button stays disabled with the word untyped")
widget(at, "button", "wipe_btn").click().run()
assert PDB.count_projects(include_closed=True) == 3
ok("clicking through the disabled button does NOT delete — the handler re-checks")

at = run("type only")
widget(at, "text_input", "wipe_typed").set_value("delete").run()
assert widget(at, "button", "wipe_btn").disabled
ok("typing the word without ticking the box leaves the button disabled")
widget(at, "button", "wipe_btn").click().run()
assert PDB.count_projects(include_closed=True) == 3
ok("and clicking it anyway still deletes nothing")

print("\n-- the wrong word is not accepted --")
at = run("wrong word")
widget(at, "checkbox", "wipe_sure").check().run()
widget(at, "text_input", "wipe_typed").set_value("yes").run()
assert widget(at, "button", "wipe_btn").disabled
widget(at, "button", "wipe_btn").click().run()
assert PDB.count_projects(include_closed=True) == 3
ok("'yes' is refused; only the word 'delete' counts")

print("\n-- case and stray whitespace are tolerated --")
at = run("uppercase")
widget(at, "checkbox", "wipe_sure").check().run()
widget(at, "text_input", "wipe_typed").set_value("  DELETE  ").run()
assert not widget(at, "button", "wipe_btn").disabled
ok("'  DELETE  ' enables it — the confirmation is not a typing test")

print("\n-- both confirmations: everything goes --")
at = run("confirmed wipe")
widget(at, "checkbox", "wipe_sure").check().run()
widget(at, "text_input", "wipe_typed").set_value("delete").run()
assert not widget(at, "button", "wipe_btn").disabled
widget(at, "button", "wipe_btn").click().run()

assert PDB.count_projects(include_closed=True) == 0
ok("all 3 projects deleted, including the CLOSED one")
conn = db.connect()
assert conn.execute("SELECT COUNT(*) FROM project_aliases").fetchone()[0] == 0
assert conn.execute("SELECT COUNT(*) FROM project_links").fetchone()[0] == 0
conn.close()
ok("aliases and links went with them — no orphans left behind")

print("\n-- ids are not reissued after a wipe --")
fresh = PDB.create_project("Something New")
assert fresh != "FAB-001", fresh
assert fresh == "FAB-004", fresh
ok(f"next project is {fresh}, continuing the sequence rather than reusing FAB-001")

print("\n-- the legacy import is offered again --")
PDB.delete_all_projects()
config.save_special_projects([{"subject": "AI Gov", "keywords": ""}])
PDB.absorb_special_projects()
assert PDB.has_absorbed_special_projects()
PDB.delete_all_projects()
PDB.clear_absorbed_flag()
assert not PDB.has_absorbed_special_projects()
ok("clearing the flag restores the seeding offer, so an empty register is not a dead end")

print("\n-- delete_all_projects on an already-empty register --")
PDB.delete_all_projects()
assert PDB.delete_all_projects() == 0
ok("returns 0 and does not raise")

shutil.rmtree(scratch, ignore_errors=True)
print("\nDELETE-ALL TESTS PASSED")
