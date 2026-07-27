"""Backend selection for the ASI camera and its filter wheel.

Kept separate so that nothing imports a hardware module until it is actually
wanted: a machine with no PICAM SDK and no serial port can still run the whole
driver against the simulators (``camera.backend = "sim"``, ``filter_wheel.port =
"sim"``), which is how the scheduler and the console are exercised in tests.
"""


def make_camera(cfg):
    """Build the camera object for an :class:`config.AsiConfig`."""
    if cfg.camera.backend == "sim":
        from .camera_sim import SimCamera
        return SimCamera(cfg.camera, cfg.cooling)
    from .camera import PixisCamera
    return PixisCamera(cfg.camera, cfg.cooling)


def make_wheel(cfg):
    """Build the filter wheel object for an :class:`config.AsiConfig`."""
    wheel = cfg.filter_wheel
    if str(wheel.port).strip().lower() == "sim":
        from .filterwheel_sim import SimFilterWheel
        return SimFilterWheel(wheel.port, wheel.baudrate, wheel.move_timeout)
    from .filterwheel import FilterWheel
    return FilterWheel(wheel.port, wheel.baudrate, wheel.move_timeout)
