"""
HTTP client and Qt helpers shared by viewer_app.py and focus_app.py.

Uses only the standard library for networking, so the observer's machine needs
nothing beyond PyQt5 — no camera SDKs, no ``requests``.
"""
import json
import os
import socket
import threading

from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

from PyQt5.QtCore import QThread, pyqtSignal

DEFAULT_TIMEOUT = 10.0
SETTINGS_FILE_NAME = "viewer.json"


class CameraHttpError(Exception):
    """Any failure talking to a camera's frame server."""


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------
def node_name_of(info):
    """Friendly machine name from a discovery reply or ``/api/info``.

    Falls back through hostname to the bare address, so there is always
    something to show even for a node that was never given a name.
    """
    info = info or {}
    for key in ("node_name", "hostname"):
        value = str(info.get(key) or "").strip()
        if value:
            return value
    return str(info.get("host") or "").strip()


def node_label(node, default_port=8765):
    """One-line label for a discovered node: name, camera, address."""
    node = node or {}
    host = node.get("host", "?")
    port = node.get("http_port", default_port)
    name = node_name_of(node)
    who = (f"{name} — {node.get('instance_name', '?')}" if name
           else str(node.get("instance_name", "?")))
    return f"{who} ({node.get('camera_type', '?')}) — {host}:{port}"


class CameraClient:
    """Thin client for one camera's frame server."""

    def __init__(self, host, port=8765, timeout=DEFAULT_TIMEOUT):
        self.host = host
        self.port = int(port)
        self.timeout = timeout

    @property
    def base_url(self):
        return f"http://{self.host}:{self.port}"

    def __str__(self):
        return f"{self.host}:{self.port}"

    # ------------------------------------------------------------------
    def url_for(self, path, **params):
        query = urlencode({k: v for k, v in params.items() if v is not None})
        return f"{self.base_url}{path}" + (f"?{query}" if query else "")

    def get_bytes(self, path, timeout=None, **params):
        url = self.url_for(path, **params)
        try:
            with urlopen(url, timeout=timeout or self.timeout) as resp:
                return resp.read(), dict(resp.headers)
        except HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error", "")
            except Exception:
                pass
            raise CameraHttpError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
        except (URLError, socket.timeout, OSError) as exc:
            raise CameraHttpError(f"{self} unreachable: {exc}") from exc

    def get_json(self, path, timeout=None, **params):
        raw, _ = self.get_bytes(path, timeout=timeout, **params)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise CameraHttpError(f"Malformed response from {self}: {exc}") from exc

    def post_json(self, path, payload, timeout=None):
        body = json.dumps(payload or {}).encode("utf-8")
        req = Request(f"{self.base_url}{path}", data=body,
                      headers={"Content-Type": "application/json"},
                      method="POST")
        try:
            with urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
        except HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error", "")
            except Exception:
                pass
            raise CameraHttpError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
        except (URLError, socket.timeout, OSError, ValueError) as exc:
            raise CameraHttpError(f"{self} unreachable: {exc}") from exc

    # -- convenience ---------------------------------------------------
    def info(self):
        return self.get_json("/api/info")

    def status(self):
        return self.get_json("/api/status")

    def dates(self):
        return self.get_json("/api/dates").get("dates", [])

    def frames(self, date=None, limit=500, offset=0):
        return self.get_json("/api/frames", date=date, limit=limit, offset=offset)

    def frame_jpeg(self, name, max_side=None, stretch="minmax"):
        data, _ = self.get_bytes("/api/frame", name=name, max=max_side,
                                 stretch=stretch)
        return data

    def frame_raw(self, name, timeout=120.0):
        data, _ = self.get_bytes("/api/frame", timeout=timeout, name=name,
                                 **{"as": "raw"})
        return data

    def frame_stats(self, name=None):
        return self.get_json("/api/stats", name=name)

    def latest_jpeg(self, max_side=None, stretch="minmax"):
        data, headers = self.get_bytes("/api/latest.jpg", max=max_side,
                                       stretch=stretch)
        return data, headers.get("X-Frame-Timestamp", "")

    def params(self):
        return self.get_json("/api/params")

    def set_params(self, params):
        return self.post_json("/api/params", params)

    def param_result(self, req_id):
        return self.get_json(f"/api/params/{req_id}")

    def keep_focus(self, ttl=60):
        return self.post_json("/api/focus", {"ttl": ttl})

    def stop_focus(self):
        return self.post_json("/api/focus", {"enabled": False})


