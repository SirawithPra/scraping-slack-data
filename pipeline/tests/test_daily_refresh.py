"""The bot must not read its own output back in.

Meowtam posts a digest into a channel it also exports. The digest quotes item labels
and evidence text, so on the next refresh those posts cluster with the very items they
describe and the group reinforces itself — measured on the test channel, five of the
bot's own posts had entered the corpus before this was noticed.

The `is_bot` gate already stops such a message from *setting state*, which is the
worst outcome. It does not stop it reaching the embeddings, so the loop is cut at the
export instead: the exporter learns its own user id from `auth.test` and drops messages
it wrote. Other bots stay — a deploy notification is a real event worth clustering; the
bot reading its own summary of yesterday is not.
"""

from __future__ import annotations

from typing import Any

from tam.ingest.export_slack import merge_exports, newest_ts


class FakeClient:
    """Enough of WebClient for export_channel: one page of history, no threads."""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._messages = messages

    def conversations_history(self, **_: Any) -> dict[str, Any]:
        return {"ok": True, "messages": self._messages, "response_metadata": {}}


def test_the_export_drops_messages_this_bot_wrote() -> None:
    from tam.ingest.export_slack import export_channel

    client = FakeClient([
        {"ts": "100.0", "user": "U0SELFBOT1", "text": "digest ที่บอทโพสต์เอง"},
        {"ts": "200.0", "user": "U0PERSON01", "text": "ข้อความของคน"},
        {"ts": "300.0", "user": "U0OTHERBOT", "text": "Deployment finished"},
    ])
    exported = export_channel(client, "C0TEST0001", 50, 200, "", "U0SELFBOT1")  # type: ignore[arg-type]
    users = [row["user"] for row in exported]
    assert "U0SELFBOT1" not in users, "the bot's own posts must not come back"
    # Another bot's notification is a real event and stays.
    assert users == ["U0PERSON01", "U0OTHERBOT"]


def test_without_a_self_id_nothing_is_dropped() -> None:
    # The filter must be opt-in: a corpus exported by a token whose auth.test gave no
    # user_id should be complete rather than silently emptied.
    from tam.ingest.export_slack import export_channel

    client = FakeClient([{"ts": "100.0", "user": "U0SELFBOT1", "text": "x"}])
    assert len(export_channel(client, "C0TEST0001", 50, 200, "", "")) == 1  # type: ignore[arg-type]


def test_a_quiet_morning_is_not_an_error(tmp_path: Any) -> None:
    # Slack returning nothing new is the normal case, and the daily driver stops before
    # rewriting the corpus for it — re-embedding zero new messages only churns the cache.
    export = tmp_path / "real_C0TEST0001.json"
    export.write_text('[{"ts": "100.0", "user": "U0PERSON01"}]', encoding="utf-8")
    assert newest_ts(export) == "100.0"
    assert merge_exports([{"ts": "100.0"}], []) == [{"ts": "100.0"}]
