from __future__ import annotations

import json

import pytest

from chance_seeker.collectors.dexscreener import DexScreenerCollector
from chance_seeker.collectors.evm_wallets import EvmWalletCollector
from chance_seeker.collectors.free_attention import _extract_symbols
from chance_seeker.collectors.geckoterminal import GeckoTerminalCollector
from chance_seeker.collectors.x_attention import QueryBudget, XAttentionCollector, _token_query
from chance_seeker.collectors.x_client.base import parse_time
from chance_seeker.collectors.x_client.twitterapi_io import TwitterApiIoClient
from chance_seeker.models import Entity, now_ts


def load(fixtures_dir, name):
    return json.loads((fixtures_dir / name).read_text(encoding="utf-8"))


def metrics_of(result, entity_key):
    return {o.metric: o.value for o in result.observations if o.entity_key == entity_key}


# ------------------------------------------------------------- DexScreener
def test_dexscreener_aggregates_multiple_pools(config, db, fixtures_dir, monkeypatch):
    collector = DexScreenerCollector(config, db)
    pairs = load(fixtures_dir, "dexscreener_tokens.json")
    result = collector._pairs_to_result("solana", pairs)

    key = Entity.token_key("solana", "Tok1111111111111111111111111111111111111")
    values = metrics_of(result, key)
    # 两个池子的流动性和成交量应该相加
    assert values["liquidity_usd"] == pytest.approx(500_000)
    assert values["volume_24h"] == pytest.approx(2_000_000)
    assert values["volume_1h"] == pytest.approx(340_000)
    # 代表性的价格/买卖数据取流动性最大的那个池子
    assert values["price_usd"] == pytest.approx(0.0421)
    assert values["buy_sell_ratio_1h"] == pytest.approx(420 / 120)
    assert values["vol_liq_ratio_1h"] == pytest.approx(340_000 / 500_000)
    assert values["age_minutes"] > 0

    entity = next(e for e in result.entities if e.key == key)
    assert entity.symbol == "ALPHA" and entity.chain == "solana"


def test_dexscreener_handles_missing_fields(config, db, fixtures_dir):
    collector = DexScreenerCollector(config, db)
    pairs = load(fixtures_dir, "dexscreener_tokens.json")
    key = Entity.token_key("solana", "Tok2222222222222222222222222222222222222")
    values = metrics_of(collector._pairs_to_result("solana", pairs), key)
    # 没有 sells 时买卖比退化成买单数，而不是崩掉或给出 inf
    assert values["buy_sell_ratio_1h"] == 10.0
    assert "age_minutes" not in values  # 缺 pairCreatedAt


def test_dexscreener_discovery_skips_unconfigured_chains(config, db, fixtures_dir, monkeypatch):
    collector = DexScreenerCollector(config, db)
    monkeypatch.setattr(collector.http, "get_json", lambda *a, **k: load(fixtures_dir, "dexscreener_boosts.json"))
    result = collector._discover("/token-boosts/latest/v1", boosts=True)
    chains = {e.chain for e in result.entities}
    assert chains == {"solana"}  # tron 不在 config 的 chains 里
    assert metrics_of(result, Entity.token_key("solana", "Tok3333333333333333333333333333333333333")) == {
        "dex_boosts": 500.0
    }


def test_dexscreener_watchlist_is_bounded(config, db):
    collector = DexScreenerCollector(config, db)
    for i in range(30):
        db.upsert_entity(Entity(kind="token", key=f"token:solana:t{i}", chain="solana", address=f"t{i}"))
    collector.settings["watchlist_size"] = 5
    assert len(collector._watchlist()) == 5