# ---------------------------------------------------------------------------
# Qt threading helpers — keep every network call off the GUI thread
# ---------------------------------------------------------------------------
class Task(QThread):
    """Run one callable in a worker thread and report the outcome."""

    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            result = self._fn()
        except CameraHttpError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.done.emit(result)


class TaskRunner:
    """Keeps references to running tasks so Qt does not collect them early."""

    def __init__(self, parent=None):
        self._parent = parent
        self._tasks = []

    def run(self, fn, on_done=None, on_failed=None):
        task = Task(fn, self._parent)
        if on_done:
            task.done.connect(on_done)
        if on_failed:
            task.failed.connect(on_failed)
        task.finished.connect(lambda: self._retire(task))
        self._tasks.append(task)
        task.start()
        return task

    def _retire(self, task):
        try:
            self._tasks.remove(task)
        except ValueError:
            pass

    def wait_all(self, ms=2000):
        for task in list(self._tasks):
            task.wait(ms)


class DiscoveryTask(QThread):
    """Broadcast for cameras on the LAN without blocking the UI."""

    found = pyqtSignal(list)

    def __init__(self, timeout=2.0, port=None, parent=None):
        super().__init__(parent)
        self._timeout = timeout
        self._port = port

    def run(self):
        try:
            import discovery
            nodes = discovery.discover(
                timeout=self._timeout,
                port=self._port or discovery.DISCOVERY_PORT)
        except Exception:
            nodes = []
        self.found.emit(nodes)


class MjpegStream(QThread):
    """Read an MJPEG stream and emit each JPEG frame.

    Reconnects on its own, so a camera restart or a brief network glitch does
    not require the user to do anything.
    """

    frame = pyqtSignal(bytes, str)
    state = pyqtSignal(str)

    def __init__(self, client, max_side=None, stretch="minmax", fps=10,
                 parent=None):
        super().__init__(parent)
        self._client = client
        self._max_side = max_side
        self._stretch = stretch
        self._fps = fps
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                self._read_stream()
            except Exception as exc:
                if self._stop.is_set():
                    break
                self.state.emit(f"reconnecting: {exc}")
                self._stop.wait(2.0)

    def _read_stream(self):
        url = self._client.url_for("/api/live.mjpg", max=self._max_side,
                                   stretch=self._stretch, fps=self._fps)
        self.state.emit("connecting")
        with urlopen(url, timeout=15.0) as resp:
            self.state.emit("streaming")
            buf = b""
            while not self._stop.is_set():
                chunk = resp.read(16384)
                if not chunk:
                    raise CameraHttpError("stream ended")
                buf += chunk
                # Parse as many complete parts as the buffer holds.
                while True:
                    header_end = buf.find(b"\r\n\r\n")
                    if header_end < 0:
                        break
                    headers = buf[:header_end].decode("latin-1", "replace")
                    length = None
                    timestamp = ""
                    for line in headers.splitlines():
                        low = line.lower()
                        if low.startswith("content-length:"):
                            try:
                                length = int(line.split(":", 1)[1])
                            except ValueError:
                                length = None
                        elif low.startswith("x-frame-timestamp:"):
                            timestamp = line.split(":", 1)[1].strip()
                    if length is None:
                        break
                    start = header_end + 4
                    if len(buf) < start + length:
                        break
                    self.frame.emit(buf[start:start + length], timestamp)
                    buf = buf[start + length:]
                if len(buf) > 8_000_000:      # desynchronised — resync hard
                    buf = b""


# ---------------------------------------------------------------------------
# Small persistent settings for the observer tools
# ---------------------------------------------------------------------------
def settings_path():
    from pathlib import Path
    return Path.home() / ".every_camera" / SETTINGS_FILE_NAME


def load_settings():
    try:
        with open(settings_path()) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data):
    """Merge ``data`` into the settings file and replace it atomically.

    viewer_app and focus_app share one file, so a blind overwrite let whichever
    quit last erase the other's recent hosts. Re-reading first keeps keys this
    program does not know about.
    """
    path = settings_path()
    merged = load_settings()
    merged.update(data or {})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as fh:
            json.dump(merged, fh, indent=2)
        os.replace(tmp, str(path))
    except OSError:
        pass
