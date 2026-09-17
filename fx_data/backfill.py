"""V5 缺口重建（第六轮建立，第七轮返工加固）：用同经纪商原始 tick/M1 重建
缺失 H1 bar。

第七轮修复（P1-1/P1-3）：
- **窗口防御**：M1 层的 time（服务器 epoch）必须换算真实 UTC 并严格过滤到
  [slot, slot+1h)——MT5 对无数据窗会返回**异日期杂散 bar**（实测 2026-03-31
  窗口返回 2026-06-11 的 bar），过滤后为空即 None/硬缺口，绝不把杂散价格
  伪装成目标 slot；tick 层同样过滤 + 排序 + 首末时刻校验。
- **证据不可变**：每次重建写 timestamped 证据 `__{slot}__{ts}.json`（同 slot
  再次重建保留历史）；load 取每 (sym, slot) 最新一份（规则：ts 最大）。
- **quarantine**：历史错误证据（异日期 M1）移入 _quarantine/ 并写明原因，
  保留审计痕迹，不删除。

规则（不变）：OHLCV 全由窗内真实报价聚合；禁止插值/前值；无数据 → 硬缺口。
"""
import json
import shutil

import numpy as np
import pandas as pd

from . import config
from .mt5_clock import raw_msc_to_utc, srv_request_naive

RECON_DIR = config.DIR_RAW / 'mt5_reconstructed'
QUARANTINE_DIR = RECON_DIR / '_quarantine'


def _default_fetcher(symbol, naive_from, naive_to):
    import MetaTrader5 as mt5
    t = mt5.copy_ticks_range(symbol, naive_from, naive_to, mt5.COPY_TICKS_ALL)
    if t is None or len(t) == 0:
        return None
    df = pd.DataFrame(t)
    df['ts_utc'] = raw_msc_to_utc(df['time_msc'])
    return df[['ts_utc', 'bid', 'ask', 'time_msc']]


def _default_m1_fetcher(symbol, naive_from, naive_to):
    import MetaTrader5 as mt5
    r = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, naive_from, naive_to)
    if r is None or len(r) == 0:
        return None
    df = pd.DataFrame(r)
    # M1 time 为服务器 epoch 秒 → 真实 UTC（防异日期杂散 bar 的关键换算）
    df['ts_utc'] = raw_msc_to_utc(df['time'].astype('int64') * 1000)
    return df


def _in_window(df: pd.DataFrame, slot, end) -> pd.DataFrame:
    """严格过滤 [slot, slot+1h) 并按时间排序——窗口防御的单一出口。"""
    ok = df[(df['ts_utc'] >= slot) & (df['ts_utc'] < end)].sort_values('ts_utc')
    return ok


def find_missing_slots(h1: pd.DataFrame) -> list[pd.Timestamp]:
    """在 H1 相邻 bar 之间找「规则认定应交易却缺失」的整点槽位。"""
    from .session_rules import expected_slots_between
    ts = pd.DatetimeIndex(h1['ts_utc']).sort_values()
    missing = []
    for i in range(len(ts) - 1):
        for s in expected_slots_between(ts[i], ts[i + 1]):
            missing.append(s)
    return missing


