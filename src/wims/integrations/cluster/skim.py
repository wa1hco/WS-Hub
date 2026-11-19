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

"""WS Skim decodes as receive-only spots.

WS Skim sends its decodes in the WSJT-X UDP format (Heartbeat, Status,
Decode, Clear, Close), one emulated instance per section of the band. Its
UDP ids look like ``SKIM-FLEX-40m-FT8`` or ``SKIM-FLEX-40m-FT8-7075``.
Each Status carries that section's dial, and each Decode's
``delta_frequency`` is the audio offset from that dial, so the true RF is
dial + delta_frequency. WS Skim sends the Status right before each Decode.

A skimmer is never a WSJT-X instance in WS Hub. Its datagrams do not reach
the fleet, the call roster, Reply, Configure, the activity map, the QSO log
grid fill, or GridTracker. They only become ``SKIM`` rows on the Spotted
list, and count as local evidence that a band is open for the PSK Reporter
ranking.

Which ids are skimmers is a Setup choice (``skim_ids``), defaulting to WS
Skim's ``SKIM-*`` and ``WS-Skim*``. A Status whose Config name, or a
Heartbeat whose version, says ``WS-Skim`` or ``WS Skim`` also marks that id
as a skimmer.
"""

from __future__ import annotations

import fnmatch
import re
from collections import deque
from dataclasses import dataclass

from wims.core.bands import band_label
from wims.integrations.cluster.parse import valid_login_call

DEFAULT_SKIM_IDS = "SKIM-*, WS-Skim*"
# A receiver with no datagram for this long is no longer listed as live.
RECEIVER_LIVE_S = 120.0
# A grid read from a Skim CQ is kept this long to fill later lines.
GRID_MEMORY_S = 3600.0
# Recent decodes kept for the "band open" evidence (PSK ranking window).
RECENT_S = 30 * 60.0

# WSJT-X Decode mode characters (mainwindow.cpp).
_MODE_CHARS = {
    "~": "FT8", "+": "FT4", "&": "MSK144", "^": "MSK144", ":": "Q65",
    "#": "JT65", "@": "JT9", "$": "JT4", "`": "FST4",
}
_MARKER = re.compile(r"ws[\s_-]*skim", re.IGNORECASE)
_PATTERN_OK = re.compile(r"^[A-Za-z0-9 _./*?-]{1,40}$")


def clean_skim_ids(value) -> str:
    """Comma-separated id patterns. Blank or junk restores the default."""
    if isinstance(value, (list, tuple)):
        parts = [str(item) for item in value]
    else:
        parts = re.split(r"[,;\n]+", str(value or ""))
    out: list[str] = []
    for part in parts:
        text = part.strip()
        if not text or not _PATTERN_OK.match(text):
            continue
        if text.lower() not in {item.lower() for item in out}:
            out.append(text)
        if len(out) >= 12:
            break
    return ", ".join(out) if out else DEFAULT_SKIM_IDS


def id_patterns(value) -> list[str]:
    return [item.strip() for item in clean_skim_ids(value).split(",") if item.strip()]


def id_matches(mid: str | None, patterns) -> bool:
    """True when this UDP id is a skimmer by the Setup list (no case)."""
    text = (mid or "").strip().upper()
    if not text:
        return False
    for pattern in patterns or []:
        pat = str(pattern or "").strip().upper()
        if not pat:
            continue
        if any(ch in pat for ch in "*?"):
            if fnmatch.fnmatchcase(text, pat):
                return True
        elif text == pat:
            return True
    return False


def has_marker(msg) -> bool:
    """WS Skim's own mark: Config name or Heartbeat version names WS Skim."""
    for name in ("config_name", "version"):
        value = getattr(msg, name, None)
        if value and _MARKER.search(str(value)):
            return True
    return False


_CALL = re.compile(r"^(?:[A-Z0-9]{1,4}/)?[A-Z0-9]{3,10}(?:/[A-Z0-9]{1,4})?$")


