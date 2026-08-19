"""A conversation copied out of Slack becomes records, because some rooms have no export.

The pastes below are the shape Slack's clipboard really produces — furniture welded to
the line it precedes, a bare clock for the next message from the same person, brackets
inside sentences that are not timestamps at all. Each test pins a decision that could
plausibly have gone the other way; the parser is heuristic, so the decisions are the
part worth protecting.
"""

from __future__ import annotations

import json
from datetime import date, timezone

from tam.ingest.blockers import lines_of
from tam.ingest.slack_paste import (
    channel_for,
    merge_into,
    merge_runs,
    parse_paste,
    read_paste,
    speaker_ids,
    to_records,
)

DAY = date(2026, 8, 19)
UTC = timezone.utc

# Verbatim from the paste this module was written for, furniture and all.
CHAT = """image.png 6 repliesAim Sirawith  [2:21 PM]
พี่มอสเคยแก้ให้ผมที่ dev
[2:22 PM]ถ้าสุดท้ายมาไม่แน่ใจนะ
[2:22 PM]ของผมสุดท้ายคือแตก
[2:22 PM]400
jah natta  [2:22 PM]
ของพี่ไม่แตกหวะ
Aim Sirawith  [2:23 PM]
อาจจะคนละเคส อันนี้อีกตังอย่าง เรื่องคอลัม REVERAPP-228 [BUG][View redemption result]The system-missing requirement for the Account_age column (edited)
"""


def records_of(text: str, **kwargs: object) -> list[dict]:
    # names={} on purpose: the machine's own name cache would resolve these speakers to
    # real ids and make the test read differently on every laptop.
    _, records = read_paste(text, title="DM Natta", day=DAY, tz=UTC, names={}, **kwargs)  # type: ignore[arg-type]
    return records


def test_a_run_of_messages_from_one_person_is_one_record() -> None:
    # "ของผมสุดท้ายคือแตก" then "400" is one thought split by the Enter key, and `400`
    # alone would be dropped as noise — the fragment that carries the actual symptom.
    records = records_of(CHAT)
    first = records[0]
    assert "400" in first["text"]
    assert first["text"].startswith("พี่มอสเคยแก้ให้ผมที่ dev")
    assert len([one for one in records if one["user"] == "Aim Sirawith"]) == 2


def test_the_merged_run_keeps_its_line_breaks_so_cue_matching_still_sees_lines() -> None:
    # Joined with spaces, the line saying `ยังรอ …` disappears into a paragraph and the
    # line-level blocker reader has no line to quote as evidence.
    waiting = "Aim Sirawith  [2:21 PM]\nขึ้น dev แล้วครับ\n[2:22 PM]ยังรอ api จากพี่มอสอยู่ ต่อไม่ได้\n"
    record = records_of(waiting)[0]
    assert record["text"].count("\n") == 1
    found = lines_of(record["text"])
    assert [line.cue for line in found] == ["ยังรอ"]
    assert found[0].line.startswith("ยังรอ api"), "the evidence is the line, not the whole message"


def test_a_bracket_that_is_not_a_clock_stays_inside_the_sentence() -> None:
    # [BUG][View redemption result] would otherwise split one message into three, and
    # invent a speaker called "เรื่องคอลัม REVERAPP-228".
    records = records_of(CHAT)
    last = records[-1]
    assert "[BUG][View redemption result]" in last["text"]
    assert last["user"] == "Aim Sirawith"
    assert len(records) == 3


def test_edited_marker_is_not_part_of_the_message() -> None:
    assert "(edited)" not in records_of(CHAT)[-1]["text"]


def test_reply_count_and_attachment_glued_to_a_name_are_not_part_of_the_name() -> None:
    # Slack pastes "image.png 6 replies" with no separator before the next display name.
    assert parse_paste(CHAT, day=DAY, tz=UTC).speakers == ["Aim Sirawith", "jah natta"]


def test_a_copied_scroll_gets_no_thread_ts() -> None:
    # The "6 replies" marker is proof the replies were not copied. One thread_ts over the
    # whole paste would tell graph.py that unrelated messages are a cohesive topic.
    records = records_of(CHAT)
    assert {one["thread_ts"] for one in records} == {""}
    assert {one["channel_id"] for one in records} == {channel_for("DM Natta")}
    assert channel_for("DM Natta").startswith("paste-"), "never collides with a real C… id"


