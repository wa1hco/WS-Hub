# WIMS — WSJT-X Instance Management System
# Copyright (C) 2026 Jeff Millar, WA1HCO
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Plain-language site server status never shows a bare CIDR."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wims.discovery.presence import (  # noqa: E402
    beacon_endpoints,
    build_beacon,
    plain_ipv4,
)
from wims.launcher.site_status import (  # noqa: E402
    canonical_site_base,
    choose_site_urls,
    format_endpoint,
    format_site_server_status,
    host_and_port,
)


def test_plain_ipv4_strips_cidr_and_url():
    assert plain_ipv4("192.168.2.113/24") == "192.168.2.113"
    assert plain_ipv4("192.168.2.113") == "192.168.2.113"
    assert plain_ipv4("http://192.168.2.113:8787") == "192.168.2.113"
    assert plain_ipv4("http://192.168.2.113/24") == "192.168.2.113"
    assert plain_ipv4("") == ""


def test_format_endpoint_never_includes_cidr():
    assert format_endpoint("http://192.168.2.113:8787") == "192.168.2.113:8787"
    assert format_endpoint("192.168.2.113/24") == "192.168.2.113:8787"
    assert format_endpoint("192.168.2.113/24:8787") == "192.168.2.113:8787"
    assert "/24" not in format_endpoint("192.168.2.113/24")


def test_host_and_port():
    assert host_and_port("http://192.168.2.113:8787") == ("192.168.2.113", 8787)
    assert host_and_port("192.168.2.113/24") == ("192.168.2.113", 8787)


def test_status_this_pc():
    text = format_site_server_status(
        this_pc=True, running=True, address="http://192.168.2.165:8787",
    )
    assert text == "Site server: this PC, running at 192.168.2.165:8787"
    assert "/24" not in text


def test_status_reachable_and_unreachable():
    ok = format_site_server_status(
        reachable=True, hostname="dell-7540",
        address="http://192.168.2.113:8787",
    )
    assert ok == "Site server: dell-7540 at 192.168.2.113:8787, reachable"
    bad = format_site_server_status(
        announced=True, hostname="dell-7540",
        address="192.168.2.113/24",
        port=8787,
    )
    assert bad == "Site server: dell-7540 at 192.168.2.113:8787, not reachable from this PC"
    assert "/24" not in bad


def test_status_not_found():
    assert format_site_server_status() == "Site server: not found, choose one in Setup"


def test_build_beacon_strips_cidr_from_lan_ips():
    b = build_beacon(
        instance_id="abc",
        http_port=8787,
        iface="0.0.0.0",
        lan_ip="192.168.2.113/24",
        lan_ips=["192.168.2.113/24"],
        hostname="dell-7540",
    )
    assert b["lan_ips"] == ["192.168.2.113"]
    assert b["console_base"] == "http://192.168.2.113:8787"
    assert "/24" not in b["console_base"]
    assert beacon_endpoints(b) == ["http://192.168.2.113:8787"]


def test_128_loopback_typo_is_corrected():
    assert plain_ipv4("128.0.0.1") == "127.0.0.1"
    assert plain_ipv4("http://128.0.0.1:8787") == "127.0.0.1"
    assert canonical_site_base("http://128.0.0.1:8787") == "http://127.0.0.1:8787"
    assert format_endpoint("128.0.0.1:8787") == "127.0.0.1:8787"


def test_choose_site_urls_this_pc_opens_loopback_shows_lan():
    open_url, show = choose_site_urls(
        this_pc=True,
        loopback="http://127.0.0.1:8787",
        lan="http://192.168.2.113:8787",
        loopback_ok=True,
        lan_ok=True,
    )
    assert open_url == "http://127.0.0.1:8787"
    assert show == "http://192.168.2.113:8787"


def test_choose_site_urls_seat_uses_lan_everywhere():
    open_url, show = choose_site_urls(
        this_pc=False,
        loopback="http://127.0.0.1:8787",
        lan="http://192.168.2.113:8787",
        loopback_ok=False,
        lan_ok=True,
    )
    assert open_url == show == "http://192.168.2.113:8787"


def test_choose_site_urls_unreachable_shows_lan_not_loopback():
    open_url, show = choose_site_urls(
        this_pc=False,
        loopback="http://127.0.0.1:8787",
        lan="http://192.168.2.113:8787",
        loopback_ok=False,
        lan_ok=False,
    )
    assert open_url == show == "http://192.168.2.113:8787"



if __name__ == "__main__":
    import unittest
    # allow running as a script with pytest-less asserts via a tiny runner
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(name, "ok")
    print("all ok")
