const $ = (id) => document.getElementById(id);
const state = { rows: [], timer: null };

const EXPLORERS = {
  ethereum: (a) => `https://etherscan.io/token/${a}`,
  base: (a) => `https://basescan.org/token/${a}`,
  bsc: (a) => `https://bscscan.com/token/${a}`,
  arbitrum: (a) => `https://arbiscan.io/token/${a}`,
  solana: (a) => `https://solscan.io/token/${a}`,
};

const fmtUsd = (v) => {
  if (v === undefined || v === null) return "—";
  const n = Number(v);
  if (Math.abs(n) >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (Math.abs(n) >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toFixed(2)}`;
};

const fmtNum = (v) => {
  if (v === undefined || v === null) return "—";
  const n = Number(v);
  return Math.abs(n) >= 1000 ? n.toLocaleString(undefined, { maximumFractionDigits: 0 }) : n.toFixed(2);
};

const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false });

const scoreClass = (s) => (s >= 85 ? "s-hot" : s >= 70 ? "s-warm" : "s-cool");

// 按检测方法给出正确的量纲：百分比变化不能写成「倍数」
function fmtMeasure(detail) {
  if (!detail || typeof detail.observed !== "number") return "";
  const o = detail.observed;
  switch (detail.method) {
    case "robust_z": return `，z=${o.toFixed(1)}`;
    case "ratio": return `，${o.toFixed(1)}× 基线`;
    case "delta_pct": return `，${o >= 0 ? "+" : ""}${o.toFixed(1)}%`;
    case "acceleration": return `，加速 ${o.toFixed(1)}×`;
    default: return "";
  }
}

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function loadStats() {
  const s = await getJSON("/api/stats");
  const cards = [
    ["活跃代币 24h", s.active_tokens_24h],
    ["信号 24h", s.signals_24h],
    ["告警 24h", s.alerts_24h],
    ["监控实体", s.entities],
    ["指标点", s.metrics],
    ["钱包事件", s.wallet_events],
  ];
  $("stats").innerHTML = cards
    .map(([k, v]) => `<div class="stat"><div class="v">${Number(v).toLocaleString()}</div><div class="k">${k}</div></div>`)
    .join("");
}

async function loadRows() {
  const minScore = Number($("minScore").value || 0);
  const rows = await getJSON(`/api/opportunities?limit=120&min_score=${minScore}`);
  state.rows = $("onlyAlerted").checked ? rows.filter((r) => r.alerted) : rows;
  render();
  $("updated").textContent = `更新于 ${fmtTime(Math.floor(Date.now() / 1000))}`;
}

function render() {
  const tbody = $("rows");
  $("empty").hidden = state.rows.length > 0;
  tbody.innerHTML = state.rows
    .map((r, i) => {
      const name = r.symbol || r.name || r.entity_key;
      const chips = (r.signals || [])
        .slice()
        .sort((a, b) => b.score - a.score)
        .slice(0, 4)
        .map((s) => `<span class="chip ${s.family}">${s.label}</span>`)
        .join("");
      return `<tr data-i="${i}">
        <td class="num"><span class="score ${scoreClass(r.score)}">${r.score.toFixed(0)}</span>
          ${r.alerted ? '<span class="flag">✓</span>' : ""}${r.cooccurrence ? " ⚡" : ""}</td>
        <td><div class="sym">${name}</div><div class="addr">${(r.address || "").slice(0, 16)}</div></td>
        <td>${r.chain || "—"}</td>
        <td class="num">${r.capital_score.toFixed(0)}</td>
        <td class="num">${r.attention_score.toFixed(0)}</td>
        <td class="num">${r.risk_penalty ? "-" + r.risk_penalty.toFixed(0) : "—"}</td>
        <td>${chips}</td>
        <td class="num">${fmtTime(r.ts)}</td>
      </tr>`;
    })
    .join("");

  tbody.querySelectorAll("tr").forEach((tr) => {
    tr.addEventListener("click", () => openDrawer(state.rows[Number(tr.dataset.i)]));
  });
}

function sparkline(points, width = 240, height = 40) {
  if (!points || points.length < 2) return "";
  const values = points.map((p) => p[1]);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const d = points
    .map((p, i) => {
      const x = (i / (points.length - 1)) * width;
      const y = height - ((p[1] - min) / span) * height;
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return `<svg class="spark" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">
    <path d="${d}" fill="none" stroke="currentColor" stroke-width="1.6" opacity="0.85"/>
  </svg>`;
}

async function openDrawer(row) {
  if (!row) return;
  const name = row.symbol || row.name || row.entity_key;
  $("drawerTitle").textContent = `${name} · ${row.score.toFixed(0)} 分`;
  $("drawer").hidden = false;

  const links = [];
  if (row.address) {
    links.push([`DexScreener`, `https://dexscreener.com/${row.chain}/${row.address}`]);
    links.push([`X 搜索`, `https://x.com/search?q=${encodeURIComponent(row.address)}&f=live`]);
    links.push([`GMGN`, `https://gmgn.ai/${row.chain === "solana" ? "sol" : row.chain}/token/${row.address}`]);
    const ex = EXPLORERS[row.chain];
    if (ex) links.push(["区块浏览器", ex(row.address)]);
  }

  const metricRows = Object.entries(row.metrics || {})
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([k, v]) => `<div class="k">${k}</div><div class="v">${k.includes("usd") || k.includes("cap") ? fmtUsd(v) : fmtNum(v)}</div>`)
    .join("");

  const signalRows = (row.signals || [])
    .slice()
    .sort((a, b) => b.score - a.score)
    .map(
      (s) => `<div class="signal">
        <span class="chip ${s.family}">${s.family}</span> <strong>${s.label}</strong> · ${s.score.toFixed(0)} 分
        <div class="meta">${s.metric}：当前 ${fmtNum(s.value)}，基线 ${fmtNum(s.baseline)}${fmtMeasure(s.detail)}</div>
      </div>`
    )
    .join("");

  $("drawerBody").innerHTML = `
    <h3>评分构成</h3>
    <div class="kv">
      <div class="k">资金面</div><div class="v">${row.capital_score.toFixed(1)}</div>
      <div class="k">注意力面</div><div class="v">${row.attention_score.toFixed(1)}</div>
      <div class="k">风险扣分</div><div class="v">${row.risk_penalty ? "-" + row.risk_penalty.toFixed(1) : "0.0"}</div>
      <div class="k">共振</div><div class="v">${row.cooccurrence ? "是 ⚡" : "否"}</div>
      <div class="k">是否告警</div><div class="v">${row.alerted ? "是" : `否（${row.skip_reason || "-"}）`}</div>
    </div>
    ${(row.notes || []).length ? `<h3>说明</h3>${row.notes.map((n) => `<div class="signal">${n}</div>`).join("")}` : ""}
    <h3>触发的信号</h3>${signalRows || "<p class='empty'>无</p>"}
    <h3>走势</h3><div id="sparks">加载中…</div>
    <h3>最新指标</h3><div class="kv">${metricRows}</div>
    ${links.length ? `<h3>快捷链接</h3><div class="links">${links.map(([t, u]) => `<a href="${u}" target="_blank" rel="noopener">${t}</a>`).join("")}</div>` : ""}
  `;

  try {
    const wanted = ["volume_1h", "liquidity_usd", "x_mentions", "x_unique_authors"];
    const data = await getJSON(`/api/series?entity=${encodeURIComponent(row.entity_key)}&metrics=${wanted.join(",")}&limit=120`);
    $("sparks").innerHTML = wanted
      .filter((m) => (data.series[m] || []).length > 1)
      .map((m) => `<div class="k">${m}</div>${sparkline(data.series[m])}`)
      .join("") || "<p class='empty'>历史数据不足</p>";
  } catch (e) {
    $("sparks").textContent = `加载失败: ${e.message}`;
  }
}

async function refresh() {
  try {
    await Promise.all([loadStats(), loadRows()]);
  } catch (e) {
    $("updated").textContent = `加载失败: ${e.message}`;
  }
}

$("refresh").addEventListener("click", refresh);
$("minScore").addEventListener("change", refresh);
$("onlyAlerted").addEventListener("change", () => { render(); });
$("drawerClose").addEventListener("click", () => { $("drawer").hidden = true; });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("drawer").hidden = true; });

refresh();
state.timer = setInterval(refresh, 30000);
