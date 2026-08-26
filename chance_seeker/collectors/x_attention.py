from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from chance_seeker.collectors.base import Collector, CollectResult
from chance_seeker.collectors.x_client import build_client
from chance_seeker.models import KIND_NARRATIVE, KIND_TOKEN, Entity, now_ts

log = logging.getLogger(__name__)


@dataclass(slots=True)
class QueryTarget:
    entity_key: str
    query: str
    priority: float
    entity: Entity | None = None


class QueryBudget:
    """按量计费的成本闸门。

    三层上限（单轮 / 每小时 / 每天）都存在 SQLite 里，重启和 GitHub Actions
    的多次冷启动之间也能正确累计，避免半夜被账单叫醒。
    """

    def __init__(self, db, settings: dict) -> None:  # type: ignore[no-untyped-def]
        self.db = db
        self.per_run = int(settings.get("max_queries_per_run", 25))
        self.per_hour = int(settings.get("max_queries_per_hour", 80))
        self.per_day = int(settings.get("max_queries_per_day", 1200))

    @staticmethod
    def _keys() -> tuple[str, str]:
        now = time.gmtime()
        return (
            f"x_budget:hour:{time.strftime('%Y%m%d%H', now)}",
            f"x_budget:day:{time.strftime('%Y%m%d', now)}",
        )

    def remaining(self) -> int:
        hour_key, day_key = self._keys()
        used_hour = int(self.db.kv_get(hour_key, 0) or 0)
        used_day = int(self.db.kv_get(day_key, 0) or 0)
        return max(0, min(self.per_run, self.per_hour - used_hour, self.per_day - used_day))

    def consume(self, count: int) -> None:
        if count <= 0:
            return
        hour_key, day_key = self._keys()
        self.db.kv_set(hour_key, int(self.db.kv_get(hour_key, 0) or 0) + count)
        self.db.kv_set(day_key, int(self.db.kv_get(day_key, 0) or 0) + count)

    def usage(self) -> dict[str, int]:
        hour_key, day_key = self._keys()
        return {
            "hour_used": int(self.db.kv_get(hour_key, 0) or 0),
            "hour_limit": self.per_hour,
            "day_used": int(self.db.kv_get(day_key, 0) or 0),
            "day_limit": self.per_day,
        }


