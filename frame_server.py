"""
Local HTTP frame server — browse captured frames and watch a live view over the LAN.

Runs inside the camera process on daemon threads, next to the measurement
worker. It is wired so that it can never interfere with measurements:

  * Started through :func:`start_frame_server`, which swallows every startup
    error — a busy port or a missing permission logs a warning and the camera
    keeps working.
  * Handler threads never touch the camera object. Live data comes from
    :class:`camera_service.CameraService`; archive data comes from the file
    system. If the worker hangs, the archive endpoints still answer.
  * ``ThreadingHTTPServer`` with ``daemon_threads``, a cap on simultaneous
    MJPEG streams, and a cap on listing sizes, so a careless client cannot
    exhaust the machine.

Access is unauthenticated by design (the operator asked for an open server):
run it on a trusted network, not on a public interface.

Endpoints
---------
    GET  /                       built-in HTML viewer
    GET  /api/info               instance name, camera type, capabilities
    GET  /api/status             worker status snapshot
    GET  /api/dates              days present in the archive
    GET  /api/frames             archive listing (date/limit/offset)
    GET  /api/composite          per-filter difference frame from three frames
    GET  /api/frame              one archived frame (as=jpeg|raw)
    GET  /api/thumb              small JPEG preview of an archived frame
    GET  /api/stats              statistics + histogram of a frame
    GET  /api/latest.jpg         newest live frame
    GET  /api/live.mjpg          live MJPEG stream (focus_app)
    GET  /api/params             current parameters + editable schema
    POST /api/params             request a parameter change
    GET  /api/params/<id>        result of a parameter request
    POST /api/focus              start/extend/stop focus mode
"""
import errno
import json
import os
import socket
import threading
import time

from datetime import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import console_ui

import cycles
import frame_archive
import frame_grouping
import intensity

SERVER_VERSION = "every-camera/1.0"

DEFAULT_PORT = 8765
# How many ports above the configured one to try when it is already taken.
# Several cameras are meant to run from one identical config.json, so a busy
# port is a normal situation, not a misconfiguration.
DEFAULT_PORT_SEARCH = 20
DEFAULT_LIST_LIMIT = 200
MAX_LIST_LIMIT = 2000
MAX_MJPEG_CLIENTS = 4
MJPEG_BOUNDARY = "everycamframe"
THUMB_MAX_SIDE = 256
THUMB_CACHE_SIZE = 64


class _FrameHTTPServer(ThreadingHTTPServer):
    """The server class, with SO_REUSEPORT explicitly off.

    SO_REUSEADDR (inherited) only shortens TIME_WAIT. SO_REUSEPORT would let a
    second instance bind the *same* port and have the kernel split requests
    between two unrelated cameras at random — the opposite of what the port
    search below is for, so it must stay off however the stdlib default moves.
    """
    allow_reuse_port = False
    daemon_threads = True


class _ThumbCache:
    """Tiny mtime-aware cache so scrolling a listing does not re-decode FITS."""

    def __init__(self, size=THUMB_CACHE_SIZE):
        self._size = size
        self._lock = threading.Lock()
        self._data = {}
        self._order = []

    def get(self, key):
        with self._lock:
            return self._data.get(key)

    def put(self, key, value):
        with self._lock:
            if key not in self._data:
                self._order.append(key)
                while len(self._order) > self._size:
                    self._data.pop(self._order.pop(0), None)
            self._data[key] = value


class FrameServer:
    """Handle returned by :func:`start_frame_server`."""

    def __init__(self, httpd, thread, responder, port):
        self._httpd = httpd
        self._thread = thread
        self._responder = responder
        self.port = port

    @property
    def url(self):
        return f"http://{_local_ip()}:{self.port}/"

    def stop(self):
        if self._responder is not None:
            try:
                self._responder.stop()
            except Exception:
                pass
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=5)


