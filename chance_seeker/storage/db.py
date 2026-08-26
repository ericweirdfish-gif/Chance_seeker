from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from chance_seeker.models import Entity, Observation, Signal, now_ts

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,
    chain       TEXT,
    address     TEXT,
    symbol      TEXT,
    name        TEXT,
    meta        TEXT NOT NULL DEFAULT '{}',
    first_seen  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities(kind);
CREATE INDEX IF NOT EXISTS idx_entities_last_seen ON entities(last_seen);

CREATE TABLE IF NOT EXISTS metrics (
    entity_id   INTEGER NOT NULL,
    metric      TEXT NOT NULL,
    ts          INTEGER NOT NULL,
    value       REAL NOT NULL,
    source      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (entity_id, metric, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_metrics_lookup ON metrics(entity_id, metric, ts DESC);

CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id   INTEGER NOT NULL,
    rule_id     TEXT NOT NULL,
    family      TEXT NOT NULL,
    metric      TEXT NOT NULL,
    label       TEXT NOT NULL DEFAULT '',
    score       REAL NOT NULL,
    value       REAL NOT NULL,
    baseline    REAL NOT NULL,
    detail      TEXT NOT NULL DEFAULT '{}',
    ts          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_entity ON signals(entity_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts DESC);

CREATE TABLE IF NOT EXISTS opportunities (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id        INTEGER NOT NULL,
    score            REAL NOT NULL,
    capital_score    REAL NOT NULL,
    attention_score  REAL NOT NULL,
    risk_penalty     REAL NOT NULL,
    cooccurrence     INTEGER NOT NULL DEFAULT 0,
    payload          TEXT NOT NULL DEFAULT '{}',
    alerted          INTEGER NOT NULL DEFAULT 0,
    ts               INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opps_ts ON opportunities(ts DESC);
CREATE INDEX IF NOT EXISTS idx_opps_entity ON opportunities(entity_id, ts DESC);

CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id    INTEGER NOT NULL,
    fingerprint  TEXT NOT NULL,
    score        REAL NOT NULL,
    channel      TEXT NOT NULL,
    status       TEXT NOT NULL,
    error        TEXT,
    ts           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_entity ON alerts(entity_id, ts DESC);

CREATE TABLE IF NOT EXISTS wallet_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet      TEXT NOT NULL,
    label       TEXT NOT NULL DEFAULT '',
    chain       TEXT NOT NULL,
    token_key   TEXT NOT NULL,
    symbol      TEXT,
    direction   TEXT NOT NULL,
    amount      REAL NOT NULL DEFAULT 0,
    usd_value   REAL,
    tx_hash     TEXT,
    ts          INTEGER NOT NULL,
    UNIQUE (wallet, token_key, tx_hash, direction, ts)
);
CREATE INDEX IF NOT EXISTS idx_wallet_events_token ON wallet_events(token_key, ts DESC);

CREATE TABLE IF NOT EXISTS kv (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL,
    ts     INTEGER NOT NULL
);
"""


class Database:
    """SQLite 持久层。单文件、零运维，够用到几百万个指标点。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 看板用 ThreadingHTTPServer，每个请求一个线程，所以连接必须允许跨线程使用。
        # Python 的 sqlite3 在 threadsafety==3（serialized）下共享连接是安全的；
        # 低于 3 的构建上退回到「看板另起进程」的用法，这里给出明确警告。
        if sqlite3.threadsafety < 3:
            log.warning(
                "当前 sqlite3 构建的 threadsafety=%d，跨线程共享连接不安全，"
                "请把看板（serve）和采集（run）放在两个进程里跑",
                sqlite3.threadsafety,
            )
        self.conn = sqlite3.connect(str(self.path), timeout=30.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ 实体
    def upsert_entity(self, entity: Entity) -> int:
        ts = now_ts()
        cur = self.conn.execute("SELECT id, meta FROM entities WHERE key = ?", (entity.key,))
        row = cur.fetchone()
        if row is None:
            cur = self.conn.execute(
                """INSERT INTO entities (key, kind, chain, address, symbol, name, meta, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entity.key,
                    entity.kind,
                    entity.chain,
                    entity.address,
                    entity.symbol,
                    entity.name,
                    json.dumps(entity.meta, ensure_ascii=False),
                    entity.first_seen or ts,
                    ts,
                ),
            )
            self.conn.commit()
            return int(cur.lastrowid)

        merged = json.loads(row["meta"] or "{}")
        merged.update({k: v for k, v in entity.meta.items() if v is not None})
        self.conn.execute(
            """UPDATE entities
               SET symbol = COALESCE(?, symbol),
                   name   = COALESCE(?, name),
                   chain  = COALESCE(?, chain),
                   address= COALESCE(?, address),
                   meta   = ?,
                   last_seen = ?
             WHERE id = ?""",
            (
                entity.symbol,
                entity.name,
                entity.chain,
                entity.address,
                json.dumps(merged, ensure_ascii=False),
                ts,
                row["id"],
            ),
        )
        self.conn.commit()
        return int(row["id"])

    def get_entity(self, key: str) -> Entity | None:
        row = self.conn.execute("SELECT * FROM entities WHERE key = ?", (key,)).fetchone()
        return _row_to_entity(row) if row else None

    def get_entity_by_id(self, entity_id: int) -> Entity | None:
        row = self.conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
        return _row_to_entity(row) if row else None

    def entity_id(self, key: str) -> int | None:
        row = self.conn.execute("SELECT id FROM entities WHERE key = ?", (key,)).fetchone()
        return int(row["id"]) if row else None

    def list_entities(self, kind: str | None = None, seen_within: int | None = None) -> list[Entity]:
        sql = "SELECT * FROM entities WHERE 1=1"
        params: list[Any] = []
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if seen_within:
            sql += " AND last_seen >= ?"
            params.append(now_ts() - seen_within)
        sql += " ORDER BY last_seen DESC"
        return [_row_to_entity(r) for r in self.conn.execute(sql, params)]

    # ------------------------------------------------------------------ 指标
    def record(self, observations: Iterable[Observation]) -> int:
        rows: list[tuple[Any, ...]] = []
        cache: dict[str, int | None] = {}
        for obs in observations:
            if obs.entity_key not in cache:
                cache[obs.entity_key] = self.entity_id(obs.entity_key)
            eid = cache[obs.entity_key]
            if eid is None:
                log.debug("跳过未知实体的指标: %s", obs.entity_key)
                continue
            if obs.value is None:
                continue
            rows.append((eid, obs.metric, obs.ts, float(obs.value), obs.source))
        if not rows:
            return 0
        self.conn.executemany(
            "INSERT OR REPLACE INTO metrics (entity_id, metric, ts, value, source) VALUES (?,?,?,?,?)",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def series(self, entity_key: str, metric: str, limit: int = 200) -> list[tuple[int, float]]:
        eid = self.entity_id(entity_key)
        if eid is None:
            return []
        rows = self.conn.execute(
            "SELECT ts, value FROM metrics WHERE entity_id = ? AND metric = ? ORDER BY ts DESC LIMIT ?",
            (eid, metric, limit),
        ).fetchall()
        return [(int(r["ts"]), float(r["value"])) for r in reversed(rows)]

    def latest_metrics(self, entity_key: str, max_age: int = 86_400) -> dict[str, float]:
        eid = self.entity_id(entity_key)
        if eid is None:
            return {}
        rows = self.conn.execute(
            """SELECT metric, value FROM metrics m
               WHERE entity_id = ? AND ts >= ?
                 AND ts = (SELECT MAX(ts) FROM metrics WHERE entity_id = m.entity_id AND metric = m.metric)""",
            (eid, now_ts() - max_age),
        ).fetchall()
        return {r["metric"]: float(r["value"]) for r in rows}

    def metric_names(self, entity_key: str) -> list[str]:
        eid = self.entity_id(entity_key)
        if eid is None:
            return []
        rows = self.conn.execute(
            "SELECT DISTINCT metric FROM metrics WHERE entity_id = ?", (eid,)
        ).fetchall()
        return [r["metric"] for r in rows]

    def prune_metrics(self, keep_points: int = 1500) -> int:
        """按实体+指标保留最近 N 个点，控制 SQLite 文件体积。"""
        # metrics 是 WITHOUT ROWID 表，只能用主键三元组来定位要删的行
        removed = self.conn.execute(
            """DELETE FROM metrics WHERE (entity_id, metric, ts) IN (
                   SELECT entity_id, metric, ts FROM (
                       SELECT entity_id, metric, ts,
                              ROW_NUMBER() OVER (PARTITION BY entity_id, metric ORDER BY ts DESC) AS rn
                         FROM metrics
                   ) WHERE rn > ?
               )""",
            (keep_points,),
        ).rowcount
        self.conn.commit()
        return max(removed, 0)

    # ------------------------------------------------------------------ 信号
    def save_signals(self, signals: Sequence[Signal]) -> int:
        rows = []
        for sig in signals:
            eid = self.entity_id(sig.entity_key)
            if eid is None:
                continue
            rows.append(
                (
                    eid,
                    sig.rule_id,
                    sig.family,
                    sig.metric,
                    sig.label,
                    sig.score,
                    sig.value,
                    sig.baseline,
                    json.dumps(sig.detail, ensure_ascii=False),
                    sig.ts,
                )
            )
        if not rows:
            return 0
        self.conn.executemany(
            """INSERT INTO signals (entity_id, rule_id, family, metric, label, score, value, baseline, detail, ts)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def recent_signals(self, entity_key: str, within_seconds: int) -> list[Signal]:
        eid = self.entity_id(entity_key)
        if eid is None:
            return []
        rows = self.conn.execute(
            "SELECT * FROM signals WHERE entity_id = ? AND ts >= ? ORDER BY ts DESC",
            (eid, now_ts() - within_seconds),
        ).fetchall()
        return [
            Signal(
                entity_key=entity_key,
                rule_id=r["rule_id"],
                family=r["family"],
                metric=r["metric"],
                label=r["label"],
                score=float(r["score"]),
                value=float(r["value"]),
                baseline=float(r["baseline"]),
                detail=json.loads(r["detail"] or "{}"),
                ts=int(r["ts"]),
                id=int(r["id"]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------- 机会与告警
    def save_opportunity(self, entity_key: str, payload: dict[str, Any]) -> int | None:
        eid = self.entity_id(entity_key)
        if eid is None:
            return None
        cur = self.conn.execute(
            """INSERT INTO opportunities
               (entity_id, score, capital_score, attention_score, risk_penalty, cooccurrence, payload, alerted, ts)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                eid,
                payload.get("score", 0.0),
                payload.get("capital_score", 0.0),
                payload.get("attention_score", 0.0),
                payload.get("risk_penalty", 0.0),
                1 if payload.get("cooccurrence") else 0,
                json.dumps(payload, ensure_ascii=False),
                1 if payload.get("alerted") else 0,
                payload.get("ts", now_ts()),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def mark_opportunity_alerted(self, opportunity_id: int) -> None:
        self.conn.execute("UPDATE opportunities SET alerted = 1 WHERE id = ?", (opportunity_id,))
        self.conn.commit()

    def last_alert(self, entity_key: str) -> sqlite3.Row | None:
        eid = self.entity_id(entity_key)
        if eid is None:
            return None
        return self.conn.execute(
            "SELECT * FROM alerts WHERE entity_id = ? AND status = 'sent' ORDER BY ts DESC LIMIT 1",
            (eid,),
        ).fetchone()

    def record_alert(
        self, entity_key: str, fingerprint: str, score: float, channel: str, status: str, error: str | None = None
    ) -> None:
        eid = self.entity_id(entity_key)
        if eid is None:
            return
        self.conn.execute(
            "INSERT INTO alerts (entity_id, fingerprint, score, channel, status, error, ts) VALUES (?,?,?,?,?,?,?)",
            (eid, fingerprint, score, channel, status, error, now_ts()),
        )
        self.conn.commit()

    def recent_opportunities(self, limit: int = 50, min_score: float = 0.0) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT o.*, e.key, e.symbol, e.name, e.chain, e.address
                 FROM opportunities o JOIN entities e ON e.id = o.entity_id
                WHERE o.score >= ?
                ORDER BY o.ts DESC LIMIT ?""",
            (min_score, limit),
        ).fetchall()
        out = []
        for r in rows:
            payload = json.loads(r["payload"] or "{}")
            payload.update(
                {
                    "id": r["id"],
                    "entity_key": r["key"],
                    "symbol": r["symbol"],
                    "name": r["name"],
                    "chain": r["chain"],
                    "address": r["address"],
                    "alerted": bool(r["alerted"]),
                    "ts": int(r["ts"]),
                }
            )
            out.append(payload)
        return out

    # ------------------------------------------------------------ 钱包事件
    def record_wallet_event(self, **kwargs: Any) -> bool:
        try:
            self.conn.execute(
                """INSERT OR IGNORE INTO wallet_events
                   (wallet, label, chain, token_key, symbol, direction, amount, usd_value, tx_hash, ts)
                   VALUES (:wallet,:label,:chain,:token_key,:symbol,:direction,:amount,:usd_value,:tx_hash,:ts)""",
                {
                    "wallet": kwargs["wallet"],
                    "label": kwargs.get("label", ""),
                    "chain": kwargs["chain"],
                    "token_key": kwargs["token_key"],
                    "symbol": kwargs.get("symbol"),
                    "direction": kwargs["direction"],
                    "amount": float(kwargs.get("amount", 0) or 0),
                    "usd_value": kwargs.get("usd_value"),
                    "tx_hash": kwargs.get("tx_hash"),
                    "ts": int(kwargs.get("ts", now_ts())),
                },
            )
            self.conn.commit()
            return True
        except sqlite3.Error as exc:  # pragma: no cover - 防御性
            log.warning("写入钱包事件失败: %s", exc)
            return False

    def distinct_wallet_buyers(self, token_key: str, within_seconds: int) -> int:
        row = self.conn.execute(
            """SELECT COUNT(DISTINCT wallet) AS n FROM wallet_events
                WHERE token_key = ? AND direction = 'in' AND ts >= ?""",
            (token_key, now_ts() - within_seconds),
        ).fetchone()
        return int(row["n"]) if row else 0

    def recent_wallet_events(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM wallet_events ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ KV
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def kv_set(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO kv (key, value, ts) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, ts = excluded.ts",
            (key, json.dumps(value, ensure_ascii=False), now_ts()),
        )
        self.conn.commit()

    def stats(self) -> dict[str, int]:
        def count(table: str) -> int:
            return int(self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])

        return {
            "entities": count("entities"),
            "metrics": count("metrics"),
            "signals": count("signals"),
            "opportunities": count("opportunities"),
            "alerts": count("alerts"),
            "wallet_events": count("wallet_events"),
        }


def _row_to_entity(row: sqlite3.Row) -> Entity:
    return Entity(
        id=int(row["id"]),
        key=row["key"],
        kind=row["kind"],
        chain=row["chain"],
        address=row["address"],
        symbol=row["symbol"],
        name=row["name"],
        meta=json.loads(row["meta"] or "{}"),
        first_seen=int(row["first_seen"]),
        last_seen=int(row["last_seen"]),
    )