def test_clocks_running_backwards_mean_the_paste_crossed_midnight() -> None:
    text = "Mos  [11:59 PM]\nปิด job แล้วนะ\nMos  [12:05 AM]\nขึ้นเรียบร้อยแล้วครับ\n"
    messages = parse_paste(text, day=DAY, tz=UTC).messages
    assert [message.when.day for message in messages] == [19, 20]


def test_a_day_separator_moves_the_day() -> None:
    text = "Yesterday\nMos  [11:00 PM]\nปิด job แล้วนะ\nToday\nMos  [9:15 AM]\nขึ้นเรียบร้อยแล้วครับ\n"
    messages = parse_paste(text, day=DAY, tz=UTC).messages
    assert [message.when.day for message in messages] == [18, 19]


def test_text_above_the_first_message_is_reported_rather_than_attributed() -> None:
    parse = parse_paste("เมื่อวานคุยกันไว้\n" + CHAT, day=DAY, tz=UTC)
    assert parse.skipped == ["เมื่อวานคุยกันไว้"]
    assert parse.messages[0].speaker == "Aim Sirawith", "the orphan line joins nobody"


def test_pasting_the_same_scroll_again_replaces_instead_of_doubling(tmp_path) -> None:
    corpus = tmp_path / "records.json"
    first = records_of(CHAT)
    total, replaced = merge_into(first, corpus)
    assert (total, replaced) == (len(first), 0)
    # The real habit: paste an overlapping scroll, one new message on the end.
    again = records_of(CHAT + "jah natta  [2:40 PM]\nเดี๋ยวพี่ลองใหม่นะ\n")
    total, replaced = merge_into(again, corpus)
    assert replaced == len(first), "the overlap is replaced, not appended"
    assert total == len(first) + 1
    stored = json.loads(corpus.read_text(encoding="utf-8"))
    assert len({one["id"] for one in stored}) == len(stored)
    assert [one["ts"] for one in stored] == sorted(one["ts"] for one in stored)


def test_a_second_paste_does_not_delete_what_only_the_first_one_had(tmp_path) -> None:
    # Unlike the meeting path there is no "forget the previous import" step: pastes
    # overlap on purpose and a shorter re-paste must not truncate the corpus.
    corpus = tmp_path / "records.json"
    merge_into(records_of(CHAT), corpus)
    merge_into(records_of("jah natta  [2:40 PM]\nเดี๋ยวพี่ลองใหม่นะ\n"), corpus)
    assert len(json.loads(corpus.read_text(encoding="utf-8"))) == 4


def test_a_pasted_name_becomes_the_slack_id_the_export_already_uses() -> None:
    # Otherwise one person is two rows on the people page: their id and their name.
    records = to_records(
        merge_runs(parse_paste(CHAT, day=DAY, tz=UTC).messages),
        title="DM Natta",
        names={"U0AIM": "Aim Sirawith", "U0JAH": "jah natta"},
    )
    assert records[0]["user"] == "U0AIM"
    assert records[0]["speaker_name"] == "Aim Sirawith", "the name as pasted is kept beside the id"


def test_a_name_two_ids_share_is_left_as_typed_rather_than_guessed() -> None:
    assert speaker_ids({"U0ONE": "Aim", "U0TWO": "Aim", "U0JAH": "jah natta"}) == {"jah natta": "U0JAH"}


def test_the_same_message_pasted_twice_keeps_one_id_wherever_it_lands() -> None:
    # The id hashes the clock, not the position, so a message that arrives second in
    # one paste and fifth in the next is still the same record.
    ids = {one["text"]: one["id"] for one in records_of(CHAT)}
    later = {one["text"]: one["id"] for one in records_of("Mos  [1:00 PM]\nเริ่มคุยกันก่อนหน้านี้\n" + CHAT)}
    assert all(ids[text] == later[text] for text in ids)


def test_every_message_keeps_its_own_place_in_time() -> None:
    # Minute resolution puts several messages in one minute; a tie would let sorting
    # reshuffle a reply above the message it answers.
    stamps = [float(one["ts"]) for one in records_of(CHAT)]
    assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps)
