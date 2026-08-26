from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

from chance_seeker import __version__
from chance_seeker.config import Config, load_config
from chance_seeker.logging_setup import setup_logging
from chance_seeker.storage import Database

log = logging.getLogger("chance_seeker.cli")


def _open(args: argparse.Namespace) -> tuple[Config, Database]:
    config = load_config(args.config)
    setup_logging(args.log_level or config.log_level)
    return config, Database(config.db_path)


# --------------------------------------------------------------------- init
def cmd_init(args: argparse.Namespace) -> int:
    root = Path.cwd()
    config_path = root / "config" / "config.yaml"
    example = root / "config" / "config.example.yaml"
    if config_path.exists():
        print(f"已存在 {config_path}，未覆盖")
    elif example.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(example, config_path)
        print(f"已创建 {config_path}")
    else:
        print("找不到 config/config.example.yaml", file=sys.stderr)
        return 1

    env_path = root / ".env"
    env_example = root / ".env.example"
    if not env_path.exists() and env_example.exists():
        shutil.copy(env_example, env_path)
        print(f"已创建 {env_path}（记得把 API key 填进去）")

    config = load_config(config_path)
    setup_logging(config.log_level)
    with Database(config.db_path) as db:
        print(f"已初始化数据库 {config.db_path}: {db.stats()}")
    print("\n下一步：编辑 .env 填 key，然后跑 `chance-seeker probe` 检查数据源连通性。")
    return 0


# ---------------------------------------------------------------------- run
def cmd_run(args: argparse.Namespace) -> int:
    from chance_seeker.pipeline import Pipeline

    config, db = _open(args)
    with db:
        Pipeline(config, db).run_forever(args.tick)
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    """跑一轮就退出。cron / GitHub Actions 用这个。"""
    from chance_seeker.pipeline import Pipeline

    config, db = _open(args)
    with db:
        report = Pipeline(config, db).tick(force=True)
        print(
            f"采集器 {','.join(report.ran) or '无'} ｜ 实体 {report.entities} ｜ 指标 {report.observations} "
            f"｜ 信号 {report.signals} ｜ 机会 {len(report.opportunities)} ｜ 告警 {len(report.alerted)}"
        )
        for opportunity in report.opportunities[: args.top]:
            from chance_seeker.alerts.renderer import summary_line

            print("  " + summary_line(opportunity))
        if report.errors:
            print("错误：" + json.dumps(report.errors, ensure_ascii=False))
            return 1
    return 0


def cmd_detect(args: argparse.Namespace) -> int:
    """只对已入库的数据重跑检测，不产生任何网络请求。调参时很有用。"""
    from chance_seeker.alerts.renderer import summary_line
    from chance_seeker.detect.anomaly import AnomalyEngine
    from chance_seeker.detect.fusion import score_opportunity

    config, db = _open(args)
    with db:
        engine = AnomalyEngine(config, db)
        results = []
        for entity in db.list_entities(seen_within=args.within * 3600):
            signals = engine.evaluate_entity(entity.key)
            if not signals:
                continue
            metrics = db.latest_metrics(entity.key)
            results.append(score_opportunity(config, db, entity, signals, metrics))
        results.sort(key=lambda o: o.score, reverse=True)
        if not results:
            print("没有触发任何信号。可能是历史数据还不够（min_samples），先多跑几轮采集。")
        for opportunity in results[: args.top]:
            print(summary_line(opportunity))
            for signal in sorted(opportunity.signals, key=lambda s: s.score, reverse=True)[:5]:
                print(f"    · [{signal.family}] {signal.label} {signal.score:.0f}")
    return 0


def cmd_top(args: argparse.Namespace) -> int:
    config, db = _open(args)
    with db:
        rows = db.recent_opportunities(limit=args.top, min_score=args.min_score)
        if not rows:
            print("暂无记录。")
            return 0
        for row in rows:
            flag = "✅" if row.get("alerted") else "  "
            name = row.get("symbol") or row.get("name") or row.get("entity_key")
            print(
                f"{flag} {row['score']:5.1f}  {str(row.get('chain') or ''):<9} {name:<16} "
                f"资金 {row['capital_score']:5.1f} 注意力 {row['attention_score']:5.1f}"
                + ("  ⚡" if row.get("cooccurrence") else "")
            )
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    config, db = _open(args)
    with db:
        print(f"数据库: {config.db_path}")
        for key, value in db.stats().items():
            print(f"  {key:<16} {value}")
    return 0


# -------------------------------------------------------------------- probe
def cmd_probe(args: argparse.Namespace) -> int:
    """逐个数据源探活，告诉你哪些能用、哪些缺 key。"""
    from chance_seeker.pipeline import build_collectors

    config, db = _open(args)
    with db:
        collectors = build_collectors(config, db)
        if not collectors:
            print("没有任何可用的采集器，检查 config.yaml 里的 collectors.*.enabled")
            return 1

        if args.schema:
            return _probe_schema(collectors)

        failed = 0
        for collector in collectors:
            try:
                result = collector.collect()
                status = "✅" if result.observations or result.entities else "⚠️ 无数据"
                print(
                    f"{status} {collector.name:<18} 实体 {len(result.entities):>4} ｜ "
                    f"指标 {len(result.observations):>5}"
                    + (f" ｜ {json.dumps(result.notes, ensure_ascii=False)}" if result.notes else "")
                )
                for line in _sample_lines(result, args.samples):
                    print(f"      {line}")
            except Exception as exc:
                failed += 1
                print(f"❌ {collector.name:<18} {type(exc).__name__}: {exc}")
        return 1 if failed else 0


