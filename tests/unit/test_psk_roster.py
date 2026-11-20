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

"""PSK Reporter far stations as receive-only PSKR rows in the Spotted list.

Needed stations only (same classify as the call roster), across every
selected band, with a per-band count to help decide whether to change
bands. Uses the reports the five-minute poller already holds; no new
requests. Nothing here transmits.
"""

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wims.engine.contest_rules import load_builtin  # noqa: E402
from wims.engine.goal_rules import ChecklistRules  # noqa: E402
from wims.lotw import use_paths  # noqa: E402
from wims.integrations.cluster.client import ClusterService  # noqa: E402
from wims.integrations.cluster.parse import parse_spot  # noqa: E402
from wims.integrations.cluster.psk import (  # noqa: E402
    PskOpenings, Report, far_station, parse_reports,
)
from wims.integrations.cluster.settings import sanitize  # noqa: E402
from wims.integrations.cluster.view import (  # noqa: E402
    OnAir, SpotBook, band_summary, build_list, with_psk,
)
from wims.integrations.n1mm.qso import LoggedQso  # noqa: E402
from wims.state.logstore import LogStore  # noqa: E402

NOW = 1_700_000_000.0
HOME = "FN41VP"
ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "psk_reporter_fn41.xml"
TTL = 15 * 60


def _reports():
    return parse_reports(FIXTURE.read_text(encoding="utf-8"))


def _log():
    log = LogStore(":memory:")
    log.rules = load_builtin("arrl-vhf")
    return log


def _list(groups, *, log=None, show_psk=True, show_cluster=True,
          show_skim=True, grid_for=None, operations=None):
    log = log or _log()
    return build_list(
        groups, now=NOW, ttl_s=TTL, our_grid=HOME, allowed=None,
        operations=operations if operations is not None else [OnAir("20m", "FT8")],
        classify=lambda c, b, g, m: log.classify(c, b, g, m),
        grid_for=grid_for, show_psk=show_psk, show_cluster=show_cluster,
        show_skim=show_skim,
    )


def _by(rows):
    return {(row["call"], row["band"]): row for row in rows}


def test_far_station_direction_and_home_square():
    reports = {(r.sender_call, r.receiver_call): r for r in _reports()}
    # A home receiver heard a far sender: the sender is on the air.
    assert far_station(reports[("PY2DX", "N1RX")], "FN41") == ("PY2DX", "GG54RK", True)
    # A far receiver heard a home sender: that receiver is on the air too.
    assert far_station(reports[("K1HOME", "KL7EX")], "FN41") == ("KL7EX", "BP51IP", False)
    # Both ends in the home square is not a far station.
    assert far_station(reports[("W1LOCAL", "N1LOCAL")], "FN41") is None
    # A missing far grid stays blank. It is never estimated.
    assert far_station(reports[("NOGRID", "N1MISS")], "FN41") == ("NOGRID", "", True)
    assert far_station(reports[("PY2DX", "N1RX")], "") is None


def test_with_psk_adds_far_stations_and_leaves_the_book_alone():
    book = SpotBook()
    book.add(parse_spot("DX de K1TTT: 14074.0 DL1DX JO31 FT8 1734Z"), NOW - 30)
    groups = with_psk(book.rows(), _reports(), home_grid=HOME, now=NOW, window_s=TTL)
    by = {(g.call, g.band): g for g in groups}
    assert set(by) == {
        ("KL7EX", "10m"), ("W0DX", "10m"), ("PY2DX", "10m"),
        ("DL1DX", "20m"), ("NOGRID", "20m"), ("N5TX", "40m"),
    }
    dl = by[("DL1DX", "20m")]
    assert dl.psk_near == {"N1EU": NOW - 240}
    assert dl.psk_heard_here and not dl.psk_hears_home
    assert dl.psk_snr == -8
    assert dl.spotters  # the cluster spot is still on the copy
    kl = by[("KL7EX", "10m")]
    assert kl.psk_hears_home and not kl.psk_heard_here
    assert kl.psk_near == {"K1HOME": NOW - 60}
    assert kl.grid == "BP51IP"
    assert by[("NOGRID", "20m")].grid is None
    # The stored cluster row did not pick up PSK data.
    (stored,) = book.rows()
    assert stored.psk_near == {} and stored.psk_reports == 0
    # The stale DL1OLD report is past the window.
    assert ("DL1OLD", "20m") not in by
    # No home grid, no PSK rows.
    assert len(with_psk(book.rows(), _reports(), home_grid="", now=NOW,
                        window_s=TTL)) == 1


