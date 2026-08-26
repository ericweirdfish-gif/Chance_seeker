from __future__ import annotations

import json
import logging
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from chance_seeker.config import Config
from chance_seeker.models import KIND_TOKEN, now_ts
from chance_seeker.storage import Database

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class Handler(BaseHTTPRequestHandler):
    """只用标准库，不引入 Flask/FastAPI —— 看板就是几个 JSON 接口加一个页面。"""

    server_version = "chance-seeker"

    def __init__(self, *args, config: Config, db: Database, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.config = config
        self.db = db
        super().__init__(*args, **kwargs)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        try:
            if route == "/":
                return self._file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            if route == "/app.js":
                return self._file(STATIC_DIR / "app.js", "application/javascript; charset=utf-8")
            if route == "/style.css":
                return self._file(STATIC_DIR / "style.css", "text/css; charset=utf-8")
            if route == "/api/stats":
                return self._json(self._stats())
            if route == "/api/opportunities":
                return self._json(
                    self.db.recent_opportunities(
                        limit=_int(query, "limit", 60), min_score=_float(query, "min_score", 0.0)
                    )
                )
            if route == "/api/series":
                return self._json(self._series(query))
            if route == "/api/wallet-events":
                return self._json(self.db.recent_wallet_events(limit=_int(query, "limit", 50)))
        except Exception as exc:  # pragma: no cover - 看板出错不应该影响主流程
            log.exception("看板请求失败: %s", self.path)
            return self._json({"error": str(exc)}, status=500)

        self._json({"error": "not found"}, status=404)

    # ------------------------------------------------------------------
    def _stats(self) -> dict:
        stats = self.db.stats()
        recent = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM signals WHERE ts >= ?", (now_ts() - 86400,)
        ).fetchone()
        alerts = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM alerts WHERE status='sent' AND ts >= ?", (now_ts() - 86400,)
        ).fetchone()
        tokens = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM entities WHERE kind = ? AND last_seen >= ?",
            (KIND_TOKEN, now_ts() - 86400),
        ).fetchone()
        stats.update(
            {
                "signals_24h": int(recent["n"]),
                "alerts_24h": int(alerts["n"]),
                "active_tokens_24h": int(tokens["n"]),
            }
        )
        return stats

    def _series(self, query: dict) -> dict:
        entity_key = (query.get("entity") or [""])[0]
        if not entity_key:
            return {"error": "missing entity"}
        metrics = (query.get("metrics") or [""])[0]
        names = [m for m in metrics.split(",") if m] or self.db.metric_names(entity_key)
        limit = _int(query, "limit", 200)
        entity = self.db.get_entity(entity_key)
        return {
            "entity": {
                "key": entity_key,
                "symbol": entity.symbol if entity else None,
                "name": entity.name if entity else None,
                "chain": entity.chain if entity else None,
                "address": entity.address if entity else None,
            },
            "series": {name: self.db.series(entity_key, name, limit) for name in names},
        }

    # ------------------------------------------------------------------
    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            return self._json({"error": f"missing {path.name}"}, status=404)
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _int(query: dict, key: str, default: int) -> int:
    try:
        return int((query.get(key) or [default])[0])
    except (TypeError, ValueError):
        return default


def _float(query: dict, key: str, default: float) -> float:
    try:
        return float((query.get(key) or [default])[0])
    except (TypeError, ValueError):
        return default


def serve(config: Config, db: Database, host: str = "127.0.0.1", port: int = 8787) -> None:
    handler = partial(Handler, config=config, db=db)
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"看板已启动: http://{host}:{port}  (Ctrl-C 退出)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n看板已停止。")
    finally:
        httpd.server_close()
