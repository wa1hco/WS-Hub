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


"""A Spotted row and a roster row get the same verdict under one Goal.

Jeff's Goal counts grids overall (any band). A square worked on 40m is not
a new grid on 20m, on the call roster or on the Spotted list (SKIM rows).
Both lists call the same log classify, so this pins that they agree, and
that the grid scope in the Goal is what decides.
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="wims-scope-")
os.environ["HOME"] = HOME
os.environ["XDG_CONFIG_HOME"] = str(Path(HOME) / ".config")
os.environ["XDG_DATA_HOME"] = str(Path(HOME) / ".local" / "share")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wims.udp import encode as E  # noqa: E402
from wims.udp import messages as M  # noqa: E402

SEAT = "WSJT-X - flex"
SKIM = "SKIM-FLEX-20m-FT8-74"


def _adif(call, grid, band, freq, confirmed=True):
    qsl = "<QSL_RCVD:1>Y " if confirmed else ""
    return (f"<CALL:{len(call)}>{call} <GRIDSQUARE:{len(grid)}>{grid} "
            f"<MODE:3>FT8 <BAND:{len(band)}>{band} <FREQ:{len(freq)}>{freq} "
            f"<QSO_DATE:8>20261101 <TIME_ON:6>120000 {qsl}<EOR>\n")


def _setup(grid_scope: str):
    cfg = Path(os.environ["XDG_CONFIG_HOME"]) / "wims"
    cfg.mkdir(parents=True, exist_ok=True)
    checklist = {k: {"enabled": False, "scope": "overall"}
                 for k in ("dxcc", "continent", "cq", "itu", "state",
                           "county", "prefix", "call")}
    checklist["grid"] = {"enabled": True, "scope": grid_scope}
    (cfg / "contest_rules.json").write_text(json.dumps({
        "schema": 5, "goal": "operating", "goal_id": "checklist",
        "contest": None, "explicit": True, "suggested": False,
        "checklist": checklist, "count_unconfirmed": True,
    }), encoding="utf-8")
    (cfg / "logs.json").write_text(json.dumps({
        "schema": 1, "primary": "wsjtx", "shared_path": "", "overrides": {},
    }), encoding="utf-8")
    data = Path(os.environ["XDG_DATA_HOME"]) / "WSJT-X"
    data.mkdir(parents=True, exist_ok=True)
    # FN31 worked on 40m only. Nothing on 20m.
    (data / "wsjtx_log.adi").write_text(
        "WSJT-X ADIF Export\n<EOH>\n" + _adif("W1AW", "FN31", "40m", "7.074"),
        encoding="utf-8")
    # The default WSJT-X folder on this OS, in case it is not XDG.
    home_data = Path(HOME) / ".local" / "share" / "WSJT-X"
    if home_data != data:
        home_data.mkdir(parents=True, exist_ok=True)
        (home_data / "wsjtx_log.adi").write_text(
            (data / "wsjtx_log.adi").read_text(encoding="utf-8"), encoding="utf-8")


def _verdicts(grid_scope: str):
    from wims.server.app import LiveFleet
    _setup(grid_scope)
    live = LiveFleet()
    live.apply_log_use()
    live.attach_rules()
    now = time.time()
    live.observe_wsjtx(M.parse(E.build_status(
        SEAT, 14_074_000, mode="FT8", de_call="WA1HCO", de_grid="FN42EV")),
        now, "127.0.0.1", 5000)
    assert live.observe_skim(M.parse(E.build_status(
        SKIM, 14_074_000, mode="FT8", de_call="", de_grid="")), now, "127.0.0.1")
    for i, (call, grid) in enumerate((("K1OLD", "FN31"), ("K2NEW", "EM79"))):
        for mid in (SEAT, SKIM):
            msg = M.parse(E.build_decode(
                mid, time_ms=0, snr=-5, delta_time=0.1,
                delta_frequency=1000 + 100 * i, message=f"CQ {call} {grid}",
                mode="~"))
            if mid == SKIM:
                assert live.observe_skim(msg, now + 1, "127.0.0.1")
            else:
                live.observe_wsjtx(msg, now + 1, "127.0.0.1", 5000)
    snap = live.snapshot(now + 2)
    assert snap["log_in_use"]["count"] == 1
    roster = {c["call"]: bool(c.get("is_needed"))
              for c in snap["roster"]["candidates"]}
    spots = {r["call"]: (bool(r.get("is_needed")), r.get("source_tag"))
             for r in snap["cluster"]["rows"]}
    return roster, spots


def test_overall_grid_scope_same_on_roster_and_spotted():
    roster, spots = _verdicts("overall")
    # FN31 is in the log on 40m: not a new grid on 20m, anywhere.
    assert roster["K1OLD"] is False
    assert "K1OLD" not in spots       # Spotted lists only needed stations
    # EM79 is not in the log on any band: needed on both lists.
    assert roster["K2NEW"] is True
    assert spots["K2NEW"] == (True, "SKIM")


def test_per_band_grid_scope_same_on_roster_and_spotted():
    roster, spots = _verdicts("band")
    # Per band: FN31 on 20m is new, on both lists.
    assert roster["K1OLD"] is True
    assert spots["K1OLD"] == (True, "SKIM")
    assert roster["K2NEW"] is True
    assert spots["K2NEW"] == (True, "SKIM")


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
