"""Ask the network who is a router or access point over UPnP (SSDP M-SEARCH).

Home Assistant runs its own SSDP listener and NetOrbit borrowed its results. Standalone,
NetOrbit asks once per sweep: a few multicast questions and two seconds of listening.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket

_LOGGER = logging.getLogger(__name__)

SSDP_ADDR = ("239.255.255.250", 1900)
SEARCH_TARGETS = (
    "urn:schemas-upnp-org:device:WLANAccessPointDevice:1",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
)
_ST_RE = re.compile(r"^st:\s*(.+?)\s*$", re.I | re.M)


def build_msearch(st: str, mx: int = 2) -> bytes:
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {mx}\r\n"
        f"ST: {st}\r\n\r\n"
    ).encode()


def parse_response(data: bytes) -> str | None:
    """The search target a device answered for, or None for anything else."""
    try:
        text = data.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    if not text.upper().startswith("HTTP/1.1 200"):
        return None
    m = _ST_RE.search(text)
    return m.group(1) if m else None


class _Collector(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.found: dict[str, list[str]] = {}

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ANN001
        st = parse_response(data)
        if st and st in SEARCH_TARGETS:
            types = self.found.setdefault(addr[0], [])
            if st not in types:
                types.append(st)

    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("SSDP error: %s", exc)


async def ssdp_types_by_ip(listen: float = 2.0) -> dict[str, list[str]]:
    """{ip: [device types]} for devices that answered as a gateway or access point."""
    loop = asyncio.get_running_loop()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.bind(("", 0))
        sock.setblocking(False)
        transport, proto = await loop.create_datagram_endpoint(_Collector, sock=sock)
    except OSError as err:
        _LOGGER.debug("SSDP unavailable: %s", err)
        return {}
    try:
        for st in SEARCH_TARGETS:
            transport.sendto(build_msearch(st), SSDP_ADDR)
        await asyncio.sleep(listen)
    finally:
        transport.close()
    return proto.found
