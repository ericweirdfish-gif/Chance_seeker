from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

from chance_seeker.models import FAMILY_ATTENTION, FAMILY_CAPITAL, FAMILY_RISK, Opportunity

EXPLORERS = {
    "ethereum": "https://etherscan.io/token/{address}",
    "base": "https://basescan.org/token/{address}",
    "bsc": "https://bscscan.com/token/{address}",
    "arbitrum": "https://arbiscan.io/token/{address}",
    "solana": "https://solscan.io/token/{address}",
}

FAMILY_ICON = {FAMILY_CAPITAL: "💰", FAMILY_ATTENTION: "📣", FAMILY_RISK: "⚠️"}


def links_for(opportunity: Opportunity) -> dict[str, str]:
    entity = opportunity.entity
    links: dict[str, str] = {}
    if not entity.address:
        return links

    address = entity.address
    chain = entity.chain or ""
    dex_url = (entity.meta or {}).get("dexscreener_url")
    links["DexScreener"] = dex_url or f"https://dexscreener.com/{chain}/{address}"
    links["X 搜索"] = f"https://x.com/search?q={quote(address)}&f=live"
    if chain == "solana":
        links["GMGN"] = f"https://gmgn.ai/sol/token/{address}"
    else:
        links["GMGN"] = f"https://gmgn.ai/{chain}/token/{address}"
    explorer = EXPLORERS.get(chain)
    if explorer:
        links["区块浏览器"] = explorer.format(address=address)
    return links


def _fmt_usd(value: float | None) -> str:
    if value is None:
        return "—"
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if abs(value) >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:,.2f}"


def _fmt_num(value: float | None) -> str:
    if value is None:
        return "—"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if abs(value) >= 10:
        return f"{value:.0f}"
    return f"{value:.2f}"


def _fmt_measure(signal) -> str:
    """按检测方法给出正确的量纲，别把百分比说成倍数。"""
    observed = signal.detail.get("observed")
    if not isinstance(observed, (int, float)):
        return ""
    method = signal.detail.get("method")
    if method == "robust_z":
        return f"z={observed:.1f}"
    if method == "ratio":
        return f"{observed:.1f}× 基线"
    if method == "delta_pct":
        return f"{observed:+.1f}%"
    if method == "acceleration":
        return f"加速 {observed:.1f}×"
    return ""


def short_name(entity) -> str:
    """代号 → 名称 → 缩写地址。

    从 boosts / profiles 发现的代币在第一次详细轮询之前是没有代号的，
    直接打整条实体 key 会把一行日志撑满且没法读。
    """
    if entity.symbol:
        return entity.symbol
    if entity.name:
        return entity.name
    if entity.address and len(entity.address) > 14:
        return f"{entity.address[:6]}…{entity.address[-4:]}"
    return entity.address or entity.key


def summary_line(opportunity: Opportunity) -> str:
    entity = opportunity.entity
    name = short_name(entity)
    chain = f"[{entity.chain}]" if entity.chain else ""
    return (
        f"{_score_icon(opportunity.score)} {opportunity.score:.0f} 分 {chain} {name} "
        f"(资金 {opportunity.capital_score:.0f} / 注意力 {opportunity.attention_score:.0f})"
    )


def _score_icon(score: float) -> str:
    if score >= 85:
        return "🔥"
    if score >= 70:
        return "🚀"
    return "👀"


def render_markdown(opportunity: Opportunity) -> str:
    """给 Telegram / Discord / 终端共用的正文。"""
    entity = opportunity.entity
    metrics = opportunity.metrics
    name = short_name(entity)
    header = f"{_score_icon(opportunity.score)} **{name}** — 机会分 **{opportunity.score:.0f}**"
    if entity.chain:
        header += f"  `{entity.chain}`"

    lines = [header, ""]
    lines.append(
        f"资金面 `{opportunity.capital_score:.0f}` ｜ 注意力 `{opportunity.attention_score:.0f}`"
        + (f" ｜ 风险 `-{opportunity.risk_penalty:.0f}`" if opportunity.risk_penalty else "")
        + ("  ⚡共振" if opportunity.cooccurrence else "")
    )
    lines.append("")

    lines.append("**触发的信号**")
    for signal in sorted(opportunity.signals, key=lambda s: s.score, reverse=True)[:8]:
        icon = FAMILY_ICON.get(signal.family, "•")
        detail = f"（当前 {_fmt_num(signal.value)}，基线 {_fmt_num(signal.baseline)}"
        measure = _fmt_measure(signal)
        detail += f"，{measure}）" if measure else "）"
        lines.append(f"{icon} {signal.label} `{signal.score:.0f}` {detail}")

    key_metrics = [
        ("流动性", _fmt_usd(metrics.get("liquidity_usd") or metrics.get("gt_reserve_usd"))),
        ("市值", _fmt_usd(metrics.get("market_cap_usd") or metrics.get("gt_fdv_usd"))),
        ("24h 量", _fmt_usd(metrics.get("volume_24h") or metrics.get("gt_volume_24h"))),
        ("1h 量", _fmt_usd(metrics.get("volume_1h"))),
        ("1h 涨跌", f"{metrics['price_change_1h']:+.1f}%" if "price_change_1h" in metrics else "—"),
        ("买/卖", _fmt_num(metrics.get("buy_sell_ratio_1h"))),
        ("X 提及", _fmt_num(metrics.get("x_mentions"))),
        ("独立作者", _fmt_num(metrics.get("x_unique_authors"))),
        ("KOL", _fmt_num(metrics.get("x_kol_mentions"))),
        ("聪明钱买入", _fmt_num(metrics.get("smart_money_buyers"))),
    ]
    shown = [f"{label} {value}" for label, value in key_metrics if value != "—"]
    if shown:
        lines.append("")
        lines.append("**关键指标**")
        lines.append(" ｜ ".join(shown))

    if opportunity.notes:
        lines.append("")
        lines.append("**评分说明**")
        lines.extend(f"· {note}" for note in opportunity.notes)

    links = links_for(opportunity)
    if links:
        lines.append("")
        lines.append(" ｜ ".join(f"[{label}]({url})" for label, url in links.items()))

    if entity.address:
        lines.append("")
        lines.append(f"`{entity.address}`")

    lines.append("")
    lines.append(f"_{datetime.fromtimestamp(opportunity.ts).strftime('%Y-%m-%d %H:%M:%S')}_")
    return "\n".join(lines)


def render_plain(opportunity: Opportunity) -> str:
    """终端用：去掉 markdown 修饰符。"""
    text = render_markdown(opportunity)
    for token in ("**", "`", "_"):
        text = text.replace(token, "")
    return text
