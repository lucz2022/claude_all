# 三层外汇数据技术框架 v1.0

> **定位**：为「环境层 / 方向层 / 位置层」三层架构提供统一的数据底座。
> **主源**：IC Markets MT5（外汇 28 交叉盘全集 + 商品 + 指数）
> **增补源**：IBKR（铜、VIX——MT5 缺失或不可用的品种）
> **叙事源**：jin10（日历、央行口径、投行观点；不参与任何数值计算）

---

## 0. 设计原则

1. **单一来源优先**。同一层的数据尽可能来自同一个源。混源的代价不是多写代码，是引入无法在回测中暴露的系统性偏置。
2. **D1 不直接取，从 H1 重建**。各交易所日切时间不同（实测差异最大 14h45m），直接取 D1 无法对齐。从 H1 按统一日切重新聚合是唯一可靠做法。
3. **三层信息源独立**。环境层不得使用任何含股票 beta 的代理品种（例如用铜矿股替代铜），否则环境层与风险判断产生隐性共线。
4. **冗余即监控**。28 个交叉盘对 8 个货币是超定系统（21 个冗余约束）。这些约束的残差是免费的实时数据质量监控器，应常驻运行而非一次性验证。
5. **换月是断裂，不是噪音**。任何需要换月的品种，换月点必须显式标记并在滚动窗口计算中降权或剔除。

---

## 1. 架构总览

### 1.1 数据源分工

| 层 | 用途 | 来源 | 品种 / 标识 |
|---|---|---|---|
| 方向 | 强度板 D1（20/50 日窗口） | **MT5** | 28 个 G8 交叉盘全集 |
| 位置 | H1 结构 + ACS/RCS 微观检查 | **MT5** | EURCHF / GBPCHF / EURGBP / AUDCHF / USDCHF |
| 环境 | 金 | **MT5** | `XAUUSD` |
| 环境 | 油 | **MT5** | `XTIUSD` / `XBRUSD`（现货 CFD，不换月） |
| 环境 | 股指 | **MT5** | `US500` |
| 环境 | **铜** | **IBKR** | `HGZ6` conid `517660690` @COMEX（FUT） |
| 环境 | **VIX** | **IBKR** | `VIX` conid `13455763` @CBOE（IND） |
| 环境 | DXY | **自算** | 六成分几何加权，成分全部来自 MT5 |
| 环境 | G7 2Y 利差 | FRED | 已有 |
| 叙事 | 日历 / 央行 / 投行观点 | jin10 | 不参与计算 |
| — | FX 期权风险逆转 | **仍缺** | 见 §8 |

### 1.2 被排除的品种及理由

| 品种 | 排除理由 |
|---|---|
| `SCCO.NYSE`（南方铜业） | 铜矿股，携带股票 beta + 秘鲁政治风险 + 公司因素。用作铜代理会污染环境层与风险层的独立性 |
| `DXY_U6`、`VIX_U6_CFD`、`BRENT_M6`、`WTI_V6_CFD`、`BR_X6_CFD` | 含到期月的滚动合约。到期后静默变成僵尸报价而非报错 |
| `WTI.NYSE`、`XCUR.NAS`、`XTIA.NAS` 等股票 CFD | 同 SCCO |
| 14 个外围货币（AED/CNH/CZK/DKK/HKD/HUF/MXN/NOK/PLN/SEK/THB/TRY/ZAR） | 不进强度板。DKK 盯欧元、HKD/AED 盯美元，硬挂钩会向板内注入零方差伪自由度，破坏协方差结构 |

> `USDSEK` 例外：不进强度板，但作为 DXY 六成分之一保留。

### 1.3 G8 全集（28 个）

```
AUDCAD AUDCHF AUDJPY AUDNZD AUDUSD
CADCHF CADJPY
CHFJPY
EURAUD EURCAD EURCHF EURGBP EURJPY EURNZD EURUSD
GBPAUD GBPCAD GBPCHF GBPJPY GBPNZD GBPUSD
NZDCAD NZDCHF NZDJPY NZDUSD
USDCAD USDCHF USDJPY
```

