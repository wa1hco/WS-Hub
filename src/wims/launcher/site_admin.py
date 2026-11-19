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

"""Who may run the site server on this PC, and the site password.

Stored in ``site_admin.json`` in the WS Hub settings folder
(``~/.config/wims`` on Linux, ``%APPDATA%\\wims`` on Windows):

* ``may_host`` - "This PC may host the site server". Decided once (on for
  a first-run single PC or a PC already hosting, off for other seats) and
  then changed only in Setup.
* ``password`` - the site password as a salted PBKDF2-SHA256 hash. The
  password itself is never written to disk, logs, the environment or a
  command line.

A handover marker (``site_handover.json``) tells the launcher on the old
host that its site server stopped because another PC took over, so it
follows the new one instead of starting the old one again.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

ADMIN_FILENAME = "site_admin.json"
HANDOVER_FILENAME = "site_handover.json"
SCHEMA = 1

PBKDF2_ALG = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 200_000
SALT_BYTES = 16
MIN_PASSWORD_LEN = 4

# A handover marker older than this is ignored.
HANDOVER_FRESH_S = 15 * 60

# Failed password tries: after MAX_FAILURES within FAILURE_WINDOW_S, every
# try is refused until LOCKOUT_S has passed since the last failure.
MAX_FAILURES = 5
FAILURE_WINDOW_S = 300.0
LOCKOUT_S = 60.0


def _config_dir() -> Path:
    from wims.rules.choice import config_dir
    return config_dir()


def admin_path(path: Path | str | None = None) -> Path:
    """``site_admin.json``. ``WIMS_SITE_ADMIN`` points tests elsewhere."""
    if path is not None and str(path).strip():
        return Path(path)
    env = (os.environ.get("WIMS_SITE_ADMIN") or "").strip()
    if env:
        return Path(env)
    return _config_dir() / ADMIN_FILENAME


def handover_path(path: Path | str | None = None) -> Path:
    if path is not None and str(path).strip():
        return Path(path)
    return admin_path().parent / HANDOVER_FILENAME


def _read_json(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def load_admin(path: Path | str | None = None) -> dict:
    return _read_json(admin_path(path))


def save_admin(data: dict, path: Path | str | None = None) -> None:
    out = dict(data)
    out["schema"] = SCHEMA
    _write_json(admin_path(path), out)


# --- password hash ---------------------------------------------------------- #

def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> dict:
    """Salted hash record for ``password``. Never holds the password."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, int(iterations))
    return {
        "alg": PBKDF2_ALG,
        "iterations": int(iterations),
        "salt": salt.hex(),
        "hash": digest.hex(),
    }


def verify_password(password: str, record: dict | None) -> bool:
    """Constant-time check of ``password`` against a hash record."""
    if not isinstance(record, dict) or not isinstance(password, str):
        return False
    if record.get("alg") != PBKDF2_ALG:
        return False
    try:
        salt = bytes.fromhex(str(record.get("salt") or ""))
        want = bytes.fromhex(str(record.get("hash") or ""))
        iterations = int(record.get("iterations") or 0)
    except (TypeError, ValueError):
        return False
    if not salt or not want or iterations <= 0:
        return False
    got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(got, want)


def has_password(path: Path | str | None = None) -> bool:
    rec = load_admin(path).get("password")
    return isinstance(rec, dict) and bool(rec.get("hash"))


def check_password(password: str, path: Path | str | None = None) -> bool:
    return verify_password(password, load_admin(path).get("password"))


def password_problem(password: str, confirm: str | None = None) -> str:
    """Plain reason a new password is not acceptable, or ``""``."""
    if len(password or "") < MIN_PASSWORD_LEN:
        return f"Use at least {MIN_PASSWORD_LEN} characters."
    if confirm is not None and confirm != password:
        return "The two passwords are not the same."
    return ""


def set_password(
    new_password: str,
    *,
    current_password: str | None = None,
    path: Path | str | None = None,
) -> tuple[bool, str]:
    """Set (or change) the site password on this PC.

    Changing an existing password needs the current one.
    """
    problem = password_problem(new_password)
    if problem:
        return False, problem
    data = load_admin(path)
    if isinstance(data.get("password"), dict) and data["password"].get("hash"):
        if not verify_password(current_password or "", data["password"]):
            return False, "The current site password is not right."
    data["password"] = hash_password(new_password)
    save_admin(data, path)
    return True, "Site password saved on this PC."


# --- may this PC host? ------------------------------------------------------ #

def may_host_saved(path: Path | str | None = None) -> bool | None:
    val = load_admin(path).get("may_host")
    return val if isinstance(val, bool) else None


