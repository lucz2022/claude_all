# data MCP 验收说明 v1.1

> **对应**：《data MCP 修改说明 v1.1》C-1 ~ C-5
> **用法**：每项独立可验，失败即打回，不做部分接受
> **原则**：验收标准写的是「观察到什么」，不是「代码里有什么」。不看实现，只看输出。

---

## 验收总表

| ID | 验收项 | 判定方式 | 阻断性 |
|---|---|---|---|
| A-1 | `regime_type` 改名 | 单次调用观察 | 是 |
| A-2 | `fx_board` 摘要三列 | 单次调用观察 | 是 |
| A-3 | `fx_board_compare` 符号一致性列 | 单次调用观察 | 否 |
| A-4 | DXY 窗口模式标注 | 跨日两次调用比对 | 是 |
| A-5 | 累积指数起点排查 | 书面回答 | 是 |
| A-6 | `get_series` 基本返回 | 单次调用观察 | 是 |
| A-7 | `get_series` 无派生指标 | 字段扫描 | **是（一票否决）** |
| A-8 | `get_series` 边界行为 | 4 个用例 | 否 |
| A-9 | `gauge_underlying_exposure` | 单次调用 + 手工核对 | 是 |
| R-1 ~ R-5 | 回归：v1.0 已交付项未被破坏 | 逐项观察 | 是 |

---

## A-1 `regime_type` 改名

**调用**：`fx_env()`

**通过标准**（全部满足）：

1. `regime_type` 值形如 `RISK_<X>_REFLATION_<Y>`，**不含 `RATES` 字样**
2. `regime_inputs` 下的 10 个字段全部以 `reflation_` 开头，无任何 `rates_` 残留
3. 顶层出现 `schema_version: "1.1"`
4. `reflation_threshold` 仍为 `|slope| < SLOPE_EPS=0.002`（阈值未被顺手改动）
5. `reflation_value` 与 `ratios.cu_au.slope_5d` **数值完全相等**

**失败示例**：
- `regime_type: "RISK_NEUTRAL_REFLATION_FLAT"` 但 `regime_inputs.rates_proxy` 仍在 → 打回
- 阈值从 0.002 变成别的数 → 打回（本项是纯改名，不允许逻辑改动）

**全文搜索检查**：对 `env.json` 原始文本执行 `grep -i "rates"`，应返回 0 行。

---

## A-2 `fx_board` 摘要三列

**调用**：`fx_board(window=20)`

**通过标准**：

1. 摘要文本中每个货币行**同时包含** `z`、`z_short`、`Δ`、`state`、`purity` 五个值
2. 任一货币满足 `purity < 0.60` 时，该行带 `(!)` 标记
3. 摘要末尾有「动能转折排行」，按 `|Δ|` 降序，至少 5 项
4. 表头区含 `staleness_hours`、`dispersion`、`board_vol`、`residual_rms`

**数值正确性**（抽 3 个货币手工核对）：

| 检查 | 公式 |
|---|---|
| Δ 计算 | `delta == z_short - z`，误差 < 1e-9 |
| purity 计算 | `purity == max(membership.values())`，误差 < 1e-9 |
| 排行顺序 | 按 `abs(delta)` 严格降序 |

**基准数据**（2026-09-16 session，若数据未变可直接对照）：

```
USD   z=+0.606   z_short=+1.515   Δ=+0.909   UP_ACCEL    purity=0.909
JPY   z=+1.120   z_short=-0.296   Δ=-1.416   UP_DECEL    purity=1.000
AUD   z=+0.390   z_short=-0.312   Δ=-0.702   UP_DECEL    purity=0.548 (!)
GBP   z=-0.128   z_short=+0.214   Δ=+0.342   FLAT_FLAT   purity=0.490 (!)
EUR   z=-0.185   z_short=+0.008   Δ=+0.193   FLAT_FLAT   purity=0.509 (!)
CHF   z=-0.862   z_short=-0.456   Δ=+0.406   DOWN_FLAT   purity=0.594 (!)
NZD   z=-0.985   z_short=-0.801   Δ=+0.184   DOWN_FLAT   purity=0.815
CAD   z=+0.044   z_short=+0.128   Δ=+0.084   FLAT_FLAT   purity=0.835
```

