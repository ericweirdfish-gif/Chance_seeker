from __future__ import annotations

import pytest

from chance_seeker.config import Rule
from chance_seeker.detect.anomaly import AnomalyEngine


@pytest.fixture()
def engine(config, db):
    return AnomalyEngine(config, db)


def rule(**kwargs):
    base = dict(id="r", family="capital", metric="volume_1h", method="robust_z", threshold=3.0, label="放量")
    base.update(kwargs)
    return Rule(**base)


def test_robust_z_rule_fires_on_spike(engine):
    series = [100.0] * 12 + [900.0]
    signal = engine.evaluate_rule("k", rule(), series)
    assert signal is not None
    assert signal.family == "capital"
    assert signal.score > 50
    assert signal.value == 900.0


def test_robust_z_rule_silent_on_noise(engine):
    series = [100, 104, 97, 101, 99, 103, 98, 102, 100, 101, 99, 100, 103]
    assert engine.evaluate_rule("k", rule(), [float(v) for v in series]) is None


def test_min_value_gate_suppresses_tiny_absolute_numbers(engine):
    """比例上是 10 倍，但绝对值只有 20 美元——这种噪音必须挡掉。"""
    series = [2.0] * 12 + [20.0]
    assert engine.evaluate_rule("k", rule(min_value=30000), series) is None
    assert engine.evaluate_rule("k", rule(min_value=0), series) is not None


def test_insufficient_history_is_not_a_signal(engine):
    assert engine.evaluate_rule("k", rule(), [100.0, 900.0]) is None


def test_level_rule_works_without_history(engine):
    signal = engine.evaluate_rule("k", rule(method="level", threshold=2.0, metric="smart_money_buyers"), [3.0])
    assert signal is not None and signal.score == pytest.approx(60.0)  # r=1.5 → 100*1.5/2.5


def test_level_below_rule(engine):
    r = rule(method="level_below", threshold=0.45, family="risk", metric="buy_sell_ratio_1h")
    assert engine.evaluate_rule("k", r, [0.2]) is not None
    assert engine.evaluate_rule("k", r, [1.5]) is None


def test_negative_threshold_detects_drops(engine):
    r = rule(method="delta_pct", threshold=-35.0, lookback=3, family="risk", metric="liquidity_usd", direction="down")
    dropping = [100000.0, 98000.0, 96000.0, 50000.0]
    assert engine.evaluate_rule("k", r, dropping) is not None
    rising = [100000.0, 98000.0, 96000.0, 150000.0]
    assert engine.evaluate_rule("k", r, rising) is None


def test_weight_scales_score(engine):
    series = [100.0] * 12 + [900.0]
    full = engine.evaluate_rule("k", rule(weight=1.0), series)
    half = engine.evaluate_rule("k", rule(weight=0.5), series)
    assert half.score == pytest.approx(full.score * 0.5, abs=0.01)


def test_unknown_method_is_ignored(engine):
    assert engine.evaluate_rule("k", rule(method="does_not_exist"), [1.0] * 20) is None


def test_evaluate_entity_only_uses_metrics_that_exist(engine, db):
    from chance_seeker.models import Entity, Observation, now_ts

    key = Entity.token_key("solana", "abc")
    db.upsert_entity(Entity(kind="token", key=key, chain="solana", address="abc"))
    base = now_ts()
    points = [Observation(entity_key=key, metric="volume_1h", value=50000.0, ts=base - (20 - i) * 300)
              for i in range(20)]
    points.append(Observation(entity_key=key, metric="volume_1h", value=900000.0, ts=base))
    db.record(points)

    signals = engine.evaluate_entity(key)
    assert [s.rule_id for s in signals] == ["vol_1h_spike"]