def _sample_lines(result, limit: int) -> list[str]:
    """打印几个真实采到的指标值，肉眼就能看出解析是不是错位了。"""
    if limit <= 0:
        return []
    by_entity: dict[str, dict[str, float]] = {}
    for obs in result.observations:
        by_entity.setdefault(obs.entity_key, {})[obs.metric] = obs.value

    lines = []
    for entity_key, metrics in list(by_entity.items())[:limit]:
        preview = ", ".join(f"{k}={v:,.4g}" for k, v in list(metrics.items())[:6])
        lines.append(f"{entity_key} → {preview}")
    return lines


def _probe_schema(collectors) -> int:
    """打印每个数据源的真实响应结构，并核对解析器依赖的字段是否存在。"""
    from chance_seeker.diagnostics import missing_fields, render

    problems: list[str] = []
    for collector in collectors:
        for probe in collector.schema_probes():
            payload = collector.http.get_json(probe.url, params=probe.params)
            if payload is None:
                problems.append(f"{probe.title}: 请求失败或返回非 JSON")
                print(f"\n===== {probe.title} =====\n❌ 请求失败（URL: {probe.url}）")
                continue

            print(render(probe.title, payload, max_depth=probe.max_depth))
            gaps = missing_fields(payload, probe.expected)
            if gaps:
                print("❌ 解析器依赖但响应里缺失的字段：")
                for gap in gaps:
                    print(f"   - {gap}")
                problems.extend(f"{probe.title}: 缺 {gap}" for gap in gaps)
            else:
                print(f"✅ 解析器依赖的 {len(probe.expected)} 个字段全部存在")

    print("\n" + "=" * 60)
    if problems:
        print(f"发现 {len(problems)} 个结构问题：")
        for problem in problems:
            print(f"  ❌ {problem}")
        return 1
    print("✅ 所有数据源的响应结构与解析器一致")
    return 0


def cmd_test_alert(args: argparse.Namespace) -> int:
    from chance_seeker.alerts import build_channels

    config, db = _open(args)
    with db:
        channels = build_channels(config)
        if not channels:
            print("没有启用任何告警渠道。")
            return 1
        text = "🔔 Chance Seeker 测试消息——看到这条说明推送通道已经打通。"
        failed = 0
        for channel in channels:
            try:
                channel.send_text(text)
                print(f"✅ {channel.name}")
            except Exception as exc:
                failed += 1
                print(f"❌ {channel.name}: {exc}")
        return 1 if failed else 0


def cmd_serve(args: argparse.Namespace) -> int:
    from chance_seeker.web.server import serve

    config, db = _open(args)
    with db:
        host = args.host or str(config.web.get("host", "127.0.0.1"))
        port = args.port or int(config.web.get("port", 8787))
        serve(config, db, host, port)
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """灌入一批合成数据，不联网也能把整条链路跑通看效果。"""
    from chance_seeker.demo import seed_demo_data

    config, db = _open(args)
    with db:
        seed_demo_data(config, db)
    return 0


# --------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chance-seeker",
        description="加密货币链上资金异动 + X 注意力异动监测",
    )
    parser.add_argument("--version", action="version", version=f"chance-seeker {__version__}")
    parser.add_argument("-c", "--config", help="配置文件路径，默认 config/config.yaml")
    parser.add_argument("--log-level", help="DEBUG / INFO / WARNING / ERROR")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="生成配置文件并初始化数据库").set_defaults(func=cmd_init)

    p_run = sub.add_parser("run", help="常驻运行")
    p_run.add_argument("--tick", type=int, help="主循环间隔秒数")
    p_run.set_defaults(func=cmd_run)

    p_once = sub.add_parser("once", help="强制跑一轮后退出（cron / GitHub Actions 用）")
    p_once.add_argument("--top", type=int, default=10)
    p_once.set_defaults(func=cmd_once)

    p_detect = sub.add_parser("detect", help="用已入库的数据重跑检测（不联网，调参用）")
    p_detect.add_argument("--top", type=int, default=20)
    p_detect.add_argument("--within", type=int, default=24, help="只看最近 N 小时活跃的实体")
    p_detect.set_defaults(func=cmd_detect)

    p_top = sub.add_parser("top", help="查看历史机会榜")
    p_top.add_argument("--top", type=int, default=20)
    p_top.add_argument("--min-score", type=float, default=0.0)
    p_top.set_defaults(func=cmd_top)

    sub.add_parser("stats", help="数据库统计").set_defaults(func=cmd_stats)
    p_probe = sub.add_parser("probe", help="探活所有数据源")
    p_probe.add_argument("--schema", action="store_true", help="打印真实响应结构并核对解析器依赖的字段")
    p_probe.add_argument("--samples", type=int, default=2, help="每个数据源打印几个采样值")
    p_probe.set_defaults(func=cmd_probe)
    sub.add_parser("test-alert", help="向所有告警渠道发一条测试消息").set_defaults(func=cmd_test_alert)
    sub.add_parser("demo", help="灌入合成数据，离线体验完整链路").set_defaults(func=cmd_demo)

    p_serve = sub.add_parser("serve", help="启动本地网页看板")
    p_serve.add_argument("--host")
    p_serve.add_argument("--port", type=int)
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except FileNotFoundError as exc:
        setup_logging("INFO")
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
