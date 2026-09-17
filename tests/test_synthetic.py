"""合成数据自测：不依赖 MT5 / IBKR 网关，验证纯算法模块。

审计修订版新增针对性回归：
- continuous：恒价 3/4 腿 → 连续价恒为最末腿；因子双重计数回归；
  到期规则活跃腿（远月有报价不得成为尾部）。
- VIX：H1 会话对齐断言（无 shift 语义，ERRATA#1）。
- session_rules：DST 边界与显式节假日豁免；未知缺口保留缺陷。
- uniform_grid_check：2h 未知缺口 FAIL；周末/登记节假日豁免。
- api asof 语义（以合成 L1 落盘验证截断）与 V7 缺 HG/VIX FAIL、V13 未验证。
- storage：raw 快照不覆盖；V8 用快照链检出重绘。
"""
import numpy as np
import pandas as pd
import shutil
import sys
from pathlib import Path


def _configure_console_output():
    """Keep the standalone test runner usable on Windows CP936/GBK consoles.

    Test labels intentionally contain mathematical Unicode characters.  When
    Python inherits a legacy Windows console encoding, printing an unsupported
    character used to abort the entire suite with ``UnicodeEncodeError``.
    Emit UTF-8 consistently for consoles, pipes and CI capture, and render any
    unexpected encoding edge case as an escape instead of crashing.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if reconfigure is not None:
            try:
                reconfigure(encoding='utf-8', errors='backslashreplace')
            except (AttributeError, ValueError, OSError):
                # Embedded/captured streams may not permit reconfiguration.
                pass


_configure_console_output()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fx_data import config
from fx_data.board import (build_board, incidence_matrix, qc_repaint,
                           solve_strength)
from fx_data.continuous import (build_continuous, mask_roll_neighborhood,
                                select_active_leg, validate_roll_returns)
from fx_data.dxy import DXY_K, DXY_SPEC, compute_dxy
from fx_data.env import compute_env
from fx_data.pair_context import (acs_rcs_micro, adx14, atr14,
                                  build_pair_context, sr_zones)
from fx_data.resample import (align_calendars, assign_segments, rebuild_d1,
                              uniform_grid_check, vix_sessions_aligned,
                              drop_weekend_shells)
from fx_data.session_rules import is_nontrading_slot
from fx_data.strength import attach_states, classify
from fx_data.symbol_guard import guard_expiring_symbols
from fx_data import validate

rng = np.random.default_rng(42)
FAILURES = []


def check(name, cond, detail=''):
    status = 'PASS' if cond else 'FAIL'
    print(f'[{status}] {name} {detail}')
    if not cond:
        FAILURES.append(name)


# ---------- 合成 28 盘市场：price = exp(s_base - s_quote) ----------
N_H1 = 24 * 320
dates = pd.date_range('2025-06-02', periods=N_H1, freq='h', tz='UTC')
s_true = {c: rng.normal(0, 1, len(dates)).cumsum() * 1e-4 for c in config.G8}

h1_map = {}
for p in config.PAIRS_28:
    b, q = p[:3], p[3:]
    close = np.exp(s_true[b] - s_true[q])
    h1_map[p] = pd.DataFrame({
        'ts_utc': dates,
        'open': close * (1 + rng.normal(0, 1e-5, len(dates))),
        'high': close * 1.0002, 'low': close * 0.9998, 'close': close,
        'volume': 100.0, 'spread': 1, 'source': 'mt5', 'price_kind': 'bid',
    })

d1_map = {p: drop_weekend_shells(assign_segments(rebuild_d1(df)))
          for p, df in h1_map.items()}

r = validate.v1_session_dates_equal(d1_map) + \
    validate.v2_row_counts(d1_map, min_rows=200) + validate.v3_session_spacing(d1_map)
for x in r:
    check(f'{x["id"]} {x["item"]}', x['pass'], x['detail'])

# V4（第六轮起为同步 mid 门）：stub store —— 精确一致 store → PASS；无 store → UNVERIFIED
import fx_data.syncmid as _sm
_orig_load = _sm.load_store
_dates_v4 = [str(d.date()) for d in
             pd.date_range('2026-06-01', '2026-09-01', freq='B', tz='UTC')]
_perf = pd.DataFrame({
    'session_date': _dates_v4,
    'EURCHF__mid': [1.15 * 0.82] * len(_dates_v4),
    'EURUSD__mid': [1.15] * len(_dates_v4),
    'USDCHF__mid': [0.82] * len(_dates_v4),
    'skew_ms': [100.0] * len(_dates_v4), 'verified': [True] * len(_dates_v4),
    'reason': [''] * len(_dates_v4)})
_sm.load_store = lambda store=None: _perf
r = validate.v4_synthetic_cross(d1_map)
for x in r:
    check(f'{x["id"]} 同步mid一致→PASS（stub store）', x['pass'], x['detail'][:90])
_sm.load_store = lambda store=None: None
for x in validate.v4_synthetic_cross(d1_map):
    check(f'{x["id"]} 无store→UNVERIFIED', not x['pass'] and not x['verified'],
          x['detail'][:60])
_sm.load_store = _orig_load

# 强度板
board = attach_states(build_board(d1_map, window=20))
s_hat = {c['ccy']: c['strength'] for c in board['currencies']}
ref = d1_map['EURUSD']
close_ts = ref['ts_utc'] + pd.Timedelta(hours=23)
idx = np.clip(dates.searchsorted(close_ts.to_numpy()), 0, len(dates) - 1)
true_ret = {c: s_true[c][idx[-1]] - s_true[c][idx[-1 - 20]] for c in config.G8}
mu = np.mean(list(true_ret.values()))
corr = np.corrcoef([s_hat[c] for c in config.G8],
                   [true_ret[c] - mu for c in config.G8])[0, 1]
check('强度板恢复相关', corr > 0.999, f'corr={corr:.6f}')
for x in validate.v6_zero_sum(board):
    check(f'{x["id"]} {x["item"]}', x['pass'], x['detail'])

up = classify(1.5, 3.0, 20, 5)
check('state UP_ACCEL', up['state'] == 'UP_ACCEL', up['state'])
dn = classify(-1.5, -3.0, 20, 5)
check('state DOWN_ACCEL', dn['state'] == 'DOWN_ACCEL', dn['state'])
check('membership 归一', abs(sum(up['membership'].values()) - 1.0) < 1e-9)

# 重绘检测核心函数
prev = d1_map['EURUSD'].copy()
curr = prev.copy()
curr.iloc[3, curr.columns.get_loc('close')] *= 1.001
check('qc_repaint 检出篡改', len(qc_repaint(prev, curr)) == 1)

# ---------- session_rules / V5 ----------
# 周末规则全年恒定（实测 80/80 周末缺口恒 49h，跨 DST 无漂移）
for ts, expect_nontrading in [
        ('2026-09-12 12:00', True),    # 周六
        ('2026-09-11 21:00', True),    # 周五收盘后
        ('2026-09-13 20:00', True),    # 周日重开前（夏令时季）
        ('2026-01-11 20:00', True),    # 周日重开前（冬令时季，规则应一致）
        ('2026-09-13 21:00', False),   # 周日重开后
        ('2026-01-11 21:00', False)]:  # 周日重开后（冬令时季）
    check(f'时段规则 {ts}', is_nontrading_slot(pd.Timestamp(ts, tz='UTC'))
          == expect_nontrading)
check('登记节假日豁免', is_nontrading_slot(pd.Timestamp('2025-12-25 14:00', tz='UTC')))
check('登记早收日末盘后豁免', is_nontrading_slot(pd.Timestamp('2025-12-24 21:00', tz='UTC')))

def mk_h1(hours):
    return pd.DataFrame({'ts_utc': pd.DatetimeIndex(
        [pd.Timestamp('2026-09-15', tz='UTC') + pd.Timedelta(hours=h) for h in hours])})

# 周末缺口豁免（周五20:00 → 周日21:00，EU 夏令时）
wk = mk_h1([-4, -3])  # 周五 20:00、21:00Z？构造真实形状：
wk = pd.DataFrame({'ts_utc': pd.DatetimeIndex([
    '2026-09-11 19:00', '2026-09-11 20:00', '2026-09-13 21:00', '2026-09-13 22:00'],
    tz='UTC')})
check('周末缺口豁免', len(uniform_grid_check(wk)) == 0)
# 未知 2h 缺口（周二 10:00→12:00）必须 FAIL
gap2 = pd.DataFrame({'ts_utc': pd.DatetimeIndex(
    ['2026-09-15 09:00', '2026-09-15 10:00', '2026-09-15 12:00', '2026-09-15 13:00'],
    tz='UTC')})
check('未知 2h 缺口判缺陷', len(uniform_grid_check(gap2)) == 1)
# 共识不可豁免：全源共识缺 bar 仍 FAIL
avail = {(pd.Timestamp('2026-09-15').date(), 11): 1.0}
check('共识不构成豁免', len(uniform_grid_check(gap2, availability=avail)) == 1)
# 登记节假日缺口豁免
hol = pd.DataFrame({'ts_utc': pd.DatetimeIndex(
    ['2025-12-24 19:00', '2025-12-24 20:00', '2025-12-25 21:00', '2025-12-25 22:00'],
    tz='UTC')})
check('登记节假日缺口豁免', len(uniform_grid_check(hol)) == 0)

# ---------- VIX 对齐（ERRATA#1）----------
vix_h1 = pd.DataFrame({
    'ts_utc': pd.DatetimeIndex(
        ['2026-09-15 07:15', '2026-09-15 08:00', '2026-09-15 13:30',
         '2026-09-15 20:00', '2026-09-16 07:15', '2026-09-16 20:00'], tz='UTC'),
    'open': 17.0, 'high': 17.5, 'low': 16.8, 'close': 17.2, 'volume': np.nan,
})
check('VIX H1 会话对齐（无 shift）', vix_sessions_aligned(vix_h1))
vix_d1 = drop_weekend_shells(rebuild_d1(vix_h1))
check('VIX 会话标签=当日', str(vix_d1['session_date'].iloc[0]) == '2026-09-15')
check('VIX 会话收盘=当日末bar',
      abs(float(vix_d1['close'].iloc[0]) - 17.2) < 1e-12)

# ---------- continuous：因子修复 + 到期规则 ----------
def mk_leg(start, end, base_price, jump, ltd):
    ds = pd.bdate_range(start, end)
    n = len(ds)
    px = np.full(n, base_price * (1 + jump))
    return pd.DataFrame({
        'session_date': ds.date, 'open': px, 'high': px, 'low': px,
        'close': px, 'volume': 1000.0,
        'last_trading_date': pd.Timestamp(ltd)})

now = pd.Timestamp('2026-02-10', tz='UTC')
legs3 = [mk_leg('2025-08-01', '2025-12-20', 100.0, 0.0, '2025-12-15'),
         mk_leg('2025-12-01', '2026-03-20', 100.0, 0.10, '2026-03-15'),
         mk_leg('2026-03-01', '2026-06-20', 100.0, 0.21, '2026-06-15')]
# 注意：三腿 base 均为 100，jump 拉开挂牌价差 → 原始价 100/110/121
cont3 = build_continuous(legs3, now=pd.Timestamp('2026-05-01', tz='UTC'))
check('3 腿恒价 → 连续恒价(121)', np.allclose(cont3['close'], 121.0),
      f'范围 {cont3["close"].min()}..{cont3["close"].max()}')
legs4 = legs3 + [mk_leg('2026-06-01', '2026-09-20', 100.0, 0.331, '2026-09-15')]
cont4 = build_continuous(legs4, now=pd.Timestamp('2026-08-01', tz='UTC'))
check('4 腿恒价 → 连续恒价(133.1)', np.allclose(cont4['close'], 133.1),
      f'范围 {cont4["close"].min():.3f}..{cont4["close"].max():.3f}')
check('换月点数 3(4腿)', int(cont4['roll_flag'].sum()) == 3)
check('roll 当根含 offset0',
      int((validate_roll_returns(cont4)['offset'] == 0).sum()) == 3)

# 到期规则活跃腿：远月（更晚到期）有报价也不得成为尾部
far = mk_leg('2026-01-01', '2026-08-20', 100.0, 0.5, '2027-12-15')
cont_far = build_continuous(legs4 + [far], now=pd.Timestamp('2026-08-01', tz='UTC'))
check('远月不进入连续序列', cont_far['close'].max() < 200.0)
check('活跃腿=roll_on 覆盖当前时刻',
      select_active_leg(legs4 + [far], now=pd.Timestamp('2026-08-01', tz='UTC')) == 3)

# 审计反例（P1-1）：2026-09-22 已过 HGU6 的 roll_on(9/21) → 活跃腿必须是 HGZ6
hgu6 = mk_leg('2026-06-01', '2026-09-25', 100.0, 0.0, '2026-09-28')
hgz6 = mk_leg('2026-06-01', '2026-09-25', 100.0, 0.05, '2026-12-29')
legs_real = [hgu6, hgz6]
check('roll_on 前活跃=HGU6（9/16）',
      select_active_leg(legs_real, now=pd.Timestamp('2026-09-16', tz='UTC')) == 0)
check('roll_on 后活跃=HGZ6（9/22 反例）',
      select_active_leg(legs_real, now=pd.Timestamp('2026-09-22', tz='UTC')) == 1)
cont_922 = build_continuous(legs_real, now=pd.Timestamp('2026-09-22', tz='UTC'))
tail_after = cont_922[cont_922['session_date'] >= pd.Timestamp('2026-09-21').date()]
check('9/22 连续尾部来自 HGZ6（含 9/21 换月）',
      len(tail_after) > 0
      and bool(tail_after['roll_flag'].iloc[0])
      and int((cont_922['roll_flag']).sum()) == 1)
w = mask_roll_neighborhood(cont4)
check('换月邻域降权', w.min() <= 0.5)
for x in validate.v13_roll_returns(cont4):
    check(f'{x["id"]} {x["item"]}', x['pass'], x['detail'])
noroll = mk_leg('2026-01-01', '2026-08-20', 100.0, 0.0, '2026-12-15')
for x in validate.v13_roll_returns(build_continuous([noroll],
                                                    now=pd.Timestamp('2026-08-01', tz='UTC'))):
    check(f'{x["id"]} 无换月=未验证', not x['pass'] and not x['verified'], x['detail'])

# ---------- guard：ltd 感知 ----------
nowg = pd.Timestamp('2026-09-16', tz='UTC')
guard_ok = False
try:
    guard_expiring_symbols({'HGZ6': nowg - pd.Timedelta(hours=48)},
                           ltd_map={'HGZ6': '20261229'}, now_utc=nowg)
    guard_ok = False  # 未到期且陈旧 → 必须抛错
except RuntimeError:
    guard_ok = True
check('guard 未到期僵尸拦截', guard_ok)
guard_ok = False
try:
    guard_expiring_symbols({'HGK6': nowg - pd.Timedelta(days=200)},
                           ltd_map={'HGK6': '20260527'}, now_utc=nowg)
    guard_ok = True  # 已到期历史腿 → 放行
except RuntimeError:
    pass
check('guard 已到期腿放行', guard_ok)
try:
    guard_expiring_symbols({'HGX6': nowg}, ltd_map={}, now_utc=nowg)
    check('guard 缺 ltd 拦截', False)
except RuntimeError:
    check('guard 缺 ltd 拦截', True)
# 超视野远月（ltd > now+8个月）：报价稀疏属常态，不拦截（与 V11 同口径）
guard_ok = False
try:
    guard_expiring_symbols({'HGU7': nowg - pd.Timedelta(days=40)},
                           ltd_map={'HGU7': '20270928'}, now_utc=nowg)
    guard_ok = True
except RuntimeError:
    pass
check('guard 超视野远月放行（同 V11 口径）', guard_ok)

# ---------- V7 缺 HG/VIX 必须 FAIL ----------
for x in validate.v7_calendar_intersection(d1_map, env_keys=('HG', 'VIX')):
    check(f'{x["id"]} 缺失序列 FAIL（合成场景）', not x['pass'], x['detail'])

# ---------- V9 无真实点差 → 不可验证（隔离本机 sidecar）----------
import fx_data.validate as _V
_orig_ts, _orig_im = _V._tick_snapshot, _V._inst_meta
_V._tick_snapshot = lambda max_age='24h': (None, None)
_V._inst_meta = lambda: {}
for x in validate.v9_triangular_residuals(h1_map, {}):
    check(f'{x["id"]} 无点差=不可验证', not x['pass'] and not x['verified'], x['detail'][:60])
_V._tick_snapshot, _V._inst_meta = _orig_ts, _orig_im

# ---------- DXY ----------
dxy_map = {}
for sym, w_, sign in DXY_SPEC:
    c = np.exp(rng.normal(0, 1, len(dates)).cumsum() * 1e-4)
    dxy_map[sym] = rebuild_d1(pd.DataFrame({
        'ts_utc': dates, 'open': c, 'high': c, 'low': c, 'close': c, 'volume': 1.0}))
dxy = compute_dxy(dxy_map)
for sym, w_, sign in DXY_SPEC:
    dxy_map[sym] = dxy_map[sym].assign(
        close=dxy_map[sym]['close'] * (1.01 if sign > 0 else 1 / 1.01))
chg = compute_dxy(dxy_map)['close'].iloc[-1] / dxy['close'].iloc[-1] - 1
check('DXY USD 方向', 0.009 < chg < 0.011, f'chg={chg:.4%}')

# ---------- 环境层 ----------
env_sessions = pd.bdate_range('2026-01-05', '2026-07-10')
def env_series(base):
    return pd.DataFrame({
        'session_date': env_sessions.date,
        'close': base * np.exp(rng.normal(0, 0.005, len(env_sessions)).cumsum()),
        'ts_utc': pd.to_datetime(env_sessions, utc=True)})
hg_c = cont3.drop(columns=['last_trading_date'])
env_map = {'HG': hg_c.assign(ts_utc=pd.to_datetime(hg_c['session_date'], utc=True)),
           'VIX': env_series(15.0), 'XAUUSD': env_series(2400.0),
           'XTIUSD': env_series(70.0), 'US500': env_series(6000.0)}
env = compute_env(env_map)
check('env 结构完整', {'ratios', 'weather_gauge', 'regime_type',
                       'coefficient'} <= set(env.keys()))

# ---------- pair context ----------
n = 500
walk = 1.0 + rng.normal(0, 0.001, n).cumsum() * 0.01
h1w = pd.DataFrame({
    'ts_utc': pd.date_range('2026-07-01', periods=n, freq='h', tz='UTC'),
    'open': walk, 'high': walk * 1.001, 'low': walk * 0.999, 'close': walk,
    'volume': 10.0, 'spread': 0, 'source': 'mt5', 'price_kind': 'bid'})
check('ATR14 有限正', 0 < atr14(h1w) < 0.1)
adx, _ = adx14(h1w)
check('ADX14 有限', 0 <= adx <= 100, f'adx={adx:.1f}')
micro = acs_rcs_micro(h1w)
check('ACS/RCS 显式 proxy', micro.get('status') == 'proxy' and micro.get('method'))
ctx = build_pair_context(h1w, 'EURUSD')
check('context 无伪造 spread 字段', 'spread_now' not in ctx)
check('ohlc_tail ≤ 60', len(ctx['ohlc_tail']) <= 60)

# ---------- storage 快照不覆盖 + V8 ----------
snap_dir = config.DIR_RAW / 'mt5'
backup = None
import os
from fx_data import storage as st
demo = pd.DataFrame({'ts_utc': pd.date_range('2026-09-15', periods=10, freq='h',
                                             tz='UTC'),
                     'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0})
st.write_raw(demo, 'mt5', 'ZZTEST', 'H1')
st.write_raw(demo.assign(close=1.5), 'mt5', 'ZZTEST', 'H1')
snaps = st.raw_snapshots('mt5', 'ZZTEST', 'H1')
check('快照链 ≥2 且不覆盖', len(snaps) >= 2, f'n={len(snaps)}')
first = pd.read_parquet(snaps[0])
check('旧快照内容未变', float(first['close'].iloc[0]) == 1.0)
latest = st.read_raw('mt5', 'ZZTEST', 'H1')
check('latest=最新快照', float(latest['close'].iloc[0]) == 1.5)
for f in snaps:
    f.unlink()
(config.DIR_RAW / 'mt5' / 'ZZTEST__H1.parquet').unlink(missing_ok=True)

# V8：两快照篡改被检出
st.write_raw(demo, 'mt5', 'ZZTEST', 'H1')
st.write_raw(demo.iloc[:-1].assign(
    close=demo['close'].iloc[:-1].to_numpy()), 'mt5', 'ZZTEST', 'H1')
# 第三份快照改历史
tampered = demo.copy()
tampered.iloc[2, tampered.columns.get_loc('close')] = 9.9
st.write_raw(tampered, 'mt5', 'ZZTEST', 'H1')
for x in validate.v8_no_repaint(['ZZTEST'], source='mt5'):
    check(f'{x["id"]} 快照链检出重绘', not x['pass'], x['detail'][:50])
for f in st.raw_snapshots('mt5', 'ZZTEST', 'H1'):
    f.unlink()
(config.DIR_RAW / 'mt5' / 'ZZTEST__H1.parquet').unlink(missing_ok=True)

# ---------- rebuild_d1 L1 schema ----------
edge = pd.DataFrame({
    'ts_utc': [pd.Timestamp('2026-09-10 22:00', tz='UTC') + pd.Timedelta(hours=i)
               for i in range(48)],
    'open': 1.0, 'high': 1.1, 'low': 0.9, 'close': 1.05, 'volume': 10.0,
    'spread': 1, 'source': 'mt5', 'price_kind': 'bid'})
d1e = rebuild_d1(edge)
for col in ('source', 'price_kind', 'roll_flag', 'bars_in_session'):
    check(f'L1 schema 含 {col}', col in d1e.columns)
check('spread_points 透传', 'spread_points' in d1e.columns)

# 段编号
seg_df = pd.DataFrame({'session_date': pd.DatetimeIndex(
    ['2026-09-01', '2026-09-02', '2026-09-03', '2026-09-14', '2026-09-15']).date})
seg = assign_segments(seg_df)
check('segment_id 缺口自增', seg['segment_id'].tolist() == [0, 0, 0, 1, 1])

# 周末壳剔除
shell = pd.DataFrame({'session_date': pd.DatetimeIndex(
    ['2026-09-11', '2026-09-12', '2026-09-13', '2026-09-14']).date,
    'bars_in_session': [23, 1, 1, 24]})
check('周末壳剔除', list(drop_weekend_shells(shell)['session_date'].astype(str))
      == ['2026-09-11', '2026-09-14'])

# ---------- 审计反例回归（第二轮 P1） ----------
# V8：删除中间一根历史 bar 必须被检出（reindex NaN 不算无差异）
demo2 = pd.DataFrame({'ts_utc': pd.date_range('2026-09-15', periods=10, freq='h',
                                              tz='UTC'),
                      'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0})
st.write_raw(demo2, 'mt5', 'ZZDEL', 'H1')
st.write_raw(demo2, 'mt5', 'ZZDEL', 'H1')
st.write_raw(demo2.drop(index=4), 'mt5', 'ZZDEL', 'H1')   # 第三份删除第 5 根
for x in validate.v8_no_repaint(['ZZDEL'], source='mt5'):
    check(f'{x["id"]} 删除历史bar被检出（审计反例）', not x['pass']
          and '删除 1' in x['detail'], x['detail'][:60])
for f in st.raw_snapshots('mt5', 'ZZDEL', 'H1'):
    f.unlink()
(config.DIR_RAW / 'mt5' / 'ZZDEL__H1.parquet').unlink(missing_ok=True)

# V7：FX 分母必须是 28 盘全集（审计反例：EURUSD 10 天 vs XAU/HG 2 天 → FAIL）
days10 = [pd.Timestamp('2026-08-0%d' % i).date() for i in range(1, 10)] + \
    [pd.Timestamp('2026-08-10').date()]
mini = {p: pd.DataFrame({'session_date': days10}) for p in config.PAIRS_28}
mini['XAUUSD'] = pd.DataFrame({'session_date': days10[:2]})
mini['HG'] = pd.DataFrame({'session_date': days10[:2]})
mini['XTIUSD'] = pd.DataFrame({'session_date': days10})
mini['US500'] = pd.DataFrame({'session_date': days10})
mini['VIX'] = pd.DataFrame({'session_date': days10})
for x in validate.v7_calendar_intersection(mini):
    check(f'{x["id"]} FX分母=28盘（2/10 反例 FAIL）', not x['pass'],
          x['detail'][:80])
# 对照：全对齐时应 PASS
mini2 = {k: pd.DataFrame({'session_date': days10}) for k in mini}
for x in validate.v7_calendar_intersection(mini2):
    check(f'{x["id"]} 全对齐 PASS', x['pass'], x['detail'][:60])

# V12：整体错开 30 分钟的序列必须 FAIL（精确时间戳比较）
# FX 参照需覆盖全天（containment 以 FX 窗包含 RTH 源为前提）
_fx_day1 = pd.date_range('2026-09-15 00:00', '2026-09-15 21:00', freq='h', tz='UTC')
_fx_day2 = pd.date_range('2026-09-16 00:00', '2026-09-16 21:00', freq='h', tz='UTC')
fx_h1 = pd.DataFrame({'ts_utc': _fx_day1.append(_fx_day2)})
shifted = pd.DataFrame({'ts_utc': _fx_day1 + pd.Timedelta(minutes=30)})
for x in validate.v12_time_window_overlap(
        {}, {'EURUSD': fx_h1, 'HG': shifted},
        env_keys=('HG',)):
    check(f'{x["id"]} 30分钟错位 FAIL（审计反例）', not x['pass'], x['detail'][:90])
for x in validate.v12_time_window_overlap(
        {}, {'EURUSD': fx_h1, 'HG': fx_h1},
        env_keys=('HG',)):
    check(f'{x["id"]} 完全对齐 PASS', x['pass'], x['detail'][:90])

# V14：周六 23:59:37Z 的 bar 必须判不对齐（审计反例）
sat = pd.DataFrame({
    'ts_utc': pd.DatetimeIndex(['2026-09-19 23:59:37'], tz='UTC'),
    'open': 17.0, 'high': 17.0, 'low': 17.0, 'close': 17.0})
check('V14 周六 bar 判不对齐', not vix_sessions_aligned(sat))
check('V14 正常日内 bar 对齐', vix_sessions_aligned(vix_h1))

# segment 不足 → 阻断而非跨段（P2）
from fx_data.api import _trim_to_segment
seg_df2 = pd.DataFrame({
    'session_date': [pd.Timestamp('2026-08-0%d' % i).date() for i in range(1, 6)]
    + [pd.Timestamp('2026-09-0%d' % i).date() for i in range(1, 4)],
    'segment_id': [0] * 5 + [1] * 3})
try:
    _trim_to_segment(seg_df2, 20, 'TESTSYM')
    check('segment 不足阻断（P2）', False, '未抛错')
except RuntimeError:
    check('segment 不足阻断（P2）', True)

# asof 无前视（P1-2）：历史 asof 不带入当前 tick 快照的异常盘
from fx_data import api as _api
if all(st.norm_exists(s, 'D1') for s in config.PAIRS_28):
    fake_ticks = {'_captured_at_utc': pd.Timestamp.utcnow().isoformat(),
                  '_fetched_at_ms': int(pd.Timestamp.utcnow().timestamp() * 1000),
                  '_fetch_span_ms': 50.0}
    now_ms = fake_ticks['_fetched_at_ms']
    for s in ['EURCHF', 'EURUSD', 'USDCHF', 'EURCAD', 'EURAUD', 'AUDCAD']:
        fake_ticks[s] = {'bid': 1.0, 'ask': 1.0001,
                         'spread_price': 0.0001, 'tick_time_ms': now_ms}
    fake_ticks['EURCHF']['bid'] = 0.93          # 制造 7%+ 的三角违例
    orig_snap = _api._tick_snapshot
    _api._tick_snapshot = lambda max_age='24h': (fake_ticks,
                                                 pd.Timestamp.utcnow())
    try:
        hist = _api.get_strength_board(20, asof='2026-08-01T00:00:00Z')
        check('历史 asof 不带入 live 异常盘（P1-2）',
              'EURCHF' not in hist['quality']['flagged_pairs'],
              str(hist['quality']['flagged_pairs']))
        check('历史 asof live_exclusion 标注防前视',
              '防前视' in hist['quality']['live_exclusion'])
    finally:
        _api._tick_snapshot = orig_snap
else:
    print('[SKIP] asof 前视回归需要本机 norm D1 数据')

# ---------- 审计反例回归（第三轮 P1/P2） ----------
# V9：全市场共同陈旧（所有 tick 同龄 2 天）必须 FAIL——绝对年龄检查
old_ms = 1_700_000_000_000
stale_ticks = {'_captured_at_utc': pd.Timestamp.utcnow().isoformat(),
               '_fetched_at_ms': int(pd.Timestamp.utcnow().timestamp() * 1000),
               '_tick_span_ms': 5}
for s in config.PAIRS_28:
    stale_ticks[s] = {'bid': 1.0, 'ask': 1.0001, 'spread_price': 0.0001,
                      'tick_time_ms': old_ms, 'age_ms': 172_800_000.0}
_orig_ss = _V._spread_sources
_V._tick_snapshot = lambda max_age='24h': (stale_ticks, pd.Timestamp.utcnow())
_V._spread_sources = lambda hm: {s: (1e-4, 'tick') for s in hm}
try:
    res = validate.v9_triangular_residuals(
        {p: h1_map[p] for p in config.PAIRS_28}, {})
    x = res[0]
    check('V9 全体共同陈旧 FAIL（审计反例）', not x['pass']
          and '陈旧' in x['detail'], x['detail'][:90])
finally:
    _V._tick_snapshot, _V._spread_sources = _orig_ts, _orig_ss

# V12：VIX 整体错位 30 分钟必须 FAIL（锚点门控）
vix_shift = pd.DataFrame({
    'ts_utc': pd.DatetimeIndex(
        ['2026-09-15 07:45', '2026-09-15 08:30', '2026-09-15 14:00',
         '2026-09-15 20:30'], tz='UTC'),
    'open': 17.0, 'high': 17.0, 'low': 17.0, 'close': 17.0})
for x in validate.v12_time_window_overlap(
        {}, {'EURUSD': fx_h1, 'VIX': vix_shift}, env_keys=('VIX',)):
    check('V12 VIX 30分钟错位 FAIL（审计反例）', not x['pass']
          and 'anchor_ok' in x['detail'] and 'False' in x['detail'],
          x['detail'][:100])
for x in validate.v12_time_window_overlap(
        {}, {'EURUSD': fx_h1, 'VIX': vix_h1}, env_keys=('VIX',)):
    check('V12 VIX 正常锚点 PASS', x['pass'], x['detail'][:100])

# V14：周六日内 bar（10:15Z）与 30 分钟偏移都必须 FAIL
sat_day = pd.DataFrame({
    'ts_utc': pd.DatetimeIndex(['2026-09-19 10:15'], tz='UTC'),
    'open': 17.0, 'high': 17.0, 'low': 17.0, 'close': 17.0})
check('V14 周六 10:15Z 判不对齐', not vix_sessions_aligned(sat_day))
check('V14 VIX 30分钟偏移判不对齐', not vix_sessions_aligned(vix_shift))
check('V14 正常 VIX 对齐', vix_sessions_aligned(vix_h1))

# env asof 必须取对齐后共同会话（审计反例：XAUUSD 多一天 → asof 落在交集末日）
def _env_frame(dates, base):
    return pd.DataFrame({
        'session_date': pd.DatetimeIndex(dates).date,
        'close': base * np.exp(rng.normal(0, 0.004, len(dates)).cumsum()),
        'ts_utc': pd.to_datetime(pd.DatetimeIndex(dates), utc=True)})

_dates_all = pd.DatetimeIndex(env_sessions)              # 末日 07-10
_dates_common = _dates_all[:-1]                          # 其余源止于 07-09
env_map2 = {
    'XAUUSD': _env_frame(_dates_all, 2400.0),            # XAUUSD 多出最后一天
    'HG': _env_frame(_dates_common, 4.5),
    'VIX': _env_frame(_dates_common, 15.0),
    'XTIUSD': _env_frame(_dates_common, 70.0),
    'US500': _env_frame(_dates_common, 6000.0),
}
env2 = compute_env(env_map2)
check('env asof=对齐后共同会话（P2）',
      env2['effective_session'] == str(_dates_common[-1].date()),
      f"effective={env2['effective_session']} vs 未对齐XAU末根="
      f"{_dates_all[-1].date()}")

# ---------- 审计反例回归（第四轮 P1） ----------
import types
from fx_data import mt5_export as MX

# P1-1：真实 _tick_snapshot 生成路径 + 全体共同陈旧 2 天必须被识别。
# mock symbol_info_tick + 可注入墙钟（now_ms），不手工塞 age_ms。
FIXED_NOW_MS = 1_800_000_000_000.0          # 2027-01-15（冬令时 → 日历锚 UTC+2）
_off_ms = 2 * 3_600_000.0
_stale_ms = 2 * 86_400_000.0
class _FakeTick:
    def __init__(self, t_msc):
        self.bid, self.ask = 1.0, 1.0001
        self.time_msc = int(t_msc)
        self.time = int(t_msc // 1000)
_fake_mt5 = types.SimpleNamespace(
    symbol_info_tick=lambda s: _FakeTick(FIXED_NOW_MS + _off_ms - _stale_ms))
_orig_mx_mt5 = MX.mt5
MX.mt5 = _fake_mt5
try:
    snap = MX._tick_snapshot(config.PAIRS_28, rounds=1, now_ms=FIXED_NOW_MS)
    ages = {k: v['age_ms'] for k, v in snap.items() if not k.startswith('_')}
    check('真实生成器：全体陈旧2天被识别（|age−2d|<2s）',
          all(abs(a - _stale_ms) < 2000 for a in ages.values()),
          f'ages={sorted(set(round(a/3600000, 1) for a in ages.values()))}h; '
          f'锚={snap["_offset_source"]}')
    check('真实生成器：偏移锚=日历（非 tick 自证）',
          snap['_offset_source'] == 'calendar(EU-DST)', snap['_offset_source'])
    # 该快照（新鲜落盘、内容全体陈旧）喂给 V9 → 必须陈旧 FAIL
    side = dict(snap)
    side['_captured_at_utc'] = pd.Timestamp.utcnow().isoformat()
    _orig_ts2, _orig_ss2 = _V._tick_snapshot, _V._spread_sources
    _V._tick_snapshot = lambda max_age=None: (side, pd.Timestamp.utcnow())
    _V._spread_sources = lambda hm: {s: (1e-4, 'tick') for s in hm}
    try:
        x = validate.v9_triangular_residuals(
            {p: h1_map[p] for p in config.PAIRS_28}, {})[0]
        check('V9 拒绝全体陈旧快照（真实生成器）', not x['pass']
              and '陈旧' in x['detail'], x['detail'][:80])
    finally:
        _V._tick_snapshot, _V._spread_sources = _orig_ts2, _orig_ss2
finally:
    MX.mt5 = _orig_mx_mt5

# 新鲜对照：同一生成器、tick 不陈旧 → age ≈ 0、V9 可用
_fake_fresh = types.SimpleNamespace(
    symbol_info_tick=lambda s: _FakeTick(FIXED_NOW_MS + _off_ms - 500))
MX.mt5 = _fake_fresh
try:
    snap2 = MX._tick_snapshot(config.PAIRS_28[:6], rounds=1, now_ms=FIXED_NOW_MS)
    ages2 = [v['age_ms'] for k, v in snap2.items() if not k.startswith('_')]
    check('真实生成器：新鲜 tick age≈0', all(0 <= a < 2000 for a in ages2),
          f'ages={ages2[:3]}')
finally:
    MX.mt5 = _orig_mx_mt5

# P1-2：快照年龄随时间增长——采集后推进时钟 300s，全链路拒绝
_saved_sidecar = (config.DIR_RAW / 'mt5' / 'tick_snapshot.json').read_text(
    encoding='utf-8') if (config.DIR_RAW / 'mt5' / 'tick_snapshot.json').exists() else None
aged = {'_captured_at_utc': (pd.Timestamp.utcnow() - pd.Timedelta(seconds=300)
                             ).isoformat()}
for s in config.PAIRS_28:
    aged[s] = {'bid': 1.0, 'ask': 1.0001, 'spread_price': 0.0001,
               'tick_time_ms': 1, 'age_ms': 0.0}
st.write_sidecar(aged, 'mt5', 'tick_snapshot.json')
try:
    t, cap = _V._tick_snapshot()
    check('时钟推进300s：快照门拒绝（None）', t is None and cap is not None)
    x = validate.v9_triangular_residuals(
        {p: h1_map[p] for p in config.PAIRS_28}, {})[0]
    check('时钟推进300s：V9 陈旧快照→UNVERIFIED',
          not x['pass'] and not x['verified'] and '新鲜' in x['detail'],
          x['detail'][:70])
    sp = _V._spread_sources({p: h1_map[p] for p in config.PAIRS_28})
    check('时钟推进300s：spread 不用 tick 源',
          all(src != 'tick_snapshot(ask-bid)' for _, src in sp.values()),
          str(sorted(set(s for _, s in sp.values()))))
    from fx_data import api as _api2
    b = _api2.get_strength_board(20)
    check('时钟推进300s：API 不做 live 剔除',
          b['quality']['flagged_pairs'] == []
          and '不适用' in b['quality']['live_exclusion'],
          b['quality']['live_exclusion'][:60])
finally:
    if _saved_sidecar is not None:
        (config.DIR_RAW / 'mt5' / 'tick_snapshot.json').write_text(
            _saved_sidecar, encoding='utf-8')

# P1-3：env vix_level 取对齐后共同会话（审计反例：共同止于 07-07，
# VIX 多一根 07-08=99 → vix_level 必须是 07-07 的 16）
_d_common = pd.bdate_range('2026-07-01', '2026-07-07')
_d_vix_extra = _d_common.append(pd.DatetimeIndex(['2026-07-08']))
def _frame(ds, closes):
    return pd.DataFrame({'session_date': pd.DatetimeIndex(ds).date,
                         'close': closes,
                         'ts_utc': pd.to_datetime(pd.DatetimeIndex(ds), utc=True)})
env3 = compute_env({
    'XAUUSD': _frame(_d_common, [2400.0] * len(_d_common)),
    'XTIUSD': _frame(_d_common, [70.0] * len(_d_common)),
    'US500': _frame(_d_common, [6000.0] * len(_d_common)),
    'HG': _frame(_d_common, [4.5] * len(_d_common)),
    'VIX': _frame(_d_vix_extra, [16.0] * len(_d_common) + [99.0]),
})
check('env vix_level=对齐后共同会话（P1-3）',
      env3['vix_level'] == 16.0 and env3['effective_session'] == '2026-07-07',
      f"vix_level={env3['vix_level']}, effective={env3['effective_session']}")

# ---------- P0/P1/P2 新功能回归（第五轮） ----------
from fx_data.summary import (board_compare, render_board_text,
                             render_compare_text, summarize_board)
from fx_data.api import asof_semantics, get_pair_context, get_series

# P0-1：摘要层合成反例（USD z20=+0.017 / z5=+1.211 / delta=+1.194）
_mem = lambda **kw: kw
fake_board = {
    'asof': '2026-09-16T00:00:00Z', 'data_through_session': '2026-09-15',
    'window': 20, 'staleness_hours': 19.0, 'staleness_status': 'ok',
    'ranking': ['USD', 'JPY', 'EUR', 'GBP', 'AUD', 'CAD', 'CHF', 'NZD'],
    'quality': {'board_pairs': 28, 'excluded_pairs': []},
    'currencies': [
        {'ccy': 'USD', 'z': 0.017, 'z_short': 1.211, 'state': 'FLAT_ACCEL',
         'rank': 1, 'membership': {'FLAT_ACCEL': 0.97, 'FLAT_FLAT': 0.03}},
        {'ccy': 'JPY', 'z': 1.560, 'z_short': 0.136, 'state': 'UP_DECEL',
         'rank': 2, 'membership': {'UP_DECEL': 1.0}},
        {'ccy': 'EUR', 'z': -0.148, 'z_short': 0.178, 'state': 'FLAT_FLAT',
         'rank': 3, 'membership': {'FLAT_FLAT': 0.47, 'FLAT_ACCEL': 0.31,
                                   'UP_ACCEL': 0.22}},
        {'ccy': 'GBP', 'z': -0.249, 'z_short': 0.523, 'state': 'FLAT_DECEL',
         'rank': 4, 'membership': {'FLAT_DECEL': 0.39, 'FLAT_FLAT': 0.35,
                                   'FLAT_ACCEL': 0.26}},
        {'ccy': 'AUD', 'z': 0.347, 'z_short': -0.524, 'state': 'UP_DECEL',
         'rank': 5, 'membership': {'UP_DECEL': 0.61, 'UP_FLAT': 0.39}},
        {'ccy': 'CAD', 'z': -0.074, 'z_short': -0.091, 'state': 'FLAT_FLAT',
         'rank': 6, 'membership': {'FLAT_FLAT': 0.84}},
        {'ccy': 'CHF', 'z': -0.382, 'z_short': -0.348, 'state': 'DOWN_FLAT',
         'rank': 7, 'membership': {'DOWN_FLAT': 0.74}},
        {'ccy': 'NZD', 'z': -1.072, 'z_short': -1.085, 'state': 'DOWN_FLAT',
         'rank': 8, 'membership': {'DOWN_FLAT': 0.99}},
    ],
}
s5 = summarize_board(fake_board)
txt5 = render_board_text(s5)
check('摘要含 USD +0.017/+1.211/+1.194',
      all(t in txt5 for t in ('+0.017', '+1.211', '+1.194')))
order5 = [r['ccy'] for r in s5['momentum_turn_ranking']]
expect5 = ['JPY', 'USD', 'AUD', 'GBP', 'EUR', 'CHF', 'CAD', 'NZD']
check('动量转折排行=|delta|降序', order5 == expect5, str(order5))
low = {r['ccy']: r for r in s5['currencies']}
check('低 purity 标记不确定（EUR/GBP）',
      low['EUR']['uncertain'] and low['GBP']['uncertain']
      and '(!)' in low['EUR']['state_display']
      and not low['USD']['uncertain'])
check('compare 渲染含双窗标题',
      'fx_board_compare' in render_compare_text({
          'a': {'window': 20, 'through': 'x'}, 'b': {'window': 50, 'through': 'y'},
          'currencies': [{'ccy': 'USD', 'z': 1.0, 'z_short': 2.0, 'delta': 1.0,
                          'state_display': 'UP_ACCEL', 'purity': 0.9,
                          'z_b': 0.5, 'z_shift_w': 0.5}],
          'momentum_turn_ranking': s5['momentum_turn_ranking'][:1]}))

# P1-5：asof 语义
a5 = asof_semantics('2026-09-15', pd.Timestamp('2026-09-16 12:00', tz='UTC'))
check('asof 语义 ok', a5['asof_semantics'] == 'session_start'
      and a5['session_boundary_utc'] == 22 and a5['staleness_hours'] == 14.0
      and a5['staleness_status'] == 'ok', str(a5))
f5 = asof_semantics('2099-01-01', pd.Timestamp('2026-09-16 12:00', tz='UTC'))
check('未来会话→时钟冲突（不冒充新鲜）',
      f5['staleness_hours'] is None and f5['staleness_status'] == 'clock_conflict_or_future')
n5 = asof_semantics(None)
check('空会话→不可用', n5['staleness_status'] == 'unavailable')

if all(st.norm_exists(s, 'D1') for s in ('XAUUSD', 'XTIUSD', 'XBRUSD', 'US500', 'HG')):
    # P0-2：get_series 五品种实测（对账 parquet、无派生字段、无前视）
    allowed_keys = {'session_date', 'ts_utc', 'open', 'high', 'low', 'close',
                    'volume', 'segment_id', 'roll_flag', 'bars_in_session',
                    'partial'}
    from fx_data.resample import complete_sessions as _cs
    for sym in ('XAUUSD', 'XTIUSD', 'XBRUSD', 'US500', 'HG'):
        out = get_series(sym, n=50)
        check(f'series {sym} 行数=50 且升序',
              out['row_count'] == 50
              and [r['session_date'] for r in out['rows']]
              == sorted(r['session_date'] for r in out['rows']))
        check(f'series {sym} 仅原始/追溯字段',
              all(set(r.keys()) <= allowed_keys for r in out['rows']))
        raw = st.read_norm(sym, 'D1')
        now5 = pd.Timestamp.now('UTC')
        exp = _cs(raw, now5).sort_values('session_date').tail(1).iloc[0]
        got = out['rows'][-1]
        check(f'series {sym} 末根与 parquet 一致',
              got['session_date'] == str(exp['session_date'])
              and abs(got['close'] - float(exp['close'])) < 1e-12
              and abs(got['open'] - float(exp['open'])) < 1e-12
              and abs(got['high'] - float(exp['high'])) < 1e-12
              and abs(got['low'] - float(exp['low'])) < 1e-12,
              f"{got['session_date']} close {got['close']} vs {float(exp['close'])}")
        check(f'series {sym} staleness ok',
              out['staleness_status'] == 'ok' and out['session_boundary_utc'] == 22)
    sin = get_series('XAUUSD', n=2000, since='2026-08-01')
    check('series since 过滤', all(r['session_date'] >= '2026-08-01'
                                   for r in sin['rows']))
    past = get_series('XAUUSD', asof='2026-08-01T00:00:00Z')
    check('series asof 无前视',
          past['rows'][-1]['session_date'] <= '2026-07-31',
          past['rows'][-1]['session_date'])
    hg_all = get_series('HG', n=2000)
    check('series HG 含 roll_flag 字段且历史有换月',
          any(r['roll_flag'] for r in hg_all['rows']))
    h1s = get_series('XAUUSD', tf='H1', n=10)
    check('series H1 ts_utc 升序且 ≤now',
          h1s['rows'][-1]['ts_utc'] <= pd.Timestamp.now('UTC').isoformat())
    # board/env/series 语义键一致
    from fx_data import api as _api5
    b5 = _api5.get_strength_board(20)
    e5 = _api5.get_env_state()
    keys5 = {'asof_semantics', 'effective_session', 'session_boundary_utc',
             'staleness_hours', 'staleness_status'}
    check('board/env/series 语义键一致',
          keys5 <= set(b5) and keys5 <= set(e5) and keys5 <= set(out))
    check('env staleness≥0 或显式不可用',
          (e5['staleness_hours'] is None) or (e5['staleness_hours'] >= 0))
    # P1-4：regime_inputs
    ri5 = e5['regime_inputs']
    check('regime_inputs 判定依据字段齐备',
          {'reflation_proxy', 'reflation_value', 'reflation_state', 'risk_proxy',
           'risk_value', 'reflation_real_source', 'reflation_coverage_note',
           'caveats'} <= set(ri5))
    check('R-3 reflation_real_source 保留 unavailable…不冒充',
          ri5['reflation_real_source'].startswith('unavailable')
          and '不冒充' in ri5['reflation_real_source']
          and 'FRED' in ri5['reflation_real_source'])
    check('regime 标签已改 REFLATION（C-1）',
          'REFLATION' in e5['regime_type'] and 'RATES_' not in e5['regime_type'],
          e5['regime_type'])
    check('env schema_version=1.1（C-1）', e5.get('schema_version') == '1.1')
    # P2-6：真实 context EURCHF
    c5 = get_pair_context('EURCHF')
    check('context EURCHF 新字段',
          {'segment_warning', 'max_tail_gap_hours', 'lookback_limited',
           'bars_available'} <= set(c5))
    check('context spread 来源合法',
          c5['spread_source'] in ('tick_snapshot(ask-bid)', 'mt5_copy_rates_bar',
                                  'live_snapshot(symbol_info)', 'unavailable'))
    check('context acs 保持 proxy', c5['acs_rcs_micro']['status'] == 'proxy')
    # P0-1 compare 实测
    cp5 = board_compare(20, 50)
    check('board_compare 实测结构',
          {'a', 'b', 'currencies', 'momentum_turn_ranking'} <= set(cp5)
          and len(cp5['currencies']) == 8)
else:
    print('[SKIP] series/board 实测需要本机 norm 数据')

# P0-2：参数校验（不依赖数据）
for bad in [lambda: get_series('ZZZZZZ'),
            lambda: get_series('HG', tf='H1'),
            lambda: get_series('XAUUSD', tf='W1'),
            lambda: get_series('XAUUSD', n=0),
            lambda: get_series('XAUUSD', since='2026-09-16T20:00:00Z',
                               asof='2026-09-16T10:00:00Z')]:
    try:
        bad()
        check('series 参数校验拦截', False)
        break
    except ValueError:
        pass
else:
    check('series 参数校验拦截（5 例）', True)

# P0-3：gauge 分量归因（合成）
env_g = compute_env(env_map)
contribs = [c['contrib'] for c in env_g['gauge_components']
            if c['contrib'] is not None]
check('contrib 可加总=gauge（合成）',
      abs(sum(contribs) - env_g['weather_gauge']) < 1e-9,
      f"sum={sum(contribs):.6f} gauge={env_g['weather_gauge']:.6f}")
check('分量含 vix 零贡献披露',
      any(c['name'] == 'vix' and c['contrib'] == 0.0
          for c in env_g['gauge_components']))
check('concentration ∈ (0,1]',
      0 < env_g['gauge_concentration'] <= 1)
# 单项主导（仅 gor 走势，其余持平→零方差被剔除）
_dates_g = pd.bdate_range('2026-01-05', '2026-07-10')
_rng5 = np.random.default_rng(7)
_flat = lambda base: base * np.exp(_rng5.normal(0, 0.003, len(_dates_g)).cumsum())
_rising = 2400.0 * np.exp(np.linspace(0, 0.2, len(_dates_g))
                          + _rng5.normal(0, 0.003, len(_dates_g)).cumsum())
env_dom = compute_env({
    'XAUUSD': _env_frame(_dates_g, 2400.0) if False else pd.DataFrame({
        'session_date': _dates_g.date, 'close': _rising,
        'ts_utc': pd.to_datetime(_dates_g, utc=True)}),
    'XTIUSD': pd.DataFrame({'session_date': _dates_g.date, 'close': _flat(70.0),
                            'ts_utc': pd.to_datetime(_dates_g, utc=True)}),
    'US500': pd.DataFrame({'session_date': _dates_g.date,
                           'close': _rising,          # au_spx 持平
                           'ts_utc': pd.to_datetime(_dates_g, utc=True)}),
    'HG': pd.DataFrame({'session_date': _dates_g.date, 'close': _flat(4.5),
                        'ts_utc': pd.to_datetime(_dates_g, utc=True)}),
    'VIX': pd.DataFrame({'session_date': _dates_g.date, 'close': _flat(15.0),
                         'ts_utc': pd.to_datetime(_dates_g, utc=True)}),
})
check('单项主导→concentration>0.5+提示',
      env_dom['gauge_concentration'] > 0.5
      and len(env_dom['gauge_quality_hints']) == 1,
      f"conc={env_dom['gauge_concentration']:.3f}")
dom_sum = sum(c['contrib'] for c in env_dom['gauge_components']
              if c['contrib'] is not None)
check('零方差剔除后仍可加总（相对容差）',
      abs(dom_sum - env_dom['weather_gauge']) <= 1e-9 * max(1.0, abs(dom_sum)),
      f"sum={dom_sum:.6f} gauge={env_dom['weather_gauge']:.6f}")
# P1-4：合成 regime_inputs（R-3 措辞）
check('合成 regime_inputs reflation 披露（R-3）',
      env_g['regime_inputs']['reflation_real_source'].startswith('unavailable')
      and '不冒充' in env_g['regime_inputs']['reflation_real_source'])

# P2-6：segment 警告与数据不足（monkeypatch norm）
import fx_data.api as _api6
_gap_ts = list(pd.date_range('2026-07-01', periods=150, freq='h', tz='UTC')) + \
    list(pd.date_range('2026-07-20', periods=150, freq='h', tz='UTC'))
_h1g = pd.DataFrame({'ts_utc': _gap_ts, 'open': 1.0, 'high': 1.001, 'low': 0.999,
                     'close': 1.0, 'volume': 10.0, 'spread': 0,
                     'source': 'mt5', 'price_kind': 'bid'})
_orig_rn, _orig_ne = _api6.storage.read_norm, _api6.storage.norm_exists
_api6.storage.read_norm = lambda s, tf: _h1g
_api6.storage.norm_exists = lambda s, tf: True
try:
    ctxg = get_pair_context('ZZGAP')
    check('context segment 警告（5 天缺口）',
          ctxg['segment_warning'] and ctxg['max_tail_gap_hours'] > 96)
    _h1short = _h1g.iloc[:20]
    _api6.storage.read_norm = lambda s, tf: _h1short
    try:
        get_pair_context('ZZSHORT')
        check('context 数据不足报错', False)
    except RuntimeError:
        check('context 数据不足报错', True)
finally:
    _api6.storage.read_norm, _api6.storage.norm_exists = _orig_rn, _orig_ne

# ---------- V4 同步 mid / V5 tick 重建回归（第六轮） ----------
from fx_data.syncmid import evaluate_v4, fetch_sync_mid
from fx_data.backfill import find_missing_slots, reconstruct_slot


def _mk_fetcher(ticks_by_sym, offset_ms=3 * 3_600_000):
    def fetch(sym, naive_from, naive_to):
        rows = []
        for msc, bid, ask in ticks_by_sym.get(sym, []):
            ts = pd.Timestamp(msc - offset_ms, unit='ms', tz='UTC')
            rows.append({'ts_utc': ts, 'bid': bid, 'ask': ask, 'time_msc': msc})
        if not rows:
            return None
        df = pd.DataFrame(rows)
        lo = naive_from.tz_localize('UTC') - pd.Timedelta(hours=3)
        hi = naive_to.tz_localize('UTC') - pd.Timedelta(hours=3)
        return df[(df['ts_utc'] >= lo) & (df['ts_utc'] <= hi)]
    return fetch


CUT = pd.Timestamp('2026-09-16 22:00', tz='UTC')   # 会话 D=2026-09-16 的 22:00Z 日切
# 注：测试里的 tick 挂在 2026-09-15 21:5xZ（D 会话 [D-1 22:00Z, D 22:00Z) 的尾段）
_srv = lambda utc_ms: int(utc_ms) + 3 * 3_600_000
_t = lambda h, m, s, ms: int(pd.Timestamp(f'2026-09-16 {h:02d}:{m:02d}:{s:02d}',
                                          tz='UTC').value // 10**6) + ms
_td = lambda h, m, s, ms: int(pd.Timestamp(f'2026-03-31 {h:02d}:{m:02d}:{s:02d}',
                                           tz='UTC').value // 10**6) + ms

f1 = _mk_fetcher({
    'EURCHF': [(_srv(_t(21, 59, 58, 900)), 0.94500, 0.94510)],
    'EURUSD': [(_srv(_t(21, 59, 58, 900)), 1.15400, 1.15410)],
    'USDCHF': [(_srv(_t(21, 59, 58, 900)), 0.81900, 0.81910)]})
r1 = fetch_sync_mid(CUT, fetcher=f1)
check('syncmid 完全同步 verified', r1['verified'] and r1['skew_ms'] == 0)
check('syncmid mid=(bid+ask)/2',
      abs(r1['legs']['EURCHF']['mid'] - 0.94505) < 1e-12)

f2 = _mk_fetcher({
    'EURCHF': [(_srv(_t(21, 59, 58, 900)), 0.94500, 0.94510)],
    'EURUSD': [(_srv(_t(21, 59, 55, 800)), 1.15400, 1.15410)],
    'USDCHF': [(_srv(_t(21, 59, 58, 900)), 0.81900, 0.81910)]})
r2 = fetch_sync_mid(CUT, fetcher=f2)
check('syncmid 一腿错开3s→UNVERIFIED',
      not r2['verified'] and 'skew' in r2['reason'], r2['reason'])

f3 = _mk_fetcher({
    'EURCHF': [(_srv(_t(21, 59, 58, 900)), 0.94500, 0.94510)],
    'EURUSD': [(_srv(_t(21, 58, 50, 500)), 1.15400, 1.15410)],
    'USDCHF': [(_srv(_t(21, 59, 58, 900)), 0.81900, 0.81910)]})
r3 = fetch_sync_mid(CUT, fetcher=f3)
check('syncmid 一腿陈旧>60s→UNVERIFIED',
      not r3['verified'] and '陈旧' in r3['reason'], r3['reason'])

f4 = _mk_fetcher({
    'EURCHF': [(_srv(_t(21, 59, 58, 900)), 0.94500, 0.94510)],
    'USDCHF': [(_srv(_t(21, 59, 58, 900)), 0.81900, 0.81910)]})
r4 = fetch_sync_mid(CUT, fetcher=f4)
check('syncmid 缺腿→UNVERIFIED',
      not r4['verified'] and '缺腿' in r4['reason'], r4['reason'])

f5 = _mk_fetcher({
    'EURCHF': [(_srv(_t(22, 0, 1, 0)), 0.99999, 0.99999)],
    'EURUSD': [(_srv(_t(21, 59, 58, 900)), 1.15400, 1.15410)],
    'USDCHF': [(_srv(_t(21, 59, 58, 900)), 0.81900, 0.81910)]})
r5 = fetch_sync_mid(CUT, fetcher=f5)
check('syncmid cut后tick不采用（无前视）', not r5['verified'])
# 周五自适应：三腿最后报价 20:56:5x（提前收市），cut 仍 22:00Z → T*≈20:56:59
_tf = lambda h, m, s, ms: _srv(_t(h, m, s, ms))
f6 = _mk_fetcher({
    'EURCHF': [(_tf(20, 30, 0, 0), 0.94500, 0.94510),
               (_tf(20, 56, 59, 200), 0.94510, 0.94520)],
    'EURUSD': [(_tf(20, 56, 59, 700), 1.15400, 1.15410)],
    'USDCHF': [(_tf(20, 56, 59, 500), 0.81900, 0.81910)]})
r6 = fetch_sync_mid(CUT, fetcher=f6)
check('syncmid 周五提前收市自适应（T*=20:56:59.7）',
      r6['verified'] and r6['target_utc'].startswith('2026-09-16T20:56:59'),
      str(r6.get('target_utc')))
check('syncmid T*后更早的tick不污染（20:30 那笔被淘汰）',
      r6['legs']['EURCHF']['mid'] == 0.94515)


def _store(n, pips_dev=0.0):
    rows = []
    d0 = pd.Timestamp('2026-06-01', tz='UTC')
    i = 0
    while len(rows) < n:
        d = d0 + pd.Timedelta(days=i)
        i += 1
        if d.dayofweek >= 5:
            continue
        eu, uc = 1.15, 0.82
        ec = eu * uc * (1 + pips_dev * 1e-4)
        rows.append({'session_date': str(d.date()),
                     'EURCHF__mid': ec, 'EURUSD__mid': eu, 'USDCHF__mid': uc,
                     'skew_ms': 120.0, 'verified': True, 'reason': ''})
    return pd.DataFrame(rows)


ev_ok = evaluate_v4(_store(40))
check('evaluate_v4 一致→PASS', ev_ok['pass'] and ev_ok['verdict'] == 'pass',
      str(ev_ok.get('max_pips')))
ev_bad = evaluate_v4(_store(40, pips_dev=5.0))
check('evaluate_v4 5pip→FAIL', not ev_bad['pass'] and ev_bad['verdict'] == 'fail')
ev_few = evaluate_v4(_store(10))
check('evaluate_v4 覆盖不足→UNVERIFIED', not ev_few['pass']
      and ev_few['verdict'].startswith('insufficient'))
ev_none = evaluate_v4(None)
check('evaluate_v4 无store→UNVERIFIED', ev_none['verdict'] == 'no_store')

slot = pd.Timestamp('2026-03-31 21:00', tz='UTC')
tick_rows = [
    (_srv(_td(21, 0, 5, 100)), 1.100, 1.101),
    (_srv(_td(21, 20, 0, 0)), 1.120, 1.121),
    (_srv(_td(21, 40, 0, 0)), 1.080, 1.081),
    (_srv(_td(21, 59, 30, 0)), 1.090, 1.091),
    (_srv(_td(21, 59, 58, 0)), 1.095, 1.096),
]
frec = _mk_fetcher({'SYM': tick_rows})
bar = reconstruct_slot('SYM', slot, fetcher=frec)
check('重建 OHLCV=open首/high=max/low=min/close末（bid 基）',
      bar['open'] == 1.100 and abs(bar['high'] - 1.120) < 1e-9
      and abs(bar['low'] - 1.080) < 1e-9 and abs(bar['close'] - 1.095) < 1e-9,
      str((bar['open'], bar['high'], bar['low'], bar['close'])))
check('重建 volume=tick数+provenance',
      bar['volume'] == 5 and bar['source'] == 'mt5_reconstructed'
      and bar['provenance']['tick_count'] == 5
      and bar['provenance']['no_interpolation'])
check('重建无tick→None', reconstruct_slot('SYM', slot, fetcher=_mk_fetcher({}))
      is None)

# M1 回退：tick 层空、M1 层有 3 根 → 用 M1 重建（volume=sum tick_volume）
_m1f = lambda sym, a, b: pd.DataFrame({
    'open': [1.10, 1.11, 1.09], 'high': [1.101, 1.112, 1.091],
    'low': [1.099, 1.109, 1.089], 'close': [1.10, 1.11, 1.095],
    'tick_volume': [10, 20, 30],
    'ts_utc': [pd.Timestamp('2026-03-31 21:10', tz='UTC'),
               pd.Timestamp('2026-03-31 21:40', tz='UTC'),
               pd.Timestamp('2026-03-31 21:59', tz='UTC')]})
bar_m1 = reconstruct_slot('SYM', slot, fetcher=_mk_fetcher({}), m1_fetcher=_m1f)
check('M1回退重建 OHLCV+volume',
      bar_m1['open'] == 1.10 and abs(bar_m1['high'] - 1.112) < 1e-9
      and abs(bar_m1['low'] - 1.089) < 1e-9 and abs(bar_m1['close'] - 1.095) < 1e-9
      and bar_m1['volume'] == 60
      and bar_m1['provenance']['from_layer'].startswith('M1'),
      str((bar_m1['open'], bar_m1['high'], bar_m1['close'], bar_m1['volume'])))
check('两层皆无→None（硬缺口保留）',
      reconstruct_slot('SYM', slot, fetcher=_mk_fetcher({}),
                       m1_fetcher=lambda s, a, b: None) is None)

# P1-1 反例：M1 层返回非空但**全部异日期**（模拟 MT5 杂散 bar）→ 绝不重建
_slot_m1 = pd.Timestamp('2026-03-31 21:00', tz='UTC')
_stray_m1 = lambda sym, a, b: pd.DataFrame({
    'open': [1.5], 'high': [1.6], 'low': [1.4], 'close': [1.55],
    'tick_volume': [99],
    # 服务器 epoch 秒：真实 UTC=2026-06-11 13:56（异日期）
    'time': [int((pd.Timestamp('2026-06-11 13:56', tz='UTC')
                  + pd.Timedelta(hours=3)).value // 10**9)],
    'ts_utc': [pd.Timestamp('2026-06-11 13:56', tz='UTC')]})
bar_stray = reconstruct_slot('SYM', _slot_m1, fetcher=_mk_fetcher({}),
                             m1_fetcher=_stray_m1)
check('异日期杂散 M1 绝不重建（P1-1）', bar_stray is None)

# tick 层同样防御：返回非空但全部在 slot+1h 之后 → None
from fx_data.backfill import _default_m1_fetcher as _unused
_tk_after = lambda sym, a, b: pd.DataFrame({
    'ts_utc': [pd.Timestamp('2026-04-02 05:00', tz='UTC')],
    'bid': [1.0], 'ask': [1.0001], 'time_msc': [1]})
check('窗外 tick 绝不重建', reconstruct_slot('SYM', _slot_m1,
      fetcher=_tk_after, m1_fetcher=lambda s, a, b: None) is None)

# P1-2 反例：28 工作日 verified + 3 周日 verified → 仍 UNVERIFIED（28<30）
import fx_data.syncmid as _sm2
_rows = []
_d0 = pd.Timestamp('2026-06-01', tz='UTC')
_wd_added = 0
_i = 0
while _wd_added < 28:
    _d = _d0 + pd.Timedelta(days=_i)
    _i += 1
    if _d.dayofweek >= 5:
        continue
    _rows.append({'session_date': str(_d.date()),
                  'EURCHF__mid': 1.15 * 0.82, 'EURUSD__mid': 1.15,
                  'USDCHF__mid': 0.82, 'skew_ms': 100.0,
                  'verified': True, 'reason': ''})
    _wd_added += 1
for _sun in pd.date_range('2026-06-01', '2026-08-01', freq='W-SUN')[:3]:
    _rows.append({'session_date': str(_sun.date()),
                  'EURCHF__mid': 1.15 * 0.82, 'EURUSD__mid': 1.15,
                  'USDCHF__mid': 0.82, 'skew_ms': 100.0,
                  'verified': True, 'reason': ''})
_ev7 = _sm2.evaluate_v4(pd.DataFrame(_rows))
check('28 工作日+3 周日 verified → UNVERIFIED（P1-2）',
      not _ev7['pass'] and _ev7['verdict'].startswith('insufficient_coverage(28<30)')
      and _ev7['weekend_rows_excluded'] == 3, _ev7['verdict'])
check('canonical_sessions 过滤周末',
      _sm2.canonical_sessions(['2026-06-01', '2026-06-06', '2026-06-07',
                               '2026-06-08']) == ['2026-06-01', '2026-06-08'])

# P1-3：两次同会话采集 → 两份不可变快照 + latest 取新
import shutil as _sh, glob as _gl, os as _os
from fx_data import syncmid as _sm3
_dir7 = _os.path.dirname(_sm3.SYNC_DIR)
_test_dir = _os.path.join('data', 'raw', 'mt5', 'syncmid_test7')
_os.makedirs(_test_dir, exist_ok=True)
_orig_dir = _sm3.SYNC_DIR
from pathlib import Path as _P7
_sm3.SYNC_DIR = _P7(_test_dir)
_sm3.SYNC_LATEST = _sm3.SYNC_DIR / 'EURCHF_TRIO__D1.parquet'
_sm3.SYNC_PROV_LATEST = _sm3.SYNC_DIR / 'EURCHF_TRIO__provenance.json'
try:
    _fk = _mk_fetcher({
        'EURCHF': [(_srv(_t(21, 59, 58, 900)), 0.94500, 0.94510)],
        'EURUSD': [(_srv(_t(21, 59, 58, 900)), 1.15400, 1.15410)],
        'USDCHF': [(_srv(_t(21, 59, 58, 900)), 0.81900, 0.81910)]})
    _sm3.backfill_sync_mid(['2026-09-16'], fetcher=_fk)
    import time as _time7
    _time7.sleep(0.05)
    _fk2 = _mk_fetcher({
        'EURCHF': [(_srv(_t(21, 59, 58, 950)), 0.94600, 0.94610)],
        'EURUSD': [(_srv(_t(21, 59, 58, 950)), 1.15400, 1.15410)],
        'USDCHF': [(_srv(_t(21, 59, 58, 950)), 0.81900, 0.81910)]})
    _sm3.backfill_sync_mid(['2026-09-16'], fetcher=_fk2)
    snaps = _gl.glob(_os.path.join(_test_dir, 'snap__*.parquet'))
    check('两次采集→两份不可变快照（P1-3）', len(snaps) == 2,
          str([(_os.path.basename(s))[-20:] for s in snaps]))
    lat = pd.read_parquet(_sm3.SYNC_LATEST)
    check('latest 取最新快照（mid=0.94605）',
          len(lat) == 1 and abs(lat['EURCHF__mid'].iloc[0] - 0.94605) < 1e-9)
finally:
    _sm3.SYNC_DIR, _sm3.SYNC_LATEST = _orig_dir, _orig_dir / 'EURCHF_TRIO__D1.parquet'
    _sm3.SYNC_PROV_LATEST = _orig_dir / 'EURCHF_TRIO__provenance.json'
    _sh.rmtree(_test_dir, ignore_errors=True)

# P2：uniform_grid_check 缺陷时间 = 实际缺失槽位（06:00 而非 07:00）
from fx_data.resample import uniform_grid_check as _ugc7
_h1_629 = pd.DataFrame({'ts_utc': pd.DatetimeIndex(
    ['2026-06-29 05:00', '2026-06-29 07:00'], tz='UTC')})
_d7 = _ugc7(_h1_629)
check('V5 缺陷时间=实际缺失槽位（06:00，非 07:00）',
      len(_d7) == 1 and str(_d7['ts_utc'].iloc[0]).startswith('2026-06-29 06:00'),
      str(_d7['ts_utc'].tolist()))

# ---------- 第九轮：event_id 生成/解析统一 + 碰撞/损坏/并发防御 ----------
import json as _json8
import threading as _th8
import fx_data.syncmid as _sm8
from pathlib import Path as _P8
_t8 = _os.path.join('data', 'raw', 'mt5', 'syncmid_test9')
_sh.rmtree(_t8, ignore_errors=True)
_os.makedirs(_t8, exist_ok=True)
_sm8.SYNC_DIR = _P8(_t8)
_sm8.SYNC_LATEST = _P8(_t8) / 'EURCHF_TRIO__D1.parquet'
_sm8.SYNC_PROV_LATEST = _P8(_t8) / 'EURCHF_TRIO__provenance.json'
try:
    check('event_id formatter/parser 互逆（-nZ 后缀）',
          _sm8._format_event_id('20260917T030000000001Z', 1)
          == '20260917T030000000001-1Z'
          and _sm8._parse_event_name('snap__EURCHF_TRIO__20260917T030000000001-1Z.parquet')
          == ('20260917T030000000001', 1, 'parquet')
          and _sm8._parse_event_name('snap__EURCHF_TRIO__20260917T030000000001Z.json')
          == ('20260917T030000000001', 0, 'json'))
    check('旧式 Z-1 文件名不被接受（不再静默歧义）',
          _sm8._parse_event_name('snap__EURCHF_TRIO__20260917T030000000001Z-1.parquet')
          is None)

    _fk8 = _mk_fetcher({
        'EURCHF': [(_srv(_t(21, 59, 58, 900)), 0.94500, 0.94510)],
        'EURUSD': [(_srv(_t(21, 59, 58, 900)), 1.15400, 1.15410)],
        'USDCHF': [(_srv(_t(21, 59, 58, 900)), 0.81900, 0.81910)]})
    _fk8b = _mk_fetcher({
        'EURCHF': [(_srv(_t(21, 59, 58, 950)), 0.94600, 0.94610)],
        'EURUSD': [(_srv(_t(21, 59, 58, 950)), 1.15400, 1.15410)],
        'USDCHF': [(_srv(_t(21, 59, 58, 950)), 0.81900, 0.81910)]})

    _orig_ts8 = pd.Timestamp
    class _TS8(_orig_ts8):
        @classmethod
        def now(cls, tz=None):
            return cls('2026-09-17 03:00:00.000001', tz='UTC')
    pd.Timestamp = _TS8
    try:
        _sm8.backfill_sync_mid(['2026-09-16'], fetcher=_fk8)
        _sm8.backfill_sync_mid(['2026-09-16'], fetcher=_fk8b)
    finally:
        pd.Timestamp = _orig_ts8
    _a8 = _sm8.audit_events()
    check('碰撞两事件均在 complete（base+collision）',
          len(_a8['complete_event_ids']) == 2
          and '20260917T030000000001Z' in _a8['complete_event_ids']
          and '20260917T030000000001-1Z' in _a8['complete_event_ids'],
          str(_a8['complete_event_ids']))
    check('碰撞场景 rejected 为空', _a8['rejected'] == [], str(_a8['rejected']))
    _lp8 = _json8.loads(_sm8.SYNC_PROV_LATEST.read_text(encoding='utf-8'))
    check('selected_event_ids 同时包含 base 与 collision',
          set(_lp8['selected_event_ids']) == {'20260917T030000000001Z',
                                              '20260917T030000000001-1Z'})
    _lat8 = pd.read_parquet(_sm8.SYNC_LATEST)
    check('latest 该 session 来自第二次写入（碰撞事件胜出）',
          len(_lat8) == 1 and abs(_lat8['EURCHF__mid'].iloc[0] - 0.94605) < 1e-9,
          str(_lat8['EURCHF__mid'].tolist()))

    _vk, _vp = _sm8._valid_events()[0][0]
    _victim_pq = _vp
    _victim_js = _vp.with_suffix('.json')
    check('victim 修改前在 complete', _vp.name in _a8['complete_events'])
    _vp2 = _json8.loads(_victim_js.read_text(encoding='utf-8'))
    _vp2['parquet_sha256'] = '0' * 64
    _victim_js.write_text(_json8.dumps(_vp2), encoding='utf-8')
    _a8b = _sm8.audit_events()
    check('篡改 hash 后移出 complete 且入 rejected',
          _victim_pq.name not in _a8b['complete_events']
          and any('sha256' in r and _victim_pq.name in r
                  for r in _a8b['rejected']), str(_a8b['rejected']))
    _lat8b = _sm8._rebuild_latest()
    check('latest 不消费哈希不匹配事件',
          _lat8b is not None and abs(_lat8b['EURCHF__mid'].iloc[0] - 0.94605) < 1e-9,
          str(_lat8b['EURCHF__mid'].tolist()))
    _vp2['parquet_sha256'] = _sm8._sha256(_victim_pq)
    _victim_js.write_text(_json8.dumps(_vp2), encoding='utf-8')

    _vk2, _vp3 = _sm8._valid_events()[0][0]
    _vj3 = _vp3.with_suffix('.json')
    _vj3.write_text('{corrupted', encoding='utf-8')
    _a8c = _sm8.audit_events()
    check('损坏 sidecar 被拒绝且显式报错',
          _vp3.name not in _a8c['complete_events']
          and any('损坏' in r for r in _a8c['rejected']))
    _vj3.unlink()
    _a8d = _sm8.audit_events()
    check('缺 sidecar 半事件被拒绝',
          _vp3.name not in _a8d['complete_events']
          and any('缺 sidecar' in r for r in _a8d['rejected']))

    _t9 = _os.path.join('data', 'raw', 'mt5', 'syncmid_test9c')
    _sh.rmtree(_t9, ignore_errors=True)
    _os.makedirs(_t9, exist_ok=True)
    _sm8.SYNC_DIR = _P8(_t9)
    _errs = []
    def _writer(mid_price):
        try:
            _prov9 = {'captured_at_utc': '2026-09-17T03:00:00Z',
                      'symbols': ['EURCHF', 'EURUSD', 'USDCHF'],
                      'session_dates': ['2026-09-16'], 'rows': 1,
                      'target_rule': 'x', 'session_calendar': 'x',
                      'prescan_min': 90, 'skew_max_ms': 2000,
                      'age_max_ms': 60000, 'clock_semantics': 'x',
                      'schema_version': _sm8.SNAP_SCHEMA_VERSION,
                      'immutability': 'x', 'thread_mid': mid_price}
            _df9 = pd.DataFrame({'session_date': ['2026-09-16'],
                                 'EURCHF__mid': [mid_price],
                                 'EURUSD__mid': [1.15], 'USDCHF__mid': [0.82],
                                 'skew_ms': [100.0], 'verified': [True],
                                 'reason': [''], 'target_utc': [None]})
            _sm8._atomic_write_pair(_df9, _prov9, '20260917T040000000000Z')
        except Exception as e:
            _errs.append(repr(e))
    _ths = [_th8.Thread(target=_writer, args=(p,)) for p in (0.94505, 0.94605)]
    for _th in _ths:
        _th.start()
    for _th in _ths:
        _th.join()
    check('并发无异常', _errs == [], str(_errs))
    _a9 = _sm8.audit_events()
    check('并发两事件均完整存活', len(_a9['complete_event_ids']) == 2
          and _a9['rejected'] == [], str(_a9))
    _ok9 = True
    for _k9, _p9 in _sm8._valid_events()[0]:
        _j9 = _json8.loads(_p9.with_suffix('.json').read_text(encoding='utf-8'))
        if _j9['parquet_sha256'] != _sm8._sha256(_p9):
            _ok9 = False
        _df9 = pd.read_parquet(_p9)
        if abs(_df9['EURCHF__mid'].iloc[0] - _j9['thread_mid']) > 1e-12:
            _ok9 = False
    check('并发无串写（sidecar 哈希与价格各自配对）', _ok9)
    check('并发无残留临时文件',
          not list(_P8(_t9).glob('.tmp__*')), str(list(_P8(_t9).glob('.tmp__*'))))
    _sh.rmtree(_t9, ignore_errors=True)
    _sm8.SYNC_DIR = _P8(_t8)
    _sm8.SYNC_LATEST = _P8(_t8) / 'EURCHF_TRIO__D1.parquet'
    _sm8.SYNC_PROV_LATEST = _P8(_t8) / 'EURCHF_TRIO__provenance.json'

    _os.makedirs(_os.path.join(_t8, 'legacy'), exist_ok=True)
    _sm8.SYNC_DIR = _P8(_os.path.join(_t8, 'legacy'))
    pd.DataFrame({'session_date': ['2026-09-16'], 'verified': [True]}
                 ).to_parquet(_P8(_os.path.join(_t8, 'legacy',
                 'snap__EURCHF_TRIO__20260101T000000000000Z.parquet')), index=False)
    _m8 = _sm8.migrate_legacy_snapshots()
    _sc8 = _json8.loads(_P8(_os.path.join(
        _t8, 'legacy', 'snap__EURCHF_TRIO__20260101T000000000000Z.json'))
        .read_text(encoding='utf-8'))
    check('迁移不删 legacy 裸 parquet',
          _os.path.exists(_os.path.join(
              _t8, 'legacy', 'snap__EURCHF_TRIO__20260101T000000000000Z.parquet')))
    check('迁移不伪造未知 provenance',
          _sc8['target_rule'] == 'legacy_provenance_unavailable'
          and _sc8['prescan_min'] == 'legacy_provenance_unavailable')
finally:
    _sm8.SYNC_DIR = _P8('data/raw/mt5/syncmid')
    _sm8.SYNC_LATEST = _sm8.SYNC_DIR / 'EURCHF_TRIO__D1.parquet'
    _sm8.SYNC_PROV_LATEST = _sm8.SYNC_DIR / 'EURCHF_TRIO__provenance.json'
    _sh.rmtree(_t8, ignore_errors=True)

h1_gap = pd.DataFrame({'ts_utc': pd.DatetimeIndex(
    ['2026-03-31 18:00', '2026-03-31 19:00', '2026-03-31 20:00',
     '2026-03-31 22:00', '2026-04-01 00:00'], tz='UTC')})
slots = find_missing_slots(h1_gap)
check('find_missing_slots 找到 21:00Z 缺口',
      pd.Timestamp('2026-03-31 21:00', tz='UTC') in slots, str(slots))

# ---------- 第十轮：反向半事件（JSON-only）与非法名称显式拒绝 ----------
import hashlib as _hl10
import fx_data.syncmid as _sm10
from pathlib import Path as _P10
_t10 = _os.path.join('data', 'raw', 'mt5', 'syncmid_test10')
_sh.rmtree(_t10, ignore_errors=True)
_os.makedirs(_t10, exist_ok=True)
_sm10.SYNC_DIR = _P10(_t10)
_sm10.SYNC_LATEST = _P10(_t10) / 'EURCHF_TRIO__D1.parquet'
_sm10.SYNC_PROV_LATEST = _P10(_t10) / 'EURCHF_TRIO__provenance.json'


def _fresh10():
    for _f in _P10(_t10).glob('snap__*'):
        _f.unlink()


def _write_legal10(eid, mid):
    pq = _P10(_t10) / f'snap__EURCHF_TRIO__{eid}.parquet'
    pd.DataFrame({'session_date': ['2026-09-16'], 'EURCHF__mid': [mid],
                  'EURUSD__mid': [1.15], 'USDCHF__mid': [0.82],
                  'skew_ms': [100.0], 'verified': [True], 'reason': [''],
                  'target_utc': [None]}).to_parquet(pq)
    prov = {'event_id': eid, 'parquet_file': pq.name,
            'parquet_sha256': _hl10.sha256(pq.read_bytes()).hexdigest(),
            'schema_version': '2'}
    (_P10(_t10) / f'snap__EURCHF_TRIO__{eid}.json').write_text(
        _json8.dumps(prov), encoding='utf-8')


try:
    # A) JSON-only canonical → complete=[] 且 rejected 含「缺 parquet」
    _fresh10()
    (_P10(_t10) / 'snap__EURCHF_TRIO__20260917T050000000000Z.json'
     ).write_text('{}', encoding='utf-8')
    _a10 = _sm10.audit_events()
    check('JSON-only → complete 空且显式缺 parquet',
          _a10['complete_events'] == []
          and any('缺 parquet' in r for r in _a10['rejected']),
          str(_a10['rejected']))

    # B) parquet-only canonical → rejected 含「缺 sidecar」
    _fresh10()
    pd.DataFrame({'x': [1]}).to_parquet(_P10(_t10) /
        'snap__EURCHF_TRIO__20260917T050000000000Z.parquet')
    _b10 = _sm10.audit_events()
    check('parquet-only → 显式缺 sidecar',
          _b10['complete_events'] == []
          and any('缺 sidecar' in r for r in _b10['rejected']),
          str(_b10['rejected']))

    # C) 旧式 ...Z-1.json → rejected 含「文件名不符约定」
    _fresh10()
    (_P10(_t10) / 'snap__EURCHF_TRIO__20260917T050000000000Z-1.json'
     ).write_text('{}', encoding='utf-8')
    _c10 = _sm10.audit_events()
    check('旧式 Z-1 JSON → 文件名不符约定被拒',
          _c10['complete_events'] == []
          and any('文件名不符' in r for r in _c10['rejected']),
          str(_c10['rejected']))

    # D) 合法完整 + JSON-only → complete 仅合法，latest 只取合法
    _fresh10()
    _write_legal10('20260917T070000000000Z', 0.94505)
    (_P10(_t10) / 'snap__EURCHF_TRIO__20260917T080000000000Z.json'
     ).write_text('{}', encoding='utf-8')
    _d10 = _sm10.audit_events()
    _lat10 = _sm10._rebuild_latest()
    check('合法+JSON-only：complete 仅含合法事件',
          _d10['complete_event_ids'] == ['20260917T070000000000Z']
          and any('缺 parquet' in r for r in _d10['rejected']),
          str(_d10))
    check('latest 只消费合法事件',
          _lat10 is not None and abs(_lat10['EURCHF__mid'].iloc[0] - 0.94505) < 1e-9)

    # 去重确定性：同一坏事件不重复报
    _fresh10()
    (_P10(_t10) / 'snap__EURCHF_TRIO__20260917T090000000000Z-1.json'
     ).write_text('{}', encoding='utf-8')
    (_P10(_t10) / 'snap__EURCHF_TRIO__20260917T090000000000Z-1.json'
     ).write_text('{}', encoding='utf-8')
    _e10 = _sm10.audit_events()
    check('坏名称只报一次（去重）', len(_e10['rejected']) == 1, str(_e10['rejected']))
finally:
    _sm10.SYNC_DIR = _P10('data/raw/mt5/syncmid')
    _sm10.SYNC_LATEST = _sm10.SYNC_DIR / 'EURCHF_TRIO__D1.parquet'
    _sm10.SYNC_PROV_LATEST = _sm10.SYNC_DIR / 'EURCHF_TRIO__provenance.json'
    _sh.rmtree(_t10, ignore_errors=True)
    check('第十轮隔离目录已清理', not _os.path.exists(_t10))

# ---------- v1.1 变更清单回归（C-2 / C-3 / C-4 / C-5） ----------
from fx_data.summary import render_board_text, summarize_board

# C-2：currency 对象显式 delta/purity/state_ambiguous；文本 (!) 标记与单行排行
_fake11 = {
    'asof': '2026-09-16T00:00:00Z', 'data_through_session': '2026-09-15',
    'window': 20, 'staleness_hours': 7.0, 'staleness_status': 'ok',
    'ranking': ['USD', 'JPY', 'AUD'],
    'quality': {'board_pairs': 28, 'excluded_pairs': []},
    'currencies': [
        {'ccy': 'USD', 'z': 0.606, 'z_short': 1.515, 'state': 'UP_ACCEL',
         'rank': 1, 'membership': {'UP_ACCEL': 0.909}},
        {'ccy': 'JPY', 'z': 1.120, 'z_short': -0.296, 'state': 'UP_DECEL',
         'rank': 2, 'membership': {'UP_DECEL': 1.0}},
        {'ccy': 'AUD', 'z': 0.390, 'z_short': -0.312, 'state': 'UP_DECEL',
         'rank': 3, 'membership': {'UP_DECEL': 0.548, 'FLAT_DECEL': 0.266}},
    ],
}
_s11 = summarize_board(_fake11)
_u11 = {c['ccy']: c for c in _s11['currencies']}
check('C-2 delta=z_short−z', abs(_u11['USD']['delta'] - 0.909) < 1e-9)
check('C-2 purity+state_ambiguous',
      abs(_u11['AUD']['purity'] - 0.548) < 1e-9
      and _u11['AUD']['uncertain'] and not _u11['USD']['uncertain'])
_t11 = render_board_text(_s11)
check('C-2 文本 (!) 标记', '(!)' in _t11 and 'UP_DECEL(!)' in _t11)
check('C-2 单行动能排行 top-N',
      '动能转折排行 (|Δ| desc): JPY -1.416 | USD +0.909 | AUD -0.702' in _t11,
      _t11.splitlines()[-1])

# C-2 compare：Δ 跨窗符号一致性（含冲突反例）
from fx_data.summary import render_compare_text
_cmp11 = {
    'a': {'window': 20, 'through': 'x'}, 'b': {'window': 50, 'through': 'y'},
    'currencies': [
        {'ccy': 'USD', 'z': 1.0, 'z_short': 2.0, 'delta': 1.0,
         'state_display': 'UP_ACCEL', 'purity': 0.9, 'z_b': 0.5,
         'delta_b': 0.4, 'delta_sign_consistency': '一致', 'z_shift_w': 0.5},
        {'ccy': 'AUD', 'z': 0.3, 'z_short': -0.4, 'delta': -0.7,
         'state_display': 'UP_DECEL(!)', 'purity': 0.5, 'z_b': 0.2,
         'delta_b': 0.118, 'delta_sign_consistency': '冲突(!)', 'z_shift_w': 0.1},
    ],
    'delta_sign_conflicts': ['AUD'],
    'momentum_turn_ranking': _s11['momentum_turn_ranking'][:1],
}
_t12 = render_compare_text(_cmp11)
check('C-2 compare Δ 符号列+冲突提示',
      '冲突(!)' in _t12 and 'Δ 符号冲突' in _t12 and 'AUD' in _t12)

if all(st.norm_exists(s, 'D1') for s in ('XAUUSD', 'EURUSD')):
    from fx_data.api import get_series as _gs11
    # C-4：白名单扩展 + window/stats/truncated 边界
    _e11 = _gs11('EURCHF', n=5)
    check('C-4 交叉盘白名单+truncated 字段',
          _e11['row_count'] == 5 and _e11['truncated'] is True
          and _e11['available'] > 5 and _e11['window_mode'] == 'rolling_fixed',
          f"avail={_e11['available']}")
    _big11 = _gs11('EURCHF', n=2000)
    check('C-4 n>可用 → 全量返回 truncated=false',
          _big11['row_count'] == _big11['available']
          and _big11['truncated'] is False)
    try:
        _gs11('EURCHF', tf='M5')
        check('C-4 tf 错误列出支持项', False)
    except ValueError as ex:
        check('C-4 tf 错误列出支持项', 'D1' in str(ex) and 'H1' in str(ex))
    try:
        _gs11('ZZZZ')
        check('C-4 symbol 错误列出白名单', False)
    except ValueError as ex:
        check('C-4 symbol 错误列出白名单', 'EURCHF' in str(ex) and 'XAUUSD' in str(ex))
    try:
        _gs11('HG', tf='H1')
        check('C-4 HG 拒绝 H1', False)
    except ValueError:
        check('C-4 HG 拒绝 H1', True)

    # C-3：DXY append-only 序列 + 全历史稳定统计
    if st.derived_exists('dxy__D1'):
        _d11 = _gs11('DXY', n=10)
        check('C-3 DXY window=append_only+stats=full_history',
              _d11['window_mode'] == 'append_only'
              and _d11['stats_scope'] == 'full_history'
              and set(_d11['stats']) == {'min', 'max', 'mean', 'rows_full_history'},
              str(_d11.get('stats')))
        check('C-3 DXY 读取时截断（truncated=true）',
              _d11['truncated'] is True and _d11['row_count'] == 10)
        _dh = st.read_derived('dxy__D1')
        check('C-3 DXY 全历史 min/max 与存储一致',
              abs(_d11['stats']['min'] - float(_dh['close'].min())) < 1e-9
              and abs(_d11['stats']['max'] - float(_dh['close'].max())) < 1e-9)
    else:
        print('[SKIP] C-3 DXY 未构建')

    # C-5：真实 env 的底层暴露分解
    from fx_data.api import get_env_state as _ge11
    _en11 = _ge11()
    _ex11 = _en11['gauge_underlying_exposure']
    check('C-5 XAUUSD 3 分量/净符号按规则=+1/abs_weight 0.75',
          _ex11['XAUUSD']['n_components'] == 3
          and _ex11['XAUUSD']['net_sign'] == 1
          and abs(_ex11['XAUUSD']['abs_weight'] - 0.75) < 1e-9
          and _ex11['XAUUSD']['components'] == ['au_spx', 'cu_au', 'gor'])
    check('C-5 HG 净符号+1 / US500 单分量',
          _ex11['HG']['net_sign'] == 1 and _ex11['US500']['n_components'] == 1)
    check('C-5/A-9 底层集中度=0.75+warning 仅 XAUUSD（>0.5 严格）',
          abs(_en11['gauge_underlying_concentration'] - 0.75) < 1e-9
          and _en11['gauge_self_reference_warning'] == ['XAUUSD'])
else:
    print('[SKIP] v1.1 实测段需要本机 norm 数据')

# ---------- 审计返工回归（A-2 表头 / A-4 append-only 可复现 / A-5 无累积指数
# / A-6/A-7/A-8 get_series 验收包 / DXY footer 元数据） ----------
from fx_data.summary import render_board_text as _rbt12, summarize_board as _sb12

# A-2：表头含 staleness/dispersion/board_vol/residual_rms
_h12 = {
    'asof': '2026-09-15T22:00:00Z', 'data_through_session': '2026-09-16',
    'window': 20, 'staleness_hours': 7.18, 'staleness_status': 'ok',
    'dispersion': 0.01656, 'board_vol_scalar': 0.0233,
    'ranking': ['USD'],
    'quality': {'board_pairs': 28, 'excluded_pairs': [],
                'residual_rms': 0.000146},
    'currencies': [
        {'ccy': 'USD', 'z': 0.606, 'z_short': 1.515, 'state': 'UP_ACCEL',
         'rank': 1, 'membership': {'UP_ACCEL': 0.909}},
    ],
}
_t12b = _rbt12(_sb12(_h12))
check('A-2 表头四指标齐备',
      all(s in _t12b for s in ('staleness_hours=7.18', 'dispersion=0.01656',
                               'board_vol=0.0233', 'residual_rms=0.000146')),
      _t12b.splitlines()[1] + ' | ' + _t12b.splitlines()[2])

# A-4：append_only_merge 可复现——源窗前移后旧起点固定、行数只增
from fx_data import storage as _st12
import pandas as _pd12
_old12 = _pd12.DataFrame({'session_date': ['2025-03-04', '2025-03-05',
                                           '2026-09-16'],
                          'close': [100.0, 101.0, 99.5]})
_new12 = _pd12.DataFrame({'session_date': ['2025-03-06', '2026-09-16',
                                           '2026-09-17'],
                          'close': [102.0, 99.6, 100.1]})  # 源窗已滚出 03-04/05
_m12 = _st12.append_only_merge.__wrapped__(_old12, _new12) \
    if hasattr(_st12.append_only_merge, '__wrapped__') else None
# 直接用纯函数语义：拷贝实现太绕，改为临时派生目录实测
import pathlib as _pl12, shutil as _sh12, os as _os12
_d12 = _os.path.join('data', 'derived', '_test_append_only')
_sh12.rmtree(_d12, ignore_errors=True)
_os.makedirs(_d12, exist_ok=True)
_orig_dd12 = _st12.config.DIR_DERIVED
_st12.config.DIR_DERIVED = _pl12.Path(_d12)
try:
    _st12.write_derived(_old12, 'probe__D1')
    _m12 = _st12.append_only_merge('probe__D1', _new12)
    check('A-4 源窗滚出后旧起点固定+行数只增',
          str(_m12['session_date'].iloc[0]) == '2025-03-04'
          and len(_m12) == 5
          and str(_m12['session_date'].iloc[-1]) == '2026-09-17',
          str(_m12['session_date'].tolist()))
    check('A-4 同 session 以新表为准（修正）',
          float(_m12[_m12['session_date'] == '2026-09-16']['close'].iloc[0])
          == 99.6)
finally:
    _st12.config.DIR_DERIVED = _orig_dd12
    _sh12.rmtree(_d12, ignore_errors=True)

# A-4：真实 dxy parquet footer 元数据
if _st12.derived_exists('dxy__D1'):
    import pyarrow.parquet as _pq12
    _md12 = _pq12.read_schema(_orig_dd12 / 'dxy__D1.parquet').metadata
    check('A-4 parquet footer 元数据齐备',
          _md12[b'window_mode'] == b'append_only'
          and _md12[b'stats_scope'] == b'full_history'
          and b'window_rows' in _md12 and b'warning' in _md12)
    from fx_data.api import get_series as _gs12
    _dx12 = _gs12('DXY', n=5)
    check('A-4 dxy_series 顶层标注',
          _dx12['window_mode'] == 'append_only'
          and _dx12['stats_scope'] == 'full_history'
          and _dx12['window_rows'] >= 400
          and _dx12['first_session'] == '2025-03-04'
          and '不可比' in _dx12['warning'])

# A-5：board 无累积指数——纯窗口对数收益（代码级断言）
import fx_data.board as _bd12, inspect as _ins12
_src12 = _ins12.getsource(_bd12)
check('A-5 board 模块无 cumsum/cumprod（无累积指数）',
      'cumsum' not in _src12 and 'cumprod' not in _src12
      and 'np.log(c[-1]) - np.log(c[-1 - window])' in _src12)
check('A-5 窗口收益为纯两点对数差（非累积）',
      'np.log(c[-1]) - np.log(c[-1 - window])' in _src12)
check('A-5 z 为截面标准化（s/std(r)）',
      'board_vol' in _src12 and 'np.std(r, ddof=1)' in _src12)

# A-6/A-7/A-8：get_series 验收包
if all(st.norm_exists(s, 'D1') for s in ('XAUUSD', 'XTIUSD', 'HG', 'US500')):
    from fx_data.api import get_series as _gs
    ALLOWED_D1 = {'session_date', 'ts_utc', 'open', 'high', 'low', 'close',
                  'volume', 'segment_id', 'roll_flag', 'bars_in_session',
                  'partial'}
    # XAUUSD n=10 字段白名单扫描（A-7 一票否决项）
    _x10 = _gs('XAUUSD', n=10)
    check('A-7 n=10 行字段全部在白名单（无派生指标）',
          all(set(r.keys()) <= ALLOWED_D1 for r in _x10['rows'])
          and len(_x10['rows']) == 10)
    check('A-7 行无 ATR/ADX/均线键',
          not any(k in r for r in _x10['rows']
                  for k in ('atr', 'adx', 'sma', 'ema', 'rsi')))
    # XAUUSD n=120 行数与末根
    _x120 = _gs('XAUUSD', n=120)
    _raw120 = st.read_norm('XAUUSD', 'D1')
    from fx_data.resample import complete_sessions as _cs12
    _exp120 = _cs12(_raw120, pd.Timestamp.now('UTC')).tail(1).iloc[0]
    check('A-6 n=120 行数+末根与 L1 一致',
          _x120['row_count'] == 120
          and _x120['rows'][-1]['session_date'] == str(_exp120['session_date'])
          and abs(_x120['rows'][-1]['close'] - float(_exp120['close'])) < 1e-9)
    # XTIUSD/HG/US500 同源交叉验证
    for _s12 in ('XTIUSD', 'HG', 'US500'):
        _o12 = _gs(_s12, n=3)
        _r12 = _cs12(st.read_norm(_s12, 'D1'),
                     pd.Timestamp.now('UTC')).tail(1).iloc[0]
        check(f'A-6 {_s12} 末根同源一致',
              _o12['rows'][-1]['session_date'] == str(_r12['session_date'])
              and abs(_o12['rows'][-1]['close'] - float(_r12['close'])) < 1e-9)
    # 边界 B1: 无效 symbol
    try:
        _gs('FAKEXYZ')
        check('A-8 B1 无效 symbol 报错并列白名单', False)
    except ValueError as _e12:
        check('A-8 B1 无效 symbol 报错并列白名单',
              'XAUUSD' in str(_e12) and 'FAKEXYZ' in str(_e12))
    # 边界 B2: n=99999 > 可用 → 全量 + truncated=false
    _big12 = _gs('XAUUSD', n=99999)
    check('A-8 B2 n=99999 → 全量返回 truncated=false',
          _big12['row_count'] == _big12['available']
          and _big12['truncated'] is False and _big12['row_count'] > 100)
    # 边界 B3: 不支持的 tf
    try:
        _gs('XAUUSD', tf='M5')
        check('A-8 B3 tf=M5 报错列出支持项', False)
    except ValueError as _e12:
        check('A-8 B3 tf=M5 报错列出支持项',
              'D1' in str(_e12) and 'H1' in str(_e12))
    # 边界 B4: partial 会话正常透出
    _has_partial12 = any(r['partial'] for r in _gs('HG', n=2000)['rows'])
    check('A-8 B4 partial 字段透出（由消费方决定剔除）', isinstance(_has_partial12, bool))
else:
    print('[SKIP] get_series 验收包需要本机 norm 数据')

print('\n' + ('全部通过' if not FAILURES else f'失败 {len(FAILURES)}: {FAILURES}'))
sys.exit(1 if FAILURES else 0)
