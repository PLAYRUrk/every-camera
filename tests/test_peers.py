"""The peer registry: what makes a camera findable after broadcast stops working.

UDP discovery works perfectly until the first switch that drops broadcast, and
then it finds nothing whatsoever. The registry is the answer to that, and it is
only worth anything if it is honest about addresses — a remembered address that
does not work is worse than no memory at all, because it is probed on every
round for a month. Hence the emphasis below on where a peer's address comes
from, and on what gets forgotten.

No sockets are opened here: the announcement path is driven directly, which is
also the only way to test it on a machine where two processes cannot share the
discovery port.
"""
import json
import os
import sys
import time

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discovery                                                 # noqa: E402
import peers                                                     # noqa: E402


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """Point the registry at a directory of its own."""
    path = str(tmp_path / "peers")
    monkeypatch.setattr(peers, "PEERS_DIR", path)
    return path


def node(host="192.168.2.36", port=8765, name="TORY", instance="ASI_ASI",
         camera="asi", **extra):
    record = {"host": host, "http_port": port, "node_name": name,
              "instance_name": instance, "camera_type": camera,
              "service": "every-camera"}
    record.update(extra)
    return record


# -- remembering -------------------------------------------------------------
def test_a_camera_seen_once_is_remembered(registry):
    peers.remember([node()])
    assert peers.addresses() == ["192.168.2.36"]


