from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Tweet:
    id: str
    text: str
    author: str
    created_at: int
    likes: int = 0
    retweets: int = 0
    replies: int = 0
    quotes: int = 0
    views: int = 0
    author_followers: int = 0

    @property
    def engagement(self) -> int:
        return self.likes + self.retweets * 2 + self.replies + self.quotes * 2


class XClient(ABC):
    """X 数据源适配层。

    换供应商只需要换这一个类的实现，上层采集与打分逻辑完全不用动。
    ``queries_used`` 用于成本核算——按量计费的第三方 API 全靠它兜底。
    """

    provider: str = "null"

    def __init__(self) -> None:
        self.queries_used = 0

    @abstractmethod
    def search(self, query: str, since_ts: int, limit: int = 60) -> list[Tweet]:
        """按时间倒序返回 since_ts 之后的推文。失败返回空列表，不抛异常。"""

    def available(self) -> bool:
        return True


class NullXClient(XClient):
    """未配置 X 数据源时的空实现，让整条流水线仍然能跑（只用免费信号）。"""

    provider = "null"

    def search(self, query: str, since_ts: int, limit: int = 60) -> list[Tweet]:
        return []

    def available(self) -> bool:
        return False


def build_client(settings: dict[str, Any]) -> XClient:
    provider = str(settings.get("provider") or "null").strip().lower()
    api_key = str(settings.get("api_key") or "").strip()
    bearer = str(settings.get("bearer_token") or "").strip()

    if provider in ("twitterapi_io", "twitterapi", "twitterapiio"):
        from chance_seeker.collectors.x_client.twitterapi_io import TwitterApiIoClient

        if not api_key:
            log.warning("X provider=twitterapi_io 但 X_API_KEY 为空，退化为 null")
            return NullXClient()
        return TwitterApiIoClient(api_key)

    if provider in ("socialdata", "socialdata_tools"):
        from chance_seeker.collectors.x_client.socialdata import SocialDataClient

        if not api_key:
            log.warning("X provider=socialdata 但 X_API_KEY 为空，退化为 null")
            return NullXClient()
        return SocialDataClient(api_key)

    if provider in ("official_v2", "official", "x_api"):
        from chance_seeker.collectors.x_client.official_v2 import OfficialV2Client

        token = bearer or api_key
        if not token:
            log.warning("X provider=official_v2 但 X_BEARER_TOKEN 为空，退化为 null")
            return NullXClient()
        return OfficialV2Client(token)

    if provider not in ("null", "none", ""):
        log.warning("未知的 X provider=%r，退化为 null", provider)
    return NullXClient()


# ------------------------------------------------------------------ 工具函数
def parse_time(raw: Any) -> int:
    """兼容 ISO8601 / Twitter 旧式 / 纯时间戳三种格式。"""
    if raw in (None, ""):
        return 0
    if isinstance(raw, (int, float)):
        value = float(raw)
        return int(value / 1000 if value > 1e11 else value)
    text = str(raw).strip()
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        pass
    try:
        return int(parsedate_to_datetime(text).timestamp())
    except (TypeError, ValueError):
        pass
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp())
        except ValueError:
            continue
    log.debug("无法解析时间: %r", raw)
    return 0


def to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

