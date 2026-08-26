from __future__ import annotations

from chance_seeker.diagnostics import describe, missing_fields, render


def test_describe_renders_nested_structure():
    payload = {"pairs": [{"baseToken": {"symbol": "ALPHA"}, "volume": {"h1": 1234.5}}]}
    text = "\n".join(describe(payload))
    assert "pairs: [] (1 items)" in text
    assert 'symbol: str = "ALPHA"' in text
    assert "h1: float = 1234.5" in text


def test_describe_only_expands_first_list_element():
    """同一接口的数组元素结构一致，全展开只会把日志刷爆。"""
    payload = [{"a": 1}, {"b": 2}, {"c": 3}]
    text = "\n".join(describe(payload))
    assert "(3 items)" in text
    assert "a: int = 1" in text
    assert "b: int" not in text


def test_describe_respects_max_depth():
    deep = {"l1": {"l2": {"l3": {"l4": {"l5": "bottom"}}}}}
    text = "\n".join(describe(deep, max_depth=2))
    assert "已达最大深度" in text
    assert "bottom" not in text


def test_describe_truncates_long_strings_and_wide_objects():
    text = "\n".join(describe({"k": "x" * 200}))
    assert "…" in text and len(text) < 150
    wide = "\n".join(describe({f"k{i}": i for i in range(60)}, max_keys=5))
    assert "还有 55 个字段" in wide


def test_describe_handles_empty_and_null():
    assert "(0 items)" in "\n".join(describe([]))
    assert "null" in "\n".join(describe({"k": None}))


def test_missing_fields_finds_gaps_not_present_ones():
    payload = {"pairs": [{"baseToken": {"symbol": "A"}, "liquidity": None}]}
    gaps = missing_fields(
        payload,
        {
            "pairs[].baseToken.symbol": "代号",
            "pairs[].liquidity.usd": "流动性",
            "pairs[].missing": "不存在",
            "topLevel": "顶层缺失",
        },
    )
    assert len(gaps) == 3
    assert not any("baseToken.symbol" in g for g in gaps)
    assert any("liquidity.usd" in g for g in gaps)


def test_missing_fields_treats_empty_array_as_missing():
    """空数组说明接口没返回数据，解析器拿不到东西，必须报出来。"""
    assert missing_fields({"pairs": []}, {"pairs[].symbol": "代号"})


def test_missing_fields_treats_explicit_null_as_missing():
    assert missing_fields({"tvl": None}, {"tvl": "TVL"})
    assert not missing_fields({"tvl": 0}, {"tvl": "TVL"})  # 0 是合法值，不算缺失


def test_render_has_a_title_header():
    assert "===== DexScreener =====" in render("DexScreener", {"a": 1})


def test_every_collector_probe_declares_expected_fields(config, db):
    """探针必须声明它依赖哪些字段，否则 --schema 只是打印一堆 JSON 没有校验价值。"""
    from chance_seeker.pipeline import build_collectors

    for name in ("dexscreener", "geckoterminal", "defillama", "free_attention"):
        config.collectors[name]["enabled"] = True

    probed = 0
    for collector in build_collectors(config, db):
        for probe in collector.schema_probes():
            probed += 1
            assert probe.url.startswith("https://"), probe.title
            assert probe.expected, f"{probe.title} 没有声明期望字段"
    assert probed >= 6