`(!)` 应恰好出现在 AUD / GBP / EUR / CHF 四行。

**JSON 侧**：每个 currency 对象含显式的 `delta`、`purity`、`state_ambiguous` 字段，值与摘要一致。

**核心验收问题**（一句话判定）：

> 不打开 `board__w20.json`，只看摘要，能否读出「USD 的 z_short 是 +1.515，是全板最高，且是唯一 ACCEL 状态」？

能 → 通过。不能 → 打回。

---

## A-3 `fx_board_compare` 符号一致性

**调用**：`fx_board_compare()`

**通过标准**：

1. 输出含 `Δ(w20)` 与 `Δ(w50)` 两列
2. 第三列标注符号一致 / 冲突
3. 冲突行带 `(!)` 标记

**非阻断**：A-2 通过而 A-3 未做，可接受为部分交付，但需在交付说明中注明遗留。

---

## A-4 DXY 窗口模式

**这项必须跨日验，单日验不出来。**

### 方案 A（append-only）判定

**Day 1**：`dxy_series(n=8)`，记录 `rows` 总数、起始日期、`min`、`max`、`mean`
**Day 2**：同样调用

| 检查 | 通过标准 |
|---|---|
| 总行数 | Day 2 > Day 1（至少 +1） |
| 起始日期 | Day 2 == Day 1（**不前移**） |
| `min` / `max` | 除非出现真实新高新低，否则两日相同 |
| 标注 | `stats_scope: "full_history"` |

### 方案 B（滚动 + 标注）判定

| 检查 | 通过标准 |
|---|---|
| 标注字段 | 含 `window_mode: "rolling_fixed"`、`window_rows`、`stats_scope: "window_only"` |
| 警告文本 | `warning` 字段明确说明 min/max/mean 不可作为稳定归一化基准 |
| 行为一致 | 行数恒定、起点前移——与标注描述相符 |

### 历史对照（问题复现记录）

```
2026-09-16 读  402 rows (2025-03-03 .. 2026-09-15)  min=95.7501  max=106.5499  mean=99.3034
2026-09-17 读  402 rows (2025-03-04 .. 2026-09-16)  min=95.7501  max=105.5799  mean=99.2879
```

`max` 下降 0.97 而价格创新高——此即滚动漂移。修复后方案 A 下 `max` 不应再因滚动而变化；方案 B 下该现象保留但必须被标注。

---

## A-5 累积指数起点排查（书面）

**不是代码检查，是要一个明确答复。**

执行方需在交付说明中回答：

> `board__w20.json` / `board__w50.json` 背后的强度累积指数（cumulative index），其**起点是固定的还是滚动的**？

| 答复 | 后续动作 |
|---|---|
| FIXED / 固定 | 通过。必须提供固定 anchor / 起点不会漂移的证据 |
| ROLLING / 滚动 | **不通过**。跨日基准漂移，必须修复 |
| NONE / 无累积指数 | **通过，但必须完成 NONE 四条件验证** |
| UNKNOWN / 不确定 | **不通过**。必须查清 |

### NONE 模式通过条件

若实现声明不存在 cumulative index，则以下四项必须全部成立：

1. **无累计状态**
   - 不存在跨 session 持久化 cumulative value；
   - 不使用 `cumsum` / `cumprod` 或等价递归累计状态。

2. **只依赖当前窗口**
   - 当前 board 结果仅由当前窗口内数据决定；
   - 更早于窗口的数据不得改变当前 strength / z / z_short / state。

3. **Prefix independence**
   - 在最后 `window+1` 根输入完全相同的情况下，
     增加或删除更早历史不得改变输出；
   - 至少验证：w20、w50；
   - 对比字段：strength、z、z_short、state。

4. **重算幂等**
   - 同一输入连续重算两次，输出必须一致；
   - z 数值误差应 < 1e-12。

最终判定：

