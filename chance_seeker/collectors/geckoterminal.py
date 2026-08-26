from __future__ import annotations

import logging
from typing import Any

from chance_seeker.collectors.base import Collector, CollectResult
from chance_seeker.collectors.http import HttpClient
from chance_seeker.models import KIND_TOKEN, Entity, now_ts

log = logging.getLogger(__name__)

BASE = "https://api.geckoterminal.com/api/v2"


class GeckoTerminalCollector(Collector):
    """GeckoTerminal：免费 30 次/分钟，用来发现「新池」和「趋势池」。

    DexScreener 负责已知代币的深度指标，这里负责把新出生的池子灌进观察列表，
    两者互补，都不要钱。
    """

    name = "geckoterminal"
    default_interval = 300

    def __init__(self, config, db) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config, db)
        self.http = HttpClient(
            "geckoterminal",
            rate_limit=25,
            period=60.0,
            headers={"Accept": "application/json;version=20230302"},
        )

    def collect(self) -> CollectResult:
        result = CollectResult()
        new_pages = int(self.settings.get("new_pools_pages", 1))
        trend_pages = int(self.settings.get("trending_pools_pages", 1))

        for chain in self.config.enabled_chains():
            network = chain.geckoterminal_network or chain.name
            for page in range(1, new_pages + 1):
                result.extend(self._fetch(chain.name, network, "new_pools", page))
            for page in range(1, trend_pages + 1):
                result.extend(self._fetch(chain.name, network, "trending_pools", page))
        return result

    def _fetch(self, chain: str, network: str, endpoint: str, page: int) -> CollectResult:
        result = CollectResult()
        payload = self.http.get_json(f"{BASE}/networks/{network}/{endpoint}", params={"page": page})
        if not isinstance(payload, dict):
            return result
        data = payload.get("data")
        if not isinstance(data, list):
            return result

        ts = now_ts()
        for pool in data:
            if not isinstance(pool, dict):
                continue
            attrs = pool.get("attributes") or {}
            address = _base_token_address(pool, network)
            if not address:
                continue

            key = Entity.token_key(chain, address)
            name = attrs.get("name") or ""
            symbol = name.split("/")[0].strip() or None

            result.entities.append(
                Entity(
                    kind=KIND_TOKEN,
                    key=key,
                    chain=chain,
                    address=address,
                    symbol=symbol,
                    meta={
                        "gt_pool_address": attrs.get("address"),
                        "pool_created_at": attrs.get("pool_created_at"),
                        "discovered_via": endpoint,
                    },
                )
            )

            volume = attrs.get("volume_usd") or {}
            txns = attrs.get("transactions") or {}
            h1 = txns.get("h1") or {}
            buyers, sellers = _num(h1.get("buyers")), _num(h1.get("sellers"))

            points: dict[str, Any] = {
                "gt_reserve_usd": attrs.get("reserve_in_usd"),
                "gt_volume_1h": volume.get("h1"),
                "gt_volume_24h": volume.get("h24"),
                "gt_fdv_usd": attrs.get("fdv_usd"),
                "unique_buyers_1h": buyers,
                "unique_sellers_1h": sellers,
                "buyer_seller_ratio_1h": buyers / sellers if sellers > 0 else (buyers if buyers else None),
            }
            if endpoint == "trending_pools":
                points["gt_trending"] = 1.0

            for metric, value in points.items():
                point = self.obs(key, metric, value, ts)
                if point:
                    result.observations.append(point)
        return result


def _base_token_address(pool: dict[str, Any], network: str) -> str | None:
    rel = ((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}
    raw = rel.get("id")
    if not isinstance(raw, str):
        return None
    prefix = f"{network}_"
    return raw[len(prefix) :] if raw.startswith(prefix) else raw


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
