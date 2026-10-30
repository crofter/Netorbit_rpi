"""Decide whether a device on the network is a hub (router, switch, access point, mesh node).

Pure Python. Each signal adds points; the total decides between "add it", "suggest it"
and "ignore". Vendor names come from the IEEE registry via aiooui.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re

from .const import HUB_AP, HUB_MESH, HUB_SWITCH

# Makers that sell (almost) only networking gear.
NETWORK_VENDORS = (
    "ubiquiti", "eero", "snapav", "ruckus", "aruba", "meraki", "mikrotik", "routerboard",
    "cambium", "engenius", "plume design", "juniper", "extreme networks", "fortinet",
    "cradlepoint", "peplink", "edgecore", "grandstream", "omada", "luxul", "draytek",
)
# Makers whose MACs show up on routers but also on plugs, phones, TVs and laptops.
MIXED_VENDORS = (
    "tp-link", "netgear", "asustek", "linksys", "belkin", "d-link", "zyxel", "cisco",
    "google", "amazon", "avm", "arris", "commscope", "sagemcom", "technicolor", "huawei",
    "nokia", "calix", "actiontec", "buffalo", "tenda", "mercusys", "synology", "xiaomi",
)
# Brands that are only mesh systems.
MESH_VENDORS = ("eero", "plume design")

_AP_RE = re.compile(r"(^|[^a-z])(uap|u6|u7|eap\d*|wap|ap|access[- ]?point|ac-?lite|ac-?pro|nanohd|unifi-ap|ruckus|extender|repeater|range-?extender|re\d{3})([^a-z]|$)")
_SWITCH_RE = re.compile(r"(^|[^a-z])(usw|switch|sw\d*|sg\d+|gs\d+|poe|tl-sg)([^a-z]|$)")
_SWITCH_MODEL_RE = re.compile(r"(^|[^a-z])(usw|sw\d+|sg\d+|gs\d+|tl-sg|poe|switch-?\d+)")
_MESH_RE = re.compile(r"(deco|eero|orbi|satellite|google-?wifi|nest-?wifi|velop|mesh|amplifi|plume|superpod)")

AUTO_SCORE = 3
SUGGEST_SCORE = 2


@dataclass
class Evidence:
    """Everything known about one device that might be a hub."""

    mac: str | None = None
    ip: str | None = None
    hostname: str | None = None
    vendor: str | None = None
    registry_model: str | None = None  # from another integration's device entry
    registry_name: str | None = None
    registry_domain: str | None = None
    snmp_bridge: bool = False  # answers the BRIDGE-MIB (keeps a MAC table)
    snmp_descr: str | None = None
    ssdp_types: list[str] = field(default_factory=list)
    label: str | None = None  # a name the user already gave it elsewhere in Home Assistant (ping, device tracker)


@dataclass
class Verdict:
    """Is it a hub, what kind, and how sure."""

    score: int
    hub_type: str | None
    reasons: list[str]

    @property
    def auto(self) -> bool:
        return self.hub_type is not None and self.score >= AUTO_SCORE

    @property
    def suggest(self) -> bool:
        return self.hub_type is not None and SUGGEST_SCORE <= self.score < AUTO_SCORE


# Integrations whose device entries describe network gear.
NETWORK_DOMAINS = {
    "unifi", "tplink_omada", "ruckus_unleashed", "netgear", "fritz", "asuswrt", "mikrotik",
    "keenetic_ndms2", "luci", "openwrt", "aruba", "eero", "tplink_deco", "google_wifi",
}


def _kind_from_text(text: str) -> str | None:
    low = text.lower()
    if _MESH_RE.search(low):
        return HUB_MESH
    if _SWITCH_RE.search(low):
        return HUB_SWITCH
    if _AP_RE.search(low):
        return HUB_AP
    return None


def classify(ev: Evidence) -> Verdict:
    """Score the evidence."""
    score = 0
    reasons: list[str] = []
    kind: str | None = None
    vendor = (ev.vendor or "").lower()

    if ev.snmp_bridge:
        score += 3
        reasons.append("keeps a MAC address table (SNMP)")
        kind = _kind_from_text(ev.snmp_descr or "") or HUB_SWITCH
        if kind == HUB_MESH:
            kind = HUB_AP

    if any("wlanaccesspoint" in t.lower() for t in ev.ssdp_types):
        score += 3
        reasons.append("announces itself as an access point")
        kind = kind or HUB_AP

    reg_text = " ".join(x for x in (ev.registry_model, ev.registry_name) if x)
    if ev.registry_domain in NETWORK_DOMAINS and reg_text:
        reg_kind = _kind_from_text(reg_text)
        if reg_kind:
            score += 3
            reasons.append(f"listed as {reg_kind.replace('_', ' ')} by the {ev.registry_domain} integration")
            kind = kind or reg_kind

    # The network hostname, or failing that a name the user gave it elsewhere in Home Assistant.
    name_text = ev.hostname or ""
    host_kind = _kind_from_text(name_text)
    if not host_kind and ev.label:
        name_text = ev.label
        host_kind = _kind_from_text(name_text)
    if host_kind:
        # "ap-3", "eap2" or "deco-office" are strong hints. "switch" is weak: people name light switches that way.
        weak = host_kind == HUB_SWITCH and not _SWITCH_MODEL_RE.search(name_text.lower())
        score += 1 if weak else 2
        reasons.append(f"its name looks like a{'n' if host_kind == HUB_AP else ''} {host_kind.replace('_', ' ')}")
        kind = kind or host_kind

    if any(v in vendor for v in NETWORK_VENDORS):
        # Alone this is only a suggestion: some of these makers also sell cameras.
        score += 2
        reasons.append(f"made by {ev.vendor}")
        if kind is None and any(v in vendor for v in MESH_VENDORS):
            kind = HUB_MESH
        kind = kind or HUB_AP
    elif any(v in vendor for v in MIXED_VENDORS) and host_kind:
        # Mixed makers also sell plugs and cameras, so this only explains a suggestion.
        reasons.append(f"made by {ev.vendor}")

    return Verdict(score=score, hub_type=kind, reasons=reasons)


def pretty_vendor(vendor: str | None) -> str | None:
    """Shorten registry vendor names for display: 'Ubiquiti Inc' -> 'Ubiquiti'."""
    if not vendor:
        return None
    v = re.sub(r"[,.]?\s*(inc|ltd|llc|corp(oration)?|co|limited|gmbh|technologies|technology|s\.?a\.?|b\.?v\.?)\b\.?", "", vendor, flags=re.I)
    v = re.sub(r"\s+", " ", v).strip(" ,.")
    return v or vendor
