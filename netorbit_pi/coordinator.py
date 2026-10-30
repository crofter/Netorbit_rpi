"""Discovery, hub detection, layout inference, ping and status tracking for NetOrbit."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from . import manage
from .const import (
    CONF_AUTO_HUBS,
    CONF_DEVICES,
    CONF_DISCOVERY_INTERVAL,
    CONF_DISMISSED_HUBS,
    CONF_GATEWAY,
    CONF_GROUP_BY_TYPE,
    CONF_HUBS,
    CONF_IGNORE_RANDOM_MAC,
    CONF_IGNORED,
    CONF_MODE,
    CONF_PING_INTERVAL,
    CONF_SNMP_COMMUNITY,
    CONF_SNMP_ENABLED,
    CONF_STALE_HOURS,
    CONF_SUBNET,
    DEFAULT_AUTO_HUBS,
    DEFAULT_DISCOVERY_INTERVAL,
    DEFAULT_IGNORE_RANDOM_MAC,
    DEFAULT_MODE,
    DEFAULT_PING_INTERVAL,
    DEFAULT_SNMP_ENABLED,
    DEFAULT_STALE_HOURS,
    EVENT_DEVICE_RECOVERED,
    EVENT_DEVICE_STALE,
    EVENT_NEW_DEVICE,
    HUB_AP,
    HUB_ICONS,
    HUB_MESH,
    HUB_OTHER,
    HUB_ROUTER,
    HUB_SWITCH,
    MODE_AUTO,
    PLACE_CONTROLLER,
    PLACE_MANUAL,
    PLACE_NONE,
    PLACE_SNMP,
    ROOT_HUB_ID,
    SNMP_DEFAULT_COMMUNITY,
    SNMP_PROBE_INTERVAL_S,
    STATUS_ONLINE,
    STATUS_STALE,
    STORAGE_VERSION,
)
from .detect import NETWORK_VENDORS, Evidence, classify, pretty_vendor
from .logic import (
    compute_status,
    fallback_name,
    guess_icon_from,
    is_random_mac,
    normalize_mac,
    short_hostname,
    subnet_hosts,
)
from .mdns import mdns_label
from .netio import NetIO
from .store import ConfigStore, StateStore
from .topology import Topology, infer, macs_close

_LOGGER = logging.getLogger(__name__)

PING_COUNT = 2
PING_TIMEOUT = 1.5
SWEEP_TIMEOUT = 1.0
DNS_TIMEOUT = 2.0
DNS_REFRESH = timedelta(hours=12)
# Bump when the way names are looked up changes, so existing installs look again right away.
NAME_LOOKUP_VERSION = 2
AUTO_PLACEMENT_TTL = timedelta(days=7)
SNMP_CONCURRENCY = 32


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UpdateFailed(Exception):
    """One update could not run; the map keeps the last good data."""


@dataclass
class Node:
    """A hub or device shown on the map."""

    key: str
    kind: str  # "hub" or "device"
    name: str
    ip: str | None = None
    mac: str | None = None
    icon: str = "mdi:lan-connect"
    link: str | None = None  # parent hub id
    sleepy: bool = False
    status: str = "recent"
    last_seen: str | None = None
    first_seen: str | None = None
    hostname: str | None = None
    source: str = "discovered"  # discovered | manual | hub
    hub_type: str | None = None
    vendor: str | None = None
    placement: str = PLACE_NONE  # manual | controller | snmp | none
    hub_source: str | None = None  # for hubs: manual | snmp | controller | detected
    ip_changes: int = 0  # how often DHCP has moved it since NetOrbit started tracking

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NetOrbitData:
    """Everything the entities need after one update."""

    nodes: dict[str, Node] = field(default_factory=dict)
    new_devices: list[dict[str, Any]] = field(default_factory=list)
    suggested_hubs: list[dict[str, Any]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    last_scan: str | None = None


def options_with_defaults(config: ConfigStore) -> dict[str, Any]:
    """Merge options over defaults so older config files keep working."""
    opts = manage.copy_options(config.options)
    opts.setdefault(CONF_MODE, DEFAULT_MODE)
    opts.setdefault(CONF_PING_INTERVAL, DEFAULT_PING_INTERVAL)
    opts.setdefault(CONF_DISCOVERY_INTERVAL, DEFAULT_DISCOVERY_INTERVAL)
    opts.setdefault(CONF_STALE_HOURS, DEFAULT_STALE_HOURS)
    opts.setdefault(CONF_IGNORE_RANDOM_MAC, DEFAULT_IGNORE_RANDOM_MAC)
    opts.setdefault(CONF_AUTO_HUBS, DEFAULT_AUTO_HUBS)
    opts.setdefault(CONF_SNMP_ENABLED, DEFAULT_SNMP_ENABLED)
    opts.setdefault(CONF_SNMP_COMMUNITY, "")
    opts.setdefault(CONF_GROUP_BY_TYPE, True)
    opts.setdefault(CONF_HUBS, [])
    opts.setdefault(CONF_DEVICES, {})
    opts.setdefault(CONF_IGNORED, [])
    opts.setdefault(CONF_DISMISSED_HUBS, [])
    if not any(h.get("id") == ROOT_HUB_ID for h in opts[CONF_HUBS]):
        opts[CONF_HUBS] = [
            {"id": ROOT_HUB_ID, "name": "Router", "host": config.data[CONF_GATEWAY], "type": HUB_ROUTER, "parent": None},
            *opts[CONF_HUBS],
        ]
    return opts


class NetOrbitCoordinator:
    """Pings tracked hosts every interval and sweeps the subnet for new ones.

    Stands in for Home Assistant's DataUpdateCoordinator: ``data`` holds the latest map,
    ``async_refresh`` runs one update, ``run`` keeps updating on the ping interval, and
    listeners hear about every update and every event.
    """

    def __init__(self, config: ConfigStore, state: StateStore, net: NetIO) -> None:
        self.config = config
        self.net = net
        self.opts = options_with_defaults(config)
        self.update_interval = timedelta(seconds=self.opts[CONF_PING_INTERVAL])
        self.subnet: str = config.data[CONF_SUBNET]
        self.gateway: str = config.data[CONF_GATEWAY]
        self.data: NetOrbitData | None = None
        self.last_update_success = True
        self.last_error: str | None = None
        self._store = state
        self._refresh_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._update_listeners: list[Callable[[], None]] = []
        self._event_listeners: list[Callable[[str, dict[str, Any]], None]] = []
        self._seen: dict[str, dict[str, Any]] = {}
        self._initialized = False
        self._last_sweep: datetime | None = None
        self._last_snmp_probe: datetime | None = None
        self._prev_status: dict[str, str] = {}
        self._force_sweep = False
        self._force_probe = False
        self._privileged: bool | None = None
        self._sweep_lock = asyncio.Lock()
        self._tables: dict[str, dict[str, int]] = {}
        self._topology = Topology()
        self._controller_ap: dict[str, str] = {}
        self._suggestions: list[dict[str, Any]] = []
        self._community_asked: set[str] = set()  # IPs already asked with the custom SNMP community

    # ----- lifecycle -------------------------------------------------------

    async def async_load(self) -> None:
        """Load persisted state and work out how we are allowed to ping."""
        data = self._store.load()
        self._seen = data.get("seen", {})
        self._initialized = data.get("initialized", False)
        self._privileged = await self.net.detect_privileges()

    def add_listener(self, fn: Callable[[], None]) -> Callable[[], None]:
        """Call ``fn`` after every update. Returns a function that removes it."""
        self._update_listeners.append(fn)
        return lambda: self._update_listeners.remove(fn)

    def add_event_listener(self, fn: Callable[[str, dict[str, Any]], None]) -> Callable[[], None]:
        """Call ``fn(event, payload)`` for new devices and status changes."""
        self._event_listeners.append(fn)
        return lambda: self._event_listeners.remove(fn)

    def _fire(self, event: str, payload: dict[str, Any]) -> None:
        for fn in list(self._event_listeners):
            try:
                fn(event, payload)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Event listener failed")

    async def async_refresh(self) -> None:
        """Run one update now. Failures are logged and keep the previous map."""
        async with self._refresh_lock:
            try:
                self.data = await self._async_update_data()
                self.last_update_success = True
                self.last_error = None
            except UpdateFailed as err:
                if self.last_update_success:
                    _LOGGER.error("NetOrbit update failed: %s", err)
                self.last_update_success = False
                self.last_error = str(err)
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during update")
                self.last_update_success = False
                self.last_error = f"Unexpected error: {err}"
        for fn in list(self._update_listeners):
            try:
                fn()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Update listener failed")

    async def run(self) -> None:
        """Update forever on the ping interval. ``wake`` starts the next update early."""
        while True:
            await self.async_refresh()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), self.update_interval.total_seconds())
            except (TimeoutError, asyncio.TimeoutError):
                pass

    def wake(self) -> None:
        self._wake.set()

    def shutdown(self) -> None:
        self._store.flush()

    def apply_options(self) -> None:
        """Use new options without restarting."""
        old_comm = self.opts.get(CONF_SNMP_COMMUNITY)
        old_net = (self.subnet, self.gateway)
        self.opts = options_with_defaults(self.config)
        self.subnet = self.config.data[CONF_SUBNET]
        self.gateway = self.config.data[CONF_GATEWAY]
        self.update_interval = timedelta(seconds=self.opts[CONF_PING_INTERVAL])
        if self.opts.get(CONF_SNMP_COMMUNITY) != old_comm or (self.subnet, self.gateway) != old_net:
            self._force_probe = True
            self._force_sweep = True

    def forget_hub_address(self, hub_id: str) -> None:
        """Stop following a hub's old MAC, for when a person gives it a new address."""
        rec = self._seen.get(f"hub:{hub_id}")
        if rec:
            rec.pop("mac", None)
            self._save()

    def save_options(self, opts: dict[str, Any]) -> None:
        """Save a changed copy of the options and start using it."""
        self.config.update(options=opts)
        self.apply_options()

    def request_sweep(self, probe: bool = False) -> None:
        """Run a discovery sweep (and optionally an SNMP probe) on the next update."""
        self._force_sweep = True
        self._force_probe = self._force_probe or probe

    def _save(self) -> None:
        self._store.delay_save(lambda: {"version": STORAGE_VERSION, "seen": self._seen, "initialized": self._initialized}, 10)

    # ----- update ------------------------------------------------------------

    async def _async_update_data(self) -> NetOrbitData:
        now = utcnow()
        sweep_due = (
            self._force_sweep
            or self._last_sweep is None
            or now - self._last_sweep >= timedelta(seconds=self.opts[CONF_DISCOVERY_INTERVAL])
        )
        if self._privileged is None:
            raise UpdateFailed("ICMP sockets are not permitted on this system")
        try:
            if sweep_due:
                await self._sweep(now)
            alive = await self._ping_tracked()
        except OSError as err:
            raise UpdateFailed(f"Network error: {err}") from err

        for key, ip in alive:
            rec = self._seen.setdefault(key, {"first_seen": now.isoformat()})
            rec["last_seen"] = now.isoformat()
            if ip:
                rec["ip"] = ip
        self._controller_ap = self.net.client_access_points()
        self._save()
        data = self._build(now, {k for k, _ in alive})
        self._fire_transitions(data)
        return data

    async def _sweep(self, now: datetime) -> None:
        """Ping every address, read ARP, then work out hubs and layout."""
        async with self._sweep_lock:
            self._force_sweep = False
            hosts = subnet_hosts(self.subnet)
            replied = await self.net.multiping(
                hosts, count=1, timeout=SWEEP_TIMEOUT, interval=0, concurrent=128, privileged=self._privileged
            )
            arp = await self.net.read_arp()
            ignored = set(self.opts[CONF_IGNORED])
            first_run = not self._initialized
            fresh: list[str] = []
            host_set = self._subnet_set(hosts)

            # One IP per MAC. Right after a DHCP change the ARP table can still hold the old
            # address too, so prefer the address that actually answered.
            mac_ip: dict[str, str] = {}
            for ip, mac in sorted(arp.items(), key=lambda kv: _ip_sort(kv[0])):
                if ip not in host_set:
                    continue
                if mac not in mac_ip or (ip in replied and mac_ip[mac] not in replied):
                    mac_ip[mac] = ip

            opts = manage.copy_options(self.opts)
            changed = self._follow_dhcp(opts, mac_ip, arp, now)

            for mac, ip in sorted(mac_ip.items(), key=lambda kv: _ip_sort(kv[1])):
                if self.opts[CONF_IGNORE_RANDOM_MAC] and is_random_mac(mac):
                    continue
                key = mac
                rec = self._seen.get(key)
                if rec is None:
                    rec = self._seen[key] = {"first_seen": now.isoformat(), "discovered": True}
                    if key not in ignored and not self._is_hub_address(mac, ip):
                        fresh.append(key)
                rec["mac"] = mac
                if rec.get("ip") and rec["ip"] != ip:
                    rec["ip_changes"] = rec.get("ip_changes", 0) + 1
                    rec["ip_changed_at"] = now.isoformat()
                rec["ip"] = ip
                if ip in replied:
                    rec["last_seen"] = now.isoformat()
            if changed:
                self._commit(opts)

            await self._lookup_vendors()
            await self._resolve_names(now)
            if self.opts[CONF_SNMP_ENABLED]:
                await self._snmp_probe(now, replied | set(arp))
            await self._detect_hubs()
            if self.opts[CONF_SNMP_ENABLED] and self.opts.get(CONF_SNMP_COMMUNITY):
                # Gear that detection just suggested may only answer to the custom community.
                # Ask it now, then look again, so it doesn't wait for the next sweep.
                before = set(self._community_asked)
                await self._snmp_probe(now, replied | set(arp))
                if self._community_asked != before:
                    await self._detect_hubs()
            # Read tables after detection so switches found in this sweep count straight away.
            self._tables = await self._read_tables() if self.opts[CONF_SNMP_ENABLED] else {}
            self._infer_layout(now)

            self._last_sweep = now
            self._initialized = True
            self._save()

            if not first_run:
                for key in fresh:
                    rec = self._seen[key]
                    self._fire(
                        EVENT_NEW_DEVICE,
                        {"mac": rec.get("mac"), "ip": rec.get("ip"), "name": self._discovered_name(key, rec)},
                    )

    def _follow_dhcp(self, opts: dict[str, Any], mac_ip: dict[str, str], arp: dict[str, str], now: datetime) -> bool:
        """Keep hubs and hand-added devices pointed at the right address when DHCP moves them.

        * A hub with a known MAC follows that MAC to its new IP.
        * A hub without a known MAC learns it from its IP once.
        * A device added by IP ("ip:x") is re-keyed to its MAC as soon as we learn it, so its
          name, icon and placement survive the next DHCP change.
        """
        changed = False
        for hub in opts[CONF_HUBS]:
            seen_key = f"hub:{hub['id']}"
            rec = self._seen.setdefault(seen_key, {"first_seen": now.isoformat()})
            mac = hub.get("mac") or rec.get("mac")
            if mac:
                rec["mac"] = mac
                new_ip = mac_ip.get(mac)
                if new_ip and hub.get("host") and new_ip != hub["host"]:
                    _LOGGER.info("NetOrbit: %s moved from %s to %s", hub.get("name"), hub["host"], new_ip)
                    hub["host"] = new_ip
                    rec["ip_changes"] = rec.get("ip_changes", 0) + 1
                    rec["ip_changed_at"] = now.isoformat()
                    changed = True
                if not hub.get("mac"):
                    hub["mac"] = mac
                    changed = True
            elif hub.get("host") and arp.get(hub["host"]):
                rec["mac"] = arp[hub["host"]]
                hub["mac"] = rec["mac"]
                changed = True

        for key in [k for k in opts[CONF_DEVICES] if k.startswith("ip:")]:
            ip = key[3:]
            mac = arp.get(ip)
            if not mac or (self.opts[CONF_IGNORE_RANDOM_MAC] and is_random_mac(mac)):
                continue
            cfg = opts[CONF_DEVICES].pop(key)
            cfg.pop("host", None)
            merged = {**cfg, **opts[CONF_DEVICES].get(mac, {})}
            opts[CONF_DEVICES][mac] = merged
            old = self._seen.pop(key, {})
            rec = self._seen.setdefault(mac, {"first_seen": old.get("first_seen") or now.isoformat(), "discovered": True})
            for f in ("last_seen", "first_seen"):
                if old.get(f) and (not rec.get(f) or (f == "last_seen" and old[f] > rec[f])):
                    rec[f] = old[f]
            if key in opts[CONF_IGNORED]:
                opts[CONF_IGNORED].remove(key)
                opts[CONF_IGNORED].append(mac)
            changed = True
        return changed

    def _commit(self, opts: dict[str, Any]) -> None:
        """Save options NetOrbit changed itself."""
        self.opts = opts
        self.config.update(options=opts)

    def _subnet_set(self, hosts: list[str]) -> set[str]:
        cached = getattr(self, "_hosts_cache", None)
        if cached is None or cached[0] != self.subnet:
            cached = (self.subnet, set(hosts))
            self._hosts_cache = cached
        return cached[1]

    async def _lookup_vendors(self) -> None:
        todo = [rec for rec in self._seen.values() if rec.get("mac") and "vendor" not in rec]
        if not todo:
            return
        found = await self.net.vendors([rec["mac"] for rec in todo])
        if found is None:
            return
        for rec in todo:
            mac = rec["mac"]
            rec["vendor"] = None if is_random_mac(mac) else pretty_vendor(found.get(mac))

    async def _resolve_names(self, now: datetime) -> None:
        """Reverse DNS, then Bonjour, for devices we have no recent hostname for."""
        todo = []
        for key, rec in self._seen.items():
            ip = rec.get("ip")
            checked = rec.get("dns_checked")
            if not ip:
                continue
            fresh = rec.get("dns_version") == NAME_LOOKUP_VERSION
            if fresh and checked and now - datetime.fromisoformat(checked) < DNS_REFRESH:
                continue
            todo.append((key, ip))
        sem = asyncio.Semaphore(16)

        async def one(key: str, ip: str) -> None:
            async with sem:
                try:
                    name = await asyncio.wait_for(self.net.reverse_dns(ip), DNS_TIMEOUT)
                except (TimeoutError, asyncio.TimeoutError):
                    name = None
                host = short_hostname(name)
                if not host:
                    # Many routers don't publish local names. Ask the device itself over Bonjour.
                    host = mdns_label(await self.net.mdns_name(ip))
                rec = self._seen[key]
                rec["dns_checked"] = now.isoformat()
                rec["dns_version"] = NAME_LOOKUP_VERSION
                if host:
                    rec["hostname"] = host

        await asyncio.gather(*(one(k, ip) for k, ip in todo))

    # ----- SNMP ----------------------------------------------------------------

    def _communities(self) -> list[str]:
        custom = (self.opts.get(CONF_SNMP_COMMUNITY) or "").strip()
        out = [custom] if custom else []
        if SNMP_DEFAULT_COMMUNITY not in out:
            out.append(SNMP_DEFAULT_COMMUNITY)
        return out

    def _community_hosts(self, hub_ips: set[str]) -> set[str]:
        """IPs allowed to receive a custom SNMP community: hubs, suggested hubs, and network-gear makers."""
        out = {ip for ip in hub_ips if ip}
        out |= {s.get("ip") for s in self._suggestions if s.get("ip")}
        for rec in self._seen.values():
            vendor = (rec.get("vendor") or "").lower()
            if rec.get("ip") and any(v in vendor for v in NETWORK_VENDORS):
                out.add(rec["ip"])
        return out

    async def _snmp_probe(self, now: datetime, live_ips: set[str]) -> None:
        """Hourly: ask every live host whether it speaks SNMP and keeps a MAC table."""
        probe_due = (
            self._force_probe
            or self._last_snmp_probe is None
            or now - self._last_snmp_probe >= timedelta(seconds=SNMP_PROBE_INTERVAL_S)
        )
        by_ip = {rec.get("ip"): key for key, rec in self._seen.items() if rec.get("ip")}
        hub_hosts = {h.get("host"): f"hub:{h['id']}" for h in self.opts[CONF_HUBS] if h.get("host")}
        trusted = self._community_hosts(set(hub_hosts))
        # Devices that just became hubs or suggestions are asked with the custom community on the
        # next sweep instead of waiting an hour.
        fresh = (trusted - self._community_asked) & (live_ips | set(hub_hosts)) if self.opts.get(CONF_SNMP_COMMUNITY) else set()
        if probe_due or fresh:
            targets = sorted(live_ips | set(hub_hosts)) if probe_due else sorted(fresh)
            if probe_due:
                self._force_probe = False
                self._last_snmp_probe = now
            sem = asyncio.Semaphore(SNMP_CONCURRENCY)

            async def probe(ip: str) -> None:
                async with sem:
                    # SNMP v2c sends the community in clear text. Your own community only goes to
                    # hubs and likely network gear; everything else is only asked with the factory default.
                    comms = self._communities() if ip in trusted else [SNMP_DEFAULT_COMMUNITY]
                    for i, comm in enumerate(comms):
                        res = await self.net.snmp_probe(ip, comm)
                        if res:
                            for key in {by_ip.get(ip), hub_hosts.get(ip)} - {None}:
                                rec = self._seen.setdefault(key, {"first_seen": now.isoformat()})
                                rec["snmp"] = {"descr": res.descr, "name": res.name, "bridge": res.bridge, "community": "custom" if (comm != SNMP_DEFAULT_COMMUNITY) else "default"}
                            return
                    for key in {by_ip.get(ip), hub_hosts.get(ip)} - {None}:
                        self._seen.get(key, {}).pop("snmp", None)

            await asyncio.gather(*(probe(ip) for ip in targets))
            self._community_asked |= {ip for ip in targets if ip in trusted}

    async def _read_tables(self) -> dict[str, dict[str, int]]:
        """Read MAC tables from every hub that is a bridge."""
        tables: dict[str, dict[str, int]] = {}
        for hub in self.opts[CONF_HUBS]:
            host = hub.get("host")
            info = self._snmp_info(hub)
            if not host or not info or not info.get("bridge"):
                continue
            comm = self._communities()[0] if info.get("community") == "custom" else SNMP_DEFAULT_COMMUNITY
            table = await self.net.snmp_mac_table(host, comm)
            if table:
                tables[hub["id"]] = table
        return tables

    def _snmp_info(self, hub: dict[str, Any]) -> dict[str, Any] | None:
        info = (self._seen.get(f"hub:{hub['id']}") or {}).get("snmp")
        if info:
            return info
        mac = hub.get("mac") or (self._seen.get(f"hub:{hub['id']}") or {}).get("mac")
        return (self._seen.get(mac) or {}).get("snmp") if mac else None

    # ----- hub detection ------------------------------------------------------

    def _hub_macs(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for hub in self.opts[CONF_HUBS]:
            mac = hub.get("mac") or (self._seen.get(f"hub:{hub['id']}") or {}).get("mac")
            if mac:
                out[hub["id"]] = mac
        return out

    def _is_hub_address(self, mac: str | None, ip: str | None) -> bool:
        hub_macs = set(self._hub_macs().values())
        hub_ips = {h.get("host") for h in self.opts[CONF_HUBS] if h.get("host")}
        return bool((mac and mac in hub_macs) or (ip and ip in hub_ips))

    def _hub_for_mac(self, mac: str | None) -> str | None:
        """The hub with this MAC. A near match (a radio MAC a few off the LAN MAC) only counts
        when exactly one hub is that close; access points bought together often have
        consecutive MACs and must not be confused."""
        if not mac:
            return None
        hubs = self._hub_macs()
        for hub_id, hmac in hubs.items():
            if hmac == mac:
                return hub_id
        near = [hub_id for hub_id, hmac in hubs.items() if macs_close(hmac, mac)]
        return near[0] if len(near) == 1 else None

    async def _detect_hubs(self) -> None:
        """Add obvious hubs, and collect less certain ones as suggestions."""
        ssdp = await self.net.ssdp_types()
        dismissed = set(self.opts[CONF_DISMISSED_HUBS])
        ignored = set(self.opts[CONF_IGNORED])
        adds: list[dict[str, Any]] = []
        suggestions: list[dict[str, Any]] = []

        candidates: dict[str, dict[str, Any]] = {}
        for key, rec in self._seen.items():
            if key.startswith("hub:") or not rec.get("mac") or not rec.get("discovered"):
                continue
            candidates[rec["mac"]] = rec
        # APs that a controller reports clients against are hubs by definition.
        controller_aps = set(self.net.client_access_points().values())

        for mac, rec in candidates.items():
            if mac in dismissed or mac in ignored or self._is_hub_address(mac, rec.get("ip")):
                continue
            snmp = rec.get("snmp") or {}
            ev = Evidence(
                mac=mac, ip=rec.get("ip"), hostname=rec.get("hostname"), vendor=rec.get("vendor"),
                snmp_bridge=bool(snmp.get("bridge")), snmp_descr=snmp.get("descr"),
                ssdp_types=ssdp.get(rec.get("ip") or "", []),
            )
            verdict = classify(ev)
            if mac in controller_aps:
                verdict.score += 3
                verdict.hub_type = verdict.hub_type or HUB_AP
                verdict.reasons.append("clients connect through it")
            if rec.get("ip") == self.gateway:
                continue
            name = self._hub_name(mac, rec, verdict.hub_type)
            if verdict.auto and self.opts[CONF_AUTO_HUBS]:
                source = "snmp" if ev.snmp_bridge else "controller" if mac in controller_aps else "detected"
                adds.append({"name": name, "type": verdict.hub_type, "host": rec.get("ip"), "mac": mac, "source": source})
            elif verdict.suggest or (verdict.auto and not self.opts[CONF_AUTO_HUBS]):
                suggestions.append({
                    "key": mac, "mac": mac.upper(), "ip": rec.get("ip"), "name": name,
                    "type": verdict.hub_type, "reasons": verdict.reasons,
                })
        self._suggestions = suggestions

        if adds:
            opts = manage.copy_options(self.opts)
            added = []
            for a in adds:
                try:
                    manage.add_hub(opts, a["name"], a["type"], None, a["host"], a["mac"], source=a["source"])
                    added.append(a["name"])
                except manage.ManageError:
                    continue
            if added:
                _LOGGER.info("NetOrbit added hubs: %s", ", ".join(added))
                self._commit(opts)

    def _hub_name(self, mac: str, rec: dict[str, Any], hub_type: str | None) -> str:
        snmp_name = (rec.get("snmp") or {}).get("name")
        if snmp_name:
            return snmp_name
        if rec.get("hostname"):
            return rec["hostname"]
        kind = {HUB_AP: "access point", HUB_SWITCH: "switch", HUB_MESH: "mesh node"}.get(hub_type or "", "hub")
        vendor = rec.get("vendor")
        return f"{vendor} {kind} {mac[-5:].upper()}" if vendor else f"{kind.capitalize()} {mac[-5:].upper()}"

    # ----- layout ----------------------------------------------------------------

    def _infer_layout(self, now: datetime) -> None:
        """Turn switch tables into hub parents and device placements, and remember them."""
        hub_macs = self._hub_macs()
        gateway_mac = hub_macs.get(ROOT_HUB_ID)
        device_macs = {k: r["mac"] for k, r in self._seen.items() if r.get("mac") and not k.startswith("hub:")}
        leaf = {h["id"] for h in self.opts[CONF_HUBS] if h.get("type") in (HUB_AP, HUB_MESH, HUB_OTHER) and h["id"] not in self._tables}
        self._topology = infer(self._tables, hub_macs, gateway_mac, device_macs, ROOT_HUB_ID, leaf_hubs=leaf)
        stamp = now.isoformat()
        for key, hub_id in self._topology.device_hub.items():
            self._seen[key]["auto"] = {"hub": hub_id, "source": PLACE_SNMP, "at": stamp}

    def _auto_placement(self, key: str, rec: dict[str, Any], now: datetime) -> tuple[str | None, str]:
        mac = rec.get("mac")
        ap = self._controller_ap.get(mac) if mac else None
        hub = self._hub_for_mac(ap)
        if hub:
            rec["auto"] = {"hub": hub, "source": PLACE_CONTROLLER, "at": now.isoformat()}
            return hub, PLACE_CONTROLLER
        auto = rec.get("auto")
        if auto and auto.get("at") and now - datetime.fromisoformat(auto["at"]) < AUTO_PLACEMENT_TTL:
            return auto.get("hub"), auto.get("source", PLACE_SNMP)
        return None, PLACE_NONE

    # ----- ping ------------------------------------------------------------------

    async def _ping_tracked(self) -> list[tuple[str, str | None]]:
        """Ping hubs and tracked devices. Returns [(key, ip)] that answered."""
        targets: dict[str, str] = {}
        for hub in self.opts[CONF_HUBS]:
            if hub.get("host"):
                targets[f"hub:{hub['id']}"] = hub["host"]
        for key, ip in self._tracked_targets().items():
            if ip:
                targets[key] = ip
        if not targets:
            return []
        by_ip: dict[str, list[str]] = {}
        for key, ip in targets.items():
            by_ip.setdefault(ip, []).append(key)
        replied = await self.net.multiping(
            list(by_ip), count=PING_COUNT, interval=0.3, timeout=PING_TIMEOUT,
            concurrent=64, privileged=self._privileged,
        )
        alive: list[tuple[str, str | None]] = []
        for ip in sorted(replied):
            for key in by_ip.get(ip, []):
                alive.append((key, ip))
        return alive

    # ----- model ---------------------------------------------------------------

    def _tracked_targets(self) -> dict[str, str | None]:
        """Keys of devices being tracked and the IP to ping for each."""
        out: dict[str, str | None] = {}
        devices = self.opts[CONF_DEVICES]
        ignored = set(self.opts[CONF_IGNORED])
        if self.opts[CONF_MODE] == MODE_AUTO:
            for key, rec in self._seen.items():
                if rec.get("discovered") and key not in ignored:
                    if self.opts[CONF_IGNORE_RANDOM_MAC] and is_random_mac(rec.get("mac")):
                        continue
                    out[key] = rec.get("ip")
        for key, cfg in devices.items():
            if key in ignored or not cfg.get("tracked", True):
                out.pop(key, None)
                continue
            out[key] = (self._seen.get(key) or {}).get("ip") or cfg.get("host")
        # Hubs are drawn as hubs, never as devices too.
        for key in list(out):
            rec = self._seen.get(key) or {}
            if self._is_hub_address(rec.get("mac"), out[key]):
                out.pop(key)
        return out

    def _discovered_name(self, key: str, rec: dict[str, Any]) -> str:
        mac = rec.get("mac")
        if rec.get("hostname"):
            return rec["hostname"]
        vendor = rec.get("vendor")
        if vendor and mac:
            return f"{vendor} {mac[-5:].upper()}"
        return fallback_name(mac, rec.get("ip"))

    def _build(self, now: datetime, alive_keys: set[str]) -> NetOrbitData:
        stale_after = timedelta(hours=self.opts[CONF_STALE_HOURS])
        hub_ids = {h["id"] for h in self.opts[CONF_HUBS]}
        nodes: dict[str, Node] = {}
        sources: set[str] = set()

        for hub in self.opts[CONF_HUBS]:
            key = f"hub:{hub['id']}"
            rec = self._seen.get(key, {})
            if hub["id"] == ROOT_HUB_ID:
                parent = None
            elif hub.get("parent_locked") and hub.get("parent") in hub_ids:
                parent = hub["parent"]
            elif self._topology.hub_parent.get(hub["id"]) in hub_ids:
                parent = self._topology.hub_parent[hub["id"]]
            elif hub.get("parent") in hub_ids:
                parent = hub["parent"]
            else:
                parent = ROOT_HUB_ID
            if parent == hub["id"]:
                parent = ROOT_HUB_ID
            nodes[key] = Node(
                key=key, kind="hub", name=hub.get("name") or hub["id"], ip=hub.get("host"),
                mac=hub.get("mac") or rec.get("mac"),
                icon=hub.get("icon") or HUB_ICONS.get(hub.get("type", HUB_OTHER), "mdi:lan"),
                link=parent, status=self._status(key in alive_keys, rec, now, stale_after),
                last_seen=rec.get("last_seen"), first_seen=rec.get("first_seen"),
                source="hub", hub_type=hub.get("type", HUB_OTHER), hub_source=hub.get("source", "manual"),
                ip_changes=rec.get("ip_changes", 0),
                vendor=(self._seen.get(hub.get("mac") or "") or {}).get("vendor"),
            )
        if self._tables:
            sources.add(PLACE_SNMP)
        if self._controller_ap:
            sources.add(PLACE_CONTROLLER)

        devices = self.opts[CONF_DEVICES]
        for key, ip in self._tracked_targets().items():
            rec = self._seen.get(key, {})
            cfg = devices.get(key, {})
            name = cfg.get("name") or self._discovered_name(key, rec)
            if cfg.get("hub") in hub_ids:
                hub, placement = cfg["hub"], PLACE_MANUAL
                sources.add(PLACE_MANUAL)
            else:
                hub, placement = self._auto_placement(key, rec, now)
                if hub not in hub_ids:
                    hub, placement = ROOT_HUB_ID, PLACE_NONE
            nodes[key] = Node(
                key=key, kind="device", name=name, ip=ip, mac=rec.get("mac") or cfg.get("mac"),
                icon=cfg.get("icon") or guess_icon_from(f"{name} {rec.get('hostname') or ''}", rec.get("vendor")),
                link=hub, sleepy=bool(cfg.get("sleepy", False)),
                status=self._status(key in alive_keys, rec, now, stale_after),
                last_seen=rec.get("last_seen"), first_seen=rec.get("first_seen"),
                hostname=rec.get("hostname"), source="manual" if key in devices else "discovered",
                vendor=rec.get("vendor"), placement=placement, ip_changes=rec.get("ip_changes", 0),
            )

        new_devices = []
        if self.opts[CONF_MODE] != MODE_AUTO:
            ignored = set(self.opts[CONF_IGNORED])
            for key, rec in self._seen.items():
                if not rec.get("discovered") or key in nodes or key in ignored:
                    continue
                if self.opts[CONF_IGNORE_RANDOM_MAC] and is_random_mac(rec.get("mac")):
                    continue
                if self._is_hub_address(rec.get("mac"), rec.get("ip")):
                    continue
                new_devices.append({
                    "key": key, "mac": rec.get("mac"), "ip": rec.get("ip"),
                    "name": self._discovered_name(key, rec), "first_seen": rec.get("first_seen"),
                    "last_seen": rec.get("last_seen"), "vendor": rec.get("vendor"),
                })
            new_devices.sort(key=lambda d: d.get("first_seen") or "", reverse=True)

        hub_mac_set = set(self._hub_macs().values())
        suggestions = [s for s in self._suggestions if s["key"] not in hub_mac_set and s["key"] not in self.opts[CONF_DISMISSED_HUBS]]

        return NetOrbitData(
            nodes=nodes, new_devices=new_devices, suggested_hubs=suggestions, sources=sorted(sources),
            last_scan=self._last_sweep.isoformat() if self._last_sweep else None,
        )

    @staticmethod
    def _status(online: bool, rec: dict[str, Any], now: datetime, stale_after: timedelta) -> str:
        last = rec.get("last_seen")
        first = rec.get("first_seen")
        return compute_status(
            online,
            datetime.fromisoformat(last) if last else None,
            datetime.fromisoformat(first) if first else now,
            now,
            stale_after,
        )

    def _fire_transitions(self, data: NetOrbitData) -> None:
        for key, node in data.nodes.items():
            prev = self._prev_status.get(key)
            if prev is not None and prev != node.status:
                payload = {"name": node.name, "ip": node.ip, "mac": node.mac, "kind": node.kind, "last_seen": node.last_seen}
                if node.status == STATUS_STALE and not node.sleepy:
                    self._fire(EVENT_DEVICE_STALE, payload)
                elif prev == STATUS_STALE and node.status == STATUS_ONLINE:
                    self._fire(EVENT_DEVICE_RECOVERED, payload)
            self._prev_status[key] = node.status

    # ----- lookups used by the API and the settings page --------------------

    def known_device(self, key: str) -> dict[str, Any] | None:
        return self._seen.get(key)

    def find_key(self, mac: str | None = None, ip: str | None = None) -> str | None:
        """Find a device key by MAC or IP."""
        mac = normalize_mac(mac)
        if mac and mac in self._seen:
            return mac
        if ip:
            for key, rec in self._seen.items():
                if rec.get("ip") == ip and not key.startswith("hub:"):
                    return key
        return mac

    def resolve_device(self, ref: str) -> str | None:
        """Find a device from a MAC, IP, key or name (exact name first, then a unique partial match)."""
        if not ref:
            return None
        ref = str(ref).strip()
        key = self.find_key(mac=ref) if normalize_mac(ref) else None
        if key and key in self._seen:
            return key
        key = self.find_key(ip=ref)
        if key and key in self._seen:
            return key
        if ref in self._seen:
            return ref
        nodes = self.data.nodes if self.data else {}
        low = ref.lower()
        exact = [k for k, n in nodes.items() if n.kind == "device" and n.name.lower() == low]
        if len(exact) == 1:
            return exact[0]
        names = {k: self._discovered_name(k, r).lower() for k, r in self._seen.items() if not k.startswith("hub:")}
        exact = [k for k, n in names.items() if n == low]
        if len(exact) == 1:
            return exact[0]
        partial = [k for k, n in nodes.items() if n.kind == "device" and low in n.name.lower()]
        return partial[0] if len(partial) == 1 else None

    def resolve_hub(self, ref: str) -> str | None:
        """Find a hub from its id, name, IP or MAC."""
        if not ref:
            return None
        ref = str(ref).strip().removeprefix("hub:")
        low = ref.lower()
        mac = normalize_mac(ref)
        for hub in self.opts[CONF_HUBS]:
            if hub["id"] == ref or (hub.get("name") or "").lower() == low or hub.get("host") == ref:
                return hub["id"]
            if mac and macs_close(hub.get("mac") or (self._seen.get(f"hub:{hub['id']}") or {}).get("mac"), mac):
                return hub["id"]
        partial = [h["id"] for h in self.opts[CONF_HUBS] if low in (h.get("name") or "").lower()]
        return partial[0] if len(partial) == 1 else None

    def match_addresses(self, macs: list[str], ips: list[str]) -> tuple[list[str], list[str]]:
        """Map pasted MACs/IPs to known device keys. Returns (matched keys, unmatched addresses)."""
        matched: list[str] = []
        unmatched: list[str] = []
        for mac in macs:
            key = mac if mac in self._seen else None
            if key is None:
                # A radio MAC in the list might be one byte off the device's real MAC.
                for k, rec in self._seen.items():
                    if not k.startswith("hub:") and macs_close(rec.get("mac"), mac, spread=0):
                        key = k
                        break
            if key and key not in matched and not self._is_hub_address(key, None):
                matched.append(key)
            elif key is None:
                unmatched.append(mac.upper())
        for ip in ips:
            key = self.find_key(ip=ip)
            if key and key in self._seen and key not in matched and not self._is_hub_address(None, ip):
                matched.append(key)
            elif not key:
                unmatched.append(ip)
        return matched, unmatched

    def seed_device(self, mac: str | None, ip: str | None) -> str:
        """Remember a device given by hand (for example from a pasted list) even if no sweep found it yet."""
        key = normalize_mac(mac) or f"ip:{ip}"
        rec = self._seen.setdefault(key, {"first_seen": utcnow().isoformat(), "discovered": bool(normalize_mac(mac))})
        if normalize_mac(mac):
            rec["mac"] = normalize_mac(mac)
        if ip:
            rec["ip"] = ip
        return key


def _ip_sort(ip: str) -> tuple[int, ...]:
    return tuple(int(p) for p in ip.split("."))
