from __future__ import annotations

from chance_seeker.models import Entity, Observation, Signal, now_ts


def test_upsert_entity_merges_instead_of_duplicating(db):
    key = Entity.token_key("solana", "ABC")
    first = db.upsert_entity(Entity(kind="token", key=key, chain="solana", address="ABC"))
    second = db.upsert_entity(
        Entity(kind="token", key=key, chain="solana", address="ABC", symbol="ABC", meta={"dex_id": "raydium"})
    )
    assert first == second
    entity = db.get_entity(key)
    assert entity is not None
    assert entity.symbol == "ABC"
    assert entity.meta["dex_id"] == "raydium"
    assert len(db.list_entities(kind="token")) == 1


def test_upsert_does_not_null_out_existing_fields(db):
    key = Entity.token_key("base", "0x1")
    db.upsert_entity(Entity(kind="token", key=key, chain="base", address="0x1", symbol="AAA", name="Alpha"))
    db.upsert_entity(Entity(kind="token", key=key, chain="base", address="0x1"))
    entity = db.get_entity(key)
    assert entity.symbol == "AAA" and entity.name == "Alpha"


def test_record_skips_unknown_entities(db):
    db.upsert_entity(Entity(kind="token", key="token:solana:a", chain="solana", address="a"))
    written = db.record(
        [
            Observation(entity_key="token:solana:a", metric="volume_1h", value=10),
            Observation(entity_key="token:solana:missing", metric="volume_1h", value=99),
        ]
    )
    assert written == 1
    assert db.series("token:solana:a", "volume_1h") == [(db.series("token:solana:a", "volume_1h")[0][0], 10.0)]


def test_series_returns_chronological_order(db):
    key = "token:solana:a"
    db.upsert_entity(Entity(kind="token", key=key, chain="solana", address="a"))
    base = now_ts()
    db.record([Observation(entity_key=key, metric="v", value=float(i), ts=base + i) for i in range(5)])
    assert [v for _, v in db.series(key, "v")] == [0.0, 1.0, 2.0, 3.0, 4.0]


def test_latest_metrics_picks_newest_point_per_metric(db):
    key = "token:solana:a"
    db.upsert_entity(Entity(kind="token", key=key, chain="solana", address="a"))
    base = now_ts()
    db.record(
        [
            Observation(entity_key=key, metric="price_usd", value=1.0, ts=base - 60),
            Observation(entity_key=key, metric="price_usd", value=2.0, ts=base),
            Observation(entity_key=key, metric="liquidity_usd", value=500.0, ts=base - 30),
        ]
    )
    assert db.latest_metrics(key) == {"price_usd": 2.0, "liquidity_usd": 500.0}


def test_latest_metrics_ignores_stale_points(db):
    key = "token:solana:a"
    db.upsert_entity(Entity(kind="token", key=key, chain="solana", address="a"))
    db.record([Observation(entity_key=key, metric="price_usd", value=1.0, ts=now_ts() - 100000)])
    assert db.latest_metrics(key, max_age=3600) == {}


def test_prune_metrics_keeps_most_recent(db):
    key = "token:solana:a"
    db.upsert_entity(Entity(kind="token", key=key, chain="solana", address="a"))
    base = now_ts()
    db.record([Observation(entity_key=key, metric="v", value=float(i), ts=base + i) for i in range(50)])
    db.prune_metrics(keep_points=10)
    series = db.series(key, "v", limit=100)
    assert len(series) == 10
    assert [v for _, v in series] == [float(i) for i in range(40, 50)]


def test_wallet_events_dedupe_and_count(db):
    payload = dict(
        wallet="0xW", label="smart", chain="base", token_key="token:base:0xt",
        direction="in", amount=1.0, tx_hash="0xhash", ts=now_ts(),
    )
    db.record_wallet_event(**payload)
    db.record_wallet_event(**payload)
    assert len(db.recent_wallet_events()) == 1
    db.record_wallet_event(**{**payload, "wallet": "0xW2", "tx_hash": "0xhash2"})
    assert db.distinct_wallet_buyers("token:base:0xt", 3600) == 2


def test_signals_and_alerts_roundtrip(db):
    key = "token:solana:a"
    db.upsert_entity(Entity(kind="token", key=key, chain="solana", address="a"))
    db.save_signals(
        [Signal(entity_key=key, rule_id="r1", family="capital", metric="volume_1h",
                label="放量", score=70, value=100, baseline=10)]
    )
    assert len(db.recent_signals(key, 3600)) == 1
    assert db.last_alert(key) is None
    db.record_alert(key, "fp", 80.0, "console", "sent")
    assert db.last_alert(key)["score"] == 80.0


def test_kv_roundtrip(db):
    assert db.kv_get("missing", 7) == 7
    db.kv_set("budget", {"used": 3})
    assert db.kv_get("budget") == {"used": 3}


def test_stats(db):
    stats = db.stats()
    assert set(stats) == {"entities", "metrics", "signals", "opportunities", "alerts", "wallet_events"}
    assert all(v == 0 for v in stats.values())