def reconstruct_slot(symbol: str, slot_utc, fetcher=None,
                     m1_fetcher=None) -> dict | None:
    """从窗 [slot, slot+1h) 的原始数据重建该 H1 bar：**tick 优先，M1 回退**。

    两层都做窗口防御：返回数据必须换算真实 UTC 并过滤进窗；过滤后为空 →
    None（硬缺口）。绝不接受窗外的异日期杂散 bar。"""
    slot = pd.Timestamp(slot_utc)
    if slot.tzinfo is None:
        slot = slot.tz_localize('UTC')
    end = slot + pd.Timedelta(hours=1)
    f = fetcher or _default_fetcher
    df = f(symbol, srv_request_naive(slot), srv_request_naive(end))
    if df is not None and len(df):
        ok = _in_window(df, slot, end)
        if len(ok):
            bid = ok['bid'].astype(float)
            prov = {
                'symbol': symbol,
                'slot_utc': slot.isoformat().replace('+00:00', 'Z'),
                'source': 'mt5_reconstructed',
                'from_layer': 'ticks(COPY_TICKS_ALL, bid)',
                'tick_count': int(len(ok)),
                'minutes_covered': int(ok['ts_utc'].dt.floor('min').nunique()),
                'first_tick_utc': ok['ts_utc'].iloc[0].isoformat().replace('+00:00', 'Z'),
                'last_tick_utc': ok['ts_utc'].iloc[-1].isoformat().replace('+00:00', 'Z'),
                'volume_basis': 'tick_count',
                'no_interpolation': True,
            }
            return {
                'ts_utc': slot,
                'open': float(bid.iloc[0]), 'high': float(bid.max()),
                'low': float(bid.min()), 'close': float(bid.iloc[-1]),
                'volume': float(len(ok)), 'spread': 0,
                'source': 'mt5_reconstructed', 'price_kind': 'bid',
                'provenance': prov,
            }
    mf = m1_fetcher or _default_m1_fetcher
    m1 = mf(symbol, srv_request_naive(slot), srv_request_naive(end))
    if m1 is not None and len(m1):
        ok = _in_window(m1, slot, end)
        if len(ok):
            prov = {
                'symbol': symbol,
                'slot_utc': slot.isoformat().replace('+00:00', 'Z'),
                'source': 'mt5_reconstructed',
                'from_layer': 'M1(copy_rates_range, bid)',
                'm1_bars': int(len(ok)),
                'minutes_covered': int(len(ok)),
                'first_m1_utc': ok['ts_utc'].iloc[0].isoformat().replace('+00:00', 'Z'),
                'last_m1_utc': ok['ts_utc'].iloc[-1].isoformat().replace('+00:00', 'Z'),
                'volume_basis': 'sum(M1 tick_volume)',
                'no_interpolation': True,
            }
            return {
                'ts_utc': slot,
                'open': float(ok['open'].iloc[0]), 'high': float(ok['high'].max()),
                'low': float(ok['low'].min()), 'close': float(ok['close'].iloc[-1]),
                'volume': float(ok['tick_volume'].sum()), 'spread': 0,
                'source': 'mt5_reconstructed', 'price_kind': 'bid',
                'provenance': prov,
            }
    return None


def _evidence_path(sym: str, slot_utc: pd.Timestamp, stamp: str):
    """不可变 timestamped 证据路径：{SYM}__{slot}__{stamp}.json。"""
    s = slot_utc.isoformat().replace(':', '-').replace('+00:00', 'Z')
    return RECON_DIR / f'{sym}__{s}__{stamp}.json'


def reconstruct_all(h1_map: dict[str, pd.DataFrame],
                    fetcher=None) -> dict[str, list[dict]]:
    """对每个 symbol 的 H1 缺口尝试 tick/M1 重建。

    证据逐 slot 写 timestamped 文件（同 slot 重复重建保留历史，不覆盖）。"""
    RECON_DIR.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now('UTC').strftime('%Y%m%dT%H%M%S%fZ')
    out = {}
    for sym, df in h1_map.items():
        bars = []
        for slot in find_missing_slots(df):
            bar = reconstruct_slot(sym, slot, fetcher=fetcher)
            if bar is None:
                continue
            bars.append(bar)
            p = _evidence_path(sym, slot, stamp)
            p.write_text(json.dumps(
                {'bar': {k: (v.isoformat().replace('+00:00', 'Z')
                             if isinstance(v, pd.Timestamp) else v)
                         for k, v in bar.items() if k != 'provenance'},
                 'provenance': bar['provenance']},
                ensure_ascii=False, indent=2), encoding='utf-8')
        if bars:
            out[sym] = bars
    return out