def default_may_host(*, last_site_is_other_pc: bool, hosting_now: bool) -> bool:
    """First decision: on for a single PC or the PC already hosting."""
    if hosting_now:
        return True
    return not last_site_is_other_pc


def may_host(
    *,
    last_site_is_other_pc: bool = False,
    hosting_now: bool = False,
    path: Path | str | None = None,
) -> bool:
    """Whether this PC may host. The first answer is remembered."""
    saved = may_host_saved(path)
    if saved is not None:
        return saved
    value = default_may_host(
        last_site_is_other_pc=last_site_is_other_pc, hosting_now=hosting_now)
    set_may_host(value, path=path)
    return value


def set_may_host(value: bool, path: Path | str | None = None) -> None:
    data = load_admin(path)
    data["may_host"] = bool(value)
    try:
        save_admin(data, path)
    except OSError:
        pass


# --- handover marker -------------------------------------------------------- #

def write_handover_marker(
    to_host: str, *, now: float | None = None, path: Path | str | None = None,
) -> None:
    _write_json(handover_path(path), {
        "schema": SCHEMA,
        "to_host": (to_host or "").strip() or "another PC",
        "ts": time.time() if now is None else float(now),
    })


def recent_handover(
    *, now: float | None = None, path: Path | str | None = None,
) -> dict | None:
    """The handover marker when it is fresh, else None."""
    data = _read_json(handover_path(path))
    ts = data.get("ts")
    if not isinstance(ts, (int, float)):
        return None
    now = time.time() if now is None else now
    if now - float(ts) > HANDOVER_FRESH_S or float(ts) - now > 60:
        return None
    return data


def clear_handover_marker(path: Path | str | None = None) -> None:
    try:
        handover_path(path).unlink()
    except OSError:
        pass


# --- failed-password rate limit -------------------------------------------- #

class FailureLimiter:
    """Refuse password tries for a while after repeated failures."""

    def __init__(
        self,
        *,
        max_failures: int = MAX_FAILURES,
        window_s: float = FAILURE_WINDOW_S,
        lockout_s: float = LOCKOUT_S,
        clock=time.monotonic,
    ) -> None:
        self.max_failures = int(max_failures)
        self.window_s = float(window_s)
        self.lockout_s = float(lockout_s)
        self._clock = clock
        self._fails: list[float] = []
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        self._fails = [t for t in self._fails if now - t < self.window_s]

    def locked_for(self) -> float:
        """Seconds until a try is allowed again (0 when allowed now)."""
        with self._lock:
            now = self._clock()
            self._prune(now)
            if len(self._fails) < self.max_failures:
                return 0.0
            left = self.lockout_s - (now - self._fails[-1])
            return max(0.0, left)

    def failed(self) -> None:
        with self._lock:
            now = self._clock()
            self._prune(now)
            self._fails.append(now)

    def succeeded(self) -> None:
        with self._lock:
            self._fails.clear()


# --- takeover from the launcher --------------------------------------------- #

def request_handover(
    base: str,
    password: str,
    *,
    to_host: str,
    timeout: float = 8.0,
) -> tuple[bool, int, str]:
    """Ask the site server at ``base`` to hand over to this PC.

    Returns ``(ok, http_status, plain_detail)``. The password goes only in
    the request body; it is never logged.
    """
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    url = (base or "").strip().rstrip("/") + "/api/site/handover"
    data = json.dumps({"password": password, "to_host": to_host}).encode("utf-8")
    req = Request(url, data=data, method="POST",
                  headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - LAN console
            body = _json_body(resp.read())
            return True, int(getattr(resp, "status", 200) or 200), str(
                body.get("detail") or "")
    except HTTPError as exc:
        try:
            body = _json_body(exc.read())
        except Exception:
            body = {}
        code = int(exc.code or 0)
        detail = str(body.get("detail") or "")
        if code == 404:
            detail = ("That site server is an older WS Hub that cannot hand "
                      "over. Stop it on that PC, then start one here.")
        return False, code, detail or f"The site server answered {code}."
    except (URLError, OSError, ValueError) as exc:
        return False, 0, f"Could not reach that site server ({exc})."


def _json_body(raw: bytes) -> dict:
    try:
        out = json.loads((raw or b"{}").decode("utf-8", errors="replace") or "{}")
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


def wait_until_down(
    is_up,
    *,
    timeout_s: float = 10.0,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> bool:
    """Poll ``is_up()`` until it is False. True when it went down in time."""
    end = monotonic() + timeout_s
    while True:
        try:
            up = bool(is_up())
        except Exception:
            up = False
        if not up:
            return True
        if monotonic() >= end:
            return False
        sleep(0.25)
