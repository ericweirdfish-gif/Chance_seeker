from __future__ import annotations

import logging
from typing import Any

from chance_seeker.collectors.http import HttpClient
from chance_seeker.collectors.x_client.base import Tweet, XClient, parse_time, to_int

log = logging.getLogger(__name__)

ENDPOINT = "https://api.twitterapi.io/twitter/tweet/advanced_search"
PAGE_SIZE = 20  # 该接口每页约 20 条


class TwitterApiIoClient(XClient):
    """twitterapi.io：按量计费，约 $0.15 / 1000 条推文，是目前最便宜的可用方案。"""

    provider = "twitterapi_io"

    def __init__(self, api_key: str) -> None:
        super().__init__()
        self.http = HttpClient(
            "twitterapi_io", rate_limit=100, period=60.0, headers={"X-API-Key": api_key}
        )

    def search(self, query: str, since_ts: int, limit: int = 60) -> list[Tweet]:
        tweets: list[Tweet] = []
        cursor: str | None = None
        pages = max(1, min(5, -(-limit // PAGE_SIZE)))

        for _ in range(pages):
            params: dict[str, Any] = {"query": query, "queryType": "Latest"}
            if cursor:
                params["cursor"] = cursor
            payload = self.http.get_json(ENDPOINT, params=params)
            self.queries_used += 1
            if not isinstance(payload, dict):
                break

            batch = payload.get("tweets")
            if not isinstance(batch, list) or not batch:
                break

            stop = False
            for raw in batch:
                tweet = _parse(raw)
                if tweet is None:
                    continue
                if tweet.created_at and tweet.created_at < since_ts:
                    stop = True
                    continue
                tweets.append(tweet)
                if len(tweets) >= limit:
                    stop = True
                    break
            if stop or not payload.get("has_next_page"):
                break
            cursor = payload.get("next_cursor")
            if not cursor:
                break
        return tweets


def _parse(raw: Any) -> Tweet | None:
    if not isinstance(raw, dict):
        return None
    tweet_id = raw.get("id") or raw.get("id_str")
    if not tweet_id:
        return None
    author = raw.get("author") or {}
    return Tweet(
        id=str(tweet_id),
        text=str(raw.get("text") or ""),
        author=str(author.get("userName") or author.get("screen_name") or "").lower(),
        created_at=parse_time(raw.get("createdAt") or raw.get("created_at")),
        likes=to_int(raw.get("likeCount")),
        retweets=to_int(raw.get("retweetCount")),
        replies=to_int(raw.get("replyCount")),
        quotes=to_int(raw.get("quoteCount")),
        views=to_int(raw.get("viewCount")),
        author_followers=to_int(author.get("followers") or author.get("followersCount")),
    )