> 四项全部通过 → A-5 PASS
> 任一失败 → A-5 FAIL

**为什么阻断**：这是所有横截面结论的地基。若累积指数起点在漂移，前面 A-2 验收通过的那张表在跨日比较时全部失效——而这种失效不报错、回测也不暴露。

---

## A-6 `get_series` 基本返回

**调用**：`get_series(symbol="XAUUSD", tf="D1", n=120)`

**通过标准**：

1. 返回 `rows` 数组，长度 > 0
2. 每行含：`session_date`、`ts_utc`、`open`、`high`、`low`、`close`、`volume`、`bars_in_session`、`partial`、`segment_id`、`roll_flag`
3. 顶层含：`symbol`、`tf`、`price_kind`、`source`、`session_boundary_utc`、`asof_semantics`、`staleness_hours`
4. `ts_utc` 为 tz-aware UTC，带 `Z` 后缀
5. `session_boundary_utc == 22`，与 `fx_env` 一致

**一致性交叉验证**（关键）：

| 检查 | 方法 |
|---|---|
| 与 `fx_env` 同源 | `get_series("XAUUSD").rows[-1].session_date` == `fx_env().effective_session` |
| 与四比率自洽 | 取 `XAUUSD` 与 `XTIUSD` 最后一根 close，相除应等于 `fx_env().ratios.gor.level`，相对误差 < 0.1% |
| 与 `HG连续` 自洽 | `HG.close / XAUUSD.close` 应等于 `ratios.cu_au.level`，相对误差 < 0.1% |

**这三条交叉验证同时通过，才说明 `get_series` 读的是同一份数据，而不是另起一路。**

**开放范围**：至少 `XAUUSD`、`XTIUSD`、`US500`、`HG连续` 四个可用，`tf` 支持 `D1` 与 `H1`。

---

## A-7 无派生指标（一票否决）

**调用**：`get_series(symbol="XAUUSD", tf="D1", n=10)`

**扫描返回 JSON 的全部字段名**，出现以下任一即**立即打回，不接受任何解释**：

```
atr  atr14  adx  adx14  adx_slope  ema  sma  ma  rsi  macd
bb_upper  bb_lower  zscore  z  slope  momentum  signal
sr_zone  support  resistance  poi  fvg  trend  state
```

**允许存在的非 OHLC 字段仅限**：`bars_in_session`、`partial`、`segment_id`、`roll_flag`、`volume`，以及顶层元数据。

这五个是**数据质量标记**，不是派生指标——它们描述这根 bar 本身是否可信，不对价格做任何判断。

**为什么一票否决**：本通道的唯一价值是提供未经引擎加工的原始序列，用于反向校验引擎。本周三个已确认问题（`RATES_*` 名不副实、DXY 滚动漂移、gauge 底层自我参照）全部依赖未加工数据才发现。加入派生指标即让该通道价值归零，且此后再无独立校验手段。

---

## A-8 `get_series` 边界行为

| 用例 | 调用 | 通过标准 |
|---|---|---|
| B-1 未知 symbol | `get_series("FAKEXYZ")` | 返回错误，**且列出可用 symbol 列表** |
| B-2 n 超量 | `get_series("XAUUSD", n=99999)` | 返回全部可用行，含 `available: <实际行数>`，不抛异常 |
| B-3 不支持的 tf | `get_series("XAUUSD", tf="M5")` | 返回错误，**且列出支持的 tf** |
| B-4 含残缺会话 | 任意 symbol，扫描 `partial` 字段 | 存在 `partial: true` 的行时正常返回，不静默剔除 |

B-1 / B-3 的重点是**错误消息要可用**——只返回「invalid symbol」不通过，必须告诉调用方有哪些可选。

---

## A-9 `gauge_underlying_exposure`

**调用**：`fx_env()`

**通过标准**：

1. 顶层含 `gauge_underlying_exposure`、`gauge_underlying_concentration`、`gauge_self_reference_warning`
2. 摘要输出中，`gauge_self_reference_warning` 非空时追加 ⚠ 行

**手工核对基准**（四比率结构固定，此表应恒等成立）：

