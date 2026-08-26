from __future__ import annotations

import logging
import re

from chance_seeker.collectors.base import Collector, CollectResult, SchemaProbe
from chance_seeker.collectors.http import HttpClient
from chance_seeker.models import KIND_NARRATIVE, KIND_TOKEN, Entity, now_ts

log = logging.getLogger(__name__)

COINGECKO_TRENDING = "https://api.coingecko.com/api/v3/search/trending"
REDDIT_NEW = "https://www.reddit.com/r/{sub}/new.json"

# 常见英文词/主流币，避免把 $ETH、$ALL 之类当成小币提及
SYMBOL_STOPLIST = {"ETH", "BTC", "SOL", "USDT", "USDC", "BNB", "ALL", "THE", "AND", "FOR", "YOU", "NEW"}


class FreeAttentionCollector(Collector):
    """零成本的注意力代理信号。

    单独看每一个都比 X 弱，但它们完全免费、无需 key，而且和 X 的噪音结构不同，
    所以叠加起来能显著降低误报——尤其在 X 预算用尽的时候仍然有输出。
    """

    name = "free_attention"
    default_interval = 900

    def __init__(self, config, db) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config, db)
        self.http = HttpClient("free_attention", rate_limit=20, period=60.0)

    def schema_probes(self) -> list[SchemaProbe]:
        sources = {str(s).lower() for s in (self.settings.get("sources") or [])}
        probes = [
            SchemaProbe(
                title="CoinGecko /search/trending",
                url=COINGECKO_TRENDING,
                expected={
                    "coins[].item.symbol": "代号，用来挂回代币实体",
                    "coins[].item.id": "slug",
                    "coins[].item.name": "名称",
                },
                max_depth=4,
            ),
        ]
        # Reddit 默认关闭，没启用就别探——否则每次都因为 403 报红，
        # 报红变成常态之后，真正的问题就没人看了
        if "reddit" in sources:
            probes.append(
                SchemaProbe(
                    title="Reddit r/CryptoCurrency/new",
                    url=REDDIT_NEW.format(sub="CryptoCurrency"),
                    params={"limit": 5},
                    expected={
                        "data.children[].data.title": "标题，从里面抓 $SYMBOL",
                        "data.children[].data.selftext": "正文",
                    },
                    max_depth=5,
                )
            )
        return probes

    def collect(self) -> CollectResult:
        result = CollectResult()
        sources = {str(s).lower() for s in (self.settings.get("sources") or [])}

        if "coingecko_trending" in sources:
            result.extend(self._coingecko())
        if "reddit" in sources:
            result.extend(self._reddit())
        if "google_trends" in sources and self.settings.get("google_trends_keywords"):
            result.extend(self._google_trends())
        if "dexscreener_boosts" in sources:
            log.debug("[free_attention] dex_boosts 由 dexscreener 采集器产出，此处跳过")
        return result

    # -------------------------------------------------------- CoinGecko 热搜
    def _coingecko(self) -> CollectResult:
        result = CollectResult()
        payload = self.http.get_json(COINGECKO_TRENDING)
        if not isinstance(payload, dict):
            return result
        coins = payload.get("coins")
        if not isinstance(coins, list):
            return result

        ts = now_ts()
        symbol_index = self._symbol_index()
        scored: set[str] = set()

        for rank, wrapper in enumerate(coins):
            item = (wrapper or {}).get("item") if isinstance(wrapper, dict) else None
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").upper()
            # 排名越靠前分数越高：第 1 名 = 16 分，第 15 名 = 2 分
            trending_score = max(1.0, 16.0 - rank)

            slug = str(item.get("id") or symbol.lower())
            narrative_key = f"narrative:cg-{slug}"
            result.entities.append(
                Entity(
                    kind=KIND_NARRATIVE,
                    key=narrative_key,
                    name=item.get("name"),
                    symbol=symbol or None,
                    meta={"source": "coingecko_trending", "market_cap_rank": item.get("market_cap_rank")},
                )
            )
            for key in {narrative_key, *symbol_index.get(symbol, set())}:
                point = self.obs(key, "coingecko_trending_score", trending_score, ts)
                if point:
                    result.observations.append(point)
                    scored.add(key)

        # 关键一步：给「掉出榜单」的实体补一个 0。
        # 不补零的话，榜上每个币每轮都是同一个值，任何基于变化的规则都失效，
        # 而基于绝对值的规则会让 BTC / ETH 这种常驻榜单的币每轮都触发信号。
        # 补零之后，「进入榜单」才在时间序列上表现成 0 → 16 的跃升。
        for key in self.db.entities_with_metric("coingecko_trending_score", within_seconds=86_400):
            if key in scored:
                continue
            point = self.obs(key, "coingecko_trending_score", 0.0, ts)
            if point:
                result.observations.append(point)
        return result

    # ------------------------------------------------------------- Reddit
    def _reddit(self) -> CollectResult:
        result = CollectResult()
        subs = self.settings.get("reddit_subreddits") or []
        if not subs:
            return result

        symbol_index = self._symbol_index()
        if not symbol_index:
            return result

        counts: dict[str, int] = {}
        ts = now_ts()
        failures = 0
        for sub in subs:
            payload = self.http.get_json(REDDIT_NEW.format(sub=sub), params={"limit": 100})
            if not isinstance(payload, dict):
                failures += 1
                continue
            children = ((payload.get("data") or {}).get("children")) or []
            for child in children:
                post = (child or {}).get("data") if isinstance(child, dict) else None
                if not isinstance(post, dict):
                    continue
                text = f"{post.get('title') or ''} {post.get('selftext') or ''}"
                for symbol in _extract_symbols(text):
                    for entity_key in symbol_index.get(symbol, ()):
                        counts[entity_key] = counts.get(entity_key, 0) + 1

        if failures == len(subs):
            log.warning(
                "[free_attention] Reddit 全部 %d 个子版块请求失败。Reddit 会拦截数据中心 IP，"
                "在 GitHub Actions 和多数 VPS 上这个源不可用（本地家宽通常可以）。"
                "建议从 collectors.free_attention.sources 里去掉 reddit。",
                len(subs),
            )
            return result

        for entity_key, count in counts.items():
            point = self.obs(entity_key, "reddit_mentions", count, ts)
            if point:
                result.observations.append(point)
        return result

    # ------------------------------------------------------ Google Trends
    def _google_trends(self) -> CollectResult:
        result = CollectResult()
        keywords = [str(k) for k in (self.settings.get("google_trends_keywords") or []) if str(k).strip()]
        if not keywords:
            return result
        try:
            from pytrends.request import TrendReq
        except ImportError:
            log.warning('[free_attention] 需要 pip install "chance-seeker[trends]" 才能用 Google Trends')
            return result

        ts = now_ts()
        try:
            trends = TrendReq(hl="en-US", tz=0)
            # Google Trends 单次最多 5 个关键词
            for i in range(0, len(keywords), 5):
                batch = keywords[i : i + 5]
                trends.build_payload(batch, timeframe="now 7-d")
                frame = trends.interest_over_time()
                if frame is None or frame.empty:
                    continue
                for keyword in batch:
                    if keyword not in frame:
                        continue
                    slug = keyword.strip().lower().replace(" ", "-")
                    key = f"narrative:{slug}"
                    result.entities.append(
                        Entity(kind=KIND_NARRATIVE, key=key, name=keyword, meta={"source": "google_trends"})
                    )
                    point = self.obs(key, "google_trends_interest", float(frame[keyword].iloc[-1]), ts)
                    if point:
                        result.observations.append(point)
        except Exception as exc:  # pragma: no cover - pytrends 经常因为反爬变动而抛异常
            log.warning("[free_attention] Google Trends 采集失败: %s", exc)
        return result

    # ------------------------------------------------------------------
    def _symbol_index(self) -> dict[str, set[str]]:
        """symbol -> {entity_key}，用于把只有代号的外部信号挂回具体代币。"""
        rows = self.db.conn.execute(
            "SELECT key, symbol FROM entities WHERE kind = ? AND symbol IS NOT NULL AND last_seen >= ?",
            (KIND_TOKEN, now_ts() - 7 * 86400),
        ).fetchall()
        index: dict[str, set[str]] = {}
        for row in rows:
            symbol = str(row["symbol"]).upper()
            if not symbol or symbol in SYMBOL_STOPLIST:
                continue
            index.setdefault(symbol, set()).add(row["key"])
        return index


_SYMBOL_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,11})\b")


def _extract_symbols(text: str) -> set[str]:
    return {m.upper() for m in _SYMBOL_RE.findall(text or "")} - SYMBOL_STOPLIST

