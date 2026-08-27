from __future__ import annotations

import os

import pytest

from chance_seeker.alerts.renderer import links_for, render_markdown, render_plain, summary_line
from chance_seeker.config import load_config
from chance_seeker.detect.fusion import score_opportunity
from chance_seeker.models import Entity, Signal


def _opportunity(config, db, chain="solana", address="Tok111"):
    entity = Entity(kind="token", key=Entity.token_key(chain, address), chain=chain,
                    address=address, symbol="ALPHA", name="Alpha Token",
                    meta={"dexscreener_url": "https://dexscreener.com/solana/pair1"})
    db.upsert_entity(entity)
    signals = [
        Signal(entity_key=entity.key, rule_id="vol_1h_spike", family="capital", metric="volume_1h",
               label="1h 成交量异常放大", score=88, value=900000, baseline=50000, detail={"observed": 12.4}),
        Signal(entity_key=entity.key, rule_id="x_author_spread", family="attention", metric="x_unique_authors",
               label="讨论人数扩散", score=71, value=180, baseline=4, detail={"observed": 45.0}),
    ]
    metrics = {"liquidity_usd": 410000, "market_cap_usd": 8200000, "volume_24h": 1850000,
               "volume_1h": 340000, "price_change_1h": 28.7, "buy_sell_ratio_1h": 3.5,
               "x_mentions": 300, "x_unique_authors": 180, "x_kol_mentions": 3}
    return score_opportunity(config, db, db.get_entity(entity.key), signals, metrics)


def test_markdown_contains_the_essentials(config, db):
    text = render_markdown(_opportunity(config, db))
    assert "ALPHA" in text
    assert "1h 成交量异常放大" in text
    assert "讨论人数扩散" in text
    assert "$410.0K" in text          # 流动性被格式化
    assert "$8.20M" in text           # 市值
    assert "dexscreener.com" in text
    assert "x.com/search" in text     # 一键去 X 看原始讨论
    assert "Tok111" in text           # 合约地址可复制


def test_plain_strips_markdown_markers(config, db):
    text = render_plain(_opportunity(config, db))
    assert "**" not in text and "`" not in text


def test_links_per_chain(config, db):
    assert "solscan.io" in links_for(_opportunity(config, db))["区块浏览器"]
    base = links_for(_opportunity(config, db, chain="base", address="0xabc"))
    assert "basescan.org" in base["区块浏览器"]
    assert "gmgn.ai/base/token/0xabc" in base["GMGN"]


def test_links_empty_without_address(config, db):
    entity = Entity(kind="narrative", key="narrative:ai", name="AI")
    db.upsert_entity(entity)
    opportunity = score_opportunity(config, db, db.get_entity("narrative:ai"), [], {})
    assert links_for(opportunity) == {}
    assert "AI" in render_markdown(opportunity)  # 没有地址时退化成名称，不应该崩


def test_summary_line(config, db):
    line = summary_line(_opportunity(config, db))
    assert "ALPHA" in line and "solana" in line and "分" in line


# ------------------------------------------------------------------ 配置
def test_env_interpolation(project, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "456")
    config = load_config(project / "config" / "config.yaml", root=project)
    assert config.alerts["telegram"]["bot_token"] == "123:abc"
    assert config.alerts["telegram"]["chat_id"] == "456"


