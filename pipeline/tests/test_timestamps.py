"""Epoch → display string → epoch, with the zone written down.

Slack stores an instant. `datetime.fromtimestamp(ts)` reads it as if the epoch had
no zone at all, so the same bytes rendered on a laptop in Asia/Bangkok and in a
container defaulting to UTC differ by the whole offset — enough to move a message
to the previous day on screen and to flip a `stalled` badge on the bot side
(measured across the seam: 4.05 days vs 3.46 for one corpus).

So the conversion goes through UTC explicitly and `tz` is a parameter. These tests
pin the offset arithmetic to a real record from the demo ledger, whose rendered
`when` was produced on a machine in Asia/Bangkok.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from tam.core import format_timestamp
from tam.retrieval.signals import timestamp

BANGKOK = ZoneInfo("Asia/Bangkok")
LOS_ANGELES = ZoneInfo("America/Los_Angeles")

# msg_C0DEMOCHAN1_1786500840.014 — the blocker the demo ledger reports as
# "ติดตั้งแต่ 2026-08-12 09:14", i.e. rendered in Asia/Bangkok.
BLOCKER_TS = "1786500840.014"


def test_the_zone_is_the_parameter_not_the_machine() -> None:
    assert format_timestamp(BLOCKER_TS, tz=BANGKOK) == "2026-08-12 09:14"
    assert format_timestamp(BLOCKER_TS, tz=timezone.utc) == "2026-08-12 02:14"
    # Far enough west that the naive reading lands on the previous day, which is
    # how a message discussed this morning came to be displayed as yesterday's.
    assert format_timestamp(BLOCKER_TS, tz=LOS_ANGELES) == "2026-08-11 19:14"


def test_offset_between_two_zones_is_exactly_the_offset() -> None:
    """The failure mode was an offset appearing out of nowhere; measure it."""
    def parsed(tz) -> datetime:
        return datetime.strptime(format_timestamp(BLOCKER_TS, tz=tz), "%Y-%m-%d %H:%M").replace(tzinfo=tz)

    delta = parsed(BANGKOK) - parsed(timezone.utc)
    assert delta.total_seconds() == 0, "the same instant, named in two zones, is one instant"


def test_round_trip_returns_the_same_instant() -> None:
    """Display drops seconds, so the round trip is exact to the minute and no more."""
    for tz in (timezone.utc, BANGKOK, LOS_ANGELES):
        rendered = format_timestamp(BLOCKER_TS, tz=tz)
        back = datetime.strptime(rendered, "%Y-%m-%d %H:%M").replace(tzinfo=tz).timestamp()
        assert 0 <= float(BLOCKER_TS) - back < 60


def test_unusable_timestamps_say_unknown_rather_than_guessing() -> None:
    assert format_timestamp("") == "unknown"
    assert format_timestamp("not-a-timestamp") == "unknown"
    # digest.py renders first/last through str(float) — an empty topic gives nan.
    assert format_timestamp(str(float("nan"))) == "unknown"


def test_signals_timestamp_is_nan_for_a_missing_or_broken_ts() -> None:
    """The other half of the seam: every finite-check downstream depends on this."""
    assert timestamp({"ts": BLOCKER_TS}) == float(BLOCKER_TS)
    assert timestamp({"ts": "1786500840"}) == 1786500840.0
    for broken in ({}, {"ts": ""}, {"ts": None}, {"ts": "2026-08-12 09:14"}):
        assert str(timestamp(broken)) == "nan", broken
