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

"""Spotted modes: no WSPR/FST4W rows, cluster "data" spots get PSK Reporter's
mode, the mode filter checks the shown mode, and PSKR shows just the call."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from wims.engine.contest_rules import load_builtin  # noqa: E402
from wims.integrations.cluster.modes import infer_mode, is_beacon_mode  # noqa: E402
from wims.integrations.cluster.parse import parse_spot  # noqa: E402
from wims.integrations.cluster.psk import Report  # noqa: E402
from wims.integrations.cluster.view import OnAir, SpotBook, build_list, with_psk  # noqa: E402
from wims.state.logstore import LogStore  # noqa: E402

NOW = 1_700_000_000.0
HOME = "FN42EV"
TTL = 15 * 60


def _rep(call, freq, mode, rx="KM3T-2", snr=-12):
    return Report(sender_call=call, sender_grid="LN40", receiver_call=rx,
                  receiver_grid="FN42AB", freq_hz=freq, mode=mode, ts=NOW - 30, snr=snr)


def _rows(groups, **kw):
    log = LogStore(":memory:")
    log.rules = load_builtin("arrl-vhf")
    rows, _ = build_list(
        groups, now=NOW, ttl_s=TTL, our_grid=HOME, allowed=None,
        operations=[OnAir("20m", "FT8")],
        classify=lambda c, b, g, m: log.classify(c, b, g, m), **kw,
    )
    return {r["call"]: r for r in rows}


def _book(*lines):
    book = SpotBook()
    for line in lines:
        book.add(parse_spot(line), NOW - 60)
    return book


def test_beacon_modes():
    for m in ("WSPR", "wspr", "FST4W", "WSPR-15"):
        assert is_beacon_mode(m), m
    for m in ("FT8", "FT4", "FST4", "data", "", None):
        assert not is_beacon_mode(m), m


def test_pskr_rows_skip_wspr_and_fst4w():
    reports = [_rep("PZ5JW", 14_097_100, "WSPR"), _rep("OH8WX", 14_076_400, "FT8"),
               _rep("ZS1AB", 14_097_000, "FST4W")]
    groups = with_psk([], reports, home_grid=HOME, now=NOW, window_s=TTL)
    assert {g.call for g in groups} == {"OH8WX"}
    rows = _rows(groups)
    assert set(rows) == {"OH8WX"}
    assert rows["OH8WX"]["mode"] == "FT8"


def test_cluster_wspr_spot_is_not_listed():
    book = _book("DX de K1TTT:     14097.1  PZ5JW        WSPR                    1734Z")
    assert _rows(book.rows()) == {}


def test_cluster_data_spot_takes_psk_mode_and_filter_uses_shown_mode():
    line = "DX de EA5AJX:    14090.0  EX7CQ        CQ                      1734Z"
    assert infer_mode(14_090_000, "CQ", "20m") == "data"
    book = _book(line)
    groups = with_psk(book.rows(), [_rep("EX7CQ", 14_090_500, "FT8")],
                      home_grid=HOME, now=NOW, window_s=TTL)
    # PSKR hidden: the CLU row shows FT8, not "data".
    clu = _rows(groups, show_psk=False)
    assert clu["EX7CQ"]["source"] == "cluster"
    assert clu["EX7CQ"]["mode"] == "FT8"
    assert "EX7CQ" in _rows(groups, show_psk=False, allowed_modes={"FT8"})
    assert "EX7CQ" not in _rows(groups, show_psk=False, allowed_modes={"Other"})
    # PSKR shown: the row is PSKR with the same mode.
    psk = _rows(groups)
    assert psk["EX7CQ"]["source"] == "psk" and psk["EX7CQ"]["mode"] == "FT8"
    assert "EX7CQ" not in _rows(groups, allowed_modes={"Other"})


def test_cluster_data_spot_without_psk_stays_data_under_other():
    book = _book("DX de EA5AJX:    14090.0  EX7CQ        LN40                    1734Z")
    rows = _rows(book.rows(), allowed_modes={"Other"})
    assert rows["EX7CQ"]["mode"] == "data"
    assert "EX7CQ" not in _rows(book.rows(), allowed_modes={"FT8"})


def test_fox_hound_comments_are_ft8():
    for comment in ("F/H", "Fox/Hound", "fox", "SuperFox", "F/H up 1000"):
        assert infer_mode(14_090_000, comment, "20m") == "FT8", comment
    assert infer_mode(14_074_000, "FT8 -12dB", "20m") == "FT8"
    assert infer_mode(14_090_000, "", "20m") == "data"


def test_pskr_spotter_is_just_the_call():
    groups = with_psk([], [_rep("OH8WX", 14_076_400, "FT8", rx="KM3T-2"),
                           _rep("OH8WX", 14_076_400, "FT8", rx="W1NT-6")],
                      home_grid=HOME, now=NOW, window_s=TTL)
    row = _rows(groups)["OH8WX"]
    assert row["spotter"] in ("PSKR KM3T", "PSKR W1NT")
    assert "-" not in row["spotter"]
    js = (ROOT / "src/wims/server/static/wims.js").read_text(encoding="utf-8")
    cell = js.split("function spotSpotter", 1)[1].split("function spotQsy", 1)[0]
    assert 'row.source === "psk"' in cell
    psk_branch = cell.split('row.source === "psk"', 1)[1].split("}", 1)[0]
    assert "count" not in psk_branch and " +" not in psk_branch


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