| 标的 | `n_components` | `components` | `net_sign` | `abs_weight` |
|---|---|---|---|---|
| XAUUSD | 3 | cu_au, gor, au_spx | −1 | 0.75 |
| HG | 2 | cu_au, hg_wti | +1 | 0.50 |
| XTIUSD | 2 | gor, hg_wti | −1 | 0.50 |
| US500 | 1 | au_spx | −1 | 0.25 |

3. `gauge_underlying_concentration == 0.75`
4. `gauge_self_reference_warning == ["XAUUSD"]`
5. VIX **不出现**在 exposure 表中（其 `contrib` 恒为 0，不是 gauge 输入）

**净符号核对**：XAUUSD 在 cu_au 作分母（−1）、gor 作分子（+1）、au_spx 作分子（+1），求和 = +1 → 取符号应为 **+1**。

> ⚠ 若实现得出 −1，需说明符号约定；两种约定都可接受，但**必须在字段说明中写清是哪一种**，否则消费方无法解读。此项不阻断，但必须有明确定义。

---

## 回归检查（v1.0 已交付项不得被破坏）

| ID | 检查 | 通过标准 |
|---|---|---|
| R-1 | `gauge_sum_check` | 仍存在，`abs_diff < 1e-9` |
| R-2 | `regime_inputs.caveats` | 仍含「循环论证风险」那条（仅随 C-1 改名，内容不删） |
| R-3 | `reflation_real_source` | 仍含 `unavailable … 不冒充` 措辞 |
| R-4 | VIX 披露 | `gauge_components` 中 VIX 条目仍在，`contrib: 0.0`，`note` 保留 |
| R-5 | `staleness_hours` / `staleness_status` | 仍存在且数值合理 |
| R-6 | `residual_rms` / `residual_by_pair` | 仍存在，28 个交叉盘全覆盖 |
| R-7 | 残差量级 | `residual_rms < 5e-4`，且无单一 symbol 残差 > 10× 中位数 |

**R-2 / R-3 / R-4 特别强调**：这三项是 v1.0 交付里质量最高的部分——主动披露了自身局限。改名或重构时容易被当作冗余注释删掉。删掉即打回。

---

## 签收表

| ID | 项 | 结果 | 备注 |
|---|---|---|---|
| A-1 | regime 改名 | ☐ 通过 ☐ 打回 | |
| A-2 | board 摘要三列 | ☐ 通过 ☐ 打回 | |
| A-3 | compare 符号列 | ☐ 通过 ☐ 打回 ☐ 遗留 | |
| A-4 | DXY 窗口 | ☐ 通过 ☐ 打回 | 方案：☐ A ☐ B |
| A-5 | 累积指数起点 | ☐ FIXED ☐ ROLLING ☐ NONE（附四条件验证） ☐ UNKNOWN | 书面答复： |
| A-6 | get_series 基本 | ☐ 通过 ☐ 打回 | 三条交叉验证：☐☐☐ |
| A-7 | 无派生指标 | ☐ 通过 ☐ **一票否决** | |
| A-8 | 边界行为 | ☐ B1 ☐ B2 ☐ B3 ☐ B4 | |
| A-9 | 底层暴露 | ☐ 通过 ☐ 打回 | 符号约定： |
| R-1~R-7 | 回归 | ☐ 全通过 ☐ 有破坏 | |

**整体判定**：A-1 / A-2 / A-4 / A-5 / A-6 / A-7 / A-9 全部通过且 R-1~R-7 无破坏 → 接受 v1.1。

A-7 单独一票否决——即便其余全部通过，A-7 失败也不接受。

---

## 交付方需附带的说明

1. A-5 的书面答复（累积指数起点固定 or 滚动）
2. A-4 选择的方案（A 或 B）及理由
3. A-9 的符号约定说明
4. 已知遗留项列表
5. 本次是否改动了任何**计算逻辑**（C-1 / C-2 / C-5 应为纯输出层，C-3 可能涉及存储层，C-4 为新增）——若有计算逻辑改动，需单独列出并说明原因
