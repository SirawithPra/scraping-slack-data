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


def test_a_cue_inside_inline_code_is_not_a_blocker() -> None:
    """`blocked` is also the name of a slash command somebody was testing.

    This reader used to take raw `text`, which is the one field that still contains what
    a person only pasted or quoted — the hole `quoted.py` exists to close. Measured on the
    real corpus it cost exactly one line, and that line put a work item at the top of the
    standup as blocked on the strength of `` `/meowtam blocked` ``. Reading the author's
    asserted lines removes inline code, so the cue is not there to match.

    The item stayed blocked either way, which is why this is pinned as a test rather than
    trusted to a count: what changed is the sentence the dashboard cites under it, and the
    date it claims the work has been stuck since.
    """
    record = {
        "id": "m1",
        "user": "U0PERSON01",
        "ts": "1787146738.183129",
        "text": "`/meowtam recall` ,`/meowtam blocked` , `/meowtam silent` ดูสิว่าของมายมีอะไรเกิดขึ้นไหม",
    }
    assert blocker_lines([record]) == []


def test_a_real_waiting_line_in_the_same_shape_still_reads() -> None:
    """The guard above must not be a way to lose genuine blockers written on one line."""
    record = {
        "id": "m2",
        "user": "U0PERSON01",
        "ts": "1787146738.183129",
        "text": "ฝั่ง my vehicle ผมขึ้นให้พี่จ๋าเทสแล้วครับ eod นี้ แต่ pending submit privillage update อยู่",
    }
    found = blocker_lines([record])
    assert [line.cue for line in found] == ["pending"]


def test_a_completion_word_ahead_of_the_waiting_word_disqualifies_the_line() -> None:
    """`Done all API pending + support` is finished work, not somebody stuck.

    This one line put a 26-message work item on the blocked list by itself, and it is a
    line in another person's daily digest reporting that the pending APIs are done.
    """
    record = {
        "id": "m1",
        "user": "U0PERSON01",
        "ts": "1786334499.406629",
        "text": "*Daily 6 Aug 2026*\nP'Mos - Done all API pending + support",
    }
    assert blocker_lines([record]) == []


def test_a_contrast_word_between_them_keeps_the_blocker() -> None:
    """The line a bare position rule would have cost, and it is a real blocker.

    `เสร็จแล้วครับ แต่ Pending …` finishes one thing and is stuck on another. Without the
    `แต่` the same two cues in the same order mean the opposite, which is exactly why the
    escape clause exists.
    """
    record = {
        "id": "m2",
        "user": "U0PERSON01",
        "ts": "1787065260.000000",
        "text": "ฝั่ง API event ผมต่อเสร็จแล้วครับ แต่ Pending p'choke prepare api อีกตัวนึงอยู่ ยังไม่ได้ของ",
    }
    found = blocker_lines([record])
    assert [line.cue for line in found] == ["pending"]


def test_a_plain_waiting_line_is_untouched_by_the_completion_rule() -> None:
    """No completion word at all means nothing to weigh — the common case stays common."""
    record = {"id": "m3", "user": "U0PERSON01", "ts": "1.0", "text": "Pending Mild - list all field"}
    assert [line.cue for line in blocker_lines([record])] == ["pending"]


def test_the_inserted_word_variant_of_the_cannot_continue_cue() -> None:
    """`ยังทำต่อไม่ได้` is `ยังทำไม่ได้` with one word inside it.

    Thai has no spaces, so cues are matched as a contiguous run of tokens — and the
    tokeniser splits this into `[ยัง, ทำ, ต่อ, ไม่, ได้]`, which the shorter cue's token
    run cannot appear in. Without its own entry the clearest blocker in the corpus reads
    as nothing at all.
    """
    record = {
        "id": "m1",
        "user": "U0PERSON01",
        "ts": "1.0",
        "text": "อันนั้นยังติดเรื่อง clearing user on dev ครับ data ไม่สะอาด รอ ops เคลียร์ให้ก่อน ยังทำต่อไม่ได้",
    }
    assert blocker_lines([record]), "a line saying work cannot continue must read as a blocker"


def test_bare_wait_and_not_yet_are_not_cues() -> None:
    """Two rejections that are load-bearing, and measured rather than assumed.

    Bare `รอ` was rejected in §7.1 because substring search found it inside `รอบ` and
    `กรอก`. Token matching fixed that, so it was re-measured — and it is still wrong, for
    different reasons that no word list can fix: `ไม่ต้องรออะไร` is a negation, `รอการ
    ยืนยันการจอง…` is product copy being specified, and `รอแลกตั๋วหนัง` is somebody
    talking about the cinema. `ยังไม่ได้` conflates "not started" with "blocked".
    """
    for text in (
        "ใช่ครับ จะแก้ให้ในสปรินต์นี้ ไม่ต้องรออะไร",
        "รอการยืนยันการจองจากเจ้าหน้าที่ผ่าน LINE Official Account ก่อนเข้าใช้บริการ",
        "รอแลกตั๋วหนังดีกว่านะ เห้นบอกว่าจะมา",
        "fe mobile ผมยังไม่ได้ดู",
    ):
        record = {"id": "m", "user": "U0PERSON01", "ts": "1.0", "text": text}
        assert blocker_lines([record]) == [], f"must not read as a blocker: {text}"
