"""
email_volume.py — mail volume over time: how much arrives, how much goes out.

Answers one question ("am I getting buried, and when?") and deliberately stops
there. First pass is a plain count of received vs sent per time bucket, with no
grouping.

Grouping IS planned, so the schema is shaped for it now rather than later: one
row per message, carrying `subject` and the counterparty, so adding "group by
subject" or "group by sender" becomes a GROUP BY over data already stored — no
re-fetch from Graph. `series()` takes a `group_by` argument that currently
accepts only None, which is where that lands.

Its table lives in the active profile's workbench.db alongside every other store
(see db.py), so it travels with the user rather than with the checkout. Schema
creation is driven by db.init_schema() at app start.

Two things worth knowing about the numbers:

  - "Received" is the Inbox and "sent" is Sent Items. A rule that files mail
    into a subfolder before you see it is therefore not counted as received.
    That matches what a human means by "my inbox", and is stated in the UI.
  - Graph returns UTC. Timestamps are stored UTC verbatim and converted to a
    display timezone at query time, because "email by hour" is meaningless in
    UTC if you don't live there.
"""
import pandas as pd

import db

RECEIVED, SENT = "received", "sent"
DIRECTIONS = (RECEIVED, SENT)

# Inbox / Sent Items well-known folder ids, and which Graph timestamp field
# carries the moment that matters for each.
FOLDERS = {
    RECEIVED: ("inbox", "receivedDateTime"),
    SENT: ("sentitems", "sentDateTime"),
}

# Time grains offered in the UI. `freq` is the pandas resample alias; `label` is
# how a bucket is printed on the axis. Order is the order shown.
#   ME  = month end, YE = year end, W-MON = weeks starting Monday
GRAINS = {
    "Hour": {"freq": "h", "label": "%Y-%m-%d %H:00", "tick": "%d %b %H:00"},
    "Day": {"freq": "D", "label": "%Y-%m-%d", "tick": "%d %b"},
    "Week": {"freq": "W-MON", "label": "w/c %Y-%m-%d", "tick": "%d %b"},
    "Month": {"freq": "MS", "label": "%Y-%m", "tick": "%b %Y"},
    "Year": {"freq": "YS", "label": "%Y", "tick": "%Y"},
}
DEFAULT_GRAIN = "Day"

# Hard refusal, and a softer "this is getting crowded" hint.
#
# The cap is set to clear the legitimate asks rather than to enforce taste: two
# years by day is ~730 and a month by hour is ~744, both of which someone may
# genuinely want. What it stops is the pathological case — an hour grain across
# a year is ~8,760 buckets, i.e. 17,500 bars, which is a grey smear and slow to
# render. Between BUSY_BUCKETS and the cap the UI suggests a coarser grain but
# still draws what was asked for.
MAX_BUCKETS = 800
BUSY_BUCKETS = 150


class TooManyBuckets(Exception):
    """The requested range and grain would produce an unreadable chart. Carries
    a message written for the user, naming the fix."""


