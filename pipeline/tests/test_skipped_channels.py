"""Channels that exist to try the bot out must not become work items.

On this workspace `#meow-meow` and `#meowtamm` hold twenty-five messages of slash-command
tests, pitch-deck drafting and an argument about ice cream. The clustering has no way to
tell that from work, so those twenty-five records had spread themselves across fourteen of
seventy work items, the pitch draft read as a work item that was `resolved`, and
`` `/meowtam blocked` `` — the name of a command somebody was testing — put an item on the
blocked list.

The filter lives at `read_records`, the one door every stage comes through, and these tests
pin it there rather than at ingest. An id-keyed merge can add and replace but not forget,
so filtering on the way in would leave a corpus written before the exclusion untouched, and
the digest, the search, the bot's API and the dashboard would each read a different set of
messages depending on when their copy was built.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tam.core import TamDataError, read_records, skipped_channels


def corpus(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    path = tmp_path / "records.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def row(rid: str, channel: str, text: str = "ข้อความหนึ่ง") -> dict[str, object]:
    return {
        "id": rid,
        "channel_id": channel,
        "ts": "1787065260.000000",
        "thread_ts": "",
        "user": "U0PERSON01",
        "text": text,
        "source": "slack",
    }


def test_unset_skips_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The default has to be "read everything" — a silently narrowed corpus is worse than none."""
    monkeypatch.delenv("TAM_SKIP_CHANNELS", raising=False)
    assert skipped_channels() == frozenset()
    path = corpus(tmp_path, [row("a", "C0WORK00001"), row("b", "C0TEST00001")])
    assert len(read_records(path)) == 2


def test_named_channels_are_dropped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TAM_SKIP_CHANNELS", "C0TEST00001")
    path = corpus(tmp_path, [row("a", "C0WORK00001"), row("b", "C0TEST00001")])
    kept = read_records(path)
    assert [record["id"] for record in kept] == ["a"]


def test_the_list_tolerates_the_spacing_a_person_types(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A config value nobody can mistype is a config value nobody has to debug."""
    monkeypatch.setenv("TAM_SKIP_CHANNELS", " C0TEST00001 , ,C0TEST00002,")
    assert skipped_channels() == frozenset({"C0TEST00001", "C0TEST00002"})
    path = corpus(tmp_path, [row("a", "C0WORK00001"), row("b", "C0TEST00001"), row("c", "C0TEST00002")])
    assert [record["id"] for record in read_records(path)] == ["a"]


def test_skipping_everything_is_an_error_not_an_empty_dashboard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A corpus filtered down to nothing must say so, the same as one that was never built.

    Silence here would render a dashboard reading "no work items", which is indistinguishable
    from a quiet week and is the one answer this project refuses to give.
    """
    monkeypatch.setenv("TAM_SKIP_CHANNELS", "C0TEST00001")
    path = corpus(tmp_path, [row("b", "C0TEST00001")])
    with pytest.raises(TamDataError):
        read_records(path)


def test_records_with_no_channel_survive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Tickets and meeting utterances carry no `channel_id`, and are not Slack channels.

    Matching them against the skip list by treating a missing field as the empty string is
    only safe while the empty string cannot be in the list, which `skipped_channels` ensures
    by dropping blank entries.
    """
    monkeypatch.setenv("TAM_SKIP_CHANNELS", "C0TEST00001")
    ticket = {
        "id": "t1",
        "ts": "1787065260.000000",
        "thread_ts": "",
        "user": "",
        "text": "REVERAPP-1 something",
        "source": "youtrack",
    }
    path = corpus(tmp_path, [row("b", "C0TEST00001"), ticket])
    assert [record["id"] for record in read_records(path)] == ["t1"]


def test_a_read_meant_for_writing_back_keeps_everything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`skip_channels=False` is what stops a display setting from deleting data.

    `ingest.daily` loads the corpus, merges the morning's export into it, and writes the
    result back. Filtering on the way in therefore deletes those records on the way out —
    measured, 27 of them the first morning after this setting existed, recoverable only by
    re-exporting from Slack. `include_threads` guards the same hazard one filter earlier,
    which is why daily passes both.
    """
    monkeypatch.setenv("TAM_SKIP_CHANNELS", "C0TEST00001")
    path = corpus(tmp_path, [row("a", "C0WORK00001"), row("b", "C0TEST00001")])
    assert [r["id"] for r in read_records(path)] == ["a"]
    assert [r["id"] for r in read_records(path, skip_channels=False)] == ["a", "b"]
