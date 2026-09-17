"""§4.1 MT5 导出。H1 原始落盘 + 服务器时区动态检测。D1 一律由 H1 重建。

审计修正：
- copy_rates 的 spread 列（int, points）保留——实测 IC Markets 历史 H1 全为 0，
  不伪造：非零时用 bar 级中位数，全零回退采集时刻的实时 symbol_info().spread，
  并在 sidecar 标注来源。
- 采集失败不再静默跳过沿用旧数据：默认 strict，任一品种失败即抛错
  （--allow-partial 显式放宽）。
"""
import time

import pandas as pd

from . import config

try:
    import MetaTrader5 as mt5
except ImportError:  # 无 MT5 终端的环境（如纯回测机）
    mt5 = None

from . import storage

INSTRUMENT_META_FILE = 'instrument_meta.json'
TICK_SNAPSHOT_FILE = 'tick_snapshot.json'


def server_utc_offset_hours(probe: str = 'EURUSD') -> int:
    """MT5 的 tick.time 是服务器时间按 UTC 语义编码的 epoch 秒。
    与真实 UTC epoch 相减即得偏移。必须动态调用——夏令时会改变它。"""
    if not mt5.symbol_select(probe, True):
        raise RuntimeError(f'cannot select {probe}')
    tick = mt5.symbol_info_tick(probe)
    if tick is None:
        raise RuntimeError('no tick')
    return int(round((tick.time - time.time()) / 3600.0))


def _instrument_meta(symbol: str) -> dict:
    si = mt5.symbol_info(symbol)
    if si is None:
        raise RuntimeError(f'no symbol_info for {symbol}')
    return {
        'point': si.point,
        'digits': si.digits,
        'spread_points_live': int(si.spread),      # 实时点差（采集时刻快照）
        'trade_mode': int(si.trade_mode),
    }


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
    # spread = copy_rates 自带的每 bar 点差（points）。实测本经纪商该列退化
    # （多数 0/1、非零值被 rollover 尖峰污染），保留原值仅供取证；
    # V9 容忍度以采集时实时快照为主源。
    df['spread'] = df['spread'].astype('int64')
    df = df[['ts_utc', 'open', 'high', 'low', 'close', 'volume', 'spread']]
    df['source'] = 'mt5'
    df['price_kind'] = 'bid'          # MT5 默认 bid 系；全集统一，勿混用
    df = df.sort_values('ts_utc').reset_index(drop=True)
    # 剔除进行中的 bar：copy_rates 末根是当前未收盘小时，各品种末 tick 时刻
    # 不同会污染三角闭合/端点计算。只落盘已收盘 bar。
    now = pd.Timestamp.utcnow()
    closed = df['ts_utc'] + pd.Timedelta(hours=1) <= now
    return df[closed.to_numpy()].reset_index(drop=True)


def _tick_snapshot(symbols: list[str], rounds: int = 3,
                   now_ms: float | None = None) -> dict:
    """同刻 tick 快照（bid/ask）——V9 同刻三角与真实点差的基础。

    绝对年龄的锚定（审计第四轮 P1-1 修复）：
    - tick_time_ms 是**服务器 epoch**。偏移锚用**日历**（IC Markets 服务器
      EET/EEST = UTC+2 冬 / UTC+3 夏），与任何 tick 的新鲜度无关——此前
      offset=max(tick_time−墙钟) 隐含「最新 tick 年龄≈0」，全市场共同陈旧
      （如周末抓旧报价）时每个 age 都是 0，假通过；
    - 探针交叉验证：若最新 tick 隐含的整小时偏移与日历差 ≥2h 且残差 <10min
      （说明探针新鲜而经纪商时区与日历不符），标 `_offset_source=conflict`，
      消费方必须按不可验证处理；
    - age_ms = (抓取墙钟 + 日历偏移) − tick_time。新鲜 tick ≈ 读取延迟
      （0~几百 ms，正值）；陈旧为大正值；负超 60s = tick 超前墙钟，记时钟异常。
    `_tick_span_ms` = 各符号 tick 时刻 max−min（真实行情跨度；
    `_last_round_span_ms` 只是末轮 API 遍历耗时）。now_ms 可注入墙钟（测试）。
    spread_price = ask − bid（真实可成交点差，价单位）。"""
    import time as _time

    def _wall_ms() -> float:
        return now_ms if now_ms is not None else _time.time() * 1000.0

    best: dict[str, dict] = {}
    last_round_span_ms = None
    for _ in range(rounds):
        t0 = _wall_ms()
        cur = {}
        for s in symbols:
            t = mt5.symbol_info_tick(s)
            wall_ms = _wall_ms()
            if t is None or t.bid <= 0 or t.ask <= 0:
                continue
            cur[s] = {
                'bid': float(t.bid), 'ask': float(t.ask),
                'spread_price': float(t.ask - t.bid),
                'tick_time_ms': int(t.time_msc),   # 服务器 epoch ms
                '_fetch_wall_ms': wall_ms,
            }
        last_round_span_ms = round(_wall_ms() - t0, 1)
        for s, v in cur.items():
            if s not in best or v['tick_time_ms'] > best[s]['tick_time_ms']:
                best[s] = v

    from .session_rules import eu_dst_active
    base_wall = _wall_ms()
    off_h = 3 if eu_dst_active(pd.Timestamp(base_wall / 1000, unit='s', tz='UTC')) else 2
    offset_ms = float(off_h) * 3_600_000.0

    conflict = False
    probe_raws = [(v['tick_time_ms'] - v['_fetch_wall_ms']) / 1000.0
                  for v in best.values()]
    if probe_raws:
        implied_h = round(max(probe_raws) / 3600.0)
        resid = max(probe_raws) - implied_h * 3600.0
        # |implied_h|>14 说明探针本身大龄（全陈旧场景）——日历锚仍有效，不判冲突
        if abs(implied_h) <= 14 and abs(resid) < 600 \
                and abs(implied_h - off_h) >= 2:
            conflict = True

    clock_anomaly = []
    for s, v in best.items():
        v['age_ms'] = round(v['_fetch_wall_ms'] + offset_ms - v['tick_time_ms'], 1)
        del v['_fetch_wall_ms']
        if v['age_ms'] < -60_000:
            clock_anomaly.append(s)
    times = [v['tick_time_ms'] for v in best.values()]
    snap = dict(best)
    snap['_server_offset_ms'] = offset_ms
    snap['_offset_hours'] = off_h
    snap['_offset_source'] = 'conflict' if conflict else 'calendar(EU-DST)'
    snap['_clock_anomaly'] = clock_anomaly
    snap['_tick_span_ms'] = (max(times) - min(times)) if times else None
    snap['_last_round_span_ms'] = last_round_span_ms
    snap['_fetched_at_ms'] = int(base_wall)
    return snap


