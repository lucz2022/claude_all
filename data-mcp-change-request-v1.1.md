# data MCP 修改说明 v1.1

> **范围**：`data` MCP 服务（`https://data.noip.bid/mcp`）及其上游派生脚本
> **基线**：v1.0 已交付 `gauge_components` / `regime_inputs` / `staleness_hours`，本文档不重复
> **本次条目**：5 项（2 项未完成的 P0 + 3 项新发现）

---

## 变更清单

| ID | 标题 | 类型 | 预估 | 顺序 |
|---|---|---|---|---|
| **C-1** | `regime_type` 字段改名 RATES→REFLATION | 改名 | 10 min | 1 |
| **C-2** | `fx_board` 摘要补 `z_short` / `Δ` / `purity` | 输出层 | 0.5 h | 2 |
| **C-3** | DXY 滚动窗口标注或改 append-only | 数据层 | 0.5 h | 3 |
| **C-4** | 新增 `get_series` 端点 | 新功能 | 0.5 d | 4 |
| **C-5** | `gauge_underlying_exposure` 底层暴露分解 | 输出层 | 1 h | 5 |

C-1 / C-2 / C-3 合计约一小时，建议一次交付。C-4 单独排期。

---

## C-1 `regime_type` 字段改名

### 背景

v1.0 的 `regime_inputs` 已诚实披露：

```
rates_proxy:       "cu_au(铜金比) 5d log-ratio 斜率状态"
rates_real_source: "unavailable — 未接入任何真实利率曲线/2Y 利差…不冒充"
caveats:           ["cu_au 分母含黄金，与 gauge 的 gold 分量部分共线——
                     作利率代理存在循环论证风险"]
```

披露是对的，但**字段名仍叫 `RATES_*`**，会持续误导任何读者。

实际案例：2026-09-15 的快照标 `RISK_OFF_RATES_DN`，当时美联储正要加息 25bp、10Y 破 5%、零售销售 1.2%（预期 0.8%）。所有真实利率信号都指向 UP，而标签显示 DN——因为它读的是 cu_au slope −0.0083，跟货币政策毫无关系。

铜金比是**再通胀 / 增长**代理。在"加息同时长端失控"这类环境下，再通胀方向与利率方向会系统性背离，用 RATES 命名必然产生误读。

### 变更

| 旧 | 新 |
|---|---|
| `RISK_*_RATES_UP` | `RISK_*_REFLATION_UP` |
| `RISK_*_RATES_FLAT` | `RISK_*_REFLATION_FLAT` |
| `RISK_*_RATES_DN` | `RISK_*_REFLATION_DN` |
| `regime_inputs.rates_proxy` | `regime_inputs.reflation_proxy` |
| `regime_inputs.rates_value` | `regime_inputs.reflation_value` |
| `regime_inputs.rates_state` | `regime_inputs.reflation_state` |
| `regime_inputs.rates_direction_convention` | `regime_inputs.reflation_direction_convention` |
| `regime_inputs.rates_threshold` | `regime_inputs.reflation_threshold` |
| `regime_inputs.rates_data_source` | `regime_inputs.reflation_data_source` |
| `regime_inputs.rates_real_source` | `regime_inputs.reflation_real_source` |
| `regime_inputs.rates_coverage_note` | `regime_inputs.reflation_coverage_note` |

### 实现注意

- **纯改名，零逻辑改动。** 阈值 `SLOPE_EPS=0.002`、方向约定、caveats 内容全部保留。
- `reflation_real_source` 的措辞建议同步调整为：`"本维度不读取任何利率数据；如需真实利率维度需另接 FRED 2Y/10Y"`。
- 下游若有硬编码 `"RATES_"` 字符串匹配，一并搜索替换。
- 加一个 `schema_version: "1.1"` 顶层字段，便于消费方识别。

---

## C-2 `fx_board` / `fx_board_compare` 摘要补三列

### 背景

`z_short` 和 `membership` 已存在于 `board__w{20,50}.json`，但**摘要输出层没有透出**。

实际损失（连续两天发生）：