# ----------------------------------------------------------- GeckoTerminal
def test_geckoterminal_parses_pools(config, db, fixtures_dir, monkeypatch):
    collector = GeckoTerminalCollector(config, db)
    monkeypatch.setattr(collector.http, "get_json", lambda *a, **k: load(fixtures_dir, "geckoterminal_pools.json"))
    result = collector._fetch("solana", "solana", "trending_pools", 1)

    key = Entity.token_key("solana", "Tok4444444444444444444444444444444444444")
    values = metrics_of(result, key)
    assert values["gt_reserve_usd"] == pytest.approx(255_000)
    assert values["gt_volume_1h"] == pytest.approx(88_000)
    assert values["unique_buyers_1h"] == 150
    assert values["buyer_seller_ratio_1h"] == pytest.approx(2.5)
    assert values["gt_trending"] == 1.0
    assert next(e for e in result.entities if e.key == key).symbol == "GAMMA"


# ------------------------------------------------------------ EVM 钱包
def test_evm_wallet_classifies_direction_and_respects_cutoff(config, db, fixtures_dir, monkeypatch):
    settings = config.collectors["evm_wallets"]
    settings["enabled"] = True
    settings["api_key"] = "test-key"
    collector = EvmWalletCollector(config, db)
    monkeypatch.setattr(collector.http, "get_json", lambda *a, **k: load(fixtures_dir, "etherscan_tokentx.json"))

    result = collector._wallet_chain("0xWALLET", "smart", "base", 8453, "k", cutoff=1000)
    events = {e["token_key"]: e for e in db.recent_wallet_events()}
    assert events[Entity.token_key("base", "0xTOKEN1")]["direction"] == "in"
    assert events[Entity.token_key("base", "0xTOKEN1")]["amount"] == pytest.approx(1.5)
    assert events[Entity.token_key("base", "0xTOKEN2")]["direction"] == "out"
    assert events[Entity.token_key("base", "0xTOKEN2")]["amount"] == pytest.approx(2.0)
    # timeStamp=1 的那条早于 cutoff，应该被跳过
    assert Entity.token_key("base", "0xTOKEN3") not in events
    assert {e.symbol for e in result.entities} == {"GAM", "DLT"}


def test_smart_money_aggregation_counts_distinct_wallets(config, db):
    settings = config.collectors["evm_wallets"]
    settings["enabled"] = True
    settings["api_key"] = "k"
    collector = EvmWalletCollector(config, db)
    token_key = "token:base:0xabc"
    db.upsert_entity(Entity(kind="token", key=token_key, chain="base", address="0xabc"))
    for wallet in ("0xA", "0xB", "0xA"):  # 同一个钱包买两次只算一个
        db.record_wallet_event(
            wallet=wallet, label="w", chain="base", token_key=token_key,
            direction="in", amount=1, tx_hash=f"{wallet}-{now_ts()}", ts=now_ts(),
        )
    values = metrics_of(collector._aggregate(), token_key)
    assert values["smart_money_buyers"] == 2
    assert values["smart_money_net"] == 2


# ------------------------------------------------------------------ X
def test_twitterapi_parses_and_filters_by_time(fixtures_dir, monkeypatch):
    client = TwitterApiIoClient("key")
    monkeypatch.setattr(client.http, "get_json", lambda *a, **k: load(fixtures_dir, "twitterapi_search.json"))
    since = parse_time("Tue Aug 26 00:00:00 +0000 2026")
    tweets = client.search("$ALPHA", since_ts=since, limit=20)
    assert [t.id for t in tweets] == ["1", "2"]  # 第 3 条是前一天的
    assert tweets[0].author == "kolone"
    assert tweets[0].engagement == 300 + 40 * 2 + 12 + 5 * 2
    assert client.queries_used == 1


def test_x_attention_measures_authors_and_kols(config, db, fixtures_dir, monkeypatch):
    settings = config.collectors["x_attention"]
    settings.update({"enabled": True, "provider": "twitterapi_io", "api_key": "k", "kols": ["@KolOne"]})
    collector = XAttentionCollector(config, db)
    monkeypatch.setattr(
        collector.client.http, "get_json", lambda *a, **k: load(fixtures_dir, "twitterapi_search.json")
    )
    tweets = collector.client.search("q", since_ts=parse_time("Tue Aug 26 00:00:00 +0000 2026"))
    values = metrics_of(collector._measure("token:solana:x", tweets), "token:solana:x")
    assert values["x_mentions"] == 2
    assert values["x_unique_authors"] == 2
    assert values["x_kol_mentions"] == 1
    assert values["x_reach"] == 120_400


