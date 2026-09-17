"""V4 同步 mid 日切价通道（第六轮建立，第七轮返工加固）。

第七轮修复（P1-2/P1-3）：
- **规范会话日历**：session_dates 必须来自规范 FX D1 会话（周一~五）；周日壳
  会话不参与 coverage/残差/drift——backfill 与 evaluate 双重防御。
- **raw 不可变**：每次采集写 timestamped 快照 snap__{ts}.parquet（同
  session_date 的两次采集保留两份 raw）；另维护 latest 合并视图
  （EURCHF_TRIO__D1.parquet，明确标注 derived）。latest 规则：同
  session_date 取**快照时间戳最大**的行。迁移保留旧文件并记录 lineage。

其余语义不变：两段式自适应目标（cut 前 90min 预扫描 → T*=三腿最后报价时刻，
周一~四≈21:59:5xZ、周五≈20:56:5xZ 提前收市自适应）；任一腿缺失 / 距 T*>60s /
跨腿 skew>2s → 该会话 UNVERIFIED；覆盖不足 → 整体 UNVERIFIED。
"""
import json
import os
import re
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

from . import config
from .mt5_clock import raw_msc_to_utc, srv_request_naive

TRIO = ['EURCHF', 'EURUSD', 'USDCHF']
SYNC_PRESCAN = pd.Timedelta(minutes=90)     # cut 前预扫描窗（覆盖周五 ~20:57Z 提前收市）
SYNC_SKEW_MAX_MS = 2000
SYNC_AGE_MAX_MS = 60_000
SYNC_DIR = config.DIR_RAW / 'mt5' / 'syncmid'
SYNC_LATEST = SYNC_DIR / 'EURCHF_TRIO__D1.parquet'
SYNC_PROV_LATEST = SYNC_DIR / 'EURCHF_TRIO__provenance.json'


def canonical_sessions(dates) -> list[str]:
    """规范 FX 会话过滤：周一~五（周日壳/周末标签一律排除）。防御之一。"""
    out = []
    for d in dates:
        if pd.Timestamp(str(d)).dayofweek < 5:
            out.append(str(d))
    return sorted(set(out))


def _default_fetcher(symbol, naive_from, naive_to):
    import MetaTrader5 as mt5
    t = mt5.copy_ticks_range(symbol, naive_from, naive_to, mt5.COPY_TICKS_ALL)
    if t is None or len(t) == 0:
        return None
    df = pd.DataFrame(t)
    df['ts_utc'] = raw_msc_to_utc(df['time_msc'])
    return df[['ts_utc', 'bid', 'ask', 'time_msc']]


