from __future__ import annotations

import json
import time
from pathlib import Path

from chance_seeker import chains


class FakeHttp:
    """按 URL 返回预置响应，并记录请求次数。"""

    def __init__(self, pages: list[dict] | None = None) -> None:
        self.pages = pages or []
        self.calls: list[tuple[str, dict | None]] = []

    def get_json(self, url, params=None, **kwargs):
        self.calls.append((url, params))
        if url == chains.GT_NETWORKS:
            index = int((params or {}).get("page", 1)) - 1
            return self.pages[index] if index < len(self.pages) else None
        return {"data": []}


def page(ids: list[str], has_next: bool) -> dict:
    return {
        "data": [{"id": i, "attributes": {"name": i.title()}} for i in ids],
        "links": {"next": "http://x" if has_next else None},
    }


def test_pagination_stops_on_last_page_without_extra_request(tmp_path):
    """靠「本页不足 20 条」推断会在最后一页刚好满页时多请求一次，触发 400。"""
    full = [f"net{i}" for i in range(20)]
    http = FakeHttp([page(full, has_next=True), page(full, has_next=False)])
    networks = chains.list_geckoterminal_networks(http, cache_root=tmp_path)

    assert len(networks) == 40
    assert [p["page"] for _, p in http.calls] == [1, 2], "不应该请求第 3 页"


def test_network_list_is_cached_across_processes(tmp_path):
    """限流器是进程内的，不落地缓存就必然被 429 打成空列表。"""
    http = FakeHttp([page(["bsc", "solana"], has_next=False)])
    first = chains.list_geckoterminal_networks(http, cache_root=tmp_path)
    assert len(http.calls) == 1

    fresh = FakeHttp([])  # 第二次「进程」拿不到任何网络响应
    second = chains.list_geckoterminal_networks(fresh, cache_root=tmp_path)
    assert second == first
    assert fresh.calls == [], "命中缓存就不该再发请求"


def test_expired_cache_is_refetched(tmp_path):
    stale = {"ts": time.time() - chains.CACHE_TTL - 1, "networks": [{"id": "old", "name": "Old"}]}
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "gt_networks.json").write_text(json.dumps(stale), encoding="utf-8")

    http = FakeHttp([page(["new"], has_next=False)])
    assert [n["id"] for n in chains.list_geckoterminal_networks(http, cache_root=tmp_path)] == ["new"]


def test_empty_result_is_not_cached(tmp_path):
    """被限流拿到空列表时不能写缓存，否则错误状态会被固化 24 小时。"""
    chains.list_geckoterminal_networks(FakeHttp([]), cache_root=tmp_path)
    assert not chains._cache_path(tmp_path).exists()


def test_probe_matches_by_id_and_name():
    networks = [
        {"id": "bsc", "name": "BNB Chain", "coingecko_asset_platform_id": "binance-smart-chain"},
        {"id": "solana", "name": "Solana", "coingecko_asset_platform_id": "solana"},
        {"id": "robinhood", "name": "Robinhood", "coingecko_asset_platform_id": ""},
    ]
    http = FakeHttp()

    # id 直接命中
    assert chains.probe_chain(http, "robinhood", networks).geckoterminal_ids == ["robinhood"]
    # 通过 coingecko 平台名命中（用户输 binance，实际 id 是 bsc）
    assert chains.probe_chain(http, "binance", networks).geckoterminal_ids == ["bsc"]
    # 完全不存在
    unknown = chains.probe_chain(http, "nosuchchain", networks)
    assert not unknown.usable
    assert any("没有匹配" in n for n in unknown.notes)


def test_suggest_config_emits_both_identifiers():
    support = chains.ChainSupport(
        query="robinhood", geckoterminal_ids=["robinhood"], dexscreener_ids=["robinhood"]
    )
    snippet = chains.suggest_config([support])
    assert "dexscreener_chain: robinhood" in snippet
    assert "geckoterminal_network: robinhood" in snippet


def test_suggest_config_skips_unsupported_chains():
    snippet = chains.suggest_config([chains.ChainSupport(query="ghostchain")])
    assert "暂无数据源支持" in snippet
    assert "enabled: true" not in snippet


def test_cache_path_is_under_data(tmp_path: Path):
    assert chains._cache_path(tmp_path) == tmp_path / "data" / "gt_networks.json"
