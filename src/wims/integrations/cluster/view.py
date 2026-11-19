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

"""Turn stored spots into the Operate list.

A spot on another band or mode stays. It is marked QSY and the score pays
the QSY cost. It is not dropped for that. Dropped spots are: off the bands or modes
the operator selected, older than 15 minutes, out of range for that
band when a real or remembered grid is known, or not needed under the Goal
(already worked, a mode the Goal does not count, or no grid when the Goal
can only need a grid — VUCC, or a checklist or contest that is only counting
squares). Under a DXCC or checklist Goal that can need a station without a
grid, a no-grid spot stays, with an approximate heading from the country-file
entity centroid. That heading is marked approximate. Centroid distance is
never a hard in-range gate, and no grid is ever estimated from the entity.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from wims.engine import scoring as S
from wims.engine.dxcc import country_fields
from wims.engine.geo import aim, distance_km
from wims.integrations.cluster.modes import canonical_mode, infer_mode
from wims.integrations.cluster.parse import ParsedSpot
from wims.integrations.cluster.ranges import distance_fit, in_range, spotter_near_amount
from wims.integrations.cluster.settings import (
    DEFAULT_TTL_S, MODE_FILTERS, all_bands,
)


@dataclass
class OnAir:
    """One live WSJT-X instance, used only for the on-band / QSY flag."""

    band: str
    mode: str


@dataclass
class SpotGroup:
    call: str
    band: str
    mode: str
    freq_hz: int
    comment: str
    grid: str | None
    spotters: dict[str, float] = field(default_factory=dict)
    last_seen: float = 0.0
    hhmm: str = ""
    # WS Skim receivers that decoded this station here: label -> last time.
    skimmers: dict[str, float] = field(default_factory=dict)
    skim_seen: float = 0.0
    skim_snr: int | None = None
    skim_freq_hz: int = 0


def _hhmm(now: float) -> str:
    import time as _time
    return _time.strftime("%H%M", _time.gmtime(now))


class SpotBook:
    """One row per call and band. Later spots refresh the row.

    A cluster spot and a WS Skim decode of the same call and band share one
    row. While Skim has heard it within the spot life, the row is a SKIM
    row and keeps Skim's true frequency and mode.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], SpotGroup] = {}

    def add(self, spot: ParsedSpot, now: float, ttl: float = DEFAULT_TTL_S) -> None:
        mode = infer_mode(spot.freq_hz, spot.comment, spot.band)
        key = (spot.call, spot.band)
        row = self._rows.get(key)
        if row is None:
            row = SpotGroup(
                call=spot.call, band=spot.band, mode=mode, freq_hz=spot.freq_hz,
                comment=spot.comment, grid=spot.grid, last_seen=now, hhmm=spot.hhmm,
            )
            self._rows[key] = row
        else:
            skim_fresh = bool(row.skimmers) and (now - row.skim_seen) <= ttl
            if not skim_fresh:
                # Skim's decoded frequency and mode beat a typed cluster line.
                row.mode = mode
                row.freq_hz = spot.freq_hz
            row.comment = spot.comment
            row.last_seen = max(row.last_seen, now)
            row.hhmm = spot.hhmm
            if spot.grid and (not row.grid or len(spot.grid) >= len(row.grid)):
                row.grid = spot.grid
        row.spotters[spot.spotter] = now

    def add_skim(self, *, call: str, band: str, mode: str, freq_hz: int,
                 grid: str | None, receiver: str, snr: int | None,
                 now: float) -> None:
        """A WS Skim decode heard here. Upgrades a cluster row to SKIM."""
        call = (call or "").strip().upper()
        if not call or not band:
            return
        key = (call, band)
        row = self._rows.get(key)
        if row is None:
            row = SpotGroup(
                call=call, band=band, mode=mode, freq_hz=int(freq_hz),
                comment="", grid=grid or None, last_seen=now, hhmm=_hhmm(now),
            )
            self._rows[key] = row
        row.mode = mode or row.mode
        row.freq_hz = int(freq_hz) or row.freq_hz
        row.skim_freq_hz = int(freq_hz)
        row.last_seen = max(row.last_seen, now)
        row.skim_seen = now
        row.skim_snr = None if snr is None else int(snr)
        row.skimmers[receiver or "Skim"] = now
        if grid and (not row.grid or len(grid) >= len(row.grid)):
            row.grid = grid

    def drop_cluster(self) -> None:
        """Forget cluster spots (cluster turned off). Skim rows stay."""
        for key in list(self._rows):
            row = self._rows[key]
            if row.skimmers:
                row.spotters.clear()
            else:
                del self._rows[key]

    def prune(self, now: float, ttl: float = DEFAULT_TTL_S) -> None:
        dead = [key for key, row in self._rows.items() if now - row.last_seen > ttl]
        for key in dead:
            del self._rows[key]

    def rows(self) -> list[SpotGroup]:
        return list(self._rows.values())

    def clear(self) -> None:
        self._rows.clear()


