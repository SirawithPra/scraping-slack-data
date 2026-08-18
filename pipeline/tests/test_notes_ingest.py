"""Typed notes become records, because that is how this team records a meeting.

The transcript path assumes a recording existed. On the real project it usually did not —
the PO writes the notes by hand and posts them into Slack — so the shape that needs
ingesting is prose with bullets in it.

The decision worth pinning is that **one paste is one record**. Splitting a note into a
record per line would model something that never happened: a note posted into Slack is one
message. Everything that needs the lines reads them inside the record, which is why
`ingest/blockers` finds 25 blocker lines across 17 multi-line posts.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tam.ingest.blockers import lines_of
from tam.ingest.notes import MIN_NOTE_CHARS, to_record

WHEN = datetime(2026, 8, 19, 9, 30, tzinfo=timezone.utc)
NOTE = "*Sprint planning 19 Aug*\n• Pending Mild - list all field\n• Tat จะขึ้น my vehicle พรุ่งนี้"


def test_one_paste_is_one_record() -> None:
    record = to_record(NOTE, when=WHEN)
    assert record["source"] == "note"
    assert record["text"].count("\n") == 2, "the lines stay together in one record"


def test_the_same_note_on_the_same_day_keeps_its_id_so_a_repaste_replaces() -> None:
    first = to_record(NOTE, when=WHEN)
    again = to_record(NOTE + "\n", when=WHEN)  # trailing whitespace is not a new note
    assert first["id"] == again["id"]


def test_a_different_day_is_a_different_note() -> None:
    later = to_record(NOTE, when=datetime(2026, 8, 20, 9, 30, tzinfo=timezone.utc))
    assert to_record(NOTE, when=WHEN)["id"] != later["id"]


def test_fixing_the_title_does_not_orphan_the_note() -> None:
    # The title is outside the hash on purpose: a typo fix should update the record, not
    # leave two near-identical ones competing in every ranking.
    assert to_record(NOTE, title="Sprint planning", when=WHEN)["id"] == to_record(NOTE, title="Sprint plannning", when=WHEN)["id"]


def test_a_title_is_prepended_so_retrieval_can_see_it() -> None:
    record = to_record("Pending Mild - list all field และอีกอย่าง", title="Sprint planning", when=WHEN)
    assert record["text"].startswith("*Sprint planning*")


def test_a_note_that_already_has_a_heading_does_not_get_a_second_one() -> None:
    record = to_record(NOTE, title="Sprint planning", when=WHEN)
    assert record["text"].startswith("*Sprint planning 19 Aug*"), "the author's own heading is kept"
    assert record["text"].count("*Sprint planning") == 1


def test_too_short_to_cluster_is_refused_rather_than_stored() -> None:
    with pytest.raises(ValueError):
        to_record("สั้น", when=WHEN)
    assert len("x" * MIN_NOTE_CHARS) == MIN_NOTE_CHARS


def test_the_author_is_recorded_and_never_invented() -> None:
    assert to_record(NOTE, author="U08H0UD5R36", when=WHEN)["user"] == "U08H0UD5R36"
    assert to_record(NOTE, when=WHEN)["user"] == "", "nobody is guessed at"


def test_the_blocker_reader_finds_the_pending_line_inside_the_note() -> None:
    # The end this exists for: a pasted note produces a blocker whose evidence is the line
    # somebody wrote, not the whole note.
    found = lines_of(to_record(NOTE, when=WHEN)["text"])
    assert [one.line for one in found] == ["Pending Mild - list all field"]
