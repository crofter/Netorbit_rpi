"""Settings and state on disk: two small JSON files in the data directory.

* ``config.json``: what a person set up (subnet, router, options, hubs, device settings).
  This is what the Home Assistant integration kept in its config entry.
* ``state.json``: what NetOrbit learned (every MAC it has seen, when, names, SNMP answers).
  This is what the integration kept in ``.storage``.

Both are written atomically and readable only by the service user, because the config can
hold an SNMP community and a password hash.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any

_LOGGER = logging.getLogger(__name__)


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as err:
        # Keep the broken file for a person to look at, and start fresh.
        _LOGGER.error("Could not read %s (%s); keeping a copy as %s.bad", path, err, path.name)
        try:
            os.replace(path, path.with_suffix(path.suffix + ".bad"))
        except OSError:
            pass
        return None


class ConfigStore:
    """``{"data": {"subnet": ..., "gateway": ...}, "options": {...}}``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        raw = read_json(path) or {}
        self.data: dict[str, Any] = dict(raw.get("data") or {})
        self.options: dict[str, Any] = dict(raw.get("options") or {})

    @property
    def configured(self) -> bool:
        return bool(self.data.get("subnet") and self.data.get("gateway"))

    def save(self) -> None:
        write_json_atomic(self.path, {"data": self.data, "options": self.options})

    def update(self, data: dict[str, Any] | None = None, options: dict[str, Any] | None = None) -> None:
        if data is not None:
            self.data = copy.deepcopy(data)
        if options is not None:
            self.options = copy.deepcopy(options)
        self.save()


class StateStore:
    """Learned state, saved a few seconds after the last change (like HA's delayed save)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._pending: Any = None
        self._handle: asyncio.TimerHandle | None = None

    def load(self) -> dict[str, Any]:
        return read_json(self.path) or {}

    def delay_save(self, producer, delay: float = 10.0) -> None:
        """Save ``producer()`` after ``delay`` seconds; later calls push the save back."""
        self._pending = producer
        loop = asyncio.get_running_loop()
        if self._handle:
            self._handle.cancel()
        self._handle = loop.call_later(delay, self.flush)

    def flush(self) -> None:
        if self._handle:
            self._handle.cancel()
            self._handle = None
        producer, self._pending = self._pending, None
        if producer is None:
            return
        try:
            write_json_atomic(self.path, producer())
        except OSError as err:
            _LOGGER.error("Could not save state to %s: %s", self.path, err)
