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

"""Private UDP addresses for test traffic (emulator, benches, validate.sh).

A live site server listens on 224.0.0.73:2237 (WSJT-X) and :12060 (N1MM).
Test tools must never send there: a fake TRL-432 / VA2WXY decode once
showed up in a real operator's roster because validate.sh ran on the
station PC. These addresses are an administratively scoped group and
ports that no WS Hub, WSJT-X, or N1MM default uses. ``tools/dev/validate.sh``
repeats these values; a unit test keeps them in step.
"""

TEST_GROUP = "239.255.13.37"
TEST_PORT = 22437
TEST_N1MM_PORT = 22460

# Live defaults the test tools refuse to use unless told to on purpose.
LIVE_GROUP = "224.0.0.73"
LIVE_PORTS = (2237, 2238, 12060)


def reaches_live(group: str, port: int) -> bool:
    """True when (group, port) is where a live WS Hub or WSJT-X listens."""
    return str(group).strip() == LIVE_GROUP or int(port) in LIVE_PORTS