---

## 2. 时间基准规范

### 2.1 各源实测日切

| 源 | 日切（UTC） | 对应本地 | 备注 |
|---|---|---|---|
| IBKR IDEALPRO（FX） | `21:15Z` | 17:15 ET | 本框架不使用 |
| IBKR COMEX（HG） | `22:00Z` | 18:00 ET | |
| IBKR CBOE（VIX） | `07:15Z` | 收盘后归档 | 实际反映**前一**交易日 |
| IC Markets MT5 | `21:00Z` 或 `22:00Z` | 服务器 UTC+2/+3 | **随夏令时变化，必须动态检测** |

### 2.2 规范

```
CANONICAL_CUT_UTC = 22          # 统一日切，COMEX 口径
SESSION_LABEL     = 'end_date'  # 会话标签 = 会话结束日的日历日期
```

选 22:00Z 的理由：铜走 COMEX，油的定价体系同属 NYMEX/COMEX。只需移动 XAU / US500 / VIX 三个序列，改动面最小。

### 2.3 时间戳三条铁律

1. **所有内部时间戳一律 tz-aware UTC**，落盘为 ISO8601 带 `Z`。禁止裸 naive datetime。
2. **bar 时间戳语义统一为「bar 起始时间」**。IBKR 原生即为起始时间，MT5 亦然——但 IBKR 日线的日期标签会比交易日早一天（`2026-09-10T21:15Z` 那根实际是 9/11 的交易日），重建时必须显式转换。
3. **VIX 需前移一日**。CBOE 的 `07:15Z` 归档时间戳对应的是前一交易日的收盘，不做前移会引入一整天的前视偏差。

### 2.4 交易日历

| 序列 | 日历 | 每周交易日 |
|---|---|---|
| FX 28 盘 | 周一 ~ 周五连续 | 5 |
| XAU / 油 | 近似 FX | 5 |
| US500 / VIX | **美股日历** | 5，含美国节假日休市 |
| HG | COMEX 日历 | 5，含节假日休市 |

比率序列（铜金比 / GOR / HG-WTI / 金SPX）**必须先做日历交集再计算**，否则节假日会造成伪突变，而你读的是 5 日斜率——伪突变直接打在主信号上。

---

## 3. 数据分层契约

```
L0  raw/        原始落盘，不做任何修改，只加采集元数据
L1  norm/       时区统一、日切统一、网格均匀化、缺口标记
L2  qc/         质量门（三角闭合 / 零和 / 重绘 / 陈旧）
L3  derived/    强度板、环境层比率、阴晴表
```

### 3.1 L1 标准 schema

每个 symbol 每个 timeframe 一张表：

| 字段 | 类型 | 说明 |
|---|---|---|
| `ts_utc` | datetime64[ns, UTC] | bar 起始时间，UTC |
| `session_date` | date | 会话结束日（仅 D1 有意义） |
| `open/high/low/close` | float64 | |
| `volume` | float64 / NaN | FX MidPoint 与 VIX 无成交量，置 NaN 而非 0 |
| `bars_in_session` | int | 该会话实际聚合了多少根 H1（用于检测残缺会话） |
| `segment_id` | int | 连续段编号，遇缺口/换月自增 |
| `source` | str | `mt5` / `ibkr` |
| `price_kind` | str | `bid` / `mid` / `last` |
| `roll_flag` | bool | 该根是否为换月点（仅期货） |

### 3.2 元数据（每次采集写一份）

```json
{
  "collected_at_utc": "2026-09-16T10:00:00Z",
  "canonical_cut_utc": 22,
  "mt5_server_utc_offset": 3,
  "mt5_server_name": "ICMarketsSC-...",
  "symbols": ["EURUSD", "..."],
  "price_kind": "bid",
  "pipeline_version": "1.0.0"
}
```

`mt5_server_utc_offset` 必须每次采集重新检测并记录，不得硬编码——这与 DML EA 的 CSV 协议 header 字段保持一致。

---

## 4. 清洗管线脚本

