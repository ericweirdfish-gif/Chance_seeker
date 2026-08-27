from __future__ import annotations

import logging

from chance_seeker.collectors.base import Collector, CollectResult
from chance_seeker.collectors.evm_wallets import aggregate_smart_money
from chance_seeker.collectors.http import HttpClient
from chance_seeker.models import KIND_TOKEN, Entity, now_ts

log = logging.getLogger(__name__)

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# 余额变动小于这个比例的忽略，避免把 dust / 手续费当成建仓
MIN_CHANGE_RATIO = 0.02


class SolanaWalletCollector(Collector):
    """Solana 聪明钱监控：快照代币余额并做差分。

    为什么不解析交易？因为 ``getSignaturesForAddress`` + ``getTransaction``
    对每个地址都要几十次 RPC 调用，免费额度撑不住。而
    ``getTokenAccountsByOwner`` 一次调用就能拿到某地址全部代币余额，
    两次快照做差就知道它在买什么、卖什么 —— 精度略低，成本低一个数量级。
    """

    name = "solana_wallets"
    default_interval = 600

    def __init__(self, config, db) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config, db)
        self.rpc_url = str(self.settings.get("rpc_url") or "") or PUBLIC_RPC
        limit = 30 if self.rpc_url == PUBLIC_RPC else 200
        self.http = HttpClient("solana_wallets", rate_limit=limit, period=60.0)

    def preflight(self) -> str | None:
        if not self.settings.get("wallets"):
            return "未配置任何监控钱包（collectors.solana_wallets.wallets）"
        if self.rpc_url == PUBLIC_RPC:
            log.warning("[solana_wallets] 正在使用公共 RPC，限流严重，建议配置 SOLANA_RPC_URL（Helius 免费档即可）")
        return None

    def collect(self) -> CollectResult:
        result = CollectResult()
        for entry in self.settings.get("wallets") or []:
            address = str(entry.get("address") or "").strip()
            if not address:
                continue
            label = str(entry.get("label") or address[:8])
            result.extend(self._diff_wallet(address, label))
        result.extend(aggregate_smart_money(self.db, self, int(self.settings.get("lookback_minutes", 90))))
        return result

    def _diff_wallet(self, address: str, label: str) -> CollectResult:
        result = CollectResult()
        current = self._balances(address)
        if current is None:
            return result

        state_key = f"solana_balances:{address}"
        previous: dict[str, float] = self.db.kv_get(state_key, {}) or {}
        self.db.kv_set(state_key, current)

        if not previous:
            log.info("[solana_wallets] %s 首次快照，%d 个代币，下一轮开始产出差分", label, len(current))
            return result

        ts = now_ts()
        for mint, amount in current.items():
            before = float(previous.get(mint, 0.0))
            delta = amount - before
            if delta == 0:
                continue
            scale = max(abs(before), abs(amount))
            if scale <= 0 or abs(delta) / scale < MIN_CHANGE_RATIO:
                continue

            token_key = Entity.token_key("solana", mint)
            result.entities.append(
                Entity(
                    kind=KIND_TOKEN,
                    key=token_key,
                    chain="solana",
                    address=mint,
                    meta={"discovered_via": "smart_money"},
                )
            )
            self.db.record_wallet_event(
                wallet=address,
                label=label,
                chain="solana",
                token_key=token_key,
                symbol=None,
                direction="in" if delta > 0 else "out",
                amount=abs(delta),
                tx_hash=f"snapshot:{ts}",
                ts=ts,
            )

        # 上一轮有、这一轮消失 = 清仓
        for mint, before in previous.items():
            if mint in current or before <= 0:
                continue
            token_key = Entity.token_key("solana", mint)
            self.db.record_wallet_event(
                wallet=address,
                label=label,
                chain="solana",
                token_key=token_key,
                symbol=None,
                direction="out",
                amount=before,
                tx_hash=f"snapshot:{ts}",
                ts=ts,
            )
        return result

    def _balances(self, owner: str) -> dict[str, float] | None:
        balances: dict[str, float] = {}
        got_any = False
        for program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            payload = self.http.post_json(
                self.rpc_url,
                json_body={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [owner, {"programId": program}, {"encoding": "jsonParsed"}],
                },
            )
            if not isinstance(payload, dict) or "result" not in payload:
                continue
            got_any = True
            accounts = ((payload.get("result") or {}).get("value")) or []
            for account in accounts:
                info = (
                    ((account.get("account") or {}).get("data") or {}).get("parsed") or {}
                ).get("info") or {}
                mint = info.get("mint")
                amount = ((info.get("tokenAmount") or {}).get("uiAmount"))
                if not mint or amount is None:
                    continue
                try:
                    value = float(amount)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    balances[mint] = balances.get(mint, 0.0) + value
        return balances if got_any else None