def test_psk_rows_are_needed_only_receive_only_and_tagged():
    log = _log()
    log.upsert(LoggedQso(
        id="q1", call="PY2DX", band="10m", grid="GG54", mode="FT8",
        points=1, is_mult=True, contest="VHF", timestamp="", operator="",
        rover_location=None, source="test",
    ))
    book = SpotBook()
    book.add(parse_spot("DX de K1TTT: 14074.0 DL1DX JO31 FT8 1734Z"), NOW - 30)
    groups = with_psk(book.rows(), _reports(), home_grid=HOME, now=NOW, window_s=TTL)
    rows, drops = _list(groups, log=log)
    by = _by(rows)
    # Worked already: dropped, the same as the roster.
    assert ("PY2DX", "10m") not in by
    assert drops["worked"] == 1
    # No grid on the report and none remembered: dropped under a grid Goal.
    assert ("NOGRID", "20m") not in by
    assert drops["no_grid"] == 1
    dl = by[("DL1DX", "20m")]
    assert dl["source"] == "psk" and dl["source_tag"] == "PSKR"
    assert dl["receive_only"] is True
    assert dl["is_needed"] is True
    assert dl["snr"] == -8
    assert dl["instance"] == "N1EU"
    assert dl["psk_receivers"] == ["N1EU"]
    assert dl["open_path"] is True and dl["dim"] is False
    assert "heard by N1EU at -8 dB" in dl["psk_reason"]
    assert dl["also_cluster_psk"] is True
    assert dl["qsy"] == "on band"
    assert "PSKR N1EU" in dl["spotter_calls"]
    kl = by[("KL7EX", "10m")]
    assert kl["source_tag"] == "PSKR"
    assert kl["snr"] is None  # its report is about our signal, not its own
    assert kl["psk_hears_home"] is True
    assert "hears K1HOME" in kl["psk_reason"]
    assert kl["qsy"] == "QSY"
    # Roster-shaped facts exist so every roster column can draw.
    for key in ("call", "country", "band", "mode", "grid", "freq_hz", "az",
                "distance_km", "age", "score", "instance", "snr"):
        assert key in kl, key


def test_goal_label_is_the_roster_verdict():
    use_paths([])
    log = LogStore(":memory:")
    log.rules = ChecklistRules(
        {"grid": {"enabled": True, "scope": "overall"}},
        title="Checklist", cty=None, log_label="the log",
    )
    log.upsert(LoggedQso(
        id="q1", call="W1ANY", band="20m", grid="GG54", mode="FT8",
        points=1, is_mult=True, contest="", timestamp="", operator="",
        rover_location=None, source="test",
    ))
    groups = with_psk([], _reports(), home_grid=HOME, now=NOW, window_s=TTL)
    rows, _drops = _list(groups, log=log)
    by = _by(rows)
    # GG54 is worked but not confirmed: listed with that word, as on the roster.
    py = by[("PY2DX", "10m")]
    assert py["chase"] == "unconfirmed" and py["why"] == "unconfirmed"
    dl = by[("DL1DX", "20m")]
    assert dl["chase"] == "needed" and dl["need_label"] == "grid"
    assert dl["why"] == "grid"


