from __future__ import annotations

import logging
from collections.abc import Sequence

from chance_seeker.config import Config, Rule
from chance_seeker.detect import baseline
from chance_seeker.models import Signal, now_ts
from chance_seeker.storage import Database

log = logging.getLogger(__name__)


class AnomalyEngine:
    """按配置里的规则逐个实体、逐个指标做异常判定。

    所有规则都是声明式的（config.yaml 的 detect.rules），改阈值不用改代码。
    """

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.window = int(config.detect.get("window", 48))
        self.min_samples = int(config.detect.get("min_samples", 8))

    def evaluate_entity(self, entity_key: str) -> list[Signal]:
        signals: list[Signal] = []
        available = set(self.db.metric_names(entity_key))
        for rule in self.config.rules:
            if rule.metric not in available:
                continue
            series = [v for _, v in self.db.series(entity_key, rule.metric, limit=self.window + 1)]
            signal = self.evaluate_rule(entity_key, rule, series)
            if signal is not None:
                signals.append(signal)
        return signals

    def evaluate_rule(self, entity_key: str, rule: Rule, series: Sequence[float]) -> Signal | None:
        if not series:
            return None
        value = float(series[-1])
        history = [float(v) for v in series[:-1]]

        if rule.min_value and abs(value) < rule.min_value:
            return None

        observed, base, detail = self._measure(rule, value, history, series)
        if observed is None:
            return None

        if not _triggered(rule, observed):
            return None

        score = baseline.normalize_score(abs(observed), abs(rule.threshold)) * rule.weight
        score = max(0.0, min(100.0, score))
        if score <= 0:
            return None

        return Signal(
            entity_key=entity_key,
            rule_id=rule.id,
            family=rule.family,
            metric=rule.metric,
            label=rule.label or rule.id,
            score=round(score, 2),
            value=value,
            baseline=round(base, 6),
            detail={"observed": round(observed, 4), "threshold": rule.threshold, "method": rule.method, **detail},
            ts=now_ts(),
        )

    # ------------------------------------------------------------------
    def _measure(
        self, rule: Rule, value: float, history: list[float], series: Sequence[float]
    ) -> tuple[float | None, float, dict]:
        method = rule.method

        if method in ("robust_z", "ratio"):
            if len(history) < self.min_samples:
                return None, 0.0, {}
            if method == "robust_z":
                observed, base = baseline.robust_z(value, history)
                return observed, base, {"samples": len(history)}
            observed, base = baseline.ratio(value, history)
            return observed, base, {"samples": len(history)}

        if method == "delta_pct":
            if len(series) <= rule.lookback:
                return None, 0.0, {}
            observed, base = baseline.delta_pct(series, rule.lookback)
            return observed, base, {"lookback": rule.lookback}

        if method == "acceleration":
            observed, base = baseline.acceleration(series, rule.lookback)
            if observed == 0.0 and base == 0.0:
                return None, 0.0, {}
            return observed, base, {"lookback": rule.lookback}

        if method in ("level", "level_below"):
            # 绝对阈值规则不需要历史，冷启动第一轮就能生效
            return value, baseline.median(history) if history else 0.0, {}

        log.warning("未知的检测方法 %r（规则 %s）", method, rule.id)
        return None, 0.0, {}


def _triggered(rule: Rule, observed: float) -> bool:
    if rule.method == "level_below":
        return observed <= rule.threshold
    if rule.direction == "down" or rule.threshold < 0:
        return observed <= rule.threshold
    return observed >= rule.threshold
