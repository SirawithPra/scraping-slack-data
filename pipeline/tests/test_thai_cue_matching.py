"""Thai cues are matched as whole words, which needs tokenising both sides.

Thai writes without spaces, so there is no boundary character to anchor a search to. The
first implementation used `text.find(cue)` and said so in its docstring; that fails in
both directions on real messages, and the two failures hide each other:

* **Over-matching.** `รอ` ("wait") occurs inside `รอบ` ("round"), `กรอก` ("fill in") and
  `หรอ` (a question particle). The bare cue hit 92 messages, almost none about waiting.
* **Under-matching**, which single-token matching would not have fixed either. The cue
  list is full of multi-syllable phrases — `ยังรอ`, `ยังไม่มา`, `ยังทำไม่ได้` — and the
  tokeniser splits every one of them, so matching one token at a time means those cues
  can never fire at all. `ไม่ได้` is `[ไม่, ได้]`.

Matching the cue's tokens as a contiguous run is what handles both, and this file pins
each direction separately so a future edit cannot trade one for the other silently.
"""

from __future__ import annotations

from tam.analysis.relations import RELATION_TYPES, cue_offset, matched_cue, thai_offset

BLOCKED_BY = next(relation for relation in RELATION_TYPES if relation.name == "blocked_by")


# Every assertion goes through `cue_offset`, the function the relation typer actually
# calls, rather than the Thai helper directly. Tested against the helper, these all still
# passed when the wiring was reverted to `text.find` — the mutation that motivated the
# whole file — because the over-matching cases never reached the substring path.


def test_a_cue_inside_a_longer_word_does_not_match() -> None:
    # The three that made the bare substring search useless.
    assert cue_offset("รอบนี้ไม่ทัน", "รอ") < 0, "รอบ is a round, not waiting"
    assert cue_offset("กรอกฟอร์มแล้ว", "รอ") < 0, "กรอก is filling in a form"
    assert cue_offset("จริงหรอ", "รอ") < 0, "หรอ is a question particle"


def test_a_multi_syllable_cue_matches_across_its_tokens() -> None:
    # These are the majority of the Thai cue list and could never fire before.
    assert cue_offset("ยังรอ api อยู่", "ยังรอ") >= 0
    assert cue_offset("ยังทำไม่ได้ครับ", "ยังทำไม่ได้") >= 0
    assert cue_offset("งานนี้ต้องรอ be ก่อน", "ต้องรอ") >= 0


def test_the_cue_still_matches_when_it_is_a_whole_word_on_its_own() -> None:
    assert cue_offset("ยังรอ api อยู่", "รอ") >= 0


def test_the_offset_points_at_the_cue_so_the_window_still_works() -> None:
    # `answers` only trusts a cue near the start of a message, so the returned position
    # has to be the cue's, not zero and not the token index.
    text = "aaaa bbbb cccc งานนี้ต้องรอ be ก่อน"
    offset = cue_offset(text, "ต้องรอ")
    assert offset == text.index("ต้องรอ")


def test_the_helper_and_the_wiring_agree() -> None:
    # Guards the seam itself: cue_offset must route a Thai cue to the tokenising path.
    for text, cue in (("รอบนี้ไม่ทัน", "รอ"), ("ยังรอ api อยู่", "ยังรอ"), ("จริงหรอ", "รอ")):
        assert cue_offset(text, cue) == thai_offset(text, cue)


def test_latin_cues_keep_their_word_boundaries() -> None:
    # The other half of the same bug: a bare "no" matched inside Android, know and now,
    # which typed every message as an answer.
    assert cue_offset("android build is green", "no") < 0
    assert cue_offset("no, that is not it", "no") >= 0


def test_a_real_blocker_sentence_reports_the_cue_it_matched() -> None:
    # matched_cue returns the phrase because it becomes the evidence string shown to a
    # person; a relation you cannot explain is one you cannot argue with.
    assert matched_cue("mkt ต้องรอ เดี๊ยวเป็นสปิ้นใหม่แยก", BLOCKED_BY) == "ต้องรอ"
    assert matched_cue("สรุปแล้วพรุ่งนี้ไปถาม user อีกที", BLOCKED_BY) == ""
