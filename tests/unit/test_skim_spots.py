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

"""WS Skim decodes become receive-only SKIM spots, never a WSJT-X instance."""

import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wims.engine.contest_rules import load_builtin  # noqa: E402
from wims.integrations.cluster.client import ClusterService  # noqa: E402
from wims.integrations.cluster.parse import parse_spot  # noqa: E402
from wims.integrations.cluster.settings import sanitize  # noqa: E402
from wims.integrations.cluster.skim import (  # noqa: E402
    DEFAULT_SKIM_IDS, SkimFeed, clean_skim_ids, id_matches, id_patterns,
    receiver_label,
)
from wims.integrations.cluster.view import OnAir, SpotBook, build_list  # noqa: E402
from wims.state.logstore import LogStore  # noqa: E402
from wims.udp import encode as E  # noqa: E402
from wims.udp import messages as M  # noqa: E402

HOME = "FN42EV"
NOW = 1_700_000_000.0
SKIM_ID = "SKIM-FLEX-6m-FT8"


def _status(mid, dial, mode="FT8", config="Default", de_call="WA1HCO"):
    return M.parse(E.build_status(mid, dial, mode=mode, de_call=de_call,
                                  de_grid="FN42EV", config_name=config))


def _decode(mid, df, text, snr=-5, mode="~", low=False):
    return M.parse(E.build_decode(mid, time_ms=0, snr=snr, delta_time=0.1,
                                  delta_frequency=df, message=text, mode=mode,
                                  low_confidence=low))


def _log():
    log = LogStore(":memory:")
    log.rules = load_builtin("arrl-vhf")
    return log


# -- ids ---------------------------------------------------------------------- #

def test_default_ids_match_ws_skim_and_not_wsjtx():
    pats = id_patterns("")
    assert clean_skim_ids("") == DEFAULT_SKIM_IDS
    assert id_matches("SKIM-FLEX-40m-FT8-7074", pats)
    assert id_matches("skim-b210-2m-ft8", pats)
    assert id_matches("WS-Skim Flex", pats)
    assert not id_matches("WSJT-X - flex", pats)
    assert not id_matches("WSJT-X", pats)
    assert not id_matches("", pats)


def test_setup_ids_are_cleaned_and_exact_names_work():
    assert clean_skim_ids("SKIM-*, MySkimmer ,<bad>") == "SKIM-*, MySkimmer"
    assert id_matches("MYSKIMMER", id_patterns("MySkimmer"))
    assert not id_matches("MySkimmer2", id_patterns("MySkimmer"))
    assert sanitize({"skim_ids": ""})["skim_ids"] == DEFAULT_SKIM_IDS
    assert sanitize({})["show_skim"] is True


def test_receiver_label_from_id():
    assert receiver_label("SKIM-FLEX-40m-FT8-7075") == "FLEX"
    assert receiver_label("SKIM-B210-2m-FT8") == "B210"
    assert receiver_label("Other") == "Other"


def test_ws_skim_marker_in_config_name():
    feed = SkimFeed()
    msg = _status("Basement RX", 50_313_000, config="WS-Skim")
    assert feed.is_skim(msg, id_patterns(""))
    # Later datagrams from that id stay a skimmer without the marker.
    assert feed.is_skim(_decode("Basement RX", 1000, "CQ K1ABC FN42"), [])


# -- RF and parsing ----------------------------------------------------------- #

def test_true_rf_is_section_dial_plus_delta():
    feed = SkimFeed()
    # Captured live from WS Skim (Nov 18): section dial 7073779 + 422 Hz.
    mid = "SKIM-FLEX-40m-FT8-7074"
    assert feed.observe(_status(mid, 7_073_779), NOW) is None
    hit = feed.observe(_decode(mid, 422, "CQ K0WLY DN40"), NOW)
    assert hit is not None
    assert hit.freq_hz == 7_074_201
    assert hit.band == "40m"
    assert hit.mode == "FT8"
    assert hit.call == "K0WLY"
    assert hit.grid == "DN40"
    assert hit.receiver == "FLEX"


