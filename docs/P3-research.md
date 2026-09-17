# P3 数据源调研（仅方案与可行性，未购买/未订阅/未改账户）

调研日期：2026-09-16。所有「可用性」标注均区分：官方文档确认 / 本环境实测 /
未验证。本机器对 cftc.gov 的自动化探测返回 403（疑似 UA 限制），不影响
「端点存在」的官方结论，但接入前须用真实 HTTP 客户端复验。

## P3-7 FX 期权风险逆转（Risk Reversal, RR25）数据源

**指标定义**：RR25 = vol(25Δ call) − vol(25Δ put)，币对/期限（1w/1m/3m）粒度。
正 = 市场为上行付溢价；负 = 下行保护需求强。

### 候选源

| 源 | 覆盖 | 频率 | 延迟 | 授权/成本 | API | 可用性标注 |
|---|---|---|---|---|---|---|
| CME QuikStrike（FX 期货期权结算波动率面） | G5+主要对（期货期权，非 OTC 交叉盘） | 日（结算） | 结算后 | 需 CME 账户（经纪/清算侧免费开通） | 无公开 REST；登录后 CSV 导出 | 未验证（未开通账户） |
| Bloomberg（FXFM/VCUB） | 全交叉盘 OTC 面板 | tick | 实时 | 终端订阅（机构级成本） | Terminal/BPIPE | 未验证（未订阅） |
| LSEG/Refinitiv Eikon | OTC 面板 | tick/日 | 实时 | 订阅 | TRKD/Refinitiv Data Platform | 未验证（未订阅） |
| TP-ICAP RISK / 传统经纪商 | OTC 报价快照 | 周 | — | 机构关系 | 无自助 API | 未验证 |
| IBKR（PHLX 上市 FX 期权） | EUR/USD 等少数对 | 实时 | — | 现有网关权限不足（用户已确认） | TWS API（已具备通道） | **实测：权限不足** |
| Darqube 等 SaaS 转售商 | 常见对 RR | 分钟~日 | 近实时 | freemium/订阅 | REST | 未验证（未注册） |
| FRED / CBOE / jin10 / MT5 | — | — | — | — | — | **确认无此数据**（框架文档 §8 已列缺口） |

**结论**：无免费公开、可 API 化的 RR 源。可行的最低成本路径：
(a) 若有 CME 账户 → QuikStrike CSV 手动/半自动导出入 L0；
(b) IBKR 若可开通 PHLX FX 期权行情权限（用户账户侧操作，本项目不代做）→
    用现有 ib_async 通道拉期权链隐含波动率自行合成 RR25；
(c) 机构订阅（Bloomberg/LSEG）超出本项目范围。

### 接入 schema（无论源如何，L1 统一形状）

```json
{"pair": "USDJPY", "asof_utc": "...", "tenor": "1M", "delta": 25,
 "rr": 0.0037, "atm": 0.0921, "source": "...", "fetched_at_utc": "...",
 "price_kind": "implied_vol", "roll_flag": false}
```

### 用途：负偏度策略拥挤度预警（框架文档 §8）

套息/负偏度策略的拥挤度在期权市场先于现货体现：
- 融资货币（funding ccy，如 carry 多头盘里的低息币）RR 深度负值 =
  市场在密集买入该币的下行保护（或卖上行）→ 偏度交易拥挤；
- RR 斜率的**周变化**比水平更有信息：快速走负 = 拥积加速，常领先于
  carry unwind 的现货拐点；
- 建议接入后作为环境层**前瞻输入**：`rr_z = (RR25_1M − 90d 均值)/90d std`，
  阈值（如 < −2σ）触发阴晴表的风险折减系数，而非事后用价格反推。

## P3-8 CFTC 持仓时间序列（方案，未实现）

### 官方源与存活性

- 门户：`publicreporting.cftc.gov`（CFTC Socrata 公共数据）。
- 已确认数据集（官方文档/数据集页）：**6dca-aqww = Legacy Futures Only**；
  **jun7-fc8e = Legacy Combined**；TFF（Traders in Financial Futures）数据集
  另列于门户。
- REST 形状：`https://publicreporting.cftc.gov/resource/{dataset}.json`
  （SoQL 过滤，`$limit`/`$where`/`$order`，JSON/CSV），无需凭据。
- **本环境实测**：自动化探测该端点返回 HTTP 403（疑似 UA/机器人限制）——
  端点存在性以官方文档为准，接入实现前必须用真实 HTTP 客户端
  （如 requests + 常规 UA）复验；P3 范围内不实现采集。

### 发布/修订语义（防前视的关键）

- 报告反映**周二**收盘持仓（`report_date_as_yyyy_mm_dd` = 该周二）；
- 发布：当周**周五 15:30 ET**（约 3 日滞后）；节假日顺延；
- 修订：迟报交易者导致后续修正，**最近 4 个报告周需每周重拉覆盖**；
- 防前视规则：任何信号计算的 `asof` 必须取 `max(release_time, ...)`，
  **严禁把发布后的数据对齐回报告周的周二**（框架铁律 §2.3 的同型约束）。

### 字段映射（Legacy Futures Only → L1）

| CFTC 字段 | L1 字段 | 说明 |
|---|---|---|
| market_and_exchange_names | contract_raw | 如 "EURO FX - CHICAGO MERCANTILE EXCHANGE" |
| report_date_as_yyyy_mm_dd | report_date | 报告周周二（≠可用时点） |
| (计算) release_time | usable_from | 发布时刻，防前视锚 |
| noncomm_positions_long_all/short_all | noncomm_long/short | 非商业净敞口分子 |
| open_interest_all | open_interest | |
| pct_of_oi_long_all/short_all | pct_long/short | |
| change_in_long_all 等 | change_* | 周变化 |

G8 合约映射（CME FX 期货，对 USD）：EUR=EURO FX、JPY=JAPANESE YEN、
GBP=BRITISH POUND、CAD=CANADIAN DOLLAR、CHF=SWISS FRANC、AUD=AUSTRALIAN
DOLLAR、NZD=NEW ZEALAND DOLLAR；USD 持仓由 DX（美元指数）+ 各币反向合成，
非美交叉盘用两币净持仓差构造（与强度板同一对数空间思想）。

### 缓存与落盘策略

- 周频任务：周五 15:30 ET 后拉最新周 + 回拉最近 4 周覆盖修订；
- L0：原始 JSON 快照（快照链不覆盖，同 raw 规范）；
- L1：按 report_date 归一，附 usable_from（release 时刻）与
  `is_revision` 标记；segment 语义同期货（修订周重算下游）。

## 来源

- [CFTC Commitments of Traders 主页](https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm)
- [Legacy Futures Only 数据集（6dca-aqww）](https://publicreporting.cftc.gov/Commitments-of-Traders/Legacy-Futures-Only/6dca-aqww)
- [Legacy Combined 数据集（jun7-fc8e）](https://publicreporting.cftc.gov/Commitments-of-Traders/Legacy-Combined/jun7-fc8e)
- [Socrata API Foundry 文档（6dca-aqww）](https://dev.socrata.com/foundry/publicreporting.cftc.gov/6dca-aqww)
- [cot_reports（社区 Python 封装，参考实现）](https://github.com/NDelventhal/cot_reports)
