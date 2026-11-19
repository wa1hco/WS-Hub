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

"""Protected site-server takeover (1.0.32).

Default follow, who may host, the site password (hash only, wrong password
refused, rate limit), the handover API turning Enable Tx off and dropping
claims, an unreachable announcement not blocking a start, and two running
servers never stopping each other on their own.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "unit"))

from wims.discovery import presence as P  # noqa: E402
from wims.launcher import app as A  # noqa: E402
from wims.launcher import site_admin as SA  # noqa: E402
from wims.launcher.app import LauncherApp  # noqa: E402
from wims.launcher.assets import AGENT_SERVER, INTENT_SERVER  # noqa: E402
from wims.launcher.site_choice import (  # noqa: E402
    START_CONFIRM,
    STOP_CONFIRM,
    no_server_message,
    site_buttons,
    takeover_confirm_text,
)
from wims.server.app import LiveFleet, make_handler  # noqa: E402
from wims.udp import encode as E, messages as M  # noqa: E402

from test_site_server_discovery import _TOWER, _fake_app  # noqa: E402

MID = "RIG-6M"
CTRL_PORT = 54329


class _TmpConfig(unittest.TestCase):
    """Every test gets its own WS Hub settings folder."""

    def setUp(self) -> None:
        self._env = dict(os.environ)
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        os.environ["WIMS_SITE_ADMIN"] = str(tmp / "site_admin.json")
        os.environ["WIMS_SITE_SERVER_PID"] = str(tmp / "site-server.pid")
        patcher = mock.patch.object(A, "save_last_site_url", lambda *_a: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()


# --- site password and permission ------------------------------------------ #

class SiteAdminTests(_TmpConfig):
    def test_password_is_stored_only_as_a_salted_hash(self):
        ok, _ = SA.set_password("rig-shack-42")
        self.assertTrue(ok)
        text = SA.admin_path().read_text(encoding="utf-8")
        self.assertNotIn("rig-shack-42", text)
        rec = json.loads(text)["password"]
        self.assertEqual(rec["alg"], SA.PBKDF2_ALG)
        self.assertTrue(rec["salt"] and rec["hash"])
        self.assertTrue(SA.check_password("rig-shack-42"))
        self.assertFalse(SA.check_password("rig-shack-43"))
        # Same password twice gives two different salts.
        self.assertNotEqual(SA.hash_password("x1234")["salt"],
                            SA.hash_password("x1234")["salt"])

    def test_changing_the_password_needs_the_current_one(self):
        SA.set_password("first-pass")
        ok, msg = SA.set_password("second-pass", current_password="nope")
        self.assertFalse(ok)
        self.assertIn("not right", msg)
        self.assertTrue(SA.check_password("first-pass"))
        ok, _ = SA.set_password("second-pass", current_password="first-pass")
        self.assertTrue(ok)
        self.assertTrue(SA.check_password("second-pass"))

    def test_short_or_mismatched_password_is_refused(self):
        self.assertTrue(SA.password_problem("ab"))
        self.assertTrue(SA.password_problem("abcdef", "abcdeg"))
        self.assertEqual(SA.password_problem("abcdef", "abcdef"), "")

    def test_may_host_defaults_and_is_remembered(self):
        # Single PC (nothing remembered elsewhere): on.
        self.assertTrue(SA.may_host(last_site_is_other_pc=False))
        SA.admin_path().unlink()
        # Another seat whose site server is on another PC: off, and it stays
        # off even after this PC hears about hosting.
        self.assertFalse(SA.may_host(last_site_is_other_pc=True))
        self.assertFalse(SA.may_host(last_site_is_other_pc=False, hosting_now=True))
        SA.admin_path().unlink()
        # The PC already hosting: on.
        self.assertTrue(SA.may_host(last_site_is_other_pc=True, hosting_now=True))

    def test_failure_limiter_locks_after_repeated_failures(self):
        t = [0.0]
        lim = SA.FailureLimiter(max_failures=3, window_s=300, lockout_s=60,
                                clock=lambda: t[0])
        for _ in range(3):
            self.assertEqual(lim.locked_for(), 0.0)
            lim.failed()
        self.assertGreater(lim.locked_for(), 0.0)
        t[0] = 61.0
        self.assertEqual(lim.locked_for(), 0.0)
        lim.succeeded()
        self.assertEqual(lim.locked_for(), 0.0)

    def test_casual_operator_sees_no_site_server_controls(self):
        for reachable in (True, False):
            for two in (True, False):
                shown = site_buttons(
                    may_host=False, site_reachable=reachable, server_here=False,
                    two_servers=two, offer=True,
                )
                self.assertFalse(any(shown.values()), shown)

    def test_permitted_pc_controls(self):
        remote = site_buttons(may_host=True, site_reachable=True,
                              server_here=False, two_servers=False, offer=False)
        self.assertTrue(remote["take_over"])
        self.assertFalse(remote["start"] or remote["restart"] or remote["checkbox"])
        none = site_buttons(may_host=True, site_reachable=False,
                            server_here=False, two_servers=False, offer=True)
        self.assertTrue(none["start"])
        self.assertFalse(none["take_over"])
        here = site_buttons(may_host=True, site_reachable=True,
                            server_here=True, two_servers=True, offer=True)
        self.assertTrue(here["restart"] and here["take_over"] and here["follow_other"])

    def test_plain_messages(self):
        self.assertEqual(
            no_server_message("jeff-Precision-Tower-3420"),
            "No site server found. The last one was on jeff-Precision-Tower-3420. "
            "Ask the station owner to start it.",
        )
        self.assertEqual(
            takeover_confirm_text("dell-7540"),
            "The site server on dell-7540 will stop, and this PC will take over. "
            "Work claims and Enable TX will be cleared. Continue?",
        )
        self.assertIn("This affects every PC at the station.", STOP_CONFIRM)


# --- the handover API on the site server ----------------------------------- #

class _FakeTx:
    def __init__(self):
        self.dest = ("127.0.0.1", 2237)
        self.sent = []

    def reply(self, inst, decode, *, modifiers=0, dests=None):
        self.sent.append(("reply", inst))

    def configure(self, inst, **_k):
        self.sent.append(("configure", inst))

    def configure_grid(self, inst, dx_grid, **_k):
        self.sent.append(("grid", inst))

    def halt(self, inst, *, auto_only=False, dests=None):
        self.sent.append(("halt", inst, auto_only))


class _FakeAnnouncer:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True

    def status_dict(self):
        return {"announcing": not self.stopped, "listening": True, "hints": [],
                "two_servers": []}


def _worked_fleet():
    tx = _FakeTx()
    live = LiveFleet(tx_controller=tx)
    live.set_seat_profile_path(Path(tempfile.mkdtemp()) / "seat-profile.json")
    now = time.time()
    live.observe_wsjtx(M.parse(E.build_status(MID, 50313000, mode="FT8",
                       de_call="WA1HCO", de_grid="FN41")), now, "127.0.0.1", CTRL_PORT)
    live.observe_wsjtx(M.parse(E.build_decode(MID, time_ms=1000, snr=-8, delta_time=0.2,
                       delta_frequency=1500, message="CQ K1ABC FN31")),
                       now, "127.0.0.1", CTRL_PORT)
    row_id = live.snapshot(now)["roster"]["candidates"][0]["id"]
    r = live.work_station(row_id, dashboard_id="dash-a")
    assert r["ok"], r
    tx.sent.clear()
    return live, tx


class HandoverApiTests(_TmpConfig):
    def _serve(self, live):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(live, 1.0))
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(lambda: (httpd.shutdown() if th.is_alive() else None))
        return httpd, th, f"http://127.0.0.1:{httpd.server_address[1]}"

    def _post(self, base, body):
        req = Request(base + "/api/site/handover", method="POST",
                      data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except HTTPError as e:
            return e.code, json.loads(e.read())

    def test_no_site_password_set_refuses(self):
        live, tx = _worked_fleet()
        _httpd, th, base = self._serve(live)
        code, body = self._post(base, {"password": "anything", "to_host": "laptop"})
        self.assertEqual(code, 409)
        self.assertEqual(body["error"], "no_password")
        self.assertTrue(th.is_alive())
        self.assertTrue(live._tx_claims)
        self.assertEqual(tx.sent, [])

    def test_wrong_password_is_refused_and_rate_limited(self):
        SA.set_password("right-one")
        live, tx = _worked_fleet()
        _httpd, th, base = self._serve(live)
        out = io_capture()
        with out:
            for _ in range(SA.MAX_FAILURES):
                code, body = self._post(base, {"password": "wrong-one", "to_host": "x"})
                self.assertEqual(code, 403)
                self.assertEqual(body["error"], "wrong_password")
            code, body = self._post(base, {"password": "right-one", "to_host": "x"})
        self.assertEqual(code, 429)
        self.assertIn("Too many wrong site passwords", body["detail"])
        self.assertTrue(th.is_alive())
        self.assertTrue(live._tx_claims)
        self.assertEqual(tx.sent, [])
        # The password never reaches the log.
        self.assertNotIn("wrong-one", out.text)
        self.assertNotIn("right-one", out.text)

    def test_right_password_turns_tx_off_clears_claims_and_exits(self):
        SA.set_password("right-one")
        live, tx = _worked_fleet()
        ann = _FakeAnnouncer()
        live._presence = ann
        self.assertIn(MID, live._tx_claims)
        _httpd, th, base = self._serve(live)
        out = io_capture()
        with out:
            code, body = self._post(base, {"password": "right-one", "to_host": "dell-7540"})
            th.join(timeout=5)
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"])
        # Enable Tx off only (auto-only halt). Nothing else was sent.
        self.assertEqual(tx.sent, [("halt", MID, True)])
        self.assertEqual(body["tx_off"], [MID])
        self.assertEqual(live._tx_claims, {})
        self.assertEqual(live._open_work, {})
        self.assertEqual(live._arbiter.holders(), {})
        self.assertTrue(ann.stopped)
        self.assertFalse(th.is_alive(), "server should stop after a handover")
        marker = SA.recent_handover()
        self.assertEqual(marker["to_host"], "dell-7540")
        self.assertNotIn("right-one", out.text)

    def test_release_never_keys_anything(self):
        live, tx = _worked_fleet()
        live.release_tx_for_handover()
        self.assertTrue(all(s[0] == "halt" and s[2] is True for s in tx.sent))


class io_capture:
    """Capture stdout/stderr text inside a with-block."""

    def __enter__(self):
        import contextlib
        import io
        self._buf = io.StringIO()
        self._stack = contextlib.ExitStack()
        self._stack.enter_context(contextlib.redirect_stdout(self._buf))
        self._stack.enter_context(contextlib.redirect_stderr(self._buf))
        return self

    def __exit__(self, *exc):
        self._stack.close()
        return False

    @property
    def text(self) -> str:
        return self._buf.getvalue()


# --- two servers never stop each other ------------------------------------- #

class SplitBrainTests(unittest.TestCase):
    def test_reachable_peer_is_reported_not_killed(self):
        peer = {"instance_id": "other", "hostname": "dell-7540",
                "console_base": "http://192.168.2.113:8787",
                "lan_ips": ["192.168.2.113"], "http_port": 8787}
        said: list[str] = []
        ann = P.PresenceAnnouncer(
            iface="127.0.0.1", http_port=8787, instance_id="me",
            on_conflict=lambda peers: list(peers), on_log=said.append,
        )
        self.assertFalse(ann._handle_peers({"other": peer}))
        self.assertFalse(ann._handle_peers({"other": peer}))
        self.assertFalse(ann.demoted)
        two = ann.two_servers()
        self.assertEqual([o["hostname"] for o in two], ["dell-7540"])
        st = ann.status_dict()
        self.assertEqual(st["two_servers"][0]["hostname"], "dell-7540")
        self.assertIn(P.TWO_SERVERS_MESSAGE, st["hints"])
        self.assertEqual(sum("Two site servers are running" in s for s in said), 1)

    def test_server_conflict_hook_no_longer_exits(self):
        src = (ROOT / "src" / "wims" / "server" / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("os._exit(2)", src)

    def test_unreachable_peer_does_not_count_as_two_servers(self):
        peer = {"instance_id": "old", "hostname": "dell-7540",
                "console_base": "http://192.168.2.113:8787"}
        ann = P.PresenceAnnouncer(iface="127.0.0.1", http_port=8787,
                                  instance_id="me", on_conflict=lambda _p: [])
        self.assertFalse(ann._handle_peers({"old": peer}))
        self.assertEqual(ann.two_servers(), [])
        self.assertIn("old", ann.unreachable_peers)


# --- launcher: default follow, permission, confirm ------------------------- #

class LauncherFlowTests(_TmpConfig):
    def test_default_follows_a_server_that_answers(self):
        fake = _fake_app()
        with mock.patch.object(A, "probe_site_urls",
                               return_value=(True, "http://192.168.2.165:8787")):
            LauncherApp._start_agent(fake, AGENT_SERVER)
        self.assertEqual(fake.started_roles, [])
        self.assertIn("not starting a second one", "\n".join(fake.log))
        self.assertEqual(fake.asked, [])

    def test_pc_that_may_not_host_never_starts_one(self):
        fake = _fake_app(_may_host=lambda: False)
        fake._site_hostname = "jeff-Precision-Tower-3420"
        with mock.patch.object(A, "probe_site_urls",
                               return_value=(False, "http://127.0.0.1:8787")):
            LauncherApp._start_agent(fake, AGENT_SERVER, lan_checked=True)
        self.assertEqual(fake.started_roles, [])
        self.assertFalse(fake._intent_vars[INTENT_SERVER].get())
        self.assertIn(no_server_message("jeff-Precision-Tower-3420"), fake.log)

    def test_start_asks_first_and_no_means_no(self):
        fake = _fake_app()
        fake._ask_yes_no = lambda title, text: fake.asked.append(text) and False
        with mock.patch.object(A, "probe_site_urls",
                               return_value=(False, "http://127.0.0.1:8787")):
            LauncherApp._start_site_server_offer(fake)
        self.assertEqual(fake.asked, [START_CONFIRM])
        self.assertEqual(fake.started_roles, [])
        self.assertFalse(fake._intent_vars[INTENT_SERVER].get())

    def test_start_asks_first_and_yes_starts(self):
        fake = _fake_app()
        with mock.patch.object(A, "probe_site_urls",
                               return_value=(False, "http://127.0.0.1:8787")):
            LauncherApp._start_site_server_offer(fake)
        self.assertEqual(fake.asked, [START_CONFIRM])
        self.assertEqual(fake.started_roles, ["server"])

    def test_unreachable_announcement_does_not_block_a_start(self):
        fake = _fake_app()
        fake._announced_site = {"hostname": "dell-7540",
                                "console_base": "http://192.168.2.113:8787"}
        with mock.patch.object(A, "probe_site_urls",
                               return_value=(False, "http://127.0.0.1:8787")):
            LauncherApp._start_site_server_offer(fake)
        self.assertEqual(fake.started_roles, ["server"])
        self.assertIn("cannot reach", fake.asked[0])
        self.assertIn("Stop that copy on dell-7540", fake.asked[0])

    def test_after_a_handover_the_old_host_follows_and_does_not_restart(self):
        fake = _fake_app(_handover_pending=lambda: {"to_host": "dell-7540", "ts": 1})
        fake._rediscover_if_down = lambda: None
        with mock.patch.object(A, "probe_site_urls",
                               return_value=(False, "http://127.0.0.1:8787")):
            LauncherApp._start_agent(fake, AGENT_SERVER, lan_checked=True)
        self.assertEqual(fake.started_roles, [])
        self.assertFalse(fake._intent_vars[INTENT_SERVER].get())
        self.assertIn("The site server moved to dell-7540", "\n".join(fake.log))

    def test_leftover_here_is_not_preferred_over_a_reachable_peer(self):
        fake = _fake_app(_find_remote_site=lambda: dict(_TOWER))
        with mock.patch.object(A, "probe_site_urls",
                               return_value=(True, "http://127.0.0.1:8787")):
            LauncherApp._kick_site_discover(fake)
        self.assertEqual(fake.applied, ["http://192.168.2.165:8787"])
        self.assertEqual(fake.started_roles, [])

    def test_unchecking_asks_and_no_keeps_the_server(self):
        stopped: list = []
        fake = _fake_app(
            _proc_running=lambda role: role == "server",
            _stop_agent=lambda *a, **k: stopped.append(a),
        )
        fake._intent_vars[INTENT_SERVER].set(False)
        fake._ask_yes_no = lambda title, text: fake.asked.append(text) and False
        LauncherApp._on_server_intent_toggle(fake)
        self.assertEqual(fake.asked, [STOP_CONFIRM])
        self.assertEqual(stopped, [])
        self.assertTrue(fake._intent_vars[INTENT_SERVER].get())

    def test_takeover_needs_permission_and_password(self):
        requests: list = []
        with mock.patch.object(SA, "request_handover",
                               side_effect=lambda *a, **k: requests.append(a)):
            fake = _fake_app(_may_host=lambda: False)
            fake._takeover_busy = False
            LauncherApp._take_over_site_server(fake)
            self.assertEqual(requests, [])
            # Permitted, but no site password set on this PC.
            fake = _fake_app()
            fake._takeover_busy = False
            fake._pw_limiter = SA.FailureLimiter()
            fake._tell = lambda title, text, error=False: fake.log.append(text)
            fake._password_ok = types.MethodType(LauncherApp._password_ok, fake)
            LauncherApp._take_over_site_server(fake)
            self.assertEqual(requests, [])
            self.assertIn("No site password is set on this PC", "\n".join(fake.log))
            # Wrong password.
            SA.set_password("right-one")
            fake._ask_password = lambda *a: "wrong-one"
            LauncherApp._take_over_site_server(fake)
            self.assertEqual(requests, [])
            self.assertIn("That is not the site password.", fake.log)

    def test_takeover_asks_the_other_server_then_starts_here(self):
        SA.set_password("right-one")
        calls: list = []
        fake = _fake_app()
        fake._takeover_busy = False
        fake._pw_limiter = SA.FailureLimiter()
        fake._two_servers = []
        fake._site_ok = True
        fake._site_show_url = "http://192.168.2.165:8787"
        fake._site_hostname = "jeff-Precision-Tower-3420"
        fake._ask_password = lambda *a: "right-one"
        fake._tell = lambda title, text, error=False: fake.log.append(text)
        fake._repoint_agents_if_needed = lambda prev, new: calls.append(("repoint", prev, new))
        for name in ("_password_ok", "_take_over_target", "_after_handover_request",
                     "_finish_takeover"):
            setattr(fake, name, types.MethodType(getattr(LauncherApp, name), fake))
        fake._site_url_is_local = lambda base: "127.0.0.1" in (base or "")

        def handover(base, password, *, to_host):
            calls.append(("handover", base, password == "right-one"))
            return True, 200, ""

        with mock.patch.object(SA, "request_handover", side_effect=handover), \
             mock.patch.object(SA, "wait_until_down", return_value=True), \
             mock.patch.object(A, "probe_site_urls",
                               return_value=(False, "http://127.0.0.1:8787")):
            LauncherApp._take_over_site_server(fake)
        self.assertEqual(fake.asked, [takeover_confirm_text("jeff-Precision-Tower-3420")])
        self.assertEqual(calls[0], ("handover", "http://192.168.2.165:8787", True))
        self.assertEqual(fake.started_roles, ["server"])
        self.assertIn(("repoint", "http://192.168.2.165:8787", A.LOCAL_SITE), calls)
        self.assertTrue(fake._intent_vars[INTENT_SERVER].get())


if __name__ == "__main__":
    unittest.main(verbosity=1)
