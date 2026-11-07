"""Automatic telemetry: one CSV row per published status, for every camera.

One file per instance and UTC day under ``~/.every_camera/telemetry``. Nothing here may
disturb a measurement, so a failure is reported once and the row is dropped.
"""
import csv
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import console_ui

# Long strings and paths that belong in the status file, not in a time series.
_SKIPPED = frozenset({"pid", "camera_type", "instance_name", "node_name",
                      "last_update", "output_dir"})


def telemetry_dir():
    return Path.home() / ".every_camera" / "telemetry"


def flatten(payload, prefix=""):
    row = {}
    for key, value in payload.items():
        if not prefix and key in _SKIPPED:
            continue
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            row.update(flatten(value, f"{name}."))
        elif isinstance(value, (list, tuple)):
            row[name] = ";".join(str(item) for item in value)
        else:
            row[name] = value
    return row


class TelemetryLog:
    def __init__(self, instance_name, directory=None, clock=None):
        safe = re.sub(r"[^\w.-]+", "_", str(instance_name or "camera"))
        self._name = safe
        self._dir = Path(directory) if directory else telemetry_dir()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._day = None
        self._part = 1
        self._columns = None
        self._failed = False

    def _path(self):
        tail = f"_part{self._part}" if self._part > 1 else ""
        return self._dir / f"{self._name}_{self._day}{tail}.csv"

    def _existing_columns(self, path):
        with open(path, newline="", encoding="utf-8") as handle:
            return next(csv.reader(handle), None)

    def _select_file(self, row):
        """A new day or a field the header lacks moves to a new file: a column added to
        a CSV already written would shift nothing and label nothing.
        """
        day = self._clock().strftime("%Y%m%d")
        if day != self._day:
            self._day = day
            self._part = 1
            self._columns = None
        while True:
            if self._columns is None:
                path = self._path()
                self._columns = (self._existing_columns(path)
                                 if path.exists() else None)
            if self._columns is None:
                self._columns = ["time_utc"] + sorted(row)
                return True
            if set(row) <= set(self._columns):
                return False
            self._part += 1
            self._columns = None

    def write(self, payload):
        try:
            row = flatten(payload)
            new_file = self._select_file(row)
            os.makedirs(self._dir, exist_ok=True)
            row["time_utc"] = self._clock().isoformat(timespec="seconds")
            with open(self._path(), "a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=self._columns)
                if new_file:
                    writer.writeheader()
                writer.writerow(row)
            self._failed = False
        except Exception as exc:
            if not self._failed:
                self._failed = True
                console_ui.warn(f"Telemetry not written: {exc}")
