"""The Volume tab: renders, and the figure obeys the chart specs.

Runs against a scratch profile so the live workbench is never touched.
"""
import os
import shutil
import tempfile

scratch = tempfile.mkdtemp(prefix="mwb_voltab_")
os.environ["MANAGER_WORKBENCH_HOME"] = scratch

import datetime as dt  # noqa: E402

import db  # noqa: E402
import email_volume as EV  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

db.init_schema()
ok = lambda m: print("  PASS", m)

TODAY = dt.date.today()


def graph_msg(mid, ts, direction):
    field = EV.TIMESTAMP_FIELD[direction]
    msg = {"id": mid, field: ts, "subject": f"subject {mid}", "conversationId": "c"}
    if direction == EV.RECEIVED:
        msg["from"] = {"emailAddress": {"name": "A Sender", "address": "s@x.com"}}
    else:
        msg["toRecipients"] = [{"emailAddress": {"name": "A Recipient", "address": "r@x.com"}}]
    return msg


def run(label):
    at = AppTest.from_file("pages/0_email_actions.py", default_timeout=90)
    at.session_state["gemini_client"] = object()
    at.run()
    if at.exception:
        print(f"  FAIL {label}: {at.exception[0].value}")
        raise SystemExit(1)
    print(f"  PASS {label} rendered clean")
    return at


print("\n-- empty state --")
at = run("volume tab with no data")
assert any("No volume data yet" in str(i.value) for i in at.info), [i.value for i in at.info]
ok("prompts for a fetch instead of drawing an empty chart")
assert not at.get("plotly_chart"), "a chart was drawn with no data"
ok("no chart rendered when there is nothing to plot")

print("\n-- with data --")
# Two weeks inside the default 30-day window, more received than sent.
for day in range(14):
    stamp = TODAY - dt.timedelta(days=day)
    for i in range(3):
        EV.upsert_messages(
            [graph_msg(f"r-{day}-{i}", f"{stamp.isoformat()}T09:0{i}:00Z", EV.RECEIVED)],
            EV.RECEIVED,
        )
    EV.upsert_messages(
        [graph_msg(f"s-{day}", f"{stamp.isoformat()}T17:00:00Z", EV.SENT)], EV.SENT
    )

at = run("volume tab with data")
charts = at.get("plotly_chart")
assert charts, "no chart rendered"
ok("chart rendered")

metrics = {m.label: m.value for m in at.metric}
assert metrics.get("Received") == "42", metrics
assert metrics.get("Sent") == "14", metrics
assert metrics.get("Received per sent") == "3.0×", metrics
ok(f"stat tiles read {metrics}")

print("\n-- the stale-scope banner --")
# The rows above were stored without marking the scope, i.e. exactly the state a
# profile is in after upgrading from Inbox-only counting.
assert not EV.scope_is_current()
assert any("only the **Inbox** counted" in str(w.value) for w in at.warning), [
    w.value for w in at.warning
]
ok("warns that stored Inbox-only data undercounts received mail")
assert [b for b in at.button if b.key == "vol_rescope"]
ok("offers a clear-and-start-again button")

EV.mark_scope()
at = run("volume tab after re-scoping")
assert not any("only the **Inbox** counted" in str(w.value) for w in at.warning)
ok("banner gone once the data was gathered under the current definition")
assert not [b for b in at.button if b.key == "vol_rescope"]
ok("...and so is the button")

print("\n-- the figure itself --")
import plotly.graph_objects as go  # noqa: E402

# Pull the figure builder and its constants out of the page and run them, so the
# specs below are asserted against the real code rather than a copy of it. One
# dict serves as both globals and locals — with separate ones the function body
# cannot see the module-level constants defined beside it.
source = open("pages/0_email_actions.py", encoding="utf-8").read()
section = source[
    source.index("SERIES_COLORS = {") : source.index("tab_identify, tab_tracker")
]
namespace = {"go": go, "EV": EV, "st": None}
exec(compile(section, "page-figure", "exec"), namespace, namespace)
build = namespace["_volume_figure"]

frame = EV.series(
    start=(TODAY - dt.timedelta(days=29)).isoformat(),
    end=(TODAY + dt.timedelta(days=1)).isoformat(),
    grain="Day",
    tz="UTC",
)
fig = build(frame, "light", "Day")

assert fig.layout.barmode == "group", fig.layout.barmode
ok("barmode is 'group' — clustered, not stacked")
assert len(fig.data) == 2, len(fig.data)
assert [t.name for t in fig.data] == ["Received", "Sent"], [t.name for t in fig.data]
ok("two named series, so identity is not colour-alone")
assert fig.data[0].marker.color == "#2a78d6", fig.data[0].marker.color
assert fig.data[1].marker.color == "#eb6834", fig.data[1].marker.color
ok("validated categorical slots 1 and 2 in light mode")
dark = build(frame, "dark", "Day")
assert dark.data[0].marker.color == "#3987e5", dark.data[0].marker.color
ok("dark mode uses its own steps, not a flip of the light ones")

assert fig.data[0].marker.cornerradius == 4
ok("4px rounded data-end on the columns")
assert fig.layout.bargroupgap == 0.08
ok("a gap separates the clustered pair (no stroke around marks)")
assert fig.layout.yaxis.showgrid and fig.layout.xaxis.showgrid is False
assert fig.layout.yaxis.griddash == "solid"
ok("solid hairline y-gridlines only, never dashed")
assert fig.layout.yaxis.zeroline is False and fig.layout.yaxis.rangemode == "tozero"
ok("bars grow from a true zero baseline")
assert fig.layout.showlegend is not False
ok("legend present for two series")
assert all("%{y:,}" in t.hovertemplate for t in fig.data)
ok("per-mark hover tooltip on every series")
assert fig.layout.plot_bgcolor == "rgba(0,0,0,0)"
ok("transparent surface, so it sits on the app's own background")
assert not any(getattr(t, "text", None) for t in fig.data)
ok("no number printed on every column")

print("\n-- bar thickness adapts so few buckets do not become fat blocks --")
few = build(frame[frame["bucket_label"].isin(sorted(frame["bucket_label"].unique())[:2])], "light", "Day")
many = build(frame, "light", "Day")
assert few.layout.bargap > many.layout.bargap, (few.layout.bargap, many.layout.bargap)
ok(f"2 buckets -> bargap {few.layout.bargap:.2f}; 30 buckets -> {many.layout.bargap:.2f}")

print("\n-- table view is present --")
assert any("Table view" in str(e) for e in [x.label for x in at.get("expander")])
ok("a table-view twin exists alongside the chart")

print("\n-- every grain renders without error --")
for grain in EV.GRAINS:
    f = EV.series(
        start=(TODAY - dt.timedelta(days=29)).isoformat(),
        end=(TODAY + dt.timedelta(days=1)).isoformat(),
        grain=grain,
        tz="UTC",
    )
    fig = build(f, "light", grain)
    assert len(fig.data) == 2
    print(f"  PASS {grain:<6} -> {f['bucket_label'].nunique()} bucket(s)")

shutil.rmtree(scratch, ignore_errors=True)
print("\nVOLUME TAB TESTS PASSED")
