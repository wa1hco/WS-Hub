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

"""Point the WSJT-X on a spot's band at that spot. Receive side only.

What a spot click can change, and how (WSJT-X UDP protocol, Configure,
message type 15 — the only "In" message that changes settings):

* **Mode.** Configure's Mode field. ``MainWindow::remote_configure`` calls
  ``set_mode`` when the mode differs (FT8, FT4, FT2, FST4, JT4, JT9, JT65,
  Q65, MSK144 …). Switching mode runs ``switch_mode``, which picks
  ``best_working_frequency`` for the **current band** from WSJT-X's own
  frequency list and retunes the radio there over CAT. So FT8 → FT4 on
  20m moves the dial from 14.074 to 14.080 (whatever the list says).
* **RX audio offset.** Configure's Rx DF field sets the Rx frequency box.
  It does not move the Tx frequency.
* **Dial frequency / band.** Not possible. The WSJT-X UDP protocol has no
  message that sets the dial, and WS Hub has no CAT path of its own. A spot
  on another band, or outside the current passband in the same mode, gets
  a one-line message instead.

Never sent: DX Call, DX Grid, Generate Std Msgs, Reply, Free Text, Halt.
Configure never clicks Enable Tx. WSJT-X acts on Configure only when
"Accept UDP requests" is on.
"""

from __future__ import annotations

from dataclasses import dataclass

# Modes WSJT-X's set_mode() accepts that a spot might name. WSPR/FST4W are
# beacons (never workable), so they are not offered.
SWITCHABLE = ("FT8", "FT4", "FT2", "FST4", "JT4", "JT9", "JT65", "Q65", "MSK144")
# Modes with no WSJT-X equivalent.
NOT_WSJTX = ("CW", "SSB", "FM", "RTTY", "PSK", "JS8", "WSPR", "FST4W")

RX_DF_MIN = 200       # WSJT-X Rx box minimum
RX_DF_MAX = 4000      # keep inside a normal waterfall / SSB passband
NODE_FRESH_S = 60.0   # a WSJT-X counts as live this long after its last packet


@dataclass
class TunePlan:
    ok: bool
    line: str
    error: str = ""
    instance: str = ""
    mode: str = ""          # Configure Mode to send ("" = no change)
    rx_df: int | None = None  # Configure Rx DF to send now
    rx_df_after_mode: bool = False  # set Rx DF once Status shows the new mode
    spot_freq_hz: int = 0


def _mode(text: str | None) -> str:
    token = (text or "").strip().upper().split(" ")[0] if text else ""
    return {"USB": "SSB", "LSB": "SSB", "DATA": "", "DIGI": "", "OTHER": ""}.get(token, token)


def _khz(hz: int) -> str:
    return f"{hz / 1000:.1f} kHz"


def _mhz(hz: int) -> str:
    return f"{hz / 1e6:.4f} MHz"


def rx_offset(spot_freq_hz: int, dial_hz: int, mode: str = "") -> int | None:
    """Audio offset for a spot on this dial, or None when it is off the passband."""
    if not spot_freq_hz or not dial_hz:
        return None
    if _mode(mode) == "MSK144":
        return None           # MSK144 Rx is fixed near 1500 Hz
    off = int(spot_freq_hz) - int(dial_hz)
    if RX_DF_MIN <= off <= RX_DF_MAX:
        return off
    return None


def _live(node, now: float) -> bool:
    last = max(float(getattr(node, "last_status", 0) or 0),
               float(getattr(node, "last_heartbeat", 0) or 0),
               float(getattr(node, "last_decode", 0) or 0))
    return (now - last) <= NODE_FRESH_S


def plan_spot_tune(spot: dict, nodes: dict, *, now: float,
                   tx_capable=lambda inst: True,
                   busy=lambda inst: "") -> TunePlan:
    """Decide what to send for a clicked spot. Pure: sends nothing."""
    call = str(spot.get("call") or "").strip().upper()
    band = str(spot.get("band") or "").strip()
    mode = _mode(spot.get("mode"))
    try:
        freq = int(float(spot.get("freq_hz") or 0))
    except (TypeError, ValueError):
        freq = 0
    who = call or "That spot"
    if mode in NOT_WSJTX:
        return TunePlan(False, f"{who} is {mode}. WSJT-X has no {mode} mode, so nothing was changed.",
                        error="not_wsjtx_mode")
    if not band or band == "?":
        return TunePlan(False, f"{who} has no band, so nothing was changed.", error="no_band")
    live = {mid: n for mid, n in (nodes or {}).items() if _live(n, now)}
    on_band = [mid for mid, n in live.items() if (getattr(n, "band", "") or "") == band]
    if not on_band:
        others = ", ".join(f"{mid} on {getattr(n, 'band', '?') or '?'}"
                           for mid, n in sorted(live.items())) or "none running"
        return TunePlan(
            False,
            f"No WSJT-X is on {band} ({others}). WS Hub cannot change WSJT-X's band. "
            f"Change band in WSJT-X, then click {who} again.",
            error="no_instance_on_band")
    workable = [mid for mid in on_band if tx_capable(mid)]
    if not workable:
        return TunePlan(
            False,
            f"The WSJT-X on {band} ({', '.join(sorted(on_band))}) is marked decode-only, "
            "so nothing was changed.", error="decode_only")
    if len(workable) > 1 and mode:
        same = [mid for mid in workable if _mode(getattr(live[mid], "mode", "")) == mode]
        if len(same) == 1:
            workable = same
    if len(workable) > 1:
        return TunePlan(
            False,
            f"{len(workable)} WSJT-X are on {band} ({', '.join(sorted(workable))}). "
            "WS Hub does not guess which one to change, so nothing was changed.",
            error="ambiguous")
    inst = workable[0]
    node = live[inst]
    why_busy = busy(inst)
    if why_busy:
        return TunePlan(False, f"{inst} is {why_busy}. Nothing was changed. Halt or finish first.",
                        error="busy", instance=inst)
    now_mode = _mode(getattr(node, "mode", ""))
    dial = int(getattr(node, "dial_hz", 0) or 0)
    if mode and mode in SWITCHABLE and mode != now_mode:
        return TunePlan(
            True,
            f"{inst}: switching WSJT-X from {now_mode or '?'} to {mode} on {band}. WSJT-X moves "
            f"the dial to its own {mode} frequency on {band}. The RX offset follows once it does. "
            "TX frequency, DX call and Enable Tx are not changed.",
            instance=inst, mode=mode, rx_df_after_mode=bool(freq), spot_freq_hz=freq)
    off = rx_offset(freq, dial, now_mode)
    if off is not None:
        return TunePlan(
            True,
            f"{inst}: RX offset set to {off} Hz for {who} on {band}. "
            "TX frequency, DX call and Enable Tx are not changed.",
            instance=inst, rx_df=off, spot_freq_hz=freq)
    if not freq or not dial:
        return TunePlan(True, f"{inst} is already on {band} {now_mode}. "
                        f"No frequency for {who}, so the RX offset was not changed.",
                        error="no_freq", instance=inst)
    if now_mode == "MSK144":
        return TunePlan(True, f"{inst} is already on {band} MSK144. Nothing to change.",
                        instance=inst)
    gap = freq - dial
    side = "above" if gap > 0 else "below"
    return TunePlan(
        False,
        f"{who} is at {_mhz(freq)}, {_khz(abs(gap))} {side} {inst}'s dial ({_mhz(dial)}), "
        "outside the WSJT-X passband. WS Hub cannot move the dial; tune the radio to it.",
        error="off_passband", instance=inst, spot_freq_hz=freq)
