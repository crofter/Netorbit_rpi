"""The NetOrbit service: the coordinator, the web page and the JSON API in one process."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from pathlib import Path
import re
from typing import Any

from . import actions
from .auth import COOKIE, SESSION_DAYS, Sessions, check_password, hash_password
from .const import (
    CONF_AUTO_HUBS,
    CONF_DISCOVERY_INTERVAL,
    CONF_GATEWAY,
    CONF_GROUP_BY_TYPE,
    CONF_HISTORY_DAYS,
    CONF_HUBS,
    CONF_IGNORE_RANDOM_MAC,
    CONF_MODE,
    CONF_NOTIFY_EVENTS,
    CONF_NTFY_URL,
    CONF_PING_INTERVAL,
    CONF_SNMP_COMMUNITY,
    CONF_SNMP_ENABLED,
    CONF_STALE_HOURS,
    CONF_SUBNET,
    CONF_WEBHOOK_URL,
    DEFAULT_HISTORY_DAYS,
    EVENT_DEVICE_RECOVERED,
    EVENT_DEVICE_STALE,
    EVENT_NEW_DEVICE,
    HUB_TYPES,
    MAX_HOSTS,
    MIN_PING_INTERVAL,
    MODES,
    ROOT_HUB_ID,
    STATUSES,
    VERSION,
)
from .coordinator import NetOrbitCoordinator, Node, options_with_defaults
from .history import History
from .httpserver import EventStream, HTTPError, HTTPServer, Request, Response, Router
from .logic import is_random_mac, slugify_id, validate_subnet
from .manage import ManageError
from .netdetect import detect_network
from .netio import NetIO
from .notify import DEFAULT_NOTIFY_EVENTS, Notifier
from .store import ConfigStore, StateStore

_LOGGER = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
PASSWORD_KEY = "password_hash"
ENTRY_ID = "standalone"
EVENTS = [EVENT_NEW_DEVICE, EVENT_DEVICE_STALE, EVENT_DEVICE_RECOVERED]
PUBLIC_PATHS = {"/login", "/api/login", "/api/health", "/favicon.svg"}


# ----- states in the shape the card expects --------------------------------------

def _entity_ids(nodes: dict[str, Node]) -> dict[str, str]:
    """A stable, unique sensor id per node, like Home Assistant would have made."""
    out: dict[str, str] = {}
    used: set[str] = {"sensor.netorbit_summary"}
    for key in sorted(nodes):
        base = "sensor.netorbit_" + slugify_id(key)
        eid, n = base, 2
        while eid in used:
            eid, n = f"{base}_{n}", n + 1
        used.add(eid)
        out[key] = eid
    return out


def build_states(coord: NetOrbitCoordinator) -> list[dict[str, Any]]:
    """One state per hub and device plus a summary, with the attributes sensor.py set."""
    data = coord.data
    nodes = data.nodes if data else {}
    ids = _entity_ids(nodes)
    states = []
    for key, node in nodes.items():
        states.append({
            "entity_id": ids[key],
            "state": node.status,
            "attributes": {
                "netorbit": True, "kind": node.kind, "label": node.name, "friendly_name": node.name,
                "icon": node.icon, "ip": node.ip, "mac": node.mac.upper() if node.mac else None,
                "link": node.link if node.link is None else f"hub:{node.link}", "node_key": node.key,
                "hub_type": node.hub_type, "sleepy": node.sleepy, "last_seen": node.last_seen,
                "first_seen": node.first_seen, "hostname": node.hostname, "source": node.source,
                "vendor": node.vendor, "placement": node.placement if node.kind == "device" else None,
                "hub_source": node.hub_source, "ip_changes": node.ip_changes,
            },
        })
    counts = {s: 0 for s in STATUSES}
    for node in nodes.values():
        if node.kind == "device":
            counts[node.status] = counts.get(node.status, 0) + 1
    opts = coord.opts
    states.append({
        "entity_id": "sensor.netorbit_summary",
        "state": counts.get("online", 0),
        "attributes": {
            "netorbit": True, "kind": "summary", "mode": opts[CONF_MODE], "stale_hours": opts[CONF_STALE_HOURS],
            "counts": counts,
            "new_devices": [{**d, "mac": d["mac"].upper() if d.get("mac") else None} for d in (data.new_devices if data else [])],
            "suggested_hubs": data.suggested_hubs if data else [],
            "sources": data.sources if data else [],
            "group_by_type": opts.get(CONF_GROUP_BY_TYPE, True),
            "last_scan": data.last_scan if data else None,
            "config_entry_id": ENTRY_ID,
        },
    })
    return states


# ----- service arguments ------------------------------------------------------------

def _str(v: Any) -> str:
    if not isinstance(v, (str, int, float)) or isinstance(v, bool):
        raise ValueError("must be text")
    return str(v).strip()


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.lower() in ("true", "false", "1", "0", "yes", "no", "on", "off"):
        return v.lower() in ("true", "1", "yes", "on")
    raise ValueError("must be true or false")


def _list(v: Any) -> list[str]:
    return [_str(x) for x in v] if isinstance(v, list) else [_str(v)]


def _dict(v: Any) -> dict:
    if not isinstance(v, dict):
        raise ValueError("must be an object")
    return v


def _hub_type(v: Any) -> str:
    v = _str(v)
    if v not in HUB_TYPES:
        raise ValueError(f"must be one of {', '.join(HUB_TYPES)}")
    return v


def _icon(v: Any) -> str:
    v = _str(v)
    if v and not re.fullmatch(r"mdi:[a-z0-9-]+", v):
        raise ValueError("must look like mdi:router")
    return v


# name -> (type, required, default)
SERVICES: dict[str, dict[str, tuple[Any, bool, Any]]] = {
    "track_device": {"mac": (_str, False, None), "ip": (_str, False, None), "name": (_str, False, None),
                     "icon": (_icon, False, None), "hub": (_str, False, None), "sleepy": (_bool, False, None)},
    "ignore_device": {"mac": (_str, False, None), "ip": (_str, False, None), "device": (_str, False, None)},
    "scan_now": {},
    "assign_device": {"device": (_list, True, None), "hub": (_str, True, None)},
    "reset_placement": {"device": (_list, True, None)},
    "add_hub": {"name": (_str, False, None), "type": (_hub_type, False, "access_point"), "parent": (_str, False, None),
                "ip": (_str, False, None), "mac": (_str, False, None), "device": (_str, False, None)},
    "remove_hub": {"hub": (_str, True, None)},
    "update_hub": {"hub": (_str, True, None), "name": (_str, False, None), "type": (_hub_type, False, None),
                   "parent": (_str, False, None)},
    "import_client_list": {"hub": (_str, True, None), "text": (_str, True, None), "add_missing": (_bool, False, True)},
    "confirm_hub": {"device": (_str, True, None), "type": (_hub_type, False, None), "name": (_str, False, None)},
    "dismiss_hub": {"device": (_str, True, None)},
    "update_device": {"device": (_str, True, None), "name": (_str, False, None), "icon": (_icon, False, None),
                      "sleepy": (_bool, False, None), "hub": (_str, False, None)},
    "export_config": {},
    "import_config": {"data": (_dict, True, None), "replace": (_bool, False, False)},
}


def parse_service_args(service: str, raw: Any) -> dict[str, Any]:
    spec = SERVICES.get(service)
    if spec is None:
        raise HTTPError(404, f"No service called {service}")
    if not isinstance(raw, dict):
        raise HTTPError(400, "Send a JSON object")
    out: dict[str, Any] = {}
    for name, (conv, required, default) in spec.items():
        value = raw.get(name)
        if value is None or value == "":
            if required:
                raise HTTPError(400, f"{name} is required")
            if default is not None:
                out[name] = default
            continue
        try:
            out[name] = conv(value)
        except ValueError as err:
            raise HTTPError(400, f"{name} {err}") from err
    return out


async def call_service(coord: NetOrbitCoordinator, service: str, a: dict[str, Any]) -> dict[str, Any]:
    if service == "scan_now":
        coord.request_sweep(probe=True)
        await coord.async_refresh()
        return {"devices": len(coord.data.nodes) if coord.data else 0}
    if service == "export_config":
        return actions.export_config(coord)
    table = {
        "track_device": lambda: actions.track(coord, **a),
        "ignore_device": lambda: actions.ignore(coord, **a),
        "assign_device": lambda: actions.assign(coord, a["device"], a["hub"]),
        "reset_placement": lambda: actions.reset(coord, a["device"]),
        "add_hub": lambda: actions.add_hub(coord, a.get("name"), a.get("type"), a.get("parent"), a.get("ip"), a.get("mac"), a.get("device")),
        "remove_hub": lambda: actions.remove_hub(coord, a["hub"]),
        "update_hub": lambda: actions.update_hub(coord, a["hub"], name=a.get("name"), type=a.get("type"), parent=a.get("parent")),
        "import_client_list": lambda: actions.import_list(coord, a["hub"], a["text"], a["add_missing"]),
        "confirm_hub": lambda: actions.confirm_hub(coord, a["device"], a.get("type"), a.get("name")),
        "dismiss_hub": lambda: actions.dismiss_hub(coord, a["device"]),
        "update_device": lambda: actions.update_device(coord, **a),
        # Home Assistant's REST API wraps the export in "service_response"; accept that too.
        "import_config": lambda: actions.import_config(coord, a["data"].get("service_response", a["data"]), a["replace"]),
    }
    return await table[service]()


# ----- settings -----------------------------------------------------------------------

def _int_in(v: Any, lo: int, hi: int, name: str) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError) as err:
        raise HTTPError(400, f"{name} must be a whole number") from err
    if not lo <= n <= hi:
        raise HTTPError(400, f"{name} must be between {lo} and {hi}")
    return n


SETTING_FIELDS = (
    CONF_MODE, CONF_PING_INTERVAL, CONF_DISCOVERY_INTERVAL, CONF_STALE_HOURS, CONF_IGNORE_RANDOM_MAC,
    CONF_AUTO_HUBS, CONF_GROUP_BY_TYPE, CONF_SNMP_ENABLED, CONF_SNMP_COMMUNITY, CONF_WEBHOOK_URL,
    CONF_NTFY_URL, CONF_NOTIFY_EVENTS, CONF_HISTORY_DAYS,
)


def clean_settings(raw: dict[str, Any], current: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate a settings form. Returns (data, options) to store."""
    data = dict(current.get("data") or {})
    opts = dict(current.get("options") or {})
    if CONF_SUBNET in raw:
        try:
            data[CONF_SUBNET] = validate_subnet(str(raw[CONF_SUBNET]), MAX_HOSTS)
        except ValueError as err:
            msg = {"too_large": f"That subnet has more than {MAX_HOSTS} addresses; use a /22 or smaller",
                   "ipv4_only": "Only IPv4 subnets are supported"}.get(str(err), "That is not a valid subnet, e.g. 192.168.1.0/24")
            raise HTTPError(400, msg) from err
    if CONF_GATEWAY in raw:
        try:
            data[CONF_GATEWAY] = str(ipaddress.IPv4Address(str(raw[CONF_GATEWAY]).strip()))
        except ValueError as err:
            raise HTTPError(400, "The router address must be an IPv4 address") from err
    if data.get(CONF_SUBNET) and data.get(CONF_GATEWAY):
        if ipaddress.IPv4Address(data[CONF_GATEWAY]) not in ipaddress.ip_network(data[CONF_SUBNET]):
            raise HTTPError(400, "The router must be inside the subnet")
    if CONF_MODE in raw:
        if raw[CONF_MODE] not in MODES:
            raise HTTPError(400, "mode must be auto or manual")
        opts[CONF_MODE] = raw[CONF_MODE]
    for key, lo, hi in ((CONF_PING_INTERVAL, MIN_PING_INTERVAL, 3600), (CONF_DISCOVERY_INTERVAL, 60, 86400),
                        (CONF_STALE_HOURS, 1, 720), (CONF_HISTORY_DAYS, 1, 3650)):
        if key in raw:
            opts[key] = _int_in(raw[key], lo, hi, key)
    for key in (CONF_IGNORE_RANDOM_MAC, CONF_AUTO_HUBS, CONF_GROUP_BY_TYPE, CONF_SNMP_ENABLED):
        if key in raw:
            try:
                opts[key] = _bool(raw[key])
            except ValueError as err:
                raise HTTPError(400, f"{key} must be true or false") from err
    if CONF_SNMP_COMMUNITY in raw:
        comm = str(raw[CONF_SNMP_COMMUNITY] or "").strip()
        if len(comm) > 64 or any(ord(c) < 32 for c in comm):
            raise HTTPError(400, "That SNMP community is not valid")
        opts[CONF_SNMP_COMMUNITY] = comm
    for key in (CONF_WEBHOOK_URL, CONF_NTFY_URL):
        if key in raw:
            url = str(raw[key] or "").strip()
            if url and not re.match(r"^https?://[^\s]+$", url):
                raise HTTPError(400, f"{key} must start with http:// or https://")
            opts[key] = url
    if CONF_NOTIFY_EVENTS in raw:
        events = raw[CONF_NOTIFY_EVENTS]
        if not isinstance(events, list) or any(e not in EVENTS for e in events):
            raise HTTPError(400, f"notify_events must be a list of {', '.join(EVENTS)}")
        opts[CONF_NOTIFY_EVENTS] = events
    return data, opts


