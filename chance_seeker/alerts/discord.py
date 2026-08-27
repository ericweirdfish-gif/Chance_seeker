from __future__ import annotations

from chance_seeker.alerts.base import AlertChannel
from chance_seeker.alerts.renderer import links_for, render_markdown, short_name
from chance_seeker.models import Opportunity

MAX_LEN = 1900  # Discord embed description 上限 4096，content 上限 2000


class DiscordChannel(AlertChannel):
    """Discord Webhook 推送。只需要一个 webhook URL，没有别的配置。"""

    name = "discord"

    def __init__(self, settings: dict) -> None:
        super().__init__(settings)
        self.webhook_url = str(settings.get("webhook_url") or "").strip()
        if not self.webhook_url:
            raise ValueError("缺少 DISCORD_WEBHOOK_URL")
        from chance_seeker.collectors.http import HttpClient

        self.http = HttpClient("discord", rate_limit=25, period=60.0)

    def send(self, opportunity: Opportunity) -> None:
        entity = opportunity.entity
        title = f"{short_name(entity)} — {opportunity.score:.0f} 分"
        links = links_for(opportunity)
        embed = {
            "title": title[:250],
            "description": render_markdown(opportunity)[:MAX_LEN],
            "color": _color(opportunity.score),
            "url": links.get("DexScreener"),
            "timestamp": None,
        }
        embed = {k: v for k, v in embed.items() if v is not None}
        self._post({"embeds": [embed]})

    def send_text(self, text: str) -> None:
        self._post({"content": text[:MAX_LEN]})

    def _post(self, payload: dict) -> None:
        # Discord webhook 成功时返回 204 空响应，所以按状态码判断而不是按响应体
        status = self.http.post_status(self.webhook_url, json_body=payload)
        if status is None or status >= 400:
            raise RuntimeError(f"Discord 发送失败: HTTP {status}")


def _color(score: float) -> int:
    if score >= 85:
        return 0xE5484D  # 红
    if score >= 70:
        return 0xF5A524  # 橙
    return 0x3E63DD  # 蓝