def _local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    server_version = SERVER_VERSION
    protocol_version = "HTTP/1.1"

    # Injected by start_frame_server
    service = None
    max_list = MAX_LIST_LIMIT
    focus_ttl = 60.0
    stream_slots = None
    thumb_cache = None
    verbose = False

    # -- plumbing ------------------------------------------------------
    def log_message(self, fmt, *args):
        if self.verbose:
            console_ui.log(f"{self.address_string()} - {fmt % args}")

    def _send(self, code, body=b"", content_type="application/octet-stream",
              extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, obj, code=200):
        self._send(code, frame_archive.dumps(obj), "application/json")

    def _error(self, code, message):
        self._send_json({"error": message}, code=code)

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    def _arg(self, query, name, default=None):
        values = query.get(name)
        return values[0] if values else default

    def _float_arg(self, query, name, default):
        try:
            return float(self._arg(query, name, default))
        except (TypeError, ValueError):
            return float(default)

    def _int_arg(self, query, name, default, minimum=None, maximum=None):
        try:
            value = int(self._arg(query, name, default))
        except (TypeError, ValueError):
            value = default
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > 1_000_000:
            return {}
        try:
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # -- routing -------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            self._route_get(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self._error(500, f"{type(exc).__name__}: {exc}")

    do_HEAD = do_GET

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/params":
                self._post_params()
            elif path == "/api/focus":
                self._post_focus()
            else:
                self._error(404, f"Unknown endpoint: {path}")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self._error(500, f"{type(exc).__name__}: {exc}")

    def _route_get(self, path):
        if path in ("/", "/index.html"):
            self._send(200, _INDEX_HTML, "text/html; charset=utf-8")
        elif path == "/api/info":
            self._get_info()
        elif path == "/api/status":
            self._send_json(self.service.status())
        elif path == "/api/dates":
            self._send_json({"dates": frame_archive.list_dates(self.service.output_dir)})
        elif path == "/api/frames":
            self._get_frames()
        elif path == "/api/composite":
            self._get_composite()
        elif path == "/api/frame":
            self._get_frame()
        elif path == "/api/thumb":
            self._get_thumb()
        elif path == "/api/stats":
            self._get_stats()
        elif path in ("/api/latest.jpg", "/api/latest"):
            self._get_latest()
        elif path in ("/api/live.mjpg", "/api/live"):
            self._get_live()
        elif path == "/api/params":
            self._send_json(self.service.params())
        elif path.startswith("/api/params/"):
            result = self.service.param_result(path.rsplit("/", 1)[-1])
            self._send_json(result or {"error": "unknown request id"},
                            code=200 if result else 404)
        else:
            self._error(404, f"Unknown endpoint: {path}")

    # -- handlers ------------------------------------------------------
    def _get_info(self):
        info = self.service.info()
        info.update({
            "server": SERVER_VERSION,
            "hostname": socket.gethostname(),
            "time": dt.now().isoformat(),
            "archive_available": bool(self.service.output_dir
                                      and os.path.isdir(self.service.output_dir)),
        })
        self._send_json(info)

    def _get_frames(self):
        query = self._query()
        limit = self._int_arg(query, "limit", DEFAULT_LIST_LIMIT, 1, self.max_list)
        offset = self._int_arg(query, "offset", 0, 0)
        listing = frame_archive.list_frames(
            self.service.output_dir, date=self._arg(query, "date"),
            limit=limit, offset=offset)
        listing["output_dir"] = self.service.output_dir
        self._send_json(listing)

    # -- the per-filter difference frame -------------------------------
    def _get_composite(self):
        """Combine three named frames into one difference frame, as a JPEG.

        The caller names the frames, because the caller is the thing holding the
        tree: ``frame_grouping`` decided which cycle and filter each frame
        belongs to, and re-deriving that here — off a period the operator may
        have overridden in the viewer — would let the picture disagree with the
        branch that asked for it.

        What is left here is what has to be here: the pixels. Three japan frames
        are ~24 MB of raw data against ~200 KB for the answer, and
        ``frame_archive`` is already the one place that turns sensor counts into
        a picture.
        """
        query = self._query()
        names = [self._arg(query, key)
                 for key in ("first", "previous", "last")]
        if not all(names):
            self._error(400, "need 'first', 'previous' and 'last' frame names")
            return

        window = self._float_arg(query, "window", 0.0)
        if window <= 0:
            self._error(400, "'window' must be a positive number of seconds")
            return
        max_side = self._int_arg(query, "max", 0, 0, 8192) or None
        stretch = self._arg(query, "stretch", "minmax")
        if stretch not in ("minmax", "percentile", "raw"):
            stretch = "minmax"

        times = []
        for name in names:
            stamp, _from_name = frame_grouping.frame_timestamp(name)
            if stamp is None:
                self._error(400, f"cannot read a capture time from {name}")
                return
            times.append(stamp)
        try:
            k0, k1 = cycles.composite_weights(times[0], times[1], times[2], window)
        except ValueError as exc:
            self._error(400, str(exc))
            return

        try:
            arrays = []
            for name in names:
                path = frame_archive.resolve_frame_path(self.service.output_dir,
                                                        name)
                if not path:
                    self._error(404, f"No such frame: {name}")
                    return
                arrays.append(frame_archive.read_frame_file(path))
            result = cycles.composite_image(*arrays, k0, k1)
            jpeg = frame_archive.to_jpeg_bytes(result, max_side=max_side,
                                               stretch=stretch)
        except Exception as exc:
            self._error(415, f"Could not build the difference frame: {exc}")
            return

        self._send(200, jpeg, "image/jpeg", extra_headers={
            "X-Composite-K0": f"{k0:.6f}",
            "X-Composite-K1": f"{k1:.6f}",
            "X-Composite-Gap0": f"{(times[0] - times[1]).total_seconds():.1f}",
            "X-Composite-Gap1": f"{(times[2] - times[0]).total_seconds():.1f}",
            "X-Composite-Window": f"{float(window):.1f}",
            "X-Composite-First": names[0],
            "X-Composite-Previous": names[1],
            "X-Composite-Last": names[2],
        })

    def _resolve_or_fail(self, query):
        name = self._arg(query, "name")
        if not name:
            self._error(400, "missing 'name' parameter")
            return None
        path = frame_archive.resolve_frame_path(self.service.output_dir, name)
        if not path:
            # Same answer for traversal attempts and genuine typos.
            self._error(404, f"No such frame: {name}")
            return None
        return path

    def _get_frame(self):
        query = self._query()
        path = self._resolve_or_fail(query)
        if not path:
            return
        if self._arg(query, "as", "jpeg") == "raw":
            self._send_file(path)
            return
        max_side = self._int_arg(query, "max", 0, 0, 8192) or None
        stretch = self._arg(query, "stretch", "minmax")
        if stretch not in ("minmax", "percentile", "raw"):
            stretch = "minmax"
        try:
            frame, full_scale = frame_archive.read_frame_with_scale(path)
            jpeg = frame_archive.to_jpeg_bytes(frame, max_side=max_side,
                                               stretch=stretch,
                                               full_scale=full_scale)
        except Exception as exc:
            self._error(415, f"Could not decode {os.path.basename(path)}: {exc}")
            return
        self._send(200, jpeg, "image/jpeg")

    def _send_file(self, path):
        """Stream the original file, so the observer can download raw data."""
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                self.send_response(200)
                self.send_header("Content-Type", frame_archive.mime_for(path))
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{os.path.basename(path)}"')
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                if self.command == "HEAD":
                    return
                while True:
                    chunk = fh.read(256 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError as exc:
            self._error(500, f"Could not read file: {exc}")

    def _get_thumb(self):
        query = self._query()
        path = self._resolve_or_fail(query)
        if not path:
            return
        try:
            key = (path, os.path.getmtime(path))
        except OSError:
            key = (path, 0)
        cached = self.thumb_cache.get(key) if self.thumb_cache else None
        if cached is None:
            try:
                frame = frame_archive.read_frame_file(path)
                cached = frame_archive.to_jpeg_bytes(frame, max_side=THUMB_MAX_SIDE,
                                                     quality=70)
            except Exception as exc:
                self._error(415, f"Could not decode {os.path.basename(path)}: {exc}")
                return
            if self.thumb_cache:
                self.thumb_cache.put(key, cached)
        self._send(200, cached, "image/jpeg")

    def _get_stats(self):
        query = self._query()
        name = self._arg(query, "name")
        if name:
            path = self._resolve_or_fail(query)
            if not path:
                return
            try:
                frame, full_scale = frame_archive.read_frame_with_scale(path)
            except Exception as exc:
                self._error(415, f"Could not decode {name}: {exc}")
                return
            meta = frame_archive.frame_metadata(path)
        else:
            frame, ts, meta, _ = self.service.latest()
            if frame is None:
                self._error(404, "No live frame available yet")
                return
            full_scale = frame_archive.FULL_SCALE
            if isinstance(frame, (bytes, bytearray)):
                try:
                    import io
                    from PIL import Image
                    with Image.open(io.BytesIO(bytes(frame))) as img:
                        frame = __import__("numpy").asarray(img)
                except Exception as exc:
                    self._error(415, f"Could not decode live frame: {exc}")
                    return
                # Canon publishes an 8-bit JPEG. Its file stays as it is, but
                # the numbers an observer reads are on the one scale the rest
                # of the program uses.
                frame = intensity.rescale_to_full(frame, 255)
            meta = dict(meta or {})
            meta["timestamp"] = ts.isoformat() if ts else None
        counts, edges = frame_archive.histogram(frame, full_scale=full_scale)
        self._send_json({
            "name": name,
            "full_scale": full_scale,
            "stats": frame_archive.frame_stats(frame, full_scale=full_scale),
            "sharpness": frame_archive.sharpness(frame),
            "histogram": {"counts": counts, "edges": edges},
            "metadata": meta,
        })

    def _get_latest(self):
        query = self._query()
        max_side = self._int_arg(query, "max", 0, 0, 8192) or None
        stretch = self._arg(query, "stretch", "minmax")
        jpeg, ts, counter = self.service.latest_jpeg(max_side=max_side,
                                                     stretch=stretch)
        if jpeg is None:
            self._error(404, "No live frame available yet")
            return
        self._send(200, jpeg, "image/jpeg", extra_headers={
            "X-Frame-Timestamp": ts.isoformat() if ts else "",
            "X-Frame-Counter": str(counter),
        })

    def _get_live(self):
        """MJPEG stream of live frames, for focus_app and browsers.

        Requesting the stream also keeps focus mode alive: the camera only
        runs its free-running capture while somebody is actually watching.
        """
        if not self.stream_slots.acquire(blocking=False):
            self._error(503, f"Too many live streams (max {MAX_MJPEG_CLIENTS})")
            return
        query = self._query()
        max_side = self._int_arg(query, "max", 0, 0, 8192) or None
        stretch = self._arg(query, "stretch", "minmax")
        try:
            fps = float(self._arg(query, "fps", "10"))
        except (TypeError, ValueError):
            fps = 10.0
        min_period = 1.0 / max(0.5, min(fps, 30.0))

        try:
            self.send_response(200)
            self.send_header("Content-Type",
                             f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

            last_counter = -1
            while True:
                self.service.request_focus(self.focus_ttl)
                frame, ts, _meta, counter = self.service.wait_for_frame(
                    last_counter, timeout=5.0)
                if frame is None:
                    # No new frame yet — resend the last one so the viewer
                    # keeps a picture and the connection stays alive.
                    jpeg, ts, counter = self.service.latest_jpeg(
                        max_side=max_side, stretch=stretch)
                    if jpeg is None:
                        time.sleep(0.5)
                        continue
                else:
                    last_counter = counter
                    jpeg, ts, counter = self.service.latest_jpeg(
                        max_side=max_side, stretch=stretch)
                    if jpeg is None:
                        continue
                header = (f"--{MJPEG_BOUNDARY}\r\n"
                          f"Content-Type: image/jpeg\r\n"
                          f"Content-Length: {len(jpeg)}\r\n"
                          f"X-Frame-Timestamp: {ts.isoformat() if ts else ''}\r\n\r\n")
                self.wfile.write(header.encode("ascii"))
                self.wfile.write(jpeg)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                time.sleep(min_period)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.stream_slots.release()

    def _post_params(self):
        params = self._read_json_body()
        if not params:
            self._error(400, "expected a JSON object with parameters")
            return
        req_id = self.service.request_params(params)
        if req_id is None:
            self._error(409, "This camera does not accept remote parameters")
            return
        # A parameter change is applied by the worker between frames, which
        # only happens while focus mode is on — keep it alive here too.
        self.service.request_focus(self.focus_ttl)
        self._send_json({"id": req_id, "accepted": True, "params": params})

    def _post_focus(self):
        body = self._read_json_body()
        if body.get("enabled") is False or body.get("stop"):
            self._send_json(self.service.stop_focus())
            return
        ttl = body.get("ttl", self.focus_ttl)
        # Absent means "leave the hold alone" — see CameraService.request_focus.
        hold = body.get("hold")
        if hold is not None:
            hold = bool(hold)
        self._send_json(self.service.request_focus(ttl, hold=hold))


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
def start_frame_server(server_cfg, service, verbose=False):
    """Start the frame server for ``service``. Returns a handle or None.

    Every failure mode — disabled in config, port in use, no permission — logs
    a warning and returns None. Callers do not need a try/except: losing the
    viewer must never stop a measurement run.
    """
    cfg = server_cfg or {}
    if not cfg.get("enabled", True):
        console_ui.log("LAN frame server disabled in config.")
        return None

    wanted_port = int(cfg.get("port", DEFAULT_PORT))
    bind = cfg.get("bind", "0.0.0.0")
    # 0 restores the old strict behaviour: use the configured port or nothing.
    port_search = max(0, int(cfg.get("port_search", DEFAULT_PORT_SEARCH)))

    handler = type("_BoundHandler", (_Handler,), {
        "service": service,
        "max_list": int(cfg.get("max_list", MAX_LIST_LIMIT)),
        "focus_ttl": float(cfg.get("focus_ttl", 60)),
        "stream_slots": threading.BoundedSemaphore(MAX_MJPEG_CLIENTS),
        "thumb_cache": _ThumbCache(),
        "verbose": bool(verbose or cfg.get("verbose")),
    })

    httpd = None
    port = wanted_port
    last_exc = None
    for candidate in range(wanted_port, wanted_port + port_search + 1):
        try:
            httpd = _FrameHTTPServer((bind, candidate), handler)
            port = candidate
            break
        except OSError as exc:
            last_exc = exc
            if exc.errno not in (errno.EADDRINUSE, errno.EACCES):
                break           # a wrong bind address will not fix itself
    if httpd is None:
        console_ui.warn(f"LAN frame server not started on {bind}:{wanted_port}"
                        f"{f'..{wanted_port + port_search}' if port_search else ''}: "
                        f"{last_exc}\n"
                        f"       Measurements continue; viewer_app/focus_app cannot "
                        f"connect to this node.")
        return None
    if port != wanted_port:
        console_ui.log(f"Port {wanted_port} is busy — this instance took {port}. "
                       f"config.json is unchanged; the port is chosen per run.")

    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.5},
                              daemon=True, name="everycam-frame-server")
    thread.start()

    responder = None
    if cfg.get("discovery", True):
        import discovery

        def _info():
            info = service.info()
            info["http_port"] = port
            info["hostname"] = socket.gethostname()
            # Several instances can share a hostname; the pid tells them apart
            # in the observer's list even before the ports are noticed.
            info["pid"] = os.getpid()
            # node_name comes from the service (config); the hostname is the
            # fallback the observer's programs show when no name was configured.
            if not info.get("node_name"):
                info["node_name"] = info["hostname"]
            return info

        responder = discovery.start_responder(
            _info, cfg.get("discovery_port", discovery.DISCOVERY_PORT))

    console_ui.log(f"LAN frame server: http://{_local_ip()}:{port}/  "
                   f"(archive: {service.output_dir or 'none'})")
    return FrameServer(httpd, thread, responder, port)


