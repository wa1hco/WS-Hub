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

"""Spot click points the WSJT-X on that band at the spot: Configure only.

Mode (WSJT-X then moves its own dial to that mode's frequency on the band)
and Rx DF. Never band/dial, TX frequency, DX call, Reply, Halt, Enable Tx.
"""

import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wims.server.app import LiveFleet  # noqa: E402
from wims.udp import encode as E  # noqa: E402
from wims.udp import messages as M  # noqa: E402
from wims.udp.spot_tune import plan_spot_tune, rx_offset  # noqa: E402

FLEX = "WSJT-X - flex"
IC = "WSJT-X - IC9700"
CTRL = 50123


def _node(band, mode, dial, **kw):
    now = time.time()
    base = dict(band=band, mode=mode, dial_hz=dial, last_status=now,
                last_heartbeat=now, last_decode=0, transmitting=False, tx_enabled=False)
    base.update(kw)
    return SimpleNamespace(**base)


def _spot(mode, freq, band="20m", call="EX7CQ"):
    return {"call": call, "band": band, "mode": mode, "freq_hz": freq}


class FakeTx:
    def __init__(self):
        self.dest = ("224.0.0.73", 2237)
        self.calls = []

    def configure(self, inst, **kw):
        self.calls.append(("configure", inst, kw))

    def reply(self, *a, **kw):
        self.calls.append(("reply", a, kw))

    def halt(self, *a, **kw):
        self.calls.append(("halt", a, kw))

    def configure_grid(self, *a, **kw):
        self.calls.append(("grid", a, kw))


def _live_with(status_kw=None, mid=FLEX, dial=14_074_000, mode="FT8"):
    live = LiveFleet()
    live._tx = FakeTx()
    later = []
    live._spot_tune_later = lambda delay, fn: later.append((delay, fn))
    kw = dict(mode=mode, de_call="WA1HCO", de_grid="FN42")
    kw.update(status_kw or {})
    live.observe_wsjtx(M.parse(E.build_status(mid, dial, **kw)), time.time(), "127.0.0.1", CTRL)
    return live, later


# -- planner -------------------------------------------------------------- #

def test_same_mode_in_passband_sets_only_rx_offset():
    plan = plan_spot_tune(_spot("FT8", 14_075_520), {FLEX: _node("20m", "FT8", 14_074_000)},
                          now=time.time())
    assert plan.ok and plan.instance == FLEX
    assert plan.mode == "" and plan.rx_df == 1520 and not plan.rx_df_after_mode


def test_ft8_to_ft4_switches_mode_and_sets_offset_later():
    plan = plan_spot_tune(_spot("FT4", 14_081_200), {FLEX: _node("20m", "FT8", 14_074_000)},
                          now=time.time())
    assert plan.ok and plan.mode == "FT4" and plan.rx_df is None and plan.rx_df_after_mode
    assert "its own FT4 frequency" in plan.line


def test_other_band_is_a_message_not_an_action():
    plan = plan_spot_tune(_spot("FT8", 7_075_000, band="40m"),
                          {FLEX: _node("20m", "FT8", 14_074_000)}, now=time.time())
    assert not plan.ok and plan.error == "no_instance_on_band"
    assert "cannot change WSJT-X's band" in plan.line and "flex on 20m" in plan.line
    assert plan.mode == "" and plan.rx_df is None


def test_off_passband_same_mode_is_a_message():
    plan = plan_spot_tune(_spot("FT8", 14_090_500), {FLEX: _node("20m", "FT8", 14_074_000)},
                          now=time.time())
    assert not plan.ok and plan.error == "off_passband"
    assert "16.5 kHz above" in plan.line and "cannot move the dial" in plan.line


def test_cw_ssb_and_beacons_change_nothing():
    for mode in ("CW", "SSB", "USB", "WSPR"):
        plan = plan_spot_tune(_spot(mode, 14_025_000), {FLEX: _node("20m", "FT8", 14_074_000)},
                              now=time.time())
        assert not plan.ok and plan.mode == "" and plan.rx_df is None, mode


