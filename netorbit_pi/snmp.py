"""Read-only SNMP helpers: identify switches and read their MAC address tables."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"
OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"
OID_BRIDGE_PORTS = "1.3.6.1.2.1.17.1.2.0"  # dot1dBaseNumPorts
OID_Q_FDB_PORT = "1.3.6.1.2.1.17.7.1.2.2.1.2"  # dot1qTpFdbPort.<vlan>.<mac>
OID_FDB_PORT = "1.3.6.1.2.1.17.4.3.1.2"  # dot1dTpFdbPort.<mac>

PROBE_TIMEOUT = 1.0
WALK_TIMEOUT = 2.0
MAX_TABLE = 4096


@dataclass
class ProbeResult:
    """What a host told us over SNMP."""

    descr: str | None
    name: str | None
    bridge: bool


class SnmpClient:
    """Thin wrapper so the rest of NetOrbit never touches pysnmp directly."""

    def __init__(self) -> None:
        self._engine = None
        self._mod: Any = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> bool:
        async with self._lock:
            if self._engine is not None:
                return True
            try:
                self._mod, self._engine = await asyncio.to_thread(_load)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("SNMP unavailable: %s", err)
                return False
            return True

    async def _target(self, host: str, timeout: float):
        return await self._mod.UdpTransportTarget.create((host, 161), timeout=timeout, retries=0)

    async def probe(self, host: str, community: str) -> ProbeResult | None:
        """Ask a host what it is. None when it does not answer with this community."""
        if not await self._ensure():
            return None
        m = self._mod
        try:
            target = await self._target(host, PROBE_TIMEOUT)
            err_ind, err_stat, _idx, binds = await m.get_cmd(
                self._engine, m.CommunityData(community, mpModel=1), target, m.ContextData(),
                m.ObjectType(m.ObjectIdentity(OID_SYS_DESCR)),
                m.ObjectType(m.ObjectIdentity(OID_SYS_NAME)),
                m.ObjectType(m.ObjectIdentity(OID_BRIDGE_PORTS)),
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("SNMP probe of %s failed: %s", host, err)
            return None
        if err_ind or err_stat:
            return None
        values = [_value(v) for _n, v in binds]
        descr, name, ports = (values + [None, None, None])[:3]
        bridge = isinstance(ports, int) and ports > 0
        return ProbeResult(descr=str(descr) if descr else None, name=str(name) if name else None, bridge=bridge)

    async def mac_table(self, host: str, community: str) -> dict[str, int] | None:
        """{mac: port} from the switch's forwarding table, or None when unreadable."""
        if not await self._ensure():
            return None
        table = await self._walk_fdb(host, community, OID_Q_FDB_PORT, vlan=True)
        if not table:
            table = await self._walk_fdb(host, community, OID_FDB_PORT, vlan=False)
        return table

    async def _walk_fdb(self, host: str, community: str, base: str, vlan: bool) -> dict[str, int] | None:
        m = self._mod
        base_t = tuple(int(x) for x in base.split("."))
        out: dict[str, int] = {}
        try:
            target = await self._target(host, WALK_TIMEOUT)
            async for err_ind, err_stat, _idx, binds in m.bulk_walk_cmd(
                self._engine, m.CommunityData(community, mpModel=1), target, m.ContextData(),
                0, 40, m.ObjectType(m.ObjectIdentity(base)), lexicographicMode=False,
            ):
                if err_ind or err_stat:
                    return out or None
                for name, val in binds:
                    oid = tuple(name)
                    if oid[: len(base_t)] != base_t:
                        continue
                    tail = oid[len(base_t) :]
                    mac_part = tail[-6:]
                    if len(mac_part) != 6 or (vlan and len(tail) < 7):
                        continue
                    port = _value(val)
                    if not isinstance(port, int) or port <= 0:
                        continue
                    out[":".join(f"{b:02x}" for b in mac_part)] = port
                    if len(out) >= MAX_TABLE:
                        return out
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("SNMP walk of %s %s failed: %s", host, base, err)
            return out or None
        return out


def _load():
    """Import pysnmp and build the engine off the event loop (both are slow)."""
    import pysnmp.hlapi.v3arch.asyncio as mod  # noqa: PLC0415
    from pysnmp.smi import view  # noqa: PLC0415

    engine = mod.SnmpEngine()
    # pysnmp reads its MIB files from disk on first use. Do that here, off the event loop,
    # the same way Home Assistant's own SNMP integration does.
    controller = view.MibViewController(engine.message_dispatcher.mib_instrum_controller.get_mib_builder())
    engine.cache["mibViewController"] = controller
    controller.mibBuilder.load_modules()
    return mod, engine


def _value(val: Any) -> Any:
    try:
        if val.__class__.__name__ in ("NoSuchObject", "NoSuchInstance", "EndOfMibView", "Null"):
            return None
        if hasattr(val, "prettyPrint") and val.__class__.__name__ in ("OctetString", "DisplayString"):
            return val.prettyPrint()
        return int(val)
    except (TypeError, ValueError):
        try:
            return val.prettyPrint()
        except Exception:  # noqa: BLE001
            return None
