# WS Hub
# Copyright (C) 2026 Jeff Millar, WA1HCO
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""One read-only cluster connection.

Login is the callsign and nothing else. A blank line is sent once when the
socket opens, so a node that waits for the client will speak. Telnet option
refusals are sent because the socket will not stay in plain text without
them. No spot, no announcement, no ``set/`` filter, no ``sh/dx``. The node
streams every spot after login. WS Hub chooses bands and modes locally.

AR-Cluster asks ``Please enter your call:``. DX-Spider asks ``login:``.
CC Cluster asks the same kind of call prompt. The match is any case.

A TCP session that sends no bytes is not logged in. After 60 seconds with
no banner the status says the node sent nothing. Reconnect waits, starting
at a few seconds and doubling up to one minute. The socket is closed before
that wait, so a dropped session does not keep the login busy. This thread
holds one socket.
"""

from __future__ import annotations

import re
import socket
import threading
import time
from collections.abc import Callable

from wims.engine import scoring as S
from wims.integrations.cluster.parse import parse_spot, valid_cluster_login
from wims.integrations.cluster.psk import PskOpenings, hearings_from_decodes
from wims.integrations.cluster.skim import DEFAULT_SKIM_IDS, id_patterns, skim_line
from wims.integrations.cluster.settings import (
    DEFAULT_TTL_S, MODE_FILTERS, allowed_bands, allowed_modes, known_nodes,
    load, save, sanitize,
    call_mismatch_warning, effective_call, effective_grid,
    clean_station_call, clean_station_grid,
)
from wims.integrations.cluster.telnet import plaintext, strip_telnet
from wims.integrations.cluster.view import (
    SpotBook, band_choices, build_list, mode_choices, status_detail, status_line,
)

# A prompt that means "send the callsign". Any case. AR-Cluster says
# "Please enter your call:", DX-Spider says "login:", CC Cluster says "call:".
_LOGIN = re.compile(
    r"(?:please\s+enter\s+(?:your\s+)?call(?:sign)?|"
    r"\blogin\s*:|"
    r"\bcallsign\s*:|"
    r"(?<![A-Za-z])call\s*:)",
    re.IGNORECASE,
)
# The node wants a name, a QTH, or a password. We do not answer.
_EXTRA = re.compile(
    r"(?:your name|name|qth|password|e-?mail)\s*:",
    re.IGNORECASE,
)
_REFUSED = re.compile(
    r"(invalid call|bad call|not a valid call|access denied|rejected)",
    re.IGNORECASE,
)

BACKOFF_START = 5.0
BACKOFF_CAP = 60.0
LOGIN_WAIT_S = 8.0
QUIET_S = 180.0
# TCP is up and the node has sent nothing. Do not call that logged in.
SILENT_S = 60.0


def next_backoff(previous: float) -> float:
    """Seconds to wait after a failed connection. A few seconds, then up to 60."""
    if previous < BACKOFF_START:
        return BACKOFF_START
    return min(BACKOFF_CAP, previous * 2.0)


class LinkStop(Exception):
    def __init__(self, phase: str, reason: str):
        super().__init__(phase)
        self.phase = phase
        self.reason = reason


class ClusterLink:
    """What one connection is allowed to send. Tests feed it bytes."""

    def __init__(self, callsign: str, send: Callable[[bytes], None],
                 now: Callable[[], float] | None = None):
        self.callsign = callsign
        self._send = send
        self._now = now or time.monotonic
        self._started = self._now()
        self._sent_login = False
        self._woke = False
        self.bytes_seen = False
        self._buf = ""
        self._telnet: dict = {}
        self.refused = False
        self.extra = False

    @property
    def sent_login(self) -> bool:
        return self._sent_login

    def wake(self) -> None:
        """One blank line so a quiet node starts talking. Not a command."""
        if self._woke:
            return
        self._woke = True
        self._send(b"\r\n")

    def feed(self, data: bytes) -> list:
        if data:
            self.bytes_seen = True
        reply = bytearray()
        plain = strip_telnet(data or b"", reply, self._telnet)
        if reply:
            self._send(bytes(reply))
        if plain:
            self._buf += plain.decode("latin-1", "replace").replace("\x00", "")
        spots = []
        while True:
            line, rest, complete = _take_line(self._buf)
            if not complete:
                self._consider(self._buf, partial=True)
                break
            self._buf = rest
            self._consider(line, partial=False)
            if self.refused or self.extra:
                break
            spot = parse_spot(line)
            if spot is not None:
                spots.append(spot)
        return spots

    def poll(self) -> None:
        """Send the callsign if the node spoke and never asked.

        A socket that has sent no bytes does not get the callsign. That
        session is not logged in.
        """
        if self._sent_login or self.refused or self.extra:
            return
        if not self.bytes_seen:
            return
        if self._now() - self._started < LOGIN_WAIT_S:
            return
        if _EXTRA.search(self._buf):
            self.extra = True
            return
        self._send_login()

    def _consider(self, text: str, *, partial: bool) -> None:
        if _REFUSED.search(text or ""):
            self.refused = True
            return
        if _EXTRA.search(text or ""):
            self.extra = True
            return
        if self._sent_login:
            return
        if is_login_prompt(text):
            self._send_login()
            if partial:
                self._buf = ""

    def _send_login(self) -> None:
        if self._sent_login:
            return
        if not valid_cluster_login(self.callsign):
            return
        self._send((self.callsign.strip().upper() + "\r\n").encode("ascii"))
        self._sent_login = True


def is_login_prompt(text: str) -> bool:
    line = (text or "").strip()
    if not line or line.upper().startswith("DX DE"):
        return False
    if _EXTRA.search(line):
        return False
    return _LOGIN.search(line) is not None


def _take_line(buf: str) -> tuple[str, str, bool]:
    for mark in ("\r\n", "\n", "\r"):
        at = buf.find(mark)
        if at >= 0:
            return buf[:at], buf[at + len(mark):], True
    return buf, "", False


def _reason_for_os(exc: BaseException) -> str:
    if isinstance(exc, socket.gaierror):
        return "dns"
    if isinstance(exc, ConnectionRefusedError):
        return "refused_port"
    if isinstance(exc, TimeoutError):
        text = str(exc).lower()
        if "quiet" in text:
            return "quiet"
        if "closed" in text:
            return "closed"
        return "timeout"
    return "timeout"


class ClusterService:
    """Spot store plus the one connection. ``start`` runs the thread."""

    def __init__(self, path=None, connect=None, sleep=None, monotonic=None):
        self.path = path
        self._connect = connect or _default_connect
        self._sleep = sleep  # tests replace _pause; kept so a stub is obvious
        self._monotonic = monotonic or time.monotonic
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._callsign_fn: Callable[[], str] | None = None
        self._book = SpotBook()
        self._settings = load(path)
        self._openings = PskOpenings()
        self._gen = 0
        self._sock = None
        self.phase = "off"
        self.reason = ""
        self.retry_s = 0.0
        self.login_call = ""
        self.lines = 0
        self.node_spoke = False
        self.login_sent = False
        self.saw_lines = False
        self.last_spot_at: float | None = None
        self._selected: dict | None = None

    def start(self, callsign_fn: Callable[[], str]) -> None:
        self._callsign_fn = callsign_fn
        self._stop.clear()
        if self._thread is not None and self._thread.is_alive():
            self._wake.set()
            return
        self._thread = threading.Thread(
            target=self._run, name="wims-cluster", daemon=True,
        )
        self._thread.start()

    def start_openings(self) -> None:
        """Site server only. Seats must not start a second poller."""
        self._openings.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._close_socket()
        self._openings.stop()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def update(self, body: dict) -> dict:
        current = self.settings()
        merged = dict(current)
        if isinstance(body, dict):
            for key in (
                "enabled", "node", "host", "port", "bands", "modes",
                "qsy_cost", "station_call", "cluster_login", "station_grid",
                "psk_rank", "show_cluster", "show_skim", "skim_ids",
            ):
                if key in body:
                    merged[key] = body[key]
        cleaned = sanitize(merged)
        save(cleaned, self.path)
        reconnect = any(
            cleaned.get(k) != current.get(k)
            for k in ("enabled", "node", "host", "port", "station_call", "cluster_login")
        ) or any(
            cleaned.get(k) != current.get(k)
            for k in ("bands", "modes", "qsy_cost")
        )
        with self._lock:
            self._settings = cleaned
            if reconnect:
                self._gen += 1
            if not cleaned["enabled"]:
                self.phase = "off"
                self.reason = ""
                self.node_spoke = False
                self.login_sent = False
                self.saw_lines = False
                self.last_spot_at = None
                # Skim rows are not from the cluster; keep them.
                self._book.drop_cluster()
        if reconnect:
            self._close_socket()
            self._wake.set()
        return cleaned

    def settings(self) -> dict:
        with self._lock:
            return dict(self._settings)

    def remember_call_if_unset(self, call: str) -> str:
        """Fill a blank Setup station call once, without dropping the connection.

        Does not overwrite a call the operator already saved. Returns the
        call now stored, which may still be blank.
        """
        call = clean_station_call(call)
        with self._lock:
            current = clean_station_call(self._settings.get("station_call"))
            if current:
                return current
            if not call:
                return ""
            settings = dict(self._settings)
            settings["station_call"] = call
            self._settings = settings
        save(settings, self.path)
        return call

    def remember_grid_if_unset(self, grid: str) -> str:
        """Fill a blank Setup Station grid from a live WSJT-X grid.

        Does not overwrite a grid the operator already saved, and does
        not accept a value that is not a Maidenhead grid. Returns the
        grid now stored, which may still be blank.
        """
        grid = clean_station_grid(grid)
        with self._lock:
            current = clean_station_grid(self._settings.get("station_grid"))
            if current:
                return current
            if not grid:
                return ""
            settings = dict(self._settings)
            settings["station_grid"] = grid
            self._settings = settings
        save(settings, self.path)
        return grid

    def set_selected(self, selected: dict | None) -> None:
        with self._lock:
            self._selected = dict(selected) if selected else None

    def skim_patterns(self) -> list[str]:
        """Setup's skimmer UDP ids, as patterns (default WS Skim's)."""
        with self._lock:
            return id_patterns(self._settings.get("skim_ids"))

    def add_skim(self, hit, now: float) -> None:
        """One WS Skim decode heard here. Never touches a WSJT-X instance."""
        with self._lock:
            self._book.add_skim(
                call=hit.call, band=hit.band, mode=hit.mode,
                freq_hz=hit.freq_hz, grid=hit.grid, receiver=hit.receiver,
                snr=hit.snr, now=now,
            )
            self._book.prune(now, DEFAULT_TTL_S)

    def ingest(self, line: str, now: float) -> bool:
        """Test helper. Parse one line into the book. No network."""
        spot = parse_spot(line)
        if spot is None:
            return False
        with self._lock:
            self._book.add(spot, now)
            self._book.prune(now, DEFAULT_TTL_S)
            self.lines += 1
            self.saw_lines = True
            self.last_spot_at = now
        return True

    def public(self, *, now: float, callsign: str, our_grid: str | None,
               operations, classify, grid_for=None, rover_for=None,
               spotter_grid=None, entity_latlon=None, live_calls=None,
               live_grid: str | None = None, decodes=None,
               skim_decodes=None, skim: dict | None = None) -> dict:
        with self._lock:
            settings = dict(self._settings)
            self._book.prune(now, DEFAULT_TTL_S)
            groups = self._book.rows()
            phase = self.phase
            reason = self.reason
            retry_s = self.retry_s
            wanted = effective_call(settings, callsign)
            login = self.login_call or wanted
            selected = dict(self._selected) if self._selected else None
            saw_lines = self.saw_lines
            node_spoke = self.node_spoke
            login_sent = self.login_sent
            last_spot_at = self.last_spot_at
        if not clean_station_call(settings.get("station_call")) and wanted:
            remembered = self.remember_call_if_unset(wanted)
            if remembered:
                settings["station_call"] = remembered
                wanted = remembered
                login = login or remembered
        home = effective_grid(settings, live_grid or our_grid)
        if not clean_station_grid(settings.get("station_grid")) and home:
            remembered_grid = self.remember_grid_if_unset(home)
            if remembered_grid:
                settings["station_grid"] = remembered_grid
                home = remembered_grid
        if home:
            our_grid = home
        self._openings.configure(
            grid=home,
            enabled=bool(settings.get("enabled")) and bool(settings.get("psk_rank")),
        )
        allowed = allowed_bands(settings)
        modes = allowed_modes(settings)
        bands_empty = allowed is not None and not allowed
        modes_empty = modes is not None and not modes
        if not settings.get("enabled"):
            phase = "off"
        elif phase == "off":
            phase = "need_call" if not valid_cluster_login(wanted) else "connecting"
        local = []
        if settings.get("psk_rank") and home:
            # Our own WSJT-X decodes and WS Skim decodes both show the
            # band is open from here.
            local = hearings_from_decodes(
                list(decodes or []) + list(skim_decodes or []),
                home_grid=home, now=now, grid_for=grid_for,
            )

        # Skim adds many local decodes. Hand each row only its own band's.
        local_by_band: dict = {}
        for sample in local:
            local_by_band.setdefault(sample.band, []).append(sample)

        def opening_for(band, az):
            if not settings.get("psk_rank"):
                return None
            return self._openings.opening(
                band=band, az=az, now=now, local=local_by_band.get(band, []),
            )

        rows, drops = ([], {"spot_count": 0})
        cluster_on = bool(settings.get("enabled"))
        has_skim = any(group.skimmers for group in groups)
        can_list = (
            (cluster_on or has_skim) and not bands_empty and not modes_empty
        )
        if can_list and ((cluster_on and phase == "connected") or groups):
            # Still show spots we already have while reconnecting, and
            # Skim spots with the cluster off.
            rows, drops = build_list(
                groups, now=now, ttl_s=DEFAULT_TTL_S, our_grid=our_grid,
                allowed=allowed, operations=operations, classify=classify,
                grid_for=grid_for, rover_for=rover_for, spotter_grid=spotter_grid,
                qsy_cost=settings.get("qsy_cost", 8),
                allowed_modes=modes,
                entity_latlon=entity_latlon,
                opening_for=opening_for if settings.get("psk_rank") else None,
                show_cluster=cluster_on and bool(settings.get("show_cluster", True)),
                show_skim=bool(settings.get("show_skim", True)),
            )
        spot_count = int(drops.get("spot_count") or 0)
        have_grid = bool((our_grid or "").strip())
        if not have_grid:
            rows = []
        line = status_line(
            enabled=bool(settings.get("enabled")), phase=phase,
            host=settings.get("host") or "", port=int(settings.get("port") or 0),
            callsign=login, retry_s=retry_s, reason=reason,
            spot_count=spot_count, needed_count=len(rows),
            bands_empty=bands_empty, have_grid=have_grid,
            have_lines=saw_lines, modes_empty=modes_empty,
        )
        weights = S.cluster_weights(settings.get("qsy_cost", 8))
        last_age = None if last_spot_at is None else max(0.0, now - last_spot_at)
        detail = status_detail(
            host=settings.get("host") or "", port=int(settings.get("port") or 0),
            callsign=login, qsy_cost=int(settings.get("qsy_cost") or 0),
            bands=None if allowed is None else sorted(allowed),
            ttl_s=DEFAULT_TTL_S, drops=drops, weights=weights,
            last_spot_age=last_age, node_spoke=node_spoke, login_sent=login_sent,
            phase=phase,
            modes=None if modes is None else [
                name for name in MODE_FILTERS if name in modes
            ],
        )
        selected_line = ""
        if selected and selected.get("call"):
            selected_line = selected.get("line") or ""
        psk_snap = self._openings.snapshot(now)
        if not settings.get("psk_rank"):
            psk_snap = dict(psk_snap)
            psk_snap["status_line"] = "PSK Reporter: off"
        return {
            "enabled": bool(settings.get("enabled")),
            "node": settings.get("node") or "custom",
            "nodes": known_nodes(),
            "host": settings.get("host"),
            "port": int(settings.get("port") or 0),
            "bands": "all" if allowed is None else sorted(allowed),
            "all_bands": allowed is None,
            "band_choices": band_choices(settings.get("bands")),
            "modes": "all" if modes is None else [
                name for name in MODE_FILTERS if name in modes
            ],
            "all_modes": modes is None,
            "mode_choices": mode_choices(settings.get("modes")),
            "qsy_cost": int(settings.get("qsy_cost") or 0),
            "station_call": settings.get("station_call") or "",
            "cluster_login": settings.get("cluster_login") or "",
            "station_grid": settings.get("station_grid") or "",
            "psk_rank": bool(settings.get("psk_rank")),
            "show_cluster": bool(settings.get("show_cluster", True)),
            "show_skim": bool(settings.get("show_skim", True)),
            "skim_ids": settings.get("skim_ids") or DEFAULT_SKIM_IDS,
            "skim_line": (skim or {}).get("line") or skim_line([], 0),
            "skim_receivers": (skim or {}).get("receivers") or [],
            "skim_count": sum(1 for row in rows if row.get("heard_here")),
            "psk_line": psk_snap["status_line"],
            "callsign": login,
            "call_warning": call_mismatch_warning(
                settings.get("station_call") or "", live_calls,
            ),
            "phase": phase,
            "status_line": line,
            "detail": detail,
            "spot_count": spot_count,
            "needed_count": len(rows),
            "weights": weights,
            "rows": rows,
            "selected": selected,
            "selected_line": selected_line,
        }

    def _mark_retry(self, phase: str, reason: str, backoff: float) -> None:
        """Record a failed attempt. Login flags do not carry into the wait."""
        with self._lock:
            if self.phase == "off" and phase == "retry":
                return
            self.phase = phase
            self.reason = reason
            self.retry_s = backoff
            self.login_sent = False
            self.node_spoke = False

    def _pause(self, seconds: float) -> None:
        """Wait, but wake immediately when Setup changes or the server stops."""
        if self._sleep is not None:
            self._sleep(seconds)
            return
        self._wake.wait(seconds)
        self._wake.clear()

    def _close_socket(self) -> None:
        with self._lock:
            sock = self._sock
            self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _run(self) -> None:
        backoff = 0.0
        while not self._stop.is_set():
            settings = self.settings()
            if not settings.get("enabled"):
                with self._lock:
                    self.phase = "off"
                    self.reason = ""
                backoff = 0.0
                self._pause(2.0)
                continue
            call = ""
            if self._callsign_fn is not None:
                try:
                    call = (self._callsign_fn() or "").strip().upper()
                except Exception:
                    call = ""
            call = effective_call(settings, call)
            if not settings.get("station_call"):
                # Remember a plain station call, never an SSID.
                self.remember_call_if_unset(call)
            if not valid_cluster_login(call):
                with self._lock:
                    self.phase = "need_call"
                    self.login_call = ""
                self._pause(5.0)
                continue
            with self._lock:
                self.phase = "connecting"
                self.login_call = call
                self.reason = ""
                self.node_spoke = False
                self.login_sent = False
                gen = self._gen
                host = settings["host"]
                port = int(settings["port"])
            try:
                sock = self._connect((host, port), 10.0)
            except Exception as exc:
                backoff = next_backoff(backoff)
                self._mark_retry("retry", _reason_for_os(exc), backoff)
                self._pause(backoff)
                continue
            wait = 0.0
            try:
                with self._lock:
                    self._sock = sock
                    self.phase = "tcp"
                    self.reason = ""
                    self.login_sent = False
                    self.node_spoke = False
                self._read_loop(sock, call, gen)
                backoff = 0.0
            except LinkStop as stop:
                backoff = next_backoff(backoff)
                self._mark_retry(stop.phase, stop.reason, backoff)
                wait = backoff
            except Exception as exc:
                backoff = next_backoff(backoff)
                self._mark_retry("retry", _reason_for_os(exc), backoff)
                wait = backoff
            finally:
                self._close_socket()
            if wait and not self._stop.is_set():
                self._pause(wait)

    def _note_progress(self, link: ClusterLink, saw_bytes: bool) -> None:
        """TCP up is not logged in. Logged in is not the same as spots."""
        with self._lock:
            self.node_spoke = saw_bytes
            self.login_sent = bool(link.sent_login and saw_bytes)
            self.retry_s = 0.0
            self.reason = ""
            if self.phase == "connected":
                return
            if self.login_sent:
                self.phase = "logged_in"
            else:
                self.phase = "tcp"

    def _read_loop(self, sock, callsign: str, gen: int) -> None:
        sock.settimeout(1.0)
        link = ClusterLink(callsign, sock.sendall, now=self._monotonic)
        link.wake()
        opened = self._monotonic()
        last_byte = None
        saw_bytes = False
        while not self._stop.is_set():
            with self._lock:
                if self._gen != gen or not self._settings.get("enabled"):
                    return
            try:
                data = sock.recv(4096)
            except socket.timeout:
                data = None
            except OSError as exc:
                with self._lock:
                    if self._gen != gen or not self._settings.get("enabled"):
                        return
                raise LinkStop("retry", "closed") from exc
            if data is None:
                link.poll()
                if link.extra:
                    raise LinkStop("extra", "extra")
                if link.refused:
                    raise LinkStop("refused", "login")
                self._note_progress(link, saw_bytes)
                now_m = self._monotonic()
                if not saw_bytes and now_m - opened >= SILENT_S:
                    raise LinkStop("silent", "silent")
                if last_byte is not None and now_m - last_byte > QUIET_S:
                    raise TimeoutError("quiet")
                continue
            if data == b"":
                with self._lock:
                    if self._gen != gen or not self._settings.get("enabled"):
                        return
                raise LinkStop("retry", "closed")
            saw_bytes = True
            last_byte = self._monotonic()
            spots = link.feed(data)
            if link.extra:
                raise LinkStop("extra", "extra")
            if link.refused:
                raise LinkStop("refused", "login")
            self._note_progress(link, True)
            if spots:
                received = time.time()
                with self._lock:
                    for spot in spots:
                        self._book.add(spot, received)
                        self.lines += 1
                    self._book.prune(received, DEFAULT_TTL_S)
                    self.saw_lines = True
                    self.last_spot_at = received
                    self.phase = "connected"
                    self.login_sent = True
                    self.node_spoke = True


def _default_connect(address, timeout: float):
    return socket.create_connection(address, timeout)


def sent_text(data: bytes) -> str:
    """Text a connection sent, with telnet bytes removed."""
    return plaintext(data).replace("\x00", "")
