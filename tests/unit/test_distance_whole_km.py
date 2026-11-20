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

"""Distances shown to people are whole km, rounded (16401 km, not 16400.8)."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from wims.engine.contest_rules import load_builtin  # noqa: E402
from wims.engine.geo import display_km, distance_km  # noqa: E402
from wims.integrations.cluster.parse import parse_spot  # noqa: E402
from wims.integrations.cluster.view import OnAir, SpotBook, build_list  # noqa: E402
from wims.state.logstore import LogStore  # noqa: E402

NOW = 1_700_000_000.0
HOME = "FN42EV"


def test_display_km_rounds_to_whole_km():
    assert display_km(16400.8) == 16401
    assert display_km(16400.4) == 16400
    assert display_km(0.4) == 0
    assert isinstance(display_km(12.6), int)
    assert display_km(None) is None
    assert display_km("junk") is None


def test_spotted_rows_and_hover_text_use_whole_km():
    log = LogStore(":memory:")
    log.rules = load_builtin("arrl-vhf")
    book = SpotBook()
    book.add(parse_spot("DX de K1TTT: 14074.0 JA1ABC PM95 FT8 1734Z"), NOW)
    rows, _ = build_list(
        book.rows(), now=NOW, ttl_s=900, our_grid=HOME, allowed=None,
        operations=[OnAir("20m", "FT8")],
        classify=lambda c, b, g, m: log.classify(c, b, g, m),
    )
    (row,) = rows
    raw = distance_km(HOME, "PM95")
    assert raw != int(raw)  # a real fraction, so rounding is tested
    assert row["distance_km"] == int(round(raw))
    assert isinstance(row["distance_km"], int)
    assert row["km"] == int(round(raw))
    texts = [f["detail"] for f in row["factors"]]
    for text in texts:
        for value in re.findall(r"(\d+(?:\.\d+)?) km", text or ""):
            assert "." not in value, text


def test_roster_snapshot_distance_is_whole_km():
    src = (ROOT / "src/wims/server/state.py").read_text(encoding="utf-8")
    assert '"distance_km": display_km(dist)' in src
    assert "round(dist, 1)" not in src
    view = (ROOT / "src/wims/integrations/cluster/view.py").read_text(encoding="utf-8")
    assert "round(km, 1)" not in view
    assert "int(best)" not in view and 'f"{int(km)} km"' not in view


def test_console_draws_whole_km():
    js = (ROOT / "src/wims/server/static/wims.js").read_text(encoding="utf-8")
    km = js.split("function rosKm", 1)[1].split("function rosAzAnt", 1)[0]
    assert "Math.round(Number(c.distance_km))" in km
    title = js.split("function spotTitle", 1)[1].split("let _spotData", 1)[0]
    assert "Math.round(Number(row.km))" in title


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
