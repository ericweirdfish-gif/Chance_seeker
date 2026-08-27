from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from chance_seeker.config import Config
from chance_seeker.models import Opportunity

log = logging.getLogger(__name__)


class AlertChannel(ABC):
    name: str = "channel"

    def __init__(self, settings: dict) -> None:
        self.settings = settings

    @abstractmethod
    def send(self, opportunity: Opportunity) -> None:
        """发送失败请抛异常，由 pipeline 记录到 alerts 表。"""

    def send_text(self, text: str) -> None:
        raise NotImplementedError


def build_channels(config: Config) -> list[AlertChannel]:
    from chance_seeker.alerts.console import ConsoleChannel
    from chance_seeker.alerts.discord import DiscordChannel
    from chance_seeker.alerts.telegram import TelegramChannel

    registry = {"console": ConsoleChannel, "telegram": TelegramChannel, "discord": DiscordChannel}
    channels: list[AlertChannel] = []
    for name, settings in (config.alerts or {}).items():
        settings = settings or {}
        if not settings.get("enabled"):
            continue
        cls = registry.get(name)
        if cls is None:
            log.warning("未知的告警渠道 %r，忽略", name)
            continue
        try:
            channels.append(cls(settings))
        except ValueError as exc:
            log.warning("告警渠道 %s 初始化失败：%s", name, exc)
    if not channels:
        log.warning("没有启用任何告警渠道，信号只会写入数据库")
    return channels
