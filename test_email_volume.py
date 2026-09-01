"""email_volume: storage, timezone handling, bucketing and zero-fill.

Runs against a scratch profile so the live workbench is never touched.
"""
import os
import shutil
import tempfile

scratch = tempfile.mkdtemp(prefix="mwb_vol_")
os.environ["MANAGER_WORKBENCH_HOME"] = scratch

import db  # noqa: E402
import email_volume as EV  # noqa: E402

db.init_schema()
ok = lambda m: print("  PASS", m)


def graph_msg(mid, ts, direction, subject="s", who="Someone", addr="a@b.com"):
    """A message shaped the way Graph returns it."""
    field = EV.TIMESTAMP_FIELD[direction]
    msg = {"id": mid, field: ts, "subject": subject, "conversationId": f"c-{mid}"}
    if direction == EV.RECEIVED:
        msg["from"] = {"emailAddress": {"name": who, "address": addr}}
    else:
        msg["toRecipients"] = [{"emailAddress": {"name": who, "address": addr}}]
    return msg


print("\n-- storage and idempotency --")
assert EV.upsert_messages([graph_msg("r1", "2026-08-03T09:15:00Z", EV.RECEIVED)], EV.RECEIVED) == 1
assert EV.upsert_messages([graph_msg("r1", "2026-08-03T09:15:00Z", EV.RECEIVED)], EV.RECEIVED) == 1
assert EV.coverage()[EV.RECEIVED]["count"] == 1
ok("re-fetching an overlapping window does not double-count")

assert EV.upsert_messages([{"id": "x", "subject": "no timestamp"}], EV.RECEIVED) == 0
assert EV.upsert_messages([{"receivedDateTime": "2026-08-03T09:00:00Z"}], EV.RECEIVED) == 0
ok("messages missing an id or a timestamp are skipped, not stored as nulls")

print("\n-- counterparty differs by direction --")
EV.upsert_messages(
    [graph_msg("s1", "2026-08-03T10:00:00Z", EV.SENT, who="Recipient", addr="to@x.com")],
    EV.SENT,
)
rows = EV.load_rows()
sent = rows[rows["direction"] == EV.SENT].iloc[0]
recv = rows[rows["direction"] == EV.RECEIVED].iloc[0]
assert sent["counterparty_name"] == "Recipient", sent.to_dict()
assert recv["counterparty_name"] == "Someone", recv.to_dict()
ok("received stores the sender; sent stores the recipient")

print("\n-- the timezone conversion actually matters --")
EV.clear()
# 23:30 UTC on the 3rd is 19:30 on the 3rd in New York, but 08:30 on the FOURTH
# in Tokyo. Bucketing before converting would file it under the wrong day.
EV.upsert_messages([graph_msg("tz1", "2026-08-03T23:30:00Z", EV.RECEIVED)], EV.RECEIVED)
utc = EV.series(grain="Day", tz="UTC")
ny = EV.series(grain="Day", tz="America/New_York")
tokyo = EV.series(grain="Day", tz="Asia/Tokyo")
assert utc["bucket_label"].iloc[0] == "2026-08-03", utc["bucket_label"].tolist()
assert ny["bucket_label"].iloc[0] == "2026-08-03", ny["bucket_label"].tolist()
assert tokyo["bucket_label"].iloc[0] == "2026-08-04", tokyo["bucket_label"].tolist()
ok("23:30Z lands on the 3rd in UTC/NY and the 4th in Tokyo")

hour_ny = EV.series(grain="Hour", tz="America/New_York")
assert hour_ny["bucket_label"].iloc[0] == "2026-08-03 19:00", hour_ny["bucket_label"].tolist()
ok("hour grain reports 19:00 local, not 23:00 UTC")

print("\n-- every grain buckets correctly --")
EV.clear()
for i, ts in enumerate(
    [
        "2026-01-15T08:00:00Z",  # Jan, w/c 2026-01-12
        "2026-01-15T08:30:00Z",  # same hour
        "2026-01-16T08:00:00Z",  # next day, same week
        "2026-02-20T08:00:00Z",  # next month
        "2027-03-01T08:00:00Z",  # next year
    ]
):
    EV.upsert_messages([graph_msg(f"g{i}", ts, EV.RECEIVED)], EV.RECEIVED)

# Hour grain over a 14-month spread is ~10,000 buckets: it must refuse.
try:
    EV.series(grain="Hour", tz="UTC")
    print("  FAIL 10,000 hour buckets were rendered")
    raise SystemExit(1)
