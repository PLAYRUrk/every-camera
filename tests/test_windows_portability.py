"""The Windows station runs, and keeps running, without a POSIX kernel under it.

every-camera's observer tools have always been claimed to work on Windows; the
Hamamatsu (``japan``) camera is now claimed to work there too, which is a much
stronger claim — it means the driver, the LAN frame server, the status files and
the shutdown path, not just a viewer.

None of that can be *run* here. What can be checked, and is, is that nothing on
that path reaches for something Windows does not have: no ``os.statvfs``, no
bare ``fcntl``, no ``/proc``, no POSIX-only port name baked into a default. Each
test below is a thing that once would have failed, or that would fail if someone
reintroduced the shortcut.

The tests that need to look like Windows patch ``<module>.os.name`` rather than
``os.name`` itself: rebinding the real one breaks the standard library halfway
through the run (``shutil`` imports the ``nt`` module on the spot).
"""
import ast
import sys

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import utils                                                 # noqa: E402
import worker_common                                         # noqa: E402

from cameras.common import filterwheel                       # noqa: E402
from cameras.asi import config as asi_config                 # noqa: E402
from cameras.japan import config as japan_config             # noqa: E402


SOURCE_ROOT = Path(__file__).resolve().parent.parent

# Modules a Windows station with a japan camera actually imports. Not the whole
# repo: the cannon driver shells out to gvfs and gphoto2, the sentry driver
# supervises a Linux ELF daemon, and the infra driver loads a ``.so`` — all
# three are documented as Linux-only rather than pretended about here. Hamamatsu's
# vendored SDK wrappers are excluded too — they are the vendor's files, they
# already branch on the platform correctly, and ``test_japan_sdk`` guards that.
WINDOWS_PATH_MODULES = [
    "utils.py", "worker_common.py", "camera_service.py", "frame_server.py",
    "discovery.py", "gateway.py", "peers.py", "net_client.py", "monitor.py",
    "console_ui.py", "alerts.py", "mailer.py", "sentinel.py", "intensity.py",
    "frame_archive.py", "frame_grouping.py", "main.py",
    "cameras/japan_driver.py",
    "cameras/japan/config.py", "cameras/japan/camera.py",
    "cameras/japan/camera_sim.py", "cameras/japan/devices.py",
    "cameras/japan/fits.py", "cameras/japan/paths.py",
    "cameras/japan/dcamsdk/__init__.py",
    "cameras/common/exposure.py", "cameras/common/exposure_config.py",
    "cameras/common/schedule.py", "cameras/common/filterwheel.py",
    "cameras/common/filterwheel_sim.py", "cameras/common/fits.py",
    "cameras/common/archive_paths.py", "cameras/common/seqno.py",
    "cameras/common/filters.py", "cameras/common/sun.py",
    "cameras/common/timeutil.py", "cameras/common/cfgparse.py",
]


def source_of(relative):
    return (SOURCE_ROOT / relative).read_text(encoding="utf-8")


def tree_of(relative):
    """The module parsed, so the checks below look at code rather than at prose.

    These modules explain themselves at length, and several of the explanations
    name the very call they are explaining the absence of — "``shutil.disk_usage``
    rather than ``os.statvfs``, which does not exist on Windows". A grep over raw
    text reads those as the mistake they document.
    """
    return ast.parse(source_of(relative), filename=relative)


def attribute_names(tree):
    """Every ``x.attr`` spelled anywhere in the module, as a set of ``attr``."""
    return {node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)}