def fetch_sync_mid(cut_utc, symbols=TRIO, prescan=SYNC_PRESCAN,
                   skew_max_ms=SYNC_SKEW_MAX_MS, age_max_ms=SYNC_AGE_MAX_MS,
                   fetcher=None, now_utc=None):
    """会话 cut 前的三腿同步 mid（两段式自适应目标）。返回
    {verified, legs, skew_ms, target_utc, reason}。

    tick 全部 < cut（结构性无前视）。"""
    fetcher = fetcher or _default_fetcher
    cut = pd.Timestamp(cut_utc)
    if cut.tzinfo is None:
        cut = cut.tz_localize('UTC')
    last = {}
    for sym in symbols:
        df = fetcher(sym, srv_request_naive(cut - prescan),
                     srv_request_naive(cut))
        last[sym] = None if (df is None or not len(df)) else df.iloc[-1]
    missing = [s for s in symbols if last[s] is None]
    if missing:
        return {'verified': False, 'legs': {s: None for s in symbols},
                'skew_ms': None, 'target_utc': None,
                'reason': f'缺腿tick: {missing}'}
    t_star = max(last[sym]['ts_utc'] for sym in symbols)
    legs = {}
    for sym in symbols:
        r = last[sym]
        legs[sym] = {
            'tick_utc': r['ts_utc'].isoformat().replace('+00:00', 'Z'),
            'tick_ms_raw': int(r['time_msc']),
            'bid': float(r['bid']), 'ask': float(r['ask']),
            'mid': (float(r['bid']) + float(r['ask'])) / 2.0,
            'age_ms': (t_star - r['ts_utc']).total_seconds() * 1000.0,
        }
    times = [legs[s]['tick_ms_raw'] for s in symbols]
    skew_ms = max(times) - min(times)
    stale = [s for s in symbols if legs[s]['age_ms'] > age_max_ms]
    if stale:
        return {'verified': False, 'legs': legs, 'skew_ms': skew_ms,
                'target_utc': t_star.isoformat().replace('+00:00', 'Z'),
                'reason': f'陈旧>{age_max_ms // 1000}s: {stale}'}
    if skew_ms > skew_max_ms:
        return {'verified': False, 'legs': legs, 'skew_ms': skew_ms,
                'target_utc': t_star.isoformat().replace('+00:00', 'Z'),
                'reason': f'跨腿skew {skew_ms}ms>{skew_max_ms}ms'}
    return {'verified': True, 'legs': legs, 'skew_ms': skew_ms,
            'target_utc': t_star.isoformat().replace('+00:00', 'Z'),
            'reason': ''}


SNAP_SCHEMA_VERSION = '2'
SYNC_QUARANTINE = SYNC_DIR / '_quarantine'
_EVENT_NAME_RE = re.compile(r'^snap__EURCHF_TRIO__(?P<base>\d{8}T\d+?)'
                            r'(?:-(?P<seq>\d+))?Z\.(?P<ext>parquet|json)$')


def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _format_event_id(base: str, seq: int = 0) -> str:
    """唯一 event_id formatter：{base}Z 或 {base_without_Z}-{n}Z。

    生成与解析共用同一约定（第九轮修复）：碰撞后缀插在 Z **之前**
    （例 20260917T030000000001-1Z），保证与 _EVENT_NAME_RE 完全互逆，
    杜绝第八轮 `...Z-1` 文件名被解析器静默丢弃的问题。"""
    core = base[:-1] if base.endswith('Z') else base
    return f'{core}-{seq}Z' if seq > 0 else f'{core}Z'


def _parse_event_name(name: str):
    """文件名 → (base, seq, ext)；不匹配约定 → None。

    排序键 = (base, seq)：base 为定宽时间戳，字典序即时间序；seq 表碰撞次序。
    「同 session_date 取最新采集」由此键决定，不依赖裸文件名字典序碰巧。"""
    m = _EVENT_NAME_RE.match(name)
    if not m:
        return None
    return m.group('base'), int(m.group('seq') or 0), m.group('ext')


def _event_sort_key(eid_pair):
    base, seq = eid_pair[0], eid_pair[1]
    return (base, seq)


