"""§5 派生层三个端点。只暴露派生结论，不暴露原始 bar。

审计修订：
- get_strength_board(window, asof)：仅使用 asof 前已完结会话的数据——
  **包括 V9 异常盘剔除**（live 三角/点差快照只在 asof 不早于快照时刻时适用，
  历史 asof 不带入当前异常盘，防前视）；segment 不足 → 阻断而非跨段。
- get_env_state：HG/VIX/XAU/XTI/US500 一律排除未完成会话（partial 行保留在
  L1 但不进斜率/状态）。
- spread 一律真实来源（tick 快照 / copy_rates bar 级 / 实时快照），无则显式
  unavailable；ACS/RCS 显式 proxy。"""
import numpy as np
import pandas as pd

from . import config, storage
from .board import build_board, qc_residuals
from .env import compute_env
from .pair_context import build_pair_context
from .resample import complete_sessions
from .strength import attach_states
from .validate import (_inst_meta, _spread_sources, _synthetic_close,
                       _tick_snapshot, _triangles)


def _norm_d1_map(symbols):
    return {s: storage.read_norm(s, 'D1') for s in symbols
            if storage.norm_exists(s, 'D1')}


def _trim_to_segment(d1: pd.DataFrame, window: int, symbol: str
                     ) -> pd.DataFrame:
    """强度窗口不得跨越 segment 边界（§3.1 segment 语义）。
    当前段样本不足 → **阻断**（README 口径：segment 边界不跨段）。"""
    if 'segment_id' not in d1.columns:
        return d1
    seg = int(d1['segment_id'].iloc[-1])
    tail = d1[d1['segment_id'] == seg]
    if len(tail) >= window + 1:
        return tail.reset_index(drop=True)
    raise RuntimeError(
        f'{symbol}: 当前 segment({seg}) 完整会话仅 {len(tail)} 根 < 窗口 '
        f'{window + 1}，segment 边界不跨段 → 阻断')


def _asof_now() -> pd.Timestamp:
    return pd.Timestamp.now('UTC')


def asof_semantics(effective_session, asof_ts: pd.Timestamp | None = None,
                   boundary_utc: int = config.CANONICAL_CUT_UTC) -> dict:
    """P1-5：asof / effective_session 语义显式化（board/env/series 统一口径）。

    - asof_semantics='session_start'：返回的时点字段表示**会话起始 bar 时刻**
      （会话 D 覆盖 [D-1 22:00Z, D 22:00Z)，首根 bar ts 即 22:00Z）。
    - staleness_hours = 可信当前 UTC − 有效会话结束时刻（session_date 当日
      22:00Z）。负值（数据时点在未来/时钟冲突）不得冒充新鲜 → None +
      'clock_conflict_or_future'。
    - effective_session 为空 → 全部不可用。"""
    out = {
        'asof_semantics': 'session_start',
        'effective_session': str(effective_session) if effective_session is not None else None,
        'session_boundary_utc': boundary_utc,
        'staleness_hours': None,
        'staleness_status': 'unavailable',
    }
    if effective_session is None:
        return out
    now = asof_ts if asof_ts is not None else _asof_now()
    if now.tzinfo is None:
        now = now.tz_localize('UTC')
    else:
        now = now.tz_convert('UTC')
    sess_end = (pd.Timestamp(str(effective_session), tz='UTC')
                + pd.Timedelta(hours=boundary_utc))
    delta_h = (now - sess_end).total_seconds() / 3600.0
    if delta_h < 0:
        out['staleness_status'] = 'clock_conflict_or_future'
        return out
    out['staleness_hours'] = round(delta_h, 2)
    out['staleness_status'] = 'ok'
    return out