def test_remembered_grid_fills_a_blank_psk_grid():
    groups = with_psk([], _reports(), home_grid=HOME, now=NOW, window_s=TTL)
    rows, _ = _list(groups, grid_for=lambda call: "JO22" if call == "NOGRID" else "")
    by = _by(rows)
    assert by[("NOGRID", "20m")]["grid"] == "JO22"
    assert by[("NOGRID", "20m")]["source_tag"] == "PSKR"


def test_psk_row_skips_the_band_range_gate():
    far_2m = Report(
        sender_call="W5ABC", sender_grid="EM12", receiver_call="N1RX",
        receiver_grid="FN41FP", freq_hz=144_175_000, mode="FT8", ts=NOW - 60,
        snr=-19, sender_in_home=False, receiver_in_home=True,
    )
    book = SpotBook()
    book.add(parse_spot("DX de W5XX: 144174.0 W5DEF EM12 FT8 1734Z"), NOW - 30)
    groups = with_psk(book.rows(), [far_2m], home_grid=HOME, now=NOW, window_s=TTL)
    rows, drops = _list(groups)
    assert [(r["call"], r["source_tag"]) for r in rows] == [("W5ABC", "PSKR")]
    assert drops["range"] == 1


def test_checkboxes_and_skim_beats_psk():
    book = SpotBook()
    book.add(parse_spot("DX de K1TTT: 14074.0 DL1DX JO31 FT8 1734Z"), NOW - 30)
    groups = with_psk(book.rows(), _reports(), home_grid=HOME, now=NOW, window_s=TTL)
    rows, _ = _list(groups, show_psk=False)
    # DL1DX falls back to its cluster spot; PSK-only rows are hidden.
    assert [(r["call"], r["source_tag"]) for r in rows] == [("DL1DX", "CLU")]
    rows, _ = _list(groups, show_cluster=False)
    assert all(r["source_tag"] == "PSKR" for r in rows)
    book.add_skim(call="DL1DX", band="20m", mode="FT8", freq_hz=14_075_100,
                  grid="JO31", receiver="FLEX", snr=-12, now=NOW - 5)
    groups = with_psk(book.rows(), _reports(), home_grid=HOME, now=NOW, window_s=TTL)
    rows, _ = _list(groups)
    dl = _by(rows)[("DL1DX", "20m")]
    assert dl["source_tag"] == "SKIM"
    assert dl["snr"] == -12 and dl["instance"] == "FLEX"


def test_band_summary_counts_needed_calls_per_band():
    groups = with_psk([], _reports(), home_grid=HOME, now=NOW, window_s=TTL)
    rows, _ = _list(groups)
    summary = band_summary(rows, [OnAir("20m", "FT8")])
    assert [item["band"] for item in summary] == ["40m", "20m", "10m"]
    by = {item["band"]: item for item in summary}
    assert by["10m"]["needed"] == 3 and by["10m"]["psk"] == 3
    assert by["10m"]["on_band"] is False
    assert by["20m"]["on_band"] is True
    assert by["40m"]["needed"] == 1
    # The same call twice on one band counts once.
    dup = band_summary([
        {"call": "K1A", "band": "6m", "source": "skim"},
        {"call": "K1A", "band": "6m", "source": "cluster"},
    ])
    assert dup == [{"band": "6m", "needed": 1, "skim": 1, "psk": 0,
                    "cluster": 1, "on_band": False}]


def _service(tmp, **settings):
    svc = ClusterService(path=Path(tmp) / "cluster.json")
    base = {"enabled": True, "station_call": "WA1HCO", "station_grid": HOME,
            "psk_rank": True}
    base.update(settings)
    svc.update(base)
    calls = []

    def fetch(url):
        calls.append(url)
        return 200, FIXTURE.read_bytes(), ""

    svc._openings = PskOpenings(fetch=fetch, min_poll_s=300, clock=lambda: NOW)
    svc._openings.configure(grid=HOME, enabled=True)
    assert svc._openings.maybe_fetch() is True
    return svc, calls


def _public(svc):
    log = _log()
    return svc.public(
        now=NOW, callsign="WA1HCO", our_grid=HOME,
        operations=[OnAir("20m", "FT8")],
        classify=lambda c, b, g, m: log.classify(c, b, g, m),
    )


