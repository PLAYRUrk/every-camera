"""Backend selection for the Hamamatsu camera and its filter wheel.

Kept separate so that nothing imports a hardware module until it is actually
wanted: a machine with no DCAM-API SDK and no serial port can still run the whole
driver against the simulators (``camera.backend = "sim"``, ``filter_wheel.port =
"sim"``), which is how the scheduler and the console are exercised in tests.

The wheel comes from ``cameras/common/`` — it is the same controller the ASI imager
drives, with the same commands.
"""


def make_camera(cfg):
    """Build the camera object for a :class:`config.JapanConfig`."""
    if cfg.camera.backend == "sim":
        from .camera_sim import SimCamera
        return SimCamera(cfg.camera)
    from .camera import HamamatsuCamera
    return HamamatsuCamera(cfg.camera)


def make_wheel(cfg):
    """Build the filter wheel object for a :class:`config.JapanConfig`."""
    wheel = cfg.filter_wheel
    if str(wheel.port).strip().lower() == "sim":
        from ..common.filterwheel_sim import SimFilterWheel
        return SimFilterWheel(wheel.port, wheel.baudrate, wheel.move_timeout)
    from ..common.filterwheel import FilterWheel
    return FilterWheel(wheel.port, wheel.baudrate, wheel.move_timeout)
