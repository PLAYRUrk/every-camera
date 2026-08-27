"""Who else is out there — remembered between runs, so discovery is not the only way.

UDP discovery finds cameras beautifully right up to the first switch that drops
broadcast, and then it finds nothing at all. That is not an exotic failure: it is
what a VLAN boundary does, and what a wireless bridge does, and it is why the
program grew a second transport in the first place.

The fix is unglamorous. Every node and every observer writes down what it has
seen — address, port, who was there — and on the next run it asks those addresses
directly, by unicast, alongside the broadcast probe. A camera found once stays
findable, and no configuration was involved in either step. That is what makes
this a *replacement* for a broker rather than a smaller version of one: the
knowledge accumulates by itself.

**One file per peer**, not one file with every peer in it. Several cameras run on
one machine and share this directory, and they all write to it whenever a
neighbour announces itself. A single JSON file meant read-modify-write from
several processes at once, and that loses entries: two cameras remembering two
different neighbours a moment apart, and the second write carrying the first
one's registry as it was before it changed. It was not theoretical — the first
two nodes tried against each other lost one of the two entries within a minute.
A file per peer has no such window, needs no lock, and works the same on Linux
and Windows.

The directory is small, human-readable and safe to delete: losing it costs one
round of broadcast discovery, nothing more.

    ~/.every_camera/peers/192.168.2.36_8765.json
    {"host": "192.168.2.36", "http_port": 8765, "node_name": "TORY",
     "instance_name": "ASI_ASI", "camera_type": "asi", "last_seen": 1787817250.4}
"""
import json
import os
import time

from pathlib import Path

PEERS_DIR = str(Path.home() / ".every_camera" / "peers")
# A peer not heard from in this long is forgotten. Long enough that a camera
# taken down for a fortnight's maintenance is still remembered when it comes
# back; short enough that a decommissioned station does not haunt the list.
MAX_AGE_SECONDS = 30 * 24 * 3600
# A ceiling, so that a misconfigured network cannot grow the directory without
# bound. Applied to what is handed out, and to what is cleaned up.
MAX_PEERS = 200

# What is worth keeping about a peer. Anything else in a discovery reply is
# about that moment rather than about the peer, and would go stale on disk.
_FIELDS = ("host", "http_port", "node_name", "instance_name", "camera_type")

# A scratch file this old belongs to a process that was killed between
# writing it and renaming it. Generous, so that a slow disk mid-write is
# never mistaken for one.
_TMP_MAX_AGE_SECONDS = 300


def _key(peer):
    """Two instances on one machine differ by port, so both are remembered."""
    return f"{peer.get('host')}:{peer.get('http_port')}"


def _filename(peer):
    """A file name that survives IPv6 colons and anything else in a host."""
    safe = "".join(ch if (ch.isalnum() or ch in "-._") else "_" for ch in _key(peer))
    return f"{safe}.json"


def load(path=None):
    """Remembered peers, freshest first. Never raises."""
    directory = path or PEERS_DIR
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    now = time.time()
    found = []
    for name in names:
        if name.endswith(".tmp"):
            _sweep_tmp(directory, name, now)
            continue
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                peer = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(peer, dict) or not peer.get("host"):
            continue
        if now - float(peer.get("last_seen") or 0) >= MAX_AGE_SECONDS:
            _remove(directory, name)
            continue
        found.append(peer)
    found.sort(key=lambda p: float(p.get("last_seen") or 0), reverse=True)
    return found[:MAX_PEERS]


def _sweep_tmp(directory, name, now):
    """Drop a scratch file left behind by a process that died mid-write."""
    full = os.path.join(directory, name)
    try:
        if now - os.path.getmtime(full) > _TMP_MAX_AGE_SECONDS:
            os.remove(full)
    except OSError:
        pass


def _remove(directory, name):
    try:
        os.remove(os.path.join(directory, name))
    except OSError:
        pass


def _write(directory, peer):
    """One peer, atomically, in a file of its own. Returns True if it got there."""
    try:
        os.makedirs(directory, exist_ok=True)
        target = os.path.join(directory, _filename(peer))
        # The temporary name carries the pid: two processes writing the same
        # peer at the same moment must not share a scratch file.
        tmp = f"{target}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(peer, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
        return True
    except OSError:
        # Windows refuses to replace a file another process has open, so a
        # simultaneous write can land here. Losing one update is fine — the
        # peer announces itself again within the minute — but leaving the
        # scratch file behind is not.
        try:
            os.remove(tmp)
        except (OSError, UnboundLocalError, NameError):
            pass
        return False


def save(peers, path=None):
    """Replace the whole registry with ``peers``. Returns True if it got there.

    Used by tests and by anything that wants to prune. Ordinary code calls
    :func:`remember`, which only ever touches the peers it actually saw and so
    cannot lose somebody else's entry.
    """
    directory = path or PEERS_DIR
    wanted = {_filename(peer) for peer in peers}
    ok = True
    for peer in peers[:MAX_PEERS]:
        ok = _write(directory, peer) and ok
    try:
        for name in os.listdir(directory):
            if name.endswith(".json") and name not in wanted:
                _remove(directory, name)
    except OSError:
        pass
    return ok


def remember(nodes, path=None):
    """Note freshly seen nodes. Returns the registry as it now stands.

    Called after every discovery round and on every announcement, by cameras and
    observers alike — which is the point: whoever has once seen a camera can
    reach it again without discovery working at all.
    """
    directory = path or PEERS_DIR
    now = time.time()
    for node in (nodes or []):
        if not isinstance(node, dict) or not node.get("host"):
            continue
        entry = {field: node.get(field) for field in _FIELDS}
        entry["last_seen"] = now
        _write(directory, entry)
    return load(directory)


def addresses(path=None, exclude=()):
    """Remembered hosts, for use as ``discovery.discover(extra_hosts=...)``.

    Deduplicated by host: the probe is per address, not per camera, so a machine
    running three cameras is asked once and answers three times.
    """
    skip = {str(host) for host in exclude}
    hosts = []
    for peer in load(path):
        host = str(peer.get("host") or "")
        if host and host not in skip and host not in hosts:
            hosts.append(host)
    return hosts


def forget(host, http_port=None, path=None):
    """Drop a peer that is not coming back. Returns how many entries went."""
    directory = path or PEERS_DIR
    gone = 0
    for peer in load(directory):
        if str(peer.get("host")) != str(host):
            continue
        if http_port is not None and str(peer.get("http_port")) != str(http_port):
            continue
        _remove(directory, _filename(peer))
        gone += 1
    return gone