### 4.1 MT5 导出（含服务器时区动态检测）

```python
# mt5_export.py
import time
import MetaTrader5 as mt5
import pandas as pd
import numpy as np

G8 = ['AUD', 'CAD', 'CHF', 'EUR', 'GBP', 'JPY', 'NZD', 'USD']

PAIRS_28 = [
    'AUDCAD','AUDCHF','AUDJPY','AUDNZD','AUDUSD',
    'CADCHF','CADJPY','CHFJPY',
    'EURAUD','EURCAD','EURCHF','EURGBP','EURJPY','EURNZD','EURUSD',
    'GBPAUD','GBPCAD','GBPCHF','GBPJPY','GBPNZD','GBPUSD',
    'NZDCAD','NZDCHF','NZDJPY','NZDUSD',
    'USDCAD','USDCHF','USDJPY',
]
ENV_MT5 = ['XAUUSD', 'XTIUSD', 'XBRUSD', 'US500']
DXY_LEGS = ['EURUSD', 'USDJPY', 'GBPUSD', 'USDCAD', 'USDSEK', 'USDCHF']


def server_utc_offset_hours(probe: str = 'EURUSD') -> int:
    """MT5 的 tick.time 是服务器时间按 UTC 语义编码的 epoch 秒。
    与真实 UTC epoch 相减即得偏移。必须动态调用——夏令时会改变它。"""
    if not mt5.symbol_select(probe, True):
        raise RuntimeError(f'cannot select {probe}')
    tick = mt5.symbol_info_tick(probe)
    if tick is None:
        raise RuntimeError('no tick')
    return int(round((tick.time - time.time()) / 3600.0))


def fetch_h1(symbol: str, count: int, offset_h: int) -> pd.DataFrame:
    """取 H1 并转成 tz-aware UTC。D1 一律由 H1 重建，不直接取 TIMEFRAME_D1。"""
    if not mt5.symbol_select(symbol, True):
        raise RuntimeError(f'cannot select {symbol}')
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, count)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f'no data for {symbol}: {mt5.last_error()}')

    df = pd.DataFrame(rates)
    df['ts_utc'] = pd.to_datetime(df['time'] - offset_h * 3600, unit='s', utc=True)
    df = df.rename(columns={'tick_volume': 'volume'})
    df = df[['ts_utc', 'open', 'high', 'low', 'close', 'volume']]
    df['source'] = 'mt5'
    df['price_kind'] = 'bid'          # MT5 默认 bid 系；全集统一，勿混用
    return df.sort_values('ts_utc').reset_index(drop=True)


def export_all(h1_count: int = 24 * 400):
    if not mt5.initialize():
        raise RuntimeError(f'mt5 init failed: {mt5.last_error()}')
    try:
        offset = server_utc_offset_hours()
        info = mt5.terminal_info()
        meta = {
            'collected_at_utc': pd.Timestamp.utcnow().isoformat(),
            'mt5_server_utc_offset': offset,
            'mt5_server_name': getattr(info, 'name', None),
            'price_kind': 'bid',
            'pipeline_version': '1.0.0',
        }
        out = {}
        for sym in PAIRS_28 + ENV_MT5 + ['USDSEK']:
            try:
                out[sym] = fetch_h1(sym, h1_count, offset)
            except RuntimeError as e:
                print(f'[WARN] {sym}: {e}')
        return out, meta
    finally:
        mt5.shutdown()
```

> **为什么取 H1 而不是 D1**：MT5 的 D1 边界由服务器时区决定，夏令时切换当天会产生一根 23 小时或 25 小时的畸形日线。从 H1 重建可完全绕开这个问题。`24*400` 约 400 个交易日，满足 D1 ≥ 250 的要求且留出余量。

### 4.2 统一日切重建 D1

