from __future__ import annotations

import logging
from typing import Any

from chance_seeker.collectors.http import HttpClient
from chance_seeker.collectors.x_client.base import Tweet, XClient, parse_time, to_int

log = logging.getLogger(__name__)

ENDPOINT = "https://api.socialdata.tools/twitter/search"
PAGE_SIZE = 20


class SocialDataClient(XClient):
    """socialdata.tools：同样按量计费，作为 twitterapi.io 的备选/冗余。"""

    provider = "socialdata"

    def __init__(self, api_key: str) -> None:
        super().__init__()
        self.http = HttpClient(
            "socialdata",
            rate_limit=100,
            period=60.0,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def search(self, query: str, since_ts: int, limit: int = 60) -> list[Tweet]:
        tweets: list[Tweet] = []
        cursor: str | None = None
        pages = max(1, min(5, -(-limit // PAGE_SIZE)))

        for _ in range(pages):
            params: dict[str, Any] = {"query": query, "type": "Latest"}
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
            cursor = payload.get("next_cursor")
            if stop or not cursor:
                break
        return tweets


def _parse(raw: Any) -> Tweet | None:
    if not isinstance(raw, dict):
        return None
    tweet_id = raw.get("id_str") or raw.get("id")
    if not tweet_id:
        return None
    user = raw.get("user") or {}
    return Tweet(
        id=str(tweet_id),
        text=str(raw.get("full_text") or raw.get("text") or ""),
        author=str(user.get("screen_name") or "").lower(),
        created_at=parse_time(raw.get("tweet_created_at") or raw.get("created_at")),
        likes=to_int(raw.get("favorite_count")),
        retweets=to_int(raw.get("retweet_count")),
        replies=to_int(raw.get("reply_count")),
        quotes=to_int(raw.get("quote_count")),
        views=to_int(raw.get("views_count")),
        author_followers=to_int(user.get("followers_count")),
    )