# ---- P0-2：原始价格序列通道（反向审计用，严禁派生指标） ----
SERIES_SYMBOLS = {
    'XAUUSD': 'mt5 金', 'XTIUSD': 'mt5 WTI', 'XBRUSD': 'mt5 布伦特',
    'US500': 'mt5 US500', 'HG': 'ibkr 铜连续合约（比例回调）',
    # C-4 白名单扩展（v1.1）：主力交叉盘与近期分析重点
    'EURCHF': 'mt5 EURCHF', 'GBPCHF': 'mt5 GBPCHF', 'EURGBP': 'mt5 EURGBP',
    'USDCHF': 'mt5 USDCHF', 'EURUSD': 'mt5 EURUSD', 'AUDUSD': 'mt5 AUDUSD',
    'USDJPY': 'mt5 USDJPY',
    # C-3：DXY 自算（append-only 全历史，读取时截断）
    'DXY': '自算 DXY（六成分几何加权，append-only 全历史）',
}
SERIES_D1_ONLY = {'HG', 'DXY'}


def _iso_utc_z(v) -> str:
    """统一 UTC-Z ISO8601（验收规定格式：2026-09-15T22:00:00Z）。"""
    ts = pd.Timestamp(v)
    if ts.tzinfo is None:
        ts = ts.tz_localize('UTC')
    else:
        ts = ts.tz_convert('UTC')
    return ts.isoformat().replace('+00:00', 'Z')


def get_series(symbol: str, tf: str = 'D1', n: int = 120,
               since: str | pd.Timestamp | None = None,
               asof: str | pd.Timestamp | None = None) -> dict:
    """P0-2：暴露磁盘 L1 已有**原始价格序列**（未加任何指标加工）。

    - 只返回 OHLCV + 追溯字段（session_date/segment_id/roll_flag/partial/
      bars_in_session）；严禁 ATR/ADX 等派生指标——本通道的用途是反向审计
      引擎（端点结论应能从本序列复算出来）。
    - symbol ∈ {XAUUSD, XTIUSD, XBRUSD, US500, HG}（HG=铜连续合约 canonical，
      兼容 'HG_CONTINUOUS'/'HG连续' 别名归一）。tf ∈ {D1, H1}；HG 仅支持 D1。
    - 无前视：D1 只返回已完结会话（complete_sessions，22:00Z 边界），默认
      asof=现在；H1 的进行中 bar 已在采集层剔除。
    - n=最后 n 根；since 过滤 session_date/ts ≥ since；since 晚于 asof 报错。
    """
    if tf not in ('D1', 'H1'):
        raise ValueError(f"tf 必须为 'D1' 或 'H1'，实得 {tf!r}")
    if not isinstance(n, int) or not (1 <= n <= 1_000_000):
        raise ValueError(f'n 必须为 1..1000000 的整数，实得 {n!r}')
    sym = str(symbol).strip().upper().replace('CONTINUOUS', '').replace('连续', '')
    if sym not in SERIES_SYMBOLS:
        raise ValueError(f'symbol 必须为 {sorted(SERIES_SYMBOLS)} 之一，实得 {symbol!r}')
    if sym in SERIES_D1_ONLY and tf != 'D1':
        raise ValueError(f'{sym} 仅有 D1（H1 只存在于各腿，非 canonical 序列）')

    ts_asof = (pd.Timestamp(asof) if asof is not None else _asof_now())
    if ts_asof.tzinfo is None:
        ts_asof = ts_asof.tz_localize('UTC')
    else:
        ts_asof = ts_asof.tz_convert('UTC')
    ts_since = None
    if since is not None:
        ts_since = pd.Timestamp(since)
        if ts_since.tzinfo is None:
            ts_since = ts_since.tz_localize('UTC')
        else:
            ts_since = ts_since.tz_convert('UTC')
        if ts_since > ts_asof:
            raise ValueError(f'since({ts_since}) 晚于 asof({ts_asof})——拒绝构造'
                             ' 空窗/未来窗请求')

    if sym == 'DXY':
        return _get_dxy_series(n=n, since=ts_since, asof=ts_asof)
    if not storage.norm_exists(sym, tf):
        raise RuntimeError(f'缺 {sym} {tf}（先跑 pipeline build）')
    df = storage.read_norm(sym, tf)
    if tf == 'D1':
        df_all = complete_sessions(df, ts_asof)
        if ts_since is not None:
            df_all = df_all[pd.to_datetime(df_all['session_date'], utc=True)
                            >= ts_since.normalize()]
        total_avail = len(df_all)
        df = df_all.sort_values('session_date').tail(n)
        rows = [{
            'session_date': str(r.session_date),
            'ts_utc': _iso_utc_z(r.ts_utc),
            'open': float(r.open), 'high': float(r.high),
            'low': float(r.low), 'close': float(r.close),
            'volume': None if pd.isna(r.volume) else float(r.volume),
            'segment_id': int(r.segment_id) if 'segment_id' in df.columns else None,
            'roll_flag': bool(r.roll_flag) if 'roll_flag' in df.columns else False,
            'bars_in_session': int(r.bars_in_session),
            'partial': bool(r.partial),
        } for r in df.itertuples()]
        eff = df['session_date'].iloc[-1] if len(df) else None
        window_mode = 'rolling_fixed'   # MT5 源为固定深度抓取窗（24×400 H1）
        stats_scope = 'source_window'
    else:
        df_all = df[df['ts_utc'] <= ts_asof]
        if ts_since is not None:
            df_all = df_all[df_all['ts_utc'] >= ts_since]
        total_avail = len(df_all)
        df = df_all.sort_values('ts_utc').tail(n)
        rows = [{
            'ts_utc': r.ts_utc.isoformat().replace('+00:00', 'Z'),
            'open': float(r.open), 'high': float(r.high),
            'low': float(r.low), 'close': float(r.close),
            'volume': None if pd.isna(r.volume) else float(r.volume),
            'spread_points': int(r.spread) if 'spread' in df.columns else None,
        } for r in df.itertuples()]
        eff = None
        if len(df):
            d = (df['ts_utc'].iloc[-1] - pd.Timedelta(hours=config.CANONICAL_CUT_UTC)
                 ).floor('D') + pd.Timedelta(days=1)
            eff = d.date()
        window_mode = 'rolling_fixed'
        stats_scope = 'source_window'

    out = {
        'symbol': sym, 'tf': tf,
        'canonical_name': SERIES_SYMBOLS[sym],
        'price_kind': str(df['price_kind'].iloc[0]) if 'price_kind' in df.columns and len(df) else None,
        'source': str(df['source'].iloc[0]) if 'source' in df.columns and len(df) else None,
        'session_boundary_utc': config.CANONICAL_CUT_UTC,
        'session_boundary_note': '会话 D 覆盖 [D-1 22:00Z, D 22:00Z)，标签=结束日',
        'window_mode': window_mode,
        'window_note': '存储为固定深度抓取窗（随采集前移），历史统计非稳定基准'
                       '（DXY 为 append-only 例外）',
        'stats_scope': stats_scope,
        'available': total_avail,
        'truncated': bool(total_avail > len(rows)),
        'row_count': len(rows),
        'rows': rows,
    }
    out.update(asof_semantics(eff, ts_asof))
    return out


