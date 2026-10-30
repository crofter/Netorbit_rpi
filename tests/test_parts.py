"""History, storage, auth, notifications, network detection and SSDP parsing."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import http.server
import json
import os
import threading
from types import SimpleNamespace

from netorbit_pi.auth import Sessions, check_password, hash_password
from netorbit_pi.history import History
from netorbit_pi.netdetect import parse_routes
from netorbit_pi.notify import Notifier, message_for
from netorbit_pi.ssdp import build_msearch, parse_response
from netorbit_pi.store import ConfigStore, StateStore

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _node(status, name="TV", ip="192.0.2.5"):
    return SimpleNamespace(status=status, name=name, ip=ip)


def test_history_records_changes_only(tmp_path):
    h = History(tmp_path / "h.db")
    assert h.record_nodes({"a": _node("online")}, T0) == 1
    assert h.record_nodes({"a": _node("online")}, T0 + timedelta(minutes=1)) == 0
    assert h.record_nodes({"a": _node("recent")}, T0 + timedelta(hours=6)) == 1
    out = h.for_key("a", days=1, now=T0 + timedelta(hours=12))
    assert [c["status"] for c in out["changes"]] == ["online", "recent"]
    assert out["online_share"] == 0.5
    h.close()
    # The last status survives a restart, so no duplicate row.
    h = History(tmp_path / "h.db")
    assert h.record_nodes({"a": _node("recent")}, T0 + timedelta(hours=13)) == 0


def test_history_window_starts_with_earlier_status(tmp_path):
    h = History(tmp_path / "h.db")
    h.record_nodes({"a": _node("online")}, T0)
    h.record_nodes({"a": _node("stale")}, T0 + timedelta(days=3))
    out = h.for_key("a", days=1, now=T0 + timedelta(days=3, hours=12))
    assert out["online_share"] == 0.5


def test_history_prunes_but_keeps_latest(tmp_path):
    h = History(tmp_path / "h.db", keep_days=10)
    h.record_nodes({"a": _node("online"), "b": _node("online")}, T0)
    h.record_nodes({"a": _node("stale")}, T0 + timedelta(days=1))
    h._last_prune = None  # noqa: SLF001
    h.record_nodes({}, T0 + timedelta(days=30))
    rows = h._db.execute("SELECT key, status FROM changes ORDER BY key").fetchall()  # noqa: SLF001
    assert rows == [("a", "stale"), ("b", "online")]


def test_history_new_device_event(tmp_path):
    h = History(tmp_path / "h.db")
    h.record_event("netorbit_new_device", {"mac": "00:00:5e:00:53:01", "name": "x", "ip": "192.0.2.9"}, T0)
    out = h.for_key("00:00:5e:00:53:01", now=T0 + timedelta(hours=1))
    assert [c["status"] for c in out["changes"]] == ["new"] and out["online_share"] is None


def test_config_store_roundtrip_and_permissions(tmp_path):
    cfg = ConfigStore(tmp_path / "config.json")
    assert not cfg.configured
    cfg.update(data={"subnet": "192.0.2.0/24", "gateway": "192.0.2.1"}, options={"mode": "manual"})
    assert oct(os.stat(tmp_path / "config.json").st_mode & 0o777) == "0o600"
    again = ConfigStore(tmp_path / "config.json")
    assert again.configured and again.options == {"mode": "manual"}


def test_broken_config_is_kept_aside(tmp_path):
    (tmp_path / "config.json").write_text("{not json")
    cfg = ConfigStore(tmp_path / "config.json")
    assert not cfg.configured
    assert (tmp_path / "config.json.bad").exists()


async def test_state_store_delays_and_flushes(tmp_path):
    st = StateStore(tmp_path / "state.json")
    st.delay_save(lambda: {"seen": {"a": 1}}, delay=60)
    assert not (tmp_path / "state.json").exists()
    st.flush()
    assert json.loads((tmp_path / "state.json").read_text()) == {"seen": {"a": 1}}
    st.delay_save(lambda: {"seen": {"b": 2}}, delay=0.01)
    await asyncio.sleep(0.05)
    assert st.load() == {"seen": {"b": 2}}


def test_passwords_and_sessions(tmp_path):
    ph = hash_password("correct horse", iterations=1000)
    assert check_password("correct horse", ph)
    assert not check_password("wrong", ph)
    assert not check_password("x", None) and not check_password("x", "garbage")
    s = Sessions(tmp_path / "secret")
    tok = s.issue(ph, now=1000)
    assert s.valid(tok, ph, now=2000)
    assert not s.valid(tok, ph, now=1000 + 31 * 86400), "expired"
    assert not s.valid(tok, hash_password("new one", iterations=1000), now=2000), "password changed"
    assert not s.valid(tok[:-1] + ("0" if tok[-1] != "0" else "1"), ph, now=2000)
    # The secret persists, so sessions survive a restart.
    assert Sessions(tmp_path / "secret").valid(tok, ph, now=2000)


ROUTES = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
eth0\t00000000\t0102A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0
eth0\t0002A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0
wlan0\t00000000\t0100000A\t0003\t0\t0\t600\t00000000\t0\t0\t0
wlan0\t0000000A\t00000000\t0001\t0\t0\t600\t0000FFFF\t0\t0\t0
"""


def test_parse_routes_prefers_lowest_metric():
    assert parse_routes(ROUTES) == ("192.168.2.0/24", "192.168.2.1")
    assert parse_routes(ROUTES.split("\n", 1)[0]) is None


def test_ssdp_parsing():
    assert b"ST: urn:x" in build_msearch("urn:x")
    resp = b"HTTP/1.1 200 OK\r\nST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\nLOCATION: http://x\r\n\r\n"
    assert parse_response(resp) == "urn:schemas-upnp-org:device:InternetGatewayDevice:1"
    assert parse_response(b"NOTIFY * HTTP/1.1\r\nNT: x\r\n\r\n") is None


def test_notification_messages():
    title, body = message_for("netorbit_device_stale", {"name": "Printer", "ip": "192.0.2.4", "mac": "aa", "kind": "device"})
    assert title == "Device not seen" and "Printer" in body and "192.0.2.4" in body
    assert message_for("netorbit_new_device", {"mac": "aa:bb"})[1].startswith("aa:bb joined")


class _Catcher(http.server.BaseHTTPRequestHandler):
    got: list = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Catcher.got.append((self.path, dict(self.headers), body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


async def test_notifier_sends_webhook_and_ntfy():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Catcher)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    _Catcher.got = []
    opts = {"webhook_url": base + "/hook", "ntfy_url": base + "/topic"}
    n = Notifier(lambda: opts)
    failed = await n.send("netorbit_new_device", {"name": "Kühlschrank", "mac": "aa", "ip": "192.0.2.3"}, opts)
    srv.shutdown()
    assert failed == []
    paths = {p: (h, b) for p, h, b in _Catcher.got}
    hook = json.loads(paths["/hook"][1])
    assert hook["event"] == "netorbit_new_device" and hook["name"] == "Kühlschrank"
    assert paths["/topic"][0]["Title"] == "New device on the network"
    assert "Kühlschrank".encode() in paths["/topic"][1]


async def test_notifier_filters_events_and_reports_failures():
    opts = {"webhook_url": "http://127.0.0.1:9/nothing", "notify_events": ["netorbit_device_stale"]}
    n = Notifier(lambda: opts)
    n("netorbit_new_device", {"name": "x"})
    assert not n._tasks  # noqa: SLF001 - not wanted, nothing sent
    assert await n.send("netorbit_device_stale", {"name": "x"}, opts) == ["webhook"]
