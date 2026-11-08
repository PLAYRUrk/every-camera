"""Local HTTP frame server: the archive and a live view over the LAN.

Runs on daemon threads inside the camera process and must never interfere with
measurements. A failed start only logs a warning. Handlers never touch the camera
object: live data comes from :class:`camera_service.CameraService`, archive data from
disk, so a hung worker leaves the archive answering. MJPEG streams and listing sizes
are capped.

The LAN listener is unauthenticated by design: run it on a trusted network. Access from
outside goes only through ``tunnel.py`` to a second listener on 127.0.0.1, where POST
needs the run's token.
"""
import errno
import hmac
import json
import os
import secrets
import select
import socket
import threading
import time
import traceback

from datetime import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.error import HTTPError

import console_ui
import gateway

import cycles
import frame_archive
import frame_grouping
import intensity
import tunnel

SERVER_VERSION = "every-camera/1.0"

DEFAULT_PORT = 8765
# Ports above the configured one to try: several cameras run from one identical
# config.json, so a busy port is normal.
DEFAULT_PORT_SEARCH = 20
DEFAULT_LIST_LIMIT = 200
MAX_LIST_LIMIT = 5000
MAX_MJPEG_CLIENTS = 4
MJPEG_BOUNDARY = "everycamframe"
THUMB_MAX_SIDE = 256
THUMB_CACHE_SIZE = 64
# A broken endpoint polled by a page would otherwise fill the log.
FAILURE_LOG_SECONDS = 60.0


class _FrameHTTPServer(ThreadingHTTPServer):
    """SO_REUSEPORT stays off: a second instance could bind the *same* port and the
    kernel would split requests between two unrelated cameras at random.
    """
    allow_reuse_port = False
    # On Windows SO_REUSEADDR lets a second socket bind a port that is already
    # listening — the same split; SO_EXCLUSIVEADDRUSE is what keeps it out.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class _ThumbCache:
    """Keyed with mtime, so scrolling a listing does not re-decode FITS."""

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
    def __init__(self, httpd, thread, responder, port, fleet=None, external=None):
        self._httpd = httpd
        self._thread = thread
        self._responder = responder
        self.port = port
        self._fleet = fleet
        self._external = external

    @property
    def url(self):
        return f"http://{_local_ip()}:{self.port}/"

    def stop(self):
        if self._external is not None:
            self._external.stop()
        if self._responder is not None:
            try:
                self._responder.stop()
            except Exception:
                pass
        if self._fleet is not None:
            try:
                self._fleet.stop()
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