def _get_dxy_series(n: int, since, asof: pd.Timestamp) -> dict:
    """C-3：DXY append-only 全历史序列（读取时截断；全历史统计为稳定基准）。"""
    if not storage.derived_exists('dxy__D1'):
        raise RuntimeError('缺 DXY 派生表（先跑 pipeline build）')
    df = storage.read_derived('dxy__D1')
    df = complete_sessions(df, asof)
    if since is not None:
        df = df[pd.to_datetime(df['session_date'], utc=True) >= since.normalize()]
    total = len(df)
    closes_all = df['close'].astype(float)
    tail = df.sort_values('session_date').tail(n)
    rows = [{'session_date': str(r.session_date), 'close': float(r.close)}
            for r in tail.itertuples()]
    out = {
        'symbol': 'DXY', 'tf': 'D1',
        'canonical_name': SERIES_SYMBOLS['DXY'],
        'price_kind': 'derived(mid 合成近似，六成分 MT5 bid 收盘)',
        'source': 'self:compute_dxy(mt5 六成分)',
        'session_boundary_utc': config.CANONICAL_CUT_UTC,
        'session_boundary_note': '会话 D 覆盖 [D-1 22:00Z, D 22:00Z)，标签=结束日',
        'window_mode': 'append_only',
        'window_rows': int(total),
        'first_session': str(df['session_date'].iloc[0]) if total else None,
        'window_note': '存储只追加不删除；首会话固定为 append-only 采用日'
                       '（2025-03-04），此后不随 MT5 源滚动窗前移',
        'stats_scope': 'full_history',
        'warning': 'append-only 采用日之前的历史从未入存储；min/max/mean 自采用'
                   '日起算且此后稳定（可作为跨日基准），采用日之前不可比',
        'stats': {'min': float(closes_all.min()), 'max': float(closes_all.max()),
                  'mean': float(closes_all.mean()), 'rows_full_history': int(total)},
        'available': total,
        'truncated': bool(total > len(rows)),
        'row_count': len(rows),
        'rows': rows,
    }
    out.update(asof_semantics(tail['session_date'].iloc[-1] if len(tail) else None,
                              asof))
    return out


