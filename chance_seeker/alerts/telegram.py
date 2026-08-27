from __future__ import annotations

import logging

from chance_seeker.alerts.base import AlertChannel
from chance_seeker.alerts.renderer import render_markdown
from chance_seeker.collectors.http import HttpClient
from chance_seeker.models import Opportunity

log = logging.getLogger(__name__)

MAX_LEN = 4000  # Telegram 单条消息上限 4096，留点余量


class TelegramChannel(AlertChannel):
    """Telegram Bot 推送。免费、手机即时到达，最适合这类实时信号。

    配置步骤：@BotFather 建 bot 拿 token → 给 bot 发一句话 →
    访问 https://api.telegram.org/bot<TOKEN>/getUpdates 拿 chat_id。
    """

    name = "telegram"

    def __init__(self, settings: dict) -> None:
        super().__init__(settings)
        self.token = str(settings.get("bot_token") or "").strip()
        self.chat_id = str(settings.get("chat_id") or "").strip()
        if not self.token or not self.chat_id:
            raise ValueError("缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID")
        self.http = HttpClient("telegram", rate_limit=20, period=60.0)

    def send(self, opportunity: Opportunity) -> None:
        self.send_text(render_markdown(opportunity))

    def send_text(self, text: str) -> None:
        payload = {
            "chat_id": self.chat_id,
            "text": text[:MAX_LEN],
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        response = self.http.post_json(
            f"https://api.telegram.org/bot{self.token}/sendMessage", json_body=payload
        )
        if not isinstance(response, dict) or not response.get("ok"):
            # Markdown 里的特殊字符（代币名常有）可能导致解析失败，退化为纯文本重发
            log.warning("Telegram Markdown 发送失败，改用纯文本重试")
            payload.pop("parse_mode")
            response = self.http.post_json(
                f"https://api.telegram.org/bot{self.token}/sendMessage", json_body=payload
            )
            if not isinstance(response, dict) or not response.get("ok"):
                raise RuntimeError(f"Telegram 发送失败: {response}")
