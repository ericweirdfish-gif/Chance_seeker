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
| CoinGecko 热搜 | 免费注意力代理 | 免费，无需 key |
| Reddit | 免费注意力代理（默认关闭） | 免费，但会拦数据中心 IP，Actions/VPS 上稳定 403 |
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

**GitHub Actions 定时**：`.github/workflows/monitor.yml` 已经配好，把 key 填进仓库 Secrets 即可。三个必须知道的点：

1. **schedule 只在默认分支触发**——工作流不合并到 `main` 就永远不会自动跑。
2. **额度**。本仓库是公开的，Actions 分钟不限，所以默认 `*/10`（每天 144 次）。
   如果改回私有，免费额度只有 2000 分钟/月，按 job 向上取整计费（实测 job 只跑 12 秒也算 1 分钟），
   `*/10` 每月约 4400 分钟会直接超额，必须改回 `*/30`。
3. **状态靠 `actions/cache` 带走 SQLite**，否则每次冷启动都算不出基线。工作流里会在缓存前跑 `prune --vacuum` 压实数据库——缓存条目占仓库 10GB 配额，任由它长大会把 pip 缓存挤掉。

3. **推送凭证**。不配的话工具照常跑、照常算分，但你什么都收不到——信号只留在数据库和运行摘要里。工作流会在日志里明确告诉你渠道状态。

**用 Actions 验证数据源**：`.github/workflows/smoke.yml` 会在有完整外网的 runner 上打真实接口，把响应结构打印出来并核对解析器依赖的字段。本地网络受限（公司网、墙）时，这是最省事的排查方式——推一下分支就能在 Actions 日志里看到每个数据源到底返回了什么。

**VPS**：`python -m chance_seeker run` 配个 systemd 就行，SQLite 单文件无需额外运维。

## 监控哪些链

默认盯 **Solana / BSC / Robinhood Chain / Base** 四条，主网 ethereum 默认关闭（gas 太贵、meme 稀少，开着只会稀释观察列表名额）。

链标识全部是实测确认的，不是照文档抄的：

```bash
chance-seeker chains bsc solana robinhood   # 反查标识符 + 确认有真实交易 + 生成配置片段
chance-seeker chains --list arbitrum        # 列出数据源支持的网络
```

**配新链之前一定跑一次这个命令。** 链标识填错的后果是「静默采不到数据」——采集器正常跑、日志正常打，就是一个指标点都没有，非常难排查。

### 阈值按链分档

`filters.per_chain` 可以覆盖任意全局阈值。这是必需的：Robinhood Chain 全网池子还是两位数，套用 Solana 的 2 万美元流动性门槛等于直接把这条链关掉；反过来为了迁就它把全局门槛降到 3000，成熟链的噪音就全进来了。

| 链 | 最低流动性 | 最低 24h 量 | 理由 |
|---|---|---|---|
| robinhood | $3,000 | $5,000 | 全新链，池子规模整体偏小 |
| solana | $15,000 | $40,000 | pump.fun 系新币起点低 |
| bsc | $25,000 | $60,000 | 池子偏大，但老盘噪音也多 |
| 其它 | $20,000 | $50,000 | 全局默认 |

跑几天后按实际命中率调，用 `chance-seeker detect` 在历史数据上试，不用重新采集。

### 新 meme 币的信号

针对 meme 加了三条规则，核心是**独立买家数**：成交量一个机器人就能对敲出来，但要凑出几十个独立地址成本高得多。

- `buyer_spread` — 买入地址数快速扩散
- `fresh_buyer_traction` — 1h 内大量独立地址买入
- `few_buyers_high_volume`（风险）— 人均成交额畸高，说明是少数地址在对敲

最后一条是 meme 专用的照妖镜：`volume_per_buyer_1h = 1h成交量 / 独立买家数`，人均几万美元的"热门币"基本都是自导自演。

`filters.max_age_days: 14` 限定只看新币。

## 命令

| 命令 | 用途 |
|------|------|
| `init` | 生成配置文件和数据库 |
| `probe` | 逐个数据源探活，缺 key 会明确告诉你 |
| `probe --schema` | 打印线上响应结构并核对解析器依赖的字段是否存在 |
| `chains <名字>` | 探测链在各数据源上的支持情况，生成配置片段 |
| `prune --vacuum` | 清理过期指标点并回收磁盘空间 |
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