def test_busy_decode_only_ambiguous_and_stale():
    nodes = {FLEX: _node("20m", "FT8", 14_074_000, tx_enabled=True)}
    plan = plan_spot_tune(_spot("FT8", 14_075_000), nodes, now=time.time(),
                          busy=lambda i: "in a QSO (Enable Tx is on)")
    assert not plan.ok and plan.error == "busy"
    plan = plan_spot_tune(_spot("FT8", 14_075_000), {FLEX: _node("20m", "FT8", 14_074_000)},
                          now=time.time(), tx_capable=lambda i: False)
    assert plan.error == "decode_only"
    two = {FLEX: _node("20m", "FT4", 14_080_000), IC: _node("20m", "FT4", 14_080_000)}
    assert plan_spot_tune(_spot("FT8", 14_075_000), two, now=time.time()).error == "ambiguous"
    # Two on the band, one already in the spot's mode: that one is used.
    mixed = {FLEX: _node("20m", "FT8", 14_074_000), IC: _node("20m", "FT4", 14_080_000)}
    assert plan_spot_tune(_spot("FT8", 14_075_000), mixed, now=time.time()).instance == FLEX
    stale = {FLEX: _node("20m", "FT8", 14_074_000, last_status=0, last_heartbeat=0)}
    assert plan_spot_tune(_spot("FT8", 14_075_000), stale, now=time.time()).error == \
        "no_instance_on_band"


def test_if_seat_offset_uses_rf_on_both_sides():
    # A 432 seat behind a 28 MHz IF: WS Hub's dial is already RF (432.174).
    nodes = {"ROY-432": _node("70cm", "FT8", 432_174_000)}
    plan = plan_spot_tune(_spot("FT8", 432_175_300, band="70cm"), nodes, now=time.time())
    assert plan.ok and plan.rx_df == 1300
    assert rx_offset(144_175_000, 144_174_000, "MSK144") is None


# -- LiveFleet: what goes on the wire -------------------------------------- #

def test_click_sends_one_configure_with_no_change_sentinels():
    live, _later = _live_with()
    out = live.tune_to_spot(_spot("FT8", 14_075_600))
    assert out["ok"] and out["retuned"] and out["transmitted"] is False
    assert len(live._tx.calls) == 1
    kind, inst, kw = live._tx.calls[0]
    assert kind == "configure" and inst == FLEX
    assert kw["dx_call"] == "" and kw["dx_grid"] == ""
    assert kw["generate_messages"] is False
    assert kw["mode"] == "" and kw["rx_df"] == 1600
    assert kw["dests"] == [("127.0.0.1", CTRL)]
    # The datagram itself: only Rx DF set; DX call/grid empty; no Gen Std Msgs.
    raw = E.build_configure(FLEX, mode=kw["mode"], rx_df=kw["rx_df"],
                            dx_call="", dx_grid="", generate_messages=False)
    msg = M.parse(raw)
    assert msg.mode == "" and msg.rx_df == 1600 and msg.dx_call == "" and msg.dx_grid == ""
    assert msg.generate_messages is False


def test_ft4_click_switches_mode_then_sets_rx_offset_after_status():
    live, later = _live_with()
    out = live.tune_to_spot(_spot("FT4", 14_081_400))
    assert out["ok"] and live._tx.calls[0][2]["mode"] == "FT4"
    assert live._tx.calls[0][2]["rx_df"] is None
    assert later and later[0][0] >= 1.0      # after WSJT-X restores its Rx box
    # WSJT-X switched and moved the dial to its FT4 frequency on 20m.
    live.observe_wsjtx(M.parse(E.build_status(FLEX, 14_080_000, mode="FT4")),
                       time.time(), "127.0.0.1", CTRL)
    later.pop(0)[1]()
    kind, inst, kw = live._tx.calls[-1]
    assert kind == "configure" and kw["rx_df"] == 1400 and kw["mode"] == ""
    assert kw["dx_call"] == "" and kw["generate_messages"] is False


def test_nothing_sent_while_enable_tx_or_transmitting():
    for status in ({"tx_enabled": True}, {"transmitting": True}):
        live, _ = _live_with(status)
        out = live.tune_to_spot(_spot("FT8", 14_075_000))
        assert not out["ok"] and out["error"] == "busy"
        assert live._tx.calls == []


def test_other_band_and_read_only_send_nothing():
    live, _ = _live_with()
    out = live.tune_to_spot(_spot("FT8", 7_075_000, band="40m"))
    assert not out["ok"] and live._tx.calls == []
    live._tx = None
    out = live.tune_to_spot(_spot("FT8", 14_075_000))
    assert out["error"] == "tx_disabled"


def test_never_reply_halt_or_grid():
    live, later = _live_with()
    for spot in (_spot("FT8", 14_075_000), _spot("FT4", 14_081_000),
                 _spot("CW", 14_025_000), _spot("FT8", 7_074_500, band="40m")):
        live.tune_to_spot(spot)
    for _, fn in later:
        fn()
    assert {c[0] for c in live._tx.calls} <= {"configure"}


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    if failed:
        raise SystemExit(1)
