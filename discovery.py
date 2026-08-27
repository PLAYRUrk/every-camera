"""
LAN discovery for every-camera nodes — a tiny UDP beacon, no dependencies.

A camera answers broadcast probes with a JSON description of itself, so
``viewer_app.py`` and ``focus_app.py`` can list the cameras on the network
instead of asking the observer to remember IP addresses. Typing ``host:port``
by hand always works too, so discovery is a convenience, never a requirement.

Protocol (deliberately trivial):
    probe  -> UDP multicast + broadcast, payload  b"EVERYCAM_DISCOVER?"
    reply  -> UDP unicast,               payload  {"service": "every-camera", ...}

The probe goes out twice on purpose. A broadcast datagram arriving at a shared
port is delivered by the kernel to exactly *one* socket, so on a machine
running two cameras only one of them was ever discoverable. A multicast
datagram is copied to every socket that joined the group — the same trick mDNS
uses — which is what makes several instances per host visible. Broadcast is
kept alongside it because switches without an IGMP querier drop multicast, and
because nodes running an older version answer nothing else.

Nodes also announce themselves unasked, every ``ANNOUNCE_EVERY`` seconds:

    hello  -> UDP multicast + broadcast, payload  b"EVERYCAM_HELLO {...}"

Nobody has to be running a probe to hear one. Every camera already keeps a
responder socket bound to this port for the life of the process, so an
announcement from one camera reaches every other camera on the segment and is
written into its peer registry (``peers.py``). Two things follow, and both are
the point: a camera started later turns up by itself instead of waiting for
somebody to search again, and — because the registry is also probed by unicast
on the next run — a camera behind a switch that drops broadcast stays
reachable once it has been seen once.

The responder runs in a daemon thread with every error swallowed: a firewall
that blocks UDP, or a port already in use, must never disturb measurements.
"""
import json
import os
import socket
import struct
import threading
import time

import console_ui
import peers

DISCOVERY_PORT = 45455
# Administratively scoped IPv4 multicast (RFC 2365), i.e. never routed off-site.
MCAST_GROUP = "239.255.42.99"
PROBE = b"EVERYCAM_DISCOVER?"
# Unsolicited "here I am", carrying the same JSON a reply would.
ANNOUNCE = b"EVERYCAM_HELLO "
# Often enough that a camera started mid-evening is known within the minute,
# rarely enough to be invisible: one datagram per camera per minute.
ANNOUNCE_EVERY = 60.0
SERVICE_TAG = "every-camera"
MAX_REPLY_BYTES = 8192


