# A-5 书面证明：board 强度指数无累积起点（固定/滚动之争的正式答复）

**结论：本框架的强度板不存在「累积强度指数」（cumulative strength index），
因此不存在「起点固定还是滚动」的问题。z 的归一化基线是逐会话重算的截面
统计量，不依赖任何历史起点。**

## 代码证据（可复核）

1. **窗口收益 = 纯两点对数差，非累积**
   `fx_data/board.py:40`（`_window_returns` 内）：
   ```python
   r.append(np.log(c[-1]) - np.log(c[-1 - window]))
   ```
   每次只取「当前收盘」与「window 根前收盘」两个点；无任何
   `cumsum/cumprod/累加链`。`fx_data/board.py` 全模块 grep 无
   `cumsum`、`cumprod`（回归 `A-5 board 模块无 cumsum/cumprod` 代码级断言）。

2. **z = 截面标准化，基线每会话重算**
   `fx_data/board.py::build_board`：
   ```python
   board_vol = float(np.std(r, ddof=1))   # 当期 28 盘窗口收益的截面 std
   z = s / board_vol
   ```
   分母是**当次调用 28 个盘 r 的截面标准差**——每会话重新计算，与历史
   任何「起点」无关。

3. **配置常量**
   `fx_data/config.py:59`：`BOARD_WINDOWS = [20, 50]`。
   w20 只需 21 根 D1、w50 只需 51 根；当前源深度 403 根（首日 2025-03-04，
   EURUSD D1），深度远大于需求，源窗口滚动不影响 z 数值。

4. **与 DXY 的区别**
   DXY 是**逐日价格序列**（存在跨日 min/max 基准问题，故改 append-only）；
   强度板是**逐会话截面统计量**，输出（strength/z/state）只描述
   「window 根前→现在」这一段，不携带任何跨会话累积量。

## 派生文件未变化的说明

两轮 board__w{20,50}.json SHA-256 相同是**预期行为**：数据（同一批 D1，
无新会话）与算法（A-2 只改 JSON 展示字段与文本 renderer，未改数值计算）
均未变化。审计已程序化复核 16 个 currency 对象的
delta/purity/state_ambiguous 全部正确——数值层一致即证明。
