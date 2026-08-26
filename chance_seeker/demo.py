"""生成合成数据，让你在没有 API key、甚至没有网络的情况下把整条链路跑通。

`chance-seeker demo` 会造三个典型形态：
  1. 资金与注意力共振的真机会（应该拿到高分并触发告警）
  2. 只有资金没有注意力的标的（分数中等，通常不告警）
  3. 拉盘后抽流动性的陷阱（风险规则应该把它压下去）
"""

from __future__ import annotations

import logging
import math
import random

from chance_seeker.alerts.renderer import render_plain
from chance_seeker.config import Config
from chance_seeker.detect.anomaly import AnomalyEngine
from chance_seeker.detect.fusion import score_opportunity, should_alert
from chance_seeker.models import KIND_TOKEN, Entity, Observation, now_ts
from chance_seeker.pipeline import _payload
from chance_seeker.storage import Database

log = logging.getLogger(__name__)

STEP = 300  # 5 分钟一个点
POINTS = 60  # 共 5 小时历史


def seed_demo_data(config: Config, db: Database) -> None:
    rng = random.Random(42)
    end = now_ts()

    specs = [
        {
            "symbol": "DEMOA",
            "name": "Demo Resonance",
            "address": "DemoA1111111111111111111111111111111111111",
            "chain": "solana",
            "capital_spike": 6.0,
            "attention_spike": 8.0,
            "liquidity_trend": 1.6,
            "market_cap": 4_200_000,
            "rug": False,
        },
        {
            "symbol": "DEMOB",
            "name": "Demo Quiet Money",
            "address": "DemoB2222222222222222222222222222222222222",
            "chain": "solana",
            "capital_spike": 5.5,
            "attention_spike": 1.0,
            "liquidity_trend": 1.3,
            "market_cap": 9_500_000,
            "rug": False,
        },
        {
            "symbol": "DEMOC",
            "name": "Demo Rug Pattern",
            "address": "0xdec0000000000000000000000000000000000000",
            "chain": "base",
            "capital_spike": 7.0,
            "attention_spike": 4.0,
            "liquidity_trend": 1.2,
            "market_cap": 1_800_000,
            "rug": True,
        },
    ]

    # 重跑 demo 时先清掉上一轮的合成数据，否则新旧序列叠在一起，
    # 形态会被打乱（尤其是断崖式抽流动性会被抹平）
    _clear_previous(db, specs)

    observations: list[Observation] = []
    for spec in specs:
        key = Entity.token_key(spec["chain"], spec["address"])
        db.upsert_entity(
            Entity(
                kind=KIND_TOKEN,
                key=key,
                chain=spec["chain"],
                address=spec["address"],
                symbol=spec["symbol"],
                name=spec["name"],
                meta={"discovered_via": "demo"},
            )
        )
        observations.extend(_series_for(key, spec, end, rng))

    written = db.record(observations)
    log.info("已写入 %d 个合成指标点", written)

    engine = AnomalyEngine(config, db)
    print(f"\n生成 {len(specs)} 个演示标的，{written} 个指标点。检测结果：\n")
    for spec in specs:
        key = Entity.token_key(spec["chain"], spec["address"])
        entity = db.get_entity(key)
        if entity is None:
            continue
        signals = engine.evaluate_entity(key)
        metrics = db.latest_metrics(key)
        opportunity = score_opportunity(config, db, entity, signals, metrics)
        db.save_signals(signals)
        ok, reason = should_alert(config, db, opportunity)
        # 也写进 opportunities 表，这样 `top` 命令和网页看板马上就有东西看
        db.save_opportunity(key, _payload(opportunity, ok, reason))
        print(render_plain(opportunity))
        print(f"→ 是否告警: {'是' if ok else '否'}{'' if ok else '（' + reason + '）'}\n")


def _clear_previous(db: Database, specs: list[dict]) -> None:
    keys = [Entity.token_key(spec["chain"], spec["address"]) for spec in specs]
    placeholders = ",".join("?" * len(keys))
    ids = [
        int(row["id"])
        for row in db.conn.execute(f"SELECT id FROM entities WHERE key IN ({placeholders})", keys)
    ]
    if not ids:
        return
    id_list = ",".join("?" * len(ids))
    for table in ("metrics", "signals", "opportunities", "alerts"):
        db.conn.execute(f"DELETE FROM {table} WHERE entity_id IN ({id_list})", ids)
    db.conn.commit()
    log.info("已清理上一轮 demo 数据（%d 个标的）", len(ids))


def _series_for(key: str, spec: dict, end: int, rng: random.Random) -> list[Observation]:
    """前 80% 平稳，最后 20% 按 spec 的强度制造异动。"""
    observations: list[Observation] = []
    base_volume_1h = 40_000.0
    base_liquidity = 180_000.0
    base_mentions = 6.0

    for i in range(POINTS):
        ts = end - (POINTS - 1 - i) * STEP
        progress = i / (POINTS - 1)
        ramp = 0.0 if progress < 0.8 else (progress - 0.8) / 0.2  # 0 → 1
        noise = lambda scale: 1.0 + rng.uniform(-scale, scale)  # noqa: E731

        capital_mult = 1.0 + (spec["capital_spike"] - 1.0) * _ease(ramp)
        attention_mult = 1.0 + (spec["attention_spike"] - 1.0) * _ease(ramp)
        liquidity_mult = 1.0 + (spec["liquidity_trend"] - 1.0) * _ease(ramp)

        # 真实的 rug 是最后一两个点断崖式抽走流动性，不是慢慢阴跌，
        # 所以这里单独造断崖，否则 liq_drain 这条规则永远测不到
        rugging = spec["rug"] and i >= POINTS - 2
        if rugging:
            liquidity_mult *= 0.22

        volume_1h = base_volume_1h * capital_mult * noise(0.08)
        liquidity = base_liquidity * liquidity_mult * noise(0.04)
        mentions = base_mentions * attention_mult * noise(0.15)

        points = {
            "volume_1h": volume_1h,
            "volume_5m": volume_1h / 12.0 * (1.0 + ramp * 2.0),
            "volume_24h": volume_1h * 18.0,
            "liquidity_usd": liquidity,
            "market_cap_usd": spec["market_cap"] * (1.0 + ramp * 0.9),
            "price_change_1h": (-70.0 if rugging else ramp * 65.0) * noise(0.2),
            "buy_sell_ratio_1h": 0.25 if rugging else 1.0 + ramp * 2.0,
            "vol_liq_ratio_1h": volume_1h / max(liquidity, 1.0),
            "age_minutes": 3200 + i * 5,
            "x_mentions": mentions,
            # 互动量跟着注意力走，不能跟着资金走——否则「只有资金没有注意力」
            # 这个演示形态会被自己造出来的互动量污染
            "x_engagement": mentions * 42.0 * attention_mult,
            "x_unique_authors": max(1.0, mentions * 0.62),
            "x_kol_mentions": float(int(_ease(ramp) * 3)) if spec["attention_spike"] > 5 else 0.0,
        }
        for metric, value in points.items():
            observations.append(
                Observation(entity_key=key, metric=metric, value=float(value), ts=ts, source="demo")
            )
    return observations


def _ease(x: float) -> float:
    """缓入曲线，让异动看起来像真的在加速而不是一个方波。"""
    return 0.0 if x <= 0 else math.pow(min(1.0, x), 1.8)