def test_x_budget_enforces_hourly_and_daily_caps(config, db):
    budget = QueryBudget(db, {"max_queries_per_run": 100, "max_queries_per_hour": 10, "max_queries_per_day": 15})
    assert budget.remaining() == 10
    budget.consume(8)
    assert budget.remaining() == 2
    budget.consume(7)
    assert budget.remaining() == 0  # 日上限 15 已用满


def test_x_budget_per_run_cap_is_the_tightest(config, db):
    budget = QueryBudget(db, {"max_queries_per_run": 3, "max_queries_per_hour": 100, "max_queries_per_day": 100})
    assert budget.remaining() == 3


def test_x_targets_prioritise_tokens_with_capital_signals(config, db):
    from chance_seeker.models import Signal

    settings = config.collectors["x_attention"]
    settings.update({"enabled": True, "provider": "null", "keywords": ["ai agent"], "auto_cashtag_top_n": 5})
    collector = XAttentionCollector(config, db)

    hot = Entity.token_key("solana", "hot")
    cold = Entity.token_key("solana", "cold")
    for key, addr in ((hot, "hot"), (cold, "cold")):
        db.upsert_entity(Entity(kind="token", key=key, chain="solana", address=addr, symbol=addr.upper()))
    db.save_signals([Signal(entity_key=hot, rule_id="r", family="capital", metric="m",
                            label="放量", score=80, value=1, baseline=0)])

    targets = collector._targets()
    keys = [t.entity_key for t in targets]
    assert keys[0] == "narrative:ai-agent"  # 关键词固定最高优先级
    assert hot in keys
    assert cold not in keys  # 没有资金信号的不烧 X 预算


def test_token_query_modes():
    assert _token_query("0xabc", "ALPHA", "address") == "0xabc"
    assert _token_query("0xabc", "ALPHA", "cashtag") == "$ALPHA"
    assert _token_query("0xabc", "ALPHA", "both") == "0xabc OR $ALPHA"
    assert _token_query(None, "ALPHA", "address") == "$ALPHA"
    assert _token_query(None, "not a symbol!", "cashtag") is None


def test_parse_time_formats():
    assert parse_time("2026-08-26T04:00:00Z") == 1787716800
    assert parse_time("Tue Aug 26 04:00:00 +0000 2026") == 1787716800
    assert parse_time(1787716800) == 1787716800
    assert parse_time(1787716800000) == 1787716800
    assert parse_time(None) == 0
    assert parse_time("garbage") == 0


def test_reddit_symbol_extraction_filters_majors():
    text = "aped into $ALPHA and $BETA today, sold $ETH and $ALL"
    assert _extract_symbols(text) == {"ALPHA", "BETA"}


# --------------------------------------------------- 榜单类信号的补零建模
def test_trending_zero_fills_entities_that_dropped_off(config, db, monkeypatch):
    """掉出榜单必须记成 0，否则「在榜」和「进榜」在时间序列上无法区分。"""
    from chance_seeker.collectors.free_attention import FreeAttentionCollector
    from chance_seeker.models import KIND_NARRATIVE

    collector = FreeAttentionCollector(config, db)

    def trending(names):
        return {"coins": [{"item": {"id": n, "symbol": n.upper(), "name": n}} for n in names]}

    # 第一轮：A、B 在榜
    monkeypatch.setattr(collector.http, "get_json", lambda *a, **k: trending(["aaa", "bbb"]))
    first = collector._coingecko()
    for entity in first.entities:
        db.upsert_entity(entity)
    db.record(first.observations)
    assert metrics_of(first, "narrative:cg-aaa")["coingecko_trending_score"] == 16.0

    # 第二轮：A 掉出，B 还在，C 新进
    monkeypatch.setattr(collector.http, "get_json", lambda *a, **k: trending(["bbb", "ccc"]))
    second = collector._coingecko()
    values = {o.entity_key: o.value for o in second.observations}
    assert values["narrative:cg-aaa"] == 0.0, "掉出榜单要补 0"
    assert values["narrative:cg-bbb"] == 16.0
    assert values["narrative:cg-ccc"] == 15.0
    assert any(e.kind == KIND_NARRATIVE for e in second.entities)


