from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from chance_seeker.collectors.http import HttpClient
from chance_seeker.collectors.x_client.base import Tweet, XClient, parse_time, to_int

log = logging.getLogger(__name__)

ENDPOINT = "https://api.x.com/2/tweets/search/recent"


class OfficialV2Client(XClient):
    """官方 X API v2 recent search（Basic 档 $200/月）。

    额度很小，务必配合 x_attention 的查询预算闸门使用。
    """

    provider = "official_v2"

    def __init__(self, bearer_token: str) -> None:
        super().__init__()
        self.http = HttpClient(
            "x_official",
            rate_limit=55,
            period=900.0,  # Basic 档 recent search 约 60 次 / 15 分钟
            headers={"Authorization": f"Bearer {bearer_token}"},
        )

    def search(self, query: str, since_ts: int, limit: int = 60) -> list[Tweet]:
        start_time = (
            datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if since_ts
            else None
        )
        params: dict[str, Any] = {
            "query": query,
            "max_results": max(10, min(100, limit)),
            "tweet.fields": "created_at,public_metrics,author_id",
            "expansions": "author_id",
            "user.fields": "username,public_metrics",
        }
        if start_time:
            params["start_time"] = start_time

        payload = self.http.get_json(ENDPOINT, params=params)
        self.queries_used += 1
        if not isinstance(payload, dict):
            return []

        users: dict[str, dict[str, Any]] = {}
        for user in ((payload.get("includes") or {}).get("users") or []):
            if isinstance(user, dict) and user.get("id"):
                users[str(user["id"])] = user

        tweets: list[Tweet] = []
        for raw in payload.get("data") or []:
            if not isinstance(raw, dict):
                continue
            metrics = raw.get("public_metrics") or {}
            user = users.get(str(raw.get("author_id") or ""), {})
            tweets.append(
                Tweet(
                    id=str(raw.get("id")),
                    text=str(raw.get("text") or ""),
                    author=str(user.get("username") or "").lower(),
                    created_at=parse_time(raw.get("created_at")),
                    likes=to_int(metrics.get("like_count")),
                    retweets=to_int(metrics.get("retweet_count")),
                    replies=to_int(metrics.get("reply_count")),
                    quotes=to_int(metrics.get("quote_count")),
                    views=to_int(metrics.get("impression_count")),
                    author_followers=to_int((user.get("public_metrics") or {}).get("followers_count")),
                )
            )
        return tweets
