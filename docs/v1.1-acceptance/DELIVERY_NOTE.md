# data MCP v1.1 交付说明（最终提交）

## A-4 DXY

```
DXY mode:
append_only（parquet footer 元数据 + dxy__D1.meta.json sidecar）

Anchor:
2025-03-04（append-only 采用日；此后不随 MT5 源滚动窗前移）

Verification:
Day1 rows = 402, first_session = 2025-03-04, last_session = 2026-09-16
Day2 尚未出现新有效 session —— 真实跨日行为验证 pending。
Simulation/replay verification: PASS（acceptance A-4 内嵌：源窗前移一天、
旧首日 2025-03-04 滚出源后合并结果首日不变、行数只增、同 session 以新表为准）。

Result:
PENDING_REAL_NEXT_SESSION（下一交易日运行 python -m fx_data.pipeline all
后重跑 tests/acceptance_v1_1.py，rows=403 且 first_session 不变即 PASS）
```

stats 检查：`stats_scope=full_history`；stats_min/max/mean 从 append-only 全存储
计算（`_get_dxy_series` 直接对存储全列聚合，不从源重算）。

## A-5 Currency cumulative index

```
Cumulative index mode:
NONE（无累积指数——不存在可漂移的起点；等价于 FIXED 语义的安全性）

Anchor session:
不适用（无累积量）

Implementation:
fx_data/board.py:_window_returns（窗口收益=纯两点对数差
log(c[-1])−log(c[-1-window])，board.py:40）
fx_data/board.py:build_board（z 分母=当期 28 盘截面 std(r, ddof=1)，逐会话重算）
fx_data/api.py:get_strength_board（输出 cumulative_index_mode='NONE' + 说明）

Persistence:
无累积状态可持久化；跨日 z 可比性由「同一统计定义 + 窗口内数据」保证。
防止漂移的机制即「不存在累积」：全模块无 cumsum/cumprod（验收脚本代码级检查）。

Cross-update anchor test:
PASS（acceptance A-5：w20/w50 模式一致 + 同数据重算幂等 z 逐币 <1e-12）

详细证明: docs/v1.1-acceptance/A5_board_index_proof.md
```

## A-9

```
Gauge self-reference rule:
abs_weight > 0.5（严格大于）

warning:
["XAUUSD"]（0.75；HG/XTIUSD 恰为 0.50 不入选，US500 0.25）

net_sign convention:
net_sign 表示标的价格上升对各 gauge ratio 的代数方向合计后的符号
（分子 +1、分母 −1，求和后取 sign）。HG +1 / XAUUSD +1 / XTIUSD −1 / US500 −1。
```

## 计算逻辑变更

```
None
```

本轮仅新增 `cumulative_index_mode`/`cumulative_index_note`/`residual_by_pair`
输出字段与验收脚本；未触碰 strength/gauge/membership/z/ratio/residual/state
任何计算公式。

## 已知遗留项

```
A-4 真实跨日行为验证 pending（READY_EXCEPT_A4_REAL_TIME_CONFIRMATION）：
2026-09-16 为当前最后完整 session；下一交易日 append-only 追加后复跑验收即闭环。
```

## 自审问答（任务书 §21）

1. 是否还有任何 blocking FAIL？ **NO**（两遍验收 BLOCKING FAILURES: 0）
2. A-7 是否真的扫描了完整 JSON？ **YES**（递归 collect_keys 全树 28 keys，
   banned 交集为空，见 acceptance_run*.log A-7 行）
3. A-5 是否有固定 anchor 的明确证据？ **YES**（mode=NONE + 代码级无累积断言 +
   重算幂等测试 + A5_board_index_proof.md）
4. 是否重启服务后重复验证过？ **YES**（run2 为 rebuild 后干净子进程全量重跑，
   两次结论逐行一致，diff 为空）
5. 是否改动任何核心计算公式？ **NO**
6. 输出文件是否来自最后一次通过验收的运行？ **YES**（build→acceptance run2→
   端点输出→拷贝，同一批次）

## 验收结果（acceptance_run2_clean_process.log）

```
A-1..A-3 PASS；A-4 PENDING_REAL_NEXT_SESSION；A-5..A-9 PASS（含 A-8 B1-B4）
R-1..R-7 PASS
BLOCKING FAILURES: 0
READY_EXCEPT_A4_REAL_TIME_CONFIRMATION
```

回归：tests/test_synthetic.py **221 PASS / 0 FAIL**（未破坏既有任何项）。