def test_missing_env_becomes_none(project, monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    config = load_config(project / "config" / "config.yaml", root=project)
    assert config.alerts["discord"]["webhook_url"] is None


def test_rules_parsed_with_defaults(config):
    rule = next(r for r in config.rules if r.id == "vol_1h_spike")
    assert rule.family == "capital" and rule.method == "robust_z"
    assert rule.threshold == 3.0 and rule.min_value == 30000
    assert rule.lookback == 1 and rule.direction == "up"
    families = {r.family for r in config.rules}
    assert families == {"capital", "attention", "risk"}


def test_enabled_chains_and_db_path(config, project):
    enabled = {c.name for c in config.enabled_chains()}
    assert {"solana", "bsc", "robinhood"} <= enabled, "这三条链是当前监控重点"
    assert "ethereum" not in enabled, "主网 gas 太贵、meme 稀少，默认关闭"
    assert config.db_path == project / "data" / "chance.db"
    assert config.chains["base"].chain_id == 8453
    assert config.chains["bsc"].chain_id == 56


def test_chain_identifiers_match_what_was_probed(config):
    """链标识是实测出来的，写错会导致静默采不到数据。"""
    assert config.chains["bsc"].geckoterminal_network == "bsc"
    assert config.chains["bsc"].dexscreener_chain == "bsc"
    assert config.chains["robinhood"].geckoterminal_network == "robinhood"
    assert config.chains["robinhood"].dexscreener_chain == "robinhood"


def test_missing_config_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(root=tmp_path)


def test_dotenv_is_loaded(tmp_path, monkeypatch):
    import shutil
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    (tmp_path / "config").mkdir()
    shutil.copy(root / "config" / "config.example.yaml", tmp_path / "config" / "config.yaml")
    (tmp_path / ".env").write_text("TELEGRAM_CHAT_ID=from-dotenv\n", encoding="utf-8")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    config = load_config(tmp_path / "config" / "config.yaml", root=tmp_path)
    assert config.alerts["telegram"]["chat_id"] == "from-dotenv"
    os.environ.pop("TELEGRAM_CHAT_ID", None)


def test_signal_measure_uses_the_right_unit(config, db):
    """百分比变化不能被写成「倍数」，否则读告警时会误判幅度。"""
    from chance_seeker.alerts.renderer import _fmt_measure

    def s(method, observed):
        return Signal(entity_key="k", rule_id="r", family="capital", metric="m", label="l",
                      score=50, value=1, baseline=1, detail={"observed": observed, "method": method})

    assert _fmt_measure(s("robust_z", 12.3)) == "z=12.3"
    assert _fmt_measure(s("ratio", 6.0)) == "6.0× 基线"
    assert _fmt_measure(s("delta_pct", 35.1)) == "+35.1%"
    assert _fmt_measure(s("delta_pct", -76.1)) == "-76.1%"
    assert _fmt_measure(s("acceleration", 3.2)) == "加速 3.2×"
    assert _fmt_measure(s("level", 3.0)) == ""


def test_short_name_falls_back_to_abbreviated_address(config, db):
    """刚发现、还没拿到代号的代币不该在日志里打出整条实体 key。"""
    from chance_seeker.alerts.renderer import short_name

    addr = "aeeytijohqt1xejccls3ms3eovgzrz2fvzzyczxepump"
    bare = Entity(kind="token", key=Entity.token_key("solana", addr), chain="solana", address=addr)
    assert short_name(bare) == "aeeyti…pump"
    assert "token:solana:" not in short_name(bare)

    assert short_name(Entity(kind="token", key="k", symbol="ALPHA", name="Alpha")) == "ALPHA"
    assert short_name(Entity(kind="token", key="k", name="Alpha")) == "Alpha"
    assert short_name(Entity(kind="narrative", key="narrative:ai")) == "narrative:ai"


# --------------------------------------------------- 告警渠道自动启用
def test_channels_auto_enable_when_credentials_present(config, monkeypatch, caplog):
    """填了 Secret 却因为 enabled: false 收不到推送，是最常见的坑。"""
    from chance_seeker.alerts import build_channels

    config.alerts["telegram"].update({"enabled": "auto", "bot_token": "", "chat_id": ""})
    with caplog.at_level("WARNING"):
        names = {c.name for c in build_channels(config)}
    assert "telegram" not in names
    assert not [r for r in caplog.records if "初始化失败" in r.getMessage()], "缺凭证时不该刷警告"

    config.alerts["telegram"].update({"bot_token": "123:abc", "chat_id": "456"})
    assert "telegram" in {c.name for c in build_channels(config)}


def test_explicit_true_without_credentials_warns_loudly(config, caplog):
    """显式开启却没凭证是配置错误，必须说出来，否则用户不知道为什么没消息。"""
    from chance_seeker.alerts import build_channels

    config.alerts["telegram"].update({"enabled": True, "bot_token": "", "chat_id": ""})
    with caplog.at_level("WARNING"):
        build_channels(config)
    assert any("初始化失败" in r.getMessage() for r in caplog.records)


def test_explicit_false_stays_off_even_with_credentials(config):
    from chance_seeker.alerts import build_channels

    config.alerts["discord"].update({"enabled": False, "webhook_url": "https://example.com/hook"})
    assert "discord" not in {c.name for c in build_channels(config)}
