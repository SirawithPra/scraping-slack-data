"""A ticket link a person made must be visible everywhere that person can look.

Measured on the live corrections file before this existed: of eight human links, two
named a record no stage could find, and searching the ticket key they had linked to
returned nothing — the linked messages do not contain the key, and search ranks text.
Both failures were silent, and both are indistinguishable from "the link did nothing".

Three properties are pinned here, one per way the silence happened:

* a link that cannot be applied is *returned*, with a cause, not logged
* a linked message is reachable by the ticket key it never types
* the two spellings of a work item id resolve to the same item
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tam.analysis.linker import unresolved_overrides


def row(rid: str, text: str = "ข้อความหนึ่ง", channel: str = "C0WORK0001") -> dict[str, Any]:
    return {
        "id": rid,
        "channel_id": channel,
        "ts": "1787065260.000000",
        "thread_ts": "",
        "user": "U0PERSON01",
        "text": text,
        "source": "slack",
    }


# ---- a link that cannot be applied is data, not a log line -----------------


def test_link_to_a_record_we_have_is_not_reported() -> None:
    records = [row("msg_C0WORK0001_1.0")]
    assert unresolved_overrides(records, {"msg_C0WORK0001_1.0": "ticket:REV-1"}) == []


def test_link_to_a_missing_record_is_returned_with_a_cause() -> None:
    rows = unresolved_overrides([row("msg_C0WORK0001_1.0")], {"msg_C0WORK0001_9.0": "ticket:REV-1"})
    assert [r["record_id"] for r in rows] == ["msg_C0WORK0001_9.0"]
    assert rows[0]["key"] == "ticket:REV-1"
    assert "export" in rows[0]["why"]


def test_a_skipped_channel_is_named_as_the_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    """The commonest cause, and the only one with a fix the reader controls.

    "not in the corpus" sends someone to look for a message that is on their screen in
    Slack. Naming the channel setting that dropped it is the difference between a
    report and a shrug.
    """
    monkeypatch.setenv("TAM_SKIP_CHANNELS", "C0PLAY0001")
    rows = unresolved_overrides([row("msg_C0WORK0001_1.0")], {"msg_C0PLAY0001_2.0": "ticket:REV-1"})
    assert rows[0]["channel"] == "C0PLAY0001"
    assert "C0PLAY0001" in rows[0]["why"] and "ข้าม" in rows[0]["why"]


def test_unlinking_is_not_a_broken_link() -> None:
    """An empty key is how a person *removes* a link. It has nothing to apply."""
    assert unresolved_overrides([row("msg_C0WORK0001_1.0")], {"msg_C0WORK0001_9.0": ""}) == []


# ---- the linked message becomes reachable by its ticket key ----------------


def test_ticket_key_finds_the_message_that_never_says_it() -> None:
    from tam.retrieval.retrieve import PRESETS, Retriever

    records = [
        row("msg_C0WORK0001_1.0", "ใช้ voucher code เดิมไปก่อนนะ PO ยังไม่ยืนยัน"),
        row("msg_C0WORK0001_2.0", "deploy staging เสร็จแล้วครับ"),
    ]
    # `lexical` only: this is a BM25 property, and building the dense half would pull a
    # 2 GB model into a unit test to assert nothing about it.
    retriever = Retriever(records, PRESETS["lexical"], use_cache=False, matrix=_zeros(len(records)))
    assert retriever.lexical_scores("REV-250").max() == 0.0

    retriever.index_links({"msg_C0WORK0001_1.0": "REV-250"})
    scores = retriever.lexical_scores("REV-250")
    assert scores[0] > 0.0
    assert scores[1] == 0.0
    assert retriever.linked_ticket["msg_C0WORK0001_1.0"] == "REV-250"


def test_the_key_goes_to_the_index_and_not_to_the_text() -> None:
    """`texts` feeds the reranker and the display, neither of which may see our note."""
    from tam.retrieval.retrieve import PRESETS, Retriever

    records = [row("msg_C0WORK0001_1.0", "ใช้ voucher code เดิม")]
    retriever = Retriever(records, PRESETS["lexical"], use_cache=False, matrix=_zeros(1))
    retriever.index_links({"msg_C0WORK0001_1.0": "REV-250"})
    assert "REV-250" not in retriever.texts[0]
    assert "REV-250" not in records[0]["text"]
    assert "REV-250" in retriever.index_texts[0]


def test_relinking_to_the_same_ticket_does_not_stack_the_key() -> None:
    from tam.retrieval.retrieve import PRESETS, Retriever

    records = [row("msg_C0WORK0001_1.0", "ใช้ voucher code เดิม")]
    retriever = Retriever(records, PRESETS["lexical"], use_cache=False, matrix=_zeros(1))
    retriever.index_links({"msg_C0WORK0001_1.0": "REV-250"})
    retriever.index_links({"msg_C0WORK0001_1.0": "REV-250"})
    assert retriever.index_texts[0].count("REV-250") == 1


def test_a_link_can_be_moved_to_another_ticket() -> None:
    """Overrides are last-write-wins, so a rebuild must not leave the old key behind.

    Asserted on the index rather than on a score of zero: the tokenizer splits
    `REV-250` into `rev`, `250` and the whole run, so a message indexed under REV-300
    keeps a nonzero BM25 for REV-250 through the shared `rev` alone. That is BM25
    working as intended — the property this test is for is that the old key is gone
    and the new one outranks it.
    """
    from tam.retrieval.retrieve import PRESETS, Retriever

    records = [row("msg_C0WORK0001_1.0", "ใช้ voucher code เดิม")]
    retriever = Retriever(records, PRESETS["lexical"], use_cache=False, matrix=_zeros(1))
    retriever.index_links({"msg_C0WORK0001_1.0": "REV-250"})
    retriever.index_links({"msg_C0WORK0001_1.0": "REV-300"})
    assert retriever.linked_ticket == {"msg_C0WORK0001_1.0": "REV-300"}
    assert "REV-250" not in retriever.index_texts[0]
    assert retriever.lexical_scores("REV-300").max() > retriever.lexical_scores("REV-250").max()


def test_a_link_to_a_record_we_do_not_have_is_ignored_by_the_index() -> None:
    from tam.retrieval.retrieve import PRESETS, Retriever

    records = [row("msg_C0WORK0001_1.0", "ใช้ voucher code เดิม")]
    retriever = Retriever(records, PRESETS["lexical"], use_cache=False, matrix=_zeros(1))
    retriever.index_links({"msg_C0WORK0001_9.0": "REV-250"})
    assert retriever.linked_ticket == {}
    assert retriever.index_texts == retriever.texts


def _zeros(count: int):
    """A stand-in embedding matrix, so a lexical-only test loads no model."""
    import numpy as np

    return np.zeros((count, 8), dtype=np.float32)


# ---- the two spellings of a work item id are one work item -----------------


def _digest(*items: tuple[int, str]):
    """A Digest holding nothing but the item ids under test."""
    from tam.analysis.digest import Digest, Topic

    topics = [Topic(key=key, label="", item_id=item_id) for key, item_id in items]
    return Digest(topics=topics, since=0.0, until=0.0, corpus_size=0)


def test_the_namespaced_key_resolves_to_the_same_item() -> None:
    """`ticket:REV-250` is what the bot writes and what a person copies out of the file.

    It used to 404 while the error listed REV-250 among the available items, which
    reads as the link having done nothing.
    """
    from tam.web.server import find_topic

    digest = _digest((64, "REV-250"))
    assert find_topic(digest, "ticket:REV-250").item_id == "REV-250"
    assert find_topic(digest, "REV-250").item_id == "REV-250"
    assert find_topic(digest, "rev-250").item_id == "REV-250"


def test_an_unknown_item_still_404s() -> None:
    from fastapi import HTTPException
    from tam.web.server import find_topic

    with pytest.raises(HTTPException) as raised:
        find_topic(_digest((64, "REV-250")), "ticket:REV-999")
    assert raised.value.status_code == 404


def test_searching_the_key_names_the_item() -> None:
    from tam.web.server import items_named

    digest = _digest((64, "REV-250"), (45, "c23053d"))
    assert [t.item_id for t in items_named(digest, "REV-250")] == ["REV-250"]
    assert [t.item_id for t in items_named(digest, "ticket:rev-250 อะไรนะ")] == ["REV-250"]
    assert [t.item_id for t in items_named(digest, "c23053d")] == ["c23053d"]


def test_a_prefix_of_a_key_is_not_that_key() -> None:
    """Whole-token matching. `REV-25` naming REV-250 would be a confident wrong answer."""
    from tam.web.server import items_named

    assert items_named(_digest((64, "REV-250")), "REV-25") == []
    assert items_named(_digest((64, "REV-250")), "รอ api ของ event") == []


def test_a_bare_number_works_while_it_is_unambiguous() -> None:
    """People type "250". They should not have to type the project prefix to find it."""
    from tam.web.server import items_named

    assert [t.item_id for t in items_named(_digest((64, "REV-250")), "250")] == ["REV-250"]


def test_an_ambiguous_number_names_nothing() -> None:
    """Two items could mean it, so answering with one of them is the original bug reversed."""
    from tam.web.server import items_named

    assert items_named(_digest((64, "REV-250"), (12, "MOB-250")), "250") == []


# ---- a link the clustering did not act on is still shown --------------------


def test_a_link_to_a_message_in_another_item_is_reported() -> None:
    """The measured case: five links to one ticket, two of which reached its item.

    An override names a message's work item; it does not move the message, because
    membership belongs to the clustering. Two of those five therefore sat in other
    items and one in none, and the item page counted six messages with no hint that
    three more had been attached to the same ticket on purpose.
    """
    from tam.web.server import State, linked_elsewhere

    mine = row("msg_C0WORK0001_1.0", "อยู่ในเรื่องนี้แล้ว")
    stray = row("msg_C0WORK0001_2.0", "โอเคครับ งั้นผมทำต่อด้วยของเดิม")
    orphan = row("msg_C0WORK0001_3.0", "pm อ้างอิงตาม design figma เลย")

    digest = _digest((64, "REV-250"), (45, "c23053d"))
    digest.topics[0].records = [mine]
    digest.topics[1].records = [stray]
    build = State(
        records_path=Path("unused.json"),
        records=[mine, stray, orphan],
        link_overrides={
            "msg_C0WORK0001_1.0": "ticket:REV-250",
            "msg_C0WORK0001_2.0": "ticket:REV-250",
            "msg_C0WORK0001_3.0": "ticket:REV-250",
        },
    )
    reported = linked_elsewhere(build, digest, digest.topics[0])
    assert [r["record_id"] for r in reported] == ["msg_C0WORK0001_2.0", "msg_C0WORK0001_3.0"]
    assert reported[0]["where"] == "c23053d"
    assert reported[1]["where"] == ""  # in no item at all, and says so


def test_a_link_that_landed_is_not_reported_as_a_stray() -> None:
    from tam.web.server import State, linked_elsewhere

    mine = row("msg_C0WORK0001_1.0")
    digest = _digest((64, "REV-250"))
    digest.topics[0].records = [mine]
    build = State(
        records_path=Path("unused.json"),
        records=[mine],
        link_overrides={"msg_C0WORK0001_1.0": "ticket:REV-250"},
    )
    assert linked_elsewhere(build, digest, digest.topics[0]) == []


def test_a_link_to_a_missing_record_is_not_a_stray() -> None:
    """It belongs in `unresolved_links`, with a cause. Listing it twice says it twice."""
    from tam.web.server import State, linked_elsewhere

    digest = _digest((64, "REV-250"))
    build = State(
        records_path=Path("unused.json"),
        records=[],
        link_overrides={"msg_C0GONE0001_9.0": "ticket:REV-250"},
    )
    assert linked_elsewhere(build, digest, digest.topics[0]) == []


def test_another_tickets_links_are_left_alone() -> None:
    from tam.web.server import State, linked_elsewhere

    other = row("msg_C0WORK0001_2.0")
    digest = _digest((64, "REV-250"), (12, "REV-300"))
    digest.topics[1].records = [other]
    build = State(
        records_path=Path("unused.json"),
        records=[other],
        link_overrides={"msg_C0WORK0001_2.0": "ticket:REV-300"},
    )
    assert linked_elsewhere(build, digest, digest.topics[0]) == []
