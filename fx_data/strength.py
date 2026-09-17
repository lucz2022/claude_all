"""§5.1 强度板状态：方向 × 加速度 3×3 隶属度网格。

state 取网格最大隶属类别，完整隶属度向量作为可选字段 membership。
网格轴：方向 = z（强度 / 板级波动），加速度 = 短窗 z 减长窗 z 的变化率。
"""
import numpy as np

# 3×3 状态名
STATES = {
    ('UP', 'ACCEL'): 'UP_ACCEL', ('UP', 'FLAT'): 'UP_FLAT', ('UP', 'DECEL'): 'UP_DECEL',
    ('FLAT', 'ACCEL'): 'FLAT_ACCEL', ('FLAT', 'FLAT'): 'FLAT_FLAT', ('FLAT', 'DECEL'): 'FLAT_DECEL',
    ('DOWN', 'ACCEL'): 'DOWN_ACCEL', ('DOWN', 'FLAT'): 'DOWN_FLAT', ('DOWN', 'DECEL'): 'DOWN_DECEL',
}

# 隶属度网格阈值：|z| < Z_BAND 视为 FLAT，否则按符号取 UP/DOWN；
# 加速度轴同理，用 Δz = z_short - z 与短窗归一。
Z_BAND = 0.5


def _tri_membership(x: float, band: float) -> tuple[float, float, float]:
    """三角隶属：中心在 -band/0/+band 的三个三角，归一化使和为 1。
    超出 ±band 饱和到纯端点（远离零轴即纯 UP/DOWN，不退化回 FLAT）。"""
    if x <= -band:
        return (1.0, 0.0, 0.0)
    if x >= band:
        return (0.0, 0.0, 1.0)
    centers = np.array([-band, 0.0, band])
    m = np.maximum(0.0, 1.0 - np.abs(x - centers) / band)
    s = m.sum()
    return tuple(m / s) if s > 0 else (0.0, 1.0, 0.0)


def classify(z: float, z_short: float, window: int, accel_window: int) -> dict:
    """单货币分类。返回 {state, membership}。

    加速度轴取「沿运动方向的加速度」：v = z（ signed 动量），
    a = (z_short − z) / √缩放；UP 时 a>0 为 ACCEL，DOWN 时 a<0 才是 ACCEL
    （下跌加快 = 空头加速）。故 accel = a · sign(z)。"""
    scale = np.sqrt(window / max(accel_window, 1))
    dv = (z_short - z) / scale if scale > 0 else 0.0
    if abs(z) < 1e-12:
        accel = dv
    else:
        accel = dv * (1.0 if z > 0 else -1.0)

    m_dir = _tri_membership(z, Z_BAND)          # (DOWN, FLAT, UP)
    m_acc = _tri_membership(accel, Z_BAND)      # (DECEL, FLAT, ACCEL)

    membership, best = {}, (None, -1.0)
    for i, d in enumerate(('DOWN', 'FLAT', 'UP')):
        for j, a in enumerate(('DECEL', 'FLAT', 'ACCEL')):
            m = float(m_dir[i] * m_acc[j])
            membership[f'{STATES[(d, a)]}'] = m
            if m > best[1]:
                best = (STATES[(d, a)], m)
    return {'state': best[0], 'membership': membership}


def attach_states(board: dict) -> dict:
    """给 build_board 的输出附加 state / membership / rank，以及 C-2 显式字段：
    delta = z_short − z（动能转折）、purity = max(membership)、
    state_ambiguous = purity < 0.60（标签不确定，读取降权）。"""
    window = board['window']
    accel_window = board.get('accel_window', max(2, window // 4))
    ranked = {ccy: rank for rank, ccy in enumerate(board['ranking'])}
    currencies = []
    for c in board['currencies']:
        st = classify(c['z'], c['z_short'], window, accel_window)
        purity = max(st['membership'].values()) if st['membership'] else None
        z, zs = c.get('z'), c.get('z_short')
        delta = ((zs - z) if (z is not None and zs is not None) else None)
        currencies.append({**c, 'rank': ranked[c['ccy']] + 1, **st,
                           'delta': delta, 'purity': purity,
                           'state_ambiguous': bool(purity is not None
                                                   and purity < 0.60)})
    board = dict(board)
    board['currencies'] = currencies
    return board
