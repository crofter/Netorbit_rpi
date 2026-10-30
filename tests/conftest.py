"""Shared helpers. Addresses use the 192.0.2.0/24 documentation range."""

from __future__ import annotations

from pathlib import Path

from netorbit_pi.coordinator import NetOrbitCoordinator
from netorbit_pi.sim import SimNetIO
from netorbit_pi.store import ConfigStore, StateStore


async def make_coordinator(tmp: Path, sim: SimNetIO, options: dict | None = None) -> NetOrbitCoordinator:
    """A loaded coordinator on a simulated network, with its files in ``tmp``."""
    cfg = ConfigStore(tmp / "config.json")
    if not cfg.configured:
        cfg.update(data={"subnet": sim.net.subnet, "gateway": sim.net.gateway}, options=options or {})
    coord = NetOrbitCoordinator(cfg, StateStore(tmp / "state.json"), sim)
    await coord.async_load()
    return coord