def test_decode_before_status_waits():
    feed = SkimFeed()
    assert feed.observe(_decode(SKIM_ID, 1000, "CQ K1ABC FN42"), NOW) is None
    assert feed.no_dial == 1


def test_skips_low_confidence_hashed_and_own_call():
    feed = SkimFeed()
    feed.observe(_status(SKIM_ID, 50_313_000), NOW)
    assert feed.observe(_decode(SKIM_ID, 900, "CQ K1ABC FN42", low=True), NOW) is None
    assert feed.observe(_decode(SKIM_ID, 900, "W1AW <...> -10"), NOW) is None
    # Status de_call is our own call: our own transmission is not a spot.
    assert feed.observe(_decode(SKIM_ID, 900, "CQ WA1HCO FN42"), NOW) is None
    assert feed.observe(_decode(SKIM_ID, 900, "K1ABC WA1HCO R-05"), NOW) is None


def test_grid_from_cq_fills_later_lines():
    feed = SkimFeed()
    feed.observe(_status(SKIM_ID, 50_313_000), NOW)
    feed.observe(_decode(SKIM_ID, 900, "CQ K1ABC FN31"), NOW)
    hit = feed.observe(_decode(SKIM_ID, 905, "W9XYZ K1ABC -07"), NOW + 15)
    assert hit.call == "K1ABC"
    assert hit.grid == "FN31"


def test_close_drops_receiver_and_snapshot_words():
    feed = SkimFeed()
    feed.observe(_status(SKIM_ID, 50_313_000), NOW, src_ip="192.168.1.181")
    feed.observe(_decode(SKIM_ID, 900, "CQ K1ABC FN31"), NOW)
    snap = feed.snapshot(NOW + 1)
    assert snap["receivers"][0]["label"] == "FLEX"
    assert "6m" in snap["line"] and "192.168.1.181" in snap["line"]
    assert "never worked" in snap["line"]
    feed.observe(M.parse(E._Writer(2, 6, SKIM_ID).bytes()), NOW + 2)
    assert feed.snapshot(NOW + 3)["receivers"] == []


# -- Spotted list ------------------------------------------------------------- #

def _list(book, *, show_skim=True, show_cluster=True, opening_for=None, now=NOW):
    log = _log()
    return build_list(
        book.rows(), now=now, ttl_s=15 * 60, our_grid=HOME, allowed=None,
        operations=[OnAir("6m", "FT8")],
        classify=lambda call, band, grid, mode: log.classify(call, band, grid, mode),
        show_skim=show_skim, show_cluster=show_cluster, opening_for=opening_for,
    )


def test_cluster_spot_heard_by_skim_upgrades_to_skim():
    book = SpotBook()
    book.add(parse_spot("DX de K1TTT: 50313.0 K1ABC FN31 FT8 1734Z"), NOW)
    book.add_skim(call="K1ABC", band="6m", mode="FT8", freq_hz=50_314_210,
                  grid=None, receiver="FLEX", snr=-7, now=NOW + 5)
    rows, _ = _list(book, now=NOW + 6)
    assert len(rows) == 1
    row = rows[0]
    assert row["source_tag"] == "SKIM"
    assert row["heard_here"] is True
    assert row["also_cluster"] is True
    assert row["freq_hz"] == 50_314_210
    assert row["dim"] is False
    assert "Skim FLEX" in row["spotter_calls"]
    # A later cluster line does not overwrite Skim's decoded frequency.
    book.add(parse_spot("DX de K2XX: 50313.0 K1ABC FN31 FT8 1736Z"), NOW + 10)
    rows, _ = _list(book, now=NOW + 11)
    assert rows[0]["freq_hz"] == 50_314_210


