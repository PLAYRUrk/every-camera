"""Every camera as a way in to the others — and the two rules that keep that safe.

The gateway is what replaces the broker: reach one camera and you have reached
the site, with the archive, the live stream and focusing coming back through it
rather than the status-and-one-JPEG a relay could carry.

That is also the whole risk. The frame server has no authentication by design —
it is meant for a trusted segment — so a forwarding endpoint on it is one
mistake away from being an open proxy to anything the observatory network can
route to. Most of what is pinned down below is therefore what the gateway
*refuses*: unknown targets, paths that are not its business, and requests that
have already been forwarded.
"""
import json
import sys

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gateway                                                   # noqa: E402
import peers                                                     # noqa: E402


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(peers, "PEERS_DIR", str(tmp_path / "peers"))
    return tmp_path


class FakeService:
    """Just enough of CameraService for the gateway to describe this node."""

    http_port = 8765

    def info(self):
        return {"instance_name": "ASI_ASI", "camera_type": "asi",
                "node_name": "TORY"}

    def status(self):
        return {"status": "running", "shots_taken": 41}


def peer(host="192.168.2.40", port=8765, name="SURA", instance="INFRA_1"):
    return {"host": host, "http_port": port, "node_name": name,
            "instance_name": instance, "camera_type": "infra"}


@pytest.fixture
def fleet(registry, monkeypatch):
    monkeypatch.setattr(gateway, "_local_address", lambda: "192.168.2.36")
    return gateway.Gateway(FakeService(), refresh=0.01, poll_timeout=0.01)


# -- the fleet view ----------------------------------------------------------
def test_a_node_with_no_neighbours_still_lists_itself(fleet):
    listed = fleet.nodes()
    assert len(listed) == 1
    assert listed[0]["self"] is True
    assert listed[0]["node_name"] == "TORY"


def test_a_remembered_neighbour_is_listed_with_the_target_to_reach_it(fleet):
    peers.remember([peer()])
    fleet.refresh_once()
    neighbour = [n for n in fleet.nodes() if not n["self"]][0]
    assert neighbour["target"] == "192.168.2.40:8765"
    assert neighbour["node_name"] == "SURA"


def test_a_neighbour_that_stopped_answering_is_kept_and_marked(fleet):
    peers.remember([peer()])
    fleet.refresh_once()          # nothing is listening, so the poll fails
    neighbour = [n for n in fleet.nodes() if not n["self"]][0]
    # Not dropped: "was there and stopped answering" is the whole reason
    # anybody is watching.
    assert neighbour["reachable"] is False
    assert neighbour["error"]


def test_a_node_does_not_list_itself_twice(fleet):
    # Cameras on one machine share the registry, so this node's own entry is in
    # it — written by a neighbour, which had no way to know it was us.
    peers.remember([peer(host="192.168.2.36", port=8765, name="TORY")])
    fleet.refresh_once()
    assert [n["self"] for n in fleet.nodes()] == [True]


def test_the_observer_can_ask_for_the_neighbours_alone(fleet):
    peers.remember([peer()])
    fleet.refresh_once()
    assert all(not n["self"] for n in fleet.nodes(include_self=False))


# -- refusing to forward -----------------------------------------------------
def test_an_unknown_node_is_not_forwarded_to(fleet):
    with pytest.raises(LookupError):
        fleet.open_upstream("8.8.8.8:80", "/api/info")


def test_a_neighbour_that_was_seen_once_stays_forwardable(fleet):
    # Straight from the registry, without a successful poll: a camera that is
    # down right now is still the camera the observer meant.
    peers.remember([peer()])
    assert fleet.known_target("192.168.2.40:8765") is not None


def test_a_request_that_has_already_been_forwarded_is_refused(fleet):
    peers.remember([peer()])
    with pytest.raises(RecursionError):
        fleet.open_upstream("192.168.2.40:8765", "/api/info",
                            hops=gateway.MAX_HOPS)


@pytest.mark.parametrize("path", [
    "/etc/passwd",
    "/../../secret",
    "/admin",
    "",
])
def test_paths_that_are_not_the_cameras_business_are_refused(fleet, path):
    peers.remember([peer()])
    with pytest.raises(ValueError):
        fleet.open_upstream("192.168.2.40:8765", path)


@pytest.mark.parametrize("path", [
    "/", "/index.html", "/api/info", "/api/live.mjpg", "/api/frame",
])
def test_what_the_camera_actually_serves_is_forwardable(path):
    assert gateway.forwardable(path) is True


def test_the_whitelist_is_not_defeated_by_a_query_string():
    assert gateway.forwardable("/api/frame?name=x") is True
    assert gateway.forwardable("/etc/passwd?name=/api/") is False


# -- addressing --------------------------------------------------------------
@pytest.mark.parametrize("path,expected", [
    ("/api/node/host:8765/api/info", ("host:8765", "/api/info")),
    ("/api/node/host:8765/", ("host:8765", "/")),
    ("/api/node/host:8765", ("host:8765", "/")),
    ("/api/node/host:8765/api/frame", ("host:8765", "/api/frame")),
    ("/api/node/", (None, None)),
    ("/api/status", (None, None)),
])
def test_the_target_and_the_path_are_split_apart(path, expected):
    assert gateway.parse_node_path(path) == expected


# -- it never gets in the way ------------------------------------------------
def test_a_neighbour_that_hangs_does_not_hold_up_the_others(fleet, monkeypatch):
    calls = []

    def slow(host, port, path):
        calls.append(host)
        raise TimeoutError("no answer")

    monkeypatch.setattr(fleet, "_get_json", slow)
    peers.remember([peer(host="10.0.0.1"), peer(host="10.0.0.2")])
    fleet.refresh_once()
    # Both were tried; neither failure stopped the other.
    assert sorted(calls) == ["10.0.0.1", "10.0.0.2"]
    assert len(fleet.nodes(include_self=False)) == 2


def test_a_service_that_will_not_answer_does_not_break_the_listing(registry,
                                                                   monkeypatch):
    class Broken:
        http_port = 8765

        def info(self):
            raise RuntimeError("camera is busy")

        def status(self):
            raise RuntimeError("camera is busy")

    monkeypatch.setattr(gateway, "_local_address", lambda: "192.168.2.36")
    listed = gateway.Gateway(Broken()).nodes()
    assert len(listed) == 1 and listed[0]["self"] is True
