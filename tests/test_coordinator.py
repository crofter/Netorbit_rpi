"""The standalone coordinator against simulated networks, checked against the answer key."""

from __future__ import annotations

import random

import pytest

from netorbit_pi import actions
from netorbit_pi.const import EVENT_DEVICE_RECOVERED, EVENT_DEVICE_STALE, EVENT_NEW_DEVICE
from netorbit_pi.netgen import SHAPES, random_network
from netorbit_pi.sim import SimNetIO

from .conftest import make_coordinator


def _check_map(coord) -> None:
    """Hard rules every map must follow."""
    nodes = coord.data.nodes
    hubs = {k for k, n in nodes.items() if n.kind == "hub"}
    macs = [n.mac for n in nodes.values() if n.kind == "device" and n.mac]
    assert len(macs) == len(set(macs)), "a device is shown twice"
    hub_macs = {n.mac for n in nodes.values() if n.kind == "hub" and n.mac}
    assert not (set(macs) & hub_macs), "something is both a hub and a device"
    for key, n in nodes.items():
        if n.kind == "hub" and n.link is not None:
            assert f"hub:{n.link}" in hubs, f"{key} links to a missing hub"
        if n.kind == "device":
            assert f"hub:{n.link}" in hubs, f"{key} hangs off a missing hub"
    # No loops in the hub tree.
    for key in hubs:
        seen, cur = set(), key
        while cur:
            assert cur not in seen, f"loop through {cur}"
            seen.add(cur)
            link = nodes[cur].link
            cur = f"hub:{link}" if link else None


def _true_hub_path(net, mac: str) -> set[str]:
    """MACs of every hub between a device and the router in the real network."""
    by_mac = {n.mac: n for n in net.nodes.values() if n.mac}
    node = by_mac[mac]
    out = set()
    for key in net.path_to_root(node.key)[1:]:
        if net.nodes[key].mac:
            out.add(net.nodes[key].mac)
    return out


@pytest.mark.parametrize("shape", sorted(SHAPES))
async def test_shapes(tmp_path, shape):
    sim = SimNetIO.from_shape(shape, seed=11)
    coord = await make_coordinator(tmp_path, sim, {"snmp_community": sim.net.community if sim.net.community != "public" else ""})
    await coord.async_refresh()
    assert coord.last_update_success, coord.last_error
    _check_map(coord)
    hub_mac = {h["id"]: (h.get("mac") or "") for h in coord.opts["hubs"]}
    for node in coord.data.nodes.values():
        if node.kind != "device" or node.placement not in ("snmp", "controller") or not node.mac:
            continue
        placed_on = hub_mac.get(node.link)
        path = _true_hub_path(sim.net, node.mac)
        # The hub chosen must be on the device's real way to the router. An access point
        # sharing an unmanaged switch port with it is the one known ambiguity.
        siblings = {n.mac for n in sim.net.nodes.values() if n.mac and n.kind.startswith("ap")}
        assert placed_on in path or placed_on in siblings, f"{node.name} wrongly under {node.link}"


@pytest.mark.parametrize("seed", range(25))
async def test_random_networks(tmp_path, seed):
    net = random_network(seed)
    sim = SimNetIO(net, seed=seed)
    coord = await make_coordinator(tmp_path, sim, {"snmp_community": net.community if net.community != "public" else ""})
    await coord.async_refresh()
    assert coord.last_update_success, coord.last_error
    _check_map(coord)


async def test_snmp_finds_switches_and_places_devices(tmp_path):
    sim = SimNetIO.from_shape("snmp_chain", seed=3)
    coord = await make_coordinator(tmp_path, sim)
    await coord.async_refresh()
    types = sorted(n.hub_type for n in coord.data.nodes.values() if n.kind == "hub")
    assert types.count("switch") == 3
    placed = [n for n in coord.data.nodes.values() if n.kind == "device" and n.placement == "snmp"]
    assert len(placed) >= 15
    assert "snmp" in coord.data.sources


async def test_custom_community_only_goes_to_network_gear(tmp_path):
    sim = SimNetIO.from_shape("snmp_single_custom_community", seed=3)
    coord = await make_coordinator(tmp_path, sim, {"snmp_community": "s3cretRO"})
    await coord.async_refresh()
    gear = {n.ip for n in sim.net.nodes.values() if n.kind != "device" and n.ip}
    for ip, comm in sim.probed:
        if comm == "s3cretRO":
            vendor = next((n.vendor for n in sim.net.nodes.values() if n.ip == ip), None)
            assert ip in gear or vendor in ("snapav", "netgear", "tplink", "ubiquiti", "eero"), ip
    assert any(n.hub_type == "switch" for n in coord.data.nodes.values() if n.kind == "hub")


async def test_state_survives_restart(tmp_path):
    sim = SimNetIO.from_shape("flat", seed=5)
    coord = await make_coordinator(tmp_path, sim)
    await coord.async_refresh()
    before = {k: n.first_seen for k, n in coord.data.nodes.items()}
    coord.shutdown()
    again = await make_coordinator(tmp_path, sim)
    await again.async_refresh()
    after = {k: n.first_seen for k, n in again.data.nodes.items()}
    assert before == after