def test_heard_here_sorts_first_and_cluster_only_can_dim():
    book = SpotBook()
    book.add(parse_spot("DX de K1TTT: 50313.0 W2XYZ FN20 FT8 1734Z"), NOW)
    book.add_skim(call="K1ABC", band="6m", mode="FT8", freq_hz=50_314_000,
                  grid="FN31", receiver="FLEX", snr=-20, now=NOW)

    def closed(_band, _az):
        return {"likely": False, "reason": "No reports", "psk_count": 0,
                "local_count": 0}

    rows, _ = _list(book, opening_for=closed)
    assert [row["call"] for row in rows] == ["K1ABC", "W2XYZ"]
    assert rows[0]["source_tag"] == "SKIM" and rows[0]["dim"] is False
    assert rows[1]["source_tag"] == "CLU" and rows[1]["dim"] is True


def test_skim_and_cluster_checkboxes():
    book = SpotBook()
    book.add(parse_spot("DX de K1TTT: 50313.0 W2XYZ FN20 FT8 1734Z"), NOW)
    book.add(parse_spot("DX de K1TTT: 50313.0 K1ABC FN31 FT8 1734Z"), NOW)
    book.add_skim(call="K1ABC", band="6m", mode="FT8", freq_hz=50_314_000,
                  grid="FN31", receiver="FLEX", snr=-3, now=NOW)
    rows, _ = _list(book, show_skim=False)
    # K1ABC falls back to its cluster spot, as CLU.
    assert {(r["call"], r["source_tag"]) for r in rows} == {
        ("W2XYZ", "CLU"), ("K1ABC", "CLU")}
    rows, _ = _list(book, show_cluster=False)
    assert [(r["call"], r["source_tag"]) for r in rows] == [("K1ABC", "SKIM")]


def test_skim_rows_age_out_like_cluster_spots():
    book = SpotBook()
    book.add_skim(call="K1ABC", band="6m", mode="FT8", freq_hz=50_314_000,
                  grid="FN31", receiver="FLEX", snr=-3, now=NOW)
    assert _list(book, now=NOW + 14 * 60)[0]
    book.prune(NOW + 16 * 60)
    assert book.rows() == []


def test_skim_spot_skips_band_range_gate():
    # 2m from FN42 to EM12 is far past the usual 2m range, but Skim heard it.
    book = SpotBook()
    book.add_skim(call="W5ABC", band="2m", mode="FT8", freq_hz=144_175_000,
                  grid="EM12", receiver="B210", snr=-18, now=NOW)
    book.add(parse_spot("DX de W5XX: 144174.0 W5DEF EM12 FT8 1734Z"), NOW)
    rows, drops = _list(book)
    assert [row["call"] for row in rows] == ["W5ABC"]
    assert drops["range"] == 1


def test_service_lists_skim_with_cluster_off_and_counts_band_open():
    with tempfile.TemporaryDirectory() as tmp:
        svc = ClusterService(path=Path(tmp) / "cluster.json")
        svc.update({"enabled": False, "station_grid": HOME})
        feed = SkimFeed()
        feed.observe(_status(SKIM_ID, 50_313_000), NOW)
        hit = feed.observe(_decode(SKIM_ID, 1200, "CQ K1ABC FN31"), NOW)
        svc.add_skim(hit, NOW)
        log = _log()
        view = svc.public(
            now=NOW + 1, callsign="WA1HCO", our_grid=HOME, operations=[],
            classify=lambda c, b, g, m: log.classify(c, b, g, m),
            skim_decodes=feed.recent_decodes(NOW + 1),
            skim=feed.snapshot(NOW + 1),
        )
        assert [row["call"] for row in view["rows"]] == ["K1ABC"]
        assert view["rows"][0]["source_tag"] == "SKIM"
        assert view["skim_count"] == 1
        assert view["show_skim"] is True
        assert view["skim_line"].startswith("Skim: FLEX on 6m")
        assert feed.recent_decodes(NOW + 1)[0]["source"] == "skim"


