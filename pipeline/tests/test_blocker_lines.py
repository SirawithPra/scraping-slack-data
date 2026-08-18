"""Blockers are read at the line, because that is the size this team writes them.

The measurement that forced this module: messages containing `pending` have a median
length of 529 characters against the corpus median of 46, and 14 of 15 are multi-line.
Adding `pending` to the whole-message cue list raised the blocked count from 2 to 3 and
cited a post beginning "Sprint 3 is officially end *RECAP*" as proof somebody was stuck —
the cue was real and the attribution was nonsense, because one line had decided a post of
thirty. Reading lines takes it from 3 reachable messages to 25 lines across 17 posts.

Pinned here: the three shapes that appear in real posts, and the four things that are
deliberately *not* blockers.
"""

from __future__ import annotations

from tam.ingest.blockers import LINE_CUES, blocker_lines, lines_of


def test_a_leading_state_on_a_bullet_is_found_past_the_marker() -> None:
    found = lines_of("• Pending add ui - หน้า Error case user redeem")
    assert len(found) == 1
    assert found[0].cue == "pending"
    assert found[0].line.startswith("Pending add ui"), "the bullet marker is stripped"


def test_a_waiting_heading_makes_the_lines_under_it_pending() -> None:
    # Slack's own bold markup, so this is the author's structure rather than a guess.
    text = "*Pending task*\n• update progress on rops integration\n*[CMS]*\n• weekly show update"
    found = lines_of(text)
    lines = {one.line for one in found}
    assert "update progress on rops integration" in lines
    assert "weekly show update" not in lines, "a later heading ends the pending section"
    assert next(one for one in found if "rops" in one.line).from_heading


def test_a_heading_is_not_itself_a_blocker() -> None:
    assert lines_of("*Pending task*") == []


def test_a_cue_inside_a_work_line_still_counts() -> None:
    found = lines_of("• additional fix [PROJ-110] and pending deploy to retest")
    assert len(found) == 1 and found[0].cue == "pending"


def test_a_question_about_a_blocker_is_not_a_declaration() -> None:
    # Somebody chasing an update is not somebody blocked; reading it as one attributes the
    # obstacle to the person asking.
    assert lines_of("• Progress on pending task?") == []
    assert lines_of("• mobile pending resubmit due to permission") != []


def test_not_done_is_not_blocked() -> None:
    # `ยังไม่ได้` ("hasn't yet") was tried and removed: five hits, none a blocker — things
    # not started, one struck through, and one praising a release.
    assert "ยังไม่ได้" not in LINE_CUES
    assert lines_of("• fe mobile ผมยังไม่ได้ดู") == []


def test_a_fragment_is_not_a_statement() -> None:
    assert lines_of("• pending") == [], "too short to say what is waiting"


def test_the_line_names_who_is_waited_on_when_it_says_so_and_never_guesses() -> None:
    named = lines_of("• P'mos - Pending struct from <@U08QC0L7CFL>, investigate fields")
    assert named[0].waiting_on == ("U08QC0L7CFL",)
    # Most real lines name people by nickname, which is not an id and is not invented.
    nickname = lines_of("• Tat - insurance + logo insurance pending from mild")
    assert nickname[0].waiting_on == ()


def test_a_tracker_record_is_not_somebody_reporting_that_they_are_stuck() -> None:
    # An issue's own description would match these cues constantly.
    issue = {"id": "yt_PROJ-1", "text": "• pending api from the other team", "ts": 1.0,
             "youtrack_key": "PROJ-1", "source": "youtrack"}
    message = {"id": "m1", "text": "• pending api from the other team", "ts": 2.0, "source": "slack"}
    assert [one.record_id for one in blocker_lines([issue, message])] == ["m1"]


def test_every_line_carries_the_message_it_came_from() -> None:
    # The whole point: the evidence a person reads has to be the line, and the line has to
    # be traceable to a message somebody actually sent.
    message = {"id": "m9", "user": "U0PERSON", "ts": 5.0, "source": "slack",
               "text": "daily\n• Pending Mild - list all field\n• shipped the rest"}
    found = blocker_lines([message])
    assert len(found) == 1
    assert (found[0].record_id, found[0].user, found[0].ts) == ("m9", "U0PERSON", 5.0)
    assert found[0].line == "Pending Mild - list all field"
