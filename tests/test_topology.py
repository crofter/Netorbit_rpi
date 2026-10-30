"""Layout inference from switch MAC tables, detection scoring and address extraction."""

from netorbit_pi.detect import Evidence, classify, pretty_vendor
from netorbit_pi.logic import extract_addresses
from netorbit_pi.topology import infer, macs_close

GW = "00:00:5e:00:53:01"
SW1 = "00:00:5e:00:53:02"
SW2 = "00:00:5e:00:53:03"
AP1 = "00:00:5e:00:53:04"
AP2 = "00:00:5e:00:53:05"
AP3 = "00:00:5e:00:53:06"
TV = "00:00:5e:00:53:10"
CAM = "00:00:5e:00:53:11"
NAS = "00:00:5e:00:53:12"
PLUG = "00:00:5e:00:53:13"
PRN = "00:00:5e:00:53:14"


def _home():
    """Router on SW1 port 1, SW2 on SW1 port 2, AP1 on SW1 port 6, AP2 on SW1 port 7,
    AP3 (no table) on SW2 port 5, NAS wired on SW2 port 3."""
    sw1 = {GW: 1, SW2: 2, AP1: 6, AP2: 7, AP3: 2, TV: 6, CAM: 7, NAS: 2, PLUG: 2, PRN: 6}
    sw2 = {GW: 1, SW1: 1, AP1: 1, AP2: 1, AP3: 5, TV: 1, CAM: 1, NAS: 3, PLUG: 5, PRN: 1}
    tables = {"sw1": sw1, "sw2": sw2}
    hubs = {"gateway": GW, "sw1": SW1, "sw2": SW2, "ap1": AP1, "ap2": AP2, "ap3": AP3}
    devices = {"tv": TV, "cam": CAM, "nas": NAS, "plug": PLUG, "printer": PRN}
    return tables, hubs, devices


def test_hub_parents():
    tables, hubs, devices = _home()
    topo = infer(tables, hubs, GW, devices, "gateway", leaf_hubs={"ap1", "ap2", "ap3"})
    assert topo.hub_parent == {"sw2": "sw1", "ap1": "sw1", "ap2": "sw1", "ap3": "sw2", "sw1": "gateway"}


def test_device_placement():
    tables, hubs, devices = _home()
    topo = infer(tables, hubs, GW, devices, "gateway", leaf_hubs={"ap1", "ap2", "ap3"})
    assert topo.device_hub == {"tv": "ap1", "cam": "ap2", "nas": "sw2", "plug": "ap3", "printer": "ap1"}


def test_unknown_device_left_alone():
    tables, hubs, devices = _home()
    devices["ghost"] = "00:00:5e:00:53:99"
    topo = infer(tables, hubs, GW, devices, "gateway")
    assert "ghost" not in topo.device_hub


def test_no_tables_means_no_opinion():
    topo = infer({}, {"gateway": GW}, GW, {"tv": TV}, "gateway")
    assert topo.device_hub == {} and topo.hub_parent == {}


def test_two_aps_on_one_port_falls_back_to_the_switch():
    sw1 = {GW: 1, AP1: 4, AP2: 4, TV: 4}
    topo = infer({"sw1": sw1}, {"gateway": GW, "sw1": SW1, "ap1": AP1, "ap2": AP2}, GW, {"tv": TV}, "gateway")
    assert topo.device_hub == {"tv": "sw1"}


def test_router_with_its_own_table():
    gw = {SW1: 2, AP1: 3, TV: 3}
    topo = infer({"gateway": gw}, {"gateway": GW, "ap1": AP1}, GW, {"tv": TV}, "gateway")
    assert topo.hub_parent["ap1"] == "gateway"
    assert topo.device_hub == {"tv": "ap1"}


def test_macs_close():
    assert macs_close("00:00:5e:00:53:04", "00:00:5e:00:53:06")
    assert macs_close("00:00:5e:00:53:04", "02:00:5e:00:53:05")  # locally administered radio MAC  example-ok
    assert not macs_close("00:00:5e:00:53:04", "00:00:5e:00:54:04")  # example-ok: synthetic test value
    assert not macs_close("00:00:5e:00:53:04", None)


def test_detect_scores():
    sure = classify(Evidence(vendor="Ubiquiti", hostname="U6-Lite-Hall"))
    assert sure.auto and sure.hub_type == "access_point"
    maybe = classify(Evidence(vendor="Ubiquiti"))
    assert maybe.suggest and not maybe.auto
    plug = classify(Evidence(vendor="TP-LINK TECHNOLOGIES", hostname="kitchen-plug"))
    assert not plug.suggest and not plug.auto
    deco = classify(Evidence(vendor="TP-Link Corporation", hostname="Deco-M5-Office"))
    assert deco.suggest and deco.hub_type == "mesh_node"
    switch = classify(Evidence(snmp_bridge=True, snmp_descr="24-port Gigabit Smart Switch"))
    assert switch.auto and switch.hub_type == "switch"
    eero = classify(Evidence(vendor="eero inc"))
    assert eero.suggest and eero.hub_type == "mesh_node"
    reg = classify(Evidence(registry_domain="unifi", registry_model="U7-Pro", vendor="Ubiquiti"))
    assert reg.auto and reg.hub_type == "access_point"
    phone = classify(Evidence(vendor="Apple", hostname="Alexs-iPhone"))
    assert phone.hub_type is None
    ext = classify(Evidence(vendor="NETGEAR", hostname="shed-extender"))
    assert ext.suggest and ext.hub_type == "access_point"
    named_ap = classify(Evidence(vendor="Generic Devices", hostname="eap7"))
    assert named_ap.suggest and named_ap.hub_type == "access_point"
    model = classify(Evidence(vendor="NETGEAR", hostname="gs308-office"))
    assert model.suggest and model.hub_type == "switch"
    light = classify(Evidence(vendor="Espressif Inc.", hostname="hallway-light-switch"))
    assert not light.suggest and not light.auto


def test_pretty_vendor():
    assert pretty_vendor("Ubiquiti Inc") == "Ubiquiti"
    assert pretty_vendor("TP-LINK TECHNOLOGIES CO.,LTD.") == "TP-LINK"
    assert pretty_vendor(None) is None


def test_extract_addresses():
    text = """Name        IP            MAC                 Signal
Kitchen TV  10.0.0.21     00-00-5E-00-53-10   -54 dBm  # example-ok: synthetic test value
Camera      10.0.0.22     0000.5e00.5311      -61 dBm  # example-ok: synthetic test value
Broadcast   10.0.0.255  # example-ok: synthetic test value
plug        00:00:5e:00:53:13 (no ip)"""
    macs, ips = extract_addresses(text)
    assert macs == [TV, CAM, PLUG]
    assert ips == ["10.0.0.21", "10.0.0.22"]  # example-ok: synthetic test value
