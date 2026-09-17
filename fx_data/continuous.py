"""§4.5 IBKR 铜连续合约（比例回调）。换月是断裂，不是噪音（设计原则 5）。

ROLL_RULE（显式规则，审计锁定，不得擅改）：
- 换月日 roll_on(leg) = leg.last_trading_date − 5 个工作日（ROLL_LEAD_DAYS）。
- 活跃腿 = 段覆盖当前时刻的腿：按 ltd 升序第一个满足 roll_on(leg) > now 的腿。
  即换月日一过即切换（例：HGU6 roll_on=2026-09-21，9/21 前活跃腿为 HGU6，
  9/21 起为 HGZ6）。流动性不参与该选择。
- 构造：leg i 使用 [roll_on(i−1), roll_on(i)) 区间，活跃腿用 [roll_on(active−1), ∞)；
  ltd 晚于活跃腿的远月腿（如 HGZ7）一律不进入序列。

文档片段两处缺陷（见 ERRATA.md 第 2 条）：
(a) 比例因子用「已乘 factor 的 p_new」再乘进 factor，多次换月重复累计；
    修正：比例一律用原始（未调整）收盘价计算。
(b) 最大 ltd 腿全量拼入——远月稀疏报价污染尾部；修正见上方 ROLL_RULE。
"""
import numpy as np
import pandas as pd

from . import config

ACTIVE_MONTHS = config.HG_ACTIVE_MONTHS
ROLL_LEAD_DAYS = config.ROLL_LEAD_DAYS


def roll_on_of(ltd) -> pd.Timestamp:
    """换月日 = 最后交易日 − ROLL_LEAD_DAYS 个工作日。"""
    return (pd.to_datetime(ltd) - pd.tseries.offsets.BDay(ROLL_LEAD_DAYS))


def select_active_leg(legs: list[pd.DataFrame],
                      now: pd.Timestamp | None = None) -> int:
    """返回活跃腿索引（legs 已按 ltd 升序）。

    规则：段覆盖 now 的腿 = 第一个 roll_on(leg) > now 的腿（换月日即切换）。
    全部已过换月日时（异常/数据滞后）退回最后一腿。"""
    now = now or pd.Timestamp.utcnow()
    if now.tzinfo is None:
        now = now.tz_localize('UTC')
    else:
        now = now.tz_convert('UTC')
    rolls = [roll_on_of(d['last_trading_date'].iloc[0]).tz_localize('UTC')
             for d in legs]
    for i, r in enumerate(rolls):
        if r > now:
            return i
    return len(legs) - 1


def build_continuous(legs: list[pd.DataFrame],
                     now: pd.Timestamp | None = None) -> pd.DataFrame:
    """比例回调连续合约。legs 每张表需含 session_date / OHLC / volume /
    last_trading_date（原始未调整价）。比例因子链基于原始收盘价：

        factor(i) = ∏_{j>i} raw_close_j(d_j) / raw_close_{j−1}(d_j),
        d_j = roll_on(j−1)（leg j 段的起始日，两腿当日均有报价的重叠日）

    调整后价格 = 原始价 × factor(所在腿)。恒价三腿 100/110/121 调整后应为 121。"""
    if not legs:
        raise ValueError('no legs')
    legs = sorted(legs, key=lambda d: d['last_trading_date'].iloc[0])
    i_active = select_active_leg(legs, now)
    used = legs[:i_active + 1]              # 远月腿一律剔除（ROLL_RULE）

    bday = pd.tseries.offsets.BDay(ROLL_LEAD_DAYS)
    roll_on = [pd.to_datetime(d['last_trading_date'].iloc[0]) - bday
               for d in used]

    # 自活跃腿向过去累乘原始价比例，得到各腿 factor
    factors = [1.0] * len(used)
    for j in range(len(used) - 1, 0, -1):
        cur, prev = used[j], used[j - 1]
        d0 = roll_on[j - 1]                 # prev 段结束、cur 段开始的换月日
        cur_raw = cur.set_index('session_date')['close'].astype(float)
        prev_raw = prev.set_index('session_date')['close'].astype(float)
        # 换月日起 cur 段首个两腿均有报价的重叠日
        overlap = sorted(set(cur_raw.index) & set(prev_raw.index))
        overlap = [d for d in overlap if pd.Timestamp(d) >= d0]
        if not overlap:                     # 回退：换月日前最后的重叠日
            overlap = sorted(set(cur_raw.index) & set(prev_raw.index))
        if not overlap:
            raise RuntimeError(f'leg {j} 与前腿无重叠日，无法定比例因子')
        d = overlap[0]
        p_new, p_old = float(cur_raw.loc[d]), float(prev_raw.loc[d])
        if p_old <= 0:
            raise RuntimeError(f'leg {j} 换月比例分母非正 @ {d}')
        factors[j - 1] = factors[j] * (p_new / p_old)

    pieces = []
    for i, leg in enumerate(used):
        sd = pd.to_datetime(leg['session_date'])
        lo = roll_on[i - 1] if i > 0 else None
        hi = roll_on[i] if i < len(used) - 1 else None
        mask = pd.Series(True, index=leg.index)
        if lo is not None:
            mask &= (sd >= lo).to_numpy()
        if hi is not None:
            mask &= (sd < hi).to_numpy()
        seg = leg[mask.to_numpy()].copy()
        if seg.empty:
            continue
        for c in ['open', 'high', 'low', 'close']:
            seg[c] = seg[c].astype(float) * factors[i]
        seg['roll_flag'] = False
        seg.iloc[0, seg.columns.get_loc('roll_flag')] = (i > 0)
        pieces.append(seg)

    if not pieces:
        raise RuntimeError('连续合约为空——legs 与换月窗口不交')
    out = pd.concat(pieces, ignore_index=True)
    out = out.sort_values('session_date').reset_index(drop=True)
    out['segment_id'] = out['roll_flag'].cumsum().astype(int)
    return out


def mask_roll_neighborhood(df: pd.DataFrame, half_width: int = 1) -> pd.Series:
    """换月当根不参与斜率计算，前后各 half_width 根降权。
    即便做了比例调整，换月日的成交量/持仓/微观结构仍是断裂的。
    按位置索引，与 df 的索引类型无关。"""
    flags = df['roll_flag'].to_numpy()
    w = np.ones(len(df))
    for pos in np.nonzero(flags)[0]:
        w[pos] = 0.0
        for k in range(1, half_width + 1):
            for j in (pos - k, pos + k):
                if 0 <= j < len(df):
                    w[j] = min(w[j], 0.5)
    return pd.Series(w, index=df.index)


def validate_roll_returns(cont: pd.DataFrame, pad: int = 3) -> pd.DataFrame:
    """V13 材料：换月点前后 ±pad 根（**含 offset 0 换月当根**）的对数收益。
    比例回调正确时换月当根收益也应无异常尖峰。空 roll_flag 返回空表（未验证）。"""
    if 'roll_flag' not in cont.columns or not cont['roll_flag'].any():
        return pd.DataFrame()
    rets = np.log(cont['close'].astype(float)).diff()
    rows = []
    for i in cont.index[cont['roll_flag']]:
        for j in range(max(0, i - pad), min(len(cont), i + pad + 1)):
            rows.append({'roll_idx': i, 'bar_idx': j,
                         'offset': j - i, 'log_ret': rets.iloc[j]})
    return pd.DataFrame(rows)