- 2026-09-15 摘要显示 `USD  z +0.017  FLAT_ACCEL`。实际 `z_short = +1.211`，是全板最强短周期动量。仅凭摘要得出的"美元上行阻力尚未确立"是错的。
- 同日 `AUD  z +0.347  UP_DECEL`，实际 `z_short = −0.524`（动能已掉头）、`purity = 0.605`（另有 0.266 的 FLAT_DECEL 隶属度，标签本身不确定）。仅凭摘要得出的"澳元抵抗力最强"方向相反。
- 2026-09-16 摘要显示 `USD  z +0.606  UP_ACCEL`。实际 `z_short = +1.515`，全板最高。

### 变更：`fx_board` 输出格式

```
window=20  asof=2026-09-15T22:00:00Z  effective_session=2026-09-16
staleness_hours=7.18  status=ok
dispersion=0.01656  board_vol=0.02330  residual_rms=0.000146

ccy   z(20)    z_short(5)   Δ        state        purity
USD   +0.606   +1.515      +0.909   UP_ACCEL     0.909
JPY   +1.120   -0.296      -1.416   UP_DECEL     1.000
AUD   +0.390   -0.312      -0.702   UP_DECEL     0.548 (!)
CAD   +0.044   +0.128      +0.084   FLAT_FLAT    0.835
GBP   -0.128   +0.214      +0.342   FLAT_FLAT    0.490 (!)
EUR   -0.185   +0.008      +0.193   FLAT_FLAT    0.509 (!)
CHF   -0.862   -0.456      +0.406   DOWN_FLAT    0.594 (!)
NZD   -0.985   -0.801      +0.184   DOWN_FLAT    0.815

动能转折排行 (|Δ| desc): JPY -1.416 | USD +0.909 | AUD -0.702 | CHF +0.406 | GBP +0.342
```

### 字段定义

| 字段 | 计算 | 说明 |
|---|---|---|
| `z_short` | 已有字段直接透出 | 5 日窗口 z |
| `delta` | `z_short - z` | **本次最有价值的一列**。对应「ADX 变化率比水平更重要」在货币强度上的形态 |
| `purity` | `max(membership.values())` | 模糊状态的最大隶属度 |
| `(!)` 标记 | `purity < 0.60` | 表示 state 标签本身不确定，读取时需降权 |
| 动能转折排行 | 按 `abs(delta)` 降序取前 5 | 20 日排名完全看不出的信息 |

### JSON 侧

同步在每个 currency 对象内补两个显式字段，避免消费方自己算：

```json
{
  "ccy": "USD",
  "strength": 0.014124250807814842,
  "z": 0.6062861467461123,
  "z_short": 1.5151091984630292,
  "delta": 0.9088230517169169,
  "rank": 2,
  "state": "UP_ACCEL",
  "purity": 0.9088230517169169,
  "state_ambiguous": false,
  "membership": { }
}
```

> `purity` 与 `delta` 数值巧合相同纯属本例，两者定义无关，勿合并。

### `fx_board_compare` 额外要求

除现有 rank/state 对比外，加一列 `Δ` 的**跨窗口符号一致性**：

```
ccy   Δ(w20)    Δ(w50)    符号
USD   +0.909    +0.412    一致
JPY   -1.416    -0.203    一致
AUD   -0.702    +0.118    冲突 (!)
```

符号冲突表示快慢两个窗口的动能方向在打架，该货币的方向判断应降权或转人工。

---

## C-3 DXY 滚动窗口

### 背景（本次新发现）

连续两天读取 `dxy_series` 的结果：

```
2026-09-16 读  →  402 rows (2025-03-03 .. 2026-09-15)   min=95.7501  max=106.5499  mean=99.3034
2026-09-17 读  →  402 rows (2025-03-04 .. 2026-09-16)   min=95.7501  max=105.5799  mean=99.2879
```

**行数恒定 402，起点前移一天** —— 这是固定长度滚动窗口，加一根丢一根。

后果：`max` 从 106.5499 变成 105.5799，**不是因为价格变化，而是因为原高点滚出了窗口**。`min` / `max` / `mean` 都不是稳定基准。

风险等级：**高**。若引擎任何位置用 DXY 的 min/max 做百分位或归一化，会产生静默漂移——不抛异常、回测也不暴露（回测用同样滚动的数据，两边一致所以"看起来正常"），只有实盘长期跑偏。

