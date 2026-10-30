"""The web server and API, end to end on a simulated network."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

import pytest

from netorbit_pi.app import App, clean_settings, parse_service_args
from netorbit_pi.httpserver import HTTPError
from netorbit_pi.sim import SimNetIO
from netorbit_pi.store import ConfigStore


class Client:
    def __init__(self, port: int) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.cookie: str | None = None

    async def call(self, method: str, path: str, body=None, headers=None, raw: bytes | None = None):
        def go():
            h = {"Content-Type": "application/json"} if body is not None else {}
            h.update(headers or {})
            if self.cookie:
                h["Cookie"] = self.cookie
            data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
            req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
            opener = urllib.request.build_opener(_NoRedirect)
            try:
                with opener.open(req, timeout=10) as r:
                    return r.status, dict(r.headers), r.read()
            except urllib.error.HTTPError as e:
                return e.code, dict(e.headers), e.read()

        status, headers, data = await asyncio.to_thread(go)
        if "Set-Cookie" in headers:
            self.cookie = headers["Set-Cookie"].split(";")[0]
        try:
            return status, json.loads(data) if data else None
        except ValueError:
            return status, data

    async def get(self, path):
        return await self.call("GET", path)

    async def post(self, path, body=None, **kw):
        return await self.call("POST", path, {} if body is None else body, **kw)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


@pytest.fixture
async def app(tmp_path):
    sim = SimNetIO.from_shape("snmp_chain", seed=2)
    cfg = ConfigStore(tmp_path / "config.json")
    cfg.update(data={"subnet": sim.net.subnet, "gateway": sim.net.gateway})
    a = App(tmp_path, sim)
    await a.start("127.0.0.1", 0)
    for _ in range(100):
        if a.coordinator.data:
            break
        await asyncio.sleep(0.02)
    a.client = Client(a.server.sockets[0].getsockname()[1])
    yield a
    await a.stop()


async def test_state_looks_like_home_assistant(app):
    status, data = await app.client.get("/api/state")
    assert status == 200 and data["status"]["ok"]
    states = {s["entity_id"]: s for s in data["states"]}
    summary = states["sensor.netorbit_summary"]["attributes"]
    assert summary["kind"] == "summary" and summary["config_entry_id"] == "standalone"
    hubs = [s for s in data["states"] if s["attributes"]["kind"] == "hub"]
    root = [h for h in hubs if h["attributes"]["link"] is None]
    assert len(root) == 1 and root[0]["attributes"]["node_key"] == "hub:gateway"
    dev = next(s for s in data["states"] if s["attributes"]["kind"] == "device")
    assert dev["state"] in ("online", "recent", "stale") and dev["attributes"]["netorbit"] is True
    assert dev["attributes"]["link"].startswith("hub:")


async def test_pages_and_static_files(app):
    c = app.client
    for path in ("/", "/settings", "/login", "/static/netorbit-card.js", "/static/icons.js", "/favicon.svg"):
        status, _ = await c.get(path)
        assert status == 200, path
    for path in ("/static/..%2Fapp.py", "/static/settings.html", "/static/.secret", "/nope"):
        status, _ = await c.get(path)
        assert status == 404, path


async def test_services_from_the_card(app):
    c = app.client
    _, data = await c.get("/api/state")
    dev = next(s["attributes"] for s in data["states"] if s["attributes"]["kind"] == "device")
    hub = next(s["attributes"]["node_key"] for s in data["states"]
               if s["attributes"]["kind"] == "hub" and s["attributes"]["node_key"] != dev["link"])
    # Exactly what the card sends, including the config_entry_id it adds.
    status, res = await c.post("/api/services/assign_device", {"device": [dev["mac"]], "hub": hub[4:], "config_entry_id": "standalone"})
    assert status == 200 and res["hub"] == hub[4:]
    _, data = await c.get("/api/state")
    moved = next(s["attributes"] for s in data["states"] if s["attributes"].get("node_key") == dev["node_key"])
    assert moved["link"] == hub and moved["placement"] == "manual"

    status, res = await c.post("/api/services/assign_device", {"device": ["nobody"], "hub": "gateway"})
    assert status == 400 and "None of these devices" in res["error"]
    status, res = await c.post("/api/services/assign_device", {"hub": "gateway"})
    assert status == 400 and res["error"] == "device is required"
    status, res = await c.post("/api/services/add_hub", {"name": "x", "type": "spaceship"})
    assert status == 400 and "type must be one of" in res["error"]
    status, _ = await c.post("/api/services/format_disk", {})
    assert status == 404


async def test_scan_export_import_and_map(app):
    c = app.client
    status, res = await c.post("/api/services/scan_now")
    assert status == 200 and res["devices"] > 10
    _, backup = await c.post("/api/services/export_config")
    assert backup["netorbit_export"] == 1
    status, res = await c.post("/api/services/import_config", {"data": backup})
    assert status == 200 and res["hubs"] == len(backup["hubs"])
    status, res = await c.post("/api/services/import_config", {"data": {"nope": 1}})
    assert status == 400
    status, m = await c.get("/api/map")
    assert status == 200 and m["hubs"] and m["devices"]


async def test_history_endpoint(app):
    c = app.client
    _, data = await c.get("/api/state")
    key = next(s["attributes"]["node_key"] for s in data["states"] if s["attributes"]["kind"] == "device")
    status, h = await c.get(f"/api/history/{key}?days=7")
    assert status == 200 and h["changes"] and h["changes"][0]["status"] in ("online", "recent")
    status, _ = await c.get(f"/api/history/{key}?days=0")
    assert status == 400


async def test_settings_roundtrip(app):
    c = app.client
    _, s = await c.get("/api/settings")
    assert s["data"]["subnet"] == "192.0.2.0/24" and "password_hash" not in s["data"]
    assert any(h["id"] == "gateway" for h in s["hubs"]) and s["devices"]
    status, _ = await c.post("/api/settings", {"stale_hours": 48, "mode": "manual", "snmp_community": "abc"})
    assert status == 200
    assert app.coordinator.opts["stale_hours"] == 48 and app.coordinator.opts["mode"] == "manual"
    status, res = await c.post("/api/settings", {"ping_interval": 1})
    assert status == 400 and "between" in res["error"]
    status, res = await c.post("/api/settings", {"gateway": "10.0.0.1"})
    assert status == 400 and "inside the subnet" in res["error"]
    status, res = await c.post("/api/settings", {"subnet": "10.0.0.0/16"})
    assert status == 400 and "/22" in res["error"]


async def test_router_change_moves_root_hub(app):
    c = app.client
    status, _ = await c.post("/api/settings", {"gateway": "192.0.2.254"})
    assert status == 200
    root = next(h for h in app.config.options["hubs"] if h["id"] == "gateway")
    assert root["host"] == "192.0.2.254" and "mac" not in root


async def test_password_flow(app):
    c = app.client
    status, _ = await c.post("/api/password", {"new": "short"})
    assert status == 400
    status, res = await c.post("/api/password", {"new": "a good password"})
    assert status == 200 and res["password_set"] and c.cookie  # the person setting it stays logged in

    stranger = Client(int(c.base.rsplit(":", 1)[1]))
    status, _ = await stranger.get("/api/state")
    assert status == 401
    status, _ = await stranger.post("/api/services/scan_now")
    assert status == 401
    status, _ = await stranger.call("GET", "/")
    assert status == 302
    status, _ = await stranger.get("/login")
    assert status == 200
    status, _ = await stranger.get("/static/style.css")
    assert status == 200
    status, _ = await stranger.post("/api/login", {"password": "wrong"})
    assert status == 401
    status, _ = await stranger.post("/api/login", {"password": "a good password"})
    assert status == 200
    status, _ = await stranger.get("/api/state")
    assert status == 200

    # Changing it needs the current one, and logs out everyone else.
    status, _ = await c.post("/api/password", {"current": "nope", "new": "another password"})
    assert status == 403
    status, _ = await c.post("/api/password", {"current": "a good password", "new": "another password"})
    assert status == 200
    status, _ = await stranger.get("/api/state")
    assert status == 401
    status, res = await c.post("/api/password", {"current": "another password", "new": ""})
    assert status == 200 and not res["password_set"]
    status, _ = await stranger.get("/api/state")
    assert status == 200


async def test_posts_must_be_same_origin_json(app):
    c = app.client
    status, res = await c.call("POST", "/api/services/scan_now", raw=b"{}", headers={"Content-Type": "text/plain"})
    assert status == 415
    status, res = await c.post("/api/services/scan_now", headers={"Origin": "http://evil.example"})
    assert status == 403
    host = c.base.split("://", 1)[1]
    status, _ = await c.post("/api/services/scan_now", headers={"Origin": f"http://{host}"})
    assert status == 200


async def test_event_stream_announces_updates(app):
    port = int(app.client.base.rsplit(":", 1)[1])
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /api/events HTTP/1.1\r\nHost: x\r\n\r\n")
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    assert b"text/event-stream" in head
    await app.coordinator.async_refresh()
    got = b""
    while b"event: update" not in got:
        got += await asyncio.wait_for(reader.read(1024), 5)
    writer.close()


async def test_unconfigured_start(tmp_path, monkeypatch):
    import netorbit_pi.app as appmod

    monkeypatch.setattr(appmod, "detect_network", lambda: None)
    a = App(tmp_path, SimNetIO.from_shape("flat", seed=1))
    await a.start("127.0.0.1", 0)
    try:
        c = Client(a.server.sockets[0].getsockname()[1])
        _, data = await c.get("/api/state")
        assert data["states"] == [] and not data["status"]["configured"]
        status, _ = await c.post("/api/services/scan_now")
        assert status == 409
        status, _ = await c.post("/api/settings", {"subnet": "192.0.2.0/24", "gateway": "192.0.2.1"})
        assert status == 200 and a.coordinator is not None
    finally:
        await a.stop()


def test_parse_service_args():
    assert parse_service_args("assign_device", {"device": "tv", "hub": "x"}) == {"device": ["tv"], "hub": "x"}
    assert parse_service_args("import_client_list", {"hub": "x", "text": "t"})["add_missing"] is True
    assert parse_service_args("track_device", {"mac": "aa", "sleepy": "yes"}) == {"mac": "aa", "sleepy": True}
    with pytest.raises(HTTPError):
        parse_service_args("track_device", {"icon": "fa:bad"})
    with pytest.raises(HTTPError):
        parse_service_args("track_device", ["not", "a", "dict"])


def test_clean_settings():
    data, opts = clean_settings({"subnet": "192.0.2.7/24", "gateway": "192.0.2.1", "auto_hubs": "false",
                                 "notify_events": ["netorbit_new_device"], "ntfy_url": " https://ntfy.sh/x "},
                                {"data": {}, "options": {}})
    assert data == {"subnet": "192.0.2.0/24", "gateway": "192.0.2.1"}
    assert opts == {"auto_hubs": False, "notify_events": ["netorbit_new_device"], "ntfy_url": "https://ntfy.sh/x"}
    with pytest.raises(HTTPError):
        clean_settings({"webhook_url": "file:///etc/passwd"}, {"data": {}, "options": {}})
    with pytest.raises(HTTPError):
        clean_settings({"notify_events": ["rm -rf"]}, {"data": {}, "options": {}})