def test_trending_rule_ignores_permanent_residents(config, db):
    """BTC 常年在榜不是异动；新币进榜才是。"""
    from chance_seeker.detect.anomaly import AnomalyEngine

    engine = AnomalyEngine(config, db)
    rule = next(r for r in config.rules if r.id == "cg_trending")

    resident = [12.0] * 12          # 一直在榜，分数不动
    newcomer = [0.0] * 11 + [16.0]  # 补零之后，进榜表现为跃升

    assert engine.evaluate_rule("k", rule, resident) is None
    signal = engine.evaluate_rule("k", rule, newcomer)
    assert signal is not None and signal.score > 0


def test_trending_rule_ignores_low_ranks(config, db):
    """榜尾进出太频繁，min_value 把它们挡在外面。"""
    from chance_seeker.detect.anomaly import AnomalyEngine

    engine = AnomalyEngine(config, db)
    rule = next(r for r in config.rules if r.id == "cg_trending")
    assert engine.evaluate_rule("k", rule, [0.0] * 11 + [3.0]) is None


def test_reddit_all_failures_logs_one_clear_warning(config, db, monkeypatch, caplog):
    from chance_seeker.collectors.free_attention import FreeAttentionCollector

    db.upsert_entity(Entity(kind="token", key="token:solana:a", chain="solana", address="a", symbol="ALPHA"))
    collector = FreeAttentionCollector(config, db)
    monkeypatch.setattr(collector.http, "get_json", lambda *a, **k: None)

    with caplog.at_level("WARNING"):
        result = collector._reddit()
    assert not result.observations
    messages = [r.getMessage() for r in caplog.records]
    assert any("数据中心 IP" in m for m in messages)
    assert len([m for m in messages if "Reddit" in m]) == 1, "只应有一条汇总告警，不是每个子版块一条"


def test_geckoterminal_derives_age_from_pool_created_at(config, db, fixtures_dir, monkeypatch):
    """DexScreener 的部分端点不返回建池时间，GeckoTerminal 的必须补上。"""
    from chance_seeker.collectors.geckoterminal import GeckoTerminalCollector

    collector = GeckoTerminalCollector(config, db)
    monkeypatch.setattr(collector.http, "get_json", lambda *a, **k: load(fixtures_dir, "geckoterminal_pools.json"))
    result = collector._fetch("solana", "solana", "new_pools", 1)
    key = Entity.token_key("solana", "Tok4444444444444444444444444444444444444")
    assert metrics_of(result, key)["age_minutes"] > 0


def test_http_error_body_is_stripped_of_html():
    """403 拦截页会返回整页 HTML，原样打日志会把有用信息淹掉。"""
    from chance_seeker.collectors.http import _brief

    class FakeResponse:
        headers = {"Content-Type": "text/html"}
        text = "<html><head><style>.a{--x:1}</style></head><body><h1>Blocked</h1></body></html>"

    brief = _brief(FakeResponse())
    assert "<" not in brief and "Blocked" in brief and brief.startswith("(HTML ")

    class JsonResponse:
        headers = {"Content-Type": "application/json"}
        text = '{"error": "rate limited"}'

    assert _brief(JsonResponse()) == '{"error": "rate limited"}'
