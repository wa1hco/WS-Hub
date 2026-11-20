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

"""PSK Reporter openings from the station's grid.

Polls https://retrieve.pskreporter.info/query at most once every five
minutes, on the site server only. A report counts when the sender or the
receiver is in the station's 4-character grid — the same filter as the
PSK Reporter map with one end in that square. The resulting per-band,
per-direction map, plus recent local WSJT-X decodes, tells the Spotted
list which cluster spots are likely workable from here. The same reports
also list needed far stations as receive-only PSKR rows (``far_station``
and ``view.with_psk``); that adds no requests.

Nothing here transmits. Callers must never pass a grid read from a
WSJT-X ini file.
"""

from __future__ import annotations

import gzip
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable, Iterable

from wims.core.bands import band_label
from wims.engine.geo import bearing, grid_to_latlon

QUERY_URL = "https://retrieve.pskreporter.info/query"
MIN_POLL_S = 5 * 60
WINDOW_S = 30 * 60
# A path "toward" a bearing includes reports within this half-width.
SECTOR_HALF_DEG = 45.0
RPT_LIMIT = 2000
APP_CONTACT = "wa1hco@gmail.com"
USER_AGENT = (
    "WS-Hub/{version} (https://github.com/wa1hco/WS-Hub; {contact})"
)


def _version() -> str:
    try:
        from wims import __version__
        return str(__version__ or "dev")
    except Exception:
        return "dev"


def clean_grid(value) -> str:
    """A 4- or 6-character Maidenhead grid, or blank."""
    g = str(value or "").strip().upper()
    # PSK Reporter sometimes returns 8 or 10 character extended locators.
    if len(g) >= 6 and g[4].isalpha() and g[5].isalpha():
        g = g[:6]
    elif len(g) >= 4:
        g = g[:4]
    if len(g) not in (4, 6):
        return ""
    if not (g[0].isalpha() and g[1].isalpha() and g[2].isdigit() and g[3].isdigit()):
        return ""
    if len(g) == 6 and not (g[4].isalpha() and g[5].isalpha()):
        return ""
    if grid_to_latlon(g) is None:
        return ""
    return g


def grid4(value) -> str:
    """The 4-character field square, or blank."""
    g = clean_grid(value)
    return g[:4] if g else ""


def query_url(grid: str, *, window_s: int = WINDOW_S,
              contact: str = APP_CONTACT, limit: int = RPT_LIMIT) -> str:
    """PSK Reporter URL for reports with one end in ``grid``.

    Uses ``callsign`` + ``modify=grid`` so either the sender or the
    receiver can be in the square, ``rronly=1`` so only reception
    reports are returned, and ``appcontact`` as their terms request.
    """
    square = grid4(grid)
    params = {
        "callsign": square,
        "modify": "grid",
        "rronly": "1",
        "noactive": "1",
        "flowStartSeconds": str(-max(1, int(window_s))),
        "rptlimit": str(max(1, int(limit))),
        "appcontact": contact,
    }
    return QUERY_URL + "?" + urllib.parse.urlencode(params)


