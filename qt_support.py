"""Shared by the Qt programs: an exception escaping a slot or QThread.run makes
PyQt 5.5+ call qFatal and abort the whole process, every camera tab with it."""
import sys
import traceback


def install_excepthook(log=None):
    """The error is printed, passed to ``log`` and shown; the program keeps running."""
    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            sys.stderr.write(text)
        except Exception:
            pass
        if log is not None:
            try:
                log(f"Unexpected error: {exc_type.__name__}: {exc}")
            except Exception:
                pass
        try:
            from PyQt5.QtCore import QThread
            from PyQt5.QtWidgets import QApplication, QMessageBox
            app = QApplication.instance()
            if app is not None and QThread.currentThread() is app.thread():
                QMessageBox.critical(None, "Unexpected error",
                                     f"{exc_type.__name__}: {exc}\n\n"
                                     f"The program keeps running; details are in "
                                     f"the terminal.")
        except Exception:
            pass

    sys.excepthook = hook
