"""P3 end to end: analyze an email, get proposals, approve one.

This is the path the other suites could not reach. The analysis loop sits behind
a button that calls Graph and then Gemini, so it was only ever exercised by the
user in anger. Here BOTH are stubbed — api_helpers.fetch_recent_inbox and
llm_prompts.generate are replaced before the page is imported, so the page's
`from api_helpers import ...` binds to the fakes — and the whole
fetch -> analyze -> propose -> approve chain runs with no network and no model.

Runs against a scratch profile so the live workbench is never touched.
"""
import json
import os
import shutil
import tempfile

scratch = tempfile.mkdtemp(prefix="mwb_p3e2e_")
os.environ["MANAGER_WORKBENCH_HOME"] = scratch

import api_helpers  # noqa: E402
import db  # noqa: E402
import email_db  # noqa: E402
import llm_prompts  # noqa: E402
import project_db as PDB  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

db.init_schema()
ok = lambda m: print("  PASS", m)

PDB.set_prefix("FAB")
ai = PDB.create_project("AI Gov", keywords="AI Governance, AI committee")
tc = PDB.create_project("TC101 Japan Post", keywords="Japan Post, TC101")
PDB.create_project("Headcount", keywords="contractor")

MESSAGE = {
    "id": "msg-e2e-1",
    "conversationId": "conv-e2e-1",
    "subject": "Japan Post escalation on TC101 quality",
    "receivedDateTime": "2026-09-03T09:00:00Z",
    "sender": {"emailAddress": {"name": "A Customer", "address": "cust@x.com"}},
    "toRecipients": [{"emailAddress": {"address": "me@zebra.com"}}],
    "body": {"content": "<p>Japan Post are chasing the TC101 RCA. Please advise.</p>"},
}

# The model's answer: one genuine hit, and one INVENTED id, so the end-to-end
# run proves the validation gate holds in the real code path rather than only
# in a unit test.
ANALYSIS = {
    "extracted_tasks": [
        {
            "Action": "Send Japan Post the TC101 RCA status",
            "Origin": "Email",
            "Email Thread": MESSAGE["subject"],
            "Due Date": "Unspecified",
            "Owner": "me",
            "Quadrant": "Urgent",
            "Priority": "High",
            "Requested_Of_Me": True,
            "Next Step": "Reply with current RCA state",
            "Suggested Response": "We are on it.",
        }
    ],
    "new_context_summary": "Japan Post chasing TC101 RCA.",
    # "Unassigned" ON PURPOSE. Were the legacy label equal to a registered
    # project's name, the deterministic dual-write would confirm it outright and
    # the model's proposal would be correctly skipped as redundant. P3 earns its
    # keep precisely when the label does NOT resolve but the content does, so
    # that is the case exercised here.
    "Project": "Unassigned",
    "shipment": None,
    "project_candidates": [
        {"project_id": "FAB-002", "confidence": 0.92, "rationale": "TC101 and Japan Post both named"},
        {"project_id": "FAB-999", "confidence": 0.99, "rationale": "invented id, must be dropped"},
    ],
}

PROMPTS = []


def fake_generate(client, contents, **kwargs):
    PROMPTS.append(contents[0] if isinstance(contents, list) else contents)
    return json.dumps(ANALYSIS)


api_helpers.fetch_recent_inbox = lambda since_iso: [MESSAGE]
llm_prompts.generate = fake_generate

print("\n-- run the analyzer --")
at = AppTest.from_file("pages/0_email_actions.py", default_timeout=90)
at.session_state["gemini_client"] = object()
at.run()
assert not at.exception, at.exception[0].value
[b for b in at.button if "Fetch and Analyze" in str(b.label)][0].click().run()
assert not at.exception, at.exception[0].value
ok("fetch -> analyze completed with Graph and Gemini both stubbed")