def imported_modules(tree):
    """Every module name imported anywhere in the module, guarded or not."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


# ---------------------------------------------------------------------------
# Nothing on the Windows path calls something Windows does not have
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("relative", WINDOWS_PATH_MODULES)
def test_no_module_on_the_windows_path_calls_statvfs(relative):
    """``os.statvfs`` does not exist on Windows; ``shutil.disk_usage`` does.

    It used to be called inside a bare ``except Exception``, so a Windows node
    simply reported no free disk space at all — the one metric an operator
    watches to know a station is about to stop writing frames.
    """
    assert "statvfs" not in attribute_names(tree_of(relative))


@pytest.mark.parametrize("relative", WINDOWS_PATH_MODULES)
def test_no_module_on_the_windows_path_reads_proc(relative):
    """``/proc`` is Linux's. Anything read from it needs a branch, not a hope."""
    tree = tree_of(relative)
    proc_paths = [node.value for node in ast.walk(tree)
                  if isinstance(node, ast.Constant)
                  and isinstance(node.value, str)
                  and node.value.startswith("/proc")]
    if proc_paths:
        assert "name" in attribute_names(tree), (
            f"{relative} names {proc_paths} with no platform branch in it")


def test_only_the_lock_helper_imports_fcntl():
    """One guarded import, in one place, with a Windows counterpart beside it.

    ``fcntl`` is the module most likely to be reached for by reflex, and an
    unguarded import of it fails at *import* time — so a single one anywhere on
    this path takes the whole program down on Windows rather than degrading.
    """
    users = [relative for relative in WINDOWS_PATH_MODULES
             if "fcntl" in imported_modules(tree_of(relative))]
    assert users == ["utils.py"]
    assert "msvcrt" in imported_modules(tree_of("utils.py")), (
        "no Windows counterpart to the flock")


# ---------------------------------------------------------------------------
# The serial port default is spelled for the platform it is offered on
# ---------------------------------------------------------------------------
def test_the_wheel_port_default_follows_the_platform(monkeypatch):
    """A Windows operator offered ``/dev/ttyUSB0`` has to be told twice."""
    monkeypatch.setattr(filterwheel.os, "name", "nt")
    assert filterwheel.default_port() == "COM3"
    monkeypatch.setattr(filterwheel.os, "name", "posix")
    assert filterwheel.default_port() == "/dev/ttyUSB0"


@pytest.mark.parametrize("module", [asi_config, japan_config])
def test_both_cameras_take_their_port_default_from_the_platform(module,
                                                                monkeypatch):
    """And both do it through the one function, so they cannot drift apart."""
    monkeypatch.setattr(filterwheel.os, "name", "nt")
    assert module.from_dict({}).filter_wheel.port == "COM3"
    monkeypatch.setattr(filterwheel.os, "name", "posix")
    assert module.from_dict({}).filter_wheel.port == "/dev/ttyUSB0"


@pytest.mark.parametrize("module", [asi_config, japan_config])
def test_a_configured_port_still_wins_on_either_platform(module, monkeypatch):
    """The default is a suggestion. What config.json says is what is opened."""
    monkeypatch.setattr(filterwheel.os, "name", "nt")
    cfg = module.from_dict({"filter_wheel": {"port": "/dev/ttyUSB7"}})
    assert cfg.filter_wheel.port == "/dev/ttyUSB7"


# ---------------------------------------------------------------------------
# System metrics degrade by leaving a key out, never by lying
# ---------------------------------------------------------------------------
def test_system_info_survives_a_machine_with_no_proc_and_no_statvfs(tmp_path,
                                                                    monkeypatch):
    """What a Windows node publishes: everything but the load average.

    The monitor treats every metric as optional, so the contract this has to
    keep is "absent, not wrong" — a station reporting 0 % memory or 0 MB free
    would read as a machine in trouble.
    """
    def no_proc(path, *args, **kwargs):
        if str(path).startswith("/proc"):
            raise FileNotFoundError(path)
        return open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", no_proc)
    monkeypatch.setattr(utils, "_windows_mem_used_pct", lambda: 42.0)
    monkeypatch.setattr(utils.os, "name", "nt")

    info = utils.get_system_info(str(tmp_path))
    assert "load_avg_1m" not in info, "Windows has no load average to report"
    assert info["mem_used_pct"] == 42.0
    assert info["disk_free_mb"] > 0, "shutil.disk_usage works on every platform"
    assert info["hostname"] and info["platform"]