def init_db():
    """Create the volume table if it doesn't already exist."""
    conn = db.connect()
    cursor = conn.cursor()
    # One row per message. subject and counterparty are stored purely so the
    # planned grouping dimensions need no re-fetch; nothing reads them yet.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS email_volume (
            message_id           TEXT PRIMARY KEY,
            direction            TEXT NOT NULL,
            ts                   TEXT NOT NULL,
            subject              TEXT,
            counterparty_name    TEXT,
            counterparty_address TEXT,
            conversation_id      TEXT,
            fetched_at           TIMESTAMP
        )
        """
    )
    # The only read shape: a direction over a time window.
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_email_volume_dir_ts "
        "ON email_volume (direction, ts)"
    )
    conn.commit()
    conn.close()


def _counterparty(message, direction):
    """Who the message is 'with': the sender for received mail, the first
    recipient for sent mail. The useful grouping dimension differs by
    direction, so it is normalized to one column at write time."""
    if direction == RECEIVED:
        holder = message.get("from") or message.get("sender") or {}
        address = holder.get("emailAddress") or {}
    else:
        recipients = message.get("toRecipients") or []
        address = (recipients[0].get("emailAddress") or {}) if recipients else {}
    return str(address.get("name") or ""), str(address.get("address") or "")


def upsert_messages(messages, direction):
    """Store (or refresh) a batch of Graph messages for one direction.

    Idempotent on message_id, so re-fetching an overlapping window never
    double-counts — which is what makes a refresh safe to run repeatedly.
    Returns the number of rows written.
    """
    _timestamp_field = FOLDERS[direction][1]
    rows = []
    for message in messages:
        message_id = message.get("id")
        timestamp = message.get(_timestamp_field)
        if not message_id or not timestamp:
            continue
        name, address = _counterparty(message, direction)
        rows.append(
            (
                message_id,
                direction,
                timestamp,
                str(message.get("subject") or ""),
                name,
                address,
                str(message.get("conversationId") or ""),
                pd.Timestamp.utcnow().isoformat(timespec="seconds"),
            )
        )
    if not rows:
        return 0

    conn = db.connect()
    conn.executemany(
        """
        INSERT INTO email_volume (
            message_id, direction, ts, subject, counterparty_name,
            counterparty_address, conversation_id, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            direction            = excluded.direction,
            ts                   = excluded.ts,
            subject              = excluded.subject,
            counterparty_name    = excluded.counterparty_name,
            counterparty_address = excluded.counterparty_address,
            conversation_id      = excluded.conversation_id,
            fetched_at           = excluded.fetched_at
        """,
        rows,
    )
    conn.commit()
    conn.close()
    return len(rows)


def coverage():
    """{direction: {"count": n, "first": ts, "last": ts}} for what is stored.

    Derived from the rows themselves rather than from a recorded fetch window,
    so it cannot drift from the data. It does mean a gap in the middle of a
    range is not detectable — acceptable while the only fetch control offers a
    single contiguous window.
    """
    conn = db.connect()
    rows = conn.execute(
        "SELECT direction, COUNT(*), MIN(ts), MAX(ts) FROM email_volume "
        "GROUP BY direction"
    ).fetchall()
    conn.close()
    out = {d: {"count": 0, "first": None, "last": None} for d in DIRECTIONS}
    for direction, count, first, last in rows:
        out[direction] = {"count": int(count), "first": first, "last": last}
    return out


def load_rows(start=None, end=None, directions=DIRECTIONS, tz="UTC"):
    """Raw rows for a window as a DataFrame: ts (UTC), direction, subject,
    counterparty. `end` is EXCLUSIVE.

    `start`/`end` without an offset are read as wall-clock times in `tz`, since
    that is what a date picker hands over — a viewer in New York asking for
    "Sept 1" means their midnight, not UTC's.
    """
    where = [f"direction IN ({', '.join('?' for _ in directions)})"]
    params = list(directions)
    if start:
        where.append("ts >= ?")
        params.append(_as_utc_iso(start, tz))
    if end:
        where.append("ts < ?")
        params.append(_as_utc_iso(end, tz))

    conn = db.connect()
    rows = conn.execute(
        f"SELECT ts, direction, subject, counterparty_name, counterparty_address "
        f"FROM email_volume WHERE {' AND '.join(where)} ORDER BY ts",
        params,
    ).fetchall()
    conn.close()
    return pd.DataFrame(
        rows,
        columns=["ts", "direction", "subject", "counterparty_name", "counterparty_address"],
    )


def _as_utc_iso(value, tz="UTC"):
    """Normalize a date/datetime/string to the UTC ISO form stored in `ts`,
    reading a naive input as wall-clock time in `tz`."""
    timestamp = pd.Timestamp(value)
    if timestamp.tz is None:
        timestamp = timestamp.tz_localize(tz)
    return timestamp.tz_convert("UTC").isoformat().replace("+00:00", "Z")


def series(start=None, end=None, grain=DEFAULT_GRAIN, tz="UTC", group_by=None):
    """Counts per time bucket per direction — the chart's input.

    Returns a tidy frame with one row per (bucket, direction):
    `bucket` (tz-aware Timestamp), `bucket_label` (formatted for the axis),
    `direction`, `count`.

    Every bucket in the range is present for BOTH directions, zero-filled. A
    missing bucket would otherwise silently close the gap on a clustered column
    chart and misstate a quiet week as no week at all.

    `group_by` is the seam for the planned per-subject / per-sender breakdown.
    Only None is implemented; anything else raises rather than being silently
    ignored.
    """
    if group_by is not None:
        raise NotImplementedError(
            "series(group_by=...) is planned but not built. The columns it needs "
            "(subject, counterparty_name) are already stored, so it is a GROUP BY "
            "here rather than a re-fetch from Graph."
        )
    if grain not in GRAINS:
        raise ValueError(f"Unknown grain {grain!r}. Expected one of {list(GRAINS)}.")

    freq = GRAINS[grain]["freq"]
    label_format = GRAINS[grain]["label"]
    frame = load_rows(start, end, tz=tz)

    if frame.empty:
        return pd.DataFrame(
            columns=["bucket", "bucket_label", "tick_label", "direction", "count"]
        )

    # UTC in the database -> the viewer's timezone -> naive local wall time,
    # THEN bucket. Converting first is what puts a 9am local spike in the 9am
    # column; at coarse grains it decides which DAY a late-evening mail lands
    # on. Dropping the offset afterwards is deliberate: buckets are wall-clock
    # labels, and keeping them tz-aware only invites DST arithmetic into a
    # calendar-bucketing problem that does not need it.
    local = (
        pd.to_datetime(frame["ts"], utc=True, format="ISO8601")
        .dt.tz_convert(tz)
        .dt.tz_localize(None)
    )
    frame = frame.assign(bucket=_bucket(local, grain))

    counted = (
        frame.groupby(["bucket", "direction"], dropna=False)
        .size()
        .reset_index(name="count")
    )

    # Zero-fill the full grid x both directions. A bucket with no mail must be a
    # visible zero: on a clustered column chart a MISSING bucket silently closes
    # the gap, so a quiet week would read as if it never happened.
    #
    # The grid spans the REQUESTED window, not just the range that happens to
    # contain mail. Asking for May 1-4 and getting one column because only the
    # 1st had traffic hides the answer — the three quiet days are the finding.
    grid_lo = (
        _bucket(pd.Series([pd.Timestamp(start)]), grain).iloc[0]
        if start
        else counted["bucket"].min()
    )
    grid_hi = (
        # end is exclusive, so step back inside it before flooring, or an
        # end-of-Monday boundary would add an extra empty bucket.
        _bucket(pd.Series([pd.Timestamp(end) - pd.Timedelta(1, "ns")]), grain).iloc[0]
        if end
        else counted["bucket"].max()
    )
    grid_lo = min(grid_lo, counted["bucket"].min())
    grid_hi = max(grid_hi, counted["bucket"].max())
    grid = pd.date_range(grid_lo, grid_hi, freq=freq)
    if len(grid) > MAX_BUCKETS:
        # Rendering thousands of columns is unreadable as well as slow, so refuse
        # rather than emit it. The caller reports this as guidance, not an error.
        raise TooManyBuckets(
            f"{len(grid):,} {grain.lower()} buckets between "
            f"{counted['bucket'].min():%Y-%m-%d} and "
            f"{counted['bucket'].max():%Y-%m-%d} — more than {MAX_BUCKETS:,} "
            f"columns would be unreadable. Narrow the date range, or use a "
            f"coarser grouping."
        )
    full = pd.MultiIndex.from_product(
        [grid, list(DIRECTIONS)], names=["bucket", "direction"]
    )
    counted = (
        counted.set_index(["bucket", "direction"])
        .reindex(full, fill_value=0)
        .reset_index()
    )
    counted["bucket_label"] = counted["bucket"].dt.strftime(label_format)
    counted["tick_label"] = counted["bucket"].dt.strftime(GRAINS[grain]["tick"])
    return counted[["bucket", "bucket_label", "tick_label", "direction", "count"]]


def _bucket(local, grain):
    """Floor naive local timestamps to the start of their bucket.

    Written out per grain rather than leaning on to_period for all of them,
    because pandas' weekly periods are anchored on their END: to_period('W-MON')
    is the week ENDING Monday, so its start_time is a Tuesday. Subtracting
    dayofweek is unambiguous and gives the Monday the week actually starts on.
    """
    if grain == "Hour":
        return local.dt.floor("h")
    if grain == "Day":
        return local.dt.floor("D")
    if grain == "Week":
        return local.dt.floor("D") - pd.to_timedelta(local.dt.dayofweek, unit="D")
    if grain == "Month":
        return local.dt.to_period("M").dt.start_time
    if grain == "Year":
        return local.dt.to_period("Y").dt.start_time
    raise ValueError(f"Unknown grain {grain!r}.")


def totals(start=None, end=None, tz="UTC"):
    """{direction: count} over a window — the stat tiles above the chart."""
    frame = load_rows(start, end, tz=tz)
    if frame.empty:
        return {d: 0 for d in DIRECTIONS}
    counts = frame["direction"].value_counts().to_dict()
    return {d: int(counts.get(d, 0)) for d in DIRECTIONS}


def clear():
    """Drop every stored row. The fetch is re-runnable, so this is recoverable."""
    conn = db.connect()
    conn.execute("DELETE FROM email_volume")
    conn.commit()
    conn.close()
