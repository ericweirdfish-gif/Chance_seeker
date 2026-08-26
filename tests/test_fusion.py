from __future__ import annotations

import pytest

from chance_seeker.detect.fusion import fingerprint, passes_filters, score_opportunity, should_alert
from chance_seeker.models import Entity, Signal, now_ts


def make_entity(db, key="token:solana:abc", **kwargs):
    entity = Entity(kind="token", key=key, chain="solana", address="abc", symbol="ABC", **kwargs)
    db.upsert_entity(entity)
    return db.get_entity(key)


def sig(family, score=70.0, rule_id=None, ts=None):
    return Signal(
        entity_key="token:solana:abc",
        rule_id=rule_id or f"{family}_rule",
        family=family,
        metric="m",
        label=f"{family} 信号",
        score=score,
        value=1.0,
        baseline=0.1,
        ts=ts or now_ts(),
    )


def test_capital_only_scores_lower_than_resonance(config, db):
    entity = make_entity(db)
    metrics = {"market_cap_usd": 50_000_000}  # 关掉早期加成，隔离共振的影响
    capital_only = score_opportunity(config, db, entity, [sig("capital", 80)], metrics)
    both = score_opportunity(config, db, entity, [sig("capital", 80), sig("attention", 80)], metrics)
    assert both.cooccurrence is True
    assert capital_only.cooccurrence is False
    assert both.score > capital_only.score + 15


def test_cooccurrence_detected_across_time_window(config, db):
    """资金和注意力很少同时到达，隔了一段时间也应该算共振。"""
    entity = make_entity(db)
    db.save_signals([sig("attention", 70, ts=now_ts() - 1800)])
    opportunity = score_opportunity(config, db, entity, [sig("capital", 70)], {"market_cap_usd": 50_000_000})
    assert opportunity.cooccurrence is True


def test_cooccurrence_expires_outside_window(config, db):
    entity = make_entity(db)
    db.save_signals([sig("attention", 70, ts=now_ts() - 10 * 3600)])
    opportunity = score_opportunity(config, db, entity, [sig("capital", 70)], {"market_cap_usd": 50_000_000})
    assert opportunity.cooccurrence is False


def test_multiple_weak_signals_accumulate(config, db):
    entity = make_entity(db)
    metrics = {"market_cap_usd": 50_000_000}
    one = score_opportunity(config, db, entity, [sig("capital", 50, "a")], metrics)
    three = score_opportunity(
        config, db, entity,
        [sig("capital", 50, "a"), sig("capital", 50, "b"), sig("capital", 50, "c")],
        metrics,
    )
    assert three.capital_score > one.capital_score
    assert three.capital_score <= 100


def test_risk_signals_reduce_score_but_are_capped(config, db):
    entity = make_entity(db)
    metrics = {"market_cap_usd": 50_000_000}
    # 用 60 分而不是 90 分，避免总分撞到 100 的上限，才能直接比较扣分幅度
    clean = score_opportunity(config, db, entity, [sig("capital", 60), sig("attention", 60)], metrics)
    risky = score_opportunity(
        config, db, entity,
        [sig("capital", 60), sig("attention", 60), sig("risk", 100, "r1"), sig("risk", 100, "r2")],
        metrics,
    )
    assert clean.score < 100  # 确认没有被上限截断
    assert risky.score < clean.score
    penalty_cap = float(config.score["risk_penalty_max"])
    assert risky.risk_penalty <= penalty_cap + 1e-6
    assert clean.score - risky.score == pytest.approx(risky.risk_penalty, abs=0.01)


def test_early_bonus_favours_smaller_market_caps(config, db):
    entity = make_entity(db)
    signals = [sig("capital", 60)]
    small = score_opportunity(config, db, entity, signals, {"market_cap_usd": 1_000_000})
    large = score_opportunity(config, db, entity, signals, {"market_cap_usd": 28_000_000})
    huge = score_opportunity(config, db, entity, signals, {"market_cap_usd": 500_000_000})
    assert small.score > large.score >= huge.score


def test_score_is_bounded(config, db):
    entity = make_entity(db)
    signals = [sig("capital", 100, f"c{i}") for i in range(5)] + [sig("attention", 100, f"a{i}") for i in range(5)]
    opportunity = score_opportunity(config, db, entity, signals, {"market_cap_usd": 100_000})
    assert 0 <= opportunity.score <= 100