def _atomic_write_pair(df: pd.DataFrame, prov: dict, event_id: str
                       ) -> tuple[Path, Path]:
    """原子写不可变事件对：snap__{event_id}.parquet + 同名 .json。

    - 临时文件名含 **PID+UUID**（相同 event_id 的并发 writer 不会互相覆盖
      临时文件/串写哈希）；写好后 os.rename 到最终路径——Windows 上目标
      已存在即 FileExistsError，捕获后换下一个碰撞后缀重试，**绝不覆盖**；
    - parquet 先落位，sidecar **最后作为 commit marker** 落位：消费方只见
      「parquet+sidecar 齐且 sha256 一致」的完整事件，半事件不被消费。"""
    import uuid as _uuid
    base = event_id[:-1] if event_id.endswith('Z') else event_id
    last_err = None
    for seq in range(0, 65):
        eid = _format_event_id(base, seq)
        pq = SYNC_DIR / f'snap__EURCHF_TRIO__{eid}.parquet'
        js = SYNC_DIR / f'snap__EURCHF_TRIO__{eid}.json'
        uniq = f'{os.getpid()}.{_uuid.uuid4().hex[:8]}'
        tmp_pq = SYNC_DIR / f'.tmp__{eid}__{uniq}.parquet'
        tmp_js = SYNC_DIR / f'.tmp__{eid}__{uniq}.json'
        try:
            df.to_parquet(tmp_pq, index=False)
            prov = dict(prov)
            prov['event_id'] = eid
            prov['parquet_file'] = pq.name
            prov['parquet_sha256'] = _sha256(tmp_pq)
            tmp_js.write_text(json.dumps(prov, ensure_ascii=False, indent=2),
                              encoding='utf-8')
            os.rename(tmp_pq, pq)   # 目标存在 → FileExistsError → 换后缀重试
            os.rename(tmp_js, js)   # commit marker 最后落位
            return pq, js
        except FileExistsError as e:
            last_err = e
            for t in (tmp_pq, tmp_js):
                try:
                    t.unlink()
                except OSError:
                    pass
            continue
        finally:
            for t in (tmp_pq, tmp_js):
                try:
                    t.unlink()
                except OSError:
                    pass
    raise RuntimeError(f'event_id 碰撞重试超限: {last_err}')


def _valid_events() -> tuple[list[tuple[tuple[str, int], Path]], list[str]]:
    """返回 ((base, seq), parquet) 完整事件列表（按 (base, seq) 升序）+
    被拒绝事件诊断清单。不完整事件绝不被 latest 消费。

    第十轮修复：以 parquet 与 json 文件名的**并集**建立候选——
    - JSON-only（缺 parquet 的反向半事件）/ parquet-only（缺 sidecar）均显式
      rejected，不再静默忽略；
    - 任一侧文件名不符 canonical event_id（含旧式 ...Z-1）→ 显式 rejected；
    - 每个坏事件只报一次（以 canonical 事件为键去重），顺序确定
      （坏名称按文件名排序在前，canonical 事件按 (base, seq) 升序）。"""
    events, rejected = [], []
    if not SYNC_DIR.exists():
        return events, rejected
    bad_names = []
    by_event: dict[tuple[str, int], dict] = {}
    for p in sorted(SYNC_DIR.glob('snap__EURCHF_TRIO__*')):
        if p.suffix not in ('.parquet', '.json'):
            continue
        parsed = _parse_event_name(p.name)
        if parsed is None:
            bad_names.append(p.name)
            continue
        base, seq, ext = parsed
        slot = by_event.setdefault((base, seq), {'parquet': None, 'json': None})
        slot[ext] = p
    for name in sorted(set(bad_names)):
        rejected.append(f'{name}: 文件名不符 event_id 约定（拒绝消费）')
    for key in sorted(by_event, key=_event_sort_key):
        eid = _format_event_id(key[0], key[1])
        slot = by_event[key]
        pq, js = slot['parquet'], slot['json']
        if pq is None:
            rejected.append(f"snap__EURCHF_TRIO__{eid}.json: "
                            f'缺 parquet（半事件，拒绝消费）')
            continue
        if js is None:
            rejected.append(f'{pq.name}: 缺 sidecar（半事件，拒绝消费）')
            continue
        try:
            prov = json.loads(js.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError) as e:
            rejected.append(f'{js.name}: sidecar 损坏（{e}），拒绝消费')
            continue
        if prov.get('parquet_sha256') != _sha256(pq):
            rejected.append(f'{pq.name}: sha256 不匹配（内容被改写），拒绝消费')
            continue
        if prov.get('parquet_file') != pq.name or prov.get('event_id') != eid:
            rejected.append(f'{pq.name}: sidecar 与 parquet 不配对，拒绝消费')
            continue
        events.append((key, pq))
    return events, rejected