# ---------------------------------------------------------------------------
# Built-in browser page
# ---------------------------------------------------------------------------
_INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Every Camera — frames</title>
<style>
 :root { color-scheme: light dark; }
 body { font-family: system-ui, sans-serif; margin: 0; display: flex;
        height: 100vh; overflow: hidden; }
 #side { width: 320px; flex: none; border-right: 1px solid #8884;
         display: flex; flex-direction: column; }
 #head { padding: 10px; border-bottom: 1px solid #8884; }
 #head h1 { font-size: 15px; margin: 0 0 6px; }
 #meta { font-size: 12px; opacity: .75; }
 #controls { padding: 8px 10px; display: flex; gap: 6px; flex-wrap: wrap; }
 #list { overflow-y: auto; flex: 1; }
 .row { padding: 6px 10px; cursor: pointer; font-size: 12px;
        border-bottom: 1px solid #8882; display: flex; justify-content: space-between; }
 .row:hover { background: #8882; }
 .row.sel { background: #2d7ff955; }
 #main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
 #bar { padding: 8px 12px; border-bottom: 1px solid #8884; font-size: 13px;
        display: flex; gap: 10px; align-items: center; }
 #view { flex: 1; display: flex; align-items: center; justify-content: center;
         background: #111; min-height: 0; }
 #view img { max-width: 100%; max-height: 100%; object-fit: contain; }
 select, button { font: inherit; padding: 3px 6px; }
 a { color: inherit; }
</style></head><body>
<div id="side">
  <div id="head"><h1 id="title">Every Camera</h1><div id="meta">loading…</div></div>
  <div id="controls">
    <select id="date"><option value="">all dates</option></select>
    <button id="live">Live</button>
    <button id="reload">Reload</button>
  </div>
  <div id="list"></div>
</div>
<div id="main">
  <div id="bar"><span id="name">—</span><span id="stats"></span>
    <a id="raw" href="#" download hidden>download original</a></div>
  <div id="view"><img id="img" alt=""></div>
</div>
<script>
const $ = s => document.querySelector(s);
let frames = [], selected = null, liveTimer = null;

async function j(url) { const r = await fetch(url); if (!r.ok) throw new Error(await r.text()); return r.json(); }

async function boot() {
  try {
    const info = await j('/api/info');
    $('#title').textContent = info.instance_name || 'Every Camera';
    $('#meta').textContent = `${info.camera_type} · ${info.hostname} · ${info.output_dir || 'no archive'}`;
    const dates = (await j('/api/dates')).dates || [];
    for (const d of dates) {
      const o = document.createElement('option'); o.value = o.textContent = d; $('#date').append(o);
    }
  } catch (e) { $('#meta').textContent = 'error: ' + e.message; }
  load();
}

async function load() {
  stopLive();
  const date = $('#date').value;
  try {
    const data = await j('/api/frames?limit=500' + (date ? '&date=' + date : ''));
    frames = data.frames || [];
    const list = $('#list'); list.innerHTML = '';
    if (!frames.length) { list.innerHTML = '<div class="row">no frames</div>'; return; }
    frames.forEach((f, i) => {
      const el = document.createElement('div');
      el.className = 'row'; el.dataset.i = i;
      el.innerHTML = `<span>${f.name}</span><span>${(f.size/1024|0)} KB</span>`;
      el.onclick = () => show(i);
      list.append(el);
    });
    show(0);
  } catch (e) { $('#list').innerHTML = '<div class="row">error: ' + e.message + '</div>'; }
}

async function show(i) {
  stopLive();
  const f = frames[i]; if (!f) return;
  selected = i;
  document.querySelectorAll('.row').forEach(r => r.classList.toggle('sel', +r.dataset.i === i));
  $('#name').textContent = f.name;
  $('#img').src = '/api/frame?as=jpeg&name=' + encodeURIComponent(f.name);
  const raw = $('#raw'); raw.href = '/api/frame?as=raw&name=' + encodeURIComponent(f.name);
  raw.hidden = false;
  try {
    const s = await j('/api/stats?name=' + encodeURIComponent(f.name));
    const st = s.stats || {};
    const scale = st.full_scale || s.full_scale || 65535;
    $('#stats').textContent = `${(st.shape||[]).join('×')} ${st.dtype||''} · min ${st.min} max ${st.max} of ${scale} · sat ${st.saturated_pct}%`;
  } catch { $('#stats').textContent = ''; }
}

function stopLive() { if (liveTimer) { clearInterval(liveTimer); liveTimer = null; $('#live').textContent = 'Live'; } }

$('#live').onclick = () => {
  if (liveTimer) return stopLive();
  $('#live').textContent = 'Stop';
  $('#name').textContent = 'live'; $('#raw').hidden = true; $('#stats').textContent = '';
  const tick = () => { $('#img').src = '/api/latest.jpg?t=' + Date.now(); };
  tick(); liveTimer = setInterval(tick, 1000);
};
$('#reload').onclick = load;
$('#date').onchange = load;
boot();
</script></body></html>
"""


# ---------------------------------------------------------------------------
# Standalone mode — serve an existing directory of captures, no camera needed
# ---------------------------------------------------------------------------
def _schedule_from_config(config_path, camera_type):
    """The observing programme from a config.json, for ``/api/info``.

    Imported here rather than at module scope: the camera config modules are
    pure Python, but this file is also the one an observer runs against a
    directory of frames, and it should not grow a package dependency for a flag
    that is usually unset.
    """
    try:
        from cameras.common.schedule import schedule_snapshot
        section = str(camera_type or "").strip().lower()
        # Read the file directly rather than through ``utils.load_config``: that
        # one writes a default config when the path is missing, which is not
        # something a read-only archive server should do to a mistyped path.
        with open(config_path) as fh:
            cfg = json.load(fh).get(section, {})
        if section == "japan":
            from cameras.japan.config import from_dict
        elif section == "asi":
            from cameras.asi.config import from_dict
        else:
            console_ui.warn(f"--config has no schedule for camera type "
                            f"{camera_type!r}; the viewer will work the "
                            f"cycle period out of the frames instead")
            return {}
        return schedule_snapshot(from_dict(cfg).schedule)
    except Exception as exc:
        console_ui.warn(f"Could not read a schedule from {config_path}: {exc}")
        return {}


def main():
    import argparse
    from camera_service import NullService

    parser = argparse.ArgumentParser(
        description="Serve a directory of captured frames over HTTP")
    parser.add_argument("--output-dir", required=True,
                        help="Directory holding the captured frames")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--name", default="archive", help="Instance name to report")
    parser.add_argument("--node-name", default="",
                        help="Friendly machine name shown to observers "
                             "(default: hostname)")
    parser.add_argument("--camera-type", default="unknown")
    parser.add_argument("--config", default=None,
                        help="config.json to read the schedule from, so the "
                             "viewer can cut this archive into cycles on the "
                             "real period instead of guessing it")
    parser.add_argument("--no-discovery", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    service = NullService(args.output_dir, args.name, args.camera_type,
                          node_name=args.node_name)
    # An archive served without its camera still groups into cycles on the
    # right period, as long as the schedule that produced it is at hand.
    # Without it the viewer falls back to working the period out of the frames.
    if args.config:
        service.set_schedule(_schedule_from_config(args.config,
                                                   args.camera_type))
    server = start_frame_server({
        "enabled": True, "port": args.port, "bind": args.bind,
        "discovery": not args.no_discovery,
    }, service, verbose=args.verbose)
    if server is None:
        raise SystemExit(1)
    print("Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
        server.stop()


if __name__ == "__main__":
    main()
