"""Several every-camera runs must coexist on one identical config.json.

Two things used to make that impossible:

* the frame server took ``server.port`` or nothing, so the second instance on a
  machine silently lost its LAN server;
* discovery answered a broadcast probe, and the kernel hands a broadcast
  datagram to exactly one socket, so only one instance per machine was ever
  found — the other was running but invisible.

No camera hardware is involved: the frame server runs against a bare
CameraService and discovery against a stub info provider.
"""
import socket
import sys

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discovery                                       # noqa: E402
import frame_server                                    # noqa: E402

from camera_service import CameraService               # noqa: E402


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _service(name):
    return CameraService("asi", name, "", node_name="testnode")


# ---------------------------------------------------------------------------
# Port search
# ---------------------------------------------------------------------------
def test_a_busy_port_moves_the_next_instance_up_one():
    port = _free_port()
    cfg = {"port": port, "discovery": False, "bind": "127.0.0.1"}
    first = frame_server.start_frame_server(cfg, _service("A"))
    assert first is not None
    try:
        second = frame_server.start_frame_server(cfg, _service("A-2"))
        assert second is not None
        try:
            assert (first.port, second.port) == (port, port + 1)
            # The URL the operator is told to point focus_app at must be the
            # port actually bound, not the one that was asked for.
            assert str(second.port) in second.url
        finally:
            second.stop()
    finally:
        first.stop()


def test_port_search_zero_keeps_the_old_strict_behaviour():
    port = _free_port()
    cfg = {"port": port, "discovery": False, "bind": "127.0.0.1", "port_search": 0}
    first = frame_server.start_frame_server(cfg, _service("A"))
    assert first is not None
    try:
        assert frame_server.start_frame_server(cfg, _service("A-2")) is None
    finally:
        first.stop()


def test_a_disabled_server_is_not_started():
    assert frame_server.start_frame_server(
        {"enabled": False}, _service("A")) is None


def test_the_search_gives_up_after_the_configured_span():
    port = _free_port()
    held = []
    try:
        for _ in range(3):
            cfg = {"port": port, "discovery": False, "bind": "127.0.0.1",
                   "port_search": 2}
            server = frame_server.start_frame_server(cfg, _service("A"))
            if server is None:
                break
            held.append(server)
        # port, port+1, port+2 fit; the fourth attempt has nowhere left to go.
        assert len(held) == 3
        assert frame_server.start_frame_server(
            {"port": port, "discovery": False, "bind": "127.0.0.1",
             "port_search": 2}, _service("A")) is None
    finally:
        for server in held:
            server.stop()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def test_two_responders_on_one_machine_are_both_found():
    port = _free_port()          # a UDP bind on the same number is fine
    responders = []
    try:
        for http_port, name in ((8765, "ASI_testnode"), (8766, "ASI_testnode-2")):
            responder = discovery.start_responder(
                lambda p=http_port, n=name: {
                    "instance_name": n, "camera_type": "asi",
                    "http_port": p, "node_name": "testnode"},
                port=port)
            if responder is None:
                pytest.skip("UDP discovery is not available in this environment")
            responders.append(responder)

        found = discovery.discover(timeout=2.0, port=port)
        by_name = {node.get("instance_name"): node for node in found}
        assert "ASI_testnode" in by_name, "the first instance answered"
        assert "ASI_testnode-2" in by_name, (
            "the second instance answered too — this is the multicast fan-out "
            "that a broadcast-only probe could not do")
        assert by_name["ASI_testnode"]["http_port"] != \
               by_name["ASI_testnode-2"]["http_port"]
    finally:
        for responder in responders:
            responder.stop()


def test_replies_are_keyed_by_host_and_port_so_ports_keep_them_apart():
    port = _free_port()
    responder = discovery.start_responder(
        lambda: {"instance_name": "solo", "camera_type": "asi",
                 "http_port": 8765, "node_name": "testnode"},
        port=port)
    if responder is None:
        pytest.skip("UDP discovery is not available in this environment")
    try:
        found = discovery.discover(timeout=1.5, port=port)
        solo = [n for n in found if n.get("instance_name") == "solo"]
        # The probe goes out on several targets; one node must still be one row.
        assert len(solo) == 1
        assert solo[0]["host"]           # filled in from the reply's source address
    finally:
        responder.stop()
