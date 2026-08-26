from __future__ import annotations

import pytest

from chance_seeker.detect import baseline


def test_median_and_mad():
    assert baseline.median([3, 1, 2]) == 2
    assert baseline.median([4, 1, 3, 2]) == 2.5
    assert baseline.mad([1, 1, 1, 1]) == 0
    assert baseline.mad([1, 2, 3, 4, 100]) == 1


def test_robust_z_flags_spike_but_not_normal_noise():
    history = [100, 102, 98, 101, 99, 103, 97, 100]
    quiet, _ = baseline.robust_z(101, history)
    spike, center = baseline.robust_z(400, history)
    assert abs(quiet) < 1.5
    assert spike > 10
    assert center == pytest.approx(100, abs=1.5)


def test_robust_z_is_not_masked_by_a_prior_outlier():
    """均值+标准差在这个序列上会被 5000 拉高，导致 400 检测不出来；中位数不会。"""
    history = [100, 101, 99, 100, 5000, 100, 98, 102]
    score, _ = baseline.robust_z(400, history)
    assert score > 10


def test_robust_z_constant_history():
    score, center = baseline.robust_z(50, [10] * 10)
    assert score == 6.0 and center == 10
    assert baseline.robust_z(10, [10] * 10)[0] == 0.0


def test_ratio_handles_zero_baseline():
    value, center = baseline.ratio(5, [0, 0, 0, 0])
    assert value == 999.0 and center == 0
    assert baseline.ratio(0, [0, 0])[0] == 0.0
    assert baseline.ratio(30, [10, 10, 10])[0] == pytest.approx(3.0)


def test_delta_pct():
    pct, before = baseline.delta_pct([100, 110, 120, 150], lookback=3)
    assert pct == pytest.approx(50.0) and before == 100
    assert baseline.delta_pct([100, 50], lookback=1)[0] == pytest.approx(-50.0)
    assert baseline.delta_pct([100], lookback=3)[0] == 0.0


def test_acceleration():
    steady = list(range(0, 11))  # 匀速上涨，不算加速
    assert baseline.acceleration(steady, lookback=3)[0] == pytest.approx(1.0)
    accelerating = [0, 1, 2, 3, 10, 25, 60]
    assert baseline.acceleration(accelerating, lookback=3)[0] > 3
    assert baseline.acceleration([1, 2], lookback=3)[0] == 0.0


def test_normalize_score_maps_threshold_to_50():
    assert baseline.normalize_score(3.0, 3.0) == pytest.approx(50.0)
    assert baseline.normalize_score(6.0, 3.0) == pytest.approx(66.67, abs=0.01)
    assert baseline.normalize_score(12.0, 3.0) == pytest.approx(80.0)
    assert baseline.normalize_score(-1.0, 3.0) == 0.0
    assert baseline.normalize_score(1.0, 0.0) == 0.0


def test_normalize_score_keeps_discriminating_at_extremes():
    """线性映射会让这三个都变成 100 分；饱和曲线必须还能排出先后。"""
    small = baseline.normalize_score(9.0, 3.0)
    medium = baseline.normalize_score(30.0, 3.0)
    huge = baseline.normalize_score(300.0, 3.0)
    assert small < medium < huge < 100.0


def test_noisy_or_accumulates_but_stays_bounded():
    assert baseline.noisy_or([]) == 0.0
    assert baseline.noisy_or([60]) == pytest.approx(60.0)
    combined = baseline.noisy_or([60, 60, 60])
    assert 90 < combined < 100
    assert baseline.noisy_or([100, 50]) == pytest.approx(100.0)


def test_ema():
    assert baseline.ema([]) == 0.0
    assert baseline.ema([5, 5, 5]) == pytest.approx(5.0)
    assert baseline.ema([0, 0, 10], span=2) > 5


def test_design_doc_masking_example_stays_true():
    """docs/DESIGN.md 3.1 节引用了这组数字，公式改了这里就会先红。"""
    import statistics

    history = [100, 101, 99, 100, 5000, 100, 98, 102]
    value = 400

    classic_z = (value - statistics.mean(history)) / statistics.stdev(history)
    assert classic_z == pytest.approx(-0.18, abs=0.01)  # 均值+标准差被历史尖峰掩蔽

    robust, center = baseline.robust_z(value, history)
    assert center == 100.0
    assert baseline.mad(history) == 1.0
    assert robust == pytest.approx(202.3, abs=0.1)


def test_design_doc_score_curve_table_stays_true():
    """docs/DESIGN.md 3.3 节的打分曲线表。"""
    table = {1: 50, 2: 67, 4: 80, 9: 90, 20: 95, 100: 99}
    for ratio_to_threshold, expected in table.items():
        assert round(baseline.normalize_score(ratio_to_threshold * 3, 3)) == expected