def on_band(band: str, mode: str, operations: list[OnAir] | None) -> bool:
    want = canonical_mode(mode)
    for op in operations or []:
        if op.band == band and op.mode and canonical_mode(op.mode) == want:
            return True
    return False


def _mhz(freq_hz: int) -> str:
    return f"{freq_hz / 1_000_000:.3f}"


def _spotter_key(call: str) -> str:
    return (call or "").upper().split("-", 1)[0].strip()


def _latest_spotter(spotters: dict) -> str:
    if not spotters:
        return ""
    return max(spotters.items(), key=lambda item: (item[1], item[0]))[0]


def _as_grid(found) -> tuple[str, dict]:
    """A grid callback may return a square, or the roster's heard-grid lookup.

    The lookup carries the same hover the call roster uses (remembered,
    assumed, tip). A stale lookup with no square is blank. A plain string
    is only the square.
    """
    if found is None or found == "":
        return "", {}
    if isinstance(found, str):
        return found.strip().upper(), {}
    grid = str(getattr(found, "grid", "") or "").strip().upper()
    if not grid:
        return "", {}
    extra: dict = {}
    if getattr(found, "remembered", False):
        extra["grid_remembered"] = True
        extra["grid_assumed"] = bool(getattr(found, "assumed", False))
        tip = getattr(found, "tip", "") or ""
        if tip:
            extra["grid_tip"] = tip
    return grid, extra


def _needed_without_grid(verdict) -> bool:
    """True when this Goal can still need the station with no Maidenhead grid.

    VUCC and a grid-only checklist return not-needed, so those spots stay
    dropped. A contest row has no chase word, so it also stays dropped.
    DXCC, and a checklist line such as DXCC or continent, can be needed
    without a square.
    """
    if verdict is None or getattr(verdict, "is_dupe", True):
        return False
    chase = getattr(verdict, "chase", "") or ""
    if chase not in ("needed", "unconfirmed"):
        return False
    if chase == "needed" and (getattr(verdict, "need_label", "") or "") == "grid":
        return False
    return True


def _ago(age_s: float | None) -> str:
    if age_s is None:
        return "No spot line yet."
    age = max(0, int(age_s))
    if age < 60:
        return f"Last spot {age}s ago."
    return f"Last spot {age // 60} min ago."


def _retry_wait(retry_s: float) -> str:
    if not retry_s:
        return ""
    return f", retrying in {max(0, int(retry_s))} s"


def mode_bucket(mode: str) -> str:
    """SSB, CW, FT8, FT4, or Other. Other is every other inferred mode."""
    token = canonical_mode(mode)
    if token.upper() in {"SSB", "CW", "FT8", "FT4"}:
        return token.upper()
    return "Other"


def mode_choices(selected) -> list[dict]:
    chosen = None if selected == "all" else set(selected or [])
    out = []
    for mode in MODE_FILTERS:
        out.append({
            "mode": mode,
            "on": chosen is None or mode in chosen,
        })
    return out


