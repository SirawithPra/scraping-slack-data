"""What a channel-to-project map has to get right before it is worth having.

The whole value of `TAM_CHANNEL_PROJECTS` is that it is *stated* rather than inferred,
so the failures that matter are the ones where a stated fact quietly stops applying:
a typo in one group silently dropping the others, a many-channels-one-project mapping
losing a channel, or the linker going back to frequency in a channel that named its
project. Each of those looks exactly like the map being empty, which is the one state
nobody would investigate.
"""

from collections import Counter

import pytest

from tam.analysis.linker import pick_ticket, ticket_prefix, trusted_prefixes
from tam.core import channel_projects


def test_groups_several_channels_under_one_project():
    mapping = channel_projects("REVERAPP=C0ABC,C0DEF,C0GHI")
    assert mapping.project_for("C0ABC") == "REVERAPP"
    assert mapping.project_for("C0GHI") == "REVERAPP"
    assert sorted(mapping.channels_of("reverapp")) == ["C0ABC", "C0DEF", "C0GHI"]


def test_label_is_optional_and_falls_back_to_the_key():
    mapping = channel_projects("REVERAPP (Rever App)=C0ABC; MOB=C0GHI")
    assert mapping.label_of("REVERAPP") == "Rever App"
    assert mapping.label_of("MOB") == "MOB"


def test_a_broken_group_does_not_take_the_others_with_it():
    # The failure this guards: one typo making the whole map read as unset, which is
    # indistinguishable from nobody having configured it.
    mapping = channel_projects("REVERAPP=C0ABC; this is not a group; MOB=C0GHI")
    assert mapping.project_for("C0ABC") == "REVERAPP"
    assert mapping.project_for("C0GHI") == "MOB"


def test_channel_names_are_kept_apart_from_ids():
    # A record carries a channel id and nothing in the pipeline can resolve a name, so
    # a `#name` entry must not end up in the id lookup where it would never match.
    mapping = channel_projects("REVERAPP=#reverapp-dev,C0ABC")
    assert mapping.project_for("C0ABC") == "REVERAPP"
    assert mapping.by_name == {"#reverapp-dev": "REVERAPP"}
    assert "#reverapp-dev" not in mapping.by_channel


def test_unset_is_falsy_so_callers_can_degrade():
    assert not channel_projects("")
    assert channel_projects("MOB=C0GHI")


def test_the_channels_own_project_beats_corpus_frequency():
    """The case frequency gets backwards.

    "MOB-12 รอ REV-1421 ก่อน" is a message about MOB-12 that references REV-1421. In a
    corpus where REV is the busy project, frequency picks REV-1421 every time — and the
    work item is then named after the ticket the message was merely waiting on.
    """
    counts = Counter({"REV-1421": 40, "MOB-12": 2})
    assert pick_ticket(["MOB-12", "REV-1421"], counts) == "REV-1421"
    assert pick_ticket(["MOB-12", "REV-1421"], counts, prefer="MOB") == "MOB-12"


def test_preferring_a_project_with_no_candidate_changes_nothing():
    counts = Counter({"REV-1421": 40, "REV-1400": 2})
    assert pick_ticket(["REV-1400", "REV-1421"], counts, prefer="MOB") == "REV-1421"


@pytest.mark.parametrize("key,prefix", [("REV-1421", "REV"), ("REVERAPP-140", "REVERAPP")])
def test_prefix_is_everything_before_the_first_dash(key, prefix):
    assert ticket_prefix(key) == prefix


def test_a_mapped_project_counts_as_a_real_ticket_prefix(monkeypatch):
    """Naming a project in the channel map must not have to be repeated in TICKET_PROJECTS.

    Without this, a team that mapped `#mobile` to MOB and mentioned MOB-12 exactly once
    would have MOB rejected as "probably a standard, not a project" by the corpus guess,
    and the linker would ignore the very key the map is about.
    """
    monkeypatch.setenv("TAM_CHANNEL_PROJECTS", "MOB=C0GHI")
    records = [{"text": "MOB-12 ยังไม่ได้เริ่ม"}]
    assert "MOB" in trusted_prefixes(records)
