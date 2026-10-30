"""Ask a device for its Bonjour (mDNS) name.

Apple devices, many printers, speakers and smart home gear answer a reverse lookup
sent straight to them on UDP 5353 with names like "Alexs-iPhone.local". One small
packet per device, and only when NetOrbit has no better name source.
"""

from __future__ import annotations

import asyncio
import random
import re
import struct

from .logic import useful_name

MDNS_PORT = 5353
_SERIAL_DIGITS = re.compile(r"\d{8,}")


def build_ptr_query(ip: str, qid: int) -> bytes:
    """A standard DNS query for the PTR record of an IPv4 address."""
    labels = list(reversed(ip.split("."))) + ["in-addr", "arpa"]
    qname = b"".join(bytes([len(p)]) + p.encode() for p in labels) + b"\x00"
    header = struct.pack("!HHHHHH", qid, 0, 1, 0, 0, 0)
    return header + qname + struct.pack("!HH", 12, 1)  # type PTR, class IN


def _read_name(data: bytes, pos: int, depth: int = 0) -> tuple[str, int]:
    """Decode a (possibly compressed) DNS name. Returns the name and the position after it."""
    if depth > 10:
        raise ValueError("compression loop")
    labels: list[str] = []
    while True:
        if pos >= len(data):
            raise ValueError("truncated")
        length = data[pos]
        if length == 0:
            return ".".join(labels), pos + 1
        if length & 0xC0 == 0xC0:
            if pos + 1 >= len(data):
                raise ValueError("truncated")
            ptr = ((length & 0x3F) << 8) | data[pos + 1]
            rest, _ = _read_name(data, ptr, depth + 1)
            if rest:
                labels.append(rest)
            return ".".join(labels), pos + 2
        pos += 1
        labels.append(data[pos : pos + length].decode("utf-8", "replace"))
        pos += length


def parse_ptr_answer(data: bytes, qid: int | None = None) -> str | None:
    """The first PTR target in a DNS response, or None."""
    try:
        rid, flags, qd, an, _ns, _ar = struct.unpack("!HHHHHH", data[:12])
        if qid is not None and rid != qid:
            return None
        if not flags & 0x8000:  # not a response
            return None
        pos = 12
        for _ in range(qd):
            _, pos = _read_name(data, pos)
            pos += 4
        for _ in range(an):
            _, pos = _read_name(data, pos)
            rtype, _rclass, _ttl, rdlen = struct.unpack("!HHIH", data[pos : pos + 10])
            pos += 10
            if rtype == 12:
                name, _ = _read_name(data, pos)
                return name or None
            pos += rdlen
    except (ValueError, struct.error):
        return None
    return None


def mdns_label(name: str | None) -> str | None:
    """'Alexs-iPhone-15-Pro.local' -> 'Alexs iPhone 15 Pro'. None for serial numbers and MACs."""
    if not name:
        return None
    name = name.strip().rstrip(".")
    if name.lower().endswith(".local"):
        name = name[: -len(".local")]
    if "." in name:
        name = name.split(".")[0]
    if not name or _SERIAL_DIGITS.search(name):
        return None
    if useful_name(name) is None:
        return None
    return useful_name(re.sub(r"[-_]+", " ", name))


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, qid: int) -> None:
        self.qid = qid
        self.result: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ANN001
        if not self.result.done():
            name = parse_ptr_answer(data, self.qid)
            if name:
                self.result.set_result(name)

    def error_received(self, exc: Exception) -> None:
        if not self.result.done():
            self.result.set_result(None)


async def mdns_name(ip: str, timeout: float = 1.0) -> str | None:
    """Ask the device at `ip` for its own name. None if it doesn't answer."""
    loop = asyncio.get_running_loop()
    qid = random.randint(1, 0xFFFF)
    try:
        transport, proto = await loop.create_datagram_endpoint(lambda: _Proto(qid), remote_addr=(ip, MDNS_PORT))
    except OSError:
        return None
    try:
        transport.sendto(build_ptr_query(ip, qid))
        return await asyncio.wait_for(proto.result, timeout)
    except (TimeoutError, asyncio.TimeoutError, OSError):
        return None
    finally:
        transport.close()