```python
# resample.py
import pandas as pd
import numpy as np

CANONICAL_CUT_UTC = 22


def rebuild_d1(h1: pd.DataFrame, cut_hour: int = CANONICAL_CUT_UTC,
               expected_bars: int = 24) -> pd.DataFrame:
    """把 H1 按统一日切重新聚合成 D1。
    会话定义：[D-1 cut, D cut)，标签 session_date = D（会话结束日）。"""
    df = h1.set_index('ts_utc').sort_index()

    # 会话起始日 = (ts - cut).floor('D')；标签取结束日 = +1 天
    session_start = (df.index - pd.Timedelta(hours=cut_hour)).floor('D')
    df = df.assign(session_date=(session_start + pd.Timedelta(days=1)).date)

    agg = df.groupby('session_date').agg(
        ts_utc=('open', lambda s: s.index[0]),   # 会话首根 H1 的起始时间
        open=('open', 'first'),
        high=('high', 'max'),
        low=('low', 'min'),
        close=('close', 'last'),
        volume=('volume', 'sum'),
        bars_in_session=('close', 'size'),
    )
    # 残缺会话标记（夏令时切换、半日市、数据缺口）
    agg['partial'] = agg['bars_in_session'] < expected_bars
    agg['volume'] = agg['volume'].replace(0, np.nan)
    return agg.reset_index()


def assign_segments(d1: pd.DataFrame, max_gap_days: int = 4) -> pd.DataFrame:
    """遇到超过阈值的日历缺口则自增 segment_id。
    正常周末缺 2 天，阈值 4 天可容忍长周末而捕捉真实断线。"""
    d = pd.to_datetime(d1['session_date'])
    gap = d.diff().dt.days.fillna(0)
    d1 = d1.copy()
    d1['segment_id'] = (gap > max_gap_days).cumsum().astype(int)
    return d1


def align_calendars(series_map: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """比率计算前的日历交集对齐。FX/商品/美股日历不同，
    不做交集会在节假日产生伪突变，直接污染 5 日斜率。"""
    common = None
    for df in series_map.values():
        s = set(df['session_date'])
        common = s if common is None else (common & s)
    common = sorted(common)
    return {
        k: df[df['session_date'].isin(common)]
             .sort_values('session_date').reset_index(drop=True)
        for k, df in series_map.items()
    }


def shift_vix_one_session(vix_d1: pd.DataFrame) -> pd.DataFrame:
    """CBOE 07:15Z 归档时间戳对应前一交易日收盘。不前移即前视偏差。"""
    out = vix_d1.copy()
    out['session_date'] = pd.to_datetime(out['session_date']).shift(-1).dt.date
    return out.dropna(subset=['session_date']).reset_index(drop=True)
```

### 4.3 关联矩阵 + 质量门

三角闭合检验与最小二乘强度板本质是同一件事：在对数空间里，`log(XXXYYY) = s_XXX − s_YYY`。三角闭合是这个线性关系的一个特例，最小二乘残差则是它的推广形式。

