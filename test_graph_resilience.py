"""Graph resilience for the volume fetches: retry, backoff, window splitting.

Covers the 502 the user hit: a mailbox-wide query with $top=999 and an $orderby
was more than Graph would serve, and there was no retry.

requests.get is stubbed throughout — nothing here contacts Graph.

Runs against a scratch profile so the live workbench is never touched.
"""
import os
import shutil
import tempfile

scratch = tempfile.mkdtemp(prefix="mwb_graph_")
os.environ["MANAGER_WORKBENCH_HOME"] = scratch

import api_helpers as AH  # noqa: E402

ok = lambda m: print("  PASS", m)

# Never actually sleep; record what the backoff would have waited.
SLEPT = []
AH.time.sleep = lambda s: SLEPT.append(s)
AH._graph_headers = lambda: {"Authorization": "Bearer test"}


class FakeResponse:
    def __init__(self, status, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text or ""

    def json(self):
        return self._payload


def stub(responses):
    """Serve `responses` in order; record the URLs requested."""
    calls = []
    queue = list(responses)

    def fake_get(url, headers=None, verify=None):
        calls.append(url)
        return queue.pop(0) if queue else FakeResponse(200, {"value": []})

    AH.requests.get = fake_get
    return calls


print("\n-- the query no longer asks Graph to do the expensive part --")
calls = stub([FakeResponse(200, {"value": []})])
AH.fetch_mailbox_messages("receivedDateTime", "2026-08-01T00:00:00Z", "2026-08-10T00:00:00Z")
url = calls[0]
assert "$orderby" not in url, url
ok("no $orderby — the caller counts, so sort order is irrelevant server work")
assert f"$top={AH.VOLUME_PAGE_SIZE}" in url and AH.VOLUME_PAGE_SIZE <= 250, url
ok(f"page size is {AH.VOLUME_PAGE_SIZE}, well under the 1000 that timed out")
assert "$select=" in url and "body" not in url
ok("still selects no message bodies")

calls = stub([FakeResponse(200, {"value": []})])
AH.fetch_mail_volume("sentitems", "sentDateTime", "2026-08-01T00:00:00Z", "2026-08-10T00:00:00Z")
assert "$orderby" not in calls[0], calls[0]
ok("the sent fetch got the same treatment")

print("\n-- retry on transient gateway errors --")
for status in (502, 503, 504, 500):
    SLEPT.clear()
    calls = stub([FakeResponse(status, text="boom"), FakeResponse(200, {"value": [{"id": "a"}]})])
    got = AH.fetch_mailbox_messages("receivedDateTime", "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z")
    assert len(got) == 1, got
    assert len(calls) == 2, calls
    ok(f"{status} is retried and then succeeds (waited {SLEPT})")

print("\n-- throttling honours Retry-After --")
SLEPT.clear()
stub([
    FakeResponse(429, headers={"Retry-After": "7"}, text="throttled"),
    FakeResponse(200, {"value": []}),
])
AH.fetch_mailbox_messages("receivedDateTime", "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z")
assert SLEPT == [7.0], SLEPT
ok("waited exactly the 7s Graph asked for, not a guess")

SLEPT.clear()
stub([
    FakeResponse(429, headers={"Retry-After": "not-a-number"}),
    FakeResponse(200, {"value": []}),
])
AH.fetch_mailbox_messages("receivedDateTime", "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z")
assert SLEPT and SLEPT[0] > 0, SLEPT
ok("an unparseable Retry-After falls back to the backoff, not a crash")

print("\n-- backoff grows, and is capped --")
SLEPT.clear()
stub([FakeResponse(502) for _ in range(AH.GRAPH_MAX_ATTEMPTS)])
try:
    AH.fetch_mailbox_messages("receivedDateTime", "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z")
    print("  FAIL persistent 502 did not raise")
    raise SystemExit(1)
except RuntimeError as exc:
    message = str(exc)
assert SLEPT == sorted(SLEPT), SLEPT
assert max(SLEPT) <= AH.GRAPH_MAX_BACKOFF, SLEPT
ok(f"backoff increases and never exceeds {AH.GRAPH_MAX_BACKOFF}s: {SLEPT}")
assert len(SLEPT) == AH.GRAPH_MAX_ATTEMPTS - 1
ok(f"gave up after {AH.GRAPH_MAX_ATTEMPTS} attempts")
assert "narrower date range" in message, message
ok("the error tells the user what to actually do:")
print(f"      {message[:120]}")

print("\n-- an error retrying cannot fix fails immediately --")
for status in (401, 403, 404):
    calls = stub([FakeResponse(status, text="nope")])
    try:
        AH.fetch_mailbox_messages("receivedDateTime", "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z")
        print(f"  FAIL {status} did not raise")
        raise SystemExit(1)
    except RuntimeError:
        pass
    assert len(calls) == 1, (status, calls)
    ok(f"{status} raises on the first attempt — no pointless retrying")

print("\n-- folder resolution is on the same footing --")
calls = stub([FakeResponse(502), FakeResponse(200, {"id": "id-inbox"})])
assert AH.fetch_well_known_folder_id("inbox") == "id-inbox"
assert len(calls) == 2
ok("a transient 502 resolving a folder retries instead of killing the fetch")

calls = stub([FakeResponse(404, text="no such folder")])
assert AH.fetch_well_known_folder_id("clutter") is None
assert len(calls) == 1
ok("404 still means 'this mailbox has no such folder', answered immediately")

print("\n-- wide ranges are split into bounded windows --")
windows = list(AH._split_window("2026-01-01T00:00:00Z", "2026-12-31T00:00:00Z"))
assert len(windows) > 1
ok(f"a year becomes {len(windows)} windows of <= {AH.VOLUME_WINDOW_DAYS} days")
# The windows must tile the range exactly: no gap, no overlap.
assert windows[0][0] == "2026-01-01T00:00:00Z"
assert windows[-1][1] == "2026-12-31T00:00:00Z"
for earlier, later in zip(windows, windows[1:]):
    assert earlier[1] == later[0], (earlier, later)
ok("windows tile the range exactly — no gap, no overlap, no double-count")

single = list(AH._split_window("2026-01-01T00:00:00Z", "2026-01-05T00:00:00Z"))
assert len(single) == 1, single
ok("a short range stays a single query")

degenerate = list(AH._split_window("2026-01-05T00:00:00Z", "2026-01-05T00:00:00Z"))
assert len(degenerate) == 1
ok("an empty range does not loop forever")

print("\n-- paging and progress across windows --")
calls = stub([
    FakeResponse(200, {"value": [{"id": "1"}], "@odata.nextLink": "https://next/page2"}),
    FakeResponse(200, {"value": [{"id": "2"}]}),
    FakeResponse(200, {"value": [{"id": "3"}]}),
])
seen = []
got = AH.fetch_mailbox_messages(
    "receivedDateTime", "2026-01-01T00:00:00Z", "2026-03-01T00:00:00Z",
    page_cb=seen.append,
)
assert [m["id"] for m in got] == ["1", "2", "3"], got
ok("follows @odata.nextLink within a window and continues to the next window")
assert seen == [1, 2, 3], seen
ok(f"progress reports a running total across the whole pull: {seen}")

shutil.rmtree(scratch, ignore_errors=True)
print("\nGRAPH RESILIENCE TESTS PASSED")
