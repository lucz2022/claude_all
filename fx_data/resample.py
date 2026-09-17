"""§4.2 统一日切重建 D1 + 日历对齐 + VIX 会话对齐。"""
import numpy as np
import pandas as pd

CANONICAL_CUT_UTC = 22


def rebuild_d1(h1: pd.DataFrame, cut_hour: int = CANONICAL_CUT_UTC,
               expected_bars: int = 24) -> pd.DataFrame:
    """把 H1 按统一日切重新聚合成 D1。
    会话定义：[D-1 cut, D cut)，标签 session_date = D（会话结束日）。
    L1 schema（§3.1）：source / price_kind 自 H1 透传，roll_flag 非期货恒 False。"""
    df = h1.set_index('ts_utc').sort_index()

    # 会话起始日 = (ts - cut).floor('D')；标签取结束日 = +1 天
    session_start = (df.index - pd.Timedelta(hours=cut_hour)).floor('D')
    df = df.assign(session_date=(session_start + pd.Timedelta(days=1)).date)

    src_series = df['source'] if 'source' in df else None
    src = (','.join(sorted(src_series.unique())) if src_series is not None
           else 'unknown')
    pk = df['price_kind'].iloc[0] if 'price_kind' in df else 'unknown'
    agg = df.groupby('session_date').agg(
        ts_utc=('open', lambda s: s.index[0]),   # 会话首根 H1 的起始时间
        open=('open', 'first'),
        high=('high', 'max'),
        low=('low', 'min'),
        close=('close', 'last'),
        volume=('volume', 'sum'),
        bars_in_session=('close', 'size'),
    )
    if src_series is not None:   # 行级 source（含 mt5_reconstructed）按会话计数
        is_recon = src_series.eq('mt5_reconstructed')
        agg['reconstructed_bars'] = is_recon.groupby(
            [df['session_date'].values]).sum().astype(int)
    # MT5 copy_rates 的 spread 列（points）：会话取中位数；全 0 时保持 0 由上层回退
    if 'spread' in df.columns:
        agg['spread_points'] = df.groupby('session_date')['spread'].median()
    # 残缺会话标记（夏令时切换、半日市、数据缺口）
    agg['partial'] = agg['bars_in_session'] < expected_bars
    agg['volume'] = agg['volume'].replace(0, np.nan)
    agg['source'] = src
    agg['price_kind'] = pk
    agg['roll_flag'] = False
    return agg.reset_index()


def drop_weekend_shells(d1: pd.DataFrame) -> pd.DataFrame:
    """剔除周末日标签的壳会话（22:00Z 日切下承接周日重开首 bar 的 Sat/Sun 标签行）。

    这些 1-2 bar 会话是日切定义的固有产物而非交易日：文档 §2.4 的 FX 模型是
    周一~周五 5 个交易日。剔除后 D1 即规范交易日历（bar 仍完整保留在 H1 L1）。
    周五 23/24 根与 DST 日 23/25 根的工作日会话是常态，不受影响。"""
    wd = pd.to_datetime(d1['session_date']).dt.dayofweek
    return d1[wd < 5].reset_index(drop=True)


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


def _us_dst_active(d: pd.Timestamp) -> bool:
    """美国 DST：3 月第二个周日 ~ 11 月第一个周日（CBOE 芝加哥遵循）。
    日期粒度判定（与 tz 无关，输入先归一化为 naive 日期）。"""
    d = pd.Timestamp(d)
    if d.tzinfo is not None:
        d = d.tz_convert('UTC').tz_localize(None)
    y = d.year

    def nth_sunday(month: int, n: int) -> int:
        first = pd.Timestamp(year=y, month=month, day=1)
        first_sunday = 1 + (6 - first.dayofweek) % 7
        return first_sunday + 7 * (n - 1)

    start = pd.Timestamp(year=y, month=3, day=nth_sunday(3, 2))
    end = pd.Timestamp(year=y, month=11, day=nth_sunday(11, 1))
    return start <= d.normalize() < end


