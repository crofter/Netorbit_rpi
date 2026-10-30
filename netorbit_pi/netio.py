"""Everything NetOrbit asks the network, behind one small interface.

The coordinator only talks to a ``NetIO``. ``SystemNetIO`` uses the real network; the
simulator (``netorbit_pi.sim``) answers from a generated home network, which is what the
demo mode and the tests use.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Protocol

from .logic import parse_proc_arp
from .mdns import mdns_name
from .snmp import ProbeResult, SnmpClient
from .ssdp import ssdp_types_by_ip

_LOGGER = logging.getLogger(__name__)


class NetIO(Protocol):
    async def detect_privileges(self) -> bool | None: ...
    async def multiping(self, hosts: list[str], *, count: int, timeout: float, interval: float,
                        concurrent: int, privileged: bool) -> set[str]: ...
    async def read_arp(self) -> dict[str, str]: ...
    async def reverse_dns(self, ip: str) -> str | None: ...
    async def mdns_name(self, ip: str) -> str | None: ...
    async def vendors(self, macs: list[str]) -> dict[str, str | None] | None: ...
    async def snmp_probe(self, ip: str, community: str) -> ProbeResult | None: ...
    async def snmp_mac_table(self, ip: str, community: str) -> dict[str, int] | None: ...
    async def ssdp_types(self) -> dict[str, list[str]]: ...
    def client_access_points(self) -> dict[str, str]: ...


class SystemNetIO:
    """The real network: ICMP via icmplib, the kernel ARP table, DNS, Bonjour, SNMP, UPnP."""

    def __init__(self, arp_path: str = "/proc/net/arp") -> None:
        self._arp_path = arp_path
        self._snmp = SnmpClient()

    async def detect_privileges(self) -> bool | None:
        """Raw sockets when running as root (or with CAP_NET_RAW), else unprivileged ICMP."""
        try:
            from icmplib import SocketPermissionError, async_ping  # noqa: PLC0415
        except ImportError:
            _LOGGER.error("icmplib is not installed; ping based presence is unavailable")
            return None
        for privileged in (True, False):
            try:
                await async_ping("127.0.0.1", count=0, timeout=0, privileged=privileged)
            except SocketPermissionError:
                continue
            except Exception:  # noqa: BLE001
                return privileged
            return privileged
        _LOGGER.error(
            "NetOrbit cannot open an ICMP socket. Run it with CAP_NET_RAW (the systemd unit does) "
            "or allow unprivileged ping: sysctl net.ipv4.ping_group_range='0 2147483647'"
        )
        return None

    async def multiping(self, hosts, *, count, timeout, interval, concurrent, privileged) -> set[str]:
        from icmplib import async_multiping  # noqa: PLC0415

        if not hosts:
            return set()
        results = await async_multiping(
            hosts, count=count, interval=interval, timeout=timeout,
            concurrent_tasks=concurrent, privileged=privileged,
        )
        return {h.address for h in results if h.is_alive}

    async def read_arp(self) -> dict[str, str]:
        return await asyncio.to_thread(self._read_arp)

    def _read_arp(self) -> dict[str, str]:
        try:
            with open(self._arp_path, encoding="ascii") as fh:
                return parse_proc_arp(fh.read())
        except OSError:
            return {}

    async def reverse_dns(self, ip: str) -> str | None:
        def lookup() -> str | None:
            try:
                return socket.gethostbyaddr(ip)[0]
            except (OSError, UnicodeError):
                return None

        return await asyncio.to_thread(lookup)

    async def mdns_name(self, ip: str) -> str | None:
        return await mdns_name(ip)

    async def vendors(self, macs: list[str]) -> dict[str, str | None] | None:
        """{mac: raw maker name}, or None when the maker database is unavailable."""
        try:
            import aiooui  # noqa: PLC0415

            if not aiooui.is_loaded():
                await aiooui.async_load()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Vendor lookup unavailable: %s", err)
            return None
        return {mac: aiooui.get_vendor(mac[:8].upper()) for mac in macs}

    async def snmp_probe(self, ip: str, community: str) -> ProbeResult | None:
        return await self._snmp.probe(ip, community)

    async def snmp_mac_table(self, ip: str, community: str) -> dict[str, int] | None:
        return await self._snmp.mac_table(ip, community)

    async def ssdp_types(self) -> dict[str, list[str]]:
        return await ssdp_types_by_ip()

    def client_access_points(self) -> dict[str, str]:
        """{client mac: AP mac}. Home Assistant got this from UniFi/Netgear; nothing standalone yet."""
        return {}
