"""§4.3 关联矩阵 + 最小二乘强度板 + 质量门。"""
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


def _window_returns(d1_map: dict[str, pd.DataFrame], window: int,
                    pairs: list[str]) -> np.ndarray:
    r = []
    for p in pairs:
        c = d1_map[p]['close'].to_numpy(dtype=float)
        if len(c) < window + 1:
            raise ValueError(f'{p}: 样本不足，需要 {window + 1} 根，实得 {len(c)}')
        r.append(np.log(c[-1]) - np.log(c[-1 - window]))
    return np.asarray(r)


def build_board(d1_map: dict[str, pd.DataFrame], window: int,
                accel_window: int | None = None) -> dict:
    """d1_map: {symbol: L1 D1 表}。返回强度板 + 质量诊断。

    accel_window: 用于方向×加速度网格的短窗口；None 取 max(2, window // 4)。
    """
    pairs = sorted(d1_map.keys())
    A = incidence_matrix(pairs)
    r = _window_returns(d1_map, window, pairs)
    s, resid = solve_strength(A, r)

    # 板级共同波动标量：用于把强度归一化到可比尺度
    board_vol = float(np.std(r, ddof=1))
    z = s / board_vol if board_vol > 0 else np.zeros_like(s)

    aw = accel_window or max(2, window // 4)
    if window > aw:
        r_short = _window_returns(d1_map, aw, pairs)
        s_short, _ = solve_strength(A, r_short)
        bv2 = float(np.std(r_short, ddof=1))
        z_short = s_short / bv2 if bv2 > 0 else np.zeros_like(s_short)
    else:
        z_short = z.copy()

    return {
        'window': window,
        'accel_window': aw,
        'currencies': [
            {'ccy': c, 'strength': float(s[i]), 'z': float(z[i]),
             'z_short': float(z_short[i])}
            for i, c in enumerate(G8)
        ],
        'ranking': [G8[i] for i in np.argsort(-s)],
        'dispersion': float(np.std(s, ddof=1)),
        'board_vol_scalar': board_vol,
        'residual_rms': float(np.sqrt(np.mean(resid ** 2))),
        'residual_by_pair': {p: float(e) for p, e in zip(pairs, resid)},
    }


def qc_residuals(resid_by_pair: dict[str, float],
                 spread_log_by_pair: dict[str, float],
                 k: float = 2.0) -> list[str]:
    """残差（log 量纲）超过 k 倍点差（log 量纲）的 symbol，其 feed 存在问题。
    常驻监控。spread_log 必须来自真实点差（bar 级或实时快照），见 spread_log()。"""
    bad = []
    for p, e in resid_by_pair.items():
        tol = k * spread_log_by_pair.get(p, 0.0)
        if tol > 0 and abs(e) > tol:
            bad.append(p)
    return bad


def spread_log(h1: pd.DataFrame, point: float | None,
               close_ref: float | None = None,
               lookback: int = 48) -> tuple[float | None, str]:
    """bar 级真实点差 → log 量纲容忍度。返回 (spread_log, 来源标注)。

    仅使用 copy_rates spread 列的非零值（本经纪商实测历史全 0 → 返回
    (None, 'unavailable')，由上层回退实时快照；绝不代理伪造）。"""
    tail = h1.tail(lookback)
    px = close_ref if close_ref else float(tail['close'].iloc[-1])
    if 'spread' in tail.columns and point:
        nz = tail['spread'][tail['spread'] > 0]
        if len(nz) and px > 0:
            sp = float(nz.median()) * point
            return sp / px, 'bar_median(copy_rates)'
    return None, 'unavailable'


def spread_log_map(h1_map: dict[str, pd.DataFrame], inst_meta: dict,
                   live_spreads: dict[str, int] | None = None) -> dict[str, tuple]:
    """批量：{pair: (spread_log | None, source)}。

    主源 = 采集时实时快照（symbol_info().spread，真实可成交点差）；
    回退 = bar 级 copy_rates 非零中位数。实测本经纪商 bar 列退化
    （多数 0/1 点、非零值被 rollover 尖峰污染），故只作回退。"""
    out = {}
    for s, df in h1_map.items():
        meta = (inst_meta or {}).get(s, {})
        point = meta.get('point')
        px = float(df['close'].iloc[-1])
        sp_log, src = None, 'unavailable'
        if live_spreads and s in live_spreads and point and px > 0:
            sp_log, src = (live_spreads[s] * point) / px, 'live_snapshot(symbol_info)'
        if sp_log is None:
            sp_log, src = spread_log(df, point, px)
        out[s] = (sp_log, src)
    return out


def residual_timeseries(d1_map: dict[str, pd.DataFrame],
                        lookback: int = 250) -> pd.DataFrame:
    """V10 常驻监控材料：逐会话 LSQ 残差的时序统计。

    对过去 lookback 个会话逐日解最小二乘强度（1 日 log 收益向量），
    输出每盘残差的均值/标准差/样本数——长期偏置 = |均值| 超容忍度。"""
    pairs = sorted(d1_map.keys())
    A = incidence_matrix(pairs)
    closes = {p: d1_map[p]['close'].to_numpy(dtype=float) for p in pairs}
    n = min(len(c) for c in closes.values())
    start = max(1, n - lookback)
    rows = {p: [] for p in pairs}
    for t in range(start, n):
        r = np.asarray([np.log(closes[p][t]) - np.log(closes[p][t - 1])
                        for p in pairs])
        _, resid = solve_strength(A, r)
        for p, e in zip(pairs, resid):
            rows[p].append(float(e))
    stats = {p: {'mean': float(np.mean(v)), 'std': float(np.std(v, ddof=1)),
                 'n': len(v)} for p, v in rows.items()}
    return pd.DataFrame(stats).T.reset_index().rename(columns={'index': 'pair'})


def qc_repaint(prev: pd.DataFrame, curr: pd.DataFrame,
               key: str = 'session_date') -> pd.DataFrame:
    """重绘检测：两次采集中，除最后一根外的历史 bar 必须完全一致。
    数据源侧的回填修正会让 prefix-invariance 测试在实盘中失效。"""
    a = prev.set_index(key).iloc[:-1]
    b = curr.set_index(key).loc[a.index]
    cols = ['open', 'high', 'low', 'close']
    diff = (a[cols] - b[cols]).abs()
    return diff[(diff > 1e-12).any(axis=1)]