# VIX 锚点规则（实测 2025-09~2026-09，501/501 个交易日无例外）：
# - 首恒 bar：夏令时 07:15Z / 冬令时 08:15Z（CBOE 早盘计算起始，分钟恒 :15）；
# - 末 bar：正常 20:00Z(夏)/21:00Z(冬)（15:15 CT 官方收盘所在小时）；
#   假期早收允许（实测 17/18/12 点各数例），但不得晚于正常收盘小时；
# - 分钟锚点 ⊆ {0, 15, 30}（:15=日首、:30=RTH 13:30/14:30 开盘），每日非 :00
#   bar ≤ 2 根；其余一律 :00 整点（小时步长）。
def vix_anchor_errors(vix_h1: pd.DataFrame,
                      cut_hour: int = CANONICAL_CUT_UTC) -> list[str]:
    """VIX 时间锚点校验，返回错误清单（空=通过）。

    覆盖：周末 bar、会话标签≠自身日期（cut 后错标）、分钟锚点、日窗口、
    首 bar 精确时刻、末 bar 不晚于正常收盘小时。整日错标 / 30 分钟整体偏移 /
    周六日内 bar 均会被抓住。"""
    errors: list[str] = []
    ts = pd.DatetimeIndex(vix_h1['ts_utc']).sort_values()
    wk = ts[ts.dayofweek >= 5]
    if len(wk):
        errors.append(f'周末bar {len(wk)} 根（如 {wk[0]}）')
    label = ((ts - pd.Timedelta(hours=cut_hour)).floor('D')
             + pd.Timedelta(days=1)).date
    mism = sum(1 for l, t in zip(label, ts) if str(l) != str(t.date()))
    if mism:
        errors.append(f'会话标签≠自身日期 {mism} 根')
    idx = ts
    for day in sorted(set(idx.date)):
        g = idx[idx.date == day]
        dst = _us_dst_active(pd.Timestamp(str(day)))
        open_hm = (7, 15) if dst else (8, 15)
        close_h = 20 if dst else 21
        minutes = set(g.minute)
        if not minutes <= {0, 15, 30}:
            errors.append(f'{day} 分钟锚点异常 {sorted(minutes)}')
        first, last = g[0], g[-1]
        if (first.hour, first.minute) != open_hm:
            errors.append(f'{day} 首bar {first.strftime("%H:%M")} ≠ '
                          f'{open_hm[0]:02d}:{open_hm[1]:02d}')
        if last.hour > close_h:
            errors.append(f'{day} 末bar {last.strftime("%H:%M")} 晚于 {close_h}:00')
        n_nz = sum(1 for t in g if t.minute != 0)
        if n_nz > 2:
            errors.append(f'{day} 非:00 bar {n_nz} > 2')
        out = [t for t in g if not (open_hm[0] <= t.hour <= close_h)]
        if out:
            errors.append(f'{day} 日窗外 {len(out)} 根')
        if len(errors) > 8:
            break
    return errors


def vix_sessions_aligned(vix_h1: pd.DataFrame,
                         cut_hour: int = CANONICAL_CUT_UTC) -> bool:
    """VIX 会话对齐断言（替代文档 §4.2 的 shift_vix_one_session，ERRATA#1）。

    H1 时间戳是真实 bar 时刻，22:00Z 日切下全部落当日会话 → 无需 shift、
    无前视。断言见 vix_anchor_errors（工作日/标签/锚点/日窗口，不是恒真式）。"""
    return not vix_anchor_errors(vix_h1, cut_hour)


def complete_sessions(d1: pd.DataFrame, asof: pd.Timestamp) -> pd.DataFrame:
    """排除未完成会话：会话结束时刻（session_date 当日 cut）> asof 的行剔除，
    并截断 asof 之后的会话（防上游数据超前）。L1 保留 partial 行（诚实），
    派生层/端点一律经本过滤消费。"""
    d = pd.to_datetime(d1['session_date'], utc=True)
    session_end = d + pd.Timedelta(hours=CANONICAL_CUT_UTC)
    keep = (d <= asof.normalize()) & (session_end <= asof)
    return d1[keep.to_numpy()].reset_index(drop=True)


def uniform_grid_check(h1: pd.DataFrame, availability: dict | None = None,
                       require_whole_hour: bool = True) -> pd.DataFrame:
    """V5：H1 网格检查。缺陷 = 非整点 bar / 重复 bar / 缺失应交易槽位。

    豁免仅依据 session_rules 的显式时段/节假日规则；规则未覆盖的缺口一律 FAIL。
    availability: {(date, hour): 有 bar 品种占比} —— 仅作诊断信息附注，
    不参与豁免判定（多品种共同缺 bar 不能自行证明休市）。"""
    from .session_rules import expected_slots_between

    ts = pd.DatetimeIndex(h1['ts_utc']).sort_values()
    epoch = pd.Timestamp('1970-01-01', tz='UTC')
    sec = (ts - epoch) / pd.Timedelta(seconds=1)
    rows, diags = [], []

    if require_whole_hour:               # MT5 价格源 bar 必须整点起始
        off = sec % 3600 != 0
        for t in ts[off]:
            rows.append((t, -1.0))       # -1 = 非整点 bar
            diags.append('')
    for t in ts[ts.duplicated()]:
        rows.append((t, -2.0))           # -2 = 重复 bar
        diags.append('')

    for i in range(len(ts) - 1):
        gap = sec[i + 1] - sec[i]
        if gap <= 3600.0:
            continue
        missing = expected_slots_between(ts[i], ts[i + 1])
        if not missing:
            continue                     # 显式规则声明的闭市（周末/登记节假日）
        note = ''
        if availability is not None:
            covered = sum(1 for s in missing
                          if availability.get((s.date(), s.hour), 0.0) >= 0.5)
            note = f'共识缺bar槽位 {covered}/{len(missing)}（诊断，不豁免）'
        # 第七轮 P2：缺陷时间 = **实际缺失的槽位本身**（此前误用缺口后首根
        # bar 的 ts[i+1]，缺 06:00 会错报 07:00）。每个 missing slot 一行。
        for s in missing:
            rows.append((s, float(gap)))
            diags.append(note)

    out = pd.DataFrame(rows, columns=['ts_utc', 'gap_seconds'])
    out['consensus_diag'] = diags
    return out