print("\n-- the shortlist really did ride in the existing call --")
assert len(PROMPTS) >= 1, PROMPTS
action_prompts = [p for p in PROMPTS if "CANDIDATE PROJECTS" in p]
assert action_prompts, "no prompt carried a candidate shortlist"
prompt = action_prompts[0]
ok(f"the analysis prompt carried the shortlist ({len(PROMPTS)} call(s) total)")
assert tc in prompt, prompt[prompt.index("CANDIDATE") : prompt.index("CANDIDATE") + 200]
ok(f"the relevant project {tc} was offered")
assert "Japan Post" in prompt
ok("...along with its keywords, so ranking has something to go on")
# The pre-filter's job: irrelevant projects never reach the prompt. This is what
# keeps prompt cost FLAT as the register grows instead of scaling with it.
assert ai not in prompt, "an unrelated project was sent to the model"
ok(f"the unrelated {ai} (AI Gov) was NOT offered — cost stays flat as the register grows")

print("\n-- proposals were filed, and the invented id was NOT --")
proposals = PDB.pending_proposals()
proposed_ids = {p["project_id"] for p in proposals}
assert tc in proposed_ids, proposals
ok(f"{tc} proposed from the model's ranking")
assert "FAB-999" not in proposed_ids
ok("the invented FAB-999 was dropped by the gate, in the real code path")
assert len(proposals) == 1, proposals
ok("exactly one proposal — nothing else leaked through")
row = proposals[0]
assert row["confidence"] == 0.92, row
# pending_proposals() is the queue view and omits assigned_by, so check the
# provenance on the link row itself.
link_row = PDB.links_for_project(tc, state="proposed")[0]
assert link_row["assigned_by"] == "llm", link_row
ok(f"recorded assigned_by='llm', confidence={row['confidence']}")
assert "TC101" in row["rationale"], row["rationale"]
ok(f"the model's rationale is carried through: {row['rationale']!r}")
assert row["entity_label"] == MESSAGE["subject"], row
ok("the queue shows the subject line")

print("\n-- the legacy label still written, nothing regressed --")
stored = email_db.get_emails_for_project("Unassigned")
assert "msg-e2e-1" in [mid for _s, _r, _sum, mid in stored], stored
ok("the legacy free-text Project label row was written as before")
# get_project_for_message deliberately maps 'Unassigned' to None — the absence
# of a label rather than a label of its own.
assert email_db.get_project_for_message("msg-e2e-1") is None
ok("...and 'Unassigned' reads back as no label, as that helper intends")
# 'Unassigned' resolves to nothing, so the dual-write linked nothing and the
# model's ranking is the ONLY route into the register for this email.
assert PDB.projects_for_entity("email", "msg-e2e-1") == []
ok("nothing was auto-confirmed — the proposal is the only route, awaiting approval")

print("\n-- approving through the queue --")
applied, errors = PDB.decide_proposals(
    [(row["project_id"], row["entity_type"], row["entity_id"], "confirmed")]
)
assert applied == 1 and not errors, (applied, errors)
assert PDB.pending_proposals() == []
ok("approving clears it from the queue")
assert tc in [p["project_id"] for p in PDB.projects_for_entity("email", "msg-e2e-1")]
ok("and the email is now linked to the project")

print("\n-- re-running does not re-bill or re-propose --")
calls_before = len(PROMPTS)
at = AppTest.from_file("pages/0_email_actions.py", default_timeout=90)
at.session_state["gemini_client"] = object()
at.run()
[b for b in at.button if "Fetch and Analyze" in str(b.label)][0].click().run()
assert not at.exception, at.exception[0].value
assert len(PROMPTS) == calls_before, (calls_before, len(PROMPTS))
ok("the cached analysis was reused — no second model call for the same email")
assert PDB.pending_proposals() == []
ok("and the approved decision was not reopened by the re-run")

shutil.rmtree(scratch, ignore_errors=True)
print("\nP3 END-TO-END TESTS PASSED")