```python
# board.py
import numpy as np
import pandas as pd

G8 = ['AUD', 'CAD', 'CHF', 'EUR', 'GBP', 'JPY', 'NZD', 'USD']
IDX = {c: i for i, c in enumerate(G8)}


def incidence_matrix(pairs: list[str]) -> np.ndarray:
    """A[i, base] = +1, A[i, quote] = -1。形状 (n_pairs, 8)，秩为 7。"""
    A = np.zeros((len(pairs), len(G8)))
    for i, p in enumerate(pairs):
        base, quote = p[:3], p[3:6]
        A[i, IDX[base]] = 1.0
        A[i, IDX[quote]] = -1.0
    return A


def solve_strength(A: np.ndarray, r: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """最小二乘解 min ||A s - r||^2  s.t.  sum(s) = 0。

    因为 A @ ones = 0（ones 正是 A 的零空间），拟合项对 s 沿 ones 方向平移不变，
    故追加一行 ones 后，最优解在该方向上必然使 sum(s) = 0 严格成立，
    无需大权重罚项。返回 (强度向量, 每个盘的残差)。
    """
    A_aug = np.vstack([A, np.ones((1, A.shape[1]))])
    r_aug = np.concatenate([r, [0.0]])
    s, *_ = np.linalg.lstsq(A_aug, r_aug, rcond=None)
    resid = A @ s - r
    return s, resid


def build_board(d1_map: dict[str, pd.DataFrame], window: int) -> dict:
    """d1_map: {symbol: L1 D1 表}。返回强度板 + 质量诊断。"""
    pairs = sorted(d1_map.keys())
    A = incidence_matrix(pairs)

    r = []
    for p in pairs:
        c = d1_map[p]['close'].to_numpy(dtype=float)
        if len(c) < window + 1:
            raise ValueError(f'{p}: 样本不足，需要 {window + 1} 根，实得 {len(c)}')
        r.append(np.log(c[-1]) - np.log(c[-1 - window]))
    r = np.asarray(r)

    s, resid = solve_strength(A, r)

    # 板级共同波动标量：用于把强度归一化到可比尺度
    board_vol = float(np.std(r, ddof=1))
    z = s / board_vol if board_vol > 0 else np.zeros_like(s)

    return {
        'window': window,
        'currencies': [
            {'ccy': c, 'strength': float(s[i]), 'z': float(z[i])}
            for i, c in enumerate(G8)
        ],
        'ranking': [G8[i] for i in np.argsort(-s)],
        'dispersion': float(np.std(s, ddof=1)),
        'board_vol_scalar': board_vol,
        'residual_rms': float(np.sqrt(np.mean(resid ** 2))),
        'residual_by_pair': {p: float(e) for p, e in zip(pairs, resid)},
    }


def qc_residuals(resid_by_pair: dict[str, float],
                 spread_by_pair: dict[str, float],
                 k: float = 2.0) -> list[str]:
    """残差超过 k 倍点差的 symbol，其 feed 存在问题。建议常驻监控。"""
    bad = []
    for p, e in resid_by_pair.items():
        tol = k * spread_by_pair.get(p, 0.0)
        if tol > 0 and abs(e) > tol:
            bad.append(p)
    return bad


def qc_repaint(prev: pd.DataFrame, curr: pd.DataFrame,
               key: str = 'session_date') -> pd.DataFrame:
    """重绘检测：两次采集中，除最后一根外的历史 bar 必须完全一致。
    数据源侧的回填修正会让 prefix-invariance 测试在实盘中失效。"""
    a = prev.set_index(key).iloc[:-1]
    b = curr.set_index(key).loc[a.index]
    cols = ['open', 'high', 'low', 'close']
    diff = (a[cols] - b[cols]).abs()
    return diff[(diff > 1e-12).any(axis=1)]
```

### 4.4 DXY 自算

```python
# dxy.py
import numpy as np
import pandas as pd

# 六成分与权重；正负号表示该盘是「USD 在分母」还是「USD 在分子」
DXY_SPEC = [
    ('EURUSD', 0.576, -1),
    ('USDJPY', 0.136, +1),
    ('GBPUSD', 0.119, -1),
    ('USDCAD', 0.091, +1),
    ('USDSEK', 0.042, +1),
    ('USDCHF', 0.036, +1),
]
DXY_K = 50.14348112


def compute_dxy(d1_map: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """几何加权自算。相比滚动期货 CFD 的优势：无换月跳空、日切与 FX 全集一致。"""
    frames = []
    for sym, w, sign in DXY_SPEC:
        s = d1_map[sym].set_index('session_date')['close'].astype(float)
        frames.append((sign * w) * np.log(s))
    log_dxy = pd.concat(frames, axis=1).dropna().sum(axis=1)
    return pd.DataFrame({
        'session_date': log_dxy.index,
        'close': DXY_K * np.exp(log_dxy.to_numpy()),
    })
```

### 4.5 IBKR 铜连续合约（比例回调）

