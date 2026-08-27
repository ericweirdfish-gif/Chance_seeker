"""链支持情况探测。

配一条链之前得先知道三件事：数据源支不支持、标识符叫什么、上面有没有真实交易。
猜错任何一条的后果都是「静默采不到数据」——采集器正常跑、日志正常打，
就是一个指标点都没有，非常难排查。所以这里把它做成一条可执行的命令。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from chance_seeker.collectors.http import HttpClient

log = logging.getLogger(__name__)

GT_NETWORKS = "https://api.geckoterminal.com/api/v2/networks"
DS_SEARCH = "https://api.dexscreener.com/latest/dex/search"


@dataclass(slots=True)
class ChainSupport:
    """一条链在各数据源上的支持情况。"""

    query: str
    geckoterminal_ids: list[str] = field(default_factory=list)
    dexscreener_ids: list[str] = field(default_factory=list)
    sample_pools: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return bool(self.geckoterminal_ids or self.dexscreener_ids)


def list_geckoterminal_networks(http: HttpClient, max_pages: int = 10) -> list[dict[str, str]]:
    """GeckoTerminal 支持的全部网络。分页拿完，用来做名称匹配。"""
    networks: list[dict[str, str]] = []
    for page in range(1, max_pages + 1):
        payload = http.get_json(GT_NETWORKS, params={"page": page})
        if not isinstance(payload, dict):
            break
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            break
        for item in data:
            if not isinstance(item, dict):
                continue
            attrs = item.get("attributes") or {}
            networks.append(
                {
                    "id": str(item.get("id") or ""),
                    "name": str(attrs.get("name") or ""),
                    "coingecko_asset_platform_id": str(attrs.get("coingecko_asset_platform_id") or ""),
                }
            )
        if len(data) < 20:
            break
    return networks


def probe_chain(http: HttpClient, query: str, networks: list[dict[str, str]]) -> ChainSupport:
    """按名称在各数据源里找这条链，并验证上面有没有真实池子。"""
    support = ChainSupport(query=query)
    needle = query.strip().lower()

    for network in networks:
        haystack = f"{network['id']} {network['name']} {network['coingecko_asset_platform_id']}".lower()
        if needle in haystack:
            support.geckoterminal_ids.append(network["id"])

    # 确认这个网络上真的有池子——支持列表里有，不代表有交易活动
    for network_id in list(support.geckoterminal_ids):
        payload = http.get_json(f"https://api.geckoterminal.com/api/v2/networks/{network_id}/new_pools")
        pools = (payload or {}).get("data") if isinstance(payload, dict) else None
        count = len(pools) if isinstance(pools, list) else 0
        support.sample_pools += count
        support.notes.append(f"GeckoTerminal `{network_id}` 新池数：{count}")

    # DexScreener 没有网络列表接口，只能从搜索结果里反查它用的 chainId
    payload = http.get_json(DS_SEARCH, params={"q": query})
    pairs = (payload or {}).get("pairs") if isinstance(payload, dict) else None
    if isinstance(pairs, list):
        seen: dict[str, int] = {}
        for pair in pairs:
            if not isinstance(pair, dict):
                continue
            chain_id = str(pair.get("chainId") or "")
            if needle in chain_id.lower():
                seen[chain_id] = seen.get(chain_id, 0) + 1
        support.dexscreener_ids = sorted(seen)
        for chain_id, count in sorted(seen.items()):
            support.notes.append(f"DexScreener `{chain_id}` 搜索命中 {count} 个交易对")

    if not support.usable:
        support.notes.append("两个数据源都没有匹配的网络——要么还没上线，要么名字不是这个")
    return support


def render_report(supports: list[ChainSupport], networks: list[dict[str, str]]) -> str:
    lines = [f"GeckoTerminal 共支持 {len(networks)} 个网络", ""]
    for support in supports:
        mark = "✅" if support.usable else "❌"
        lines.append(f"{mark} 查询「{support.query}」")
        lines.append(f"   GeckoTerminal 网络 id: {support.geckoterminal_ids or '（无匹配）'}")
        lines.append(f"   DexScreener chainId:   {support.dexscreener_ids or '（无匹配）'}")
        for note in support.notes:
            lines.append(f"   · {note}")
        lines.append("")
    return "\n".join(lines)


def suggest_config(supports: list[ChainSupport]) -> str:
    """把探测结果直接渲染成可以粘进 config.yaml 的片段。"""
    lines = ["chains:"]
    for support in supports:
        if not support.usable:
            lines.append(f"  # 「{support.query}」暂无数据源支持，先不配")
            continue
        name = (support.geckoterminal_ids or support.dexscreener_ids)[0]
        lines.append(f"  {name}:")
        lines.append("    enabled: true")
        if support.dexscreener_ids:
            lines.append(f"    dexscreener_chain: {support.dexscreener_ids[0]}")
        if support.geckoterminal_ids:
            lines.append(f"    geckoterminal_network: {support.geckoterminal_ids[0]}")
    return "\n".join(lines)


def discover(queries: list[str]) -> tuple[list[ChainSupport], list[dict[str, str]]]:
    http = HttpClient(
        "chains", rate_limit=25, period=60.0, headers={"Accept": "application/json;version=20230302"}
    )
    networks = list_geckoterminal_networks(http)
    return [probe_chain(http, q, networks) for q in queries], networks


def _matches(networks: list[dict[str, str]], needle: str) -> list[dict[str, str]]:
    needle = needle.lower()
    return [
        n
        for n in networks
        if needle in f"{n['id']} {n['name']} {n['coingecko_asset_platform_id']}".lower()
    ]


def render_network_catalog(networks: list[dict[str, str]], grep: str | None = None) -> str:
    rows = _matches(networks, grep) if grep else networks
    lines = [f"匹配到 {len(rows)} 个网络：" if grep else f"全部 {len(rows)} 个网络："]
    for network in sorted(rows, key=lambda n: n["id"]):
        lines.append(f"  {network['id']:<24} {network['name']}")
    return "\n".join(lines)

