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


"""Hover tooltips on live lists stay steady (Jeff: the Skim row tooltip flashed).

The page redraws on every /events frame (once a second). Rebuilding a list's
rows threw away the row under the mouse, so its hover tooltip blinked. While
the pointer is over Spotted, the call roster, or a Status table, the rebuild
waits and catches up on mouseleave. These checks load the page script in node.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "src" / "wims" / "server" / "static" / "wims.js"

SCRIPT = r"""
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync(process.argv[1], "utf8");

class El {
  constructor(tag, id) {
    this.tagName = tag || "div"; this.id = id || "";
    this.hidden = false; this.className = ""; this.textContent = ""; this.title = "";
    this.style = {}; this.dataset = {}; this.innerHTML = ""; this.hover = false;
    this.listeners = {}; this.rows = []; this.paints = 0;
    const self = this;
    this.classList = {
      set: new Set(),
      toggle(name, on) { if (on) this.set.add(name); else this.set.delete(name); },
      contains(name) { return this.set.has(name); },
    };
  }
  matches(sel) { return sel === ":hover" ? this.hover : false; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  fire(type, ev) { for (const fn of this.listeners[type] || []) fn(ev || {}); }
  querySelector() { return null; }
  querySelectorAll(sel) { return sel === "tr[data-call]" ? this.rows : []; }
  closest(sel) { return sel === "table" ? (this.table || null) : null; }
  remove() {}
}
const ids = {};
function make(id, tag) { const el = new El(tag, id); ids[id] = el; return el; }
make("conn", "span");
make("spot-status", "span");
const spot = make("spot", "table");
const spotBody = make("spot-body", "tbody");
spotBody.table = spot;

const document = {
  getElementById(id) { return ids[id] || null; },
  createElement(tag) { return new El(tag); },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {},
  activeElement: null,
};
const context = {
  console, document, location: {pathname: "/", search: ""},
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  EventSource: function EventSource() { this.close = function () {}; },
  fetch() { return Promise.resolve({ok: true, status: 200, json: async () => ({})}); },
  setTimeout() { return 0; }, clearTimeout() {}, setInterval() { return 0; }, clearInterval() {},
  requestAnimationFrame() { return 0; },
};
context.window = context;
context.ids = ids;
context.El = El;
context.spot = spot;
context.spotBody = spotBody;
vm.createContext(context);

const harness = `
const out = {};
// 1. holdWhileHovered: hold while hovered, catch up on mouseleave, with latest data.
const t = new El("table", "t");
let drawn = [];
out.idle = holdWhileHovered(t, () => drawn.push("a"), 1000);
t.hover = true;
out.held1 = holdWhileHovered(t, () => drawn.push("b"), 1000);
out.held2 = holdWhileHovered(t, () => drawn.push("c"), 2000);
out.beforeLeave = drawn.slice();
t.hover = false; t.fire("mouseleave");
out.afterLeave = drawn.slice();
t.fire("mouseleave");
out.afterSecondLeave = drawn.slice();
// 2. A pointer parked on one row still gets a refresh every HOVER_HOLD_MAX_MS.
const p = new El("table", "p"); p.hover = true;
out.parkStart = holdWhileHovered(p, () => {}, 10000);
out.parkMid = holdWhileHovered(p, () => {}, 10000 + HOVER_HOLD_MAX_MS - 1);
out.parkCap = holdWhileHovered(p, () => {}, 10000 + HOVER_HOLD_MAX_MS);
out.parkAgain = holdWhileHovered(p, () => {}, 10000 + HOVER_HOLD_MAX_MS + 1);
out.maxMs = HOVER_HOLD_MAX_MS;

// 3. Spotted: a live frame under the mouse keeps the rows; selection follows.
const tr1 = new El("tr"); tr1.dataset = {call: "JA1ABC", band: "20m"};
const tr2 = new El("tr"); tr2.dataset = {call: "VK2XY", band: "20m"};
const row = (call) => ({call, band: "20m", mode: "FT8", score: 1, factors: []});
renderSpotted({rows: [row("JA1ABC"), row("VK2XY")]});
out.spotFirst = spotBody.innerHTML.includes("JA1ABC");
spotBody.innerHTML = "KEEP"; spotBody.rows = [tr1, tr2];
spot.hover = true;
renderSpotted({rows: [row("ZL1ZZ")], selected: {call: "VK2XY", band: "20m"}});
out.spotHeld = spotBody.innerHTML;
out.spotSel = [tr1.classList.contains("spot-sel"), tr2.classList.contains("spot-sel")];
renderSpotted({rows: [row("ZL1ZZ")]}, true);
out.spotForced = spotBody.innerHTML.includes("ZL1ZZ");
spotBody.innerHTML = "KEEP";
renderSpotted({rows: [row("W1AW")]});
out.spotHeld2 = spotBody.innerHTML;
spot.hover = false; spot.fire("mouseleave");
out.spotCaughtUp = spotBody.innerHTML.includes("W1AW");

// 4. Roster alert key: calling-us / armed / can-TX changes always redraw.
out.keyQuiet = rosAlertKey({candidates: [{id: "a", call: "K1A"}]});
out.keyCalling = rosAlertKey({candidates: [{id: "a", call: "K1A", is_calling_us: true}]});
out.keyArmed = rosAlertKey({candidates: [{id: "a", call: "K1A", is_armed: true}]});
console.log(JSON.stringify(out));
`;
vm.runInContext(src + "\n" + harness, context);
"""

_OUT = None


def _out() -> dict:
    global _OUT
    if _OUT is None:
        node = shutil.which("node")
        if not node:
            raise AssertionError("node is required to load the page script")
        proc = subprocess.run([node, "-e", SCRIPT, str(JS)], capture_output=True,
                              text=True, timeout=30)
        if proc.returncode != 0:
            raise AssertionError(proc.stderr or proc.stdout)
        line = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
        _OUT = json.loads(line[-1])
    return _OUT


def test_hold_while_hovered_then_catch_up_with_latest_data():
    o = _out()
    assert o["idle"] is False
    assert o["held1"] is True and o["held2"] is True
    assert o["beforeLeave"] == []
    assert o["afterLeave"] == ["c"]          # latest frame, once
    assert o["afterSecondLeave"] == ["c"]


def test_parked_pointer_still_refreshes():
    o = _out()
    assert 3000 <= o["maxMs"] <= 15000
    assert o["parkStart"] is True and o["parkMid"] is True
    assert o["parkCap"] is False             # one refresh after the cap
    assert o["parkAgain"] is True            # then held again


def test_spotted_rows_steady_under_mouse_selection_follows():
    o = _out()
    assert o["spotFirst"] is True
    assert o["spotHeld"] == "KEEP"
    assert o["spotSel"] == [False, True]
    assert o["spotForced"] is True           # sort / columns still redraw
    assert o["spotHeld2"] == "KEEP"
    assert o["spotCaughtUp"] is True


def test_roster_alerts_are_never_held():
    o = _out()
    assert o["keyQuiet"] != o["keyCalling"]
    assert o["keyQuiet"] != o["keyArmed"]
    assert o["keyCalling"] != o["keyArmed"]
    js = JS.read_text(encoding="utf-8")
    body = js.split("function renderRoster(r) {", 1)[1].split("\n}\n", 1)[0]
    assert 'rosAlertKey(r) === _rosDrawnAlert && holdWhileHovered($("ros"), rosDraw)' in body
    draw = js.split("function rosDraw() {", 1)[1].split("\n}\n", 1)[0]
    assert "_rosDrawnAlert = rosAlertKey(r);" in draw


def test_status_tables_and_operator_actions():
    js = JS.read_text(encoding="utf-8")
    render = js.split("function render(s) {", 1)[1].split("\n}\n", 1)[0]
    for body_id in ("band-inv-body", "agents-body", "ant-body", "inst-body",
                    "dlog-body", "log-body"):
        assert f'renderUnlessHovered("{body_id}"' in render, body_id
    # The interlock / TX displays are never held.
    assert "renderInterlock(s.interlock, s.tx);" in render
    assert "renderTxBar(s.tx);" in render
    # Sort and column clicks force Spotted to redraw under the mouse.
    assert js.count("renderSpotted(_spotData, true)") >= 3


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
    sys.exit(1 if failed else 0)
