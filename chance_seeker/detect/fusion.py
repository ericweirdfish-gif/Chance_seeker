from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Sequence

from chance_seeker.config import Config
from chance_seeker.detect.baseline import noisy_or
from chance_seeker.models import (
    FAMILY_ATTENTION,
    FAMILY_CAPITAL,
    FAMILY_RISK,
    Entity,
    Opportunity,
    Signal,
    now_ts,
)
from chance_seeker.storage import Database

log = logging.getLogger(__name__)


def score_opportunity(
    config: Config,
    db: Database,
    entity: Entity,
    signals: Sequence[Signal],
    metrics: dict[str, float] | None = None,
) -> Opportunity:
    """把一堆零散信号融合成一个可比较的 0-100 机会分。

    核心思路：
      - 资金和注意力各自内部用 noisy-OR 聚合（多个弱证据可以叠加成强证据）
      - 两边 *同时* 出现才是最值钱的形态，给共振加成
      - 风险信号直接扣分，扣分有上限，避免一个噪音指标把好机会全否掉
      - 同等条件下市值越小加成越高，因为这个工具的目的是「早」
    """
    cfg = config.score
    weights = cfg.get("weights") or {}
    w_capital = float(weights.get("capital", 0.55))
    w_attention = float(weights.get("attention", 0.45))
    metrics = metrics or {}
    notes: list[str] = []

    capital_signals = [s for s in signals if s.family == FAMILY_CAPITAL]
    attention_signals = [s for s in signals if s.family == FAMILY_ATTENTION]
    risk_signals = [s for s in signals if s.family == FAMILY_RISK]

    capital_score = noisy_or([s.score for s in capital_signals])
    attention_score = noisy_or([s.score for s in attention_signals])

    risk_penalty_max = float(cfg.get("risk_penalty_max", 45))
    risk_penalty = noisy_or([s.score for s in risk_signals]) / 100.0 * risk_penalty_max

    score = w_capital * capital_score + w_attention * attention_score

    # ---- 资金 × 注意力 共振 ----
    window = int(cfg.get("cooccurrence_window_minutes", 90)) * 60
    cooccurrence = _has_cooccurrence(db, entity.key, capital_signals, attention_signals, window)
    if cooccurrence:
        bonus = float(cfg.get("cooccurrence_bonus", 15))
        score += bonus
        notes.append(f"资金面与注意力面在 {window // 60} 分钟内共振（+{bonus:g}）")

    # ---- 早期加成 ----
    early_max = float(cfg.get("early_bonus_max", 12))
    ceiling = float(cfg.get("early_bonus_mcap_ceiling", 30_000_000))
    mcap = metrics.get("market_cap_usd") or metrics.get("gt_fdv_usd") or 0.0
    if early_max > 0 and ceiling > 0 and 0 < mcap < ceiling:
        # 对数刻度：100 万市值拿满，接近上限时趋近 0
        factor = math.log10(ceiling / max(mcap, 1.0)) / math.log10(max(ceiling / 1_000_000.0, 1.0000001))
        bonus = early_max * max(0.0, min(1.0, factor))
        if bonus >= 0.5:
            score += bonus
            notes.append(f"早期加成（市值 ${mcap:,.0f}，+{bonus:.1f}）")

    if risk_penalty > 0:
        score -= risk_penalty
        notes.append(f"风险扣分 -{risk_penalty:.1f}：" + "、".join(s.label for s in risk_signals))

    score = max(0.0, min(100.0, score))

    return Opportunity(
        entity=entity,
        score=round(score, 2),
        capital_score=round(capital_score, 2),
        attention_score=round(attention_score, 2),
        risk_penalty=round(risk_penalty, 2),
        cooccurrence=cooccurrence,
        signals=list(signals),
        metrics=metrics,
        notes=notes,
        ts=now_ts(),
    )