def test_skim_decodes_count_as_local_band_open_evidence():
    from wims.integrations.cluster.psk import hearings_from_decodes
    feed = SkimFeed()
    feed.observe(_status(SKIM_ID, 50_313_000), NOW)
    feed.observe(_decode(SKIM_ID, 1200, "CQ W5ABC EM12"), NOW)
    samples = hearings_from_decodes(feed.recent_decodes(NOW), home_grid=HOME, now=NOW)
    assert len(samples) == 1
    assert samples[0].band == "6m" and samples[0].source == "local"


# -- site server: never a WSJT-X instance ------------------------------------- #

def test_live_fleet_keeps_skim_out_of_fleet_and_roster():
    from wims.server.app import LiveFleet

    live = LiveFleet()
    live._cluster = ClusterService(path=Path(tempfile.mkdtemp()) / "cluster.json")
    now = time.time()
    assert live.observe_skim(_status(SKIM_ID, 50_313_000), now, "127.0.0.1")
    assert live.observe_skim(_decode(SKIM_ID, 1200, "CQ K1ABC FN31"), now, "127.0.0.1")
    assert SKIM_ID not in live._tracker.nodes
    assert SKIM_ID not in live._maps
    assert not any(key[0] == SKIM_ID for key in live._roster._entries)
    assert len(live._decodes) == 0
    rows = live._cluster._book.rows()
    assert [(r.call, r.band) for r in rows] == [("K1ABC", "6m")]
    # A real WSJT-X id is not taken.
    assert not live.observe_skim(_status("WSJT-X - flex", 50_313_000), now, "127.0.0.1")


def test_skim_id_already_in_fleet_is_removed():
    from wims.server.app import LiveFleet

    live = LiveFleet()
    live._cluster = ClusterService(path=Path(tempfile.mkdtemp()) / "cluster.json")
    now = time.time()
    live.observe_wsjtx(_status("Basement RX", 50_313_000), now, "127.0.0.1", 50000)
    assert "Basement RX" in live._tracker.nodes
    live._cluster.update({"skim_ids": "SKIM-*, Basement RX"})
    assert live.observe_skim(_status("Basement RX", 50_313_000), now, "127.0.0.1")
    assert "Basement RX" not in live._tracker.nodes


def test_ingest_loop_does_not_forward_skim_to_gridtracker():
    from wims.server.app import LiveFleet, ingest_loop

    live = LiveFleet()
    live._cluster = ClusterService(path=Path(tempfile.mkdtemp()) / "cluster.json")

    class GT:
        sock = None

        def __init__(self):
            self.sent = []

        def forward_wsjt(self, data, now=None):
            self.sent.append(data)

    gt = GT()
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    gen = live._ingest_generation
    t = threading.Thread(target=ingest_loop, args=(live, [rx], None, gt),
                         kwargs={"generation": gen}, daemon=True)
    t.start()
    try:
        tx.sendto(E.build_status(SKIM_ID, 50_313_000), rx.getsockname())
        tx.sendto(E.build_decode(SKIM_ID, time_ms=0, snr=-3, delta_time=0.1,
                                 delta_frequency=1200, message="CQ K1ABC FN31"),
                  rx.getsockname())
        tx.sendto(E.build_status("WSJT-X - flex", 50_313_000), rx.getsockname())
        deadline = time.time() + 3
        while time.time() < deadline and (live.skim_pkts < 2 or not gt.sent):
            time.sleep(0.05)
    finally:
        live._ingest_generation += 1
        t.join(timeout=3)
        rx.close()
        tx.close()
    assert live.skim_pkts == 2
    assert len(gt.sent) == 1  # only the real WSJT-X Status
    assert SKIM_ID not in live._tracker.nodes
    assert "WSJT-X - flex" in live._tracker.nodes