def load_reconstructed() -> dict[str, pd.DataFrame]:
    """读取重建 bar → {sym: DataFrame}。

    选择规则（明确且可复现）：每 (sym, slot) 取**时间戳最大**的证据文件；
    兼容第六轮的固定文件名旧格式（视为一份早期证据，新格式优先）。"""
    import re
    if not RECON_DIR.exists():
        return {}
    cand: dict[tuple[str, str], tuple[str, dict]] = {}
    for p in sorted(RECON_DIR.glob('*.json')):
        try:
            rec = json.loads(p.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            continue
        prov = rec.get('provenance', {})
        sym = prov.get('symbol')
        slot = prov.get('slot_utc')
        if not sym or not slot:
            continue
        m = re.search(r'__(\d{8}T\d+Z)\.json$', p.name)
        stamp = m.group(1) if m else '00000000T000000Z'   # 旧固定名排最前
        key = (sym, slot)
        if key not in cand or stamp > cand[key][0]:
            cand[key] = (stamp, rec)
    rows_by_sym: dict[str, list[dict]] = {}
    for (sym, slot), (_stamp, rec) in cand.items():
        b = rec['bar']
        rows_by_sym.setdefault(sym, []).append({
            'ts_utc': pd.Timestamp(b['ts_utc']),
            'open': b['open'], 'high': b['high'], 'low': b['low'],
            'close': b['close'], 'volume': b['volume'], 'spread': b['spread'],
            'source': 'mt5_reconstructed', 'price_kind': 'bid',
        })
    frames = {}
    for s, rows in rows_by_sym.items():
        df = pd.DataFrame(rows)
        df['ts_utc'] = pd.to_datetime(df['ts_utc'], utc=True)   # 防 object 列
        frames[s] = df
    return frames


def quarantine_invalid_evidence(reason: str) -> list[str]:
    """把窗校验失败的证据移入 _quarantine/（保留审计痕迹，不删除）。

    判定：M1 层证据的 first/last 时刻不在其 slot 的 [slot, slot+1h) 内，
    或 tick 层同类问题。返回被隔离的文件名清单。"""
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    moved = []
    for p in sorted(RECON_DIR.glob('*.json')):
        try:
            rec = json.loads(p.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            continue
        prov = rec.get('provenance', {})
        slot = prov.get('slot_utc')
        if not slot:
            continue
        slot_ts = pd.Timestamp(slot)
        first = prov.get('first_m1_utc') or prov.get('first_tick_utc')
        last = prov.get('last_m1_utc') or prov.get('last_tick_utc')
        if first is None or last is None:
            # 旧格式 M1 证据无首末时刻（无法证明在窗内）——不可证即隔离。
            # 实测 MT5 对无数据窗返回异日期杂散 bar，M1 深度自 2026-06-11 起，
            # 早于该日期的 M1 证据必为杂散。
            if str(prov.get('from_layer', '')).startswith('M1'):
                dest = QUARANTINE_DIR / p.name
                shutil.move(str(p), str(dest))
                moved.append(p.name)
            continue
        f_ts, l_ts = pd.Timestamp(first), pd.Timestamp(last)
        in_win = (f_ts >= slot_ts and l_ts < slot_ts + pd.Timedelta(hours=1)
                  and f_ts <= l_ts)
        if not in_win:
            dest = QUARANTINE_DIR / p.name
            shutil.move(str(p), str(dest))
            moved.append(p.name)
    if moved:
        manifest = QUARANTINE_DIR / 'manifest.json'
        hist = json.loads(manifest.read_text(encoding='utf-8')) \
            if manifest.exists() else []
        hist.append({'quarantined_at': pd.Timestamp.now('UTC').isoformat(),
                     'reason': reason, 'files': moved})
        manifest.write_text(json.dumps(hist, ensure_ascii=False, indent=2),
                            encoding='utf-8')
    return moved
