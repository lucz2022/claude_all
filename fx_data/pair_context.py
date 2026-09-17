"""§5.3 位置层 pair context：ATR / ADX / 支撑阻力区 / ACS-RCS 微观检查。"""
import numpy as np
import pandas as pd

from . import config


def atr14(df: pd.DataFrame, n: int = 14) -> float:
    """Wilder ATR（最后一根）。"""
    h, l, c = df['high'], df['low'], df['close']
    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    return float(tr.ewm(alpha=1 / n, min_periods=n).mean().iloc[-1])


def adx14(df: pd.DataFrame, n: int = 14) -> tuple[float, float]:
    """ADX(14) 与其 3 根斜率。返回 (adx, adx_slope_3)。"""
    h, l = df['high'], df['low']
    up = h.diff()
    dn = -l.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    prev_c = df['close'].shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / n, min_periods=n).mean()
    pdi = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / n, min_periods=n).mean() / atr
    mdi = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / n, min_periods=n).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / n, min_periods=n).mean()
    tail = adx.dropna().tail(4)
    slope = float((tail.iloc[-1] - tail.iloc[0]) / 3) if len(tail) >= 2 else float('nan')
    return float(adx.iloc[-1]), slope


def sr_zones(df: pd.DataFrame, lookback: int = 250, touch_tol_atr: float = 0.5,
             min_touches: int = 2, merge_tol_atr: float = 0.4) -> list[dict]:
    """摆动点聚合成支撑/阻力区。触点容差与合并容差均以 ATR 为单位。"""
    d = df.tail(lookback).reset_index(drop=True)
    if len(d) < 20:
        return []
    a = atr14(d)
    if not np.isfinite(a) or a <= 0:
        return []

    # 分形摆动点：左右各 2 根确认
    hh, ll = d['high'].to_numpy(), d['low'].to_numpy()
    highs, lows = [], []
    for i in range(2, len(d) - 2):
        if hh[i] == max(hh[i - 2:i + 3]):
            highs.append((i, float(hh[i])))
        if ll[i] == min(ll[i - 2:i + 3]):
            lows.append((i, float(ll[i])))

    def cluster(points, kind):
        zones = []
        for i, p in sorted(points, key=lambda t: t[1]):
            hit = None
            for z in zones:
                if abs(p - z['mid']) <= merge_tol_atr * a:
                    hit = z
                    break
            if hit is None:
                hit = {'prices': [], 'mid': p, 'idxs': [], 'kind': kind}
                zones.append(hit)
            hit['prices'].append(p)
            hit['idxs'].append(i)
            hit['mid'] = float(np.mean(hit['prices']))
        out = []
        for z in zones:
            touches = sum(
                1 for i, px in enumerate(d['close'])
                if abs(px - z['mid']) <= touch_tol_atr * a
            )
            if touches >= min_touches:
                out.append({'lo': float(min(z['prices'])), 'hi': float(max(z['prices'])),
                            'kind': kind, 'touches': touches})
        return out

    cur = float(df['close'].iloc[-1])
    zones = cluster(highs, 'supply') + cluster(lows, 'demand')
    for z in zones:  # 距现价由近及远
        z['distance_atr'] = abs((z['lo'] + z['hi']) / 2 - cur) / a
    return sorted(zones, key=lambda z: z['distance_atr'])[:8]


def acs_rcs_micro(df: pd.DataFrame, ema_n: int = 20) -> dict:
    """ACS/RCS 微观检查。

    注意：ACS/RCS 的完整定义不在 fx-data-framework-v1.md 内。此处实现是
    **显式标注的 proxy**（H1 收盘价对 EMA 的位置与转向计数），不得冒称
    真正的 ACS/RCS。alive = 存在可辨识的微观方向；direction = 该方向；
    bars_since_turn = 上次穿越 EMA 以来的 bar 数。"""
    c = df['close']
    ema = c.ewm(span=ema_n, min_periods=ema_n).mean()
    above = (c > ema).astype(int)
    crosses = (above.diff() != 0)
    cross_idx = df.index[crosses.fillna(False)]
    if len(cross_idx) == 0:
        return {'status': 'proxy', 'method': 'ema_cross',
                'alive': False, 'direction': 'FLAT', 'bars_since_turn': None}
    last_cross = cross_idx[-1]
    pos = df.index.get_loc(last_cross)
    if not np.isscalar(pos):
        pos = pos.stop - 1 if hasattr(pos, 'stop') else int(pos[-1])
    bars_since = len(df) - 1 - int(pos)
    direction = 'UP' if above.iloc[-1] == 1 else 'DOWN'
    alive = bars_since <= 3 * ema_n
    return {'status': 'proxy', 'method': 'ema_cross',
            'alive': bool(alive), 'direction': direction,
            'bars_since_turn': int(bars_since)}


def build_pair_context(h1: pd.DataFrame, symbol: str, tf: str = 'H1') -> dict:
    """组装 get_pair_context 的 JSON。spread_now 由 api 层用真实点差填充。"""
    tail = h1.tail(config.OHLC_TAIL_MAX)
    ohlc_tail = [
        {'ts_utc': r.ts_utc.isoformat().replace('+00:00', 'Z'),
         'o': float(r.open), 'h': float(r.high), 'l': float(r.low), 'c': float(r.close)}
        for r in tail.itertuples()
    ]
    a = atr14(h1)
    adx, adx_slope = adx14(h1)
    return {
        'symbol': symbol,
        'tf': tf,
        'asof': h1['ts_utc'].iloc[-1].isoformat().replace('+00:00', 'Z'),
        'ohlc_tail': ohlc_tail,
        'atr14': a,
        'adx14': adx,
        'adx_slope_3': adx_slope,
        'sr_zones': sr_zones(h1),
        'acs_rcs_micro': acs_rcs_micro(h1),
    }