def _has_cooccurrence(
    db: Database,
    entity_key: str,
    capital_signals: Sequence[Signal],
    attention_signals: Sequence[Signal],
    window: int,
) -> bool:
    """本轮同时命中，或本轮命中一边、另一边在时间窗内命中过，都算共振。

    这一步很关键：资金和注意力很少精确同时到达，通常差几十分钟，
    只看单轮会漏掉绝大部分真实共振。
    """
    if capital_signals and attention_signals:
        return True
    if not capital_signals and not attention_signals:
        return False

    historical = db.recent_signals(entity_key, window)
    families = {s.family for s in historical}
    if capital_signals:
        return FAMILY_ATTENTION in families
    return FAMILY_CAPITAL in families


# ------------------------------------------------------------------ 过滤器
def passes_filters(config: Config, entity: Entity, metrics: dict[str, float]) -> tuple[bool, str]:
    """告警前的质量闸门。不通过的机会仍然入库，只是不打扰你。"""
    filters = config.filters
    if entity.kind != "token":
        return True, ""

    min_liq = float(filters.get("min_liquidity_usd", 0) or 0)
    liquidity = metrics.get("liquidity_usd", metrics.get("gt_reserve_usd", 0.0))
    if min_liq and liquidity < min_liq:
        return False, f"流动性 ${liquidity:,.0f} < ${min_liq:,.0f}"

    min_vol = float(filters.get("min_volume_24h", 0) or 0)
    volume = metrics.get("volume_24h", metrics.get("gt_volume_24h", 0.0))
    if min_vol and volume < min_vol:
        return False, f"24h 成交量 ${volume:,.0f} < ${min_vol:,.0f}"

    min_age = float(filters.get("min_age_minutes", 0) or 0)
    age = metrics.get("age_minutes")
    if min_age:
        if age is None:
            # 年龄未知时不能当成「通过」——那等于让 min_age_minutes 静默失效。
            # 默认仍然放行（否则数据源一变就全线拦死），但这是一个显式选择。
            if not filters.get("allow_unknown_age", True):
                return False, "上线时间未知（allow_unknown_age=false）"
        elif age < min_age:
            return False, f"上线 {age:.0f} 分钟 < {min_age:.0f} 分钟（太新，先观察）"

    max_age_days = float(filters.get("max_age_days", 0) or 0)
    if max_age_days and age is not None and age > max_age_days * 1440:
        return False, f"上线已 {age / 1440:.1f} 天 > {max_age_days:g} 天"

    for metric in filters.get("require_metrics") or []:
        if metric not in metrics:
            return False, f"缺少必需指标 {metric}"

    return True, ""


# ------------------------------------------------------------------ 冷却
def fingerprint(opportunity: Opportunity) -> str:
    """同一实体 + 同一组规则 = 同一个指纹，用来识别重复告警。"""
    rules = ",".join(sorted({s.rule_id for s in opportunity.signals}))
    raw = f"{opportunity.entity.key}|{rules}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def should_alert(config: Config, db: Database, opportunity: Opportunity) -> tuple[bool, str]:
    cfg = config.score
    threshold = float(cfg.get("alert_threshold", 62))
    if opportunity.score < threshold:
        return False, f"分数 {opportunity.score:.1f} < 阈值 {threshold:g}"

    veto = float(cfg.get("risk_veto", 0) or 0)
    if veto and opportunity.risk_penalty >= veto:
        labels = "、".join(s.label for s in opportunity.signals if s.family == FAMILY_RISK)
        return False, f"风险一票否决（扣分 {opportunity.risk_penalty:.0f} ≥ {veto:g}）：{labels}"

    ok, reason = passes_filters(config, opportunity.entity, opportunity.metrics)
    if not ok:
        return False, f"未通过质量过滤：{reason}"

    last = db.last_alert(opportunity.entity.key)
    if last is None:
        return True, ""

    cooldown = int(cfg.get("cooldown_minutes", 120)) * 60
    elapsed = now_ts() - int(last["ts"])
    if elapsed >= cooldown:
        return True, ""

    # 分数显著抬升时允许突破冷却——行情在加速，值得再提醒一次
    delta = float(cfg.get("cooldown_override_delta", 18))
    if opportunity.score - float(last["score"]) >= delta:
        return True, f"分数较上次告警 +{opportunity.score - float(last['score']):.1f}，突破冷却"

    return False, f"冷却中（还剩 {(cooldown - elapsed) // 60} 分钟）"
