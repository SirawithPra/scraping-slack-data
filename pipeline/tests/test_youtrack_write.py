"""The write path, which is the one thing here a person cannot undo quietly.

`add_comment` posts to a live tracker where the whole team can see the result, so the
tests that matter are about refusing: refusing without an explicit opt-in, refusing an
empty body, and never falling back to the read token unless somebody said yes. A
regression in any of those is a bot writing to a real project on a deployment that
believed it had granted read access only.

`search_query` is here for a different reason — it decides whether a person typing a
ticket number finds their ticket, and every one of its three shapes is a case where
the obvious implementation gets it wrong.
"""

import pytest

from tam.ingest.youtrack import YouTrackError, add_comment, search_query, write_config


@pytest.fixture(autouse=True)
def _tracker_env(monkeypatch):
    monkeypatch.setenv("YOUTRACK_URL", "https://example.youtrack.cloud")
    monkeypatch.setenv("YOUTRACK_TOKEN", "read-only-token")
    monkeypatch.delenv("YOUTRACK_WRITE", raising=False)
    monkeypatch.delenv("YOUTRACK_WRITE_TOKEN", raising=False)
    monkeypatch.setenv("YOUTRACK_PROJECTS", "REV")


def test_writing_is_off_until_somebody_turns_it_on():
    with pytest.raises(YouTrackError) as error:
        write_config()
    assert "YOUTRACK_WRITE" in str(error.value)


def test_the_read_token_is_only_borrowed_after_an_explicit_yes(monkeypatch):
    monkeypatch.setenv("YOUTRACK_WRITE", "1")
    base, token = write_config()
    assert base == "https://example.youtrack.cloud"
    assert token == "read-only-token"


def test_a_dedicated_write_token_wins(monkeypatch):
    monkeypatch.setenv("YOUTRACK_WRITE", "yes")
    monkeypatch.setenv("YOUTRACK_WRITE_TOKEN", "can-comment")
    assert write_config()[1] == "can-comment"


def test_an_empty_comment_is_refused_before_any_request(monkeypatch):
    # Checked before write_config, so the message is about the comment rather than
    # about configuration — a blank comment on a ticket is noise somebody deletes by hand.
    monkeypatch.setenv("YOUTRACK_WRITE", "1")
    with pytest.raises(YouTrackError) as error:
        add_comment("REV-1", "   ")
    assert "ว่างเปล่า" in str(error.value)


def test_commenting_without_the_switch_never_reaches_the_network(monkeypatch):
    def explode(*args, **kwargs):  # pragma: no cover - it must not be called
        raise AssertionError("a request went out with writing switched off")

    monkeypatch.setattr("tam.ingest.youtrack._request", explode)
    with pytest.raises(YouTrackError):
        add_comment("REV-1", "hello")


def test_a_full_key_searches_by_identity_not_by_text():
    # The digits of a ticket number appear in half the descriptions in a project, so a
    # text search for "1421" buries REV-1421 under everything that mentions it.
    assert search_query("REV-1421", ["REV"]) == "issue id: REV-1421"
    assert search_query("#rev-1421", ["REV"]) == "issue id: REV-1421"


def test_a_bare_number_resolves_only_when_one_project_is_in_scope():
    assert search_query("1421", ["REV"]) == "issue id: REV-1421"
    # Two projects and a bare number is genuinely ambiguous; guessing one would send
    # somebody to the wrong ticket with no sign that a choice was made.
    assert "issue id:" not in search_query("1421", ["REV", "MOB"])


def test_free_text_is_scoped_and_ordered():
    query = search_query("redemption", ["REV"])
    assert query.startswith("project: REV")
    assert "redemption" in query
    assert "sort by: updated desc" in query


def test_an_empty_query_still_returns_something_useful():
    # The picker opens before anyone types, and "no query" must mean "the project's
    # most recent tickets", not an error or an empty list.
    assert search_query("", ["REV"]) == "project: REV sort by: updated desc"
