"""One address, the whole fleet: every camera is also a way in to the others.

This is what replaces the broker. The problem a broker solved was never
publish/subscribe — it was that an observer had to be able to reach a camera,
and often could not. A relay in the middle answered that by making every camera
dial *out* to it, at the price of an external service, six settings, a payload
ceiling, and a channel that could carry a status and a single JPEG and nothing
else: no archive, no live stream, no focusing.

The cheaper answer is that the cameras can already reach each other. They are on
one segment, they announce themselves (``discovery.py``) and they remember each
other (``peers.py``), so any one of them knows where all of them are. Making
each one willing to forward a request turns that knowledge into access: reach a
single camera and you have reached the site.

    GET /api/nodes                     everything this node knows about
    GET /api/node/<host>:<port>/<path> that request, made on your behalf

And because it forwards whole HTTP requests rather than a fixed set of messages,
what comes back through it is the real thing: the archive, ``/api/live.mjpg``,
``/api/params``, ``/api/focus``. The relay was never able to carry any of those.

Two rules keep it from being a liability:

* **Only known peers.** A forwarding target has to be a node this one has
  actually seen. Otherwise every camera on the site would be an open proxy to
  anything its network can route to.
* **No forwarding a forward.** A hop counter, checked on the way in and set on
  the way out. Two nodes that know each other would otherwise be able to pass a
  request back and forth until something ran out.
"""
import json
import socket
import threading
import time

from urllib.request import Request, urlopen

import peers

# How often the fleet view is refreshed. Not a poll of the network — the peer
# registry fills itself from announcements — just a status read per known node.
REFRESH_SECONDS = 20.0
# One node must never be able to hold up the view of the others.
POLL_TIMEOUT = 3.0
# A node that has not answered for this long is still listed, and said to be
# unreachable. Dropping it would be the one thing a monitor must not do: "was
# there and stopped answering" is the whole reason anybody is watching.
STALE_SECONDS = 120.0

# Header carrying how many nodes a request has already been through.
HOP_HEADER = "X-Everycam-Hops"
MAX_HOPS = 2
PROXY_TIMEOUT = 30.0
# Paths worth forwarding. A whitelist rather than a blacklist: this is the one
# place where a request from outside chooses what a camera does next. "/" is
# listed exactly, not as a prefix — as a prefix it matches every path there
# is, which would have made the whole list decorative.
PROXY_PREFIXES = ("/api/",)
PROXY_EXACT = ("/", "/index.html")


def forwardable(path):
    """True if ``path`` is one this node is willing to fetch for somebody."""
    bare = (path or "").split("?", 1)[0]
    return bare in PROXY_EXACT or any(bare.startswith(prefix)
                                      for prefix in PROXY_PREFIXES)


def _target_of(node):
    return f"{node.get('host')}:{node.get('http_port')}"


class Gateway:
    """This node's view of every other node, and the door through to them."""

    def __init__(self, service=None, refresh=REFRESH_SECONDS,
                 poll_timeout=POLL_TIMEOUT):
        self.service = service
        self.refresh = float(refresh)
        self.poll_timeout = float(poll_timeout)
        self._lock = threading.Lock()
        self._seen = {}            # target -> {peer fields, status, last_ok, error}
        self._stop = threading.Event()
        self._thread = None

    # -- lifecycle -----------------------------------------------------------
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

    # -- the fleet view ------------------------------------------------------
    def refresh_once(self):
        """Read the status of every remembered peer. Returns how many answered."""
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
            # The whole record is swapped in under the lock rather than
            # edited in place: nodes() runs on an HTTP thread and would
            # otherwise be able to read one half-updated.
            with self._lock:
                previous = self._seen.get(target) or {}
                if status is None:
                    record["status"] = previous.get("status")
                    record["last_ok"] = previous.get("last_ok")
                self._seen[target] = record
        return answered

    def nodes(self, include_self=True):
        """Everything this node knows, itself first.

        Each entry carries ``target`` — the ``host:port`` to put in a
        ``/api/node/<target>/...`` request — and ``reachable``, which is this
        node's opinion and not the observer's: a camera the gateway can see may
        still be unreachable from wherever the question came from, which is
        exactly the situation that makes forwarding worth having.
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
                # Cameras on one machine share peers.json, so this node's own
                # entry is in it — written there by its neighbour, which had
                # no way to know it was us. Listing it again would put the
                # same camera on the monitor twice.
                continue
            last_ok = float(record.get("last_ok") or 0)
            record["target"] = _target_of(record)
            record["reachable"] = bool(last_ok) and (now - last_ok) < STALE_SECONDS
            record["seen_ago"] = round(now - last_ok, 1) if last_ok else None
            record["self"] = False
            listed.append(record)
        return listed

    def _self_target(self):
        """This node's own ``host:port``, or "" if the port is not known yet."""
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

    # -- forwarding ----------------------------------------------------------
    def known_target(self, target):
        """The peer behind ``host:port``, or None if this node has not seen it.

        The gate on forwarding. Without it every camera would forward anything
        anywhere its network can route to, which is a proxy nobody asked for on
        a port that has no authentication by design.
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
        """Make the request on the caller's behalf and hand back the live response.

        Returns the open response object rather than its bytes, so that the
        caller can stream it — ``/api/live.mjpg`` never ends, and buffering it
        would mean holding a night of video in memory.
        """
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

    # -- helpers -------------------------------------------------------------
    def _get_json(self, host, port, path):
        url = f"http://{host}:{port}{path}"
        request = Request(url, method="GET")
        request.add_header(HOP_HEADER, "1")
        with urlopen(request, timeout=self.poll_timeout) as response:
            raw = response.read(1_000_000)
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}


def _local_address():
    """This machine's address as the network sees it."""
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
    """Split ``/api/node/<target>/<rest>`` into ``(target, rest)``.

    ``rest`` always starts with a slash, so ``/api/node/host:8765`` and
    ``/api/node/host:8765/`` both mean the node's own index page.
    """
    prefix = "/api/node/"
    if not path.startswith(prefix):
        return None, None
    remainder = path[len(prefix):]
    if not remainder:
        return None, None
    target, slash, rest = remainder.partition("/")
    return target, ("/" + rest if slash else "/")