class XAttentionCollector(Collector):
    """X 注意力采集。

    查询目标有两类：
      1. 资金面已经有异动的代币 —— 用合约地址做精确检索，几乎没有同名噪音，
         而且「有人在推 CA」本身就是极早期信号
      2. 配置里的叙事关键词 —— 用来捕捉板块级注意力轮动

    每个目标产出四个指标：提及量、独立作者数、KOL 提及数、互动量。
    其中 *独立作者数* 最重要：单人刷 100 条和 50 个人各发 1 条，
    前者是噪音，后者才是扩散。
    """

    name = "x_attention"
    default_interval = 900

    def __init__(self, config, db) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config, db)
        self.client = build_client(self.settings)
        self.budget = QueryBudget(db, self.settings)
        self.kols = {str(k).lstrip("@").lower() for k in (self.settings.get("kols") or [])}

    def preflight(self) -> str | None:
        if not self.client.available():
            return f"X 数据源不可用（provider={self.settings.get('provider')!r}，检查 X_API_KEY）"
        return None

    def collect(self) -> CollectResult:
        result = CollectResult()
        allowance = self.budget.remaining()
        if allowance <= 0:
            log.info("[x_attention] 查询预算已用尽，本轮跳过。%s", self.budget.usage())
            return result

        targets = self._targets()[:allowance]
        if not targets:
            return result

        since = now_ts() - int(self.settings.get("lookback_minutes", 60)) * 60
        per_query = int(self.settings.get("tweets_per_query", 60))
        before = self.client.queries_used

        for target in targets:
            tweets = self.client.search(target.query, since_ts=since, limit=per_query)
            if target.entity is not None:
                result.entities.append(target.entity)
            result.extend(self._measure(target.entity_key, tweets))

        spent = self.client.queries_used - before
        self.budget.consume(spent)
        result.notes["x_queries"] = spent
        result.notes["x_budget"] = self.budget.usage()
        log.info("[x_attention] 本轮 %d 个查询，预算 %s", spent, self.budget.usage())
        return result

    # ------------------------------------------------------------------
    def _measure(self, entity_key: str, tweets: list) -> CollectResult:
        result = CollectResult()
        ts = now_ts()
        authors = {t.author for t in tweets if t.author}
        kol_hits = authors & self.kols if self.kols else set()

        points = {
            "x_mentions": len(tweets),
            "x_unique_authors": len(authors),
            "x_kol_mentions": len(kol_hits),
            "x_engagement": sum(t.engagement for t in tweets),
            "x_reach": sum(t.author_followers for t in tweets),
        }
        for metric, value in points.items():
            point = self.obs(entity_key, metric, value, ts)
            if point:
                result.observations.append(point)
        if kol_hits:
            result.notes.setdefault("kol_hits", {})[entity_key] = sorted(kol_hits)
        return result

    def _targets(self) -> list[QueryTarget]:
        targets: list[QueryTarget] = []

        # 叙事关键词优先级固定较高：便宜且信息量稳定
        for keyword in self.settings.get("keywords") or []:
            slug = str(keyword).strip().lower().replace(" ", "-")
            if not slug:
                continue
            key = f"narrative:{slug}"
            targets.append(
                QueryTarget(
                    entity_key=key,
                    query=str(keyword),
                    priority=1_000_000,
                    entity=Entity(kind=KIND_NARRATIVE, key=key, name=str(keyword), meta={"source": "x_keyword"}),
                )
            )

        top_n = int(self.settings.get("auto_cashtag_top_n", 20))
        mode = str(self.settings.get("token_query_mode", "address")).lower()
        for row in self._hot_tokens(top_n):
            query = _token_query(row["address"], row["symbol"], mode)
            if not query:
                continue
            targets.append(
                QueryTarget(
                    entity_key=row["key"],
                    query=query,
                    priority=float(row["priority"]),
                    entity=Entity(
                        kind=KIND_TOKEN,
                        key=row["key"],
                        chain=row["chain"],
                        address=row["address"],
                        symbol=row["symbol"],
                    ),
                )
            )

        targets.sort(key=lambda t: t.priority, reverse=True)
        return targets

    def _hot_tokens(self, limit: int) -> list[dict]:
        """挑选资金面最热的代币来花 X 的钱——预算永远优先给已经有资金异动的标的。"""
        window = now_ts() - 6 * 3600
        rows = self.db.conn.execute(
            """SELECT e.key, e.chain, e.address, e.symbol,
                      COALESCE((SELECT SUM(s.score) FROM signals s
                                 WHERE s.entity_id = e.id AND s.family = 'capital' AND s.ts >= ?), 0)
                      + COALESCE((SELECT MAX(o.score) FROM opportunities o
                                   WHERE o.entity_id = e.id AND o.ts >= ?), 0) AS priority
                 FROM entities e
                WHERE e.kind = ? AND e.address IS NOT NULL AND e.last_seen >= ?
                ORDER BY priority DESC, e.last_seen DESC
                LIMIT ?""",
            (window, window, KIND_TOKEN, window, limit),
        ).fetchall()
        return [dict(r) for r in rows if float(r["priority"]) > 0]


def _token_query(address: str | None, symbol: str | None, mode: str) -> str | None:
    cashtag = f"${symbol}" if symbol and symbol.isalnum() and len(symbol) <= 12 else None
    if mode == "cashtag":
        return cashtag
    if mode == "both" and address and cashtag:
        return f"{address} OR {cashtag}"
    return address or cashtag
