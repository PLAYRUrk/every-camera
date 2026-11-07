"""LAN discovery over UDP. A convenience: typing ``host:port`` by hand always works.

    probe  -> multicast + broadcast, payload  b"EVERYCAM_DISCOVER?"
    reply  -> unicast,               payload  {"service": "every-camera", ...}
    hello  -> multicast + broadcast, payload  b"EVERYCAM_HELLO {...}"

Probes go out twice: a broadcast to a shared port reaches exactly *one* socket, while
multicast is copied to every socket in the group, so only it finds several instances
per host. Broadcast stays because switches without an IGMP querier drop multicast,
and older nodes answer nothing else.

Hellos reach every camera's responder and land in its peer registry (``peers.py``):
a camera started later turns up by itself, and one behind a switch that drops
broadcast stays reachable by unicast once it has been seen.

The responder swallows every error: a firewall blocking UDP or a port in use must
never disturb measurements.
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
# Known within the minute, at one datagram per camera per minute.
ANNOUNCE_EVERY = 60.0
SERVICE_TAG = "every-camera"
# A description with its parameter list and schedule runs past a few KB; UDP carries
# up to about 64 KB.
MAX_REPLY_BYTES = 60000
_RECV_BYTES = 65535


class DiscoveryResponder(threading.Thread):
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
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Several responders per machine need both: SO_REUSEPORT to bind the port,
            # and the multicast membership below to give each of them a copy.
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            except OSError:
                pass
            # Announcements go out on this socket too. TTL 1: they stay on this segment.
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
            # Broadcast still works, but on a host with several instances only one is
            # found.
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
                data, addr = self._sock.recvfrom(_RECV_BYTES)
            except socket.timeout:
                continue
            except OSError:
                # Windows reports an ICMP "port unreachable" from an earlier reply
                # here; only a stop ends the listener.
                if self._stop.is_set():
                    break
                self._stop.wait(0.2)
                continue
            if data.startswith(ANNOUNCE):
                self._note_announcement(data[len(ANNOUNCE):], addr)
                continue
            if not data.startswith(PROBE):
                continue
            payload = self._describe()
            if payload and len(payload) > MAX_REPLY_BYTES:
                self._warn_too_large(len(payload))
                continue
            try:
                if payload:
                    self._sock.sendto(payload, addr)
            except Exception:
                continue

    def _warn_too_large(self, size):
        if getattr(self, "_warned_size", False):
            return
        self._warned_size = True
        console_ui.warn(f"Discovery: this camera's description is {size} bytes, over "
                        f"the {MAX_REPLY_BYTES}-byte limit — it is not announced")

    def _describe(self):
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
        """The address comes from the packet, not the sender: a node behind NAT does not
        know how it is reached.

        Its own announcement returns through multicast loopback and is dropped by the
        pid check.
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
        # Short first wait, so a camera just started is known within seconds.
        if self._stop.wait(2.0):
            return
        while True:
            payload = self._describe()
            if payload and len(payload) > MAX_REPLY_BYTES:
                self._warn_too_large(len(payload))
            elif payload:
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
    try:
        responder = DiscoveryResponder(info_provider, port, group)
        return responder if responder.start_safely() else None
    except Exception as exc:
        console_ui.warn(f"Could not start discovery responder: {exc}")
        return None


def _broadcast_addresses():
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
    """Returns dicts with at least ``host``, ``http_port``, ``instance_name`` and
    ``camera_type``, unique by ``host:http_port``, so two instances on a machine both
    show.

    With ``use_peers`` addresses seen on earlier runs are probed by unicast too, which
    is how a camera behind a switch that drops broadcast stays findable once seen.
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
                data, addr = sock.recvfrom(_RECV_BYTES)
            except socket.timeout:
                continue
            except OSError:
                # Windows: an unreachable probe target surfaces here (WSAECONNRESET).
                continue
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
