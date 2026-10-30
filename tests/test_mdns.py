"""Bonjour name lookups."""

from __future__ import annotations

import asyncio
import struct

from netorbit_pi.mdns import build_ptr_query, mdns_label, mdns_name, parse_ptr_answer


def _answer(qid: int, ip: str, target: str, compress: bool = True) -> bytes:
    q = build_ptr_query(ip, qid)
    header = struct.pack("!HHHHHH", qid, 0x8400, 1, 1, 0, 0)
    body = q[12:]
    name = b"\xc0\x0c" if compress else body[:-4]
    rdata = b"".join(bytes([len(p)]) + p.encode() for p in target.split(".")) + b"\x00"
    return header + body + name + struct.pack("!HHIH", 12, 1, 10, len(rdata)) + rdata


def test_parse_ptr_answer():
    assert parse_ptr_answer(_answer(7, "192.0.2.5", "Alexs-iPhone.local"), 7) == "Alexs-iPhone.local"
    assert parse_ptr_answer(_answer(7, "192.0.2.5", "Office-iMac.local", compress=False)) == "Office-iMac.local"
    assert parse_ptr_answer(_answer(7, "192.0.2.5", "x.local"), 8) is None  # wrong id
    assert parse_ptr_answer(build_ptr_query("192.0.2.5", 7)) is None  # a query, not an answer
    assert parse_ptr_answer(b"\x00\x01") is None
    # A pointer that points at itself must not hang.
    loop = struct.pack("!HHHHHH", 1, 0x8400, 0, 1, 0, 0) + b"\xc0\x0c"
    assert parse_ptr_answer(loop) is None


def test_mdns_label():
    assert mdns_label("Alexs-iPhone-15-Pro.local.") == "Alexs iPhone 15 Pro"
    assert mdns_label("Kids-Room.local") == "Kids Room"
    assert mdns_label("PS5-1A2B3C.local.") == "PS5 1A2B3C"
    assert mdns_label("Speaker-00005E00531E.local.") is None  # MAC inside
    assert mdns_label("ea1-00005E005342.local.") is None
    assert mdns_label("YX1234567890.local.") is None  # serial number
    assert mdns_label("10001234-01-A--C1D23456E78901.local.") is None
    assert mdns_label(None) is None and mdns_label("") is None


async def test_mdns_name_against_a_local_responder():
    loop = asyncio.get_running_loop()

    class Responder(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.t = transport

        def datagram_received(self, data, addr):
            qid = struct.unpack("!H", data[:2])[0]
            self.t.sendto(_answer(qid, "127.0.0.1", "Office-iMac.local"), addr)

    transport, _ = await loop.create_datagram_endpoint(Responder, local_addr=("127.0.0.1", 0))
    port = transport.get_extra_info("sockname")[1]
    import netorbit_pi.mdns as m

    old = m.MDNS_PORT
    m.MDNS_PORT = port
    try:
        assert await mdns_name("127.0.0.1", timeout=1) == "Office-iMac.local"
    finally:
        m.MDNS_PORT = old
        transport.close()
    # Nobody listening: no answer, no exception.
    assert await mdns_name("127.0.0.1", timeout=0.2) is None
