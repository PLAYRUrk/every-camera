"""Every camera forwards requests to the others: reach one and you have reached the
site.

    GET /api/nodes                     everything this node knows about
    GET /api/node/<host>:<port>/<path> that request, made on your behalf

Cameras announce themselves (``discovery.py``) and remember each other (``peers.py``),
so any one of them knows them all. Whole HTTP requests are forwarded, so the archive,
live stream, parameters and focus all work through it.

Only nodes this one has seen are targets, or every camera would be an open proxy to
anything its network routes to. A hop counter stops two nodes passing a request back and
forth.
"""
import json
import socket
import threading
import time

from urllib.request import Request, urlopen

import peers

# Not a poll of the network: the peer registry fills itself from announcements. This
# is a status read per known node.
REFRESH_SECONDS = 20.0
# One node must never be able to hold up the view of the others.
POLL_TIMEOUT = 3.0
# A silent node stays listed as unreachable: "was there and stopped answering" is the
# whole reason anybody is watching.
STALE_SECONDS = 120.0

HOP_HEADER = "X-Everycam-Hops"
MAX_HOPS = 2
PROXY_TIMEOUT = 30.0
# A whitelist: here a request from outside chooses what a camera does next. "/" is
# listed exactly because as a prefix it would match every path.
PROXY_PREFIXES = ("/api/",)
PROXY_EXACT = ("/", "/index.html")


def forwardable(path):
    bare = (path or "").split("?", 1)[0]
    return bare in PROXY_EXACT or any(bare.startswith(prefix)
                                      for prefix in PROXY_PREFIXES)


def _target_of(node):
    return f"{node.get('host')}:{node.get('http_port')}"


class Gateway:
    def __init__(self, service=None, refresh=REFRESH_SECONDS,
                 poll_timeout=POLL_TIMEOUT):
        self.service = service
        self.refresh = float(refresh)
        self.poll_timeout = float(poll_timeout)
        self._lock = threading.Lock()
        self._seen = {}            # target -> {peer fields, status, last_ok, error}
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="everycam-gateway")
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _run(self):
        # A short first wait so a node that has only just started does not poll
        # neighbours it has not heard from yet.
        if self._stop.wait(3.0):
            return
        while True:
            try:
                self.refresh_once()
            except Exception:
                pass                # a neighbour's fault is never ours
            if self._stop.wait(self.refresh):
                return

    def refresh_once(self):
        answered = 0
        for peer in peers.load():
            target = _target_of(peer)
            record = {key: peer.get(key) for key in
                      ("host", "http_port", "node_name", "instance_name",
                       "camera_type")}
            try:
                status = self._get_json(peer.get("host"), peer.get("http_port"),
                                        "/api/status")
            except Exception as exc:
                record["error"] = str(exc)
                status = None
            else:
                record["status"] = status
                record["error"] = ""
                record["last_ok"] = time.time()
                answered += 1
            # Swapped in whole under the lock: nodes() runs on an HTTP thread and must
            # not read a half-updated record.
            with self._lock:
                previous = self._seen.get(target) or {}
                if status is None:
                    record["status"] = previous.get("status")
                    record["last_ok"] = previous.get("last_ok")
                self._seen[target] = record
        return answered

    def nodes(self, include_self=True):
        """``target`` is the ``host:port`` for an ``/api/node/<target>/...`` request.

        ``reachable`` is this node's opinion, not the observer's: a camera the gateway
        sees may be unreachable from the caller, which is what makes forwarding worth
        having.
        """
        listed = []
        mine = self._self_target()
        if include_self and self.service is not None:
            listed.append(self._describe_self())
        now = time.time()
        with self._lock:
            records = [dict(record) for record in self._seen.values()]
        for record in records:
            if mine and _target_of(record) == mine:
                # Cameras on one machine share peers.json, so a neighbour wrote this
                # node's own entry there. Listing it would show the same camera twice.
                continue
            last_ok = float(record.get("last_ok") or 0)
            record["target"] = _target_of(record)
            record["reachable"] = bool(last_ok) and (now - last_ok) < STALE_SECONDS
            record["seen_ago"] = round(now - last_ok, 1) if last_ok else None
            record["self"] = False
            listed.append(record)
        return listed

    def _self_target(self):
        port = getattr(self.service, "http_port", None)
        if not port:
            return ""
        return f"{_local_address()}:{port}"

    def _describe_self(self):
        info = {}
        try:
            info = dict(self.service.info() or {})
        except Exception:
            pass
        status = {}
        try:
            status = dict(self.service.status() or {})
        except Exception:
            pass
        return {
            "self": True,
            "target": "",
            "reachable": True,
            "host": _local_address(),
            "http_port": getattr(self.service, "http_port", None),
            "node_name": info.get("node_name"),
            "instance_name": info.get("instance_name"),
            "camera_type": info.get("camera_type"),
            "status": status,
            "error": "",
            "seen_ago": 0.0,
        }

    def known_target(self, target):
        """The gate on forwarding: without it every camera would forward anything
        anywhere, on a port that has no authentication by design.
        """
        wanted = str(target or "").strip()
        with self._lock:
            if wanted in self._seen:
                return dict(self._seen[wanted])
        for peer in peers.load():
            if _target_of(peer) == wanted:
                return dict(peer)
        return None

    def open_upstream(self, target, path, method="GET", body=None,
                      content_type=None, hops=0, timeout=PROXY_TIMEOUT):
        """Returns the open response, not its bytes: ``/api/live.mjpg`` never ends."""
        peer = self.known_target(target)
        if peer is None:
            raise LookupError(f"unknown node: {target}")
        if hops >= MAX_HOPS:
            raise RecursionError("too many hops")
        if not forwardable(path):
            raise ValueError(f"path not forwarded: {path}")

        url = f"http://{peer['host']}:{peer['http_port']}{path}"
        request = Request(url, data=body, method=method)
        request.add_header(HOP_HEADER, str(hops + 1))
        if content_type:
            request.add_header("Content-Type", content_type)
        return urlopen(request, timeout=timeout)

    def _get_json(self, host, port, path):
        url = f"http://{host}:{port}{path}"
        request = Request(url, method="GET")
        request.add_header(HOP_HEADER, "1")
        with urlopen(request, timeout=self.poll_timeout) as response:
            raw = response.read(1_000_000)
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}


def _local_address():
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(0.3)
        probe.connect(("8.8.8.8", 80))
        address = probe.getsockname()[0]
        probe.close()
        return address
    except OSError:
        return "127.0.0.1"


def parse_node_path(path):
    """``rest`` always starts with a slash, so a bare ``/api/node/host:8765`` is the
    index.
    """
    prefix = "/api/node/"
    if not path.startswith(prefix):
        return None, None
    remainder = path[len(prefix):]
    if not remainder:
        return None, None
    target, slash, rest = remainder.partition("/")
    return target, ("/" + rest if slash else "/")
