from __future__ import annotations

import json
import os
import threading
import time
from urllib.request import urlopen

import pytest

from chance_seeker.cli import main
from chance_seeker.storage import Database
from chance_seeker.web.server import serve


@pytest.fixture()
def cwd(project, monkeypatch):
    monkeypatch.chdir(project)
    return project


def test_init_creates_config_and_db(tmp_path, monkeypatch, capsys):
    import shutil
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    (tmp_path / "config").mkdir()
    shutil.copy(root / "config" / "config.example.yaml", tmp_path / "config" / "config.example.yaml")
    monkeypatch.chdir(tmp_path)

    assert main(["init"]) == 0
    assert (tmp_path / "config" / "config.yaml").exists()
    assert (tmp_path / "data" / "chance.db").exists()
    assert "已初始化数据库" in capsys.readouterr().out


def test_demo_seeds_data_and_scores_it(cwd, capsys):
    assert main(["demo"]) == 0
    out = capsys.readouterr().out
    assert "DEMOA" in out and "DEMOC" in out

    with Database(cwd / "data" / "chance.db") as db:
        assert db.stats()["metrics"] > 500
        assert db.stats()["signals"] > 0


def test_demo_ranks_resonance_above_quiet_money(cwd):
    """DEMOA（资金+注意力共振）应该比 DEMOB（只有资金）分高。"""
    from chance_seeker.config import load_config
    from chance_seeker.detect.anomaly import AnomalyEngine
    from chance_seeker.detect.fusion import score_opportunity
    from chance_seeker.models import Entity

    main(["demo"])
    config = load_config(cwd / "config" / "config.yaml", root=cwd)
    with Database(config.db_path) as db:
        engine = AnomalyEngine(config, db)
        scores = {}
        for symbol, chain, address in (
            ("DEMOA", "solana", "DemoA1111111111111111111111111111111111111"),
            ("DEMOB", "solana", "DemoB2222222222222222222222222222222222222"),
            ("DEMOC", "base", "0xdec0000000000000000000000000000000000000"),
        ):
            key = Entity.token_key(chain, address)
            entity = db.get_entity(key)
            signals = engine.evaluate_entity(key)
            scores[symbol] = score_opportunity(config, db, entity, signals, db.latest_metrics(key))

        from chance_seeker.detect.fusion import should_alert

        # 共振 > 只有资金
        assert scores["DEMOA"].score > scores["DEMOB"].score
        assert scores["DEMOA"].cooccurrence is True
        assert should_alert(config, db, scores["DEMOA"])[0] is True

        # 只有资金没有注意力：分数中等，不值得推送
        assert scores["DEMOB"].attention_score == 0
        assert should_alert(config, db, scores["DEMOB"])[0] is False

        # 抽流动性的陷阱：即使又热闹又放量，也必须被否决
        assert scores["DEMOC"].risk_penalty > 0
        ok, reason = should_alert(config, db, scores["DEMOC"])
        assert not ok and "一票否决" in reason


def test_detect_and_top_and_stats_commands(cwd, capsys):
    main(["demo"])
    capsys.readouterr()
    assert main(["detect", "--top", "5"]) == 0
    assert "DEMO" in capsys.readouterr().out
    assert main(["stats"]) == 0
    assert "entities" in capsys.readouterr().out
    assert main(["top"]) == 0


def test_test_alert_uses_the_console_channel_by_default(cwd, capsys):
    assert main(["test-alert"]) == 0
    out = capsys.readouterr().out
    assert "测试消息" in out and "✅ console" in out


def test_test_alert_fails_when_no_channel_is_enabled(cwd, capsys):
    config_path = cwd / "config" / "config.yaml"
    disabled = config_path.read_text(encoding="utf-8").replace(
        "  console:\n    enabled: true", "  console:\n    enabled: false"
    )
    config_path.write_text(disabled, encoding="utf-8")
    assert main(["test-alert"]) == 1
    assert "没有启用任何告警渠道" in capsys.readouterr().out


def test_web_api_serves_opportunities(cwd):
    from chance_seeker.config import load_config

    main(["demo"])
    config = load_config(cwd / "config" / "config.yaml", root=cwd)
    db = Database(config.db_path)
    thread = threading.Thread(target=serve, args=(config, db, "127.0.0.1", 8899), daemon=True)
    thread.start()

    try:
        for _ in range(50):
            try:
                with urlopen("http://127.0.0.1:8899/api/stats", timeout=1) as resp:
                    stats = json.loads(resp.read())
                break
            except Exception:  # 等服务器起来
                time.sleep(0.05)
        else:
            raise AssertionError("本地 HTTP 服务未能启动")

        assert stats["entities"] >= 3
        with urlopen("http://127.0.0.1:8899/", timeout=2) as resp:
            assert b"Chance Seeker" in resp.read()
        with urlopen("http://127.0.0.1:8899/api/opportunities?limit=5", timeout=2) as resp:
            assert isinstance(json.loads(resp.read()), list)
        with urlopen(
            "http://127.0.0.1:8899/api/series?entity=token:solana:demoa1111111111111111111111111111111111111"
            "&metrics=volume_1h",
            timeout=2,
        ) as resp:
            payload = json.loads(resp.read())
        assert len(payload["series"]["volume_1h"]) > 10
    finally:
        db.close()


def test_unknown_command_exits_nonzero():
    with pytest.raises(SystemExit):
        main(["nope"])


def test_missing_config_is_a_clean_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.environ.pop("CHANCE_SEEKER_CONFIG", None)
    assert main(["stats"]) == 1


def test_demo_is_idempotent(cwd, capsys):
    """重跑 demo 不能把新旧序列叠起来，否则形态（尤其是抽流动性）会被抹平。"""
    from chance_seeker.config import load_config
    from chance_seeker.detect.anomaly import AnomalyEngine
    from chance_seeker.models import Entity

    config = load_config(cwd / "config" / "config.yaml", root=cwd)
    rugged = Entity.token_key("base", "0xdec0000000000000000000000000000000000000")

    fired = []
    for _ in range(3):
        main(["demo"])
        capsys.readouterr()
        with Database(config.db_path) as db:
            assert db.stats()["metrics"] < 3000, "重跑不应该无限累积指标点"
            fired.append({s.rule_id for s in AnomalyEngine(config, db).evaluate_entity(rugged)})

    assert fired[0] == fired[1] == fired[2]
    assert "liq_drain" in fired[0], "断崖式抽流动性每次都必须被检出"


def test_prune_command_shrinks_the_database(cwd, capsys):
    """Actions 用缓存带走数据库，体积失控会把 pip 缓存挤出仓库配额。"""
    from chance_seeker.config import load_config
    from chance_seeker.models import Entity, Observation, now_ts

    config = load_config(cwd / "config" / "config.yaml", root=cwd)
    with Database(config.db_path) as db:
        key = "token:solana:abc"
        db.upsert_entity(Entity(kind="token", key=key, chain="solana", address="abc"))
        base = now_ts()
        for metric in ("a", "b", "c"):
            db.record(
                [Observation(entity_key=key, metric=metric, value=float(i), ts=base + i) for i in range(2000)]
            )
        assert db.stats()["metrics"] == 6000

    assert main(["prune", "--keep", "50", "--vacuum"]) == 0
    out = capsys.readouterr().out
    assert "清理了 5850 个指标点" in out

    with Database(config.db_path) as db:
        assert db.stats()["metrics"] == 150
