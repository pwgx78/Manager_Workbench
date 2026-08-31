"""Render pages/2_project_management.py for real via Streamlit's AppTest.

Runs against a scratch profile so the live workbench is never touched.
"""
import os
import shutil
import tempfile

scratch = tempfile.mkdtemp(prefix="mwb_page_")
os.environ["MANAGER_WORKBENCH_HOME"] = scratch

import db  # noqa: E402  (must import after the env var is set)
import config  # noqa: E402
import project_db  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

db.init_schema()

# The nine legacy rows this profile should offer to absorb.
config.save_special_projects(
    [
        {"subject": "AI Gov", "keywords": "AI Governance, AI committee"},
        {"subject": "AI tool", "keywords": "AI tool development"},
        {"subject": "EDCR", "keywords": ""},
        {"subject": "Resilliency", "keywords": ""},
        {"subject": "Headcount", "keywords": "contractor, requisition"},
        {"subject": "Talent Planning", "keywords": ""},
        {"subject": "Promotions", "keywords": ""},
        {"subject": "Goals", "keywords": ""},
        {"subject": "Core Compliance", "keywords": ""},
    ]
)


def run(label):
    at = AppTest.from_file("pages/2_project_management.py", default_timeout=60)
    at.session_state["gemini_client"] = None  # no LLM calls in this test
    at.run()
    if at.exception:
        print(f"  FAIL {label}: {at.exception[0].value}")
        raise SystemExit(1)
    print(f"  PASS {label} rendered clean")
    return at


print("\n-- 1. first run: empty register --")
at = run("empty register")
assert any("prefix" in str(i.value).lower() for i in at.info), [i.value for i in at.info]
print("  PASS prefix setup prompt shown while unlocked")
assert any("9" in str(m.value) for m in at.markdown), "seed panel missing"
print("  PASS seed panel offers the 9 legacy entries")

print("\n-- 2. set the prefix through the UI --")
at.text_input[0].set_value("fab").run()
at.button[0].click().run()
assert project_db.get_prefix() == "FAB", project_db.get_prefix()
print("  PASS prefix set to FAB via the form")

print("\n-- 3. import the legacy Special Projects --")
at = run("with seed panel")
imported = False
for btn in at.button:
    if "Import" in str(btn.label):
        btn.click().run()
        imported = True
        break
assert imported, "Import button not found"
assert project_db.count_projects() == 9, project_db.count_projects()
ids = [p["project_id"] for p in project_db.list_projects()]
assert "FAB-001" in ids and "FAB-009" in ids, ids
print(f"  PASS imported 9 projects: FAB-001 … FAB-009")
assert project_db.has_absorbed_special_projects()
print("  PASS absorption flagged, so the panel will not reappear")

print("\n-- 4. populated register renders, prefix now locked --")
at = run("populated register")
assert project_db.prefix_is_locked()
assert any("locked" in str(c.value) for c in at.caption), [c.value for c in at.caption]
print("  PASS prefix shown as locked")
assert len(at.dataframe) >= 1, "register table missing"
table = at.dataframe[0].value
assert len(table) == 9, len(table)
for column in ("ID", "Name", "Status", "Emails", "Jira", "Linked"):
    assert column in table.columns, (column, list(table.columns))
print(f"  PASS register table has 9 rows and columns {list(table.columns)}")

print("\n-- 5. detail pane + proposals tab --")
assert len(at.selectbox) >= 1, "detail selectbox missing"
print(f"  PASS detail selector offers {len(at.selectbox[0].options)} projects")
assert any(
    "Nothing awaiting approval" in str(i.value) for i in at.info
), [i.value for i in at.info]
print("  PASS Proposals tab shows an honest zero")

print("\n-- 6. a closed project leaves the default view --")
project_db.close_project("FAB-001")
at = run("with a closed project")
assert len(at.dataframe[0].value) == 8, len(at.dataframe[0].value)
print("  PASS closed project excluded from the active register (8 of 9)")

shutil.rmtree(scratch, ignore_errors=True)
print("\nPAGE RENDER TESTS PASSED")