def test_disk_free_is_reported_without_statvfs(tmp_path):
    """The plain case, on whatever platform the suite is running on."""
    info = utils.get_system_info(str(tmp_path))
    assert info["disk_free_mb"] > 0


# ---------------------------------------------------------------------------
# The instance-name reservation is a real lock on both platforms
# ---------------------------------------------------------------------------
def test_a_second_claim_of_one_name_gets_a_different_one(tmp_path):
    """The property the whole scheme rests on, exercised on this platform.

    Two copies of every-camera sharing one name share their status, log and
    preview files — which is how one camera's dashboard ends up showing
    another's frames.
    """
    first = utils.claim_instance_name("station", lock_dir=str(tmp_path))
    second = utils.claim_instance_name("station", lock_dir=str(tmp_path))
    try:
        assert first.name == "station"
        assert second.name == "station-2"
    finally:
        first.release()
        second.release()


def test_a_released_name_can_be_claimed_again(tmp_path):
    """Closing the descriptor drops the lock — on POSIX and on Windows alike."""
    first = utils.claim_instance_name("station", lock_dir=str(tmp_path))
    first.release()
    second = utils.claim_instance_name("station", lock_dir=str(tmp_path))
    try:
        assert second.name == "station"
    finally:
        second.release()


def test_a_platform_with_no_locking_still_runs_and_says_so(tmp_path, monkeypatch):
    """Not being able to reserve a name must cost a warning, not the run."""
    def unsupported(fd):
        raise NotImplementedError

    monkeypatch.setattr(utils, "_lock_file_exclusive", unsupported)
    warnings = []
    monkeypatch.setattr(utils.console_ui, "warn", warnings.append)

    claim = utils.claim_instance_name("station", lock_dir=str(tmp_path))
    assert claim.name == "station"
    assert len(warnings) == 1
    assert "not reserved" in warnings[0]


# ---------------------------------------------------------------------------
# The launcher
# ---------------------------------------------------------------------------
def test_there_is_a_windows_launcher_beside_the_shell_one():
    """``run.sh`` has no meaning on Windows, and a station needs one entry point."""
    assert (SOURCE_ROOT / "run.cmd").is_file()
    assert (SOURCE_ROOT / "env.cmd.example").is_file()


def test_the_windows_launcher_looks_in_the_windows_venv():
    """``venv/bin/python`` does not exist there; ``venv\\Scripts\\python.exe`` does."""
    source = source_of("run.cmd")
    assert r"venv\Scripts\python.exe" in source
    assert "main.py" in source and "sentinel.py" in source


def test_the_real_env_file_is_not_committed():
    """env.cmd describes one machine, exactly as env.sh does."""
    ignored = source_of(".gitignore").split()
    assert "env.cmd" in ignored
    assert not (SOURCE_ROOT / "env.cmd").exists(), (
        "a machine-local env.cmd has been committed")


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------
def test_the_service_config_dir_is_not_an_etc_path_on_windows(monkeypatch):
    """There is no /etc to read the installed services' configs out of."""
    monkeypatch.setenv("PROGRAMDATA", r"C:\ProgramData")
    import sentinel

    monkeypatch.setattr(sentinel.os, "name", "nt")
    windows_dir = sentinel._service_config_dir()
    # Joined with the running machine's separator, so this asserts the parts
    # rather than one spelling of them.
    assert windows_dir.startswith(r"C:\ProgramData")
    assert windows_dir.endswith("every-camera")
    monkeypatch.setattr(sentinel.os, "name", "posix")
    assert sentinel._service_config_dir() == "/etc/every-camera"


def test_the_japan_driver_installs_the_shared_stop_handler():
    """Not its own SIGINT: the shared helper is where the Windows routes live.

    ``worker_common.install_stop_handler`` is what knows about SIGBREAK and the
    console-close hook, so a driver that registered signals itself would be the
    one that loses its closing darks when the window is shut.
    """
    source = source_of("cameras/japan_driver.py")
    assert "install_stop_handler" in source
    assert "signal.signal(signal.SIGINT" not in source
