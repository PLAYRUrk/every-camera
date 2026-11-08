"""Cloudflare quick tunnel to the frame server, so the station is reachable from outside.

The tunnel points at a listener on 127.0.0.1 that refuses POST without the run's token;
the LAN listener is untouched. Nothing here may stop a measurement: every failure is a
warning and a later retry. WARNING, not ERROR, on purpose: alerts mails on ERROR.
"""
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time

from collections import deque
from pathlib import Path
from urllib.request import urlopen

import console_ui

BIN_DIR = Path.home() / ".every_camera" / "bin"
DOWNLOAD_BASE = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
DOWNLOAD_TIMEOUT = 120.0
# Another camera on this machine may be downloading; past this its lock is abandoned.
LOCK_STALE_SECONDS = 600.0
RESTART_BASE_SECONDS = 30.0
RESTART_MAX_SECONDS = 600.0
# api.trycloudflare.com appears in cloudflared's own errors; it is not a tunnel.
URL_RE = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")
# The URL is printed before the edge knows the tunnel; until this line it answers 530.
REGISTERED_RE = re.compile(r"Registered tunnel connection")
ERR_RE = re.compile(r"(?:^|\s)ERR\s")
ERR_WARN_SECONDS = 60.0
TAIL_LINES = 20
# QUIC (UDP 7844) is often blocked at stations; TCP 443 nearly never.
DEFAULT_PROTOCOL = "http2"
# With --config pointing here, an ingress from ~/.cloudflared or /etc/cloudflared cannot
# take over the quick tunnel and answer 503.
CONFIG_STUB = "quick-tunnel.yml"
CONFIG_STUB_TEXT = "no-autoupdate: true\n"
TOKEN_HEADER = "X-Everycam-Token"


def binary_name():
    return "cloudflared.exe" if os.name == "nt" else "cloudflared"


def asset_name():
    """Release asset for this machine, or None where Cloudflare publishes no bare binary."""
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64",
            "i386": "386", "i686": "386", "x86": "386"}.get(machine)
    if arch is None and machine.startswith("arm"):
        arch = "arm"
    if arch is None:
        return None
    if os.name == "nt":
        return f"cloudflared-windows-{arch}.exe" if arch in ("amd64", "386") else None
    if sys.platform.startswith("linux"):
        return f"cloudflared-linux-{arch}"
    return None


def _usable(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def download(bin_dir=None, timeout=DOWNLOAD_TIMEOUT):
    """Returns the path, or None. One download per machine: the others wait for it."""
    bin_dir = Path(bin_dir or BIN_DIR)
    target = bin_dir / binary_name()
    asset = asset_name()
    if asset is None:
        console_ui.warn(f"Tunnel: no cloudflared build for {platform.machine()} — "
                        f"install it by hand")
        return None
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        console_ui.warn(f"Tunnel: cannot create {bin_dir}: {exc}")
        return None
    lock = bin_dir / "cloudflared.download"
    # A slow link can take longer than any socket timeout; the lock's age decides.
    deadline = time.monotonic() + LOCK_STALE_SECONDS
    while True:
        if _usable(str(target)):
            return str(target)
        try:
            handle = open(lock, "x")
            handle.close()
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > LOCK_STALE_SECONDS:
                    lock.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                return None
            time.sleep(2)
        except OSError as exc:
            console_ui.warn(f"Tunnel: cannot lock {lock}: {exc}")
            return None
    tmp = bin_dir / f"{binary_name()}.part"
    try:
        console_ui.log(f"Tunnel: downloading {asset}…")
        with urlopen(DOWNLOAD_BASE + asset, timeout=timeout) as response, \
                open(tmp, "wb") as out:
            shutil.copyfileobj(response, out)
        with open(tmp, "rb") as check:
            magic = check.read(4)
        # A captive portal or proxy answers 200 with a page; kept, it would be "found"
        # on every later run.
        if magic[:2] != b"MZ" and magic != b"\x7fELF":
            raise ValueError(f"not an executable (starts with {magic!r})")
        if os.name != "nt":
            os.chmod(tmp, 0o755)
        os.replace(tmp, target)
        console_ui.log(f"Tunnel: cloudflared saved to {target}")
        return str(target)
    except Exception as exc:
        console_ui.warn(f"Tunnel: cloudflared download failed: {exc}")
        return None
    finally:
        for path in (tmp, lock):
            try:
                path.unlink()
            except OSError:
                pass


def find_cloudflared(cfg, bin_dir=None):
    cfg = cfg or {}
    explicit = str(cfg.get("cloudflared") or "").strip()
    if explicit:
        return explicit if _usable(explicit) else None
    found = shutil.which("cloudflared")
    if found:
        return found
    local = Path(bin_dir or BIN_DIR) / binary_name()
    if _usable(str(local)):
        return str(local)
    if cfg.get("auto_download", True):
        return download(bin_dir)
    return None


def _death_signal_setter():
    """Loaded in the parent: after fork() in a threaded process the child may only call,
    not import or dlopen, or it can hang on a lock another thread held.
    """
    try:
        import ctypes
        import signal
        prctl = ctypes.CDLL("libc.so.6", use_errno=True).prctl
        term = int(signal.SIGTERM)
    except Exception:
        return None

    def _set():
        prctl(1, term)                     # PR_SET_PDEATHSIG
    return _set


class _WindowsJob:
    """Kill-on-close job object: cloudflared ends with this process however it ends."""

    def __init__(self):
        self.handle = None
        try:
            import ctypes
            from ctypes import wintypes

            class _Basic(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                            ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD),
                            ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t),
                            ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t),
                            ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class _Io(ctypes.Structure):
                _fields_ = [(name, ctypes.c_uint64) for name in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class _Extended(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", _Basic),
                            ("IoInfo", _Io),
                            ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            kernel32 = ctypes.windll.kernel32
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return
            info = _Extended()
            info.BasicLimitInformation.LimitFlags = 0x2000   # KILL_ON_JOB_CLOSE
            if kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info),
                                                ctypes.sizeof(info)):
                self.handle = handle
        except Exception:
            self.handle = None

    def add(self, process):
        if self.handle is None:
            return
        try:
            import ctypes
            ctypes.windll.kernel32.AssignProcessToJobObject(
                self.handle, int(process._handle))
        except Exception:
            pass