# ------------------------------------------------------------------ 过滤器
def test_filters_reject_thin_liquidity(config, db):
    entity = make_entity(db)
    ok, reason = passes_filters(config, entity, {"liquidity_usd": 500, "volume_24h": 999_999})
    assert not ok and "流动性" in reason


def test_filters_reject_too_new(config, db):
    entity = make_entity(db)
    metrics = {"liquidity_usd": 999_999, "volume_24h": 999_999, "age_minutes": 3}
    ok, reason = passes_filters(config, entity, metrics)
    assert not ok and "分钟" in reason


def test_filters_pass_healthy_token(config, db):
    entity = make_entity(db)
    metrics = {"liquidity_usd": 300_000, "volume_24h": 900_000, "age_minutes": 600}
    assert passes_filters(config, entity, metrics) == (True, "")


def test_non_token_entities_skip_filters(config, db):
    narrative = Entity(kind="narrative", key="narrative:ai")
    db.upsert_entity(narrative)
    assert passes_filters(config, db.get_entity("narrative:ai"), {})[0] is True


# ------------------------------------------------------------------ 冷却
def _good(config, db):
    entity = make_entity(db)
    metrics = {"liquidity_usd": 300_000, "volume_24h": 900_000, "age_minutes": 600, "market_cap_usd": 2_000_000}
    return score_opportunity(config, db, entity, [sig("capital", 95), sig("attention", 95)], metrics)


def test_should_alert_blocks_low_scores(config, db):
    entity = make_entity(db)
    weak = score_opportunity(config, db, entity, [sig("capital", 10)], {"liquidity_usd": 300_000})
    ok, reason = should_alert(config, db, weak)
    assert not ok and "阈值" in reason


def test_cooldown_blocks_repeat_alerts(config, db):
    opportunity = _good(config, db)
    assert should_alert(config, db, opportunity)[0] is True
    db.record_alert(opportunity.entity.key, "fp", opportunity.score, "console", "sent")
    ok, reason = should_alert(config, db, opportunity)
    assert not ok and "冷却" in reason


def test_big_score_jump_breaks_cooldown(config, db):
    opportunity = _good(config, db)
    db.record_alert(opportunity.entity.key, "fp", opportunity.score - 30, "console", "sent")
    ok, reason = should_alert(config, db, opportunity)
    assert ok and "突破冷却" in reason


def test_fingerprint_is_stable_and_rule_sensitive(config, db):
    entity = make_entity(db)
    metrics = {"liquidity_usd": 300_000}
    a = score_opportunity(config, db, entity, [sig("capital", 70, "x"), sig("attention", 70, "y")], metrics)
    b = score_opportunity(config, db, entity, [sig("attention", 70, "y"), sig("capital", 70, "x")], metrics)
    c = score_opportunity(config, db, entity, [sig("capital", 70, "z")], metrics)
    assert fingerprint(a) == fingerprint(b)
    assert fingerprint(a) != fingerprint(c)


def test_severe_risk_vetoes_the_alert_outright(config, db):
    """资金和注意力都拉满，但流动性被抽走——这种绝对不能推送。"""
    entity = make_entity(db)
    metrics = {"liquidity_usd": 300_000, "volume_24h": 900_000, "age_minutes": 600, "market_cap_usd": 2_000_000}
    trap = score_opportunity(
        config, db, entity,
        [sig("capital", 99, "c"), sig("attention", 99, "a"), sig("risk", 96, "liq_drain")],
        metrics,
    )
    assert trap.score >= float(config.score["alert_threshold"]), "分数本身够高，说明扣分挡不住它"
    ok, reason = should_alert(config, db, trap)
    assert not ok and "一票否决" in reason


def test_mild_risk_does_not_veto(config, db):
    entity = make_entity(db)
    metrics = {"liquidity_usd": 300_000, "volume_24h": 900_000, "age_minutes": 600, "market_cap_usd": 2_000_000}
    mild = score_opportunity(
        config, db, entity,
        [sig("capital", 95, "c"), sig("attention", 95, "a"), sig("risk", 20, "sell_pressure")],
        metrics,
    )
    assert 0 < mild.risk_penalty < float(config.score["risk_veto"])
    assert should_alert(config, db, mild)[0] is True