def _call_ok(call: str) -> bool:
    """A real station call. Hashed ``<...>`` and free text are not."""
    text = (call or "").strip().upper()
    if not text or "<" in text or text in {"RR73", "RRR", "73", "CQ", "DE", "QRZ"}:
        return False
    if not _CALL.match(text):
        return False
    return valid_login_call(_base(text)) or (
        any(ch.isalpha() for ch in text) and any(ch.isdigit() for ch in text)
    )


def _base(call: str) -> str:
    """The longest slash part: ``W1AW/P`` -> ``W1AW``, ``VP2/W1AW`` -> ``W1AW``."""
    parts = [part for part in (call or "").upper().split("/") if part]
    if not parts:
        return ""
    return max(parts, key=len)


def receiver_label(mid: str | None) -> str:
    """``SKIM-FLEX-40m-FT8-7075`` -> ``FLEX``. Other ids stay as they are."""
    text = (mid or "").strip()
    parts = text.split("-")
    if len(parts) >= 2 and parts[0].upper() == "SKIM" and parts[1]:
        return parts[1].upper()
    return text or "Skim"


def mode_from(decode_mode: str | None, status_mode: str | None) -> str:
    raw = (decode_mode or "").strip()
    if raw[:1] in _MODE_CHARS:
        return _MODE_CHARS[raw[:1]]
    if raw.upper() in set(_MODE_CHARS.values()):
        return raw.upper()
    status = (status_mode or "").strip().upper()
    return status or "FT8"


@dataclass
class SkimHit:
    """One Skim decode that names a station. True RF in Hz."""

    call: str
    band: str
    mode: str
    freq_hz: int
    grid: str | None
    snr: int
    receiver: str
    instance: str
    message: str
    ts: float


@dataclass
class _Receiver:
    instance: str
    label: str
    dial_hz: int = 0
    mode: str = ""
    last_seen: float = 0.0
    src_ip: str | None = None
    de_call: str = ""


