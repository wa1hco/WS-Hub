# WS Hub
# Copyright (C) 2026 Jeff Millar, WA1HCO
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Plain-language site-server status for the launcher and local status page.

Never show a bare CIDR such as 192.168.2.113/24. The operator sees a host
address and port, or a short sentence when no server is found.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from wims.discovery.presence import plain_ipv4

DEFAULT_HTTP_PORT = 8787


def host_and_port(url_or_addr: str | None, *, default_port: int = DEFAULT_HTTP_PORT) -> tuple[str, int]:
    """Host address and TCP port from a URL or ``ip[:port]``, prefix stripped."""
    text = (url_or_addr or "").strip()
    port = default_port
    if not text:
        return "", port
    if "://" in text or text.lower().startswith("http"):
        try:
            parsed = urlsplit(text if "://" in text else f"http://{text}")
            host = plain_ipv4(parsed.hostname or "") or (parsed.hostname or "")
            if parsed.port:
                port = int(parsed.port)
            return host, port
        except ValueError:
            pass
    raw = text.split()[0]
    if raw.count(":") == 1 and not raw.startswith("["):
        maybe, _, p = raw.partition(":")
        if p.isdigit():
            return plain_ipv4(maybe) or maybe, int(p)
    return plain_ipv4(raw) or raw, port


def format_endpoint(url_or_addr: str | None, *, default_port: int = DEFAULT_HTTP_PORT) -> str:
    """``192.168.2.113:8787`` with no scheme and no CIDR."""
    host, port = host_and_port(url_or_addr, default_port=default_port)
    if not host:
        return ""
    return f"{host}:{int(port)}"


def canonical_site_base(url_or_addr: str | None, *, default_port: int = DEFAULT_HTTP_PORT) -> str:
    """``http://192.168.2.113:8787`` with typo loopback and CIDR stripped."""
    host, port = host_and_port(url_or_addr, default_port=default_port)
    if not host:
        return ""
    return f"http://{host}:{int(port)}"


def is_loopback_host(host: str) -> bool:
    h = (host or "").strip().lower()
    return h in {"127.0.0.1", "localhost", "::1", "128.0.0.1"}


def choose_site_urls(
    *,
    this_pc: bool,
    loopback: str = "",
    lan: str = "",
    loopback_ok: bool = False,
    lan_ok: bool = False,
    default_port: int = DEFAULT_HTTP_PORT,
) -> tuple[str, str]:
    """Return (url_to_open, url_to_show).

    On the PC that hosts the server, the window opens at 127.0.0.1 (the
    firewall never blocks this PC from itself) and the status line shows
    the LAN address other computers should use. Everywhere else, both
    are the one reachable LAN URL.
    """
    loopback = canonical_site_base(loopback, default_port=default_port)
    lan = canonical_site_base(lan, default_port=default_port)
    if this_pc and loopback_ok:
        show = lan or loopback
        return loopback or show, show
    if lan_ok:
        return lan, lan
    if loopback_ok:
        return loopback, lan or loopback
    # Not reachable: still show the LAN address when we know it, not loopback.
    show = lan or loopback
    return show, show


def format_site_server_status(
    *,
    this_pc: bool = False,
    running: bool = False,
    reachable: bool = False,
    announced: bool = False,
    hostname: str = "",
    address: str = "",
    port: int = DEFAULT_HTTP_PORT,
) -> str:
    """One plain sentence for the site server line."""
    endpoint = format_endpoint(address, default_port=port) if address else ""
    if not endpoint and port:
        host = plain_ipv4(hostname) if hostname and hostname[0].isdigit() else ""
        if host:
            endpoint = f"{host}:{int(port)}"
    name = (hostname or "").strip()
    if name and (name[0].isdigit() or "/" in name or ":" in name):
        # Do not repeat the address as the name.
        if plain_ipv4(name) == (endpoint.split(":")[0] if endpoint else ""):
            name = ""

    if this_pc and running and endpoint:
        return f"Site server: this PC, running at {endpoint}"
    if this_pc and running:
        return "Site server: this PC, running"
    if reachable and endpoint and name:
        return f"Site server: {name} at {endpoint}, reachable"
    if reachable and endpoint:
        return f"Site server: {endpoint}, reachable"
    if (announced or address) and endpoint and name:
        return f"Site server: {name} at {endpoint}, not reachable from this PC"
    if (announced or address) and endpoint:
        return f"Site server: {endpoint}, not reachable from this PC"
    return "Site server: not found, choose one in Setup"


def linux_firewall_may_block_incoming() -> bool:
    """True when this Linux PC's firewall is on and defaults to dropping inbound.

    Does not change the firewall. Used only to warn that other PCs may not
    reach a site server running here.
    """
    from pathlib import Path
    import sys

    if not sys.platform.startswith("linux"):
        return False
    conf = Path("/etc/ufw/ufw.conf")
    default = Path("/etc/default/ufw")
    enabled = False
    drops = False
    try:
        if conf.is_file():
            text = conf.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                if line.strip().replace(" ", "").upper() == "ENABLED=YES":
                    enabled = True
                    break
    except OSError:
        return False
    try:
        if default.is_file():
            text = default.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                if "DEFAULT_INPUT_POLICY" in line and "DROP" in line.upper():
                    drops = True
                    break
    except OSError:
        drops = enabled
    return bool(enabled and drops)


def firewall_block_hint() -> str:
    """Plain warning when this PC may be hiding the site console from the LAN."""
    if not linux_firewall_may_block_incoming():
        return ""
    return (
        "This PC's firewall may be blocking other computers from the site "
        "console. Allow TCP port 8787 and UDP port 8788 on the station "
        "network, or the console will only work on this PC."
    )
