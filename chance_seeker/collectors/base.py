from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from chance_seeker.config import Config
from chance_seeker.models import Entity, Observation
from chance_seeker.storage import Database

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SchemaProbe:
    """一个用来校验线上响应结构的探针。

    ``expected`` 是解析器真正依赖的字段路径 -> 说明；``probe --schema``
    会拿真实响应逐条核对，字段对不上会直接报出来，而不是等到解析出空值。
    """

    title: str
    url: str
    params: dict[str, Any] | None = None
    expected: dict[str, str] = field(default_factory=dict)
    max_depth: int = 4


@dataclass(slots=True)
class CollectResult:
    """一次采集的产出：发现的实体 + 观测到的指标点。"""

    entities: list[Entity] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)

    def extend(self, other: CollectResult) -> None:
        self.entities.extend(other.entities)
        self.observations.extend(other.observations)
        self.notes.update(other.notes)

    def __len__(self) -> int:
        return len(self.observations)


class Collector(ABC):
    """采集器基类。

    子类只需要实现 ``collect()``，返回实体与指标点；调度、限流、异常隔离
    都由 pipeline 统一处理，单个采集器挂掉不会影响其它采集器。
    """

    name: str = "collector"
    default_interval: int = 300

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.settings = config.collector(self.name)
        self._last_run: float = 0.0

    # ---- 调度 ----
    @property
    def interval(self) -> int:
        return int(self.settings.get("interval_seconds", self.default_interval))

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", False))

    def due(self, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        return (now - self._last_run) >= self.interval

    def mark_ran(self) -> None:
        self._last_run = time.monotonic()

    def preflight(self) -> str | None:
        """返回不为 None 表示无法运行的原因（例如缺 API key）。"""
        return None

    def schema_probes(self) -> list[SchemaProbe]:
        """`probe --schema` 用来核对线上响应结构的探针，默认没有。"""
        return []

    @abstractmethod
    def collect(self) -> CollectResult:
        ...

    # ---- 便捷方法 ----
    def obs(self, entity_key: str, metric: str, value: Any, ts: int | None = None) -> Observation | None:
        if value is None:
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if numeric != numeric:  # NaN
            return None
        kwargs: dict[str, Any] = {"entity_key": entity_key, "metric": metric, "value": numeric, "source": self.name}
        if ts is not None:
            kwargs["ts"] = ts
        return Observation(**kwargs)