```python
# continuous.py
import re
import numpy as np
import pandas as pd

ACTIVE_MONTHS = {'H', 'K', 'N', 'U', 'Z'}   # HG 活跃月：3/5/7/9/12
ROLL_LEAD_DAYS = 5                           # 到期前 5 个交易日换月


def build_continuous(legs: list[pd.DataFrame]) -> pd.DataFrame:
    """legs 按到期日升序排列，每张表需含 session_date / open|high|low|close /
    volume / last_trading_date。

    使用**比例回调**而非价差回调：本框架下游计算的是比率与对数收益，
    比例回调保持收益率连续；价差回调在长序列上可能产生负价格。
    """
    legs = sorted(legs, key=lambda d: d['last_trading_date'].iloc[0])
    pieces, factor = [], 1.0

    # 从最新合约往回处理，累乘调整因子
    for i in range(len(legs) - 1, -1, -1):
        cur = legs[i].copy()
        ltd = pd.to_datetime(cur['last_trading_date'].iloc[0])
        roll_on = ltd - pd.tseries.offsets.BDay(ROLL_LEAD_DAYS)

        start = (pd.to_datetime(legs[i - 1]['last_trading_date'].iloc[0])
                 - pd.tseries.offsets.BDay(ROLL_LEAD_DAYS)) if i > 0 else None

        sd = pd.to_datetime(cur['session_date'])
        mask = (sd < roll_on) if i < len(legs) - 1 else pd.Series(True, index=cur.index)
        if start is not None:
            mask &= (sd >= start)
        seg = cur[mask].copy()
        if seg.empty:
            continue

        for c in ['open', 'high', 'low', 'close']:
            seg[c] = seg[c].astype(float) * factor

        seg['roll_flag'] = False
        seg.iloc[0, seg.columns.get_loc('roll_flag')] = (i > 0)
        pieces.append(seg)

        # 用换月日的重叠收盘价更新因子：factor *= P_new / P_old
        if i > 0:
            prev = legs[i - 1]
            overlap = set(seg['session_date']) & set(prev['session_date'])
            if overlap:
                d0 = min(overlap)
                p_new = float(seg.loc[seg['session_date'] == d0, 'close'].iloc[0])
                p_old = float(prev.loc[prev['session_date'] == d0, 'close'].iloc[0]) * 1.0
                if p_old > 0:
                    factor *= p_new / p_old

    out = pd.concat(pieces[::-1], ignore_index=True)
    out = out.sort_values('session_date').reset_index(drop=True)
    out['segment_id'] = out['roll_flag'].cumsum().astype(int)
    return out


def mask_roll_neighborhood(df: pd.DataFrame, half_width: int = 1) -> pd.Series:
    """换月当根不参与斜率计算，前后各 half_width 根降权。
    即便做了比例调整，换月日的成交量/持仓/微观结构仍是断裂的。"""
    w = pd.Series(1.0, index=df.index)
    idx = df.index[df['roll_flag']]
    for i in idx:
        w.loc[i] = 0.0
        for k in range(1, half_width + 1):
            for j in (i - k, i + k):
                if j in w.index:
                    w.loc[j] = min(w.loc[j], 0.5)
    return w
```

### 4.6 到期符号自检（启动时必跑）

```python
# symbol_guard.py
import re
import pandas as pd

FUT_MONTH_RE = re.compile(r'_?[FGHJKMNQUVXZ]\d(?:_|$)')


def guard_expiring_symbols(last_bar_ts: dict[str, pd.Timestamp],
                           now_utc: pd.Timestamp | None = None,
                           stale_hours: int = 24) -> None:
    """任何名字里含到期月模式的 symbol，若最后一根 bar 距今超过阈值，
    立即抛错。到期合约会静默变成不动的僵尸报价，而不是报错——
    这类失效最难发现，必须主动拦截。"""
    now = now_utc or pd.Timestamp.utcnow().tz_localize('UTC')
    stale = []
    for sym, ts in last_bar_ts.items():
        if not FUT_MONTH_RE.search(sym):
            continue
        if ts is None or (now - ts) > pd.Timedelta(hours=stale_hours):
            stale.append((sym, ts))
    if stale:
        raise RuntimeError(f'到期/僵尸合约: {stale}；检查换月配置')
```

---

## 5. 派生层输出契约

供 LLM 读取的三个端点。**只暴露派生结论，不暴露原始 bar**——8 货币 28 组合的 D1 全集约 3.5 万个数字，直接送进上下文会爆，且模型在对话里做数值计算既慢又易错。

