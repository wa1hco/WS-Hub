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

"""Test traffic never reaches a live site server; IF-seat Setup safeguards.

validate.sh, the emulator, and the interlock bench use a private multicast
group and ports, so a fake TRL-432 / VA2WXY decode can never land in a real
roster again. Setup's IF-reporting seats list warns when a seat marked IF
is tuned outside the 21/28 MHz IFs, and does not redraw under the mouse.
"""

import ipaddress
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools" / "testbed"))

import testnet  # noqa: E402
from wims.core.bands import if_dial_outside, in_if_range  # noqa: E402

VALIDATE = (ROOT / "tools/dev/validate.sh").read_text(encoding="utf-8")
EMULATOR = ROOT / "tools/testbed/simulators/emulator.py"
BENCH = (ROOT / "tools/testbed/interlock_bench.py").read_text(encoding="utf-8")
SERVER = (ROOT / "src/wims/server/app.py").read_text(encoding="utf-8")


def test_test_addresses_are_private_and_not_live():
    group = ipaddress.IPv4Address(testnet.TEST_GROUP)
    assert group.is_multicast
    assert group in ipaddress.IPv4Network("239.255.0.0/16")  # site-local scope
    assert testnet.TEST_GROUP != testnet.LIVE_GROUP
    for port in (testnet.TEST_PORT, testnet.TEST_N1MM_PORT):
        assert port not in testnet.LIVE_PORTS
    assert not testnet.reaches_live(testnet.TEST_GROUP, testnet.TEST_PORT)
    assert testnet.reaches_live("224.0.0.73", testnet.TEST_PORT)
    assert testnet.reaches_live(testnet.TEST_GROUP, 2237)
    # The real app's defaults are unchanged.
    assert 'ap.add_argument("--group", default="224.0.0.73"' in SERVER
    assert 'FLEET_WSJT_PORTS = "2237"' in SERVER
    assert 'ap.add_argument("--n1mm-port", type=int, default=12060)' in SERVER


def _shell_value(name):
    m = re.search(rf'^{name}="([^"]+)"', VALIDATE, re.M)
    assert m, name
    return m.group(1)


def test_validate_sh_uses_the_private_addresses_for_server_and_emulator():
    assert _shell_value("TEST_GROUP") == testnet.TEST_GROUP
    assert _shell_value("TEST_PORT") == str(testnet.TEST_PORT)
    assert _shell_value("TEST_N1MM_PORT") == str(testnet.TEST_N1MM_PORT)
    live = VALIDATE.split('hdr "4. Live end-to-end', 1)[1].split("# wait for HTTP", 1)[0]
    server = live.split("src/wims/server/app.py", 1)[1].split("SRV_PID=$!", 1)[0]
    for flag in ('--group "$TEST_GROUP"', '--ports "$TEST_PORT"',
                 '--n1mm-group "$TEST_GROUP"', '--n1mm-port "$TEST_N1MM_PORT"',
                 "--no-presence", "--no-tx"):
        assert flag in server, flag
    emu = live.split("emulator.py", 1)[1].split("EMU_PID=$!", 1)[0]
    assert '--group "$TEST_GROUP"' in emu and '--port "$TEST_PORT"' in emu
    assert "--allow-live" not in VALIDATE


def test_interlock_bench_uses_the_private_addresses():
    assert "GROUP, PORT, IFACE = TEST_GROUP, TEST_PORT" in BENCH
    assert '"--group", GROUP, "--port", str(PORT)' in BENCH
    assert '"224.0.0.73", 2237' not in BENCH


