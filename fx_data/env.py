"""§5.2 环境层：比率序列 + 5 日斜率 + 阴晴表 + 体制映射。

slope_5d 为主信号（斜率优先于水平），level 仅供参考。
所有比率先做日历交集再计算（§2.4），换月邻域降权（mask_roll_neighborhood）。
"""
import numpy as np
import pandas as pd

from .continuous import mask_roll_neighborhood
from .resample import align_calendars

SLOPE_DAYS = 5
SLOPE_EPS = 0.002          # 斜率死区：|slope| 低于此值视为 FLAT（对数/天）

# 比率定义：name → (分子序列, 分母序列, 是否降权换月邻域[分子])
RATIOS = {
    'cu_au':  ('HG', 'XAUUSD', True),    # 铜金比
    'gor':    ('XAUUSD', 'XTIUSD', False),  # 油金比 GOR
    'hg_wti': ('HG', 'XTIUSD', True),    # 铜油比
    'au_spx': ('XAUUSD', 'US500', False),   # 金股指比
}


def slope_5d(log_ratio: pd.Series, n: int = SLOPE_DAYS,
             weight: pd.Series | None = None) -> float:
    """对数比率的 n 日 OLS 斜率（对数/天），支持换月邻域权重。"""
    y = log_ratio.dropna().tail(n)
    if len(y) < max(3, n - 1):
        return float('nan')
    x = np.arange(len(y), dtype=float)
    w = weight.loc[y.index].to_numpy() if weight is not None else None
    X = np.vstack([x, np.ones_like(x)]).T
    if w is not None:
        sw = np.sqrt(w)
        Xw, yw = X * sw[:, None], y.to_numpy() * sw
    else:
        Xw, yw = X, y.to_numpy()
    coef, *_ = np.linalg.lstsq(Xw, yw, rcond=None)
    return float(coef[0])


def _state_of(slope: float) -> str:
    if np.isnan(slope) or abs(slope) < SLOPE_EPS:
        return 'FLAT'
    return 'EXPANDING' if slope > 0 else 'CONTRACTING'


def _window_z(log_r: pd.Series, n: int, weight: pd.Series | None = None):
    """n 日窗斜率的标准化读数：slope_n / std(n 日日差分)。
    供 gauge 分量横向比较，不参与 gauge 计算。数据不足/零分母 → None。"""
    y = log_r.dropna()
    if len(y) < n + 1:
        return None
    slope = slope_5d(log_r, n=n, weight=weight)
    diffs = (y - y.shift(n)).dropna() / n
    sd = float(diffs.std())
    if sd is None or sd <= 0 or np.isnan(slope):
        return None
    return float(slope / sd)


