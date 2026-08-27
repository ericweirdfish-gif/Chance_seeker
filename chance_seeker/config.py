from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

DEFAULT_CONFIG_PATHS = ("config/config.yaml", "config/config.example.yaml")


def _load_dotenv(root: Path) -> None:
    """加载 .env（若存在）。缺少 python-dotenv 时退化为手写解析。"""
    env_file = root / ".env"
    if not env_file.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_file, override=False)
        return
    except ImportError:
        pass
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _interpolate(node: Any) -> Any:
    """递归把 ${ENV_VAR} 替换成环境变量值；未设置的替换成空。"""
    if isinstance(node, dict):
        return {k: _interpolate(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_interpolate(v) for v in node]
    if isinstance(node, str):
        match = _ENV_PATTERN.fullmatch(node.strip())
        if match:
            return os.environ.get(match.group(1), "") or None
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), node)
    return node


@dataclass(slots=True)
class Rule:
    id: str
    family: str
    metric: str
    method: str
    threshold: float
    label: str = ""
    weight: float = 1.0
    min_value: float = 0.0
    lookback: int = 1
    direction: str = "up"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Rule:
        return cls(
            id=str(raw["id"]),
            family=str(raw.get("family", "capital")),
            metric=str(raw["metric"]),
            method=str(raw.get("method", "robust_z")),
            threshold=float(raw["threshold"]),
            label=str(raw.get("label", raw["id"])),
            weight=float(raw.get("weight", 1.0)),
            min_value=float(raw.get("min_value", 0.0) or 0.0),
            lookback=int(raw.get("lookback", 1) or 1),
            direction=str(raw.get("direction", "up")),
        )


@dataclass(slots=True)
class ChainConfig:
    name: str
    enabled: bool = True
    chain_id: int | None = None
    dexscreener_chain: str | None = None
    geckoterminal_network: str | None = None


@dataclass(slots=True)
class Config:
    root: Path
    raw: dict[str, Any]

    general: dict[str, Any] = field(default_factory=dict)
    chains: dict[str, ChainConfig] = field(default_factory=dict)
    collectors: dict[str, dict[str, Any]] = field(default_factory=dict)
    detect: dict[str, Any] = field(default_factory=dict)
    rules: list[Rule] = field(default_factory=list)
    score: dict[str, Any] = field(default_factory=dict)
    filters: dict[str, Any] = field(default_factory=dict)
    alerts: dict[str, dict[str, Any]] = field(default_factory=dict)
    web: dict[str, Any] = field(default_factory=dict)

    # ---- 便捷访问 ----
    @property
    def db_path(self) -> Path:
        p = Path(str(self.general.get("db_path", "data/chance.db")))
        return p if p.is_absolute() else self.root / p

    @property
    def log_level(self) -> str:
        return str(self.general.get("log_level", "INFO"))

    @property
    def tick_seconds(self) -> int:
        return int(self.general.get("tick_seconds", 60))

    @property
    def retention_points(self) -> int:
        return int(self.general.get("series_retention_points", 1500))

    def enabled_chains(self) -> list[ChainConfig]:
        return [c for c in self.chains.values() if c.enabled]

    def collector(self, name: str) -> dict[str, Any]:
        return self.collectors.get(name, {}) or {}

    def collector_enabled(self, name: str) -> bool:
        return bool(self.collector(name).get("enabled", False))


def load_config(path: str | os.PathLike[str] | None = None, root: Path | None = None) -> Config:
    root = root or Path.cwd()
    _load_dotenv(root)

    candidates: list[Path] = []
    if path:
        candidates.append(Path(path))
    else:
        candidates.extend(root / p for p in DEFAULT_CONFIG_PATHS)

    config_file = next((p for p in candidates if p.exists()), None)
    if config_file is None:
        raise FileNotFoundError(
            "找不到配置文件。请先执行：cp config/config.example.yaml config/config.yaml"
        )

    raw = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    raw = _interpolate(raw)

    chains: dict[str, ChainConfig] = {}
    for name, body in (raw.get("chains") or {}).items():
        body = body or {}
        chains[name] = ChainConfig(
            name=name,
            enabled=bool(body.get("enabled", True)),
            chain_id=body.get("chain_id"),
            dexscreener_chain=body.get("dexscreener_chain", name),
            geckoterminal_network=body.get("geckoterminal_network", name),
        )

    detect = raw.get("detect") or {}
    rules = [Rule.from_dict(r) for r in (detect.get("rules") or [])]

    return Config(
        root=root,
        raw=raw,
        general=raw.get("general") or {},
        chains=chains,
        collectors=raw.get("collectors") or {},
        detect=detect,
        rules=rules,
        score=raw.get("score") or {},
        filters=raw.get("filters") or {},
        alerts=raw.get("alerts") or {},
        web=raw.get("web") or {},
    )
