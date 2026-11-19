# WS Hub
# Copyright (C) 2026 Jeff Millar, WA1HCO
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Site-server URL defaults and first-run hosting choice.

A fresh install on one PC must just work: if no site server is already
reachable on the LAN, this PC runs one and points agents at localhost.
A remembered or pinned URL that points at another host is left alone so
a fleet seat does not start a second server when the real one is down.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlparse

# Loopback console — never a specific LAN address. Examples in help text
# may use a placeholder such as 192.168.x.y; they must not be defaults.
DEFAULT_HTTP_PORT = 8787
LOCAL_SITE = f"http://127.0.0.1:{DEFAULT_HTTP_PORT}"

_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def normalize_site_url(url: str | None) -> str:
    """Strip whitespace and a trailing slash; empty when unset."""
    return (url or "").strip().rstrip("/")


def site_url_host(url: str | None) -> str:
    """Hostname or IP from a console base URL (lowercased, no brackets)."""
    try:
        host = (urlparse(normalize_site_url(url) or "").hostname or "").strip().lower()
    except Exception:
        return ""
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def is_loopback_host(host: str | None) -> bool:
    return (host or "").strip().lower() in _LOOPBACK


def _local_ips(lan_ips: Iterable[str] | None = None) -> set[str]:
    if lan_ips is not None:
        return {ip.strip().lower() for ip in lan_ips if ip and str(ip).strip()}
    try:
        from wims.agent.report import _lan_ips
        return {ip.strip().lower() for ip in (_lan_ips() or []) if ip}
    except Exception:
        return set()


def site_url_is_this_pc(url: str | None, lan_ips: Iterable[str] | None = None) -> bool:
    """True when the URL points at this machine (loopback or a local IP)."""
    host = site_url_host(url)
    if not host:
        return False
    if is_loopback_host(host):
        return True
    return host in _local_ips(lan_ips)


def same_site_host(
    first: str | None,
    second: str | None,
    lan_ips: Iterable[str] | None = None,
) -> bool:
    """True when two console URLs refer to the same site.

    http://127.0.0.1:8787 and this PC's LAN address are the same site
    (the launcher often pins localhost; presence advertises the LAN IP).
    """
    a = normalize_site_url(first)
    b = normalize_site_url(second)
    if not a or not b:
        return True
    if a == b:
        return True
    ha, hb = site_url_host(a), site_url_host(b)
    if not ha or not hb:
        return False
    if ha == hb:
        return True
    local = _local_ips(lan_ips)
    if is_loopback_host(ha) and (is_loopback_host(hb) or hb in local):
        return True
    if is_loopback_host(hb) and (is_loopback_host(ha) or ha in local):
        return True
    return False


def should_host_local_site_server(
    *,
    discovery_done: bool,
    site_reachable: bool,
    last_site_url: str | None = None,
    env_server: str | None = None,
    lan_ips: Iterable[str] | None = None,
) -> bool:
    """Whether this PC should start the site server after a LAN search.

    Returns False until discovery has finished, when a server is already
    reachable, or when a remembered or pinned URL points at another host
    (a multi-PC seat waiting for that server). A missing last-site, a
    loopback last-site, or a last-site that is this PC itself means this
    machine should host when nothing answered.
    """
    if not discovery_done or site_reachable:
        return False
    env = normalize_site_url(env_server)
    if env and not site_url_is_this_pc(env, lan_ips):
        return False
    last = normalize_site_url(last_site_url)
    if last and not site_url_is_this_pc(last, lan_ips):
        return False
    return True


def adopt_discovered_server_url(
    current: str | None,
    discovered: str | None,
    *,
    configured: str | None = None,
    current_reachable: bool = True,
) -> str:
    """Pick the server URL an already-running agent should use.

    Keeps a working configured (or current) URL. If that URL is down and
    discovery found a different host, switches to the discovered URL so
    the agent does not need a manual restart.
    """
    disc = normalize_site_url(discovered)
    cur = normalize_site_url(current)
    if not disc:
        return cur
    if not cur:
        return disc
    if same_site_host(cur, disc):
        return cur
    if current_reachable and (normalize_site_url(configured) or cur):
        return cur
    return disc


# --- protected site-server control (who may host, takeover) ---------------- #

AFFECTS_STATION = "This affects every PC at the station."
START_CONFIRM = "No site server answered. Start one on this PC?"
STOP_CONFIRM = "Stop the site server on this PC? " + AFFECTS_STATION
RESTART_CONFIRM = "Restart the site server on this PC? " + AFFECTS_STATION
TWO_SERVERS_MESSAGE = (
    "Two site servers are running. The station owner should pick one."
)


def takeover_confirm_text(host: str | None) -> str:
    name = (host or "").strip() or "the other PC"
    return (
        f"The site server on {name} will stop, and this PC will take over. "
        "Work claims and Enable TX will be cleared. Continue?"
    )


def follow_other_confirm_text(host: str | None) -> str:
    name = (host or "").strip() or "the other PC"
    return (
        f"Stop the site server on this PC and use the one on {name}? "
        + AFFECTS_STATION
    )


def no_server_message(last_host: str | None) -> str:
    """What a PC that may not host says when no site server answers."""
    host = (last_host or "").strip()
    if host and not is_loopback_host(host):
        return (
            f"No site server found. The last one was on {host}. "
            "Ask the station owner to start it."
        )
    return "No site server found. Ask the station owner to start it."


def last_site_is_other_pc(
    last_site_url: str | None, lan_ips: Iterable[str] | None = None,
) -> bool:
    """True when the remembered site server is on another PC."""
    last = normalize_site_url(last_site_url)
    return bool(last) and not site_url_is_this_pc(last, lan_ips)


def site_buttons(
    *,
    may_host: bool,
    site_reachable: bool,
    server_here: bool,
    two_servers: bool,
    offer: bool,
    choice_ready: bool = True,
) -> dict[str, bool]:
    """Which site-server controls a launcher shows.

    A PC that may not host sees only the status line. A PC that may host
    sees Start (nothing answers), Restart (it hosts), "Make this PC the
    site server" (another PC hosts, or two are running), and "Follow the
    other" (two are running and one is here).
    """
    if not may_host:
        return {"checkbox": False, "start": False, "restart": False,
                "take_over": False, "follow_other": False}
    return {
        "checkbox": bool(offer),
        "start": bool(not site_reachable and offer),
        "restart": bool(site_reachable and server_here and choice_ready),
        "take_over": bool(
            (site_reachable and not server_here) or two_servers
        ),
        "follow_other": bool(two_servers and server_here),
    }
