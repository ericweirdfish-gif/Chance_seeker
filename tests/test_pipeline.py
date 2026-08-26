from __future__ import annotations

import pytest

from chance_seeker.alerts.base import AlertChannel
from chance_seeker.collectors.base import Collector, CollectResult
from chance_seeker.models import Entity, Observation, Opportunity, now_ts
from chance_seeker.pipeline import Pipeline


class FakeCollector(Collector):
    name = "dexscreener"  # 复用已有配置项，省得改 config

    def __init__(self, config, db, script):
        super().__init__(config, db)
        self.script = script
        self.calls = 0

    def collect(self) -> CollectResult:
        self.calls += 1
        return self.script(self.calls)


class BoomCollector(Collector):
    name = "geckoterminal"

    def collect(self) -> CollectResult:
        raise RuntimeError("上游挂了")


class RecordingChannel(AlertChannel):
    name = "recording"

    def __init__(self):
        super().__init__({})
        self.sent: list[Opportunity] = []

    def send(self, opportunity: Opportunity) -> None:
        self.sent.append(opportunity)


class BrokenChannel(AlertChannel):
    name = "broken"

    def __init__(self):
        super().__init__({})

    def send(self, opportunity: Opportunity) -> None:
        raise RuntimeError("webhook 502")


def steady_then_spike(config, db, key="token:solana:abc"):
    """造一段平稳历史，最后一轮同时放量并被广泛讨论。"""
    entity = Entity(kind="token", key=key, chain="solana", address="abc", symbol="ABC")

    def script(call: int) -> CollectResult:
        result = CollectResult(entities=[entity])
        ts = now_ts() - (30 - call) * 300
        spike = call >= 30
        points = {
            "volume_1h": 900_000.0 if spike else 50_000.0,
            "volume_5m": 200_000.0 if spike else 4_000.0,
            "liquidity_usd": 400_000.0,
            "volume_24h": 2_000_000.0,
            "market_cap_usd": 3_000_000.0,
            "age_minutes": 5000.0,
            "buy_sell_ratio_1h": 4.0 if spike else 1.0,
            "x_mentions": 300.0 if spike else 5.0,
            "x_unique_authors": 180.0 if spike else 3.0,
        }
        for metric, value in points.items():
            result.observations.append(
                Observation(entity_key=key, metric=metric, value=value, ts=ts, source="fake")
            )
        return result

    return script


@pytest.fixture()
def pipeline(config, db):
    pipe = Pipeline(config, db)
    pipe.collectors = []
    pipe.channels = []
    return pipe


def test_tick_persists_entities_and_metrics(config, db, pipeline):
    collector = FakeCollector(config, db, steady_then_spike(config, db))
    pipeline.collectors = [collector]
    report = pipeline.tick(force=True)
    assert report.ran == ["dexscreener"]
    assert report.entities == 1
    assert report.observations == 9
    assert db.get_entity("token:solana:abc").symbol == "ABC"


def test_collector_failure_is_isolated(config, db, pipeline):
    good = FakeCollector(config, db, steady_then_spike(config, db))
    pipeline.collectors = [BoomCollector(config, db), good]
    report = pipeline.tick(force=True)
    assert "geckoterminal" in report.errors
    assert good.calls == 1
    assert report.observations == 9


def test_end_to_end_spike_produces_alert(config, db, pipeline):
    channel = RecordingChannel()
    pipeline.channels = [channel]
    collector = FakeCollector(config, db, steady_then_spike(config, db))
    pipeline.collectors = [collector]

    for _ in range(29):
        report = pipeline.tick(force=True)
    assert not report.alerted, "平稳期不应该告警"

    report = pipeline.tick(force=True)  # 第 30 轮：异动
    assert report.signals > 0
    assert len(channel.sent) == 1
    opportunity = channel.sent[0]
    assert opportunity.capital_score > 0
    assert opportunity.attention_score > 0
    assert opportunity.cooccurrence is True
    assert db.last_alert("token:solana:abc") is not None


def test_cooldown_prevents_duplicate_alerts(config, db, pipeline):
    channel = RecordingChannel()
    pipeline.channels = [channel]
    pipeline.collectors = [FakeCollector(config, db, steady_then_spike(config, db))]
    for _ in range(32):
        pipeline.tick(force=True)
    assert len(channel.sent) == 1, "冷却期内不应该重复告警"


def test_channel_failure_is_recorded_not_raised(config, db, pipeline):
    pipeline.channels = [BrokenChannel()]
    pipeline.collectors = [FakeCollector(config, db, steady_then_spike(config, db))]
    for _ in range(30):
        report = pipeline.tick(force=True)
    assert report.alerted == []  # 没有任何渠道成功 = 不算已告警
    row = db.conn.execute("SELECT status, error FROM alerts ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "failed" and "502" in row["error"]


def test_opportunities_are_stored_even_when_not_alerted(config, db, pipeline):
    pipeline.collectors = [FakeCollector(config, db, steady_then_spike(config, db))]
    for _ in range(30):
        pipeline.tick(force=True)
    rows = db.recent_opportunities(limit=10)
    assert rows and rows[0]["score"] > 0
    assert "signals" in rows[0] and rows[0]["signals"]


def test_due_scheduling_skips_collectors_not_yet_due(config, db, pipeline):
    collector = FakeCollector(config, db, steady_then_spike(config, db))
    pipeline.collectors = [collector]
    pipeline.tick(force=True)
    pipeline.tick(force=False)  # 间隔 300s，立刻再跑不应该触发
    assert collector.calls == 1


def test_disabled_collectors_are_not_constructed(config, db, caplog):
    """未启用的采集器不该在初始化时抱怨缺 key——那只会让日志里全是假警报。"""
    from chance_seeker.pipeline import build_collectors

    assert config.collectors["x_attention"]["enabled"] is False
    with caplog.at_level("WARNING"):
        names = {c.name for c in build_collectors(config, db)}
    assert "x_attention" not in names
    assert not [r for r in caplog.records if "X_API_KEY" in r.getMessage()]


def test_prune_counter_survives_process_restarts(config, db):
    """`once` 模式每次都是新进程；计数器放内存里清理就永远不会触发。"""
    from chance_seeker.models import Entity, Observation, now_ts

    key = "token:solana:abc"
    db.upsert_entity(Entity(kind="token", key=key, chain="solana", address="abc"))
    base = now_ts()
    db.record([Observation(entity_key=key, metric="v", value=float(i), ts=base + i) for i in range(200)])
    config.general["series_retention_points"] = 10

    # 模拟 Pipeline 被反复重建（每次 once 都是一个全新进程）
    for _ in range(Pipeline.PRUNE_EVERY - 1):
        pipe = Pipeline(config, db)
        pipe.collectors, pipe.channels = [], []
        pipe.tick(force=True)
    assert len(db.series(key, "v", limit=500)) == 200, "还没到清理轮次"

    pipe = Pipeline(config, db)
    pipe.collectors, pipe.channels = [], []
    pipe.tick(force=True)
    assert len(db.series(key, "v", limit=500)) == 10, "第 20 轮必须触发清理"
    assert db.kv_get("pipeline_ticks") == Pipeline.PRUNE_EVERY
