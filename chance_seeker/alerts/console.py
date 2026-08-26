from __future__ import annotations

import sys

from chance_seeker.alerts.base import AlertChannel
from chance_seeker.alerts.renderer import render_plain
from chance_seeker.models import Opportunity

SEPARATOR = "─" * 72


class ConsoleChannel(AlertChannel):
    """终端输出。零配置，本地跑的时候最直接。"""

    name = "console"

    def send(self, opportunity: Opportunity) -> None:
        self.send_text(render_plain(opportunity))

    def send_text(self, text: str) -> None:
        print(f"\n{SEPARATOR}\n{text}\n{SEPARATOR}", file=sys.stdout, flush=True)