def get_strength_board(window: int = 20, asof: str | pd.Timestamp | None = None,
                       exclude_pairs: list[str] | None = None) -> dict:
    """§5.1 强度板（20/50 交易日窗口）。

    - asof：ISO 时间或 Timestamp；None = 现在。仅使用 asof 前已完结的会话。
    - 排除周末壳（L1 已剔除）、未完成会话；窗口不跨 segment（不足阻断）。
    - live 三角剔除仅当 asof ≥ tick 快照时刻（防历史 asof 带入当前异常盘/点差）；
      历史 asof 下 flagged 仅来自显式 exclude_pairs。
    """
    d1_map = _norm_d1_map(config.PAIRS_28)
    missing = set(config.PAIRS_28) - set(d1_map)
    if missing:
        raise RuntimeError(f'缺 D1 数据: {missing}（先跑 pipeline build）')
    if window not in config.BOARD_WINDOWS:
        raise ValueError(f'window 必须为 {config.BOARD_WINDOWS}')

    ts_asof = (pd.Timestamp(asof) if asof is not None else _asof_now())
    if ts_asof.tzinfo is None:
        ts_asof = ts_asof.tz_localize('UTC')
    else:
        ts_asof = ts_asof.tz_convert('UTC')

    trimmed = {}
    for s, df in d1_map.items():
        t = complete_sessions(df, ts_asof)
        trimmed[s] = _trim_to_segment(t, window, s)
    n_sessions = min(len(v) for v in trimmed.values())
    if n_sessions < window + 1:
        raise RuntimeError(f'完整会话不足（{n_sessions} < {window + 1}），asof={ts_asof}')

    # live V9 三角 → 异常盘剔除重算。三重门（审计第四轮 P1-2）：
    # ① 快照 ≤120s 新鲜（_tick_snapshot 陈旧返回 None——落盘年龄不随时间
    #    增长，有效年龄 = 存储年龄 + 流逝时间）；② asof ≥ 快照时刻（防前视）；
    # ③ 偏移锚无冲突、无时钟异常。任一不满足 → 不剔除（fail-safe）。
    ticks, captured = _tick_snapshot()
    live_ok = (ticks is not None and captured is not None and ts_asof >= captured
               and ticks.get('_offset_source') != 'conflict'
               and not ticks.get('_clock_anomaly'))
    flagged, spread_detail, live_note = [], {}, None
    if live_ok:
        h1_map = {s: storage.read_norm(s, 'H1') for s in config.PAIRS_28
                  if storage.norm_exists(s, 'H1')}
        smap = _spread_sources(h1_map)
        for cross, lb, lq, br, qr in _triangles(sorted(h1_map.keys())):
            if lb is None or not all(k in ticks for k in (cross, lb, lq)):
                continue
            sp, _src = smap.get(cross, (None, ''))
            if sp is None:
                continue
            trio = {k: ticks[k] for k in (cross, lb, lq)}
            skew = max(v['tick_time_ms'] for v in trio.values()) - \
                min(v['tick_time_ms'] for v in trio.values())
            stale = any(v.get('age_eff_ms', v.get('age_ms', 0)) > 120_000
                        for v in trio.values())
            if skew > 5000 or stale:
                continue          # tick 不同刻/陈旧：不据此剔除（V9 中另行披露）
            last = {k: ticks[k]['bid'] for k in (cross, lb, lq)}
            synth = _synthetic_close(last, lb, lq, br, qr)
            if abs(np.log(last[cross]) - np.log(synth)) > 2 * sp:
                flagged.append(cross)
        spread_detail = {s: {'spread_log': smap[s][0], 'source': smap[s][1]}
                         for s in config.PAIRS_28 if s in smap}
        live_note = (f'live剔除@{captured:%m-%d %H:%MZ}, '
                     f'tick跨度{ticks.get("_tick_span_ms")}ms')
    else:
        live_note = ('live剔除不适用（历史 asof 或快照过期）——防前视，'
                     'flagged 仅来自显式 exclude_pairs')
    flagged = sorted(set(flagged) | set(exclude_pairs or []))
    usable = {s: df for s, df in trimmed.items() if s not in flagged}
    if len(usable) < 8:
        raise RuntimeError(f'剔除异常盘后仅剩 {len(usable)} 盘，不足以求解 8 货币')

    board = attach_states(build_board(usable, window))

    partial = 0
    for df in usable.values():
        wd = pd.to_datetime(df['session_date']).dt.dayofweek < 5
        partial += int((wd & (df['bars_in_session'] < 20).to_numpy()).sum())
    sum_check = float(sum(c['strength'] for c in board['currencies']))
    last = usable[sorted(usable)[0]]
    asof_eff = last['ts_utc'].iloc[-1]
    seg = int(last['segment_id'].iloc[-1]) if 'segment_id' in last else None

    out = {
        'asof': ts_asof.isoformat().replace('+00:00', 'Z'),
        'data_through_session': str(last['session_date'].iloc[-1]),
        'last_bar_ts_utc': asof_eff.isoformat().replace('+00:00', 'Z'),
        'window': window,
        'canonical_cut_utc': config.CANONICAL_CUT_UTC,
        'segment_id': seg,
        'cumulative_index_mode': 'NONE',
        'cumulative_index_note': (
            '本强度板无累积指数（NONE）：窗口收益为纯两点对数差 '
            'log(c[-1])−log(c[-1-window])（board.py:40），z 分母为当期 28 盘'
            '截面 std（逐会话重算），归一化基线不依赖任何历史起点。跨日 z 可比'
            '性由「同一统计定义 + 窗口内数据」保证，证据见 '
            'docs/v1.1-acceptance/A5_board_index_proof.md'),
        'currencies': board['currencies'],
        'ranking': board['ranking'],
        'dispersion': board['dispersion'],
        'board_vol_scalar': board['board_vol_scalar'],
        'quality': {
            'residual_rms': board['residual_rms'],
            'residual_by_pair': board['residual_by_pair'],
            'flagged_pairs': flagged,
            'excluded_pairs': flagged,
            'board_pairs': len(usable),
            'partial_sessions': partial,
            'sum_check': sum_check,
            'live_exclusion': live_note,
            'spread_detail': spread_detail,
        },
    }
    out.update(asof_semantics(out['data_through_session'], ts_asof))
    return out


