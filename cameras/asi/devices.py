"""Hardware modules are imported only when selected, so the whole driver runs against the
simulators on a machine with no PICAM SDK or serial port.
"""


def make_camera(cfg):
    if cfg.camera.backend == "sim":
        from .camera_sim import SimCamera
        return SimCamera(cfg.camera, cfg.cooling)
    from .camera import PixisCamera
    return PixisCamera(cfg.camera, cfg.cooling)


def make_omega(cfg):
    from ..common.omega import OmegaMonitor
    return OmegaMonitor(cfg.omega.port, cfg.omega.baudrate, cfg.omega.poll_seconds,
                        exclude=(cfg.filter_wheel.port,))


def make_wheel(cfg):
    wheel = cfg.filter_wheel
    if str(wheel.port).strip().lower() == "sim":
        from ..common.filterwheel_sim import SimFilterWheel
        return SimFilterWheel(wheel.port, wheel.baudrate, wheel.move_timeout)
    from ..common.filterwheel import FilterWheel
    return FilterWheel(wheel.port, wheel.baudrate, wheel.move_timeout)