### 5.1 `get_strength_board(window, asof)`

```json
{
  "asof": "2026-09-16T22:00:00Z",
  "window": 20,
  "canonical_cut_utc": 22,
  "segment_id": 14,
  "currencies": [
    {"ccy": "AUD", "strength": 0.0182, "z": 1.41, "rank": 1, "state": "UP_ACCEL"}
  ],
  "ranking": ["AUD", "USD", "CAD", "GBP", "NZD", "EUR", "JPY", "CHF"],
  "dispersion": 0.0113,
  "board_vol_scalar": 0.0129,
  "quality": {
    "residual_rms": 0.00004,
    "flagged_pairs": [],
    "partial_sessions": 0,
    "sum_check": 1.2e-17
  }
}
```

`state` 取 3×3 隶属度网格（方向 × 加速度）的最大隶属类别；完整隶属度向量作为可选字段 `membership`。

### 5.2 `get_env_state()`

```json
{
  "asof": "2026-09-16T22:00:00Z",
  "ratios": {
    "cu_au":  {"level": 0.001468, "slope_5d": -0.021, "state": "CONTRACTING"},
    "gor":    {"level": 66.4,     "slope_5d":  0.014, "state": "EXPANDING"},
    "hg_wti": {"level": 0.0975,   "slope_5d": -0.006, "state": "FLAT"},
    "au_spx": {"level": 0.641,    "slope_5d":  0.019, "state": "EXPANDING"}
  },
  "weather_gauge": -18.4,
  "regime_type": "RISK_NEUTRAL_RATES_UP",
  "coefficient": {"breakout": 0.7, "reversal": 1.0, "trend": 0.8},
  "calendar_intersection_ratio": 0.96,
  "sources": {"copper": "ibkr:HGZ6", "vix": "ibkr:VIX", "gold": "mt5:XAUUSD"}
}
```

`slope_5d` 为主信号（斜率优先于水平），`level` 仅供参考。

### 5.3 `get_pair_context(symbol, tf)`

```json
{
  "symbol": "EURCHF",
  "tf": "H1",
  "asof": "2026-09-16T22:00:00Z",
  "ohlc_tail": [{"ts_utc": "...", "o": 0.9447, "h": 0.9452, "l": 0.9441, "c": 0.9449}],
  "atr14": 0.00082,
  "adx14": 27.3,
  "adx_slope_3": 1.8,
  "sr_zones": [{"lo": 0.9432, "hi": 0.9441, "kind": "demand", "touches": 3}],
  "acs_rcs_micro": {"alive": true, "direction": "UP", "bars_since_turn": 4},
  "spread_now": 0.00021
}
```

`ohlc_tail` 上限 60 根。

---

## 6. 验证清单

| # | 验证项 | 通过标准 |
|---|---|---|
| V1 | 28 盘 D1 的 `session_date` 数组 | 逐元素完全相等 |
| V2 | 各 symbol D1 根数 | ≥ 250 且全集一致 |
| V3 | D1 相邻会话间隔 | 除周末外恒为 1 个交易日 |
| V4 | EURCHF 直接报价 vs EURUSD/USDCHF 合成 | 偏差 < 1 pip，且**无趋势性漂移** |
| V5 | H1 网格 | 恒为 3600s，无残缺 bar |
| V6 | 八货币当日对数收益零和校验 | `abs(sum) < 1e-12` |
| V7 | 环境层各序列与 FX 日历交集 | 交集根数 / FX 根数 ≥ 0.9 |
| V8 | 两次采集的历史 bar（除最后一根） | 完全一致（无重绘） |
| V9 | 21 个非美交叉的三角闭合残差 | 全部 < 2× 点差；**常驻监控** |
| V10 | 最小二乘残差 `A·s − r` 分布 | 无单一 symbol 长期偏置 |
| V11 | 含到期月的 symbol 扫描 | 全部标记，pipeline 中不得硬编码 |
| V12 | 统一重采样后四源同日 bar 对齐 | 实际时间窗重叠 ≥ 90% |
| V13 | 铜连续合约换月点收益率 | 换月前后 ±3 根对数收益无异常尖峰 |