def _snapshots() -> list[Path]:
    """兼容旧接口：完整事件的 parquet 路径（事件序升序）。"""
    return [p for _k, p in _valid_events()[0]]


def audit_events() -> dict:
    """事件审计：完整/被拒清单，供验证与报告使用。"""
    events, rejected = _valid_events()
    return {'complete_events': [p.name for _k, p in events],
            'complete_event_ids': [_format_event_id(k[0], k[1])
                                   for k, _p in events],
            'rejected': rejected}


def _rebuild_latest() -> pd.DataFrame | None:
    """latest 合并视图（derived，明确非 raw）：按事件序 (base, seq) 依次合并，
    同 session_date 由**后一事件**覆盖（即真正最新的采集，含碰撞次序）——
    规则确定且可复现；仅消费完整事件。"""
    events, _rejected = _valid_events()
    if not events:
        return None
    merged = None
    for _key, p in events:
        df = pd.read_parquet(p)
        if merged is None:
            merged = df
        else:
            merged = pd.concat([merged[~merged['session_date'].isin(
                df['session_date'])], df])
    return merged.sort_values('session_date').reset_index(drop=True)


def backfill_sync_mid(session_dates, symbols=TRIO,
                      fetcher=None) -> pd.DataFrame | None:
    """按规范会话日回填同步 mid。

    - session_dates 先过滤为周一~五（canonical_sessions，防御一）；
    - 本次结果形成**不可变原子事件对**（parquet + 同 event_id 的 provenance
      sidecar，含 sha256；不覆盖、碰撞安全），随后重建 latest 视图。"""
    dates = canonical_sessions(session_dates)
    rows = []
    for d in dates:
        cut = pd.Timestamp(str(d), tz='UTC') + pd.Timedelta(hours=22)
        res = fetch_sync_mid(cut, symbols=symbols, fetcher=fetcher)
        row = {'session_date': str(d), 'target_utc': res.get('target_utc')}
        for sym in symbols:
            leg = res['legs'].get(sym)
            row[f'{sym}__mid'] = leg['mid'] if leg else np.nan
            row[f'{sym}__tick_utc'] = leg['tick_utc'] if leg else None
            row[f'{sym}__age_ms'] = leg['age_ms'] if leg else None
        row['skew_ms'] = res['skew_ms']
        row['verified'] = res['verified']
        row['reason'] = res['reason']
        rows.append(row)
    new = pd.DataFrame(rows)
    if not len(new):
        return _rebuild_latest()
    SYNC_DIR.mkdir(parents=True, exist_ok=True)
    prov = {
        'captured_at_utc': pd.Timestamp.now('UTC').isoformat(),
        'symbols': list(symbols),
        'session_dates': [r['session_date'] for r in rows],
        'rows': len(new),
        'target_rule': 'cut(22:00Z) 前 90min 预扫描 → T*=三腿最后报价时刻'
                       '（周一~四≈21:59:5xZ；周五≈20:56:5xZ 提前收市自适应）',
        'session_calendar': 'canonical Mon–Fri（周日壳/周末排除）',
        'prescan_min': int(SYNC_PRESCAN.total_seconds() // 60),
        'skew_max_ms': SYNC_SKEW_MAX_MS,
        'age_max_ms': SYNC_AGE_MAX_MS,
        'clock_semantics': 'from/to=服务器naive(UTC+offset), time_msc=服务器epoch '
                           '（实证 2026-09-16，三例收盘价精确匹配）',
        'schema_version': SNAP_SCHEMA_VERSION,
        'immutability': 'raw 事件对只增不覆盖；本 sidecar 与 parquet 同 event_id',
    }
    event_id = pd.Timestamp.now('UTC').strftime('%Y%m%dT%H%M%S%fZ')
    pq, _js = _atomic_write_pair(new, prov, event_id)
    latest = _rebuild_latest()
    if latest is not None:
        latest.to_parquet(SYNC_LATEST, index=False)    # derived 视图（可覆写）
    events, _rej = _valid_events()
    latest_prov = {
        'view': 'derived/latest（非 raw；raw 为 snap__*.{parquet,json} 事件对）',
        'selected_event_ids': [_format_event_id(k[0], k[1]) for k, _p in events],
        'selection_rule': '事件序 (base, seq) 升序合并，同 session_date 由后一'
                          '事件覆盖（真正最新的采集，含碰撞次序）',
        'parquet_file': SYNC_LATEST.name,
        'updated_at_utc': pd.Timestamp.now('UTC').isoformat(),
        'rejected_events': _rej,
    }
    tmp = SYNC_PROV_LATEST.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(latest_prov, ensure_ascii=False, indent=2),
                   encoding='utf-8')
    os.replace(tmp, SYNC_PROV_LATEST)   # derived 视图允许覆写
    return latest


