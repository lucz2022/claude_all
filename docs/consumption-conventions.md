# 数据消费约定 v1.1（C-1 ~ C-6）

> 来源：2026-09-17 外审实战中暴露的使用规则（fx-data-gap-analysis-v1.1.md §3）。
> 消费方（模型 / 人工 / 下游脚本）读取本数据链输出时必须遵守。

## C-1 跨源比价前必须对齐会话边界

`get_series` 使用 22:00Z 日切：会话 D 覆盖 `[D-1 22:00Z, D 22:00Z)`，标签 = 结束日。
第三方实时报价使用其自身日切。**直接比对两源的极值必然产生假阳性。**

实证（2026-09-16 session，XAUUSD）：

| 来源 | 覆盖窗口（北京时间） | low | 是否含 FOMC 后暴跌 |
|---|---|---|---|
| `get_series` | 9/16 06:00 → 9/17 06:00 | 4235.06 | 含 |
| 第三方（9/17 00:50 读数） | 该 session 尚未走完 | 4275.51 | 不含（FOMC 在 02:00） |

差异 22.5 美元是**观测窗口错位**，不是数据错误。真实的 feed 差异量级看 open：
4294.27 vs 4294.99（0.72）。

## C-2 gauge 自我参照降权

`gauge_self_reference_warning` 中的标的（当前 `["XAUUSD"]`，abs_weight=0.75，
出现在 4 个分量中的 3 个），**用 weather_gauge 分析该标的自身时必须降权或显式标注**
——gauge 对其不是独立环境信号，而是其自身波动的镜像。

## C-3 purity < 0.60 的 state 标签读取降权

`fx_board` 中 `(!)` 标记的货币（state 标签本身不确定），方向结论需降权。
当前：AUD / GBP / EUR / CHF。

## C-4 Δ 符号跨窗口冲突的货币，方向判断转人工

`fx_board_compare` 中「冲突(!)」行的货币（w20 与 w50 动能方向打架），
方向判断降权或转人工。当前：CAD / EUR / JPY / NZD。

## C-5 staleness_hours > 30 的快照不得用于当日决策

陈旧快照描述的是旧市场状态。实证：2026-09-17 曾出现 50 小时陈旧的 env 快照，
其 coefficient 描述的是 FOMC 前的压缩市场，与决议后状态不符。
消费前先看 `staleness_hours` / `staleness_status`。

## C-6 `partial` 字段当前不可用作筛选依据

`rows.partial = bars_in_session < 24`（框架文档 §4.2 静态 expected_bars）。
FX 周五 23 根、DST 日 23/25 根均为常态，故该字段对 FX **多数会话恒为 true**。
L3 派生层的实际残缺判据是 `bars_in_session < 20`。
若需按数据完整度过滤，用 `bars_in_session` 数值本身，不要用 `partial` 布尔值。

（`get_series` 顶层 `partial_basis` 字段已固化本说明。）
