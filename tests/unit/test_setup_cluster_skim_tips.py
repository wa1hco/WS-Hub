# WS Hub
# Copyright (C) 2026 Jeff Millar, WA1HCO
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Setup DX cluster and Skim receivers: help lives in hover tooltips, not paragraphs."""

from __future__ import annotations

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "wims" / "server" / "static"


class _SectionText(HTMLParser):
    """Visible text in a Setup section, skipping tooltip / aria / title copy."""

    def __init__(self) -> None:
        super().__init__()
        self.visible: list[str] = []
        self.tips: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        ad = dict(attrs)
        cls = ad.get("class", "")
        if "tiptxt" in cls.split():
            self._skip += 1
        for key in ("title", "aria-label"):
            if ad.get(key):
                self.tips.append(ad[key])

    def handle_endtag(self, tag):
        if self._skip and tag == "span":
            self._skip -= 1

    def handle_data(self, data):
        if self._skip:
            self.tips.append(data)
            return
        text = " ".join(data.split())
        if text:
            self.visible.append(text)


def _section(html: str, heading: str, next_heading: str) -> str:
    start = html.index(f"<h2>{heading}")
    end = html.index(f"<h2>{next_heading}")
    return html[start:end]


def _parse(section: str) -> _SectionText:
    parser = _SectionText()
    parser.feed(section)
    parser.close()
    return parser


class ClusterSkimTooltipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (STATIC / "setup.html").read_text(encoding="utf-8")
        cls.css = (STATIC / "wims.css").read_text(encoding="utf-8")
        cls.cluster = _section(cls.html, "DX cluster", "Skim receivers")
        cls.skim = _section(cls.html, "Skim receivers", "Networking checklist")
        cls.cluster_parsed = _parse(cls.cluster)
        cls.skim_parsed = _parse(cls.skim)
        cls.cluster_visible = " ".join(cls.cluster_parsed.visible)
        cls.skim_visible = " ".join(cls.skim_parsed.visible)
        cls.cluster_tips = " ".join(cls.cluster_parsed.tips)
        cls.skim_tips = " ".join(cls.skim_parsed.tips)

    def test_sections_keep_field_labels_and_status_hooks(self):
        for phrase in (
            "Show spots",
            "Station call",
            "Cluster login",
            "Station grid",
            "Use PSK Reporter to rank spots",
            "Node",
            "Host",
            "Port",
            "QSY cost",
            "All bands",
        ):
            self.assertIn(phrase, self.cluster_visible, phrase)
        self.assertIn("Skimmer ids", self.skim_visible)
        self.assertIn("Where to point WS Skim", self.skim_visible)
        for cid in (
            'id="cluster-enabled"',
            'id="cluster-station-call"',
            'id="cluster-login"',
            'id="cluster-station-grid"',
            'id="cluster-psk-rank"',
            'id="cluster-psk-line"',
            'id="cluster-save-error"',
            'id="cluster-all"',
            'id="cluster-setup-line"',
            'id="cluster-call-warning"',
            'id="skim-ids"',
            'id="skim-setup-line"',
            'id="skim-server-hint"',
        ):
            self.assertIn(cid, self.html)

    def test_explanatory_paragraphs_are_gone(self):
        for visible, banned in (
            (
                self.cluster_visible,
                (
                    "Spots from a DX cluster, read only",
                    "does not post spots",
                    "drop off after 15 minutes",
                    "Use WA1HCO-1, -2",
                    "Bands to keep",
                    "settings file",
                    "once every five minutes",
                    "from a logger",
                ),
            ),
            (
                self.skim_visible,
                (
                    "WS Skim decodes the whole band",
                    "green SKIM tag",
                    "receive only",
                    "224.0.0.73",
                    "Port to 2237",
                    "DX cluster off",
                ),
            ),
        ):
            for phrase in banned:
                self.assertNotIn(phrase, visible, phrase)

    def test_unique_help_moved_into_tooltips(self):
        for phrase in (
            "Spots from a DX cluster, read only",
            "does not post spots",
            "drop off after 15 minutes",
            "Use WA1HCO-1, -2",
            "from a logger if WSJT-X has no call",
            "settings file",
            "once every five minutes",
            "All bands start on",
            "QSY on the Operate line",
        ):
            self.assertIn(phrase, self.cluster_tips, phrase)
        for phrase in (
            "WS Skim decodes the whole band",
            "green SKIM tag",
            "receive only",
            "does not transmit, change band, or turn an antenna",
            "Skim spots work with the DX cluster off",
            "Port to 2237",
            "224.0.0.73",
            "127.0.0.1 works only when WS Skim runs on the same PC",
            "own address also works as Host",
        ):
            self.assertIn(phrase, self.skim_tips, phrase)

    def test_headings_use_the_setup_tip_button(self):
        for block in (self.cluster, self.skim):
            self.assertIn('class="tipbtn"', block)
            self.assertIn('class="tiptxt"', block)
            self.assertIn('role="tooltip"', block)
            self.assertIn('type="button"', block)
        self.assertIn("text-transform: none", self.css)
        self.assertIn("#skim-server-hint:empty", self.css)
        self.assertIn("h2 .advtip:hover .tiptxt", self.css)
        self.assertIn(".hover-tip:hover .tiptxt", self.css)

    def test_live_server_hint_is_status_only(self):
        js = (STATIC / "wims.js").read_text(encoding="utf-8")
        self.assertIn("This site server is ", js)
        self.assertNotIn("Its address also works as Host.", js)
        self.assertIn("own address also works as Host", self.skim_tips)


if __name__ == "__main__":
    unittest.main()