def migrate_legacy_snapshots() -> list[str]:
    """非破坏迁移：为第八轮前的裸 parquet 快照补写 sidecar。

    只写**能从现有文件可靠恢复**的字段（行数、会话、捕获时刻=文件 mtime 上限
    估计并明示）；当时的规则参数与时钟口径证词未随文件保存——一律
    legacy_provenance_unavailable，不臆造。原 parquet 不动，lineage 记录来源。"""
    migrated = []
    if not SYNC_DIR.exists():
        return migrated
    for p in sorted(SYNC_DIR.glob('snap__EURCHF_TRIO__*.parquet')):
        if p.with_suffix('.json').exists():
            continue
        df = pd.read_parquet(p)
        m = re.search(r'snap__EURCHF_TRIO__(\d{8}T\d+(?:-\d+)?Z)\.parquet$', p.name)
        eid = m.group(1) if m else p.stem
        prov = {
            'event_id': eid,
            'captured_at_utc': None,
            'captured_at_note': 'legacy：裸 parquet 无 sidecar；文件 mtime='
                                f'{pd.Timestamp(p.stat().st_mtime, unit="s", tz="UTC").isoformat()}（上限估计）',
            'symbols': TRIO,
            'session_dates': sorted(df['session_date'].astype(str).tolist()),
            'rows': int(len(df)),
            'target_rule': 'legacy_provenance_unavailable',
            'session_calendar': 'canonical Mon–Fri（周日壳/周末排除）',
            'prescan_min': 'legacy_provenance_unavailable',
            'skew_max_ms': 'legacy_provenance_unavailable',
            'age_max_ms': 'legacy_provenance_unavailable',
            'clock_semantics': 'legacy_provenance_unavailable',
            'schema_version': '1-legacy',
            'parquet_file': p.name,
            'parquet_sha256': _sha256(p),
            'migration': {'migrated_at': pd.Timestamp.now('UTC').isoformat(),
                          'source': '第八轮前裸快照（无 sidecar），非破坏补写'},
            'immutability': 'legacy 补写 sidecar；parquet 未改动',
        }
        js = p.with_suffix('.json')
        tmp = js.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(prov, ensure_ascii=False, indent=2),
                       encoding='utf-8')
        try:
            os.rename(tmp, js)      # 目标不存在才成功（防覆盖）
        except FileExistsError:
            tmp.unlink()
            continue
        migrated.append(p.name)
    if migrated:
        lineage = SYNC_DIR / 'lineage.json'
        hist = json.loads(lineage.read_text(encoding='utf-8')) \
            if lineage.exists() else []
        hist.append({'migrated_at': pd.Timestamp.now('UTC').isoformat(),
                     'action': 'legacy 裸快照补写 sidecar（非破坏）',
                     'files': migrated,
                     'note': '未知规则/时钟口径记 legacy_provenance_unavailable'})
        lineage.write_text(json.dumps(hist, ensure_ascii=False, indent=2),
                           encoding='utf-8')
    return migrated