def test_emulator_defaults_private_and_refuses_live_without_flag():
    src = EMULATOR.read_text(encoding="utf-8")
    assert 'ap.add_argument("--group", default=TEST_GROUP' in src
    assert 'ap.add_argument("--port", type=int, default=TEST_PORT' in src
    # The refusal happens in argument checks, before any socket opens, so
    # this test sends nothing.
    for extra in (["--port", "2237"], ["--group", "224.0.0.73"], ["--port", "12060"]):
        proc = subprocess.run(
            [sys.executable, str(EMULATOR), "--instances", "T-6M:50313000", *extra],
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 2, (extra, proc.stdout, proc.stderr)
        assert "live site server" in proc.stderr


def test_if_range_helpers():
    assert in_if_range(21_200_000) and in_if_range(28_074_000)
    assert not in_if_range(7_074_000) and not in_if_range(144_174_000)
    assert not in_if_range(29_700_000)
    assert if_dial_outside(7_074_000)
    assert if_dial_outside(144_170_000)
    assert if_dial_outside(432_174_000)
    assert not if_dial_outside(28_074_000)
    assert not if_dial_outside(0)
    assert not if_dial_outside(None)


def test_instance_snapshot_flags_an_if_seat_on_an_rf_dial():
    from wims.discovery.fleet import NodeState
    from wims.server.state import _instance

    old = os.environ.get("WIMS_IF_SEATS_FILE")
    oldx = os.environ.pop("WIMS_TRANSVERTER", None)
    with tempfile.TemporaryDirectory() as d:
        try:
            os.environ["WIMS_IF_SEATS_FILE"] = str(Path(d) / "if_seats.json")
            from wims.core.if_seats import set_reports_if
            set_reports_if("WSJT-X - flex", True)
            now = time.time()

            def node(mid, raw, rf):
                n = NodeState(id=mid, first_seen=now)
                n.dial_raw_hz = raw
                n.dial_hz = rf
                return n

            flex = _instance(node("WSJT-X - flex", 7_074_000, 7_074_000), now)
            assert flex["if_configured"] is True
            assert flex["dial_raw_hz"] == 7_074_000
            assert flex["if_dial_outside"] is True
            ten = _instance(node("WSJT-X - flex", 28_074_000, 432_074_000), now)
            assert ten["if_dial_outside"] is False
            other = _instance(node("WSJT-X - IC9700", 144_170_000, 144_170_000), now)
            assert other["if_configured"] is False
            assert other["if_dial_outside"] is False
            unknown = _instance(node("WSJT-X - flex", 0, 0), now)
            assert unknown["if_dial_outside"] is False
        finally:
            if old is None:
                os.environ.pop("WIMS_IF_SEATS_FILE", None)
            else:
                os.environ["WIMS_IF_SEATS_FILE"] = old
            if oldx is not None:
                os.environ["WIMS_TRANSVERTER"] = oldx


def test_fleet_keeps_the_raw_dial_beside_the_mapped_one():
    from wims.discovery.fleet import FleetTracker
    from wims.udp import encode as E
    from wims.udp import messages as M

    old = os.environ.get("WIMS_IF_SEATS_FILE")
    with tempfile.TemporaryDirectory() as d:
        try:
            os.environ["WIMS_IF_SEATS_FILE"] = str(Path(d) / "if_seats.json")
            from wims.core.if_seats import set_reports_if
            set_reports_if("ROY-432", True)
            tr = FleetTracker()
            tr.observe(M.parse(E.build_status("ROY-432", 28_074_000, mode="FT8")), time.time())
            n = tr.nodes["ROY-432"]
            assert n.dial_raw_hz == 28_074_000
            assert n.dial_hz == 432_074_000
        finally:
            if old is None:
                os.environ.pop("WIMS_IF_SEATS_FILE", None)
            else:
                os.environ["WIMS_IF_SEATS_FILE"] = old


def test_setup_if_seats_list_waits_for_the_mouse_and_warns():
    js = (ROOT / "src/wims/server/static/wims.js").read_text(encoding="utf-8")
    render = js.split("function renderIfSeats", 1)[1].split("function renderSetupExtras", 1)[0]
    assert "!ifSeatsBusy(box)" in render
    busy = render.split("function ifSeatsBusy", 1)[1]
    assert ':hover' in busy and ':focus-within' in busy
    assert "ifDialWarning(n)" in render
    warn = render.split("function ifDialWarning", 1)[1]
    assert "if_dial_outside" in warn and "dial_raw_hz" in warn
    assert "21–24" in warn and "28–29.7" in warn
    # Display only: the warning never posts or unticks anything.
    assert "fetch(" not in warn and "postIfSeat" not in warn


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