def test_remembering_survives_the_process(registry):
    peers.remember([node()])
    files = sorted(Path(registry).glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8"))["instance_name"] == "ASI_ASI"


def test_two_writers_do_not_lose_each_other(registry):
    """The reason for a file per peer rather than one file with a list in it.

    Read-modify-write from several processes drops entries, and this directory
    is shared by every camera on the machine. Simulated here by interleaving:
    both writers read the registry as it was, and then both write.
    """
    before_a = peers.load()
    before_b = peers.load()
    assert before_a == before_b == []

    peers.remember([node(port=8801, instance="A")])
    peers.remember([node(port=8802, instance="B")])
    assert sorted(p["http_port"] for p in peers.load()) == [8801, 8802]


def test_two_cameras_on_one_machine_are_two_peers_but_one_address(registry):
    peers.remember([node(port=8765, instance="ASI_1"),
                    node(port=8766, instance="ASI_2")])
    assert len(peers.load()) == 2
    # The probe is per address, so the machine is asked once and answers twice.
    assert peers.addresses() == ["192.168.2.36"]


def test_seeing_a_camera_again_updates_it_rather_than_duplicating_it(registry):
    peers.remember([node(name="OLD")])
    peers.remember([node(name="TORY")])
    stored = peers.load()
    assert len(stored) == 1
    assert stored[0]["node_name"] == "TORY"


def test_nothing_seen_leaves_what_was_remembered_alone(registry):
    peers.remember([node()])
    assert len(peers.remember([])) == 1


def test_a_reply_without_an_address_is_not_a_peer(registry):
    peers.remember([{"instance_name": "nowhere", "http_port": 8765}])
    assert peers.load() == []


def test_only_what_stays_true_is_kept(registry):
    # A discovery reply carries the moment as well as the peer — focus state,
    # frame counters, whether the shutter is open. Storing those would mean
    # serving a month-old answer as if it were current.
    peers.remember([node(status="running", focus_active=True, shots_taken=41)])
    stored = peers.load()[0]
    assert "status" not in stored and "shots_taken" not in stored
    assert stored["camera_type"] == "asi"


# -- forgetting --------------------------------------------------------------
def test_a_peer_not_heard_from_for_a_month_is_forgotten(registry):
    peers.save([dict(node(), last_seen=time.time() - peers.MAX_AGE_SECONDS - 1)])
    assert peers.load() == []


def test_a_peer_down_for_a_fortnight_is_still_remembered(registry):
    peers.save([dict(node(), last_seen=time.time() - 14 * 24 * 3600)])
    assert len(peers.load()) == 1


def test_a_peer_can_be_dropped_outright(registry):
    peers.remember([node(port=8765), node(port=8766)])
    assert peers.forget("192.168.2.36", 8765) == 1
    assert [p["http_port"] for p in peers.load()] == [8766]


def test_the_file_cannot_grow_without_bound(registry):
    peers.remember([node(host=f"10.0.0.{i}") for i in range(1, 255)])
    assert len(peers.load()) <= peers.MAX_PEERS


def test_a_corrupt_entry_costs_one_peer_and_not_the_registry(registry):
    peers.remember([node(port=8801)])
    Path(registry, "broken.json").write_text("{ not json", encoding="utf-8")
    # The unreadable file is skipped; the good one is still there.
    assert [p["http_port"] for p in peers.load()] == [8801]


def test_an_unwritable_registry_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(peers, "PEERS_DIR", str(tmp_path / "nope"))
    monkeypatch.setattr(peers.os, "makedirs",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
    assert peers.save([node()]) is False


# -- announcements -----------------------------------------------------------
class _Responder(discovery.DiscoveryResponder):
    """A responder with no socket: only the parsing side is under test."""

    def __init__(self):
        super().__init__(lambda: {})


def test_an_announcement_from_a_neighbour_is_remembered(registry):
    responder = _Responder()
    payload = json.dumps(node(host="wrong-and-ignored", pid=1)).encode()
    responder._note_announcement(payload, ("192.168.2.36", 45455))

    stored = peers.load()
    assert len(stored) == 1
    # The address comes from the packet, never from what the sender claimed: a
    # node behind NAT does not know how it is reached.
    assert stored[0]["host"] == "192.168.2.36"


def test_a_node_does_not_remember_itself(registry):
    responder = _Responder()
    payload = json.dumps(node(pid=os.getpid())).encode()
    responder._note_announcement(payload, ("192.168.2.36", 45455))
    assert peers.load() == []


@pytest.mark.parametrize("payload", [
    b"{ not json",
    json.dumps({"service": "something-else", "host": "1.2.3.4"}).encode(),
    json.dumps(["not", "a", "dict"]).encode(),
    b"",
])
def test_rubbish_on_the_wire_is_ignored(registry, payload):
    responder = _Responder()
    responder._note_announcement(payload, ("192.168.2.36", 45455))
    assert peers.load() == []


# -- discovery uses the registry ---------------------------------------------
def test_remembered_addresses_are_probed_by_unicast(registry, monkeypatch):
    peers.remember([node(host="10.9.8.7")])
    probed = []

    class FakeSocket:
        def setsockopt(self, *a):
            pass

        def settimeout(self, *a):
            pass

        def connect(self, target):
            pass                      # _broadcast_addresses derives the /24

        def getsockname(self):
            return ("192.168.2.10", 0)

        def sendto(self, payload, target):
            probed.append(target[0])

        def recvfrom(self, size):
            raise TimeoutError

        def close(self):
            pass

    monkeypatch.setattr(discovery.socket, "socket", lambda *a, **k: FakeSocket())
    monkeypatch.setattr(discovery.socket, "timeout", TimeoutError)
    discovery.discover(timeout=0)

    # The broadcast probe still goes out; the remembered address goes out too,
    # and that is the one that crosses a switch which drops broadcast.
    assert "10.9.8.7" in probed
    assert discovery.MCAST_GROUP in probed


def test_the_registry_can_be_left_out_of_it(registry, monkeypatch):
    peers.remember([node(host="10.9.8.7")])
    probed = []

    class FakeSocket:
        def setsockopt(self, *a):
            pass

        def settimeout(self, *a):
            pass

        def connect(self, target):
            pass                      # _broadcast_addresses derives the /24

        def getsockname(self):
            return ("192.168.2.10", 0)

        def sendto(self, payload, target):
            probed.append(target[0])

        def recvfrom(self, size):
            raise TimeoutError

        def close(self):
            pass

    monkeypatch.setattr(discovery.socket, "socket", lambda *a, **k: FakeSocket())
    monkeypatch.setattr(discovery.socket, "timeout", TimeoutError)
    discovery.discover(timeout=0, use_peers=False)
    assert "10.9.8.7" not in probed
