from __future__ import annotations

import logging
from typing import Any

from chance_seeker.collectors.base import Collector, CollectResult
from chance_seeker.collectors.http import HttpClient
from chance_seeker.models import KIND_TOKEN, Entity, now_ts

log = logging.getLogger(__name__)

ETHERSCAN_V2 = "https://api.etherscan.io/v2/api"


class EvmWalletCollector(Collector):
    """EVM 聪明钱监控（Etherscan V2 统一接口，一个免费 key 覆盖全部 EVM 链）。

    做法是拉取被观察地址最近的 ERC-20 转账，把「转入」当作建仓信号。
    单个地址买入意义有限，真正值钱的是 *多个* 独立聪明钱在同一时间窗内
    买同一个代币 —— 这个聚合在 ``smart_money_buyers`` 指标里给出。
    """

    name = "evm_wallets"
    default_interval = 600

    def __init__(self, config, db) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config, db)
        # Etherscan 免费档 5 次/秒，这里保守到 4 次/秒
        self.http = HttpClient("evm_wallets", rate_limit=240, period=60.0)

    def preflight(self) -> str | None:
        if not self.settings.get("api_key"):
            return "缺少 ETHERSCAN_API_KEY"
        if not self.settings.get("wallets"):
            return "未配置任何监控钱包（collectors.evm_wallets.wallets）"
        return None

    def collect(self) -> CollectResult:
        result = CollectResult()
        api_key = str(self.settings.get("api_key") or "")
        lookback = int(self.settings.get("lookback_minutes", 90)) * 60
        cutoff = now_ts() - lookback

        chain_ids = {c.name: c.chain_id for c in self.config.enabled_chains() if c.chain_id}

        for entry in self.settings.get("wallets") or []:
            address = str(entry.get("address") or "").strip()
            if not address:
                continue
            label = str(entry.get("label") or address[:10])
            targets = entry.get("chains") or list(chain_ids)
            for chain in targets:
                chain_id = chain_ids.get(chain)
                if chain_id is None:
                    continue
                result.extend(self._wallet_chain(address, label, chain, chain_id, api_key, cutoff))

        result.extend(self._aggregate())
        return result

    def _wallet_chain(
        self, address: str, label: str, chain: str, chain_id: int, api_key: str, cutoff: int
    ) -> CollectResult:
        result = CollectResult()
        payload = self.http.get_json(
            ETHERSCAN_V2,
            params={
                "chainid": chain_id,
                "module": "account",
                "action": "tokentx",
                "address": address,
                "page": 1,
                "offset": 100,
                "sort": "desc",
                "apikey": api_key,
            },
        )
        if not isinstance(payload, dict):
            return result
        if str(payload.get("status")) != "1":
            message = str(payload.get("message") or "")
            if "No transactions found" not in message:
                log.debug("[evm_wallets] %s/%s: %s", label, chain, message or payload.get("result"))
            return result

        rows = payload.get("result")
        if not isinstance(rows, list):
            return result

        wallet_lower = address.lower()
        for tx in rows:
            if not isinstance(tx, dict):
                continue
            ts = _int(tx.get("timeStamp"))
            if ts < cutoff:
                break  # sort=desc，遇到过期的就可以停
            token_address = str(tx.get("contractAddress") or "")
            if not token_address:
                continue
            to_addr = str(tx.get("to") or "").lower()
            from_addr = str(tx.get("from") or "").lower()
            if to_addr == wallet_lower:
                direction = "in"
            elif from_addr == wallet_lower:
                direction = "out"
            else:
                continue

            token_key = Entity.token_key(chain, token_address)
            result.entities.append(
                Entity(
                    kind=KIND_TOKEN,
                    key=token_key,
                    chain=chain,
                    address=token_address,
                    symbol=tx.get("tokenSymbol"),
                    name=tx.get("tokenName"),
                    meta={"discovered_via": "smart_money"},
                )
            )
            self.db.record_wallet_event(
                wallet=address,
                label=label,
                chain=chain,
                token_key=token_key,
                symbol=tx.get("tokenSymbol"),
                direction=direction,
                amount=_amount(tx.get("value"), tx.get("tokenDecimal")),
                tx_hash=tx.get("hash"),
                ts=ts,
            )
        return result

    def _aggregate(self) -> CollectResult:
        """把钱包事件聚合成 smart_money_buyers 指标。"""
        return aggregate_smart_money(self.db, self, int(self.settings.get("lookback_minutes", 90)))


def aggregate_smart_money(db, collector: Collector, lookback_minutes: int) -> CollectResult:
    """共用聚合逻辑：统计每个代币在时间窗内有多少个独立聪明钱地址买入。"""
    result = CollectResult()
    window = lookback_minutes * 60
    ts = now_ts()
    rows = db.conn.execute(
        """SELECT token_key,
                  COUNT(DISTINCT CASE WHEN direction = 'in'  THEN wallet END) AS buyers,
                  COUNT(DISTINCT CASE WHEN direction = 'out' THEN wallet END) AS sellers
             FROM wallet_events
            WHERE ts >= ?
            GROUP BY token_key""",
        (ts - window,),
    ).fetchall()
    for row in rows:
        for metric, value in (
            ("smart_money_buyers", row["buyers"]),
            ("smart_money_sellers", row["sellers"]),
            ("smart_money_net", (row["buyers"] or 0) - (row["sellers"] or 0)),
        ):
            point = collector.obs(row["token_key"], metric, value, ts)
            if point:
                result.observations.append(point)
    return result


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _amount(raw: Any, decimals: Any) -> float:
    try:
        return int(raw) / (10 ** int(decimals or 0))
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0