def get_env_state(asof: str | pd.Timestamp | None = None) -> dict:
    """§5.2 环境层。HG/VIX/XAU/XTI/US500 全部排除未完成会话后参与
    5 日斜率与环境状态（partial 行只在 L1 留痕，不进计算）。"""
    symbols = ['HG', 'VIX'] + config.ENV_MT5
    d1_map = _norm_d1_map(symbols)
    missing = set(symbols) - set(d1_map)
    if missing:
        raise RuntimeError(f'缺 D1 数据: {missing}（先跑 pipeline build）')
    ts_asof = (pd.Timestamp(asof) if asof is not None else _asof_now())
    if ts_asof.tzinfo is None:
        ts_asof = ts_asof.tz_localize('UTC')
    else:
        ts_asof = ts_asof.tz_convert('UTC')
    d1_map = {s: complete_sessions(df, ts_asof) for s, df in d1_map.items()}
    dropped = {s: n for s, n in ((s, len(df)) for s, df in d1_map.items()) if n == 0}
    if dropped:
        raise RuntimeError(f'完整会话过滤后为空: {dropped}')
    env = compute_env(d1_map)
    # asof/effective_session 由 compute_env 从对齐后共同会话生成，此处不再用
    # 未对齐的 XAUUSD 末根覆写（节假日交集落后时防止虚报数据时点）
    env['sources']['copper'] = 'ibkr:HG连续(比例回调, roll_on 到期规则活跃腿)'
    env.update(asof_semantics(env.get('effective_session'), ts_asof))
    return env


