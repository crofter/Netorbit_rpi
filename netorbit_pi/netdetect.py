"""Work out the local subnet and router on first start, from the kernel routing table."""

from __future__ import annotations

import ipaddress
import socket
import struct


def _hex_ip(h: str) -> str:
    return socket.inet_ntoa(struct.pack("<L", int(h, 16)))


def parse_routes(text: str) -> tuple[str, str] | None:
    """(subnet, gateway) from /proc/net/route text, or None.

    The default route gives the router and its interface; the directly connected route on
    that interface (no gateway) gives the subnet.
    """
    routes = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 8:
            continue
        try:
            iface, dest, gw, flags, mask = parts[0], _hex_ip(parts[1]), _hex_ip(parts[2]), int(parts[3], 16), _hex_ip(parts[7])
        except ValueError:
            continue
        if not flags & 0x1:  # RTF_UP
            continue
        routes.append((iface, dest, gw, mask, int(parts[6]) if parts[6].isdigit() else 0))
    defaults = sorted((r for r in routes if r[1] == "0.0.0.0" and r[2] != "0.0.0.0"), key=lambda r: r[4])
    if not defaults:
        return None
    iface, _, gateway, _, _ = defaults[0]
    gw = ipaddress.ip_address(gateway)
    for r_iface, dest, r_gw, mask, _ in routes:
        if r_iface != iface or r_gw != "0.0.0.0" or dest == "0.0.0.0":
            continue
        net = ipaddress.ip_network(f"{dest}/{mask}", strict=False)
        if gw in net:
            return str(net), gateway
    return str(ipaddress.ip_network(f"{gateway}/24", strict=False)), gateway


def detect_network(path: str = "/proc/net/route") -> tuple[str, str] | None:
    try:
        with open(path, encoding="ascii") as fh:
            return parse_routes(fh.read())
    except OSError:
        return None