# ---------------------------------------------------------------------------
# Camera side — answer probes
# ---------------------------------------------------------------------------
class DiscoveryResponder(threading.Thread):
    """Answers ``EVERYCAM_DISCOVER?`` broadcasts with this node's details."""

    def __init__(self, info_provider, port=DISCOVERY_PORT, group=MCAST_GROUP):
        super().__init__(daemon=True, name="everycam-discovery")
        self._info_provider = info_provider
        self._port = int(port)
        self._group = group
        self._joined = False
        self._sock = None
        self._stop = threading.Event()
        self._announcer = None

    def start_safely(self):
        """Bind and start. Returns True on success, False (with a warning) otherwise."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Both options are needed for several responders per machine:
            # SO_REUSEPORT lets them all bind the port, and the multicast
            # membership below is what actually gets each of them a copy.
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            except OSError:
                pass
            # Announcements go out on this same socket, so it needs the
            # sending options too. TTL 1: an announcement is about this
            # segment and has no business being routed off it.
            for option, value in ((socket.IP_MULTICAST_TTL, 1),
                                  (socket.IP_MULTICAST_LOOP, 1)):
                try:
                    sock.setsockopt(socket.IPPROTO_IP, option, value)
                except OSError:
                    pass
            sock.settimeout(0.5)
            sock.bind(("", self._port))
            self._sock = sock
        except OSError as exc:
            console_ui.warn(f"Discovery responder disabled (UDP :{self._port}): {exc}")
            return False

        try:
            mreq = struct.pack("4sl", socket.inet_aton(self._group),
                               socket.INADDR_ANY)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            self._joined = True
        except OSError as exc:
            # Broadcast still works, so this is a degradation, not a failure —
            # but on a host with several instances only one will be found.
            console_ui.warn(f"Discovery multicast group {self._group} unavailable "
                            f"({exc}); falling back to broadcast only.")

        self.start()
        self._announcer = threading.Thread(target=self._announce_loop,
                                          daemon=True,
                                          name="everycam-announce")
        self._announcer.start()
        console_ui.log(f"Discovery responder listening on UDP :{self._port}"
                       f"{f' (+{self._group})' if self._joined else ''}")
        return True

    def run(self):
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            if data.startswith(ANNOUNCE):
                self._note_announcement(data[len(ANNOUNCE):], addr)
                continue
            if not data.startswith(PROBE):
                continue
            payload = self._describe()
            try:
                if payload and len(payload) <= MAX_REPLY_BYTES:
                    self._sock.sendto(payload, addr)
            except Exception:
                continue

    def _describe(self):
        """This node as a JSON datagram, or None if it will not say."""
        try:
            info = dict(self._info_provider() or {})
        except Exception:
            info = {}
        info["service"] = SERVICE_TAG
        try:
            return json.dumps(info).encode("utf-8")
        except Exception:
            return None

    def _note_announcement(self, payload, addr):
        """Write a neighbour into the peer registry.

        The address is taken from the packet rather than from what the sender
        claimed: a node behind NAT does not know how it is reached, and the
        one thing this must never record is an address that does not work.
        Its own announcement comes back through multicast loopback and is
        dropped by the pid check.
        """
        try:
            info = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(info, dict) or info.get("service") != SERVICE_TAG:
            return
        try:
            if int(info.get("pid") or -1) == os.getpid():
                return
        except (TypeError, ValueError):
            pass
        info["host"] = addr[0]
        try:
            peers.remember([info])
        except Exception:
            pass

    def _announce_loop(self):
        """Say "here I am" on a timer, so nobody has to ask."""
        # A short first wait, so a camera just started is known within seconds
        # rather than at the end of the first full interval.
        if self._stop.wait(2.0):
            return
        while True:
            payload = self._describe()
            if payload and len(payload) <= MAX_REPLY_BYTES:
                for target in [self._group] + _broadcast_addresses():
                    try:
                        self._sock.sendto(ANNOUNCE + payload,
                                          (target, self._port))
                    except OSError:
                        continue
            if self._stop.wait(ANNOUNCE_EVERY):
                return

    def stop(self):
        self._stop.set()
        try:
            if self._sock and self._joined:
                mreq = struct.pack("4sl", socket.inet_aton(self._group),
                                   socket.INADDR_ANY)
                self._sock.setsockopt(socket.IPPROTO_IP,
                                      socket.IP_DROP_MEMBERSHIP, mreq)
        except OSError:
            pass
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass


def start_responder(info_provider, port=DISCOVERY_PORT, group=MCAST_GROUP):
    """Start a DiscoveryResponder. Returns the responder, or None on failure."""
    try:
        responder = DiscoveryResponder(info_provider, port, group)
        return responder if responder.start_safely() else None
    except Exception as exc:
        console_ui.warn(f"Could not start discovery responder: {exc}")
        return None


# ---------------------------------------------------------------------------
# Viewer side — send probes
# ---------------------------------------------------------------------------
def _broadcast_addresses():
    """Best-effort list of broadcast targets for the probe."""
    targets = ["255.255.255.255"]
    try:
        # Derive the /24 broadcast of the interface holding the default route.
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        parts = local_ip.split(".")
        if len(parts) == 4:
            targets.append(".".join(parts[:3] + ["255"]))
    except OSError:
        pass
    return list(dict.fromkeys(targets))


def discover(timeout=1.5, port=DISCOVERY_PORT, extra_hosts=None,
             group=MCAST_GROUP, use_peers=True):
    """Probe the LAN and collect replies for ``timeout`` seconds.

    Returns a list of dicts, each carrying at least ``host``, ``http_port``,
    ``instance_name`` and ``camera_type``. Duplicates (a node answering both the
    multicast and the broadcast probe) are collapsed by ``host:http_port``;
    two instances on one machine differ by port and are both listed.

    With ``use_peers``, every address seen on a previous run is probed by
    unicast as well, and whatever answers is written back. This is what makes
    a camera behind a switch that drops broadcast findable at all: it has to
    be seen once — by broadcast, by an announcement, or by being typed in —
    and after that it is remembered.
    """
    found = {}
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        # TTL 1: the probe must not leave the local segment.
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        # Loopback on, or cameras running on this very machine stay invisible.
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        sock.settimeout(0.3)
    except OSError as exc:
        console_ui.warn(f"Discovery not available: {exc}")
        return []

    targets = [group] if group else []
    targets += _broadcast_addresses()
    for host in (extra_hosts or []):
        targets.append(host)
    if use_peers:
        try:
            targets += peers.addresses()
        except Exception:
            pass
    targets = list(dict.fromkeys(targets))

    try:
        for target in targets:
            try:
                sock.sendto(PROBE, (target, int(port)))
            except OSError:
                continue

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(MAX_REPLY_BYTES)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                info = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(info, dict) or info.get("service") != SERVICE_TAG:
                continue
            info.setdefault("host", addr[0])
            # The responder cannot know which address the client reached it on.
            info["host"] = addr[0]
            key = f"{info['host']}:{info.get('http_port')}"
            found[key] = info
    finally:
        try:
            sock.close()
        except OSError:
            pass
    nodes = list(found.values())
    if use_peers and nodes:
        try:
            peers.remember(nodes)
        except Exception:
            pass
    return nodes


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Probe the LAN for every-camera nodes")
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--port", type=int, default=DISCOVERY_PORT)
    args = parser.parse_args()
    nodes = discover(timeout=args.timeout, port=args.port)
    if not nodes:
        print("No cameras answered.")
    for node in sorted(nodes, key=lambda n: str(n.get("node_name") or
                                                n.get("hostname") or "")):
        name = node.get("node_name") or node.get("hostname") or "-"
        print(f"{name:<20} "
              f"{node.get('instance_name', '?'):<20} "
              f"{node.get('camera_type', '?'):<8} "
              f"http://{node['host']}:{node.get('http_port')}")