### 方案 A（推荐）：改 append-only

- `dxy__D1.parquet` 存全历史，只追加不删除
- `dxy_series(n)` 在**读取时**截断，不在存储时截断
- `min` / `max` / `mean` 基于全历史计算，作为稳定基准
- 输出加 `stats_scope: "full_history"`

### 方案 B：保持滚动 + 显式标注

```json
"window_mode": "rolling_fixed",
"window_rows": 402,
"stats_scope": "window_only",
"stats": {"min": 95.7501, "max": 105.5799, "mean": 99.2879},
"warning": "min/max/mean 为窗口内统计，随滚动变化，不可作为稳定归一化基准"
```

### 连带排查（必做）

检查**其他所有派生序列**是否同样是滚动窗口，重点：

- `board__w20.json` / `board__w50.json` 背后的强度累积指数（cumulative index）
- 若累积指数的起点也在漂移，**问题比 DXY 严重得多** —— 起点漂移会直接改变全部 z 的基准，导致今天的 z 与昨天的 z 不可比

排查结果请在交付说明中明确回答：累积指数的起点是固定的还是滚动的。

---

## C-4 新增 `get_series` 端点

### 背景

位置层（三层架构的第三层）目前完全空白。

而 2026-09-16 的快照显示，**环境层与方向层已首次对齐**：

```
环境：RISK_NEUTRAL_REFLATION_FLAT，coefficient.trend 0.6 → 0.8（不再压制趋势）
方向：USD UP_ACCEL purity 0.909，w20 dispersion 0.01401 → 0.01656（+18%，压缩解除）
位置：空白
```

三层里两层对齐，缺的那层正好是决定入场点的那层。

另有一个具体缺口：`env.json` 的 `sources` 明写 `gold: 'mt5:XAUUSD'`，说明黄金 D1 数据就在磁盘上、四比率就是从它算出来的——但没有任何端点能读它。连续三次分析黄金，只能用第三方的 1 分钟线（100 根 / 24 小时窗口）拼，拿不到任何 D1 结构。

### 接口

```
get_series(symbol: str, tf: str = "D1", n: int = 120, since: str | None = None)
```

### 返回

```json
{
  "symbol": "XAUUSD",
  "tf": "D1",
  "price_kind": "bid",
  "source": "mt5",
  "session_boundary_utc": 22,
  "asof_semantics": "session_start",
  "staleness_hours": 7.18,
  "staleness_status": "ok",
  "window_mode": "rolling_fixed",
  "stats_scope": "window_only",
  "rows": [
    {
      "session_date": "2026-09-16",
      "ts_utc": "2026-09-15T22:00:00Z",
      "open": 4294.99,
      "high": 4361.69,
      "low": 4257.54,
      "close": 4264.88,
      "volume": null,
      "bars_in_session": 24,
      "partial": false,
      "segment_id": 14,
      "roll_flag": false
    }
  ]
}
```

### 硬性约束

> **输出原始 OHLC，不得预先计算 ATR / ADX / 均线 / 任何派生指标。**

理由不是省事，是**独立性**。派生指标由引擎计算，原始序列作为独立通道保留——这样消费方才有能力反过来质疑引擎，而非只能复述它。

本周三个已确认的引擎问题（`RATES_*` 名不副实、DXY 滚动窗口漂移、gauge 底层自我参照），全部是靠拿到未加工数据才发现的。C-4 若把指标算进去，这条通道的价值即归零。

### 优先开放的 symbol

| 优先级 | symbol | 理由 |
|---|---|---|
| 1 | `XAUUSD` | 已分析三次，一次未拿到 D1 |
| 2 | `XTIUSD`, `US500`, `HG连续` | 环境层四比率的全部原料，可查分子分母各自走向 |
| 3 | `EURCHF`, `GBPCHF`, `EURGBP` | 主力交叉盘 |
| 4 | `USDCHF`, `EURUSD`, `AUDUSD`, `USDJPY` | 近期分析重点 |

`tf` 至少支持 `D1` 与 `H1`。接口形状参照已有的 `dxy_series`。