except EV.TooManyBuckets as exc:
    ok(f"Hour over 14 months refuses: {str(exc)[:56]}...")

# Hour grain on a single day is fine.
one_day = EV.series(start="2026-01-15", end="2026-01-16", grain="Hour", tz="UTC")
got = one_day[(one_day["bucket_label"] == "2026-01-15 08:00")
              & (one_day["direction"] == EV.RECEIVED)]
assert int(got["count"].iloc[0]) == 2, one_day["count"].tolist()
ok("Hour   bucket '2026-01-15 08:00' counts 2 (within a one-day window)")

expected = {
    "Day": ("2026-01-15", 2),
    "Week": ("w/c 2026-01-12", 3),
    "Month": ("2026-01", 3),
    "Year": ("2026", 4),
}
for grain, (label, count) in expected.items():
    s = EV.series(grain=grain, tz="UTC")
    got = s[(s["bucket_label"] == label) & (s["direction"] == EV.RECEIVED)]
    assert len(got) == 1, (grain, label, s["bucket_label"].tolist())
    assert int(got["count"].iloc[0]) == count, (grain, label, got["count"].tolist())
    ok(f"{grain:<6} bucket {label!r} counts {count}")

print("\n-- zero-fill: a quiet bucket is a zero, not a missing column --")
EV.clear()
EV.upsert_messages([graph_msg("z1", "2026-05-01T08:00:00Z", EV.RECEIVED)], EV.RECEIVED)
EV.upsert_messages([graph_msg("z2", "2026-05-04T08:00:00Z", EV.RECEIVED)], EV.RECEIVED)
s = EV.series(grain="Day", tz="UTC")
labels = sorted(s["bucket_label"].unique())
assert labels == ["2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04"], labels
ok(f"the 2nd and 3rd appear as zeroes: {labels}")
quiet = s[(s["bucket_label"] == "2026-05-02") & (s["direction"] == EV.RECEIVED)]
assert int(quiet["count"].iloc[0]) == 0
ok("...with an actual 0 count")

assert set(s["direction"].unique()) == set(EV.DIRECTIONS)
assert len(s) == 4 * 2
ok("both directions present in every bucket, so clustered pairs never go ragged")

print("\n-- window filtering, end-exclusive --")
sub = EV.series(start="2026-05-01", end="2026-05-04", grain="Day", tz="UTC")
labels = sorted(sub["bucket_label"].unique())
assert labels == ["2026-05-01", "2026-05-02", "2026-05-03"], labels
ok("end is exclusive, so 05-04 is outside a 05-01..05-04 window")
assert int(sub[sub["bucket_label"] == "2026-05-03"]["count"].sum()) == 0
ok("the requested window is filled even where it has no mail at all")

# A naive bound is the viewer's wall clock, not UTC. 2026-05-01T02:00Z is
# 22:00 on Apr 30 in New York, so a NY window starting May 1 must exclude it.
EV.upsert_messages([graph_msg("edge", "2026-05-01T02:00:00Z", EV.RECEIVED)], EV.RECEIVED)
assert EV.totals(start="2026-05-01", tz="UTC")[EV.RECEIVED] == 3
assert EV.totals(start="2026-05-01", tz="America/New_York")[EV.RECEIVED] == 2
ok("a naive start is read in the viewer's timezone, not as UTC")

print("\n-- totals and empty states --")
assert EV.totals() == {EV.RECEIVED: 3, EV.SENT: 0}  # z1, z2 and the edge row
ok(f"totals: {EV.totals()}")
assert EV.series(start="2030-01-01", end="2030-02-01", grain="Day").empty
ok("an empty window returns an empty frame, not an exception")
EV.clear()
assert EV.series(grain="Day").empty and EV.totals() == {EV.RECEIVED: 0, EV.SENT: 0}
ok("cleared store is empty and still safe to query")

print("\n-- the planned-grouping seam refuses rather than silently ignoring --")
try:
    EV.series(grain="Day", group_by="subject")
    print("  FAIL group_by was silently ignored")
    raise SystemExit(1)
except NotImplementedError as exc:
    ok(f"series(group_by='subject') raises: {str(exc)[:52]}...")
try:
    EV.series(grain="Fortnight")
    print("  FAIL bad grain accepted")
    raise SystemExit(1)
except ValueError:
    ok("an unknown grain raises ValueError")

shutil.rmtree(scratch, ignore_errors=True)
print("\nEMAIL VOLUME TESTS PASSED")
