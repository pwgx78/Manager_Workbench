"""Mailbox-wide received counting: folder classification and the scope marker.

The bug this covers: cloud-side rules file mail out of the Inbox, so counting
only the Inbox undercounts. Received is now the whole mailbox minus an
exclusion list.

Runs against a scratch profile so the live workbench is never touched.
"""
import os
import shutil
import tempfile

scratch = tempfile.mkdtemp(prefix="mwb_folders_")
os.environ["MANAGER_WORKBENCH_HOME"] = tempfile.mkdtemp(prefix="mwb_folders_home_")

import db  # noqa: E402
import email_volume as EV  # noqa: E402

db.init_schema()
ok = lambda m: print("  PASS", m)

# A fake mailbox: every well-known folder gets a stable id, except 'clutter'
# and the failure folders, which this mailbox does not have (Graph answers 404).
MISSING = {"clutter", "localfailures", "serverfailures", "scheduled"}
CALLS = []


def fake_resolve(name):
    CALLS.append(name)
    if name in MISSING:
        return None
    if name == "boom":
        raise RuntimeError("Graph exploded")
    return f"id-{name}"


print("\n-- resolving and caching folder ids --")
ids = EV.folder_ids(resolve=fake_resolve)
assert set(CALLS) == set(EV.WELL_KNOWN_FOLDERS), set(EV.WELL_KNOWN_FOLDERS) - set(CALLS)
ok(f"resolved all {len(EV.WELL_KNOWN_FOLDERS)} well-known folders in one pass")
assert "clutter" not in ids and "inbox" in ids
ok("a folder this mailbox lacks (404 -> None) is simply absent, not an error")

CALLS.clear()
again = EV.folder_ids(resolve=fake_resolve)
assert again == ids and CALLS == []
ok("second call is served from the profile cache — no Graph round trips")
assert EV.cached_folder_ids() == ids
ok("cached_folder_ids() reads the cache without contacting Graph")

CALLS.clear()
EV.folder_ids(resolve=fake_resolve, refresh=True)
assert CALLS, "refresh=True did not re-resolve"
ok("refresh=True forces re-resolution")


def resolve_raising(name):
    raise RuntimeError("Graph is down")


EV.folder_ids(resolve=resolve_raising, refresh=True)
ok("a resolver that throws for every folder does not abort the fetch")
EV.folder_ids(resolve=fake_resolve, refresh=True)  # restore

print("\n-- classification: what counts as received --")
for name in ("inbox", "archive", "deleteditems"):
    assert EV.classify(f"id-{name}", ids) == EV.RECEIVED, name
    ok(f"{name:<13} -> received")

assert EV.classify("id-sentitems", ids) == EV.SENT
ok("sentitems     -> sent")

for name in ("junkemail", "drafts", "outbox", "conversationhistory", "syncissues"):
    assert EV.classify(f"id-{name}", ids) is None, name
    ok(f"{name:<13} -> ignored")

print("\n-- THE FIX: an arbitrary rule-target folder counts as received --")
assert EV.classify("id-of-some-folder-called-Newsletters", ids) == EV.RECEIVED
ok("an unrecognized custom folder counts as received")
assert EV.classify("id-of-a-deeply-nested-subfolder", ids) == EV.RECEIVED
ok("nesting is irrelevant — no folder tree is walked at all")
assert EV.classify(None, ids) == EV.RECEIVED
assert EV.classify("", ids) == EV.RECEIVED
ok("a message with no parentFolderId still counts (errs toward counting)")

# The failure mode that matters: if resolution failed entirely, mail must still
# be counted rather than silently dropped.
assert EV.classify("id-inbox", {}) == EV.RECEIVED
assert EV.classify("id-junkemail", {}) == EV.RECEIVED
ok("with NO resolved ids everything counts — undercounting is never the failure")

print("\n-- storing a mailbox-wide fetch --")


def msg(mid, folder, ts="2026-08-03T09:00:00Z"):
    return {
        "id": mid,
        "receivedDateTime": ts,
        "subject": f"subject {mid}",
        "parentFolderId": f"id-{folder}" if folder else None,
        "from": {"emailAddress": {"name": "A Sender", "address": "s@x.com"}},
    }


batch = [
    msg("m-inbox", "inbox"),
    msg("m-archive", "archive"),
    msg("m-deleted", "deleteditems"),
    msg("m-rule", "Newsletters"),          # unrecognized -> counted
    msg("m-nested", "Deep/Sub/Folder"),    # unrecognized -> counted
    msg("m-junk", "junkemail"),            # ignored
    msg("m-draft", "drafts"),              # ignored
    msg("m-teams", "conversationhistory"),  # ignored
    msg("m-sent", "sentitems"),            # skipped; the sent pass owns it
]
stored, ignored, sent_skipped = EV.upsert_mailbox_messages(batch, ids)
assert (stored, ignored, sent_skipped) == (5, 3, 1), (stored, ignored, sent_skipped)
ok(f"stored {stored} received, ignored {ignored}, skipped {sent_skipped} sent-folder")
assert EV.totals()[EV.RECEIVED] == 5
ok("only the five received rows landed in the table")

rows = set(EV.load_rows()["subject"])
assert "subject m-rule" in rows and "subject m-nested" in rows
ok("the rule-filed mail — the whole point — is present")
assert "subject m-junk" not in rows and "subject m-sent" not in rows
ok("junk and sent-folder mail are absent from the received series")

print("\n-- the sent pass is unchanged and dates by sentDateTime --")
EV.upsert_messages(
    [
        {
            "id": "s-1",
            "sentDateTime": "2026-08-03T17:00:00Z",
            "subject": "outbound",
            "toRecipients": [{"emailAddress": {"name": "R", "address": "r@x.com"}}],
        }
    ],
    EV.SENT,
)
assert EV.totals() == {EV.RECEIVED: 5, EV.SENT: 1}
sent_row = EV.load_rows(directions=(EV.SENT,)).iloc[0]
assert sent_row["ts"] == "2026-08-03T17:00:00Z"
ok("sent mail is dated by sentDateTime, not receivedDateTime")
assert sent_row["counterparty_name"] == "R"
ok("sent mail still stores the recipient as counterparty")

print("\n-- idempotency across a re-fetch --")
EV.upsert_mailbox_messages(batch, ids)
assert EV.totals()[EV.RECEIVED] == 5
ok("re-running the same mailbox fetch does not double-count")

print("\n-- the scope marker --")
db.set_meta(EV.SCOPE_KEY, "")
assert not EV.scope_is_current()
ok("data gathered under an older definition is detected as stale")
EV.mark_scope()
assert EV.scope_is_current()
ok("mark_scope() clears the warning")
assert EV.SCOPE == "mailbox-v2"
ok(f"current scope is {EV.SCOPE!r}")

print("\n-- what the UI tells the user is counted --")
named = EV.counted_folder_names(ids)
assert "inbox" in named and "deleteditems" in named
assert "clutter" not in named, named
ok(f"names only folders this mailbox actually has: {named}")

shutil.rmtree(scratch, ignore_errors=True)
print("\nFOLDER CLASSIFICATION TESTS PASSED")
