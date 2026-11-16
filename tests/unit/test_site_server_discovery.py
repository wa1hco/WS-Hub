# WS Hub
# Copyright (C) 2026 Jeff Millar, WA1HCO
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Site server: find the one on another PC before starting or restarting here.

Covers the 1.0.29 laptop case: an old copy left on this PC was restarted in
place of the station's site server on another PC, the restart raced the old
process, and the window stayed pointed at 127.0.0.1 with nothing there.

No real process, socket scan or window is used. The OS, HTTP and network
edges are injected or patched.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wims.discovery import presence as P  # noqa: E402
from wims.launcher import app as A  # noqa: E402
from wims.launcher.app import LauncherApp  # noqa: E402
from wims.launcher.assets import AGENT_SERVER, INTENT_SERVER  # noqa: E402
from wims.launcher.site_restart import (  # noqa: E402
    STILL_RUNNING_MESSAGE,
    SITE_ROLE,
    clear_stale_site_server_pid,
    perform_site_restart,
    read_site_server_pid,
    restarted_message,
    wait_for_pids_to_exit,
    write_site_server_pid,
)

_SERVER_ARGV = ["/usr/bin/python3", "-u", "-m", "wims.server.app"]
_TOWER = {
    "ok": True,
    "role": SITE_ROLE,
    "hostname": "jeff-Precision-Tower-3420",
    "console_base": "http://192.168.2.165:8787",
    "lan_ips": ["192.168.2.165"],
    "http_port": 8787,
}


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def sleep(self, s: float) -> None:
        self.t += s

    def monotonic(self) -> float:
        return self.t


class _Var:
    def __init__(self, value=None) -> None:
        self.value = value

    def get(self):
        return self.value

    def set(self, value) -> None:
        self.value = value


class _Root:
    """root.after runs the callback at once so the flow is synchronous."""

    def after(self, _ms, fn=None, *args):
        if fn is not None:
            fn(*args)
        return "after#1"


def _health(rev: str, pid: int) -> dict:
    return {"ok": True, "role": SITE_ROLE, "server_rev": rev, "pid": pid}


def _fake_app(**extra):
    """Bare object with just enough LauncherApp state to run one flow."""
    fake = types.SimpleNamespace()
    fake.log = []
    fake.started_roles = []
    fake.adopted = []
    fake.applied = []
    fake.root = _Root()
    fake._agent_role = {AGENT_SERVER: "server"}
    fake._starting = set()
    fake._server_start_blocked = False
    fake._site_var = _Var("http://127.0.0.1:8787")
    fake._site_ok = False
    fake._site_external = False
    fake._site_show_url = ""
    fake._site_choice_ready = False
    fake._discovering = False
    fake._remote_primary = ""
    fake._remote_search_busy = False
    fake._remote_search_at = 0.0
    fake._intent_vars = {INTENT_SERVER: _Var(True)}
    fake._REMOTE_SEARCH_FRESH_S = LauncherApp._REMOTE_SEARCH_FRESH_S
    fake._append_log = fake.log.append
    fake._schedule_status = lambda *_a, **_k: None
    fake._persist_intent = lambda: None
    fake._proc_running = lambda _role: False
    fake._system_agent_procs = lambda _aid: []
    fake._local_server_procs = lambda: []
    fake._open_url_once = lambda *_a: None
    fake._current_intent = lambda: {INTENT_SERVER: True}
    fake._run_background = lambda fn: fn()
    fake._start_role = lambda role, **_k: fake.started_roles.append(role.id)
    fake._apply_discovered_site = lambda url, *a: fake.applied.append(url)
    fake._refresh_status = lambda: None
    fake._find_remote_site = lambda: None
    for key, value in extra.items():
        setattr(fake, key, value)
    for name in (
        "_start_agent",
        "_check_lan_then_start",
        "_after_lan_check_for_start",
        "_adopt_remote_site",
        "_start_site_server_offer",
        "_kick_site_discover",
        "_finish_leftover_check",
        "_site_url_is_local",
        "_after_failed_restart",
        "_restart_found_remote",
        "_restart_site_server_worker",
        "_note_server_revision",
    ):
        if name not in extra:
            setattr(fake, name, types.MethodType(getattr(LauncherApp, name), fake))
    return fake