def _attr(elem: ET.Element, *names: str) -> str:
    for name in names:
        value = elem.attrib.get(name)
        if value:
            return str(value).strip()
    lowered = {k.lower(): v for k, v in elem.attrib.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value:
            return str(value).strip()
    return ""


def _flag(elem: ET.Element, *names: str) -> bool:
    text = _attr(elem, *names).lower()
    return text in {"1", "true", "yes"}


def _int(text: str, default: int | None = None) -> int | None:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Report:
    sender_call: str
    sender_grid: str
    receiver_call: str
    receiver_grid: str
    freq_hz: int
    mode: str
    ts: float
    snr: int | None = None
    sender_in_home: bool = False
    receiver_in_home: bool = False


@dataclass(frozen=True)
class PathSample:
    band: str
    az: float
    ts: float
    source: str  # "psk" or "local"


@dataclass
class Opening:
    likely: bool | None
    reason: str
    psk_count: int = 0
    local_count: int = 0


def parse_reports(xml_text: str | bytes) -> list[Report]:
    """Parse a PSK Reporter receptionReports document. Bad input is empty."""
    if isinstance(xml_text, bytes):
        xml_text = xml_text.decode("utf-8", "replace")
    text = (xml_text or "").strip()
    if not text:
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    tag = root.tag.split("}", 1)[-1]
    if tag.lower() not in {"receptionreports", "receptionreport"}:
        # Some error pages are XML but not this document.
        if tag.lower() not in {"error", "html"}:
            reports = list(root.iter())
            if not reports:
                return []
        else:
            return []
    out: list[Report] = []
    nodes = [root] if tag.lower() == "receptionreport" else list(root)
    for elem in nodes:
        name = elem.tag.split("}", 1)[-1]
        if name.lower() != "receptionreport":
            continue
        freq = _int(_attr(elem, "frequency"))
        ts = _int(_attr(elem, "flowStartSeconds"))
        if not freq or ts is None:
            continue
        sender_grid = clean_grid(_attr(elem, "senderLocator"))
        receiver_grid = clean_grid(_attr(elem, "receiverLocator"))
        out.append(Report(
            sender_call=_attr(elem, "senderCallsign").upper(),
            sender_grid=sender_grid,
            receiver_call=_attr(elem, "receiverCallsign").upper(),
            receiver_grid=receiver_grid,
            freq_hz=freq,
            mode=_attr(elem, "mode"),
            ts=float(ts),
            snr=_int(_attr(elem, "sNR", "snr")),
            sender_in_home=_flag(elem, "isSender"),
            receiver_in_home=_flag(elem, "isReceiver"),
        ))
    return out


def _in_home(grid: str, home4: str) -> bool:
    return bool(home4 and grid and grid[:4] == home4)


def far_grid(report: Report, home4: str) -> str:
    """The end of the path that is not in the home square, or blank."""
    send_home = report.sender_in_home or _in_home(report.sender_grid, home4)
    recv_home = report.receiver_in_home or _in_home(report.receiver_grid, home4)
    if send_home and recv_home:
        return ""
    if send_home:
        return report.receiver_grid
    if recv_home:
        return report.sender_grid
    # Flags missing: infer from locators only.
    if _in_home(report.sender_grid, home4):
        return report.receiver_grid
    if _in_home(report.receiver_grid, home4):
        return report.sender_grid
    return ""


def far_station(report: Report, home4: str) -> tuple[str, str, bool] | None:
    """The far end of a report: ``(call, grid, heard_here)``, or None.

    ``heard_here`` is True when the far station transmitted and a receiver
    in the home square heard it. False means the far station is the
    receiver that heard a station in the home square. A report with both
    ends (or neither end) in the home square is None. The grid is only the
    one PSK Reporter gave for that end; it may be blank, and it is never
    estimated.
    """
    if not home4:
        return None
    send_home = report.sender_in_home or _in_home(report.sender_grid, home4)
    recv_home = report.receiver_in_home or _in_home(report.receiver_grid, home4)
    if send_home == recv_home:
        return None
    if recv_home:
        call, grid, heard = report.sender_call, report.sender_grid, True
    else:
        call, grid, heard = report.receiver_call, report.receiver_grid, False
    call = (call or "").strip().upper()
    if not call:
        return None
    if grid and _in_home(grid, home4):
        return None
    return call, grid, heard


def path_from_report(report: Report, home_grid: str, *, now: float,
                     window_s: float = WINDOW_S) -> PathSample | None:
    home = clean_grid(home_grid)
    home4 = grid4(home)
    if not home4:
        return None
    if now - report.ts > window_s or report.ts - now > 60:
        return None
    far = far_grid(report, home4)
    if not far:
        return None
    az = bearing(home, far)
    if az is None:
        return None
    band = band_label(report.freq_hz, apply_if=False)
    if not band or band == "?":
        return None
    return PathSample(band=band, az=az, ts=report.ts, source="psk")


def angle_diff(a: float, b: float) -> float:
    d = abs(float(a) - float(b)) % 360.0
    return d if d <= 180.0 else 360.0 - d


def matches(sample: PathSample, band: str, az: float,
            half: float = SECTOR_HALF_DEG) -> bool:
    if sample.band != band:
        return False
    return angle_diff(sample.az, az) <= half


def recency_weight(ts: float, now: float, window_s: float = WINDOW_S) -> float:
    age = max(0.0, now - ts)
    if window_s <= 0 or age >= window_s:
        return 0.0
    return 1.0 - (age / window_s)


def explain_opening(*, grid4_s: str, band: str, az: float | None,
                    psk_count: int, local_count: int,
                    window_s: float, known: bool) -> str:
    square = grid4_s or "the station grid"
    mins = max(1, int(round(window_s / 60.0)))
    if az is None:
        return "No bearing, so this spot is not ranked."
    toward = f"toward {int(round(az))}° on {band}"
    if not known:
        return "PSK Reporter has no current map, so this spot is not ranked yet."
    bits = []
    if psk_count:
        word = "report" if psk_count == 1 else "reports"
        bits.append(f"{psk_count} {square} {word}")
    if local_count:
        word = "decode" if local_count == 1 else "decodes"
        bits.append(f"{local_count} local {word}")
    if bits:
        return f"Open: {' and '.join(bits)} {toward} in last {mins} min"
    return f"No {square} reports {toward} in {mins} min"


def evaluate(*, band: str, az: float | None, now: float,
             samples: Iterable[PathSample], home_grid: str,
             window_s: float = WINDOW_S, half: float = SECTOR_HALF_DEG,
             known: bool = True) -> Opening:
    """Whether this band and bearing look open from the home grid."""
    square = grid4(home_grid)
    if az is None or not band:
        return Opening(
            likely=None,
            reason=explain_opening(
                grid4_s=square, band=band or "?", az=az,
                psk_count=0, local_count=0, window_s=window_s, known=known,
            ),
        )
    psk_n = 0
    local_n = 0
    for sample in samples:
        if now - sample.ts > window_s or sample.ts - now > 60:
            continue
        if recency_weight(sample.ts, now, window_s) <= 0:
            continue
        if not matches(sample, band, az, half):
            continue
        if sample.source == "local":
            local_n += 1
        else:
            psk_n += 1
    likely: bool | None
    if not known:
        likely = None
    else:
        likely = (psk_n + local_n) > 0
    return Opening(
        likely=likely,
        reason=explain_opening(
            grid4_s=square, band=band, az=az,
            psk_count=psk_n, local_count=local_n,
            window_s=window_s, known=known,
        ),
        psk_count=psk_n,
        local_count=local_n,
    )


def hearings_from_decodes(decodes: Iterable[dict], *, home_grid: str,
                          now: float, window_s: float = WINDOW_S,
                          grid_for=None) -> list[PathSample]:
    """Local WSJT-X decodes with a bearing from the home grid.

    ``grid_for(call)`` may return a heard-grid lookup or a grid string.
    Decodes without a grid are skipped. This never invents a square.
    """
    home = clean_grid(home_grid)
    if not home:
        return []
    try:
        from wims.udp.messages import extract_grid
    except Exception:  # pragma: no cover
        extract_grid = lambda _m: None  # noqa: E731
    out: list[PathSample] = []
    for item in decodes or []:
        try:
            ts = float(item.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if now - ts > window_s or ts - now > 60:
            continue
        band = str(item.get("band") or "").strip()
        if not band or band == "?":
            freq = item.get("freq_hz") or item.get("frequency")
            if freq:
                band = band_label(freq, apply_if=False)
        if not band or band == "?":
            continue
        grid = clean_grid(extract_grid(item.get("message") or ""))
        if not grid:
            call = str(item.get("dx_call") or "").strip()
            if call and grid_for is not None:
                found = grid_for(call)
                if isinstance(found, dict):
                    grid = clean_grid(found.get("grid") or "")
                elif hasattr(found, "grid"):
                    grid = clean_grid(getattr(found, "grid", "") or "")
                else:
                    grid = clean_grid(found)
        if not grid:
            continue
        # A decode in our own square is not evidence of a DX path.
        if grid4(grid) == grid4(home):
            continue
        az = bearing(home, grid)
        if az is None:
            continue
        out.append(PathSample(band=band, az=az, ts=ts, source="local"))
    return out


def psk_status_line(*, enabled: bool, grid: str, report_count: int | None,
                    updated_at: float | None, now: float,
                    error: str = "") -> str:
    """A short, plain status for Setup and the Spotted list."""
    if not enabled:
        return "PSK Reporter: off"
    square = grid4(grid)
    if not square:
        return "PSK Reporter: waiting for a station grid. Set it in Setup, or start WSJT-X."
    if error and updated_at is None:
        return f"PSK Reporter: {error}"
    if updated_at is None:
        return f"PSK Reporter: looking up reports from {square}…"
    age = max(0, int(round(now - updated_at)))
    if age < 45:
        when = "just now"
    elif age < 90:
        when = "1 min ago"
    else:
        when = f"{age // 60} min ago"
    n = int(report_count or 0)
    word = "report" if n == 1 else "reports"
    line = f"PSK Reporter: {n} {word} from {square}, updated {when}"
    if error:
        line += f". {error}"
    return line


def _decode_body(raw: bytes, headers) -> bytes:
    enc = ""
    if headers is not None:
        try:
            enc = (headers.get("Content-Encoding") or "").lower()
        except Exception:
            enc = ""
    if enc == "gzip" or (raw[:2] == b"\x1f\x8b"):
        try:
            return gzip.decompress(raw)
        except OSError:
            return raw
    return raw


def default_fetch(url: str, *, timeout: float = 20.0) -> tuple[int, bytes, str]:
    """GET ``url``. Returns (status, body, error). Never raises."""
    headers = {
        "User-Agent": USER_AGENT.format(version=_version(), contact=APP_CONTACT),
        "Accept": "application/xml, text/xml, */*",
        "Accept-Encoding": "gzip",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", 200) or 200)
            body = _decode_body(resp.read(), resp.headers)
            if status >= 400:
                return status, body, f"can't reach the service ({status}), will try again"
            return status, body, ""
    except urllib.error.HTTPError as exc:
        status = int(exc.code or 0)
        if status == 429:
            return status, b"", "rate limited, will try again later"
        return status, b"", f"can't reach the service ({status}), will try again"
    except urllib.error.URLError:
        return 0, b"", "can't reach the service, will try again"
    except TimeoutError:
        return 0, b"", "can't reach the service, will try again"
    except Exception:
        return 0, b"", "can't read the service, will try again"


class PskOpenings:
    """Site-server poller. Seats must not construct one of their own."""

    def __init__(self, *, fetch: Callable[..., tuple[int, bytes, str]] | None = None,
                 min_poll_s: float = MIN_POLL_S, window_s: float = WINDOW_S,
                 contact: str = APP_CONTACT, clock=time.time,
                 sleep=time.sleep):
        self._fetch = fetch or default_fetch
        self.min_poll_s = float(min_poll_s)
        self.window_s = float(window_s)
        self.contact = contact
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._enabled = False
        self._grid = ""
        self._reports: list[Report] = []
        self._samples: list[PathSample] = []
        self._updated_at: float | None = None
        self._last_attempt: float | None = None
        self._error = ""
        self._fetch_count = 0

    def configure(self, *, grid: str, enabled: bool) -> None:
        with self._lock:
            self._grid = grid4(grid)
            self._enabled = bool(enabled) and bool(self._grid)
        self._wake.set()

    def start(self) -> None:
        self._stop.clear()
        if self._thread is not None and self._thread.is_alive():
            self._wake.set()
            return
        self._thread = threading.Thread(
            target=self._run, name="wims-psk-openings", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    @property
    def fetch_count(self) -> int:
        with self._lock:
            return self._fetch_count

    @property
    def last_attempt(self) -> float | None:
        with self._lock:
            return self._last_attempt

    def snapshot(self, now: float | None = None) -> dict:
        now = self._clock() if now is None else now
        with self._lock:
            reports = list(self._reports)
            samples = list(self._samples)
            updated = self._updated_at
            error = self._error
            grid = self._grid
            enabled = self._enabled
        known = updated is not None
        # A map older than two windows is no longer evidence of a closing.
        if known and now - (updated or 0) > self.window_s * 2:
            known = False
        return {
            "enabled": enabled,
            "grid": grid,
            "reports": reports,
            "samples": samples,
            "report_count": len(reports),
            "updated_at": updated,
            "error": error,
            "known": known,
            "status_line": psk_status_line(
                enabled=enabled, grid=grid, report_count=len(reports) if known else None,
                updated_at=updated, now=now, error=error,
            ),
        }

    def opening(self, *, band: str, az: float | None, now: float,
                local: Iterable[PathSample] | None = None) -> Opening:
        snap = self.snapshot(now)
        samples = list(snap["samples"])
        if local:
            samples.extend(local)
        known = bool(snap["known"]) if snap["enabled"] else False
        return evaluate(
            band=band, az=az, now=now, samples=samples,
            home_grid=snap["grid"], window_s=self.window_s,
            known=known,
        )

    def maybe_fetch(self, now: float | None = None) -> bool:
        """Fetch if due. Returns True when a request was made.

        The 5-minute floor is enforced even after an error, so a failure
        cannot hammer the service.
        """
        now = self._clock() if now is None else now
        with self._lock:
            if not self._enabled or not self._grid:
                return False
            last = self._last_attempt
            if last is not None and (now - last) < self.min_poll_s:
                return False
            grid = self._grid
            self._last_attempt = now
            self._fetch_count += 1
        url = query_url(grid, window_s=int(self.window_s), contact=self.contact)
        status, body, error = self._fetch(url)
        reports: list[Report] = []
        if status == 200 and body:
            reports = parse_reports(body)
            if not reports and not error:
                # A valid empty document is fine; a junk body is an error.
                text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
                if "<receptionReport" not in text and "receptionReports" not in text:
                    error = "can't read the service, will try again"
        samples = []
        if not error:
            for report in reports:
                sample = path_from_report(
                    report, grid, now=now, window_s=self.window_s,
                )
                if sample is not None:
                    samples.append(sample)
        with self._lock:
            if not error:
                self._reports = reports
                self._samples = samples
                self._updated_at = now
                self._error = ""
            else:
                self._error = error
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            self.maybe_fetch()
            # Wake early on configure, but still honor the 5-minute floor
            # inside maybe_fetch.
            self._wake.wait(timeout=min(30.0, max(1.0, self.min_poll_s / 2)))
            self._wake.clear()
