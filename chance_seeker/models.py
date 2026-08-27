from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# 实体类型
KIND_TOKEN = "token"
KIND_CHAIN = "chain"
KIND_NARRATIVE = "narrative"
KIND_WALLET = "wallet"

FAMILY_CAPITAL = "capital"
FAMILY_ATTENTION = "attention"
FAMILY_RISK = "risk"


def now_ts() -> int:
    return int(time.time())


@dataclass(slots=True)
class Entity:
    """被监控的对象：一个代币 / 一条链 / 一个叙事关键词 / 一个钱包。"""

    kind: str
    key: str  # 全局唯一，例如 "token:solana:So111..."、"narrative:ai-agent"
    chain: str | None = None
    address: str | None = None
    symbol: str | None = None
    name: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    id: int | None = None
    first_seen: int = field(default_factory=now_ts)
    last_seen: int = field(default_factory=now_ts)

    @staticmethod
    def token_key(chain: str, address: str) -> str:
        return f"token:{chain}:{address.lower()}"

    @property
    def display(self) -> str:
        if self.symbol:
            return f"${self.symbol}"
        return self.name or self.key


@dataclass(slots=True)
class Observation:
    """一次采集产生的单个指标点。"""

    entity_key: str
    metric: str
    value: float
    ts: int = field(default_factory=now_ts)
    source: str = ""


@dataclass(slots=True)
class Signal:
    """一次规则命中。"""

    entity_key: str
    rule_id: str
    family: str
    metric: str
    label: str
    score: float
    value: float
    baseline: float
    detail: dict[str, Any] = field(default_factory=dict)
    ts: int = field(default_factory=now_ts)
    id: int | None = None


@dataclass(slots=True)
class Opportunity:
    """融合后的机会评分。"""

    entity: Entity
    score: float
    capital_score: float
    attention_score: float
    risk_penalty: float
    cooccurrence: bool
    signals: list[Signal] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    ts: int = field(default_factory=now_ts)
    # 为什么没有推送（分数不够 / 未过过滤 / 冷却中 / 风险否决），空串表示已推送
    skip_reason: str = ""