def get_pair_context(symbol: str, tf: str = 'H1') -> dict:
    """§5.3 位置层 pair context（P2-6 审计补齐）。

    - 数据不足：< 30 根 H1 直接报错（无法计算 ATR/ADX）；30~250 根标注
      lookback_limited。
    - segment 边界：H1 norm 不带 segment_id，以「尾部 250 根内最大相邻缺口
      > 4 天」近似检测并给 segment_warning（指标跨断点时读数可疑）。
    - 无前视：H1 在采集层已剔除进行中 bar，asof 即末根已收盘 bar 时刻。
    - spread 新鲜度（与 V9/API 同门）：≤120s tick 快照 ask−bid → bar 级
      copy_rates → ≤120s live points → unavailable。
    - ACS/RCS 完整定义不在框架文档内 → 恒显式 status=proxy（不冒称）。"""
    if tf != 'H1':
        raise ValueError('本框架 H1/H4 全走 MT5（附录 B），pair context 仅支持 H1')
    if not storage.norm_exists(symbol, 'H1'):
        raise RuntimeError(f'缺 {symbol} H1（先跑 pipeline build）')
    h1 = storage.read_norm(symbol, 'H1')
    if len(h1) < 30:
        raise RuntimeError(f'{symbol} H1 仅 {len(h1)} 根（<30），无法计算 ATR/ADX')
    ctx = build_pair_context(h1, symbol, tf)

    lookback = h1.tail(250)
    gaps_h = lookback['ts_utc'].diff().dt.total_seconds().dropna() / 3600.0
    max_gap_h = float(gaps_h.max()) if len(gaps_h) else 0.0
    ctx['segment_warning'] = bool(max_gap_h > 96.0)
    ctx['max_tail_gap_hours'] = round(max_gap_h, 1)
    ctx['lookback_limited'] = bool(len(h1) < 250)
    ctx['bars_available'] = int(len(h1))

    # 真实点差（120s 门统一）：tick 快照 → bar 级 → 新鲜 live points → unavailable
    ticks, _cap = _tick_snapshot()
    inst = _inst_meta()
    meta = inst.get(symbol) if isinstance(inst.get(symbol), dict) else {}
    point = meta.get('point')
    ctx['spread_now'], ctx['spread_source'] = None, 'unavailable'
    if ticks and symbol in ticks:
        ctx['spread_now'] = float(ticks[symbol]['spread_price'])
        ctx['spread_source'] = 'tick_snapshot(ask-bid)'
    elif 'spread' in h1.columns and float(h1['spread'].iloc[-1]) > 0 and point:
        ctx['spread_now'] = float(h1['spread'].iloc[-1]) * point
        ctx['spread_source'] = 'mt5_copy_rates_bar'
    else:
        cap = inst.get('_captured_at_utc')
        fresh = False
        if cap:
            c = pd.Timestamp(cap)
            c = c.tz_localize('UTC') if c.tzinfo is None else c.tz_convert('UTC')
            now = _asof_now()
            fresh = (now - c) <= pd.Timedelta(seconds=120)
        live = meta.get('spread_points_live')
        if fresh and live is not None and point:
            ctx['spread_now'] = live * point
            ctx['spread_source'] = 'live_snapshot(symbol_info)'
    return ctx
