"""Tell someone when a device appears or goes quiet: a webhook, ntfy, or both.

In Home Assistant these were events for automations and two notification blueprints.
Standalone, NetOrbit sends them itself:

* **Webhook**: POSTs ``{"event": "netorbit_device_stale", "name": ..., "ip": ..., "mac": ...}``
  as JSON. Works with Node-RED, n8n, Home Assistant webhooks and most chat bots.
* **ntfy**: POSTs a one-line message to an ntfy topic URL (for example
  ``https://ntfy.sh/my-network-abc123``), which shows up as a phone notification.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
import urllib.request

from .const import (
    CONF_NOTIFY_EVENTS,
    CONF_NTFY_URL,
    CONF_WEBHOOK_URL,
    EVENT_DEVICE_RECOVERED,
    EVENT_DEVICE_STALE,
    EVENT_NEW_DEVICE,
    VERSION,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_NOTIFY_EVENTS = [EVENT_NEW_DEVICE, EVENT_DEVICE_STALE]
TIMEOUT = 10


def message_for(event: str, payload: dict[str, Any]) -> tuple[str, str]:
    """(title, body) in plain words."""
    name = payload.get("name") or payload.get("mac") or payload.get("ip") or "A device"
    where = " ".join(x for x in (payload.get("ip"), f"({payload['mac']})" if payload.get("mac") else None) if x)
    if event == EVENT_NEW_DEVICE:
        return "New device on the network", f"{name} joined the network {where}".strip()
    if event == EVENT_DEVICE_STALE:
        kind = "Hub" if payload.get("kind") == "hub" else "Device"
        return f"{kind} not seen", f"{name} has not answered for a while {where}".strip()
    if event == EVENT_DEVICE_RECOVERED:
        return "Device back", f"{name} is answering again {where}".strip()
    return "NetOrbit", f"{event}: {name}"


def _post(url: str, body: bytes, headers: dict[str, str]) -> None:
    req = urllib.request.Request(url, data=body, method="POST", headers={"User-Agent": f"netorbit-pi/{VERSION}", **headers})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 - URL set by the admin
        resp.read(1024)


class Notifier:
    """Listens to coordinator events and forwards the ones the person asked for."""

    def __init__(self, options_getter) -> None:
        self._options = options_getter
        self._tasks: set[asyncio.Task] = set()

    def __call__(self, event: str, payload: dict[str, Any]) -> None:
        opts = self._options()
        wanted = opts.get(CONF_NOTIFY_EVENTS)
        if wanted is None:
            wanted = DEFAULT_NOTIFY_EVENTS
        if event not in wanted:
            return
        task = asyncio.get_running_loop().create_task(self.send(event, payload, opts))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def send(self, event: str, payload: dict[str, Any], opts: dict[str, Any]) -> list[str]:
        """Send to every configured target. Returns the targets that failed."""
        failed = []
        title, body = message_for(event, payload)
        webhook = (opts.get(CONF_WEBHOOK_URL) or "").strip()
        ntfy = (opts.get(CONF_NTFY_URL) or "").strip()
        if webhook:
            data = json.dumps({"event": event, "title": title, "message": body, **payload}).encode()
            try:
                await asyncio.to_thread(_post, webhook, data, {"Content-Type": "application/json"})
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Webhook failed: %s", err)
                failed.append("webhook")
        if ntfy:
            tags = {EVENT_NEW_DEVICE: "new", EVENT_DEVICE_STALE: "warning", EVENT_DEVICE_RECOVERED: "white_check_mark"}
            try:
                await asyncio.to_thread(
                    _post, ntfy, body.encode(), {"Title": title, "Tags": tags.get(event, "satellite")}
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("ntfy failed: %s", err)
                failed.append("ntfy")
        return failed
