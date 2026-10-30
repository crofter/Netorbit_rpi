"""Work out the network layout from switch MAC tables. Pure Python, no Home Assistant imports.

A managed switch remembers which port it last saw each MAC address on. With the
tables from one or more switches we can tell:

* which switch (and port) every device hangs off,
* which hub sits on that same port (an access point, a mesh node, another switch),
* how the switches and hubs chain together back to the router.

The rule for "nearest" is simple: a switch S is the nearest switch to MAC m when no
other switch with a table sits between S and m on the same port.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Topology:
    """Result of the inference."""

    hub_parent: dict[str, str] = field(default_factory=dict)
    device_hub: dict[str, str] = field(default_factory=dict)


def macs_close(a: str | None, b: str | None, spread: int = 16) -> bool:
    """True when two MACs are the same device's base MAC and a radio (BSSID) MAC.

    Access points often report clients against a radio MAC that differs from the
    LAN MAC only in the last byte, or in a locally administered first byte.
    """
    if not a or not b:
        return False
    if a == b:
        return True
    pa, pb = a.split(":"), b.split(":")
    if len(pa) != 6 or len(pb) != 6:
        return False
    if pa[1:5] == pb[1:5] and abs(int(pa[5], 16) - int(pb[5], 16)) <= spread:
        first_a, first_b = int(pa[0], 16), int(pb[0], 16)
        return first_a == first_b or (first_a | 0x02) == (first_b | 0x02)
    return False


def infer(
    tables: dict[str, dict[str, int]],
    hub_macs: dict[str, str],
    gateway_mac: str | None,
    device_macs: dict[str, str],
    root: str,
    leaf_hubs: set[str] | None = None,
) -> Topology:
    """Infer hub parents and device placement.

    tables: switch hub key -> {mac: port}
    hub_macs: hub key -> mac (switches and other hubs)
    gateway_mac: the router's MAC, used to find each switch's uplink port
    device_macs: device key -> mac
    root: the router's hub key
    leaf_hubs: hubs that devices can sit behind (access points, mesh nodes). Defaults
               to every hub without a table of its own.
    """
    result = Topology()
    if not tables:
        return result
    leaf_hubs = set(hub_macs) - set(tables) if leaf_hubs is None else leaf_hubs

    uplink: dict[str, int | None] = {}
    for sw, table in tables.items():
        uplink[sw] = table.get(gateway_mac) if gateway_mac and sw != root else None

    def downstream_port(sw: str, mac: str) -> int | None:
        port = tables[sw].get(mac)
        if port is None or port == 0 or port == uplink[sw]:
            return None
        return port

    def locate(mac: str, exclude: str | None = None) -> tuple[str, int] | None:
        candidates = [(sw, p) for sw in tables if sw != exclude and (p := downstream_port(sw, mac)) is not None]
        for sw, port in candidates:
            blocked = False
            for other, _ in candidates:
                if other == sw:
                    continue
                other_mac = hub_macs.get(other)
                if other_mac and tables[sw].get(other_mac) == port:
                    blocked = True
                    break
            if not blocked:
                return sw, port
        return candidates[0] if candidates else None

    hub_location: dict[str, tuple[str, int]] = {}
    for hub, mac in hub_macs.items():
        if hub == root or not mac:
            continue
        loc = locate(mac, exclude=hub)
        if loc:
            hub_location[hub] = loc
            result.hub_parent[hub] = loc[0]

    # Switches whose router MAC sits on a port facing another switch hang off that switch;
    # anything still unplaced hangs off the router.
    for sw in tables:
        if sw != root and sw not in result.hub_parent:
            result.hub_parent[sw] = root

    by_port: dict[tuple[str, int], list[str]] = {}
    for hub, loc in hub_location.items():
        if hub in leaf_hubs:
            by_port.setdefault(loc, []).append(hub)

    known_hub_macs = {m for m in hub_macs.values() if m}
    for key, mac in device_macs.items():
        if not mac or mac in known_hub_macs:
            continue
        loc = locate(mac)
        if not loc:
            continue
        hubs_here = by_port.get(loc, [])
        result.device_hub[key] = hubs_here[0] if len(hubs_here) == 1 else loc[0]
    return result
