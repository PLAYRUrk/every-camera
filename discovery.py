"""
LAN discovery for every-camera nodes — a tiny UDP beacon, no dependencies.

A camera answers broadcast probes with a JSON description of itself, so
``viewer_app.py`` and ``focus_app.py`` can list the cameras on the network
instead of asking the observer to remember IP addresses. Typing ``host:port``
by hand always works too, so discovery is a convenience, never a requirement.

Protocol (deliberately trivial):
    probe  -> UDP broadcast, payload  b"EVERYCAM_DISCOVER?"
    reply  -> UDP unicast,   payload  {"service": "every-camera", ...}

The responder runs in a daemon thread with every error swallowed: a firewall
that blocks UDP, or a port already in use, must never disturb measurements.
"""
import json
import socket
import threading
import time

DISCOVERY_PORT = 45455
PROBE = b"EVERYCAM_DISCOVER?"
SERVICE_TAG = "every-camera"
MAX_REPLY_BYTES = 8192


# ---------------------------------------------------------------------------
# Camera side — answer probes
# ---------------------------------------------------------------------------
class DiscoveryResponder(threading.Thread):
    """Answers ``EVERYCAM_DISCOVER?`` broadcasts with this node's details."""

    def __init__(self, info_provider, port=DISCOVERY_PORT):
        super().__init__(daemon=True, name="everycam-discovery")
        self._info_provider = info_provider
        self._port = int(port)
        self._sock = None
        self._stop = threading.Event()

    def start_safely(self):
        """Bind and start. Returns True on success, False (with a warning) otherwise."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            except OSError:
                pass
            sock.settimeout(0.5)
            sock.bind(("", self._port))
            self._sock = sock
        except OSError as exc:
            print(f"[WARN] Discovery responder disabled (UDP :{self._port}): {exc}",
                  flush=True)
            return False
        self.start()
        print(f"[INFO] Discovery responder listening on UDP :{self._port}",
              flush=True)
        return True

    def run(self):
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data.startswith(PROBE):
                continue
            try:
                info = dict(self._info_provider() or {})
            except Exception:
                info = {}
            info["service"] = SERVICE_TAG
            try:
                payload = json.dumps(info).encode("utf-8")
                if len(payload) <= MAX_REPLY_BYTES:
                    self._sock.sendto(payload, addr)
            except Exception:
                continue

    def stop(self):
        self._stop.set()
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass


def start_responder(info_provider, port=DISCOVERY_PORT):
    """Start a DiscoveryResponder. Returns the responder, or None on failure."""
    try:
        responder = DiscoveryResponder(info_provider, port)
        return responder if responder.start_safely() else None
    except Exception as exc:
        print(f"[WARN] Could not start discovery responder: {exc}", flush=True)
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


def discover(timeout=1.5, port=DISCOVERY_PORT, extra_hosts=None):
    """Broadcast a probe and collect replies for ``timeout`` seconds.

    Returns a list of dicts, each carrying at least ``host``, ``http_port``,
    ``instance_name`` and ``camera_type``. Duplicates (a node answering on
    several broadcast addresses) are collapsed by ``host:http_port``.
    """
    found = {}
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.3)
    except OSError as exc:
        print(f"[WARN] Discovery not available: {exc}")
        return []

    targets = _broadcast_addresses()
    for host in (extra_hosts or []):
        targets.append(host)

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
    return list(found.values())


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Probe the LAN for every-camera nodes")
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--port", type=int, default=DISCOVERY_PORT)
    args = parser.parse_args()
    nodes = discover(timeout=args.timeout, port=args.port)
    if not nodes:
        print("No cameras answered.")
    for node in nodes:
        print(f"{node.get('instance_name', '?'):<24} "
              f"{node.get('camera_type', '?'):<8} "
              f"http://{node['host']}:{node.get('http_port')}")
