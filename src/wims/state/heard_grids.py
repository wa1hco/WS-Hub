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

"""Last grid heard for each station.

A tail-end reply such as ``W1XX CX4BAN R-10`` carries no grid. WSJT-X then
offers a log line with a blank grid. This store keeps the last grid seen in a
standard decode, with the band and the UTC time, so WS Hub can fill its own
record and the record it forwards.

Grids from free text, other non-standard text, and ``RR73`` are ignored.
A 6-character grid upgrades a matching 4-character one. Any other newly
heard square replaces the stored one, including a 4-character square
replacing a 6-character one. A later 4-character hearing of that same
square keeps the 6-character form.

Calls ending in ``/R``, and any call heard in two different squares inside
the expiry window, are rovers. A blank logged grid for a rover, and the
square sent to WSJT-X when that rover is worked, use only a hearing inside
``ROVER_WINDOW_S`` before the contact. Older rover squares are left blank.

A fixed station stays in the square it sent. A remembered grid for that
station is its grid: the roster shows the square with no marker, and the
hover says where it was heard (``Grid from <band> at <time> UTC``). A rover
square heard in the current QSO is the same. ``grid assumed`` is only a
rover square that was not heard in this QSO.

Same QSO: an exchange with our station. It starts at the earlier of our
first message to that call (Work, or a Status whose TX text names the DX
call and is not a CQ) and their first message addressed to us. It continues
while the next such message arrives within ``QSO_WINDOW_S`` (3 minutes). A
gap longer than that is a new QSO. A square was heard in this QSO when its
last hearing is inside that span, or in the ``QSO_PREAMBLE_S`` (90 seconds)
before the start — the CQ or the grid that opened the contact. The exchange
is session state and is not written to the file.

The ADIF comment ``grid remembered`` is written only for that assumed rover
square. A fixed station, and a rover square from this QSO, are filled with
no comment: the square is not in doubt, and the words would be noise. The
log copy still marks ``grid_source`` ``remembered`` whenever this store
filled a blank, so a later decode can correct it. That flag is not the
comment.

The file is JSON next to the other WS Hub preferences. ``WIMS_HEARD_GRIDS``
points at another file; that is for tests. Operator docs do not mention it.

Speed: a busy band hears a grid every second or so, and the file holds
tens of thousands of calls. A new hearing only marks the store changed. A
background timer writes the file at most every ``SAVE_DELAY_S``, and the
slow part (turning every time into text) runs outside the store lock.
``flush()`` writes at once; the site server calls it on the way out.
Expiry sweeps the whole store at most every ``SWEEP_EVERY_S``; a lookup
still expires the one call it reads, so answers do not change.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import threading
import time
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from wims.engine.callsign import is_compound_call, plain_call, same_home
from wims.udp.messages import extract_grid, standard_station_grid

# Drop a station after this long with no new hearing.
MAX_AGE_S = 30 * 24 * 3600
# A rover square older than this is not written onto a blank log line
# and is not sent to WSJT-X. One window for both fills.
ROVER_WINDOW_S = 10 * 60
# Silence that ends an exchange with our station. A new directed message
# after this starts a new QSO. Long enough for a slow FT8 contact with
# repeats; a later hearing is a different contact.
QSO_WINDOW_S = 3 * 60
# The CQ or the grid that opened the contact, heard just before the first
# message to us or our first message to them. Covers an FT8 or FT4 period
# and one Q65-60 period, plus a little slack. A rover square older than
# this, relative to the QSO start, is an earlier hearing.
QSO_PREAMBLE_S = 90
# Distinct squares kept for the roster hover ("now FN43, was FN42").
HISTORY_KEEP = 4
# A hearing a couple of minutes after the log timestamp still counts.
CLOCK_SLACK_S = 120
# A new hearing is written to the file at most this long after it arrives.
SAVE_DELAY_S = 20.0
# The full expiry sweep runs at most this often (store time, seconds).
SWEEP_EVERY_S = 60.0
SCHEMA = 1
GRID_REMEMBERED = "grid remembered"
# Roster words for a rover square that was not heard in this QSO.
GRID_ASSUMED = "grid assumed"

_GRID_OK = re.compile(r"^[A-R]{2}\d{2}([A-X]{2})?$")
_ADIF_FIELD = re.compile(r"<([A-Za-z0-9_]+):(\d+)(?::[^>]*)?>", re.IGNORECASE)
_ADIF_EOR = re.compile(r"<eor>", re.IGNORECASE)


@dataclass
class SquareHearing:
    grid: str
    first_utc: float
    last_utc: float
    first_band: str
    last_band: str


@dataclass
class GridLookup:
    """What to show or write for one call at one moment."""

    grid: str | None = None
    tip: str = ""
    remembered: bool = False
    stale: bool = False
    # Rover square from an earlier hearing, not this QSO. A fixed station,
    # and a rover square heard in this QSO, are never assumed.
    assumed: bool = False


@dataclass
class _CallMemory:
    history: list[SquareHearing] = field(default_factory=list)
    # Session only (not written to heard_grids.json). See the module docstring.
    exchange_start: float | None = None
    exchange_last: float | None = None

    def hear(self, grid: str, band: str, when: float) -> None:
        square = grid[:4]
        same = next((h for h in self.history if h.grid[:4] == square), None)
        if same is None:
            self.history.append(SquareHearing(grid, when, when, band, band))
        else:
            old = same.grid
            if len(grid) == 6 and len(old) == 4 and grid.startswith(old):
                same.grid = grid
            elif len(grid) == 4 and len(old) == 6 and old.startswith(grid):
                pass
            elif len(grid) >= len(old):
                same.grid = grid
            if when < same.first_utc:
                same.first_utc = when
                same.first_band = band
            if when >= same.last_utc:
                same.last_utc = when
                same.last_band = band
        self.history.sort(key=lambda h: h.last_utc)
        if len(self.history) > HISTORY_KEEP:
            self.history = self.history[-HISTORY_KEEP:]


def default_path() -> Path:
    env = (os.environ.get("WIMS_HEARD_GRIDS") or "").strip()
    if env:
        return Path(env)
    from wims.rules.choice import config_dir
    return config_dir() / "heard_grids.json"


def _hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M")


def _dump_time(ts: float) -> str:
    # Same text as datetime(...).strftime("%Y-%m-%dT%H:%M:%SZ"), about
    # twice as fast; the file holds one per hearing.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _load_time(text: str) -> float | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def grid_for_work(message: str | None, found: GridLookup) -> str | None:
    """Square to send to the worked WSJT-X after Reply, or None.

    A grid already in the decode is left for WSJT-X. A station with no
    remembered square, and a rover whose square is older than
    ``ROVER_WINDOW_S``, stay blank. ``found`` is a ``HeardGrids.lookup``
    result, so the rover window is the same rule the log fill uses.
    """
    if extract_grid(message):
        return None
    if not found.remembered:
        return None
    grid = (found.grid or "").strip().upper()
    if not grid or grid == "RR73" or not _GRID_OK.match(grid):
        return None
    return grid


def qso_time_unix(msg) -> float:
    """UTC unix time of a logged-QSO datagram, or now when it has none."""
    from wims.udp import messages as M
    if isinstance(msg, M.QSOLogged):
        found = _qt_unix(msg.datetime_off or msg.datetime_on)
        if found is not None:
            return found
    if isinstance(msg, M.LoggedADIF):
        found = adif_time_unix(msg.adif or "")
        if found is not None:
            return found
    return datetime.now(timezone.utc).timestamp()


def stamp_unix(timestamp: str | None = None, qso_date: str | None = None,
               time_on: str | None = None) -> float:
    """UTC unix time from a log-copy row. Missing values use the current time."""
    text = (timestamp or "").strip()
    if text:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(text[:19], fmt).replace(
                    tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
    found = adif_time_unix(
        f"<qso_date:8>{(qso_date or '')[:8]} <time_on:6>{(time_on or '')[:6]}"
    )
    if found is not None and (qso_date or "").strip() and (time_on or "").strip():
        return found
    return datetime.now(timezone.utc).timestamp()


def _qt_unix(dt: dict | None) -> float | None:
    if not dt:
        return None
    from wims.adif import qt_datetime_to_adif
    qdate, qtime = qt_datetime_to_adif(dt)
    return adif_time_unix(f"<qso_date:8>{qdate} <time_on:6>{qtime}")


def adif_time_unix(adif: str) -> float | None:
    qdate = _adif_value(adif, "QSO_DATE")
    qtime = _adif_value(adif, "TIME_ON") or _adif_value(adif, "TIME_OFF")
    if not qdate or not qtime:
        return None
    digits_d = "".join(ch for ch in qdate if ch.isdigit())
    digits_t = "".join(ch for ch in qtime if ch.isdigit())
    if len(digits_d) < 8 or len(digits_t) < 4:
        return None
    digits_t = (digits_t + "00")[:6]
    try:
        return datetime.strptime(digits_d[:8] + digits_t, "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _adif_value(adif: str, tag: str) -> str:
    want = tag.upper()
    for match in _ADIF_FIELD.finditer(adif or ""):
        if match.group(1).upper() != want:
            continue
        try:
            n = int(match.group(2))
        except ValueError:
            continue
        raw = (adif or "")[match.end():match.end() + n]
        return raw.strip()
    return ""


def _adif_set(adif: str, tag: str, value: str) -> str:
    text = adif or ""
    want = tag.upper()
    for match in _ADIF_FIELD.finditer(text):
        if match.group(1).upper() != want:
            continue
        try:
            n = int(match.group(2))
        except ValueError:
            continue
        piece = f"<{match.group(1)}:{len(value)}>{value}"
        return text[:match.start()] + piece + text[match.end() + n:]
    piece = f"<{tag}:{len(value)}>{value}"
    eor = _ADIF_EOR.search(text)
    if eor:
        return text[:eor.start()].rstrip() + " " + piece + " " + text[eor.start():]
    return (text.rstrip() + " " + piece).strip()


class HeardGrids:
    """Per-callsign grid memory. ``path`` None keeps it in memory only."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._calls: dict[str, _CallMemory] = {}
        # Store time of the last full expiry sweep (None: never).
        self._swept_at: float | None = None
        # Changed since the last write; a timer is pending to write it.
        self._dirty = False
        self._timer: threading.Timer | None = None
        # One writer at a time, so two flushes cannot interleave.
        self._write_lock = threading.Lock()
        if self.path is not None:
            self.load()
            _LIVE_STORES.add(self)

    def note(self, call: str, grid: str, band: str, when: float) -> bool:
        call_u = (call or "").strip().upper()
        grid_u = (grid or "").strip().upper()
        if not call_u or not _GRID_OK.match(grid_u) or grid_u == "RR73":
            return False
        with self._lock:
            self._maybe_sweep(when)
            entry = self._entry_locked(call_u, when)
            if entry is None:
                entry = _CallMemory()
                self._calls[call_u] = entry
            entry.hear(grid_u, (band or "?").strip() or "?", float(when))
            self._mark_dirty_locked()
        return True

    def note_message(self, message: str | None, band: str, when: float) -> bool:
        pair = standard_station_grid(message)
        if pair is None:
            return False
        call, grid = pair
        return self.note(call, grid, band, when)

    def note_decode(self, msg, band: str, when: float) -> bool:
        if getattr(msg, "off_air", False):
            return False
        # A '?' / UDP low-confidence decode is kept as a decode, but is not
        # a confirmed grid source.
        if getattr(msg, "low_confidence", False):
            return False
        pair = standard_station_grid(getattr(msg, "message", None))
        if pair is None:
            return False
        parsed, grid = pair
        named = plain_call(getattr(msg, "dx_call", None)) or ""
        call = parsed
        # A standard message can name the home call while the roster has
        # already resolved the transmitter to the full compound. Keep the
        # square on that full call. Do not attach another station's grid.
        if (named and named != parsed and same_home(named, parsed)
                and is_compound_call(named)):
            call = named
        return self.note(call, grid, band, when)

    def note_exchange(self, call: str, when: float) -> None:
        """Mark a QSO with our station as in progress at ``when``.

        The QSO starts at the earliest directed message and stays open while
        the next one arrives within ``QSO_WINDOW_S``. A gap longer than that
        starts a new QSO. A message older than the live QSO is left alone.
        This is not written to the file.
        """
        call_u = (call or "").strip().upper()
        if not call_u:
            return
        when = float(when)
        with self._lock:
            entry = self._calls.get(call_u)
            if entry is None:
                entry = _CallMemory()
                self._calls[call_u] = entry
            start = entry.exchange_start
            last = entry.exchange_last
            if start is None or last is None or when > last + QSO_WINDOW_S:
                entry.exchange_start = when
                entry.exchange_last = when
                return
            if when + QSO_WINDOW_S < start:
                return
            if when < start:
                entry.exchange_start = when
            if when > last:
                entry.exchange_last = when

    def lookup(self, call: str, when: float) -> GridLookup:
        call_u = (call or "").strip().upper()
        if not call_u:
            return GridLookup()
        with self._lock:
            entry = self._entry_locked(call_u, when)
            if entry is None or not entry.history:
                return GridLookup()
            rover = self._is_rover_locked(call_u, entry)
            usable = [h for h in entry.history if h.last_utc <= when + CLOCK_SLACK_S]
            if not usable:
                return GridLookup()
            latest = max(usable, key=lambda h: h.last_utc)
            age = when - latest.last_utc
            if age > MAX_AGE_S:
                return GridLookup()
            if rover and age > ROVER_WINDOW_S:
                return GridLookup(tip=self._stale_tip(latest), stale=True)
            assumed = bool(rover and not self._in_this_qso(entry, latest, when))
            return GridLookup(
                grid=latest.grid,
                tip=self._tip(entry, latest, assumed),
                remembered=True,
                assumed=assumed,
            )

    def is_rover(self, call: str, when: float) -> bool:
        call_u = (call or "").strip().upper()
        if not call_u:
            return False
        if call_u.endswith("/R"):
            return True
        with self._lock:
            entry = self._entry_locked(call_u, when)
            if entry is None:
                return False
            return self._is_rover_locked(call_u, entry)

    def current(self, call: str, when: float) -> str | None:
        """Newest square at ``when``, ignoring the rover log window."""
        call_u = (call or "").strip().upper()
        with self._lock:
            entry = self._entry_locked(call_u, when)
            if entry is None or not entry.history:
                return None
            usable = [h for h in entry.history if h.last_utc <= when + CLOCK_SLACK_S]
            if not usable:
                return None
            return max(usable, key=lambda h: h.last_utc).grid

    def fill_blank_adif(self, adif: str, call: str, when: float) -> tuple[str, str]:
        """Fill a blank ``GRIDSQUARE``.

        Returns ``(adif, source)`` where source is ``logged`` when the
        datagram already carried a grid, ``remembered`` when this store
        filled one, or ``""`` when the grid stays blank.

        The comment ``grid remembered`` is written only when the square is
        a rover grid from an earlier hearing (``GridLookup.assumed``). A
        fixed station stays in the square it sent, and a rover square heard
        in this QSO was copied from that contact, so the comment would be
        noise. ``source`` is still ``remembered`` in those cases: the log
        copy uses it to tell a filled square from one the datagram carried,
        and a later decode can correct it. Does not write a WSJT-X log file.
        """
        if (_adif_value(adif, "GRIDSQUARE") or "").strip():
            return adif, "logged"
        found = self.lookup(call, when)
        if not found.grid:
            return adif, ""
        text = _adif_set(adif, "GRIDSQUARE", found.grid)
        if found.assumed:
            comment = _adif_value(text, "COMMENT")
            if GRID_REMEMBERED not in (comment or "").lower():
                merged = (
                    f"{comment}; {GRID_REMEMBERED}" if (comment or "").strip()
                    else GRID_REMEMBERED
                )
                text = _adif_set(text, "COMMENT", merged)
        return text, "remembered"

    def load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return
        if not isinstance(data, dict):
            return
        calls = data.get("calls")
        if not isinstance(calls, dict):
            return
        loaded: dict[str, _CallMemory] = {}
        for call, body in calls.items():
            if not isinstance(body, dict):
                continue
            memory = _CallMemory()
            rows = body.get("history")
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                grid = str(row.get("grid") or "").strip().upper()
                if not _GRID_OK.match(grid):
                    continue
                first = _load_time(str(row.get("first_utc") or ""))
                last = _load_time(str(row.get("last_utc") or ""))
                if first is None or last is None:
                    continue
                memory.history.append(SquareHearing(
                    grid=grid,
                    first_utc=first,
                    last_utc=last,
                    first_band=str(row.get("first_band") or "?"),
                    last_band=str(row.get("last_band") or "?"),
                ))
            if memory.history:
                memory.history.sort(key=lambda h: h.last_utc)
                loaded[(str(call) or "").strip().upper()] = memory
        with self._lock:
            self._calls = loaded

    @staticmethod
    def _expire_entry(entry: _CallMemory, when: float) -> bool:
        """Drop old hearings from one call. True when the call is dead."""
        if entry.history and any(
            when - h.last_utc > MAX_AGE_S for h in entry.history
        ):
            entry.history = [
                h for h in entry.history if when - h.last_utc <= MAX_AGE_S
            ]
        if entry.history:
            return False
        last = entry.exchange_last
        return not (last is not None and abs(when - last) <= QSO_WINDOW_S)

    def _entry_locked(self, call_u: str, when: float) -> _CallMemory | None:
        """The call's memory, expired as of ``when`` (caller holds the lock).

        Same answer as a full sweep for this call, at the cost of one entry.
        """
        entry = self._calls.get(call_u)
        if entry is None:
            return None
        if self._expire_entry(entry, when):
            del self._calls[call_u]
            return None
        return entry

    def _maybe_sweep(self, when: float) -> None:
        last = self._swept_at
        if last is None or abs(when - last) >= SWEEP_EVERY_S:
            self._expire(when)

    def _expire(self, when: float) -> None:
        dead = [
            call for call, entry in self._calls.items()
            if self._expire_entry(entry, when)
        ]
        for call in dead:
            del self._calls[call]
        self._swept_at = when

    def _in_this_qso(self, entry: _CallMemory, latest: SquareHearing,
                     when: float) -> bool:
        """True when ``latest`` was heard in the exchange with our station.

        No exchange, or one that has gone quiet, is not this QSO. The
        opening grid just before the first directed message still counts.
        """
        start = entry.exchange_start
        last = entry.exchange_last
        if start is None or last is None:
            return False
        if when + CLOCK_SLACK_S < start:
            return False
        if when > last + QSO_WINDOW_S:
            return False
        return latest.last_utc >= start - QSO_PREAMBLE_S

    def _is_rover_locked(self, call_u: str, entry: _CallMemory) -> bool:
        if call_u.endswith("/R"):
            return True
        return len({h.grid[:4] for h in entry.history}) >= 2

    def _tip(self, entry: _CallMemory, current: SquareHearing, assumed: bool) -> str:
        when = _hhmm(current.last_utc)
        prevs = [h for h in entry.history if h.grid[:4] != current.grid[:4]]
        prev = max(prevs, key=lambda h: h.last_utc) if prevs else None
        if assumed:
            lead = GRID_ASSUMED[:1].upper() + GRID_ASSUMED[1:] + ". "
            if prev is None:
                return (
                    f"{lead}{current.grid}, heard on {current.last_band} "
                    f"at {when} UTC."
                )
            return (
                f"{lead}Now {current.grid}, heard on {current.last_band} at {when} UTC. "
                f"Was {prev.grid} at {_hhmm(prev.last_utc)} UTC on {prev.last_band}."
            )
        # The lead is the band and time. The square stays in the hover so a
        # Work line that does not print the grid still names it.
        if prev is None:
            return f"Grid from {current.last_band} at {when} UTC ({current.grid})."
        return (
            f"Grid from {current.last_band} at {when} UTC ({current.grid}). "
            f"Was {prev.grid} at {_hhmm(prev.last_utc)} UTC on {prev.last_band}."
        )

    def _stale_tip(self, latest: SquareHearing) -> str:
        minutes = max(1, ROVER_WINDOW_S // 60)
        return (
            f"Rover grid not used. Last heard {latest.grid} on {latest.last_band} "
            f"at {_hhmm(latest.last_utc)} UTC, more than {minutes} minutes "
            f"before this contact."
        )

    def _rows_locked(self) -> list[tuple[str, list[tuple]]]:
        """Plain copy of every hearing (caller holds the lock). Cheap; the
        text formatting happens later, outside the lock."""
        return [
            (call, [
                (h.grid, h.first_utc, h.last_utc, h.first_band, h.last_band)
                for h in entry.history
            ])
            for call, entry in self._calls.items()
            if entry.history
        ]

    @staticmethod
    def _dump_rows(rows: list[tuple[str, list[tuple]]]) -> dict:
        calls = {}
        for call, history in sorted(rows, key=lambda r: r[0]):
            calls[call] = {
                "history": [
                    {
                        "grid": grid,
                        "first_utc": _dump_time(first),
                        "last_utc": _dump_time(last),
                        "first_band": first_band,
                        "last_band": last_band,
                    }
                    for grid, first, last, first_band, last_band in history
                ],
            }
        return {"schema": SCHEMA, "calls": calls}

    def _mark_dirty_locked(self) -> None:
        """Note a change; write it within ``SAVE_DELAY_S`` (caller holds the lock)."""
        if self.path is None:
            return
        self._dirty = True
        if self._timer is not None:
            return
        timer = threading.Timer(SAVE_DELAY_S, self._timer_fired)
        timer.daemon = True
        timer.name = "wims-heard-grids-save"
        self._timer = timer
        timer.start()

    def _timer_fired(self) -> None:
        with self._lock:
            self._timer = None
        self.flush()

    def pending(self) -> bool:
        """True when a hearing has not been written to the file yet."""
        with self._lock:
            return self._dirty

    def flush(self, *, create_dir: bool = True) -> bool:
        """Write the file now if anything changed. True when written."""
        if self.path is None:
            return False
        with self._write_lock:
            with self._lock:
                if not self._dirty:
                    return False
                if self._timer is not None:
                    self._timer.cancel()
                    self._timer = None
                rows = self._rows_locked()
                self._dirty = False
            if not self._write(rows, create_dir=create_dir):
                with self._lock:
                    self._dirty = True
                return False
            return True

    def _write(self, rows, *, create_dir: bool = True) -> bool:
        if self.path is None:
            return False
        try:
            if create_dir:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            elif not self.path.parent.is_dir():
                return False
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._dump_rows(rows), indent=1) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError:
            return False
        return True


# Stores with a file. On a normal exit, anything not yet written is saved.
_LIVE_STORES: "weakref.WeakSet[HeardGrids]" = weakref.WeakSet()


def flush_all() -> None:
    for store in list(_LIVE_STORES):
        try:
            # Never recreate a folder that is gone (a finished test).
            store.flush(create_dir=False)
        except Exception:
            pass


atexit.register(flush_all)
