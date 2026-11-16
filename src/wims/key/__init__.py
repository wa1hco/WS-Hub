# WS Hub
# Copyright (C) 2026 Jeff Millar, WA1HCO
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""WS Hub KEY entry: lab bench tools + ``daemon`` → ``wims.seat --key``."""

from __future__ import annotations

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    from wims.key.app import main as _main
    return _main(argv)
