# data MCP v1.1 交付说明（最终闭环版）

Core computation formula changes:
NONE

实际改动清单：
- MCP adapter switched to fx_data single source of truth（fx_board/fx_env/
  fx_board_compare/dxy_series 全部改为调用 fx_data，删除 server 内第二套
  renderer；repo root 注入 sys.path）
- MCP get_series added（tools/list 已注册，JSON 返回 fx_data.get_series 原样）
- D1 get_series ts_utc added（UTC-Z helper _iso_utc_z，每行携带）
- DXY MCP metadata exposure fixed（window_mode/window_rows/first_session/
  stats_scope/warning/stats.{min,max,mean}/available 全部可见）
- acceptance changed from internal API validation to MCP E2E
  （tests/acceptance_mcp_v1_1.py：启动真实 server + MCP 协议 tools/list +
  tools/call；ENGINE 层独立保留）
- A-5 acceptance formally supports NONE（任务书 §12 四条件：无累计状态/
  只依赖窗口/前缀无关/重算幂等；新增 tests/test_a5_prefix_independence.py
  实测 w20/w50 截尾等价）
- acceptance false-positive conditions fixed（A-3 逐行符号独立核算，不再
  「conflicts 非空即 PASS」；A-7 全树递归扫描 + rows ⊆ ALLOWED_ROW；
  BANNED/ALLOWED 常量统一到 tests/acceptance_rules_v1_1.py，evidence
  generator 共用同一份）
- get_series 参数错误以 {"error": ...} 结构化返回（含 symbol/tf 清单）——
  实测 MCP SDK 在长 SSE 会话上工具异常不回包，结构化错误为接口层修复，
  不涉及核心计算

## 验收结果（acceptance_mcp_run2_clean_process.log）

ENGINE: A-1/A-2/A-5 PASS；A-4 PENDING_REAL_NEXT_SESSION（day1=402r/
2025-03-04 快照已存 A4_dxy_day1.json；replay 验证通过；真实跨日未出现）
MCP E2E: TOOLS-LIST PASS（get_series 已注册）
A-1/A-2/A-3/A-5/A-6/A-7/A-8 B1-B4/A-9 全 PASS
（A-2: (!)x4 top2=JPY,USD；A-3: 逐行符号核算冲突=CAD/EUR/JPY/NZD；
 A-6: contract/UTC-Z/boundary/同源三项全过；A-7: banned=[], 全树29keys 递归）
R-1~R-6 PASS；R-7-w20 PASS（rms=1.46e-04, max/median=4.0）；
R-7-w50 PASS（rms=1.04e-04, max/median=6.9）

BLOCKING FAILURES: 0
PENDING: A-4 REAL NEXT SESSION
READY_EXCEPT_A4_REAL_TIME_CONFIRMATION

## 双 clean-process 一致性

acceptance_mcp_run1.log vs acceptance_mcp_run2_clean_process.log：
两次均从全新进程启动真实 server（各自随机端口、独立 token），结论逐行一致
（仅时间字段差异）。synthetic regression: 221 PASS / 0 FAIL。

## A-4 跨日流程（下一交易日起生效）

1. python -m fx_data.pipeline all
2. python tests/acceptance_mcp_v1_1.py（自动生成 A4_dxy_day2.json 并判：
   rows 增加、first_session 仍为 2025-03-04、last_session 前进 → A-4 PASS
   → 输出 ALL PASS）

## 自问（任务书 §30）

1. MCP Server 是否真正使用 fx_data 新实现？ YES（server.py 仅 adapter）
2. tools/list 是否存在 get_series？ YES（mcp_tools_list.txt）
3. A-1~A-9 是否实际经过 MCP tools/call？ YES（evidence 均来自 tools/call）
4. D1 get_series 是否每行有 UTC-Z ts_utc？ YES（A-6 逐行 parse 验证）
5. A-7 是否对 MCP 返回业务 JSON 全树递归扫描？ YES（A7_recursive_field_scan.txt）
6. A-5 NONE 是否写入正式 acceptance？ YES（四条件 ENGINE PASS）
7. 两遍 clean-process MCP E2E 是否一致？ YES（diff 为空）
8. 核心金融公式是否保持不变？ YES
9. synthetic regression 是否 0 FAIL？ YES（221 PASS / 0 FAIL）
10. A-4 若无真实下一 session，是否仍保持 PENDING？ YES

## 已知遗留

- A-4 真实跨日行为验证 pending（唯一遗留；下一 session 后自动闭环）
- P2 - session-specific partial semantics（HG partial 503/503、XAU 23-bar
  session 判 partial——expected_bars=24 统一判据对不同市场过严；按任务书
  §28 不在本轮处理，已立项后续 issue）
