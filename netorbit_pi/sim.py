"""A NetIO that answers from a generated home network instead of the real one.

Used by ``netorbit-pi --demo`` (so you can try the map anywhere) and by the tests. The
network comes from ``netgen``, the original project's simulator, so every answer is
consistent: who replies to ping, the ARP table, names, makers, SNMP and switch tables.
"""

from __future__ import annotations

import random

from .netgen import SHAPES, VENDORS, Network
from .snmp import ProbeResult


class SimNetIO:
    """Answers like the network ``net`` would. ``churn`` makes devices come and go."""

    def __init__(self, net: Network, seed: int = 1, churn: float = 0.0) -> None:
        self.net = net
        self.rng = random.Random(seed)
        self.churn = churn
        self.alive: set[str] = {n.ip for n in net.nodes.values() if n.ip and n.answers_ping}
        self.arp: dict[str, str] = {n.ip: n.mac for n in net.nodes.values() if n.ip and n.mac}
        self.dns: dict[str, str] = {n.ip: f"{n.hostname}.lan" for n in net.nodes.values() if n.ip and n.hostname}
        self.mdns: dict[str, str] = {}
        self.vendor_db: dict[str, str] = {p.upper(): v for p, v in VENDORS.values() if v}
        self.snmp: dict[str, tuple[str, ProbeResult]] = {
            n.ip: (n.snmp_community, ProbeResult(descr=f"{n.hostname or 'switch'} managed switch", name=n.hostname, bridge=True))
            for n in net.nodes.values() if n.snmp_community and n.ip
        }
        self.tables = net.mac_tables(self.rng)
        self.ssdp: dict[str, list[str]] = {}
        self.probed: list[tuple[str, str]] = []
        self.privileged: bool | None = False

    @classmethod
    def from_shape(cls, shape: str = "snmp_chain", seed: int = 1, churn: float = 0.0) -> SimNetIO:
        return cls(SHAPES[shape](random.Random(seed)), seed=seed, churn=churn)

    def _tick(self) -> None:
        """A few devices drop off or come back, so the demo map looks alive."""
        if not self.churn:
            return
        for n in self.net.devices():
            if n.ip and self.rng.random() < self.churn:
                if n.ip in self.alive:
                    self.alive.discard(n.ip)
                elif n.answers_ping:
                    self.alive.add(n.ip)

    async def detect_privileges(self) -> bool | None:
        return self.privileged

    async def multiping(self, hosts, *, count, timeout, interval, concurrent, privileged) -> set[str]:
        if len(hosts) > 1:
            self._tick()
        return {h for h in hosts if h in self.alive}

    async def read_arp(self) -> dict[str, str]:
        return dict(self.arp)

    async def reverse_dns(self, ip: str) -> str | None:
        return self.dns.get(ip)

    async def mdns_name(self, ip: str) -> str | None:
        return self.mdns.get(ip)

    async def vendors(self, macs: list[str]) -> dict[str, str | None] | None:
        return {mac: self.vendor_db.get(mac[:8].upper()) for mac in macs}

    async def snmp_probe(self, ip: str, community: str) -> ProbeResult | None:
        self.probed.append((ip, community))
        entry = self.snmp.get(ip)
        if entry and entry[0] == community:
            return entry[1]
        return None

    async def snmp_mac_table(self, ip: str, community: str) -> dict[str, int] | None:
        entry = self.snmp.get(ip)
        if entry and entry[0] == community:
            return dict(self.tables.get(ip, {}))
        return None

    async def ssdp_types(self) -> dict[str, list[str]]:
        return dict(self.ssdp)

    def client_access_points(self) -> dict[str, str]:
        if self.net.controller != "unifi":
            return {}
        out = {}
        for n in self.net.devices():
            parent = self.net.nodes[n.parent]
            if parent.kind == "ap_unifi" and n.wireless and n.mac and parent.mac:
                out[n.mac] = parent.mac
        return out