class SkimFeed:
    """Receive-only Skim ingest. Not thread-safe; the caller holds a lock."""

    def __init__(self) -> None:
        self._rx: dict[str, _Receiver] = {}
        self._grids: dict[str, tuple[str, float]] = {}
        self._recent: deque = deque(maxlen=4000)
        self._marked: set[str] = set()
        self.datagrams = 0
        self.decodes = 0
        self.spots = 0
        self.no_dial = 0

    # -- identification ------------------------------------------------------ #

    def is_skim(self, msg, patterns) -> bool:
        mid = getattr(msg, "id", None) or ""
        if not mid:
            return False
        if mid in self._marked:
            return True
        if id_matches(mid, patterns):
            return True
        if has_marker(msg):
            self._marked.add(mid)
            return True
        return False

    def marked_ids(self) -> set[str]:
        return set(self._marked)

    # -- ingest -------------------------------------------------------------- #

    def observe(self, msg, now: float, src_ip: str | None = None,
                own_calls=None) -> SkimHit | None:
        """Take one parsed datagram from a skimmer. Returns a spot or None."""
        mid = getattr(msg, "id", None) or "?"
        self.datagrams += 1
        kind = type(msg).__name__
        if kind == "Close":
            self._rx.pop(mid, None)
            return None
        rx = self._rx.get(mid)
        if rx is None:
            rx = _Receiver(instance=mid, label=receiver_label(mid))
            self._rx[mid] = rx
        rx.last_seen = now
        if src_ip:
            rx.src_ip = src_ip
        if kind == "Status":
            dial = int(getattr(msg, "dial_frequency", 0) or 0)
            if dial > 0:
                rx.dial_hz = dial
            rx.mode = (getattr(msg, "mode", None) or rx.mode or "").strip()
            rx.de_call = (getattr(msg, "de_call", None) or rx.de_call or "").strip().upper()
            return None
        if kind != "Decode":
            return None
        self.decodes += 1
        if getattr(msg, "low_confidence", False) or getattr(msg, "off_air", False):
            return None
        if rx.dial_hz <= 0:
            # No Status yet, so the RF is unknown. Wait for the next one.
            self.no_dial += 1
            return None
        rf = rx.dial_hz + int(getattr(msg, "delta_frequency", 0) or 0)
        band = band_label(rf, apply_if=False)
        if not band or band == "?":
            return None
        mode = mode_from(getattr(msg, "mode", None), rx.mode)
        text = (getattr(msg, "message", None) or "").strip()
        call = (getattr(msg, "dx_call", None) or "").strip().upper()
        grid = (getattr(msg, "grid", None) or "").strip().upper() or None
        self._recent.append({
            "ts": now, "band": band, "freq_hz": rf, "message": text,
            "dx_call": call, "mode": mode, "source": "skim",
        })
        if not _call_ok(call):
            return None
        own = {c.upper() for c in (own_calls or set()) if c}
        if rx.de_call:
            own.add(_base(rx.de_call))
        if call in own or _base(call) in own:
            return None
        if grid and grid in {"RR73"}:
            grid = None
        if grid:
            self._grids[call] = (grid, now)
        else:
            kept = self._grids.get(call)
            if kept and now - kept[1] <= GRID_MEMORY_S:
                grid = kept[0]
        self.spots += 1
        return SkimHit(
            call=call, band=band, mode=mode, freq_hz=int(rf), grid=grid,
            snr=int(getattr(msg, "snr", 0) or 0), receiver=rx.label,
            instance=mid, message=text, ts=now,
        )

    # -- views --------------------------------------------------------------- #

    def recent_decodes(self, now: float, window_s: float = RECENT_S) -> list[dict]:
        """Skim decodes for the band-open evidence (same shape as WSJT-X)."""
        cut = now - window_s
        return [item for item in self._recent if item["ts"] >= cut]

    def prune(self, now: float) -> None:
        dead = [mid for mid, rx in self._rx.items()
                if now - rx.last_seen > RECEIVER_LIVE_S * 5]
        for mid in dead:
            del self._rx[mid]
        old = [call for call, (_g, ts) in self._grids.items()
               if now - ts > GRID_MEMORY_S]
        for call in old:
            del self._grids[call]

    def snapshot(self, now: float) -> dict:
        """Plain-words status for Operate and Setup."""
        self.prune(now)
        live = [rx for rx in self._rx.values() if now - rx.last_seen <= RECEIVER_LIVE_S]
        groups: dict[str, dict] = {}
        for rx in live:
            g = groups.setdefault(rx.label, {
                "label": rx.label, "bands": set(), "modes": set(),
                "sections": 0, "last_seen": 0.0, "hosts": set(),
            })
            g["sections"] += 1
            g["last_seen"] = max(g["last_seen"], rx.last_seen)
            if rx.dial_hz:
                g["bands"].add(band_label(rx.dial_hz, apply_if=False))
            if rx.mode:
                g["modes"].add(rx.mode.upper())
            if rx.src_ip:
                g["hosts"].add(rx.src_ip)
        recent5 = [item for item in self._recent if now - item["ts"] <= 300]
        receivers = []
        for label in sorted(groups):
            g = groups[label]
            receivers.append({
                "label": label,
                "bands": sorted(b for b in g["bands"] if b and b != "?"),
                "modes": sorted(g["modes"]),
                "sections": g["sections"],
                "age_s": int(max(0.0, now - g["last_seen"])),
                "hosts": sorted(g["hosts"]),
            })
        return {
            "receivers": receivers,
            "decodes_5min": len(recent5),
            "line": skim_line(receivers, len(recent5)),
        }


def skim_line(receivers: list[dict], decodes_5min: int) -> str:
    if not receivers:
        return ("Skim: nothing heard from WS Skim. "
                "See Setup, Skim receivers, to send its decodes here.")
    bits = []
    for rx in receivers:
        where = ", ".join(rx.get("bands") or []) or "no band yet"
        modes = "/".join(rx.get("modes") or [])
        host = (rx.get("hosts") or [""])[0]
        frm = f" from {host}" if host else ""
        bits.append(f"{rx['label']} on {where}{(' ' + modes) if modes else ''}{frm}")
    word = "decode" if decodes_5min == 1 else "decodes"
    return (f"Skim: {'; '.join(bits)}. {decodes_5min} {word} in the last 5 min. "
            "Receive only: a Skim spot is never worked from here.")