def compute_env(d1_map: dict[str, pd.DataFrame]) -> dict:
    """d1_map 需含 HG（连续合约）、XAUUSD、XTIUSD、US500、VIX 的 D1 表。
    返回 get_env_state 的完整 JSON 结构。"""
    need = {m for spec in RATIOS.values() for m in spec[:2]} | {'VIX'}
    missing = need - set(d1_map)
    if missing:
        raise ValueError(f'compute_env 缺序列: {missing}')

    # 日历交集（§2.4）：比率序列必须同日历，否则节假日伪突变污染斜率
    aligned = align_calendars({k: v for k, v in d1_map.items() if k in need})

    ratios, part_by_name = {}, {}
    # 分量标签：把比率成分映射到底层资产（copper=HG / gold=XAU / oil=XTI /
    # es=US500）。注意 gauge 的实际线性基是**四个比率项**（见下），不是五资产。
    COMPONENT_LABELS = {
        'cu_au': ('copper/gold', ['HG', 'XAUUSD']),
        'gor': ('gold/oil', ['XAUUSD', 'XTIUSD']),
        'hg_wti': ('copper/oil', ['HG', 'XTIUSD']),
        'au_spx': ('gold/es', ['XAUUSD', 'US500']),
    }
    for name, (num, den, roll_sensitive) in RATIOS.items():
        a = aligned[num].set_index('session_date')['close'].astype(float)
        b = aligned[den].set_index('session_date')['close'].astype(float)
        log_r = np.log(a / b)
        w = None
        if roll_sensitive and 'roll_flag' in aligned[num].columns:
            w = mask_roll_neighborhood(aligned[num].set_index('session_date'))
        slope = slope_5d(log_r, weight=w)
        # 波动基准：对数比率日差分的 std，用于把斜率标准化进阴晴表
        vol = float(log_r.diff().std()) if len(log_r) > 2 else np.nan
        ratios[name] = {
            'level': float(np.exp(log_r.iloc[-1])),
            'slope_5d': slope,
            'state': _state_of(slope),
        }
        if not np.isnan(slope) and vol and vol > 0:
            part_by_name[name] = slope / vol

    # P0-3 第二遍：gauge = mean(有效 parts)×100，contrib 分母 = **有效项数**
    # （零方差/数据不足的比率不进 gauge，也不能按 4 项摊分——否则 Σcontrib≠gauge）
    n_valid = len(part_by_name)
    gauge_components = []
    for name, (num, den, roll_sensitive) in RATIOS.items():
        a = aligned[num].set_index('session_date')['close'].astype(float)
        b = aligned[den].set_index('session_date')['close'].astype(float)
        log_r = np.log(a / b)
        w = None
        if roll_sensitive and 'roll_flag' in aligned[num].columns:
            w = mask_roll_neighborhood(aligned[num].set_index('session_date'))
        contrib = (part_by_name[name] * 100.0 / n_valid) if name in part_by_name else None
        label, underlyings = COMPONENT_LABELS[name]
        gauge_components.append({
            'name': name, 'label': label, 'underlyings': underlyings,
            'z20': _window_z(log_r, 20, w), 'z50': _window_z(log_r, 50, w),
            'contrib': contrib,
        })
    gauge_parts = list(part_by_name.values())

    # VIX 不是 gauge 输入（仅水平监控）——如实列零贡献，不冒充分量
    gauge_components.append({
        'name': 'vix', 'label': 'vix(level-only)', 'underlyings': ['VIX'],
        'z20': None, 'z50': None, 'contrib': 0.0,
        'note': '非 weather_gauge 输入，仅作 vix_level 监控披露',
    })

    # 阴晴表：四个标准化斜率的均值 ×100。正 = 环境扩张（risk-on 倾向），
    # 负 = 收缩。标准化使各比率量级可比。
    weather = float(np.mean(gauge_parts) * 100) if gauge_parts else float('nan')

    # C-5：底层暴露分解——四个「独立比率」实际由 4 个标的构成；贡献集中度
    # ≠ 底层集中度。分子记 +1、分母记 −1；VIX 不计入（contrib 恒 0）。
    n_ratio_components = len(RATIOS)
    exposure: dict[str, dict] = {}
    for name, (num, den, _roll) in RATIOS.items():
        for underlying, sign in ((num, +1), (den, -1)):
            e = exposure.setdefault(underlying, {'n_components': 0,
                                                 'components': [], 'net_sign': 0})
            e['n_components'] += 1
            e['components'].append(name)
            e['net_sign'] += sign
    for underlying, e in exposure.items():
        e['components'] = sorted(set(e['components']))
        e['n_components'] = len(e['components'])
        e['net_sign'] = int(np.sign(e['net_sign']))
        e['abs_weight'] = e['n_components'] / n_ratio_components
    underlying_conc = max((e['abs_weight'] for e in exposure.values()),
                          default=float('nan'))
    # A-9：warning 阈值 abs_weight > 0.5（严格大于）——当前四比率结构下仅
    # XAUUSD(0.75) 入选；HG/XTIUSD 恰为 0.5 不构成「主导性自我参照」
    self_ref = sorted(u for u, e in exposure.items() if e['abs_weight'] > 0.5)

    # P0-3：归因可加总性与集中度
    contribs = [c['contrib'] for c in gauge_components if c['contrib'] is not None]
    gauge_sum = float(sum(contribs)) if contribs else float('nan')
    abs_sum = sum(abs(c) for c in contribs)
    concentration = (max(abs(c) for c in contribs) / abs_sum) if abs_sum > 0 else float('nan')
    regime, coeff, regime_inputs = _regime(ratios, weather, concentration)

    fx_dates = set(d1_map['XAUUSD']['session_date']) if 'XAUUSD' in d1_map else set()
    common_n = len(aligned['XAUUSD']) if 'XAUUSD' in aligned else 0
    cal_ratio = (common_n / len(fx_dates)) if fx_dates else float('nan')

    # vix_level 必须取自对齐后的共同会话（审计第四轮 P1-3）：未对齐的
    # d1_map['VIX'] 末根可能比共同交集多一天（节假日），届时 effective_session
    # 与 vix_level 会来自不同交易日——前视错位。
    vix_close = (aligned['VIX']['close'].astype(float).iloc[-1]
                 if 'VIX' in aligned and len(aligned['VIX']) else float('nan'))

    return {
        'schema_version': '1.1',
        'ratios': ratios,
        'weather_gauge': weather,
        'gauge_components': gauge_components,
        'gauge_sum_check': {'sum_contrib': gauge_sum,
                            'weather_gauge': weather,
                            'abs_diff': abs(gauge_sum - weather)
                            if not (np.isnan(gauge_sum) or np.isnan(weather))
                            else None,
                            'note': 'gauge = Σcontrib（数学恒等：mean(parts)×100 '
                                    '的逐项分解；差异应仅来自浮点舍入）'},
        'gauge_concentration': concentration,
        'gauge_underlying_exposure': exposure,
        'gauge_underlying_concentration': underlying_conc,
        'gauge_self_reference_warning': self_ref,
        'gauge_self_reference_note': (
            'warning 中的标的（abs_weight>0.5，严格大于）：用本 gauge 分析该标的'
            '自身必须降权或显式标注——gauge 对其不是独立环境信号，而是其自身'
            '波动的镜像'),
        'gauge_quality_hints': (
            ['单一分量主导（concentration>0.5），建议降权或核查该分量数据']
            if not np.isnan(concentration) and concentration > 0.5 else []),
        'regime_type': regime,
        'regime_inputs': regime_inputs,
        'coefficient': coeff,
        'calendar_intersection_ratio': cal_ratio,
        'vix_level': vix_close,
        # 有效时点取自对齐后的共同会话（非某一源的末根）——节假日导致交集
        # 落后时，指标实际截止日如实反映，不得虚报
        'effective_session': str(aligned['XAUUSD']['session_date'].iloc[-1]),
        'asof': pd.to_datetime(
            aligned['XAUUSD']['ts_utc'].iloc[-1]).isoformat().replace('+00:00', 'Z'),
        'sources': {'copper': 'ibkr:HG连续(比例回调)', 'vix': 'ibkr:VIX',
                    'gold': 'mt5:XAUUSD', 'oil': 'mt5:XTIUSD', 'spx': 'mt5:US500'},
    }


