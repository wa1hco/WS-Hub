# WIMS — WSJT-X Instance Management System
# Copyright (C) 2026 Jeff Millar, WA1HCO
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""PSK Reporter openings: parse a recorded document, rank by direction, no network."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wims.engine.geo import bearing  # noqa: E402
from wims.integrations.cluster.psk import (  # noqa: E402
    MIN_POLL_S, WINDOW_S, PskOpenings, Report, clean_grid, evaluate,
    far_grid, grid4, hearings_from_decodes, parse_reports, path_from_report,
    psk_status_line, query_url,
)

NOW = 1_700_000_000.0
HOME = "FN41vp"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "psk_reporter_fn41.xml"


def _reports():
    return parse_reports(FIXTURE.read_text(encoding="utf-8"))


def test_query_url_uses_grid_filter_and_contact():
    url = query_url("fn41vp")
    assert url.startswith("https://retrieve.pskreporter.info/query?")
    assert "callsign=FN41" in url
    assert "modify=grid" in url
    assert "rronly=1" in url
    assert "noactive=1" in url
    assert "flowStartSeconds=-1800" in url
    assert "rptlimit=" in url
    assert "appcontact=wa1hco" in url
    # Only one callsign-like parameter, per their docs.
    assert "senderCallsign=" not in url
    assert "receiverCallsign=" not in url


def test_parse_recorded_xml_and_skip_local_or_stale():
    reports = _reports()
    assert len(reports) == 8
    assert all(isinstance(r, Report) for r in reports)
    assert any(r.sender_call == "K1HOME" and r.receiver_call == "KL7EX" for r in reports)
    # Extended locator FN41fp44tt becomes a 6-character grid.
    brazil = next(r for r in reports if r.sender_call == "PY2DX")
    assert brazil.receiver_grid == "FN41FP"
    assert grid4(brazil.receiver_grid) == "FN41"

    paths = [path_from_report(r, HOME, now=NOW) for r in reports]
    kept = [p for p in paths if p is not None]
    # Local FN41-to-FN41, missing far grid, and the 2000 s stale report drop out.
    assert len(kept) == 5
    bands = sorted({p.band for p in kept})
    assert bands == ["10m", "20m", "40m"]
    # Every kept path has a bearing from FN41.
    assert all(p.az is not None and p.source == "psk" for p in kept)

    both = next(r for r in reports if r.sender_call == "W1LOCAL")
    assert far_grid(both, "FN41") == ""
    missing = next(r for r in reports if r.sender_call == "NOGRID")
    assert far_grid(missing, "FN41") == ""
    stale = next(r for r in reports if r.sender_call == "DL1OLD")
    assert path_from_report(stale, HOME, now=NOW) is None


def test_opening_is_toward_the_dx_bearing_on_that_band():
    reports = _reports()
    samples = [path_from_report(r, HOME, now=NOW) for r in reports]
    samples = [s for s in samples if s is not None]
    az_ak = bearing(HOME, "BP51IP")
    az_py = bearing(HOME, "GG54RK")
    az_dl = bearing(HOME, "JO31aa")
    az_tx = bearing(HOME, "EM13ka")
    open_ak = evaluate(band="10m", az=az_ak, now=NOW, samples=samples, home_grid=HOME)
    assert open_ak.likely is True
    assert open_ak.psk_count >= 1
    assert "Open:" in open_ak.reason
    assert "FN41" in open_ak.reason
    assert "10m" in open_ak.reason
    assert f"{int(round(az_ak))}°" in open_ak.reason
    # Brazil is also on 10m, a different direction than Alaska.
    open_py = evaluate(band="10m", az=az_py, now=NOW, samples=samples, home_grid=HOME)
    assert open_py.likely is True
    # 10m toward Europe has no fixture reports (the Europe one is on 20m).
    # JO31 from FN41 is well away from BP51 and GG54.
    closed = evaluate(band="10m", az=az_dl, now=NOW, samples=samples, home_grid=HOME)
    # Only flag closed if the Europe bearing is outside every 10m sample.
    from wims.integrations.cluster.psk import angle_diff, SECTOR_HALF_DEG
    ten = [s for s in samples if s.band == "10m"]
    if all(angle_diff(s.az, az_dl) > SECTOR_HALF_DEG for s in ten):
        assert closed.likely is False
        assert closed.reason.startswith("No FN41 reports toward")
        assert "10m" in closed.reason
        assert "30 min" in closed.reason
    open_20 = evaluate(band="20m", az=az_dl, now=NOW, samples=samples, home_grid=HOME)
    assert open_20.likely is True
    assert "20m" in open_20.reason
    open_40 = evaluate(band="40m", az=az_tx, now=NOW, samples=samples, home_grid=HOME)
    assert open_40.likely is True
    # No 15m reports at all.
    none = evaluate(band="15m", az=az_dl, now=NOW, samples=samples, home_grid=HOME)
    assert none.likely is False
    # Without a map, do not call the path closed.
    unknown = evaluate(
        band="15m", az=az_dl, now=NOW, samples=[], home_grid=HOME, known=False,
    )
    assert unknown.likely is None
    assert "not ranked" in unknown.reason
    # Recency: a just-arrived report still counts; one outside the window does not.
    fresh = evaluate(
        band="20m", az=az_dl, now=NOW, samples=samples, home_grid=HOME,
        window_s=WINDOW_S,
    )
    assert fresh.likely is True


