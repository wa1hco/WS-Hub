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


"""Ingest keeps up with a busy band plus WS Skim.

Regression for 1.0.33 on a site server with a month of heard grids
(about 28,000 calls): every decode that carried a grid rewrote the whole
heard-grid file under the fleet lock (about 150 ms), and every spot-list
lookup swept all 28,000 calls (about 2 ms each, 900 per refresh, so about
2 s per refresh). The UDP reader could not keep up and the OS dropped
datagrams. These tests replay that load and pin the cheap paths.
"""

import random
import socket
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import wims.state.heard_grids as HG  # noqa: E402
from wims.state.heard_grids import HeardGrids  # noqa: E402
from wims.udp import encode as E  # noqa: E402
from wims.udp import messages as M  # noqa: E402

NOW = 1_795_000_000.0
_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _call(rng):
    prefix = rng.choice(["K", "W", "N", "VE", "EA", "DL", "JA", "G"])
    tail = "".join(rng.choice(_LETTERS) for _ in range(rng.randint(2, 3)))
    return f"{prefix}{rng.randint(0, 9)}{tail}"


def _grid(rng):
    return (rng.choice("DEFIJKLNOP") + rng.choice("JKLMNO")
            + str(rng.randint(0, 9)) + str(rng.randint(0, 9)))


def _big_store(path, calls=20_000):
    """A store like a month of 40m FT8: many calls, a few squares each."""
    rng = random.Random(7)
    store = HeardGrids(path)
    with store._lock:
        for i in range(calls):
            entry = HG._CallMemory()
            when = NOW - rng.randint(0, 25 * 24 * 3600)
            entry.hear(_grid(rng), "40m", when)
            if i % 3 == 0:
                entry.hear(_grid(rng), "20m", when + 60)
            store._calls[f"{_call(rng)}{i}"] = entry
    return store


def test_note_marks_dirty_and_does_not_write_at_once():
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "heard_grids.json"
        store = HeardGrids(path)
        assert store.note("CX4BAN", "GF15", "20m", NOW)
        assert not path.exists()
        assert store.pending()
        assert store.flush() is True
        assert path.is_file()
        assert not store.pending()
        assert store.flush() is False   # nothing new
        assert HeardGrids(path).current("CX4BAN", NOW + 5) == "GF15"


def test_timer_writes_soon_after_a_hearing():
    old = HG.SAVE_DELAY_S
    HG.SAVE_DELAY_S = 0.05
    try:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "heard_grids.json"
            store = HeardGrids(path)
            store.note("W1AW", "FN31", "6m", NOW)
            store.note("K1TTT", "FN32", "6m", NOW + 1)
            deadline = time.monotonic() + 5.0
            while store.pending() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert not store.pending()
            again = HeardGrids(path)
            assert again.current("W1AW", NOW + 2) == "FN31"
            assert again.current("K1TTT", NOW + 2) == "FN32"
    finally:
        HG.SAVE_DELAY_S = old


def test_memory_only_store_never_starts_a_timer():
    store = HeardGrids()
    store.note("W1AW", "FN31", "6m", NOW)
    assert store._timer is None
    assert store.flush() is False


def test_reads_do_not_sweep_the_whole_store_but_still_expire_the_call():
    store = HeardGrids()
    store.note("OLD1", "FN31", "6m", NOW)
    store.note("NEW1", "FN42", "6m", NOW + HG.MAX_AGE_S)
    sweeps = []
    real = store._expire
    store._expire = lambda when: (sweeps.append(when), real(when))[1]
    later = NOW + HG.MAX_AGE_S + 10
    for _ in range(50):
        assert store.lookup("OLD1", later).grid is None
        assert store.current("OLD1", later) is None
        assert store.is_rover("OLD1", later) is False
        assert store.lookup("NEW1", later).grid == "FN42"
    assert sweeps == []
    assert "OLD1" not in store._calls


def test_notes_sweep_at_most_once_a_minute():
    store = HeardGrids()
    sweeps = []
    real = store._expire
    store._expire = lambda when: (sweeps.append(when), real(when))[1]
    for i in range(600):
        store.note(f"K{i}AA", "FN42", "40m", NOW + i * 0.5)   # 5 minutes
    assert 1 <= len(sweeps) <= 6


def test_busy_band_plus_skim_replay_keeps_up():
    """Five minutes of a 40m FT8 seat (89 decodes a period) and WS Skim
    (16 ids, 45 decodes a period) against a month-sized heard store."""
    from wims.server.app import LiveFleet

    rng = random.Random(11)
    stations = [(_call(rng), _grid(rng)) for _ in range(400)]
    skim_ids = [f"SKIM-FLEX-40m-FT8-{7074 + i * 3}" for i in range(15)]
    skim_ids.append("SKIM-FLEX-40m-FT8")
    seat = "WSJT-X - flex"

    def status(mid, dial):
        return M.parse(E.build_status(mid, dial, mode="FT8", de_call="WA1HCO",
                                      de_grid="FN42EV", config_name="Default"))

    def decode(mid, text):
        return M.parse(E.build_decode(mid, time_ms=0, snr=-10, delta_time=0.1,
                                      delta_frequency=rng.randint(200, 2800),
                                      message=text, mode="~"))

    with tempfile.TemporaryDirectory() as folder:
        live = LiveFleet()
        live._heard = _big_store(Path(folder) / "heard_grids.json")
        msgs = 0
        t0 = time.perf_counter()
        for slot in range(20):
            now = NOW + slot * 15
            live.observe_wsjtx(status(seat, 7_074_000), now, "192.168.1.181", 44638)
            for k, sid in enumerate(skim_ids):
                m = status(sid, 7_074_000 + k * 3000)
                assert live.observe_skim(m, now, "127.0.0.1")
            for i in range(89):
                call, grid = rng.choice(stations)
                text = f"CQ {call} {grid}" if i % 2 else f"{_call(rng)} {call} {grid}"
                live.observe_wsjtx(decode(seat, text), now, "192.168.1.181", 44638)
            for i in range(45):
                call, grid = rng.choice(stations)
                text = f"CQ {call} {grid}" if i % 2 else f"{_call(rng)} {call} -12"
                assert live.observe_skim(decode(rng.choice(skim_ids), text),
                                         now, "127.0.0.1")
            msgs += 1 + len(skim_ids) + 89 + 45
        ingest_s = time.perf_counter() - t0
        t1 = time.perf_counter()
        snap = live.snapshot(NOW + 300)
        snap_s = time.perf_counter() - t1
        # Before the fix: about 70 ms per message and 2 s per snapshot on
        # a desktop PC. Limits are loose so a slow CI runner still passes.
        assert ingest_s / msgs < 0.005, f"{1000 * ingest_s / msgs:.2f} ms/msg"
        assert snap_s < 1.0, f"snapshot {snap_s:.2f} s"
        assert snap["roster"]["count"] > 0
        assert live._heard.pending()
        # Nothing was written on the ingest path; one flush writes it all.
        assert not (Path(folder) / "heard_grids.json").exists()
        assert live._heard.flush() is True


def test_listen_socket_asks_for_a_bigger_receive_buffer():
    from wims.udp.sink import open_socket
    plain = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        default = plain.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    finally:
        plain.close()
    sock = open_socket("127.0.0.1", 0, None)
    try:
        got = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    finally:
        sock.close()
    assert got >= default


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
