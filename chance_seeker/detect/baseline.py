"""基线统计。

刻意只用中位数 / MAD 这类稳健统计量，不用均值和标准差：
加密市场的指标序列本身就充满尖峰，均值会被一次异常拉高，
导致「真正的异动」反而检测不出来（掩蔽效应）。
"""

from __future__ import annotations

from collections.abc import Sequence

# 0.6745 = 标准正态分布的 0.75 分位数，用它把 MAD 换算成标准差的稳健估计
MAD_SCALE = 0.6745
EPSILON = 1e-9


def median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def mad(values: Sequence[float], center: float | None = None) -> float:
    """中位绝对偏差。"""
    if not values:
        return 0.0
    mid = median(values) if center is None else center
    return median([abs(v - mid) for v in values])


def mean_abs_deviation(values: Sequence[float], center: float | None = None) -> float:
    if not values:
        return 0.0
    mid = median(values) if center is None else center
    return sum(abs(v - mid) for v in values) / len(values)


def robust_z(value: float, history: Sequence[float]) -> tuple[float, float]:
    """返回 (稳健 z 分数, 基线中位数)。

    MAD 为 0（例如序列长期为常数）时退化到平均绝对偏差；两者都为 0 时，
    只要当前值不等于基线就给一个较大的固定分数，否则给 0。
    """
    if not history:
        return 0.0, 0.0
    center = median(history)
    spread = mad(history, center)
    if spread <= EPSILON:
        spread = mean_abs_deviation(history, center)
    if spread <= EPSILON:
        if abs(value - center) <= EPSILON:
            return 0.0, center
        return (6.0 if value > center else -6.0), center
    return MAD_SCALE * (value - center) / spread, center


def ratio(value: float, history: Sequence[float]) -> tuple[float, float]:
    """当前值相对基线中位数的倍数。"""
    center = median(history)
    if center <= EPSILON:
        # 基线接近 0：任何非零值都是「从无到有」，给一个封顶的大倍数
        return (999.0 if value > EPSILON else 0.0), center
    return value / center, center


def delta_pct(series: Sequence[float], lookback: int = 1) -> tuple[float, float]:
    """最近一个点相对 lookback 个点之前的百分比变化。"""
    if len(series) <= lookback:
        return 0.0, series[0] if series else 0.0
    current = series[-1]
    before = series[-1 - lookback]
    if abs(before) <= EPSILON:
        return (999.0 if current > EPSILON else 0.0), before
    return (current - before) / abs(before) * 100.0, before


def acceleration(series: Sequence[float], lookback: int = 3) -> tuple[float, float]:
    """增速的增速：最近一段的变化幅度 / 上一段的变化幅度。

    > 1 表示上涨在加速，这比单纯的「涨了多少」更能区分「刚起步」和「已经走完」。
    """
    need = lookback * 2 + 1
    if len(series) < need:
        return 0.0, 0.0
    recent = series[-1] - series[-1 - lookback]
    prior = series[-1 - lookback] - series[-1 - 2 * lookback]
    if abs(prior) <= EPSILON:
        return (999.0 if recent > EPSILON else 0.0), prior
    if prior < 0 and recent <= 0:
        return 0.0, prior
    return recent / abs(prior), prior


def ema(values: Sequence[float], span: int = 12) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (span + 1.0)
    result = float(values[0])
    for value in values[1:]:
        result = alpha * float(value) + (1 - alpha) * result
    return result


def normalize_score(observed: float, threshold: float) -> float:
    """把「超过阈值多少」映射到 0-100 分：``100 * r / (1 + r)``，r = 观测值 / 阈值。

    刚好达到阈值 = 50 分，2 倍 = 67 分，4 倍 = 80 分，9 倍 = 90 分，永远逼近但不到 100。

    这里刻意不用线性映射。加密市场里真实异动的 z 分数动辄十几二十，
    线性映射会让绝大多数信号都顶到 100 分，分数就失去了区分度——
    「放量 5 倍」和「放量 50 倍」都是满分，排序就没有意义了。
    这条饱和曲线在任何量级上都还留着区分度。
    """
    if threshold == 0:
        return 0.0
    ratio_to_threshold = observed / threshold
    if ratio_to_threshold <= 0:
        return 0.0
    return 100.0 * ratio_to_threshold / (1.0 + ratio_to_threshold)


def noisy_or(scores: Sequence[float]) -> float:
    """把多个 0-100 的独立证据合成一个 0-100 的总分。

    比「取最大值」更能体现证据叠加（3 个 60 分的信号比 1 个 60 分强），
    又比「求和」更合理（不会因为信号多就轻易爆表）。
    """
    product = 1.0
    for score in scores:
        product *= 1.0 - max(0.0, min(100.0, score)) / 100.0
    return (1.0 - product) * 100.0