class _EnvGuard(unittest.TestCase):
    def setUp(self) -> None:
        self._env = dict(os.environ)
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["WIMS_SITE_SERVER_PID"] = str(Path(self._tmp.name) / "site-server.pid")
        patcher = mock.patch.object(A, "save_last_site_url", lambda *_a: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()


# --- 1. Restart race -------------------------------------------------------- #

class RestartRaceTests(unittest.TestCase):
    def test_start_waits_until_the_old_process_has_exited(self):
        """The port closes first; the process is gone a little later."""
        clock = _Clock()
        state = {"up": True, "started": False, "exit_at": None}
        start_saw_alive: list[bool] = []

        def alive(pid: int) -> bool:
            if pid != 4242:
                return False
            if state["up"]:
                return True
            return clock.t < state["exit_at"]

        def ask_stop() -> bool:
            state["up"] = False
            state["exit_at"] = clock.t + 1.0  # still exiting for one second
            return True

        def start() -> None:
            start_saw_alive.append(alive(4242))
            state["started"] = True
            state["up"] = True

        def fetch():
            if not state["up"]:
                return None
            return _health("ccc3333", 5000 if state["started"] else 4242)

        outcome = perform_site_restart(
            port=8787,
            disk_rev="ccc3333",
            fetch_health=fetch,
            start_server=start,
            request_stop=ask_stop,
            stop_pid=lambda _pid: None,
            listener_pids=[4242],
            read_pid_file=lambda: None,
            argv_of=lambda _pid: _SERVER_ARGV,
            alive=lambda pid: (not state["started"]) and alive(pid),
            port_open=lambda: state["up"],
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        self.assertEqual(start_saw_alive, [False])
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.message, restarted_message("ccc3333"))

    def test_process_that_never_exits_is_stopped_and_not_doubled(self):
        clock = _Clock()
        state = {"up": True}
        stopped: list[int] = []
        started: list[bool] = []

        outcome = perform_site_restart(
            port=8787,
            disk_rev="ccc3333",
            fetch_health=lambda: _health("aaa1111", 4242) if state["up"] else None,
            start_server=lambda: started.append(True),
            request_stop=lambda: state.update(up=False) or True,
            stop_pid=stopped.append,
            listener_pids=[4242],
            read_pid_file=lambda: None,
            argv_of=lambda _pid: _SERVER_ARGV,
            alive=lambda _pid: True,  # never goes away
            port_open=lambda: state["up"],
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        self.assertEqual(stopped, [4242])
        self.assertEqual(started, [])
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.message, STILL_RUNNING_MESSAGE)

    def test_wait_for_pids_to_exit_is_bounded(self):
        clock = _Clock()
        self.assertTrue(wait_for_pids_to_exit([], alive=lambda _p: True))
        self.assertFalse(wait_for_pids_to_exit(
            [7], timeout_s=2.0, alive=lambda _p: True,
            sleep=clock.sleep, monotonic=clock.monotonic,
        ))
        self.assertLessEqual(clock.t, 2.5)

    def test_failed_restart_searches_the_network(self):
        from wims.launcher.site_restart import RestartOutcome
        kicked: list[bool] = []
        notes: list[tuple] = []
        fake = _fake_app(
            _kick_site_discover=lambda: kicked.append(True),
            _note_restart_result=lambda msg, ok: notes.append((msg, ok)),
        )
        with mock.patch.object(A, "perform_site_restart",
                               return_value=RestartOutcome(False, "did not come up", stopped=True)), \
             mock.patch.object(A, "git_stamp_fresh", return_value="ccc3333"), \
             mock.patch.object(A, "clear_stale_site_server_pid", lambda *a, **k: False):
            LauncherApp._restart_site_server_worker(fake)
        self.assertEqual(notes, [("did not come up", False)])
        self.assertEqual(kicked, [True])


# --- 2. Start: look on the network first ------------------------------------ #

class StartLooksOnTheNetworkFirstTests(_EnvGuard):
    def test_start_adopts_a_server_on_another_pc(self):
        fake = _fake_app(_find_remote_site=lambda: dict(_TOWER))
        with mock.patch.object(A, "probe_site_urls", return_value=(False, "http://127.0.0.1:8787")):
            LauncherApp._start_site_server_offer(fake)
        self.assertEqual(fake.started_roles, [])
        self.assertEqual(fake.applied, ["http://192.168.2.165:8787"])
        self.assertEqual(fake._remote_primary, "http://192.168.2.165:8787")
        self.assertFalse(fake._intent_vars[INTENT_SERVER].get())
        text = "\n".join(fake.log)
        self.assertIn("The site server is on jeff-Precision-Tower-3420 (192.168.2.165:8787)", text)
        self.assertIn("Not starting another site server on this PC.", text)

    def test_start_runs_here_when_no_other_pc_answers(self):
        fake = _fake_app()
        with mock.patch.object(A, "probe_site_urls", return_value=(False, "http://127.0.0.1:8787")):
            LauncherApp._start_site_server_offer(fake)
        self.assertEqual(fake.started_roles, ["server"])
        self.assertIn("No site server on another PC answered. Starting one on this PC.", fake.log)

    def test_sync_start_also_checks_and_does_not_search_twice_at_once(self):
        searches: list[int] = []
        pending: list = []
        fake = _fake_app(
            _find_remote_site=lambda: searches.append(1) or None,
            _run_background=pending.append,
        )
        with mock.patch.object(A, "probe_site_urls", return_value=(False, "http://127.0.0.1:8787")):
            LauncherApp._start_agent(fake, AGENT_SERVER)
            LauncherApp._start_agent(fake, AGENT_SERVER)  # sync tick while busy
            self.assertEqual(len(pending), 1)
            self.assertEqual(fake.started_roles, [])
            pending[0]()
        self.assertEqual(searches, [1])
        self.assertEqual(fake.started_roles, ["server"])

    def test_after_a_network_search_start_is_direct(self):
        fake = _fake_app(_find_remote_site=lambda: self.fail("searched again"))
        with mock.patch.object(A, "probe_site_urls", return_value=(False, "http://127.0.0.1:8787")):
            LauncherApp._start_agent(fake, AGENT_SERVER, lan_checked=True)
        self.assertEqual(fake.started_roles, ["server"])


# --- 3. Leftover local server after an update ------------------------------- #

class LeftoverLocalServerTests(_EnvGuard):
    def test_leftover_on_this_pc_is_not_trusted_before_the_network_check(self):
        fake = _fake_app(_find_remote_site=lambda: dict(_TOWER))
        with mock.patch.object(A, "probe_site_urls", return_value=(True, "http://127.0.0.1:8787")):
            LauncherApp._kick_site_discover(fake)
        self.assertEqual(fake.applied, ["http://192.168.2.165:8787"])
        self.assertEqual(fake._remote_primary, "http://192.168.2.165:8787")

    def test_leftover_is_used_when_no_other_pc_answers(self):
        fake = _fake_app()
        with mock.patch.object(A, "probe_site_urls", return_value=(True, "http://127.0.0.1:8787")):
            LauncherApp._kick_site_discover(fake)
        self.assertEqual(fake.applied, ["http://127.0.0.1:8787"])
        self.assertEqual(fake._remote_primary, "")

    def test_server_this_window_started_is_used_without_a_search(self):
        fake = _fake_app(
            _proc_running=lambda role: role == "server",
            _find_remote_site=lambda: self.fail("searched"),
        )
        with mock.patch.object(A, "probe_site_urls", return_value=(True, "http://127.0.0.1:8787")):
            LauncherApp._kick_site_discover(fake)
        self.assertEqual(fake.applied, ["http://127.0.0.1:8787"])

    def test_restart_is_not_run_when_another_pc_has_the_site_server(self):
        fake = _fake_app(
            _find_remote_site=lambda: dict(_TOWER),
            _local_server_procs=lambda: [types.SimpleNamespace(pid=725731)],
        )
        fake._restart_in_progress = True
        with mock.patch.object(A, "perform_site_restart",
                               side_effect=AssertionError("restarted the leftover")), \
             mock.patch.object(A, "clear_stale_site_server_pid", lambda *a, **k: False):
            LauncherApp._restart_site_server_worker(fake)
        self.assertFalse(fake._restart_in_progress)
        self.assertEqual(fake.applied, ["http://192.168.2.165:8787"])
        self.assertIn(
            "An older site server is still running on this PC. "
            "It was left alone and is not restarted.",
            fake.log,
        )

    def _stale(self, *, ready: bool, base: str) -> bool:
        fake = _fake_app(_show_loaded_revs=lambda *a: None)
        fake._site_choice_ready = ready
        fake._server_code_stale = False
        with mock.patch.object(A, "git_stamp_fresh", return_value="new1234"), \
             mock.patch.object(A, "server_restart_needed", return_value=True):
            return LauncherApp._note_server_revision(
                fake, ok_site=True, base=base,
                health={"role": SITE_ROLE, "server_rev": "old0000", "version": "1.0.22"},
            )

    def test_restart_needed_waits_for_the_network_check(self):
        self.assertFalse(self._stale(ready=False, base="http://127.0.0.1:8787"))
        self.assertTrue(self._stale(ready=True, base="http://127.0.0.1:8787"))

    def test_restart_needed_is_not_shown_for_a_server_on_another_pc(self):
        self.assertFalse(self._stale(ready=True, base="http://192.168.2.165:8787"))

    def test_status_keeps_the_adopted_server_over_a_leftover_on_loopback(self):
        def fetch(base, timeout=0.5):
            if "127.0.0.1" in base:
                return {"ok": True, "role": SITE_ROLE, "console_base": "http://127.0.0.1:8787",
                        "hostname": "dell-7540"}
            return dict(_TOWER)

        with mock.patch.object(A, "fetch_site_health", side_effect=fetch):
            plain = A.probe_resolved_site("http://192.168.2.165:8787")
            pinned = A.probe_resolved_site("http://192.168.2.165:8787", prefer_lan=True)
        self.assertTrue(plain["this_pc"])
        self.assertFalse(pinned["this_pc"])
        self.assertEqual(pinned["show_url"], "http://192.168.2.165:8787")
        self.assertEqual(pinned["hostname"], "jeff-Precision-Tower-3420")


# --- 2/3. Finding a server on another PC ------------------------------------ #

class FindRemoteTests(unittest.TestCase):
    OWN = {"127.0.0.1", "0.0.0.0", "localhost", "192.168.2.113"}

    def test_own_announcement_is_skipped_and_scan_skips_own_address(self):
        own_beacon = {"instance_id": "a", "console_base": "http://192.168.2.113:8787",
                      "lan_ips": ["192.168.2.113"]}
        scans: list[dict] = []

        def scan(**kw):
            scans.append(kw)
            return dict(_TOWER)

        found = P.find_remote_site_server(
            own=self.OWN,
            listen=lambda **_k: [own_beacon],
            reachable=lambda _b: self.fail("probed this PC"),
            http_scan=scan,
        )
        self.assertEqual(found["hostname"], "jeff-Precision-Tower-3420")
        self.assertEqual(scans[0]["exclude_ips"], ["0.0.0.0", "192.168.2.113", "localhost"])

    def test_announcing_server_that_does_not_answer_http_is_not_used(self):
        silent = dict(_TOWER, instance_id="t")
        found = P.find_remote_site_server(
            own=self.OWN,
            listen=lambda **_k: [silent],
            reachable=lambda _b: False,
            http_scan=lambda **_k: None,
        )
        self.assertIsNone(found)

    def test_reachable_announcement_is_used_without_a_scan(self):
        found = P.find_remote_site_server(
            own=self.OWN,
            listen=lambda **_k: [dict(_TOWER, instance_id="t")],
            reachable=lambda _b: True,
            http_scan=lambda **_k: self.fail("scanned"),
        )
        self.assertEqual(found["console_base"], "http://192.168.2.165:8787")

    def test_beacon_is_this_pc(self):
        self.assertTrue(P.beacon_is_this_pc({"console_base": "http://127.0.0.1:8787"}, self.OWN))
        self.assertTrue(P.beacon_is_this_pc(
            {"console_base": "http://127.0.0.1:8787", "lan_ips": ["192.168.2.113"]}, self.OWN))
        self.assertFalse(P.beacon_is_this_pc(dict(_TOWER), self.OWN))

    def test_http_scan_never_probes_excluded_addresses(self):
        probed: list[str] = []

        def probe(ip, _port, _timeout):
            probed.append(ip)
            return None

        with mock.patch.object(P, "list_lan_ipv4s", return_value=["192.168.2.113"]), \
             mock.patch.object(P, "_http_probe_one", side_effect=probe):
            P.http_discover_site_server(exclude_ips=["192.168.2.113"])
        self.assertNotIn("192.168.2.113", probed)
        self.assertIn("192.168.2.165", probed)


# --- 4. Presence announcer: never go quiet silently ------------------------- #

class AnnouncerTests(unittest.TestCase):
    def _announcer(self, **kw):
        logs: list[str] = []
        ann = P.PresenceAnnouncer(iface="0.0.0.0", http_port=8787, interval_s=0.05,
                                  on_log=logs.append, **kw)
        return ann, logs

    def _run_briefly(self, ann, seconds=0.6):
        with mock.patch.object(P.random, "uniform", return_value=0.0):
            ann.start()
            time.sleep(seconds)
            ann.stop()

    def test_listen_failure_is_logged_and_announcing_continues(self):
        ann, logs = self._announcer()
        with mock.patch.object(P, "open_listen_socket",
                               side_effect=OSError(98, "Address already in use")), \
             mock.patch.object(P, "send_beacon_all", return_value=3):
            self._run_briefly(ann)
        self.assertGreater(ann._seq, 1)
        self.assertEqual(ann.last_send_ok, 3)
        self.assertIn("Address already in use", ann.listen_error)
        st = ann.status_dict()
        self.assertFalse(st["listening"])
        self.assertTrue(any("could not listen on UDP 8788" in line for line in logs))
        self.assertTrue(any("Announcing, but not listening" in h for h in st["hints"]))

    def test_send_failure_is_logged_once_and_shown(self):
        ann, logs = self._announcer()
        with mock.patch.object(P, "open_listen_socket", side_effect=OSError("no")), \
             mock.patch.object(P, "send_beacon_all", return_value=0):
            self._run_briefly(ann)
        sends = [line for line in logs if "could not announce itself" in line]
        self.assertEqual(len(sends), 1)
        self.assertFalse(ann.status_dict()["announcing"])
        self.assertTrue(any("Not announcing" in h for h in ann.status_dict()["hints"]))

    def test_unreachable_peer_does_not_silence_this_server(self):
        """The Tower heard the laptop's old copy (firewalled) and went quiet."""
        ann, logs = self._announcer(on_conflict=lambda _peers: False)
        peer = {"instance_id": "laptop", "hostname": "dell-7540",
                "console_base": "http://192.168.2.113:8787"}
        with mock.patch.object(P, "open_listen_socket", return_value=object()), \
             mock.patch.object(ann, "_read_peers", return_value={"laptop": peer}), \
             mock.patch.object(P, "send_beacon_all", return_value=3):
            self._run_briefly(ann)
        self.assertFalse(ann.demoted)
        self.assertGreater(ann._seq, 1)
        heard = [line for line in logs if "dell-7540 is also announcing" in line]
        self.assertEqual(len(heard), 1)

    def test_reachable_peer_demotes(self):
        ann, _logs = self._announcer(on_conflict=lambda _peers: True)
        with mock.patch.object(P, "open_listen_socket", return_value=object()), \
             mock.patch.object(ann, "_read_peers", return_value={"x": {"instance_id": "x"}}), \
             mock.patch.object(P, "send_beacon_all", return_value=3):
            self._run_briefly(ann, 0.3)
        self.assertTrue(ann.demoted)
        self.assertEqual(ann._seq, 0)

    def test_unexpected_error_is_logged_not_silent(self):
        ann, logs = self._announcer()
        with mock.patch.object(P, "open_listen_socket", side_effect=OSError("no")), \
             mock.patch.object(P, "build_beacon", side_effect=RuntimeError("boom")):
            self._run_briefly(ann, 0.3)
        self.assertIn("boom", ann.stopped_error)
        self.assertTrue(any("stopped announcing" in line for line in logs))

    def test_listen_socket_shares_the_port(self):
        """A seat agent searching on 8788 must not lock the server out."""
        try:
            a = P.open_listen_socket("0.0.0.0", P.DEFAULT_GROUP, 0)
        except OSError as exc:
            self.skipTest(f"no UDP here ({exc})")
        try:
            port = a.getsockname()[1]
            b = P.open_listen_socket("0.0.0.0", P.DEFAULT_GROUP, port)
            b.close()
        finally:
            a.close()

    def test_healthz_presence_summary(self):
        from wims.server.app import _presence_summary
        self.assertEqual(_presence_summary(None), {"enabled": False, "announcing": False})
        ann, _ = self._announcer()
        ann.listen_error = "Address already in use"
        ann.last_send_ok = 3
        out = _presence_summary(ann)
        self.assertTrue(out["enabled"])
        self.assertFalse(out["listening"])
        self.assertIn("not listening", out["error"])


# --- 5. Stale pid file ------------------------------------------------------ #

class StalePidFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "site-server.pid"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_dead_pid_is_cleared(self):
        write_site_server_pid(725731, 8787, self.path)
        self.assertTrue(clear_stale_site_server_pid(self.path, alive=lambda _p: False))
        self.assertIsNone(read_site_server_pid(self.path))
        self.assertFalse(self.path.exists())

    def test_live_server_pid_is_kept(self):
        write_site_server_pid(4242, 8787, self.path)
        self.assertFalse(clear_stale_site_server_pid(
            self.path, alive=lambda _p: True, argv_of=lambda _p: _SERVER_ARGV))
        self.assertEqual(read_site_server_pid(self.path), (4242, 8787))

    def test_reused_pid_is_cleared_unreadable_one_kept(self):
        write_site_server_pid(4242, 8787, self.path)
        self.assertFalse(clear_stale_site_server_pid(
            self.path, alive=lambda _p: True, argv_of=lambda _p: None))
        self.assertTrue(clear_stale_site_server_pid(
            self.path, alive=lambda _p: True, argv_of=lambda _p: ["/usr/sbin/nginx"]))
        self.assertIsNone(read_site_server_pid(self.path))

    def test_missing_or_foreign_file_is_left_alone(self):
        self.assertFalse(clear_stale_site_server_pid(self.path, alive=lambda _p: False))
        self.path.write_text(json.dumps({"pid": 1, "port": 8787, "role": "other"}))
        self.assertFalse(clear_stale_site_server_pid(self.path, alive=lambda _p: False))
        self.assertTrue(self.path.exists())


if __name__ == "__main__":
    unittest.main(verbosity=1)
