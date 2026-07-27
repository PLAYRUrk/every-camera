"""Instance names must stay unique so several every-camera runs cannot collide.

The name drives the MQTT topics, the log file and preview_{instance}.png. Two
copies started from one identical config.json used to get the same name and
quietly overwrite each other's status and frames. ``claim_instance_name`` holds
a flock for the life of the process and appends -2, -3 … while a live process
owns the name.
"""
import sys

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import utils  # noqa: E402


def test_the_default_name_follows_the_node_not_the_ip_octet():
    # The old {camera}_{last_IP_octet} scheme collided across subnets: a .5 in
    # 192.168.1.x and a .5 in 10.0.0.x produced the same topics.
    name = utils.get_instance_name("Cannon", {"node_name": "hut-north"})
    assert name == "Cannon_hut-north"


def test_an_awkward_node_name_is_made_safe_for_topics_and_files():
    name = utils.get_instance_name("ASI", {"node_name": "roof/east tower #2"})
    assert name == "ASI_roof_east_tower__2"
    assert "/" not in name and " " not in name


def test_an_empty_node_name_falls_back_to_the_hostname():
    name = utils.get_instance_name("ASI", {"node_name": "   "})
    assert name.startswith("ASI_") and len(name) > len("ASI_")


def test_a_second_claim_of_a_live_name_gets_an_ordinal(tmp_path):
    first = utils.claim_instance_name("ASI_hut", lock_dir=str(tmp_path))
    try:
        second = utils.claim_instance_name("ASI_hut", lock_dir=str(tmp_path))
        try:
            assert first.name == "ASI_hut"
            assert second.name == "ASI_hut-2"
            third = utils.claim_instance_name("ASI_hut", lock_dir=str(tmp_path))
            try:
                assert third.name == "ASI_hut-3"
            finally:
                third.release()
        finally:
            second.release()
    finally:
        first.release()


def test_a_released_name_is_handed_out_again(tmp_path):
    first = utils.claim_instance_name("Infra_hut", lock_dir=str(tmp_path))
    assert first.name == "Infra_hut"
    first.release()
    # A run that ended — cleanly or by crashing, since the kernel drops the
    # flock either way — must not push the next one to Infra_hut-2 forever.
    second = utils.claim_instance_name("Infra_hut", lock_dir=str(tmp_path))
    try:
        assert second.name == "Infra_hut"
    finally:
        second.release()


def test_the_claim_works_as_a_context_manager(tmp_path):
    with utils.claim_instance_name("SPTT_hut", lock_dir=str(tmp_path)) as claim:
        assert claim.name == "SPTT_hut"
    assert utils.claim_instance_name("SPTT_hut", lock_dir=str(tmp_path)).name == "SPTT_hut"


def test_releasing_twice_is_harmless(tmp_path):
    claim = utils.claim_instance_name("Sentry_hut", lock_dir=str(tmp_path))
    claim.release()
    claim.release()


def test_an_explicit_name_is_uniquified_too(tmp_path):
    # Two nodes deliberately configured with the same sentry.instance_name are
    # just as much of a collision as two defaults.
    a = utils.claim_instance_name("my-camera", lock_dir=str(tmp_path))
    b = utils.claim_instance_name("my-camera", lock_dir=str(tmp_path))
    try:
        assert (a.name, b.name) == ("my-camera", "my-camera-2")
    finally:
        a.release()
        b.release()
