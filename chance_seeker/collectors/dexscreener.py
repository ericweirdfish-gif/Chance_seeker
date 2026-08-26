from __future__ import annotations

import logging
from typing import Any

from chance_seeker.collectors.base import Collector, CollectResult
from chance_seeker.collectors.http import HttpClient
from chance_seeker.models import KIND_TOKEN, Entity, now_ts

log = logging.getLogger(__name__)

BASE = "https://api.dexscreener.com"
BATCH_SIZE = 30  # DexScreener 单次最多 30 个地址


class DexScreenerCollector(Collector):
    """DexScreener：免费、无需 key，是整个资金面的主数据源。

    职责：
      1. 从 token-profiles / token-boosts 发现新代币（顺带产出付费推广强度这一注意力代理指标）
      2. 对观察列表里的代币批量拉取价格 / 流动性 / 成交量 / 买卖笔数
    """

    name = "dexscreener"
    default_interval = 300

    def __init__(self, config, db) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config, db)
        # 官方限制：token-pairs 类 300/min，profiles/boosts 类 60/min，取保守值
        self.http = HttpClient("dexscreener", rate_limit=200, period=60.0)
        self._chain_map = {
            c.dexscreener_chain or c.name: c.name for c in config.enabled_chains()
        }

    # ------------------------------------------------------------------
    def collect(self) -> CollectResult:
        result = CollectResult()
        if self.settings.get("discover_from_profiles", True):
            result.extend(self._discover("/token-profiles/latest/v1", boosts=False))
        if self.settings.get("discover_from_boosts", True):
            result.extend(self._discover("/token-boosts/latest/v1", boosts=True))
            result.extend(self._discover("/token-boosts/top/v1", boosts=True))
        result.extend(self._refresh_watchlist())
        return result

    # ------------------------------------------------------- 发现新代币
    def _discover(self, path: str, boosts: bool) -> CollectResult:
        result = CollectResult()
        payload = self.http.get_json(f"{BASE}{path}")
        if not isinstance(payload, list):
            return result

        ts = now_ts()
        for item in payload:
            if not isinstance(item, dict):
                continue
            chain_raw = item.get("chainId")
            address = item.get("tokenAddress")
            if not chain_raw or not address:
                continue
            chain = self._chain_map.get(chain_raw)
            if chain is None:
                continue

            key = Entity.token_key(chain, address)
            result.entities.append(
                Entity(
                    kind=KIND_TOKEN,
                    key=key,
                    chain=chain,
                    address=address,
                    meta={
                        "dexscreener_url": item.get("url"),
                        "description": (item.get("description") or "")[:400] or None,
                        "links": item.get("links"),
                        "discovered_via": "boosts" if boosts else "profiles",
                    },
                )
            )
            if boosts:
                total = item.get("totalAmount") or item.get("amount")
                point = self.obs(key, "dex_boosts", total, ts)
                if point:
                    result.observations.append(point)
        return result

    # --------------------------------------------------- 刷新观察列表指标
    def _refresh_watchlist(self) -> CollectResult:
        result = CollectResult()
        watchlist = self._watchlist()
        by_chain: dict[str, list[str]] = {}
        for entity in watchlist:
            if entity.chain and entity.address:
                by_chain.setdefault(entity.chain, []).append(entity.address)

        for chain, addresses in by_chain.items():
            ds_chain = next(
                (k for k, v in self._chain_map.items() if v == chain), chain
            )
            for i in range(0, len(addresses), BATCH_SIZE):
                batch = addresses[i : i + BATCH_SIZE]
                payload = self.http.get_json(f"{BASE}/tokens/v1/{ds_chain}/{','.join(batch)}")
                pairs = _as_pairs(payload)
                if not pairs:
                    continue
                result.extend(self._pairs_to_result(chain, pairs))
        return result

    def _watchlist(self) -> list[Entity]:
        """按最近机会分排序取前 N 个代币，控制请求量。"""
        limit = int(self.settings.get("watchlist_size", 240))
        rows = self.db.conn.execute(
            """SELECT e.*, COALESCE(
                       (SELECT MAX(o.score) FROM opportunities o
                         WHERE o.entity_id = e.id AND o.ts >= ?), 0) AS recent_score
                 FROM entities e
                WHERE e.kind = ? AND e.address IS NOT NULL
                ORDER BY recent_score DESC, e.last_seen DESC
                LIMIT ?""",
            (now_ts() - 6 * 3600, KIND_TOKEN, limit),
        ).fetchall()
        enabled = {c.name for c in self.config.enabled_chains()}
        out = []
        for r in rows:
            if r["chain"] not in enabled:
                continue
            out.append(Entity(kind=r["kind"], key=r["key"], chain=r["chain"], address=r["address"]))
        return out

    # -------------------------------------------------------- 解析 pair
    def _pairs_to_result(self, chain: str, pairs: list[dict[str, Any]]) -> CollectResult:
        result = CollectResult()
        ts = now_ts()

        # 一个代币可能有多个池子，取流动性最高的那个作为代表，成交量则累加
        best: dict[str, dict[str, Any]] = {}
        totals: dict[str, dict[str, float]] = {}
        for pair in pairs:
            base = pair.get("baseToken") or {}
            address = base.get("address")
            if not address:
                continue
            key = Entity.token_key(chain, address)
            liq = _num(pair.get("liquidity", {}).get("usd"))
            agg = totals.setdefault(key, {"liquidity_usd": 0.0, "volume_24h": 0.0, "volume_1h": 0.0, "volume_5m": 0.0})
            agg["liquidity_usd"] += liq
            agg["volume_24h"] += _num((pair.get("volume") or {}).get("h24"))
            agg["volume_1h"] += _num((pair.get("volume") or {}).get("h1"))
            agg["volume_5m"] += _num((pair.get("volume") or {}).get("m5"))
            if key not in best or liq > _num((best[key].get("liquidity") or {}).get("usd")):
                best[key] = pair

        for key, pair in best.items():
            base = pair.get("baseToken") or {}
            agg = totals[key]
            created_ms = pair.get("pairCreatedAt")
            age_minutes = (ts - int(created_ms) / 1000) / 60 if created_ms else None

            result.entities.append(
                Entity(
                    kind=KIND_TOKEN,
                    key=key,
                    chain=chain,
                    address=base.get("address"),
                    symbol=base.get("symbol"),
                    name=base.get("name"),
                    meta={
                        "dexscreener_url": pair.get("url"),
                        "dex_id": pair.get("dexId"),
                        "pair_address": pair.get("pairAddress"),
                        "pair_created_at": created_ms,
                    },
                )
            )

            txns = pair.get("txns") or {}
            h1 = txns.get("h1") or {}
            buys, sells = _num(h1.get("buys")), _num(h1.get("sells"))
            price_change = pair.get("priceChange") or {}

            points: dict[str, Any] = {
                "price_usd": pair.get("priceUsd"),
                "liquidity_usd": agg["liquidity_usd"],
                "volume_24h": agg["volume_24h"],
                "volume_1h": agg["volume_1h"],
                "volume_5m": agg["volume_5m"],
                "market_cap_usd": pair.get("marketCap") or pair.get("fdv"),
                "price_change_5m": price_change.get("m5"),
                "price_change_1h": price_change.get("h1"),
                "price_change_24h": price_change.get("h24"),
                "txns_1h": buys + sells,
                "txns_buys_1h": buys,
                "txns_sells_1h": sells,
                "buy_sell_ratio_1h": buys / sells if sells > 0 else (buys if buys else None),
                "age_minutes": age_minutes,
            }
            if agg["liquidity_usd"] > 0:
                points["vol_liq_ratio_1h"] = agg["volume_1h"] / agg["liquidity_usd"]

            for metric, value in points.items():
                point = self.obs(key, metric, value, ts)
                if point:
                    result.observations.append(point)
        return result


def _as_pairs(payload: Any) -> list[dict[str, Any]]:
    """/tokens/v1 返回数组，/latest/dex/* 返回 {"pairs": [...]}，统一成数组。"""
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        pairs = payload.get("pairs")
        if isinstance(pairs, list):
            return [p for p in pairs if isinstance(p, dict)]
    return []


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