# ----- the application ----------------------------------------------------------------

class App:
    def __init__(self, data_dir: Path, net: NetIO, *, demo: bool = False) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.net = net
        self.demo = demo
        self.config = ConfigStore(self.data_dir / "config.json")
        self.state = StateStore(self.data_dir / "state.json")
        self.history = History(self.data_dir / "history.db", self.config.options.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS))
        self.sessions = Sessions(self.data_dir / "secret")
        self.notifier = Notifier(lambda: self.config.options)
        self.coordinator: NetOrbitCoordinator | None = None
        self._task: asyncio.Task | None = None
        self.server = HTTPServer(self._routes(), self._middleware)

    # lifecycle

    async def start(self, host: str = "0.0.0.0", port: int = 8099) -> None:
        if not self.config.configured:
            found = detect_network()
            if found:
                subnet, gateway = found
                try:
                    subnet = validate_subnet(subnet, MAX_HOSTS)
                except ValueError:
                    # A big LAN: start with the /24 around the router; it can be changed in Settings.
                    subnet = str(ipaddress.ip_network(f"{gateway}/24", strict=False))
                _LOGGER.info("First start: using subnet %s and router %s (change them in Settings)", subnet, gateway)
                self.config.update(data={CONF_SUBNET: subnet, CONF_GATEWAY: gateway})
            else:
                _LOGGER.warning("Could not work out the local network; set the subnet and router in Settings")
        if self.config.configured:
            await self._start_coordinator()
        await self.server.start(host, port)

    async def _start_coordinator(self) -> None:
        coord = NetOrbitCoordinator(self.config, self.state, self.net)
        await coord.async_load()
        coord.add_listener(self._on_update)
        coord.add_event_listener(self._on_event)
        self.coordinator = coord
        self._task = asyncio.get_running_loop().create_task(coord.run(), name="netorbit-coordinator")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self.coordinator:
            self.coordinator.shutdown()
        await self.server.stop()
        self.history.close()

    def _on_update(self) -> None:
        coord = self.coordinator
        if coord and coord.data:
            self.history.keep_days = coord.opts.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS)
            try:
                self.history.record_nodes(coord.data.nodes)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Could not write history")
        self._broadcast("update", {"ok": coord.last_update_success if coord else False})

    def _on_event(self, event: str, payload: dict[str, Any]) -> None:
        try:
            self.history.record_event(event, payload)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Could not write history")
        if not self.demo:
            self.notifier(event, payload)
        self._broadcast("netorbit", {"event": event, **payload})

    def _broadcast(self, event: str, data: Any) -> None:
        for stream in list(self.server.streams):
            stream.send(event, data)

    # auth

    @property
    def password_hash(self) -> str | None:
        return self.config.data.get(PASSWORD_KEY)

    def _logged_in(self, req: Request) -> bool:
        ph = self.password_hash
        return ph is None or self.sessions.valid(req.cookies.get(COOKIE), ph)

    async def _middleware(self, req: Request, handler) -> Response | EventStream:  # noqa: ANN001
        if req.method == "POST":
            # Only our own page can change things: JSON bodies and same-origin requests.
            if not req.headers.get("content-type", "").startswith("application/json"):
                raise HTTPError(415, "Send JSON")
            origin = req.headers.get("origin")
            if origin and origin.split("://", 1)[-1] != req.headers.get("host"):
                raise HTTPError(403, "Cross-origin request refused")
        public = req.path in PUBLIC_PATHS or req.path.startswith("/static/")
        if not public and not self._logged_in(req):
            if req.path.startswith("/api/"):
                raise HTTPError(401, "Log in first")
            return Response(302, b"", headers={"Location": "/login"})
        return await handler(req)

    # routes

    def _routes(self) -> Router:
        r = Router()
        page = lambda name: (lambda req: self._page(name))  # noqa: E731
        r.add("GET", "/", page("index.html"))
        r.add("GET", "/settings", page("settings.html"))
        r.add("GET", "/login", page("login.html"))
        r.add("GET", "/favicon.svg", lambda req: self._static("favicon.svg"))
        r.add("GET", "/static/{name}", lambda req: self._static(req.params["name"]))
        r.add("GET", "/api/health", self.api_health)
        r.add("GET", "/api/state", self.api_state)
        r.add("GET", "/api/events", self.api_events)
        r.add("GET", "/api/map", self.api_map)
        r.add("GET", "/api/history/{key}", self.api_history)
        r.add("POST", "/api/services/{service}", self.api_service)
        r.add("GET", "/api/settings", self.api_settings)
        r.add("POST", "/api/settings", self.api_save_settings)
        r.add("POST", "/api/login", self.api_login)
        r.add("POST", "/api/logout", self.api_logout)
        r.add("POST", "/api/password", self.api_password)
        r.add("POST", "/api/test_notify", self.api_test_notify)
        return r

    async def _page(self, name: str) -> Response:
        return Response.file(WEB_DIR / name)

    async def _static(self, name: str) -> Response:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name.startswith(".") or name.endswith(".html"):
            raise HTTPError(404)
        return Response.file(WEB_DIR / name)

    def _coord(self) -> NetOrbitCoordinator:
        if not self.coordinator:
            raise HTTPError(409, "Set the subnet and router in Settings first")
        return self.coordinator

    async def api_health(self, req: Request) -> Response:
        coord = self.coordinator
        ok = bool(coord and coord.last_update_success)
        return Response.json({"ok": ok, "version": VERSION, "configured": self.config.configured}, 200 if ok or not coord else 503)

    def _status(self) -> dict[str, Any]:
        coord = self.coordinator
        return {
            "version": VERSION,
            "demo": self.demo,
            "configured": self.config.configured,
            "running": coord is not None,
            "ok": coord.last_update_success if coord else False,
            "error": coord.last_error if coord else None,
            "last_scan": coord.data.last_scan if coord and coord.data else None,
            "password_set": self.password_hash is not None,
        }

    async def api_state(self, req: Request) -> Response:
        coord = self.coordinator
        return Response.json({
            "states": build_states(coord) if coord else [],
            "user": {"is_admin": True, "name": "admin"},
            "status": self._status(),
        })

    async def api_events(self, req: Request) -> EventStream:
        return EventStream()

    async def api_map(self, req: Request) -> Response:
        return Response.json(actions.network_map(self._coord()))

    async def api_history(self, req: Request) -> Response:
        days = _int_in(req.arg("days", "7"), 1, 3650, "days")
        return Response.json(self.history.for_key(req.params["key"], days))

    async def api_service(self, req: Request) -> Response:
        service = req.params["service"]
        args = parse_service_args(service, req.json())
        try:
            result = await call_service(self._coord(), service, args)
        except ManageError as err:
            raise HTTPError(400, str(err)) from err
        return Response.json(result or {})

    async def api_settings(self, req: Request) -> Response:
        coord = self.coordinator
        opts = coord.opts if coord else options_with_defaults_safe(self.config)
        seen = {}
        if coord:
            for key, rec in coord._seen.items():  # noqa: SLF001
                if not key.startswith("hub:"):
                    seen[key] = {"ip": rec.get("ip"), "mac": rec.get("mac"), "vendor": rec.get("vendor"),
                                 "hostname": rec.get("hostname"), "last_seen": rec.get("last_seen"),
                                 "random_mac": is_random_mac(rec.get("mac"))}
        nodes = coord.data.nodes if coord and coord.data else {}
        devices = []
        for key, node in nodes.items():
            if node.kind != "device":
                continue
            cfg = opts.get("devices", {}).get(key, {})
            devices.append({"key": key, "name": node.name, "ip": node.ip, "mac": node.mac, "icon": node.icon,
                            "hub": node.link, "placement": node.placement, "sleepy": node.sleepy,
                            "status": node.status, "vendor": node.vendor, "custom_name": cfg.get("name"),
                            "custom_icon": cfg.get("icon")})
        devices.sort(key=lambda d: d["name"].lower())
        return Response.json({
            "data": {k: v for k, v in self.config.data.items() if k != PASSWORD_KEY},
            "options": {k: opts.get(k) for k in SETTING_FIELDS} | {
                CONF_NOTIFY_EVENTS: opts.get(CONF_NOTIFY_EVENTS, DEFAULT_NOTIFY_EVENTS),
                CONF_HISTORY_DAYS: opts.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS),
            },
            "hubs": [{**h, "name": h.get("name") or h["id"],
                      "link": nodes[f"hub:{h['id']}"].link if f"hub:{h['id']}" in nodes else h.get("parent")}
                     for h in opts.get(CONF_HUBS, [])],
            "hub_types": HUB_TYPES,
            "devices": devices,
            "new_devices": coord.data.new_devices if coord and coord.data else [],
            "suggested_hubs": coord.data.suggested_hubs if coord and coord.data else [],
            "ignored": [{"key": k, **seen.get(k, {})} for k in opts.get("ignored", [])],
            "status": self._status(),
        })

    async def api_save_settings(self, req: Request) -> Response:
        raw = req.json()
        if not isinstance(raw, dict):
            raise HTTPError(400, "Send a JSON object")
        old = dict(self.config.data)
        data, opts = clean_settings(raw, {"data": self.config.data, "options": self.config.options})
        if not (data.get(CONF_SUBNET) and data.get(CONF_GATEWAY)):
            raise HTTPError(400, "Set both the subnet and the router address")
        if data.get(CONF_GATEWAY) != old.get(CONF_GATEWAY):
            # Point the router hub at the new address; its MAC is learned again.
            for hub in opts.get(CONF_HUBS, []):
                if hub.get("id") == ROOT_HUB_ID:
                    hub["host"] = data[CONF_GATEWAY]
                    hub.pop("mac", None)
        self.config.update(data=data, options=opts)
        if self.coordinator:
            if data.get(CONF_GATEWAY) != old.get(CONF_GATEWAY):
                self.coordinator.forget_hub_address(ROOT_HUB_ID)
            self.coordinator.apply_options()
            self.coordinator.wake()
        else:
            await self._start_coordinator()
        return Response.json({"saved": True, "status": self._status()})

    async def api_login(self, req: Request) -> Response:
        body = req.json()
        ph = self.password_hash
        if ph is None:
            return Response.json({"ok": True})
        if not isinstance(body, dict) or not check_password(str(body.get("password") or ""), ph):
            await asyncio.sleep(1)  # slow down guessing
            raise HTTPError(401, "Wrong password")
        token = self.sessions.issue(ph)
        return Response.json({"ok": True}, headers={
            "Set-Cookie": f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_DAYS * 86400}",
        })

    async def api_logout(self, req: Request) -> Response:
        return Response.json({"ok": True}, headers={"Set-Cookie": f"{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})

    async def api_password(self, req: Request) -> Response:
        body = req.json()
        if not isinstance(body, dict):
            raise HTTPError(400, "Send a JSON object")
        ph = self.password_hash
        if ph is not None and not check_password(str(body.get("current") or ""), ph):
            await asyncio.sleep(1)
            raise HTTPError(403, "The current password is wrong")
        new = str(body.get("new") or "")
        data = dict(self.config.data)
        if new:
            if len(new) < 8:
                raise HTTPError(400, "Use at least 8 characters")
            data[PASSWORD_KEY] = hash_password(new)
        else:
            data.pop(PASSWORD_KEY, None)
        self.config.update(data=data)
        headers = {}
        if new:
            token = self.sessions.issue(data[PASSWORD_KEY])
            headers["Set-Cookie"] = f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_DAYS * 86400}"
        return Response.json({"password_set": bool(new)}, headers=headers)

    async def api_test_notify(self, req: Request) -> Response:
        opts = self.config.options
        if not (opts.get(CONF_WEBHOOK_URL) or opts.get(CONF_NTFY_URL)):
            raise HTTPError(400, "Save a webhook or ntfy address first")
        failed = await self.notifier.send(
            EVENT_NEW_DEVICE, {"name": "NetOrbit test", "ip": None, "mac": None, "test": True}, opts
        )
        if failed:
            raise HTTPError(502, f"Could not reach: {', '.join(failed)} (see the log)")
        return Response.json({"sent": True})


def options_with_defaults_safe(config: ConfigStore) -> dict[str, Any]:
    """Defaults for the settings page before the network is configured."""
    if config.configured:
        return options_with_defaults(config)
    return dict(config.options)