class _Handler(BaseHTTPRequestHandler):
    server_version = SERVER_VERSION
    protocol_version = "HTTP/1.1"
    # A client that vanished without closing must not pin a thread, or a stream
    # slot, forever.
    timeout = 120

    # Injected by start_frame_server
    service = None
    max_list = MAX_LIST_LIMIT
    focus_ttl = 60.0
    stream_slots = None
    thumb_cache = None
    # Set only on the listener the tunnel reaches.
    control_token = None
    gateway = None
    verbose = False

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

    def _client_gone(self):
        """Nothing is written while there is no frame, so a closed socket is only
        noticed by asking: it reads as end of stream."""
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
            if not ready:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _read_json_body(self):
        """None for a body that cannot be taken: the connection is then closed, since
        its unread bytes would be parsed as the next request."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            return None
        if length <= 0:
            return {}
        if length > 1_000_000:
            self.close_connection = True
            return None
        try:
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            self._route_get(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self._fail(exc)

    do_HEAD = do_GET

    _failure_lock = threading.Lock()
    _failure_at = {}

    def _fail(self, exc):
        key = (self.command, urlparse(self.path).path, type(exc).__name__)
        now = time.monotonic()
        with self._failure_lock:
            last = _Handler._failure_at.get(key)
            log_it = last is None or now - last >= FAILURE_LOG_SECONDS
            if log_it:
                _Handler._failure_at[key] = now
        if log_it:
            where = "tunnel" if self.control_token is not None else "LAN"
            console_ui.warn(f"Frame server ({where}): 500 on {key[0]} {key[1]}\n"
                            f"{traceback.format_exc().rstrip()}")
        self._error(500, f"{type(exc).__name__}: {exc}")

    def _token_ok(self):
        if self.control_token is None:
            return True
        given = (self.headers.get(tunnel.TOKEN_HEADER)
                 or self._arg(self._query(), "token") or "")
        return hmac.compare_digest(str(given).encode("utf-8"),
                                   self.control_token.encode("utf-8"))

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if not self._token_ok():
                self._error(403, "this address is read-only without the control token")
            elif path == "/api/params":
                self._post_params()
            elif path == "/api/focus":
                self._post_focus()
            elif path.startswith("/api/node/"):
                self._proxy(path, "POST")
            else:
                self._error(404, f"Unknown endpoint: {path}")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self._fail(exc)

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
        elif path == "/api/nodes":
            self._get_nodes()
        elif path.startswith("/api/node/"):
            self._proxy(path, "GET")
        else:
            self._error(404, f"Unknown endpoint: {path}")

    def _hops(self):
        try:
            return int(self.headers.get(gateway.HOP_HEADER) or 0)
        except (TypeError, ValueError):
            return 0

    def _get_nodes(self):
        if self.gateway is None:
            self._send_json({"nodes": [], "gateway": False})
            return
        self._send_json({"nodes": self.gateway.nodes(), "gateway": True})

    def _proxy(self, path, method):
        """Streamed, not buffered: ``/api/live.mjpg`` does not end."""
        if self.gateway is None:
            self._error(503, "This node does not forward requests")
            return
        target, rest = gateway.parse_node_path(path)
        if not target:
            self._error(400, "Usage: /api/node/<host>:<port>/<path>")
            return
        query = urlparse(self.path).query
        if query:
            rest = f"{rest}?{query}"

        body = None
        if method == "POST":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > 1_000_000:
                self.close_connection = True
                self._error(413, "request body too large")
                return
            if length > 0:
                body = self.rfile.read(length)

        try:
            upstream = self.gateway.open_upstream(
                target, rest, method=method, body=body,
                content_type=self.headers.get("Content-Type"),
                hops=self._hops())
        except LookupError as exc:
            self._error(404, str(exc))
            return
        except RecursionError:
            # Two nodes that know each other could otherwise pass one request back and
            # forth until a thread pool ran out.
            self._error(508, "Request forwarded too many times")
            return
        except ValueError as exc:
            self._error(400, str(exc))
            return
        except HTTPError as exc:
            # The node answered: its status and message go through unchanged.
            upstream = exc
        except Exception as exc:
            self._error(502, f"{target}: {type(exc).__name__}: {exc}")
            return

        self._stream_upstream(upstream)

    def _stream_upstream(self, upstream):
        try:
            self.send_response(getattr(upstream, "status", None) or upstream.code)
            content_type = upstream.headers.get("Content-Type",
                                                "application/octet-stream")
            self.send_header("Content-Type", content_type)
            for name, value in upstream.headers.items():
                lower = name.lower()
                if (lower.startswith("x-composite-") or lower.startswith("x-frame-")
                        or lower == "content-disposition"):
                    self.send_header(name, value)
            length = upstream.headers.get("Content-Length")
            if length:
                self.send_header("Content-Length", length)
            else:
                # No length upstream means a stream; say so rather than let
                # HTTP/1.1 keep-alive wait for a body that never finishes.
                self.send_header("Connection", "close")
                self.close_connection = True
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command == "HEAD":
                return
            # read1: whatever has arrived, so a live stream is not held back until
            # 64 KB have piled up.
            read = getattr(upstream, "read1", None) or upstream.read
            while True:
                chunk = read(64 * 1024)
                if not chunk:
                    return
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                upstream.close()
            except Exception:
                pass

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

    def _get_composite(self):
        """The caller names the frames: re-deriving them here, off a period the operator
        may have overridden in the viewer, could disagree with the branch that asked.

        Only the pixels have to be here: ~24 MB of raw frames against ~200 KB for the
        answer.
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

    def _merge_written_header(self, meta):
        """Adds the FITS header of the file the driver just wrote: a live frame carries
        only a few facts of its own.

        ``meta["path"]`` is dropped afterwards: it describes this machine's disk, not
        the frame.
        """
        path = meta.pop("path", None)
        if not path:
            return
        base = self.service.output_dir
        try:
            if not base or os.path.commonpath(
                    [os.path.realpath(base), os.path.realpath(path)]) \
                    != os.path.realpath(base):
                return
            written = frame_archive.frame_metadata(path)
        except (OSError, ValueError):
            return
        # The live facts win: the file only fills in what the frame itself cannot say.
        for key, value in written.items():
            meta.setdefault(key, value)

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
                # Canon publishes an 8-bit JPEG; the numbers an observer reads stay on
                # the program's one scale.
                frame = intensity.rescale_to_full(frame, 255)
            meta = dict(meta or {})
            meta["timestamp"] = ts.isoformat() if ts else None
            self._merge_written_header(meta)
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
        """Requesting the stream keeps focus mode alive: the camera free-runs only while
        somebody is watching.
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
                if self._client_gone():
                    return
                self.service.request_focus(self.focus_ttl, renew=True)
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
        if params is None:
            self._error(413, "request body too large or malformed")
            return
        if not params:
            self._error(400, "expected a JSON object with parameters")
            return
        req_id = self.service.request_params(params)
        if req_id is None:
            self._error(409, "This camera does not accept remote parameters")
            return
        # The worker applies parameters between frames, only while focus mode is on, so
        # this keeps it alive too.
        self.service.request_focus(self.focus_ttl)
        self._send_json({"id": req_id, "accepted": True, "params": params})

    def _post_focus(self):
        body = self._read_json_body()
        if body is None:
            self._error(413, "request body too large or malformed")
            return
        if body.get("enabled") is False or body.get("stop"):
            self._send_json(self.service.stop_focus())
            return
        ttl = body.get("ttl", self.focus_ttl)
        # Absent means "leave the hold alone" — see CameraService.request_focus.
        hold = body.get("hold")
        if hold is not None:
            hold = bool(hold)
        self._send_json(self.service.request_focus(ttl, hold=hold))


# WSAEACCES (10013) is what Windows says for a port reserved by the system.
_PORT_TAKEN_ERRNOS = {errno.EADDRINUSE, errno.EACCES,
                      getattr(errno, "WSAEACCES", 10013),
                      getattr(errno, "WSAEADDRINUSE", 10048)}


def start_frame_server(server_cfg, service, verbose=False):
    """Returns a handle, or None with a warning on any failure: callers need no
    try/except, since losing the viewer must never stop a measurement run.
    """
    cfg = server_cfg or {}
    if not cfg.get("enabled", True):
        console_ui.log("LAN frame server disabled in config.")
        return None

    try:
        wanted_port = int(cfg.get("port", DEFAULT_PORT))
        # 0 means strict: the configured port or nothing.
        port_search = max(0, int(cfg.get("port_search", DEFAULT_PORT_SEARCH)))
        max_list = int(cfg.get("max_list", MAX_LIST_LIMIT))
        focus_ttl = float(cfg.get("focus_ttl", 60))
    except (TypeError, ValueError) as exc:
        console_ui.warn(f"LAN frame server not started: bad value in the server "
                        f"section of the config ({exc}). Measurements continue.")
        return None
    bind = cfg.get("bind", "0.0.0.0")

    # Makes a single reachable camera a way in to every camera beside it.
    fleet = gateway.Gateway(service)
    handler = type("_BoundHandler", (_Handler,), {
        "service": service,
        "gateway": fleet,
        "max_list": max_list,
        "focus_ttl": focus_ttl,
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
            if exc.errno not in _PORT_TAKEN_ERRNOS:
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

    # The gateway lists this node too, so it must know the port the search settled on.
    try:
        service.http_port = port
    except Exception:
        pass
    fleet.start()

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
            # The hostname is the fallback when no node name was configured.
            if not info.get("node_name"):
                info["node_name"] = info["hostname"]
            return info

        responder = discovery.start_responder(
            _info, cfg.get("discovery_port", discovery.DISCOVERY_PORT))

    console_ui.log(f"LAN frame server: http://{_local_ip()}:{port}/  "
                   f"(archive: {service.output_dir or 'none'})")
    external = _start_external(cfg.get("tunnel"), handler, service)
    return FrameServer(httpd, thread, responder, port, fleet, external)


class _External:
    def __init__(self, httpd, thread, link):
        self._httpd = httpd
        self._thread = thread
        self.tunnel = link

    def stop(self):
        try:
            self.tunnel.stop()
        except Exception:
            pass
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:
            pass
        self._thread.join(timeout=5)


def _start_external(tunnel_cfg, handler, service):
    """No tunnel section means off: only a loaded config carries the default."""
    if not isinstance(tunnel_cfg, dict) or not tunnel_cfg.get("enabled"):
        return None
    try:
        token = secrets.token_urlsafe(16)
        guarded = type("_TunnelHandler", (handler,), {"control_token": token})
        httpd = _FrameHTTPServer(("127.0.0.1", 0), guarded)
        thread = threading.Thread(target=httpd.serve_forever,
                                  kwargs={"poll_interval": 0.5}, daemon=True,
                                  name="everycam-tunnel-server")
        thread.start()
        try:
            service.tunnel_token = token
        except Exception:
            pass
        link = tunnel.Tunnel(httpd.server_address[1], tunnel_cfg, service).start()
        return _External(httpd, thread, link)
    except Exception as exc:
        console_ui.warn(f"Tunnel not started: {exc}. Measurements continue.")
        return None


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


def _schedule_from_config(config_path, camera_type):
    """Imports are local: this file also runs on an observer's directory of frames and
    must not grow a package dependency for a flag that is usually unset.
    """
    try:
        from cameras.common.schedule import schedule_snapshot
        section = str(camera_type or "").strip().lower()
        # Not ``utils.load_config``: it writes a default config when the path is
        # missing, which a read-only archive server must not do to a mistyped path.
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
    # With the schedule at hand an archive groups into cycles on the right period;
    # without it the viewer works the period out of the frames.
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
