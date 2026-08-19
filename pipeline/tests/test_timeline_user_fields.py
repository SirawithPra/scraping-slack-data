"""A timeline event carries the id *and* the name, not one of the two.

The API used to send only `names().of(...)` under `from_user`/`to_user`, and that
single choice broke both consumers at once. The Slack bot could not resolve the
value under its own `TAM_NAMES` — a bot in pseudonym mode rendered whatever mode
the *server* was in, real names included — and nothing downstream could match the
value back to a user id, because a name is not a key. Meanwhile the bot rendered
message authors from the raw `user` field, so one Slack card showed a name in its
timeline and an id on the evidence line above it.

So: `_user` is the id, `_user_name` is the rendering of it. This test pins both
halves of that contract, and pins that a corpus value which is already a name (a
meeting transcript's speaker) still survives in the id field — there is nothing
else to put there.
"""

from __future__ import annotations

from tam.analysis.digest import Topic, timeline
from tam.analysis.relations import extract_relations
from tam.ingest.users import pseudonym

HOUR = 3600.0
BASE = 1786500000.0

BLOCKED = {
    "id": f"msg_C0DEMOCHAN1_{BASE:.3f}",
    "text": "FE sorting ยังรอ API อยู่ ไปต่อไม่ได้",
    "ts": f"{BASE:.3f}",
    "user": "U0DEMOUSER1",
}
RESOLVED = {
    "id": f"mtg_20260814-0930-daily-standup_{BASE + HOUR:.3f}",
    "text": "sorting API deploy แล้ว เสร็จแล้วครับ",
    "ts": f"{BASE + HOUR:.3f}",
    # A transcript speaker: already a name, never an id.
    "user": "Alice",
}


def events(monkeypatch) -> list[dict]:
    monkeypatch.setenv("TAM_NAMES", "pseudonym")
    records = [BLOCKED, RESOLVED]
    relations = extract_relations(records, [(0, 1)], method="rules")
    topic = Topic(key=0, label="sorting api", item_id="TAM-x", records=records,
                  state="resolved", evidence="", evidence_id="", state_since=0.0)
    topic.relations = relations
    return timeline(topic, records)


def test_id_and_name_are_separate_fields(monkeypatch) -> None:
    found = events(monkeypatch)
    assert found, "the fixture must produce at least one relation to describe"
    event = found[0]

    # The id, verbatim from the record — joinable, and what the bot resolves itself.
    assert event["from_user"] == "U0DEMOUSER1"
    # The rendering, under the mode the server is running in.
    assert event["from_user_name"] == pseudonym("U0DEMOUSER1")
    assert event["from_user_name"] != event["from_user"]


def test_a_speaker_name_survives_in_the_id_field(monkeypatch) -> None:
    """A meeting utterance has no id to carry, and must not be blanked."""
    event = events(monkeypatch)[0]
    assert event["to_user"] == "Alice"
    assert event["to_user_name"] == "Alice"


def test_every_event_has_all_four_fields(monkeypatch) -> None:
    """A partially-filled event would leave one row of a rendered timeline blank."""
    for event in events(monkeypatch):
        for field in ("from_user", "from_user_name", "to_user", "to_user_name"):
            assert field in event, f"{field} missing from a timeline event"
            assert event[field], f"{field} is empty"
