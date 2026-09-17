# fx_data — 三层外汇数据框架 v1.0 实现

对应 `fx-data-framework-v1.md` 的参考实现（规范片段勘误见 `ERRATA.md`）。
主源 IC Markets MT5（28 交叉盘 + 商品 + 指数），增补源 IBKR 网关 `127.0.0.1:4002`
（铜 HG 期货 legs **含已到期腿——conId 由 reqContractDetails 实时发现，不臆造** + VIX），
DXY 六成分几何加权自算。IBKR 侧只读（行情/契约查询），不下单、不改账户。

## 用法

```bash
cd C:\data\claude_all
PY=C:\Users\Administrator\AppData\Local\Python\pythoncore-3.14-64\python.exe

$PY -m fx_data.pipeline collect            # L0：MT5 + IBKR（严格模式，任一失败即抛错）
$PY -m fx_data.pipeline collect --allow-partial   # 显式放宽部分失败
$PY -m fx_data.pipeline build              # L1/L3（D1 剔周末壳；HG 到期规则连续合约）
$PY -m fx_data.pipeline validate           # V1-V14 严格验证（FAIL 非零退出码）
$PY -m fx_data.pipeline board --window 20 [--asof 2026-08-01T00:00:00Z]
$PY -m fx_data.pipeline env
$PY -m fx_data.pipeline context EURCHF

$PY tests/test_synthetic.py                # 合成回归（不依赖外部源）
```

## 关键语义（审计修订后）

- **快照链**：`data/raw/*/__snap__*.parquet` 永不覆盖；`__H1.parquet` 仅为最新副本。
  V8 用最近两份快照做前缀不变性（重绘）检测。
- **只落已收盘 bar**：采集中剔除进行中的小时 bar（各品种末 tick 时刻不同会污染
  同刻三角与端点）。
- **点差（真实来源优先级）**：**≤120s 新鲜** tick 快照 `ask−bid`（下限 1 point=
  tick 尺寸）→ **≤120s 新鲜**采集实时 `symbol_info().spread` → `copy_rates`
  spread 列非零中位数；皆无则显式 `unavailable`。V9/V10 的 log 量纲容忍度由此
  换算。陈旧的「live」声称一律不得使用。
- **V9 同刻三角**：21 个非美交叉直接报价 vs 合成（含倒数合成，如
  AUDNZD=AUDUSD÷NZDUSD）。**绝对年龄的锚 = 日历**（IC Markets 服务器
  EET/EEST = UTC+2/+3，与任何 tick 新鲜度无关；探针隐含偏移与日历冲突 →
  不可验证）——绝不允许「最新 tick 年龄≈0」的自证（全市场共同陈旧会假通过）。
  **只在取得 ≤120s 新鲜快照时才报告残差与剔除盘**；落盘年龄不随时间增长，
  有效年龄 = 存储年龄 + 流逝时间（V9 / API live 剔除 / 点差 tick 源同门）；
  快照陈旧/偏移冲突/时钟异常 → UNVERIFIED，不回退 H1 采样。三角内 skew ≤5s、
  有效年龄 ≤120s，PASS 前提 21/21 全覆盖。异常盘在 `get_strength_board` 中
  剔除重算并披露 `excluded_pairs`（live 剔除需快照新鲜且 asof ≥ 快照时刻）。
- **roll 规则（锁定）**：`roll_on = ltd − 5BD`；**活跃腿 = 按 ltd 升序第一个
  `roll_on > now` 的腿**（换月日一到即切换：2026-09-21 前为 HGU6，9/21 起为
  HGZ6——流动性不参与选择）；远月腿不进入序列。见 ERRATA#3。
- **VIX**：H1 重建即当日会话（ERRATA#1）。V14 锚点断言：工作日、会话标签=自身
  日期、分钟 ⊆ {0,15,30}、首 bar 精确为 07:15Z(美 DST)/08:15Z(冬)、末 bar 不晚于
  20/21Z（假期早收允许）。V12 对 VIX 门控 = 窗口包含度 + 该锚点规则——
  30 分钟整体错位 containment 仍 1.0，但锚点必 FAIL。