def status_line(*, enabled: bool, phase: str, host: str, port: int,
                callsign: str, retry_s: float, reason: str,
                spot_count: int, needed_count: int,
                bands_empty: bool, have_grid: bool,
                have_lines: bool = False, modes_empty: bool = False) -> str:
    """One line for Operate. A failure names what to do next.

    ``connected, N spots`` is only for a session that has received spot
    lines. A TCP session with no bytes is not that line.
    """
    if not enabled:
        return "Cluster: off. Turn it on in Setup."
    if bands_empty:
        return "Cluster: no bands are selected. Choose bands in Setup."
    if modes_empty:
        return "Cluster: no modes are selected. Choose modes on Operate."
    if phase == "need_call":
        return "Cluster: no callsign. Set the station call in Setup."
    if phase in {"off", "connecting", "tcp"}:
        return f"Cluster: connecting to {host}..."
    if phase == "refused":
        who = callsign or "that callsign"
        return f"Cluster: {host} refused {who}. Check the callsign in Setup."
    if phase == "extra":
        return (
            "Cluster: the node asked for more than a callsign. "
            "WS Hub only sends your callsign. Pick another node in Setup."
        )
    if phase == "silent" or (phase == "retry" and reason == "silent"):
        return "Cluster: node sent nothing. Try another node on Setup" + _retry_wait(retry_s) + "."
    if phase == "retry":
        wait = _retry_wait(retry_s)
        target = f"{host}:{port}" if port else host
        if reason == "dns":
            return (
                f"Cluster: the name {host} did not resolve. "
                f"Check the node address in Setup{wait}."
            )
        if reason == "refused_port":
            return (
                f"Cluster: {host} refused the connection on port {port}. "
                f"Check the port in Setup{wait}."
            )
        if reason == "quiet":
            return f"Cluster: {host} went quiet{wait}."
        if reason == "closed":
            return f"Cluster: {host} closed the connection{wait}."
        return f"Cluster: can't reach {target}{wait}."
    who = callsign or "the station"
    if phase == "logged_in" and spot_count <= 0 and not have_lines:
        return f"Cluster: logged in as {who}, no spots yet."
    if not have_grid:
        return (
            f"Cluster: logged in as {who}, {spot_count} spots. "
            "No grid from WSJT-X yet, so range cannot be checked."
        )
    return f"Cluster: logged in as {who}, {spot_count} spots, {needed_count} needed"


def status_detail(*, host: str, port: int, callsign: str, qsy_cost: int,
                  bands, ttl_s: int, drops: dict, weights: dict,
                  last_spot_age: float | None = None,
                  node_spoke: bool = False, login_sent: bool = False,
                  modes=None, phase: str = "") -> str:
    if bands is None:
        band_words = "every amateur band"
    elif not bands:
        band_words = "no bands"
    else:
        band_words = ", ".join(bands)
    if modes is None:
        mode_words = "every mode"
    elif not modes:
        mode_words = "no modes"
    else:
        mode_words = ", ".join(modes)
    parts = [
        f"Node {host} port {port}.",
        f"Login {callsign}." if callsign else "No callsign yet.",
        (
            "Logged in."
            if login_sent and phase in {"logged_in", "connected"}
            else "Not logged in yet."
        ),
        (
            "The node has spoken."
            if node_spoke and phase in {"tcp", "logged_in", "connected"}
            else "The node has not spoken."
        ),
        _ago(last_spot_age),
        "Read-only. WS Hub sends only your callsign, then listens.",
        "It does not post spots. A click does not transmit or retune a radio.",
        "Some nodes allow one connection per callsign.",
        f"Spots drop off after {max(1, ttl_s // 60)} minutes.",
        "QSY means no live WSJT-X is on that band and mode. On band means one is.",
        f"QSY cost is {qsy_cost} points.",
        f"Bands: {band_words}.",
        f"Modes: {mode_words}.",
    ]
    if drops:
        bits = []
        labels = (
            ("no_grid", "no grid"),
            ("range", "out of range"),
            ("worked", "already worked"),
            ("no_station_grid", "station grid unknown"),
        )
        for key, label in labels:
            n = int(drops.get(key) or 0)
            if n:
                bits.append(f"{n} {label}")
        if bits:
            parts.append("Not listed: " + ", ".join(bits) + ".")
    named = ", ".join(f"{name} {value:g}" for name, value in weights.items())
    parts.append("Weights: " + named + ".")
    return " ".join(parts)