class Tunnel:
    def __init__(self, port, cfg=None, service=None, bin_dir=None):
        self.port = int(port)
        self.cfg = cfg or {}
        self.service = service
        self.bin_dir = bin_dir
        self.url = None
        self._process = None
        self._stop = threading.Event()
        self._thread = None
        self._job = None
        self._err_at = None
        self._err_skipped = 0

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="everycam-tunnel")
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._kill()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._set_url(None)

    def _set_url(self, url):
        self.url = url
        if self.service is not None:
            try:
                self.service.tunnel_url = url
            except Exception:
                pass

    def _find(self):
        try:
            return find_cloudflared(self.cfg, self.bin_dir)
        except Exception as exc:
            console_ui.warn(f"Tunnel: {exc}")
            return None

    def _run(self):
        delay = RESTART_BASE_SECONDS
        warned = False
        while not self._stop.is_set():
            started = time.monotonic()
            # Looked for again each time: the network may come up after the camera.
            binary = self._find()
            if not binary:
                if not warned:
                    console_ui.warn("Tunnel: cloudflared not found — no access from "
                                    "outside for now. Measurements are not affected.")
                    warned = True
                if self._stop.wait(delay):
                    break
                delay = min(RESTART_MAX_SECONDS, delay * 2)
                continue
            warned = False
            try:
                self._run_once(binary)
            except Exception as exc:
                console_ui.warn(f"Tunnel: {exc}")
            self._set_url(None)
            if self._stop.is_set():
                break
            if time.monotonic() - started > RESTART_MAX_SECONDS:
                delay = RESTART_BASE_SECONDS
            console_ui.warn(f"Tunnel: cloudflared stopped; retrying in {delay:.0f} s")
            if self._stop.wait(delay):
                break
            delay = min(RESTART_MAX_SECONDS, delay * 2)

    def _config_stub(self):
        path = Path(self.bin_dir or BIN_DIR) / CONFIG_STUB
        try:
            if not path.is_file() or path.read_text(encoding="utf-8") != CONFIG_STUB_TEXT:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(CONFIG_STUB_TEXT, encoding="utf-8")
            return str(path)
        except OSError as exc:
            console_ui.warn(f"Tunnel: cannot write {path}: {exc}; a cloudflared "
                            f"config.yml on this machine may break the tunnel")
            return None

    def _args(self, binary):
        args = [binary, "tunnel", "--no-autoupdate"]
        config = self._config_stub()
        if config:
            args += ["--config", config]
        protocol = str(self.cfg.get("protocol") or DEFAULT_PROTOCOL).strip()
        args += ["--protocol", protocol, "--url", f"http://127.0.0.1:{self.port}"]
        return args

    def _spawn(self, binary):
        args = self._args(binary)
        kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
                  "stdin": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        elif sys.platform.startswith("linux"):
            setter = _death_signal_setter()
            if setter is not None:
                kwargs["preexec_fn"] = setter
        process = subprocess.Popen(args, **kwargs)
        if os.name == "nt":
            if self._job is None:
                self._job = _WindowsJob()
            self._job.add(process)
        return process

    def _run_once(self, binary):
        self._process = self._spawn(binary)
        tail = deque(maxlen=TAIL_LINES)
        candidate = None
        # Read to the end even when nothing is wanted: a full pipe would stall cloudflared.
        for raw in self._process.stdout:
            if self._stop.is_set():
                break
            line = raw.decode("utf-8", errors="replace").rstrip()
            tail.append(line)
            if not self.url:
                match = URL_RE.search(line)
                if match:
                    candidate = match.group(0)
                elif candidate and REGISTERED_RE.search(line):
                    self._set_url(candidate)
                    console_ui.log(f"Tunnel: {self.url}")
            if ERR_RE.search(line):
                self._warn_err(line)
        self._kill()
        if not self._stop.is_set():
            code = self._process.poll()
            console_ui.warn(f"Tunnel: cloudflared exited with code {code}; its last "
                            f"output:\n" + "\n".join(tail))

    def _warn_err(self, line):
        now = time.monotonic()
        if self._err_at is not None and now - self._err_at < ERR_WARN_SECONDS:
            self._err_skipped += 1
            return
        skipped = (f" ({self._err_skipped} more since the last one)"
                   if self._err_skipped else "")
        self._err_at = now
        self._err_skipped = 0
        console_ui.warn(f"Tunnel: cloudflared: {line}{skipped}")

    def _kill(self):
        process = self._process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        except Exception:
            pass
