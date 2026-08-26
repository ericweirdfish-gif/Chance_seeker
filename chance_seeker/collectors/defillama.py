from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from chance_seeker.collectors.base import Collector, CollectResult, SchemaProbe
from chance_seeker.collectors.http import HttpClient
from chance_seeker.models import KIND_CHAIN, KIND_NARRATIVE, Entity, now_ts

log = logging.getLogger(__name__)


class DefiLlamaCollector(Collector):
    """DefiLlama：免费无 key，提供跨链宏观资金流。

    产出三类实体的指标：
      - chain:<name>        链 TVL、链上稳定币规模（钱有没有在往这条链搬）
      - narrative:<category> 按赛道聚合的 TVL 与 1d/7d 变化（叙事轮动）
      - chain:global        全网稳定币总量（增量 = 新钱进场）
    """

    name = "defillama"
    default_interval = 3600

    def __init__(self, config, db) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config, db)
        self.http = HttpClient("defillama", rate_limit=30, period=60.0, timeout=45.0)

    def schema_probes(self) -> list[SchemaProbe]:
        return [
            SchemaProbe(
                title="DefiLlama /v2/chains",
                url="https://api.llama.fi/v2/chains",
                expected={"[].name": "链名", "[].tvl": "链 TVL -> chain_tvl"},
                max_depth=3,
            ),
            SchemaProbe(
                title="DefiLlama /stablecoinchains",
                url="https://stablecoins.llama.fi/stablecoinchains",
                expected={"[].name": "链名", "[].totalCirculatingUSD": "稳定币流通量 -> chain_stablecoins"},
                max_depth=3,
            ),
        ]

    def collect(self) -> CollectResult:
        result = CollectResult()
        result.extend(self._chains())
        result.extend(self._stablecoins())
        result.extend(self._narratives())
        return result

    def _chains(self) -> CollectResult:
        result = CollectResult()
        payload = self.http.get_json("https://api.llama.fi/v2/chains")
        if not isinstance(payload, list):
            return result
        ts = now_ts()
        total = 0.0
        for item in payload:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").lower()
            tvl = _num(item.get("tvl"))
            total += tvl
            if not name:
                continue
            key = f"chain:{name}"
            result.entities.append(Entity(kind=KIND_CHAIN, key=key, chain=name, name=item.get("name")))
            point = self.obs(key, "chain_tvl", tvl, ts)
            if point:
                result.observations.append(point)

        result.entities.append(Entity(kind=KIND_CHAIN, key="chain:global", name="Global"))
        point = self.obs("chain:global", "total_tvl", total, ts)
        if point:
            result.observations.append(point)
        return result

    def _stablecoins(self) -> CollectResult:
        result = CollectResult()
        payload = self.http.get_json("https://stablecoins.llama.fi/stablecoinchains")
        if not isinstance(payload, list):
            return result
        ts = now_ts()
        total = 0.0
        for item in payload:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").lower()
            circ = item.get("totalCirculatingUSD") or {}
            amount = sum(_num(v) for v in circ.values()) if isinstance(circ, dict) else 0.0
            total += amount
            if not name or amount <= 0:
                continue
            key = f"chain:{name}"
            result.entities.append(Entity(kind=KIND_CHAIN, key=key, chain=name, name=item.get("name")))
            point = self.obs(key, "chain_stablecoins", amount, ts)
            if point:
                result.observations.append(point)

        result.entities.append(Entity(kind=KIND_CHAIN, key="chain:global", name="Global"))
        point = self.obs("chain:global", "total_stablecoins", total, ts)
        if point:
            result.observations.append(point)
        return result

    def _narratives(self) -> CollectResult:
        """按赛道聚合协议 TVL，用来看叙事轮动。"""
        result = CollectResult()
        payload = self.http.get_json("https://api.llama.fi/protocols")
        if not isinstance(payload, list):
            return result

        ts = now_ts()
        tvl_by_cat: dict[str, float] = defaultdict(float)
        change_by_cat: dict[str, list[float]] = defaultdict(list)
        for item in payload:
            if not isinstance(item, dict):
                continue
            category = str(item.get("category") or "").strip()
            tvl = _num(item.get("tvl"))
            if not category or tvl <= 0:
                continue
            tvl_by_cat[category] += tvl
            change_1d = item.get("change_1d")
            if isinstance(change_1d, (int, float)):
                change_by_cat[category].append(float(change_1d))

        for category, tvl in tvl_by_cat.items():
            slug = category.lower().replace(" ", "-")
            key = f"narrative:{slug}"
            result.entities.append(
                Entity(kind=KIND_NARRATIVE, key=key, name=category, meta={"source": "defillama"})
            )
            for metric, value in (
                ("narrative_tvl", tvl),
                (
                    "narrative_change_1d",
                    sum(change_by_cat[category]) / len(change_by_cat[category])
                    if change_by_cat[category]
                    else None,
                ),
            ):
                point = self.obs(key, metric, value, ts)
                if point:
                    result.observations.append(point)
        return result


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