def migrate_legacy_store() -> str | None:
    """迁移（保留）：把第六轮可覆盖的 latest 复制为首份不可变快照。幂等。"""
    if not SYNC_LATEST.exists() or _snapshots():
        return None
    SYNC_DIR.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now('UTC').strftime('%Y%m%dT%H%M%S%fZ')
    snap = SYNC_DIR / f'snap__EURCHF_TRIO__{stamp}.parquet'
    shutil.copy(str(SYNC_LATEST), str(snap))
    lineage = SYNC_DIR / 'lineage.json'
    hist = json.loads(lineage.read_text(encoding='utf-8')) \
        if lineage.exists() else []
    hist.append({'migrated_at': pd.Timestamp.now('UTC').isoformat(),
                 'from': SYNC_LATEST.name, 'to': snap.name,
                 'note': '第六轮可覆盖 store 迁移为首份不可变快照（copy；'
                         '原文件保留为 derived latest 视图）'})
    lineage.write_text(json.dumps(hist, ensure_ascii=False, indent=2),
                       encoding='utf-8')
    return snap.name


def evaluate_v4(store_df, min_sessions=30, pip_eps=1.0,
                drift_eps=1e-6) -> dict:
    """V4 严格门：同步 mid 下 EURCHF vs EURUSD×USDCHF。

    - 仅 **verified 且规范工作日（Mon–Fri）** 会话参与（防御二：周日壳即使
      verified 也不计入 coverage/残差/drift，只计入排除计数）；
    - 门：全部参与会话 |偏差| < 1 pip 且 |drift| < 1e-6 pips/bar；
    - 规范工作日 verified 覆盖 < min_sessions → UNVERIFIED。"""
    out = {'verified_sessions': 0, 'unverified_sessions': 0,
           'weekend_rows_excluded': 0, 'unverified_reasons': {},
           'skew_ms': {}, 'pips': {}, 'pass': False, 'verdict': 'unverified'}
    if store_df is None or not len(store_df):
        out['verdict'] = 'no_store'
        return out
    wd_mask = (pd.to_datetime(store_df['session_date']).dt.dayofweek < 5
               ).to_numpy()
    out['weekend_rows_excluded'] = int((~wd_mask).sum())
    sdf = store_df[wd_mask]
    v = sdf[sdf['verified'].astype(bool)].dropna(
        subset=[f'{s}__mid' for s in TRIO]).reset_index(drop=True)
    out['verified_sessions'] = len(v)
    out['unverified_sessions'] = int((~sdf['verified'].astype(bool)).sum())
    for r in sdf.loc[~sdf['verified'].astype(bool), 'reason']:
        key = str(r).split(':')[0]
        out['unverified_reasons'][key] = out['unverified_reasons'].get(key, 0) + 1
    if len(v) < min_sessions:
        out['verdict'] = f'insufficient_coverage({len(v)}<{min_sessions})'
        return out
    direct = v['EURCHF__mid'].astype(float)
    synth = v['EURUSD__mid'].astype(float) * v['USDCHF__mid'].astype(float)
    pips = (direct - synth).abs() * 1e4
    drift = float(np.polyfit(np.arange(len(pips)), pips.to_numpy(), 1)[0])
    skew = v['skew_ms'].astype(float)
    out['pips'] = {'max': float(pips.max()), 'median': float(pips.median()),
                   'n': int(len(pips))}
    out['skew_ms'] = {'p50': float(skew.quantile(0.5)),
                      'p95': float(skew.quantile(0.95)),
                      'max': float(skew.max())}
    ok = float(pips.max()) < pip_eps and abs(drift) < drift_eps
    out.update({'max_pips': float(pips.max()), 'median_pips': float(pips.median()),
                'drift': drift, 'pass': bool(ok),
                'verdict': 'pass' if ok else 'fail'})
    return out


def load_store(path=SYNC_LATEST) -> pd.DataFrame | None:
    return pd.read_parquet(path) if path.exists() else None