def test_service_lists_psk_rows_without_fetching_again():
    with tempfile.TemporaryDirectory() as tmp:
        svc, calls = _service(tmp)
        view = _public(svc)
        _public(svc)
        assert len(calls) == 1  # public() reuses the poll; it never fetches
        tags = {(r["call"], r["source_tag"]) for r in view["rows"]}
        assert ("DL1DX", "PSKR") in tags and ("KL7EX", "PSKR") in tags
        assert view["show_psk"] is True
        assert view["psk_count"] == len(view["rows"])
        assert {b["band"] for b in view["band_summary"]} == {"10m", "20m", "40m"}
        svc.update({"show_psk": False})
        assert _public(svc)["rows"] == []
        svc.update({"show_psk": True, "psk_rank": False})
        assert _public(svc)["rows"] == []
        assert len(calls) == 1


def test_selected_bands_and_modes_still_apply():
    with tempfile.TemporaryDirectory() as tmp:
        svc, _calls = _service(tmp, bands=["20m"])
        view = _public(svc)
        assert {r["band"] for r in view["rows"]} == {"20m"}
        svc.update({"bands": "all", "modes": ["CW"]})
        assert _public(svc)["rows"] == []


def test_show_psk_setting_defaults_on_and_round_trips():
    assert sanitize({})["show_psk"] is True
    assert sanitize({"show_psk": False})["show_psk"] is False


def test_spotted_ui_has_psk_checkbox_band_line_and_roster_columns():
    js = (ROOT / "src/wims/server/static/wims.js").read_text(encoding="utf-8")
    html = (ROOT / "src/wims/server/static/ops.html").read_text(encoding="utf-8")
    css = (ROOT / "src/wims/server/static/wims.css").read_text(encoding="utf-8")
    assert 'id="spot-show-psk"' in html
    assert 'id="spot-band-line"' in html
    assert 'id="spot-cols"' in html
    assert "ros-cols-menu spot-cols-menu" in html
    assert ".spot-tag.psk" in css
    # Every call-roster column is offered on the Spotted columns menu.
    ros = js.split("const ROS_COLS = [", 1)[1].split("\n];", 1)[0]
    ros_keys = re.findall(r'\{key:"([a-z0-9_]+)"', ros)
    order = re.search(r"const SPOT_COL_ORDER = \[([^\]]+)\]", js)
    assert order, "SPOT_COL_ORDER missing"
    order_keys = re.findall(r'"([a-z0-9_]+)"', order.group(1))
    assert set(ros_keys) <= set(order_keys)
    shared = [k for k in order_keys if k in ros_keys]
    assert shared == ros_keys  # same order as the roster
    defaults = re.search(r"const SPOT_COL_KEYS = \[([^\]]+)\]", js)
    assert set(re.findall(r'"([a-z0-9_]+)"', defaults.group(1))) <= set(order_keys)
    # One chooser, used by both lists.
    assert js.count("function buildColChecks(") == 1
    ros_sync = js.split("function rosSyncColChecks", 1)[1].split("function rosBuildHead", 1)[0]
    assert "buildColChecks(" in ros_sync
    spot_sync = js.split("function spotSyncColChecks", 1)[1].split("function spotCols", 1)[0]
    assert "buildColChecks(" in spot_sync
    assert "SPOT_COL_ORDER" in spot_sync
    source = js.split("function spotSource", 1)[1].split("const SPOT_EXTRA", 1)[0]
    assert '"PSKR"' in source
    assert "show_psk" in js
    # Receive only: the Spotted code never calls a TX endpoint.
    spotted = js.split("function spotSpotter", 1)[1].split("function clusterReadForm", 1)[0]
    assert "/api/tx" not in spotted
    # A null dB (cluster or PSKR "hears us" row) draws "-", not "+null".
    assert 'c.snr==null?"-"' in ros


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