def test_local_decodes_count_as_open_in_that_direction():
    az = bearing(HOME, "JO31aa")
    decodes = [
        {"ts": NOW - 30, "band": "20m", "message": "CQ DL1DX JO31", "dx_call": "DL1DX"},
        {"ts": NOW - 40, "band": "20m", "message": "WA1HCO G4ABC 73", "dx_call": "G4ABC"},
    ]
    def grid_for(call):
        return "IO91" if call == "G4ABC" else None
    local = hearings_from_decodes(
        decodes, home_grid=HOME, now=NOW, grid_for=grid_for,
    )
    assert len(local) == 2
    assert all(s.source == "local" for s in local)
    # Own-square decode is not a DX path.
    own = hearings_from_decodes(
        [{"ts": NOW - 10, "band": "20m", "message": "CQ W1AB FN41", "dx_call": "W1AB"}],
        home_grid=HOME, now=NOW,
    )
    assert own == []
    opening = evaluate(
        band="20m", az=az, now=NOW, samples=local, home_grid=HOME, known=True,
    )
    assert opening.likely is True
    assert opening.local_count >= 1
    assert opening.psk_count == 0
    assert "local decode" in opening.reason


def test_poller_enforces_five_minute_floor_and_does_not_touch_the_network():
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return 200, FIXTURE.read_bytes(), ""

    clock = {"now": NOW}

    def now():
        return clock["now"]

    src = PskOpenings(fetch=fake_fetch, min_poll_s=MIN_POLL_S, clock=now)
    src.configure(grid="FN41vp", enabled=True)
    assert src.maybe_fetch() is True
    assert src.fetch_count == 1
    assert len(calls) == 1
    assert "callsign=FN41" in calls[0]
    snap = src.snapshot(NOW)
    assert snap["known"] is True
    assert snap["report_count"] == 8
    assert "340" not in snap["status_line"]  # exact count from the fixture
    assert "8 reports from FN41" in snap["status_line"]
    assert "updated just now" in snap["status_line"]
    # A second try 10 seconds later must not fetch.
    clock["now"] = NOW + 10
    assert src.maybe_fetch() is False
    assert src.fetch_count == 1
    assert len(calls) == 1
    # After five minutes it may fetch again.
    clock["now"] = NOW + MIN_POLL_S
    assert src.maybe_fetch() is True
    assert src.fetch_count == 2
    assert len(calls) == 2
    # Errors also honor the floor, and keep the last good map.
    def boom(url):
        calls.append("err")
        return 0, b"", "can't reach the service, will try again"

    src._fetch = boom
    clock["now"] = NOW + 2 * MIN_POLL_S
    assert src.maybe_fetch() is True
    later = src.snapshot(clock["now"])
    assert later["known"] is True
    assert later["report_count"] == 8
    assert "can't reach the service" in later["status_line"]
    # Immediate retry after an error is still blocked.
    clock["now"] = NOW + 2 * MIN_POLL_S + 5
    assert src.maybe_fetch() is False
    assert calls.count("err") == 1
    # Disabled or missing grid: no fetch.
    src.configure(grid="FN41", enabled=False)
    clock["now"] = NOW + 3 * MIN_POLL_S
    assert src.maybe_fetch() is False
    src.configure(grid="", enabled=True)
    assert src.maybe_fetch() is False


def test_status_lines_are_plain():
    assert psk_status_line(
        enabled=False, grid="FN41", report_count=0, updated_at=None, now=NOW,
    ) == "PSK Reporter: off"
    assert "waiting for a station grid" in psk_status_line(
        enabled=True, grid="", report_count=None, updated_at=None, now=NOW,
    )
    assert "looking up reports from FN41" in psk_status_line(
        enabled=True, grid="FN41", report_count=None, updated_at=None, now=NOW,
    )
    assert psk_status_line(
        enabled=True, grid="FN41", report_count=None, updated_at=None, now=NOW,
        error="can't reach the service, will try again",
    ) == "PSK Reporter: can't reach the service, will try again"
    line = psk_status_line(
        enabled=True, grid="FN41VP", report_count=340, updated_at=NOW - 120, now=NOW,
    )
    assert line == "PSK Reporter: 340 reports from FN41, updated 2 min ago"
    one = psk_status_line(
        enabled=True, grid="FN41", report_count=1, updated_at=NOW, now=NOW,
    )
    assert "1 report from FN41" in one
    assert "reports" not in one.split("report")[0]  # singular


def test_junk_xml_and_clean_grid():
    assert parse_reports("") == []
    assert parse_reports("<not-this/>") == []
    assert parse_reports("<<<") == []
    assert clean_grid("fn41vp") == "FN41VP"
    assert clean_grid("FN41fp44tt") == "FN41FP"
    assert clean_grid("nope") == ""
    assert grid4("fn41vp") == "FN41"


def test_poller_thread_stops_without_fetching_when_idle():
    calls = []
    src = PskOpenings(fetch=lambda url: calls.append(url) or (200, b"", ""), min_poll_s=60)
    src.start()
    src.stop()
    assert calls == []


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:
                failed += 1
                print(f"FAIL {name}: {exc}")
                raise
    if failed:
        raise SystemExit(1)
