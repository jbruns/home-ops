"""hops bump's target tag: the newest tag shaped like the pinned one (the rule Canary uses), refusing
a jump of more than one major version."""
import pytest

from hops.registry import Registry
from hops.tags import propose
from hops_registry_fixtures import Replay


def newest_same_shape(pinned, tags):
    return propose(pinned, tags).tag


def test_keeps_the_v_prefix_and_number_of_version_parts():
    tags = ["latest", "v1.10.3", "v1.11.0", "v1.12.0", "v1.12", "v1", "1.13.0", "v1.11.0-arm64"]
    assert newest_same_shape("v1.11.0", tags) == "v1.12.0"


def test_without_a_v_prefix_ignores_v_tags():
    assert newest_same_shape("3.7.8", ["3.7.8", "v3.9.0", "3.8.1"]) == "3.8.1"


def test_compares_versions_numerically_across_any_number_of_parts():
    tags = ["4.0.9.2244", "4.0.15.2941", "4.0.14.2939", "rolling"]
    assert newest_same_shape("4.0.14.2939", tags) == "4.0.15.2941"


def test_keeps_the_variant_suffix():
    tags = ["9.1.2", "9.2.0", "9.1.2-alpine", "9.1.3-alpine", "9.1.3-alpine3.22", "10.0.0-bookworm"]
    assert newest_same_shape("9.1.2-alpine", tags) == "9.1.3-alpine"


def test_excludes_pre_releases():
    tags = ["v3.7.15", "v3.8.0-rc.1", "v3.8.0-beta1", "v4.0.0-alpha", "v3.7.16"]
    assert newest_same_shape("v3.7.15", tags) == "v3.7.16"
    assert newest_same_shape("17.0-alpine", ["17.0-alpine", "18.0-rc1-alpine"]) == "17.0-alpine"


@pytest.mark.parametrize("fixture, repository", [
    ("lscr", "lscr.io/linuxserver/qbittorrent"),
    ("ghcr-qbittorrent", "ghcr.io/linuxserver/qbittorrent"),
])
def test_a_stray_tag_more_than_one_major_ahead_is_rejected(fixture, repository):
    tags = Registry(Replay(fixture)).tags(repository)  # the real tag set: has a 2021 `20.04.1`
    proposal = propose("5.2.4", tags)
    assert proposal.rejected == "20.04.1"
    assert proposal.tag == "5.3.0"


def test_one_major_ahead_is_accepted():
    assert propose("4.0.20.3012", ["4.0.20.3012", "5.0.0.2742"]) == ("5.0.0.2742", None)
    assert propose("v2.9.1", ["v2.9.1", "v4.0.0", "v3.1.0"]) == ("v3.1.0", "v4.0.0")


def test_with_only_a_stray_ahead_the_pin_is_the_best_acceptable_tag():
    assert propose("5.2.4", ["5.2.4", "20.04.1"]) == ("5.2.4", "20.04.1")


def test_a_pin_that_is_not_a_version_has_no_candidate():
    assert propose("latest", ["latest", "1.0.0"]) == (None, None)
    assert newest_same_shape("supervised-1.4.27", ["supervised-1.4.28"]) is None
