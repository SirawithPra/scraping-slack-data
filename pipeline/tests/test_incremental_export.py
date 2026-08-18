"""A daily export must not re-read yesterday's messages.

Slack gives an app created after 2025-05-29 and not on the Marketplace roughly one
`conversations.history` call per minute. Re-fetching two hundred messages from each of
five channels every morning to find the handful that are new spends the whole budget
on messages already on disk — measured, the first full pass takes minutes and the
same pass with a resume point takes 3.4 seconds and returns nothing.

The resume point is read back out of the export rather than kept in a state file
beside it, so it cannot drift away from the data it describes: delete the export and
the next run starts over, which is what anyone would expect it to do.
"""

from __future__ import annotations

import json
from pathlib import Path

from tam.ingest.export_slack import merge_exports, newest_ts


def write(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def test_the_resume_point_is_the_newest_message_including_replies(tmp_path: Path) -> None:
    # A thread reply can be newer than every parent, and resuming from a parent would
    # re-fetch the whole thread every morning.
    path = tmp_path / "export.json"
    write(path, [
        {"ts": "1000.5", "text": "a"},
        {"ts": "2000.5", "text": "b", "replies": [{"ts": "3000.5", "text": "reply"}]},
    ])
    assert newest_ts(path) == "3000.5"


def test_a_missing_file_means_start_from_the_beginning(tmp_path: Path) -> None:
    assert newest_ts(tmp_path / "nope.json") == ""


def test_a_corrupt_file_starts_over_rather_than_crashing(tmp_path: Path) -> None:
    # Better a slow full export than a daily job that dies on a truncated write.
    path = tmp_path / "export.json"
    path.write_text('[{"ts": "100', encoding="utf-8")
    assert newest_ts(path) == ""


def test_timestamps_compare_as_numbers_not_as_strings(tmp_path: Path) -> None:
    # Slack timestamps are decimal strings, so "9.0" sorts above "10.0" lexically and
    # the resume point would go backwards as a channel crossed a digit boundary.
    path = tmp_path / "export.json"
    write(path, [{"ts": "9999999999.1"}, {"ts": "10000000000.1"}])
    assert newest_ts(path) == "10000000000.1"


def test_merge_keeps_the_fresh_copy_of_a_thread_that_grew() -> None:
    # Slack's `oldest` is inclusive, so the boundary message arrives again — and a
    # thread that gained a reply arrives with a longer reply list. Taking the old copy
    # would truncate the thread back on every run.
    old = [{"ts": "100.0", "text": "parent", "replies": [{"ts": "101.0"}]}]
    fresh = [{"ts": "100.0", "text": "parent", "replies": [{"ts": "101.0"}, {"ts": "102.0"}]}]
    merged = merge_exports(old, fresh)
    assert len(merged) == 1, "the same message must not appear twice"
    assert len(merged[0]["replies"]) == 2


def test_merge_adds_new_messages_and_keeps_order() -> None:
    merged = merge_exports([{"ts": "100.0"}], [{"ts": "300.0"}, {"ts": "200.0"}])
    assert [row["ts"] for row in merged] == ["100.0", "200.0", "300.0"]


def test_merge_never_drops_a_message_that_has_no_ts() -> None:
    # Defensive: a record without `ts` cannot be keyed, and silently discarding it
    # would lose data on a malformed export rather than carrying it through.
    merged = merge_exports([{"text": "no ts"}], [{"ts": "100.0"}])
    assert len(merged) == 2
