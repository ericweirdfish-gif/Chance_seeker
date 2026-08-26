from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from chance_seeker.alerts import build_channels
from chance_seeker.collectors.base import Collector, CollectResult
from chance_seeker.config import Config
from chance_seeker.detect.anomaly import AnomalyEngine
from chance_seeker.detect.fusion import fingerprint, score_opportunity, should_alert
from chance_seeker.models import Opportunity
from chance_seeker.storage import Database

log = logging.getLogger(__name__)


def build_collectors(config: Config, db: Database) -> list[Collector]:
    from chance_seeker.collectors.defillama import DefiLlamaCollector
    from chance_seeker.collectors.dexscreener import DexScreenerCollector
    from chance_seeker.collectors.evm_wallets import EvmWalletCollector
    from chance_seeker.collectors.free_attention import FreeAttentionCollector
    from chance_seeker.collectors.geckoterminal import GeckoTerminalCollector
    from chance_seeker.collectors.solana_wallets import SolanaWalletCollector
    from chance_seeker.collectors.x_attention import XAttentionCollector

    classes = [
        DexScreenerCollector,
        GeckoTerminalCollector,
        DefiLlamaCollector,
        EvmWalletCollector,
        SolanaWalletCollector,
        XAttentionCollector,
        FreeAttentionCollector,
    ]

    collectors: list[Collector] = []
    for cls in classes:
        # 先看配置再构造：未启用的采集器不应该在初始化时抱怨缺 key
        if not config.collector_enabled(cls.name):
            log.debug("采集器 %s 未启用，跳过", cls.name)
            continue
        collector = cls(config, db)
        reason = collector.preflight()
        if reason:
            log.warning("采集器 %s 无法启动：%s（已跳过）", collector.name, reason)
            continue
        collectors.append(collector)
        log.info("采集器 %s 已就绪（间隔 %ds）", collector.name, collector.interval)
    return collectors


@dataclass(slots=True)
class TickReport:
    ran: list[str] = field(default_factory=list)
    observations: int = 0
    entities: int = 0
    signals: int = 0
    opportunities: list[Opportunity] = field(default_factory=list)
    alerted: list[Opportunity] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


class Pipeline:
    """采集 → 存储 → 检测 → 打分 → 告警 的一轮闭环。"""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.collectors = build_collectors(config, db)
        self.engine = AnomalyEngine(config, db)
        self.channels = build_channels(config)
        self._ticks = 0

    # ------------------------------------------------------------------
    def tick(self, force: bool = False) -> TickReport:
        report = TickReport()
        merged = CollectResult()

        for collector in self.collectors:
            if not force and not collector.due():
                continue
            started = time.monotonic()
            try:
                result = collector.collect()
            except Exception as exc:  # 单个采集器出错不能拖垮整轮
                log.exception("采集器 %s 执行失败", collector.name)
                report.errors[collector.name] = str(exc)
                collector.mark_ran()
                continue
            collector.mark_ran()
            merged.extend(result)
            report.ran.append(collector.name)
            log.info(
                "采集器 %s 完成：%d 个实体 / %d 个指标点（%.1fs）",
                collector.name, len(result.entities), len(result.observations), time.monotonic() - started,
            )

        touched = self._persist(merged, report)
        report.notes.update(merged.notes)

        if touched:
            report.opportunities = self._detect(touched, report)
            report.alerted = self._dispatch(report.opportunities)

        self._ticks += 1
        if self._ticks % 20 == 0:
            removed = self.db.prune_metrics(self.config.retention_points)
            if removed:
                log.info("清理了 %d 个过期指标点", removed)
        return report

    # ------------------------------------------------------------------
    def _persist(self, result: CollectResult, report: TickReport) -> list[str]:
        seen: dict[str, None] = {}
        for entity in result.entities:
            if entity.key in seen:
                continue
            self.db.upsert_entity(entity)
            seen[entity.key] = None
        report.entities = len(seen)
        report.observations = self.db.record(result.observations)
        # 只对本轮真的有新指标的实体做检测，省掉大量无意义的计算
        return sorted({obs.entity_key for obs in result.observations})

    def _detect(self, entity_keys: list[str], report: TickReport) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        for key in entity_keys:
            entity = self.db.get_entity(key)
            if entity is None:
                continue
            signals = self.engine.evaluate_entity(key)
            if not signals:
                continue
            report.signals += self.db.save_signals(signals)

            metrics = self.db.latest_metrics(key)
            opportunity = score_opportunity(self.config, self.db, entity, signals, metrics)
            if opportunity.score <= 0:
                continue
            opportunities.append(opportunity)

        opportunities.sort(key=lambda o: o.score, reverse=True)
        return opportunities

    def _dispatch(self, opportunities: list[Opportunity]) -> list[Opportunity]:
        alerted: list[Opportunity] = []
        for opportunity in opportunities:
            ok, reason = should_alert(self.config, self.db, opportunity)
            payload = _payload(opportunity, alerted=ok, skip_reason=reason)
            opportunity_id = self.db.save_opportunity(opportunity.entity.key, payload)

            if not ok:
                log.debug("不告警 %s（%.1f 分）：%s", opportunity.entity.key, opportunity.score, reason)
                continue

            print_key = fingerprint(opportunity)
            delivered = False
            for channel in self.channels:
                try:
                    channel.send(opportunity)
                    self.db.record_alert(
                        opportunity.entity.key, print_key, opportunity.score, channel.name, "sent"
                    )
                    delivered = True
                except Exception as exc:
                    log.error("渠道 %s 发送失败: %s", channel.name, exc)
                    self.db.record_alert(
                        opportunity.entity.key, print_key, opportunity.score, channel.name, "failed", str(exc)
                    )
            if delivered:
                alerted.append(opportunity)
                if opportunity_id:
                    self.db.mark_opportunity_alerted(opportunity_id)
        return alerted

    # ------------------------------------------------------------------
    def run_forever(self, tick_seconds: int | None = None) -> None:
        interval = tick_seconds or self.config.tick_seconds
        log.info("进入常驻模式，每 %ds 检查一次到期的采集器（Ctrl-C 退出）", interval)
        try:
            while True:
                started = time.monotonic()
                report = self.tick()
                if report.ran:
                    log.info(
                        "本轮：采集器 %s ｜ %d 指标点 ｜ %d 信号 ｜ %d 机会 ｜ %d 告警",
                        ",".join(report.ran), report.observations, report.signals,
                        len(report.opportunities), len(report.alerted),
                    )
                elapsed = time.monotonic() - started
                time.sleep(max(1.0, interval - elapsed))
        except KeyboardInterrupt:
            log.info("已停止。")


def _payload(opportunity: Opportunity, alerted: bool, skip_reason: str) -> dict[str, Any]:
    return {
        "score": opportunity.score,
        "capital_score": opportunity.capital_score,
        "attention_score": opportunity.attention_score,
        "risk_penalty": opportunity.risk_penalty,
        "cooccurrence": opportunity.cooccurrence,
        "alerted": alerted,
        "skip_reason": skip_reason,
        "notes": opportunity.notes,
        "ts": opportunity.ts,
        "signals": [
            {
                "rule_id": s.rule_id,
                "family": s.family,
                "metric": s.metric,
                "label": s.label,
                "score": s.score,
                "value": s.value,
                "baseline": s.baseline,
                "detail": s.detail,
            }
            for s in opportunity.signals
        ],
        "metrics": opportunity.metrics,
    }