- **V5 规则引擎**（`session_rules.py`）：豁免仅来自显式规则——全年恒定周末窗口
  （周五 20:00Z 收、周日 21:00Z 开）+ 实测登记的节假日表；未知缺口保留 FAIL，
  多品种共识缺 bar 只作诊断附注不作豁免。
- **asof**：`get_strength_board(window, asof)` 不使用 asof 之后数据（含 live 剔除
  与点差明细），排除未完成会话，窗口为交易日（周末壳已在 L1 D1 剔除），segment
  边界不跨段（当前段不足即阻断）。`get_env_state` 的 asof/effective_session 取自
  **对齐后五源共同会话**（非某一源末根，节假日交集落后时不虚报时点）。
- **ACS/RCS**：完整定义不在框架文档内，端点输出显式 `status: proxy`（EMA 穿越
  代理），不冒称。

## 当前真实数据验证状态（2026-09-16，本轮实测）

PASS：V1 V2 V3 V6 V7（FX 分母=EURUSD=403；HG 0.9876 / XTI 0.9901 / US500 0.9926 /
VIX 0.9603）V8（快照链实测，无重绘/无删除）V10（时序偏置，覆盖 28/28）
V11（18 个到期月符号全登记）V12（coverage+exact：HG 0.962/0.996、XAU 0.965/0.998、
XTI 0.964/0.998、US500 0.961/0.998；VIX containment 0.996 + 锚点门控）
V13（5 个真实换月点含 offset0，max_z=2.6）V14。

诚实 FAIL/UNVERIFIED（不放宽、不伪造，成因已实证定性；第七轮口径）：
- **V4（同步 mid 门）→ UNVERIFIED**：coverage 只计**规范工作日**会话（周日壳
  排除，backfill/evaluate 双重防御）；当前 28 个工作日 verified < 30 门限 →
  insufficient_coverage(28<30)。此前把 3 个周日壳计入导致误判 FAIL(max 3.107
  pips)。工作日覆盖达到 30 后再按真实偏差判 PASS/FAIL；门（<1 pip + drift）
  不变。旧 bid-D1 检查保留为内嵌诊断（非门控）。
- **V5**：**纠正后真实分布 = 20 根合法重建**（2026-03-31 21:00Z×17 品种 tick +
  2026-06-29 06:00Z×3 品种 tick）。第六轮曾把 10 根**异日期杂散 M1 bar**（MT5
  对无数据窗返回 2026-06-11 价格）误作 3/31 重建——已修：M1/tick 均做 UTC
  窗口严格过滤（过滤后为空即硬缺口），10 个错误证据移入
  `data/raw/mt5_reconstructed/_quarantine/`（含原因 manifest，不删除），L1/D1
  已重建清除伪造 bar。**2026-06-29 06:00Z 仍有 27 品种缺口**（tick=0 且 M1=0，
  含 3/31 的 10 个无 tick 品种）→ 按审计规则保留硬缺口 FAIL（无交易日程/维护
  证据，不得以共识豁免）。缺陷时间报告 = 实际缺失槽位（06:00 而非 07:00）。
- **V9**：随每次采集的 tick 快照实时变化（违例盘组合/数量、同刻偏差均逐次
  不同）——**权威读数以 data/qc/latest.json 与 validate 输出为准**（含采集时
  点）；本 README 不固定具体违例名单。快照 ≤120s 新鲜时强度板据此剔除异常盘
  重算并披露，陈旧时不剔除（V9 记 UNVERIFIED）。

## 已知限制

- HG 历史深度：网关 `includeExpired` 应答最早到 HGU5（2025-09 到期）；
  更早腿（HGH5 等）未被网关返回，连续序列自 ~2024-09 起，已覆盖 FX 全历史
  （V7 = HG 0.9901，分母 FX=EURUSD=403）。
- V5 门控范围 = 28 FX 对。XAU/XTI/XBR/US500 等 CFD 有自己的日内休市
  （金 21:00-22:00Z），其日程规则未登记前不纳入 V5 门控（V12 已按时间窗覆盖）。
- 环境层系数/体制映射为显式规则实现（文档未给公式），在 `env.py` 顶部可查。
