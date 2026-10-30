"""Tests for the pure helpers."""

from datetime import datetime, timedelta, timezone

import pytest

from netorbit_pi.logic import (
    compute_status,
    fallback_name,
    guess_icon,
    is_random_mac,
    normalize_mac,
    parse_proc_arp,
    short_hostname,
    slugify_id,
    validate_subnet,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
DAY = timedelta(hours=24)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("00-00-5E-00-53-0B", "00:00:5e:00:53:0b"),
        ("00005e00530b", "00:00:5e:00:53:0b"),
        ("00:00:00:00:00:00", None),
        ("ff:ff:ff:ff:ff:ff", None),
        ("nope", None),
        (None, None),
    ],
)
def test_normalize_mac(raw, expected):
    assert normalize_mac(raw) == expected


def test_random_mac():
    assert is_random_mac("02:00:5e:00:00:0a")  # example-ok: synthetic test value
    assert is_random_mac("DA:00:00:00:00:01")  # example-ok: synthetic test value
    assert not is_random_mac("00:00:5e:00:53:0b")
    assert not is_random_mac(None)


def test_status():
    assert compute_status(True, None, None, NOW, DAY) == "online"
    assert compute_status(False, NOW - timedelta(hours=2), None, NOW, DAY) == "recent"
    assert compute_status(False, NOW - timedelta(hours=30), None, NOW, DAY) == "stale"
    # never seen, but only just started tracking: give it the full window
    assert compute_status(False, None, NOW - timedelta(hours=1), NOW, DAY) == "recent"
    assert compute_status(False, None, NOW - timedelta(hours=25), NOW, DAY) == "stale"


@pytest.mark.parametrize(
    ("name", "icon"),
    [
        ("Living-Room-AppleTV", "mdi:television-play"),
        ("iPhone", "mdi:cellphone"),
        ("hp-envy-printer", "mdi:printer"),
        ("roborock-s7", "mdi:robot-vacuum"),
        ("something", "mdi:lan-connect"),
        (None, "mdi:lan-connect"),
    ],
)
def test_guess_icon(name, icon):
    assert guess_icon(name) == icon


def test_short_hostname():
    assert short_hostname("kitchen-speaker.lan.") == "kitchen-speaker"
    assert short_hostname("192.0.2.5") is None
    assert short_hostname("192-0-2-5.example") is None
    assert short_hostname("") is None


def test_fallback_name():
    assert fallback_name("00:00:5e:00:53:0b", None) == "Device 53:0B"
    assert fallback_name(None, "192.0.2.9") == "Device 192.0.2.9"


def test_parse_arp():
    text = (
        "IP address       HW type     Flags       HW address            Mask     Device\n"
        "192.0.2.10       0x1         0x2         00:00:5e:00:53:0b     *        eth0\n"
        "192.0.2.11       0x1         0x0         00:00:00:00:00:00     *        eth0\n"
        "192.0.2.12       0x1         0x2         00:00:5E:00:53:0C     *        eth0\n"
    )
    assert parse_proc_arp(text) == {"192.0.2.10": "00:00:5e:00:53:0b", "192.0.2.12": "00:00:5e:00:53:0c"}


def test_validate_subnet():
    assert validate_subnet("192.0.2.7/24", 1024) == "192.0.2.0/24"
    with pytest.raises(ValueError, match="too_large"):
        validate_subnet("10.0.0.0/16", 1024)  # example-ok: synthetic test value
    with pytest.raises(ValueError):
        validate_subnet("not a subnet", 1024)


def test_slug():
    assert slugify_id("Hallway AP #2") == "hallway_ap_2"
    assert slugify_id("!!!") == "item"


def test_useful_name_skips_addresses():
    from netorbit_pi.logic import ping_label, useful_name

    assert useful_name("Pool") == "Pool"
    assert useful_name("  Living   Room ") == "Living Room"
    assert useful_name("00:00:5e:00:53:1e") is None
    assert useful_name("00:00:5e:00:53:1e Speaker 53:1E") is None
    assert useful_name("controller_ea1_00005E005342") is None
    assert useful_name("192.0.2.20") is None
    assert useful_name("") is None and useful_name(None) is None
    assert useful_name("Office Printer 2") == "Office Printer 2"
    assert ping_label("Ping Hallway AP") == "Hallway AP"
    assert ping_label("ping: NAS") == "NAS"
    assert ping_label("Pinger box") == "Pinger box"
    assert ping_label("192.0.2.9") is None


def test_label_helps_hub_detection():
    from netorbit_pi.detect import Evidence, classify

    sw = classify(Evidence(vendor="SnapAV", label="Office Switch 16"))
    assert sw.auto and sw.hub_type == "switch"
    ap = classify(Evidence(vendor="SnapAV", label="Hallway AP"))
    assert ap.auto and ap.hub_type == "access_point"
    # A user's name for a light switch is not network gear.
    light = classify(Evidence(vendor="Espressif Inc.", label="Hallway Light Switch"))
    assert not light.suggest and not light.auto
    # The hostname still wins over the label when both say something.
    both = classify(Evidence(vendor="Ubiquiti", hostname="usw-lite-8", label="Hallway AP"))
    assert both.hub_type == "switch"


def test_icon_from_vendor_when_name_says_nothing():
    from netorbit_pi.logic import guess_icon_from

    assert guess_icon_from("Living Room TV", "Apple, Inc.") == "mdi:television"  # name wins
    assert guess_icon_from("Apple 28:54", "Apple, Inc.") == "mdi:cellphone-link"
    assert guess_icon_from("Sonos 28:F1", "Sonos, Inc.") == "mdi:speaker"
    assert guess_icon_from("Device 12:34", "ecobee inc") == "mdi:thermostat"
    assert guess_icon_from("Device 12:34", "Realtek Semiconductor") == "mdi:lan-connect"
    assert guess_icon_from("Device 12:34", None) == "mdi:lan-connect"