def _regime(ratios: dict, weather: float,
            concentration: float = float('nan')) -> tuple[str, dict, dict]:
    """体制映射：晴雨表 + 铜金比方向 → 交易系数。规则显式可查；斜率死区内
    一律视为该轴中性。

    P1-4 审计结论（判定依据透明化，不改标签算法）；C-1（v1.1）改名：
    - 该轴的 proxy = cu_au（铜金比 5 日对数斜率状态，EXPANDING→REFLATION_UP），
      数据源 = ibkr HG 连续 + mt5 XAUUSD——**再通胀/增长代理，不是利率数据**。
      旧名 RATES_* 名不副实（加息+长端失控环境下再通胀方向与利率方向系统性
      背离），纯改名，阈值/方向约定/系数零改动；
    - 本维度不读取任何利率数据；如需真实利率维度需另接 FRED 2Y/10Y；
    - cu_au 分母含黄金，与 gauge 的 gold 分量部分共线——用作该轴代理有
      循环论证风险，已披露；不使用 DXY、不使用新闻。"""
    cu = ratios.get('cu_au', {})
    cu_slope = cu.get('slope_5d')
    cu_state = cu.get('state', 'FLAT')
    w = weather if not np.isnan(weather) else 0.0

    reflation_up = cu_state == 'EXPANDING'
    reflation_dn = cu_state == 'CONTRACTING'
    risk_on = w > 10
    risk_off = w < -10

    if risk_off and reflation_dn:
        regime, coeff = 'RISK_OFF_REFLATION_DN', {'breakout': 0.9, 'reversal': 1.2, 'trend': 0.6}
    elif risk_off and reflation_up:
        regime, coeff = 'RISK_OFF_REFLATION_UP', {'breakout': 0.8, 'reversal': 1.1, 'trend': 0.7}
    elif risk_on and reflation_up:
        regime, coeff = 'RISK_ON_REFLATION_UP', {'breakout': 1.1, 'reversal': 0.8, 'trend': 1.0}
    elif risk_on:
        regime, coeff = 'RISK_ON_REFLATION_FLAT', {'breakout': 1.0, 'reversal': 0.9, 'trend': 0.9}
    elif reflation_up:
        regime, coeff = 'RISK_NEUTRAL_REFLATION_UP', {'breakout': 0.7, 'reversal': 1.0, 'trend': 0.8}
    elif reflation_dn:
        regime, coeff = 'RISK_NEUTRAL_REFLATION_DN', {'breakout': 0.8, 'reversal': 1.1, 'trend': 0.7}
    else:
        regime, coeff = 'RISK_NEUTRAL_REFLATION_FLAT', {'breakout': 0.9, 'reversal': 1.0, 'trend': 0.8}

    regime_inputs = {
        'reflation_proxy': 'cu_au(铜金比) 5d log-ratio 斜率状态',
        'reflation_value': None if cu_slope is None or np.isnan(cu_slope) else float(cu_slope),
        'reflation_state': cu_state,
        'reflation_direction_convention': 'EXPANDING→REFLATION_UP（铜相对走强=再通胀/增长假设）',
        'reflation_threshold': f'|slope| < SLOPE_EPS={SLOPE_EPS} 视为 FLAT',
        'reflation_data_source': 'ibkr:HG连续 + mt5:XAUUSD（商品比率，非利率数据）',
        'reflation_real_source': 'unavailable — 当前未接入真实利率数据，'
                                 '不冒充真实利率维度；如需真实利率需另接 FRED 2Y/10Y '
                                 '（文档 §1.1 的 FRED 2Y 在本代码库未实现）',
        'reflation_coverage_note': '单一商品比率代理无法区分短端/长端；即便接入 2Y '
                                   '单点亦无法代表长端',
        'risk_proxy': 'weather_gauge（4 比率标准化 5d 斜率均值×100）',
        'risk_value': None if np.isnan(weather) else float(weather),
        'risk_threshold': 'risk_on > +10, risk_off < −10',
        'gauge_concentration': None if np.isnan(concentration) else float(concentration),
        'caveats': [
            'cu_au 分母含黄金，与 gauge 的 gold 分量部分共线——作利率代理存在循环论证风险',
            '不使用 DXY、不使用新闻/叙事改标签',
        ],
    }
    return regime, coeff, regime_inputs