**V8 与 V10 建议做成常驻**，其余为部署前一次性验证。V8 尤其重要：若数据源会回填修正历史 bar，你的 prefix-invariance 测试会在真实环境中静默失效。

---

## 7. 已知失效模式与监控

| 失效模式 | 表现 | 检测 | 处置 |
|---|---|---|---|
| 夏令时切换 | MT5 服务器偏移改变，D1 边界漂移 | 每次采集比对 `mt5_server_utc_offset` | 从 H1 重建即自动免疫 |
| 到期合约僵尸报价 | 价格不动，**不报错** | `symbol_guard` 启动自检 | 抛错阻断 |
| 单 symbol feed 异常 | 三角闭合残差放大 | V9 / V10 常驻 | 该盘退出当次板计算 |
| 历史回填 | prefix-invariance 静默失效 | V8 常驻 | 重算受影响窗口 |
| 节假日错位 | 比率序列伪突变 | V7 + `align_calendars` | 交集对齐 |
| 换月跳空 | 5 日斜率假信号 | V13 + `roll_flag` | `mask_roll_neighborhood` 降权 |
| 混源日切 | 斜率系统性偏置，**回测不可见** | V12 | 统一到 `CANONICAL_CUT_UTC` |

最后一行是本框架最需要防范的一类：它不会让程序崩溃，不会在回测里暴露（因为回测用同样错位的数据），只会让实盘长期跑偏。

---

## 8. 未覆盖缺口

| 缺口 | 现状 | 影响 |
|---|---|---|
| **FX 期权风险逆转 / 波动率曲面** | jin10 无；MT5 无；IBKR 的期权链是股票/期货期权，FX 覆盖不足 | 无法度量套息交易的拥挤度。对负偏度收益结构的策略，这是唯一具前瞻性的预警输入 |
| CFTC 持仓时间序列 | jin10 仅零散快讯 | 需接 CFTC 官方 API，周频 + 3 日滞后 |
| 掉期 / carry 实际成本 | 仅经纪商 swap 表 | 需从 MT5 侧单独取 |
| **铜现货指数** | IBKR COMEX IND 报 `No market data permissions` | 只能走期货，必须自建连续合约 |

---

## 附录 A：IBKR 契约标识实测

| 品种 | contract_id | security_type | exchange | 数据延迟 | volume | 日切 |
|---|---|---|---|---|---|---|
| HG 铜指数 | 36557087 | IND | COMEX | — | — | **权限不足** |
| HGU6 铜期货（9月） | 499901736 | FUT | COMEX | 600s | 有 | 22:00Z（**2026-09-28 到期**） |
| HGZ6 铜期货（12月） | 517660690 | FUT | COMEX | 600s | 有 | 22:00Z |
| VIX 指数 | 13455763 | IND | CBOE | 900s | 无 | 07:15Z（需前移一日） |
| EUR.CHF | 12087817 | CASH | IDEALPRO | — | 无（MidPoint） | 21:15Z |
| AUD.CHF | 15016125 | CASH | IDEALPRO | — | 无（MidPoint） | 21:15Z |

HG 活跃月序列：`HGH7` 535526340 / `HGK7` 546989039 / `HGN7` 558870405 / `HGU7` 570499461 / `HGZ7` 588626189

> IBKR 的 IDEALPRO FX 契约仅供交叉验证，本框架的外汇数据全部来自 MT5，不混源。

## 附录 B：IBKR H4 网格缺陷

实测 `FOUR_HOURS` 返回序列：

```
... 16:00Z → 20:00Z → 21:15Z → 00:00Z ...
```

每个交易日在日切处插入一根残缺 bar（`20:00Z` 那根仅 1h15m），且请求 12 根实际返回 15 根。**任何固定窗口的滚动计算都会被这两根污染。** 若使用 IBKR 的 H4，必须先重采样到均匀网格。本框架中 H1/H4 全部走 MT5，故不受影响。