async def test_manual_placement_wins_and_reset(tmp_path):
    sim = SimNetIO.from_shape("snmp_single", seed=2)
    coord = await make_coordinator(tmp_path, sim)
    await coord.async_refresh()
    dev = next(n for n in coord.data.nodes.values() if n.kind == "device" and n.placement == "snmp")
    other = next(h["id"] for h in coord.opts["hubs"] if h["id"] not in ("gateway", dev.link))
    await actions.assign(coord, [dev.mac], other)
    node = coord.data.nodes[dev.key]
    assert (node.link, node.placement) == (other, "manual")
    # Saved to disk.
    assert coord.config.options["devices"][dev.key]["hub"] == other
    await actions.reset(coord, [dev.mac])
    assert coord.data.nodes[dev.key].placement == "snmp"


async def test_dhcp_change_is_followed(tmp_path):
    sim = SimNetIO.from_shape("flat", seed=8)
    coord = await make_coordinator(tmp_path, sim)
    await coord.async_refresh()
    dev = next(n for n in coord.data.nodes.values() if n.kind == "device")
    await actions.update_device(coord, dev.mac, name="Kitchen tablet")
    new_ip = "192.0.2.250"
    sim.arp.pop(dev.ip)
    sim.alive.discard(dev.ip)
    sim.arp[new_ip] = dev.mac
    sim.alive.add(new_ip)
    coord.request_sweep()
    await coord.async_refresh()
    node = coord.data.nodes[dev.key]
    assert (node.ip, node.name, node.status, node.ip_changes) == (new_ip, "Kitchen tablet", "online", 1)


async def test_events(tmp_path):
    sim = SimNetIO.from_shape("flat", seed=9)
    coord = await make_coordinator(tmp_path, sim, {"stale_hours": 1})
    events = []
    coord.add_event_listener(lambda ev, payload: events.append((ev, payload)))
    await coord.async_refresh()
    assert not events, "the first sweep is not news"

    # A new device joins.
    sim.arp["192.0.2.251"] = "4c:00:5e:00:99:01"
    sim.alive.add("192.0.2.251")
    coord.request_sweep()
    await coord.async_refresh()
    assert [e for e, _ in events] == [EVENT_NEW_DEVICE]
    assert events[0][1]["mac"] == "4c:00:5e:00:99:01"

    # It goes quiet for longer than the window, then comes back.
    rec = coord._seen["4c:00:5e:00:99:01"]  # noqa: SLF001
    rec["last_seen"] = rec["first_seen"] = "2020-01-01T00:00:00+00:00"
    sim.alive.discard("192.0.2.251")
    await coord.async_refresh()
    assert events[-1][0] == EVENT_DEVICE_STALE
    sim.alive.add("192.0.2.251")
    await coord.async_refresh()
    assert events[-1][0] == EVENT_DEVICE_RECOVERED


async def test_manual_mode_lists_new_devices(tmp_path):
    sim = SimNetIO.from_shape("flat", seed=4)
    coord = await make_coordinator(tmp_path, sim, {"mode": "manual"})
    await coord.async_refresh()
    assert not [n for n in coord.data.nodes.values() if n.kind == "device"]
    assert len(coord.data.new_devices) >= 10
    first = coord.data.new_devices[0]
    await actions.track(coord, mac=first["mac"])
    assert first["key"] in coord.data.nodes


async def test_import_client_list_and_export_roundtrip(tmp_path):
    sim = SimNetIO.from_shape("mesh_no_data", seed=6)
    coord = await make_coordinator(tmp_path, sim)
    await coord.async_refresh()
    res = await actions.add_hub(coord, "Upstairs mesh", "mesh_node")
    devices = [n for n in coord.data.nodes.values() if n.kind == "device"][:4]
    text = "Name  MAC\n" + "\n".join(f"{d.name}\t{d.mac.upper()}" for d in devices) + "\nunknown 02-11-22-33-44-55"
    out = await actions.import_list(coord, res["hub"], text)
    assert len(out["assigned"]) == 5 and out["added_new"] == ["02:11:22:33:44:55".upper()]
    for d in devices:
        assert coord.data.nodes[d.key].link == res["hub"]

    backup = actions.export_config(coord)
    await actions.remove_hub(coord, res["hub"])
    assert all(coord.data.nodes[d.key].link != res["hub"] for d in devices)
    await actions.import_config(coord, backup)
    assert all(coord.data.nodes[d.key].link == res["hub"] for d in devices)


async def test_failed_update_keeps_last_map(tmp_path):
    sim = SimNetIO.from_shape("flat", seed=1)
    coord = await make_coordinator(tmp_path, sim)
    await coord.async_refresh()
    good = coord.data

    async def boom(*a, **k):
        raise OSError("network is down")

    sim.multiping = boom
    await coord.async_refresh()
    assert not coord.last_update_success and "network is down" in coord.last_error
    assert coord.data is good


async def test_no_icmp_permission_reports_clearly(tmp_path):
    sim = SimNetIO.from_shape("flat", seed=1)
    sim.privileged = None
    coord = await make_coordinator(tmp_path, sim)
    await coord.async_refresh()
    assert not coord.last_update_success and "ICMP" in coord.last_error


def test_random_network_is_deterministic():
    a, b = random_network(3), random_network(3)
    assert [n.mac for n in a.nodes.values()] == [n.mac for n in b.nodes.values()]
    assert random.Random(1).random() == random.Random(1).random()