### 边界行为

| 情况 | 行为 |
|---|---|
| symbol 不在白名单 | 返回错误，并列出可用 symbol 列表 |
| `n` 超过可用行数 | 返回全部可用行，并在响应中标注 `truncated: false, available: <n>` |
| `tf` 不支持 | 返回错误，列出支持的 tf |
| 序列含 `partial: true` 的会话 | 正常返回，由消费方决定是否剔除 |

---

## C-5 `gauge_underlying_exposure`

### 背景（本次新发现）

v1.0 交付的 `gauge_concentration = 0.357` 度量的是**贡献集中度**，不是**底层资产集中度**。

从 `gauge_components[].underlyings` 反推：

| 底层 | 出现分量数 | 位置 | 符号 |
|---|---|---|---|
| **XAUUSD** | **3 / 4** | cu_au 分母、gor 分子、au_spx 分子 | 净 −1 |
| HG | 2 / 4 | cu_au 分子、hg_wti 分子 | 净 +1 |
| XTIUSD | 2 / 4 | gor 分母、hg_wti 分母 | 净 −1 |
| US500 | 1 / 4 | au_spx 分母 | 净 −1 |

四个"独立比率"实际只由 4 个标的构成。**贡献集中度 0.357 看起来健康，底层集中度是 0.75。**

直接后果：**用 weather_gauge 分析黄金存在自我参照。**

实例：2026-09-16 黄金下跌 1.9%，`weather_gauge` 从 −50.96 回升到 −10.47。这个回升里有多少是真实环境改善、有多少只是黄金自身波动在四个比率里绕了一圈后的镜像，当前输出无法区分。

### 变更

在 `env.json` 顶层新增：

```json
"gauge_underlying_exposure": {
  "XAUUSD": {"n_components": 3, "components": ["cu_au", "gor", "au_spx"],
             "net_sign": -1, "abs_weight": 0.75},
  "HG":     {"n_components": 2, "components": ["cu_au", "hg_wti"],
             "net_sign": 1,  "abs_weight": 0.50},
  "XTIUSD": {"n_components": 2, "components": ["gor", "hg_wti"],
             "net_sign": -1, "abs_weight": 0.50},
  "US500":  {"n_components": 1, "components": ["au_spx"],
             "net_sign": -1, "abs_weight": 0.25}
},
"gauge_underlying_concentration": 0.75,
"gauge_self_reference_warning": ["XAUUSD"]
```

### 计算规则

| 字段 | 定义 |
|---|---|
| `n_components` | 该标的出现在几个 gauge 分量中 |
| `components` | 具体是哪几个分量 |
| `net_sign` | 分子记 +1、分母记 −1，求和后取符号 |
| `abs_weight` | `n_components / 分量总数`（VIX 不计入，其 contrib 恒为 0） |
| `gauge_underlying_concentration` | `max(abs_weight)` |
| `gauge_self_reference_warning` | `abs_weight >= 0.5` 的标的列表 |

### 消费约定（写进文档）

> 当某标的出现在 `gauge_self_reference_warning` 中时，**用该 gauge 分析该标的本身必须降权或显式标注**。gauge 对该标的不是独立的环境信号，而是其自身波动的镜像。

摘要输出中，若 `gauge_self_reference_warning` 非空，追加一行：

```
⚠ 自我参照: XAUUSD (abs_weight=0.75) — 分析该标的时本 gauge 不独立
```

---

## 附：不需要改的部分

以下 v1.0 已交付内容质量良好，本次**不要动**：

- `gauge_sum_check`（`abs_diff: 0.0` 的恒等式自校验）——保留
- `regime_inputs.caveats` 的全部内容，尤其"循环论证风险"那条——保留，仅随 C-1 改名
- `reflation_real_source` 的 `unavailable … 不冒充` 措辞——这是正确的工程态度，保留
- `gauge_components` 中 VIX 的 `note: "非 weather_gauge 输入，仅作 vix_level 监控披露"`——保留。此披露已澄清一处误判（曾据错误模型推测 gauge 与 VIX "内部不一致"，实际 VIX 根本不是输入）
- `staleness_hours` / `staleness_status`——保留
