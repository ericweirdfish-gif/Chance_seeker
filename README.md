# Chance Seeker

链上资金异动 × X 注意力异动的双维度监测工具。目标是在**资金已经动了、但注意力还没扩散**（或者反过来）的窗口期把机会捞出来，而不是等热搜出来了再追。

核心判断：**单看资金面噪音太大，单看注意力面全是营销号，两者在同一时间窗内共振才值得看一眼。**

```
┌───────────────┐   ┌──────────────┐   ┌──────────────┐   ┌────────────┐
│  资金面采集    │   │              │   │  稳健异常检测 │   │  Telegram  │
│  DexScreener  │──▶│              │──▶│  中位数/MAD  │──▶│  Discord   │
│  GeckoTerminal│   │   SQLite     │   │  ────────────│   │  终端       │
│  DefiLlama    │   │   时间序列    │   │  共振融合打分 │   │  网页看板   │
│  聪明钱钱包    │   │              │   │  风险一票否决 │   └────────────┘
├───────────────┤   │              │   └──────────────┘
│  注意力面采集  │──▶│              │
│  X / 免费代理  │   └──────────────┘
└───────────────┘
```

## 30 秒看效果（不需要 API key，不需要网络）

```bash
pip install -e .
cp config/config.example.yaml config/config.yaml
python -m chance_seeker demo        # 灌合成数据，跑完整链路
python -m chance_seeker serve       # 打开 http://127.0.0.1:8787
```

`demo` 会造三个典型形态，正好演示这套评分想解决的问题：

| 标的 | 形态 | 结果 |
|------|------|------|
| DEMOA | 资金放量 + 讨论扩散 + KOL 跟进 | **100 分，告警** |
| DEMOB | 只有资金放量，没人讨论 | 56 分，不告警（分数不到阈值） |
| DEMOC | 又热闹又放量，但流动性被抽走 76% | 78 分，**风险一票否决，不告警** |

DEMOC 是重点：它的分数比 DEMOB 高得多，光靠扣分挡不住——所以严重风险信号是直接否决，而不是减分。

## 正式跑起来

```bash
python -m chance_seeker init          # 生成 config/config.yaml 和 .env
vim .env                              # 填 key（全部可选，见下）
python -m chance_seeker probe         # 逐个数据源探活，告诉你哪些能用
python -m chance_seeker run           # 常驻运行
python -m chance_seeker serve         # 另开一个终端跑看板
```

不填任何 key 也能跑：DexScreener / GeckoTerminal / DefiLlama / CoinGecko / Reddit 全部免费无需注册，资金面是完整的，注意力面会退化到免费代理信号。

## 成本

| 数据源 | 提供什么 | 费用 |
|--------|----------|------|
| DexScreener | 价格、流动性、成交量、买卖笔数、推广投放 | 免费，无需 key |
| GeckoTerminal | 新池 / 趋势池发现、独立买家数 | 免费，无需 key |
| DefiLlama | 链 TVL、稳定币流向、赛道 TVL 轮动 | 免费，无需 key |
| CoinGecko 热搜 / Reddit | 免费注意力代理 | 免费，无需 key |
| Etherscan V2 | EVM 聪明钱转账（一个 key 覆盖全部 EVM 链） | 免费档 10 万次/天 |
| Solana RPC | Solana 聪明钱持仓 | 公共节点免费；建议 Helius 免费档 |
| **twitterapi.io** | **X 提及量、独立作者、KOL、互动量** | **约 $0.15 / 1000 条推文** |

X 是唯一花钱的地方。按默认配置（每 15 分钟最多 25 个查询、每个查询 60 条），预算闸门会把开销压在**每月几美元**量级。三层上限（单轮 / 每小时 / 每天）写在 SQLite 里，重启和 GitHub Actions 冷启动之间也能正确累计。

官方 X API Basic 是 $200/月换 1 万次读取，对个人监控性价比很低，但适配器也写好了（`X_PROVIDER=official_v2`）。

## 部署

**本地常驻**（推荐起步，实时性最好）：

```bash
python -m chance_seeker run
```

**GitHub Actions 定时**（完全免费）：`.github/workflows/monitor.yml` 已经配好，把 key 填进仓库 Secrets 就行。注意两个限制：cron 最快 5 分钟一次且高峰会延迟；每次都是全新容器，所以用 `actions/cache` 把 SQLite 带到下一次运行——否则每次冷启动都算不出基线，也就检测不出异常。

**VPS**：`python -m chance_seeker run` 配个 systemd 就行，SQLite 单文件无需额外运维。

## 命令

| 命令 | 用途 |
|------|------|
| `init` | 生成配置文件和数据库 |
| `probe` | 逐个数据源探活，缺 key 会明确告诉你 |
| `run` | 常驻运行 |
| `once` | 强制跑一轮就退出（cron / Actions 用） |
| `detect` | 用已入库的数据重跑检测，**不联网**，调参专用 |
| `top` | 查看历史机会榜 |
| `serve` | 本地网页看板 |
| `test-alert` | 向所有告警渠道发测试消息 |
| `demo` | 灌合成数据，离线体验完整链路 |
| `stats` | 数据库统计 |

## 配置要点

全部在 `config/config.yaml`，`${VAR}` 会从 `.env` 读。几个真正需要你动的地方：

- `collectors.x_attention.kols` — **填你自己信任的 KOL 名单**，这是注意力面质量的关键。空着的话 `x_kol_pickup` 这条规则永远不触发。
- `collectors.evm_wallets.wallets` / `solana_wallets.wallets` — 填你追踪的聪明钱地址。空着的话聪明钱信号不参与。
- `detect.rules` — 所有检测规则都是声明式的，改阈值不用改代码。
- `score.alert_threshold` — 嫌吵就调高，嫌漏就调低。先用 `detect` 命令在历史数据上试，不用重新采集。

告警渠道：Telegram（`@BotFather` 建 bot，给它发句话，访问 `https://api.telegram.org/bot<TOKEN>/getUpdates` 拿 chat_id）、Discord（一个 webhook URL）、终端、网页看板，可以同时开。

## 设计文档

信号体系、评分公式、为什么用中位数而不是均值、每条规则想抓什么形态——见 [docs/DESIGN.md](docs/DESIGN.md)。

## 开发

```bash
pip install -e ".[dev]"
pytest -q          # 96 个测试，全部离线，不发任何网络请求
ruff check chance_seeker tests
```

## 说明

这是一个**信息工具**，输出的是「这里有异动，值得你去看一眼」，不是投资建议。链上早期标的的欺诈率极高，风险规则只能挡掉最明显的几种形态（抽流动性、对敲、卖压主导），挡不住精心设计的骗局。任何标的都请自己做尽调。