def export_all(h1_count: int = 24 * 400, symbols: list[str] | None = None,
               save: bool = True, allow_partial: bool = False
               ) -> tuple[dict[str, pd.DataFrame], dict]:
    """采集 MT5 全集并落盘 L0 raw（快照式，不覆盖历史快照）。返回 ({sym: df}, meta)。

    附带同刻 tick 快照（bid/ask/spread_price）供 V9 同刻三角与 V10 真实点差。
    allow_partial=False（默认）：任一品种失败即抛错，防止静默沿用旧数据。"""
    if mt5 is None:
        raise RuntimeError('MetaTrader5 包不可用（未安装 MT5 终端）')
    if not mt5.initialize():
        raise RuntimeError(f'mt5 init failed: {mt5.last_error()}')
    try:
        offset = server_utc_offset_hours()
        info = mt5.terminal_info()
        meta = {
            'collected_at_utc': pd.Timestamp.utcnow().isoformat(),
            'canonical_cut_utc': config.CANONICAL_CUT_UTC,
            'mt5_server_utc_offset': offset,
            'mt5_server_name': getattr(info, 'name', None),
            'symbols': [],
            'price_kind': 'bid',
            'source': 'mt5',
            'pipeline_version': config.PIPELINE_VERSION,
        }
        out, inst_meta, failures = {}, {}, []
        sym_list = symbols or config.ALL_MT5_SYMBOLS
        for sym in sym_list:
            try:
                df = fetch_h1(sym, h1_count, offset)
                out[sym] = df
                inst_meta[sym] = _instrument_meta(sym)
                meta['symbols'].append(sym)
                if save:
                    storage.write_raw(df, 'mt5', sym, 'H1')
            except RuntimeError as e:
                failures.append(f'{sym}: {e}')
                print(f'[FAIL] {sym}: {e}')
        if failures and not allow_partial:
            raise RuntimeError(
                f'MT5 采集失败 {len(failures)} 个品种（不允许静默沿用旧数据）: '
                f'{failures}；如确需部分采集请显式 --allow-partial')
        if save:
            inst_meta['_captured_at_utc'] = pd.Timestamp.utcnow().isoformat()
            storage.write_sidecar(inst_meta, 'mt5', INSTRUMENT_META_FILE)
            storage.write_meta(meta)
        # 第六轮：同步 mid 通道回填 + H1 缺口 tick 重建（失败可见，不静默）
        try:
            from .syncmid import TRIO, backfill_sync_mid
            dates = sorted({str(d) for s, df in out.items()
                            for d in pd.DatetimeIndex(df['ts_utc']).normalize().date})
            store = backfill_sync_mid(dates[-140:])
            print(f"[SYNCMID] 三腿 {TRIO}: {len(store)} 会话, "
                  f"{int(store['verified'].sum())} verified "
                  f"(coverage {store['session_date'].iloc[0]}.."
                  f"{store['session_date'].iloc[-1]})", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f'[SYNCMID][WARN] 回填失败（V4 将不可验证）: {e}', flush=True)
        try:
            from .backfill import reconstruct_all
            from . import storage as _st
            h1_map = {}
            for sym in out:
                # 重建范围 = 28 FX 对：环境品种（XAU/XTI/XBR/US500）的 21:00Z
                # 缺 bar 是其自身日内休市日程（README 已知限制），非数据缺口，
                # 重建会改变 env 层 D1 收盘语义——不得扩展
                if sym in config.PAIRS_28 and _st.norm_exists(sym, 'H1'):
                    h1_map[sym] = _st.read_norm(sym, 'H1')
            recon = reconstruct_all(h1_map)
            n = sum(len(v) for v in recon.values())
            print(f"[RECON] tick 重建 {n} 根 "
                  f"({len(recon)} 品种: {sorted(recon) or '无'})", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f'[RECON][WARN] 重建失败（V5 相关缺口保留）: {e}', flush=True)
        # tick 快照在 collect **末尾**写入：SYNCMID/RECON 耗时数分钟，
        # 早写会让链式 validate 的 120s 新鲜度门永远过期
        try:
            ticks = _tick_snapshot(sym_list)
            ticks['_captured_at_utc'] = pd.Timestamp.utcnow().isoformat()
            storage.write_sidecar(ticks, 'mt5', TICK_SNAPSHOT_FILE)
        except Exception as e:  # noqa: BLE001
            print(f'[TICKSNAP][WARN] {e}', flush=True)
        return out, meta
    finally:
        mt5.shutdown()
