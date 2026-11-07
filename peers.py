"""Cameras seen before, remembered between runs, so discovery is not the only way.

UDP discovery stops at the first switch, VLAN boundary or wireless bridge that drops
broadcast. So every node and observer writes down what it has seen, and on the next run
asks those addresses by unicast too.

One file per peer: several cameras on a machine share this directory and write whenever
a neighbour announces itself, and a single JSON file would lose entries to concurrent
read-modify-write. This needs no lock and works the same on Linux and Windows.

Safe to delete: losing it costs one round of broadcast discovery.

    ~/.every_camera/peers/192.168.2.36_8765.json
"""
import json
import os
import time

from pathlib import Path

PEERS_DIR = str(Path.home() / ".every_camera" / "peers")
# Peers older than this are forgotten. Long enough for a fortnight's maintenance,
# short enough that a decommissioned station does not haunt the list.
MAX_AGE_SECONDS = 30 * 24 * 3600
# A ceiling on what ``load`` hands out and ``save`` writes.
MAX_PEERS = 200

# The rest of a discovery reply is about that moment and would go stale on disk.
_FIELDS = ("host", "http_port", "node_name", "instance_name", "camera_type")

# A scratch file this old belongs to a process killed between writing and renaming
# it. Generous, so a slow disk mid-write is never mistaken for one.
_TMP_MAX_AGE_SECONDS = 300


def _key(peer):
    """Two instances on one machine differ by port, so both are remembered."""
    return f"{peer.get('host')}:{peer.get('http_port')}"


def _filename(peer):
    """Survives IPv6 colons and anything else in a host."""
    safe = "".join(ch if (ch.isalnum() or ch in "-._") else "_" for ch in _key(peer))
    return f"{safe}.json"


def load(path=None):
    """Freshest first. Never raises."""
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
    try:
        os.makedirs(directory, exist_ok=True)
        target = os.path.join(directory, _filename(peer))
        # The pid: two processes writing one peer must not share a scratch file.
        tmp = f"{target}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(peer, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
        return True
    except OSError:
        # Windows refuses to replace a file another process has open. Losing one update
        # is fine, the peer announces itself within the minute; leaving the scratch file
        # is not.
        try:
            os.remove(tmp)
        except (OSError, UnboundLocalError, NameError):
            pass
        return False


def save(peers, path=None):
    """Replaces the whole registry. Ordinary code calls :func:`remember`, which cannot
    lose somebody else's entry.
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
    """Returns the registry as it now stands. Whoever has once seen a camera can reach
    it again without discovery working at all.
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
    """Deduplicated by host: the probe is per address, so a machine running three
    cameras is asked once and answers three times.
    """
    skip = {str(host) for host in exclude}
    hosts = []
    for peer in load(path):
        host = str(peer.get("host") or "")
        if host and host not in skip and host not in hosts:
            hosts.append(host)
    return hosts


def forget(host, http_port=None, path=None):
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