def build_list(
    groups: list[SpotGroup],
    *,
    now: float,
    ttl_s: float,
    our_grid: str | None,
    allowed: set[str] | None,
    operations: list[OnAir] | None,
    classify,
    grid_for=None,
    rover_for=None,
    spotter_grid=None,
    qsy_cost: float = 8,
    allowed_modes: set[str] | None = None,
    entity_latlon=None,
    opening_for=None,
    show_cluster: bool = True,
    show_skim: bool = True,
) -> tuple[list[dict], dict]:
    """Rows for the Spotted list, and counts of why others were left off.

    Need is ``classify(call, band, grid, mode)`` for this spot's own band
    and mode — the same check as the roster. The row keeps that verdict's
    ``need_label``, ``chase``, and reasons so the Operate badge is the one
    the roster already draws. This does not invent a second place order.

    ``spot_count`` is every fresh spot on a selected band and mode. ``needed``
    is the list the operator sees.

    A row WS Skim heard within ``ttl_s`` is a SKIM row (green tag, never
    dimmed, sorted first: it was heard here). Skim rows skip the band range
    gate, because a decode here proves the path. Other rows are CLU rows.
    The Skim and Cluster checkboxes hide each kind.
    """
    weights = S.cluster_weights(qsy_cost)
    rows: list[dict] = []
    drops = {"no_grid": 0, "range": 0, "worked": 0, "no_station_grid": 0, "spot_count": 0}
    home = (our_grid or "").strip().upper() or None
    for group in groups:
        age = now - group.last_seen
        if age > ttl_s or age < -5:
            continue
        if allowed is not None and group.band not in allowed:
            continue
        if allowed_modes is not None and mode_bucket(group.mode) not in allowed_modes:
            continue
        heard_here = bool(group.skimmers) and (now - group.skim_seen) <= ttl_s
        if heard_here and show_skim:
            as_skim = True
        elif group.spotters and show_cluster:
            as_skim = False
        else:
            continue
        if as_skim:
            age = max(0.0, now - group.skim_seen)
        elif group.spotters:
            age = max(0.0, now - max(group.spotters.values()))
        drops["spot_count"] += 1
        grid = (group.grid or "").strip().upper() or None
        remembered: dict = {}
        if grid is None and grid_for is not None:
            filled, remembered = _as_grid(grid_for(group.call))
            grid = filled or None
        if not home:
            drops["no_station_grid"] += 1
            continue
        latlon = None
        if not grid:
            # Still pass grid=None into classify. Never invent a square.
            verdict = classify(group.call, group.band, None, group.mode)
            if not _needed_without_grid(verdict):
                drops["no_grid"] += 1
                continue
            if entity_latlon is not None:
                try:
                    latlon = entity_latlon(group.call)
                except Exception:
                    latlon = None
            az_dx, km, bearing_source = aim(home, None, entity_latlon=latlon)
            # Centroid distance is not a hard in-range gate.
        else:
            km = distance_km(home, grid)
            if not as_skim and not in_range(km, group.band, group.mode):
                drops["range"] += 1
                continue
            verdict = classify(group.call, group.band, grid, group.mode)
            if getattr(verdict, "is_dupe", False):
                drops["worked"] += 1
                continue
            from_memory = bool(remembered.get("grid_remembered"))
            az_dx, km, bearing_source = aim(
                home, None if from_memory else grid,
                heard_grid=grid if from_memory else None,
            )
        needs_qsy = not on_band(group.band, group.mode, operations)
        rover = "/R" in group.call.upper()
        if not rover and rover_for is not None:
            rover = bool(rover_for(group.call))
        near_amt = 0.0
        near_detail = ""
        best = None
        best_call = ""
        if spotter_grid is not None:
            for spotter in group.spotters:
                square, _spotter_extra = _as_grid(spotter_grid(_spotter_key(spotter)))
                if not square:
                    continue
                dist = distance_km(home, square)
                if dist is None:
                    continue
                if best is None or dist < best:
                    best = dist
                    best_call = _spotter_key(spotter)
        if best is not None:
            near_amt = spotter_near_amount(best)
            near_detail = f"{best_call} {int(best)} km away"
        skim_names = sorted(group.skimmers) if as_skim else []
        if as_skim:
            near_amt = spotter_near_amount(0.0)
            near_detail = "heard here by WS Skim " + ", ".join(skim_names)
        if bearing_source == "dxcc":
            # Do not score an untrustworthy centroid distance.
            fit = 0.0
            if km is None:
                distance_detail = ""
            else:
                distance_detail = f"approx {int(round(km))} km"
        else:
            fit = distance_fit(km, group.band, group.mode)
            distance_detail = f"{int(km)} km" if km is not None else ""
        scored = S.score_spot(S.SpotFeatures(
            is_new_mult=bool(getattr(verdict, "is_new_mult", False)),
            chase=getattr(verdict, "chase", "") or "",
            is_rover=rover,
            needs_qsy=needs_qsy,
            age_s=age,
            ttl_s=ttl_s,
            spotter_count=len(group.spotters) + len(skim_names),
            spotter_near=near_amt,
            distance_fit=fit,
            need_detail=getattr(verdict, "need_reason", "") or "",
            mult_detail=getattr(verdict, "mult_reason", "") or "",
            near_detail=near_detail,
            distance_detail=distance_detail,
        ), weights)
        chase = getattr(verdict, "chase", "") or ""
        need_label = getattr(verdict, "need_label", "") or ""
        need_reason = getattr(verdict, "need_reason", "") or ""
        mult_reason = getattr(verdict, "mult_reason", "") or ""
        is_new_mult = bool(getattr(verdict, "is_new_mult", False))
        # Same word the roster badge shows. A contest row leaves it blank;
        # the mult badge is separate. Unconfirmed keeps that word.
        if chase == "needed":
            why = need_label
        elif chase == "unconfirmed":
            why = "unconfirmed"
        else:
            why = ""
        place = country_fields(group.call)
        qsy_points = 0.0
        for factor in scored.factors:
            if factor.name == "qsy":
                qsy_points = factor.contribution
        where = "QSY" if needs_qsy else "on band"
        open_path = None
        open_reason = ""
        psk_count = 0
        local_count = 0
        if opening_for is not None:
            try:
                found = opening_for(group.band, az_dx)
            except Exception:
                found = None
            if found is not None:
                open_path = getattr(found, "likely", None)
                if isinstance(found, dict):
                    open_path = found.get("likely")
                    open_reason = found.get("reason") or ""
                    psk_count = int(found.get("psk_count") or 0)
                    local_count = int(found.get("local_count") or 0)
                else:
                    open_reason = getattr(found, "reason", "") or ""
                    psk_count = int(getattr(found, "psk_count", 0) or 0)
                    local_count = int(getattr(found, "local_count", 0) or 0)
        skim_reason = ""
        if as_skim:
            snr = group.skim_snr
            snr_words = f", {snr:+d} dB" if snr is not None else ""
            ago = int(max(0.0, now - group.skim_seen))
            skim_reason = (
                f"Heard here by WS Skim {', '.join(skim_names)}{snr_words}, "
                f"{ago} s ago. Receive only."
            )
            open_path = True
            open_reason = skim_reason
        cluster_calls = sorted(group.spotters)
        spotter_calls = cluster_calls + [f"Skim {name}" for name in skim_names]
        if as_skim:
            latest_skim = max(group.skimmers.items(), key=lambda kv: (kv[1], kv[0]))[0]
            spotter = f"Skim {latest_skim}"
        else:
            spotter = _latest_spotter(group.spotters)
        rows.append({
            "call": group.call,
            "country": place["country"],
            "continent": place["continent"],
            "cq_zone": place["cq_zone"],
            "grid": grid,
            "grid_remembered": bool(remembered.get("grid_remembered")),
            "grid_assumed": bool(remembered.get("grid_assumed")),
            "grid_tip": remembered.get("grid_tip") or "",
            "grid_stale": False,
            "band": group.band,
            "mode": group.mode,
            "freq_hz": group.freq_hz,
            "freq": _mhz(group.freq_hz),
            "az": None if az_dx is None else round(az_dx),
            "distance_km": None if km is None else round(km, 1),
            "bearing_source": bearing_source,
            "where": where,
            "qsy": where,
            "needs_qsy": needs_qsy,
            "qsy_points": qsy_points,
            "why": why,
            "why_full": need_reason or mult_reason,
            "chase": chase,
            "need_label": need_label,
            "need_reason": need_reason,
            "mult_reason": mult_reason,
            "is_new_mult": is_new_mult,
            "is_needed": True,
            "is_dupe": False,
            "is_rover": rover,
            "score": scored.total,
            "age": round(max(0.0, age), 1),
            "age_s": int(max(0, age)),
            "spotter": spotter,
            "spotters": len(spotter_calls),
            "spotter_calls": spotter_calls,
            "km": None if km is None else int(round(km)),
            "factors": [
                {"name": f.name, "contribution": f.contribution, "detail": f.detail}
                for f in scored.factors
            ],
            "source": "skim" if as_skim else "cluster",
            "source_tag": "SKIM" if as_skim else "CLU",
            "heard_here": as_skim,
            "also_cluster": bool(as_skim and group.spotters),
            "skim_receivers": skim_names,
            "skim_snr": group.skim_snr if as_skim else None,
            "skim_reason": skim_reason,
            "open_path": open_path,
            "open_reason": open_reason,
            "psk_count": psk_count,
            "local_count": local_count,
            "dim": (not as_skim) and open_path is False,
        })
    # Heard here first, then likely-workable, then score. Unlikely spots
    # stay listed.
    rows.sort(key=lambda row: (
        0 if row.get("heard_here") else 1,
        0 if row.get("open_path") is True else 1 if row.get("open_path") is False else 0,
        -row["score"],
        row["age_s"],
        row["call"],
    ))
    return rows, drops


def band_choices(selected) -> list[dict]:
    chosen = None if selected == "all" else set(selected or [])
    out = []
    for band in all_bands():
        out.append({
            "band": band,
            "on": chosen is None or band in chosen,
        })
    return out
