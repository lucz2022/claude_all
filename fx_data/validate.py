"""§6 验证清单 V1-V13（审计修订版）。

原则：不放宽阈值、不用代理冒充、未知缺口保留 FAIL、缺数据必须 FAIL、
无真实换月标「未验证」。共识统计仅作诊断附注，不构成豁免。"""
import numpy as np
import pandas as pd

from . import config, storage
from .board import incidence_matrix, residual_timeseries, solve_strength, spread_log
from .continuous import validate_roll_returns
from .resample import uniform_grid_check, vix_sessions_aligned
from .symbol_guard import FUT_MONTH_RE


def _record(results, vid, item, passed, detail='', verified=True):
    results.append({'id': vid, 'item': item, 'pass': bool(passed),
                    'verified': bool(verified), 'detail': detail})


def _inst_meta():
    return storage.read_sidecar('mt5', 'instrument_meta.json') or {}


TICK_SNAPSHOT_MAX_AGE = pd.Timedelta(seconds=120)   # 全链路统一新鲜度门


def _tick_snapshot(max_age: pd.Timedelta = TICK_SNAPSHOT_MAX_AGE):
    """同刻 tick 快照（mt5_export._tick_snapshot 落盘）。

    统一 120s 门（审计第四轮 P1-2 修复）：落盘 age_ms 不会随时间增长，故
    有效年龄 = 存储 age_ms + (now − captured_at)。超过 120s 的快照一律
    视为陈旧返回 (None, captured)——V9 / API live 剔除 / 点差 tick 源同门，
    陈旧快照不得报告残差、不得剔除交易对。"""
    t = storage.read_sidecar('mt5', 'tick_snapshot.json')
    if not t or '_captured_at_utc' not in t:
        return None, None
    captured = pd.Timestamp(t['_captured_at_utc'])
    now = pd.Timestamp.utcnow()
    now = now.tz_localize('UTC') if now.tzinfo is None else now.tz_convert('UTC')
    if captured.tzinfo is None:
        captured = captured.tz_localize('UTC')
    elapsed = now - captured
    if elapsed > max_age:
        return None, captured
    # 有效年龄 = 存储年龄 + 流逝时间（原地标注，消费方统一用 age_eff_ms）
    elapsed_ms = elapsed.total_seconds() * 1000.0
    for k, v in t.items():
        if not k.startswith('_') and isinstance(v, dict):
            v['age_eff_ms'] = v.get('age_ms', 0.0) + elapsed_ms
    return t, captured


def _spread_sources(h1_map):
    """真实点差（log 量纲）：{sym: (spread_log | None, source)}。

    优先级：**≤120s 新鲜** tick 快照 ask−bid（下限 1 point=tick 尺寸）→
    **≤120s 新鲜**采集实时 symbol_info().spread×point → bar 级 copy_rates
    非零中位数。陈旧的「live」声称一律不得使用（与 V9/API 同一门）。"""
    ticks, _cap = _tick_snapshot()
    inst = _inst_meta()
    inst_captured = inst.get('_captured_at_utc') if isinstance(inst, dict) else None
    inst_fresh = False
    if inst_captured:
        c = pd.Timestamp(inst_captured)
        c = c.tz_localize('UTC') if c.tzinfo is None else c.tz_convert('UTC')
        now = pd.Timestamp.utcnow()
        now = now.tz_localize('UTC') if now.tzinfo is None else now.tz_convert('UTC')
        inst_fresh = (now - c) <= TICK_SNAPSHOT_MAX_AGE
    out = {}
    for s, df in h1_map.items():
        px = float(df['close'].iloc[-1])
        meta = inst.get(s) if isinstance(inst.get(s), dict) else {}
        point = meta.get('point')
        if ticks and s in ticks and px > 0:
            sp = max(float(ticks[s]['spread_price']), point or 0.0)
            out[s] = (sp / px, 'tick_snapshot(ask-bid)')
            continue
        live = meta.get('spread_points_live')
        if inst_fresh and live is not None and point and px > 0 and live > 0:
            out[s] = (live * point / px, 'live_snapshot(symbol_info)')
            continue
        out[s] = spread_log(df, point, px)
    return out


def v1_session_dates_equal(d1_map) -> list[dict]:
    """V1: 28 盘 D1 的 session_date 数组逐元素完全相等。"""
    results = []
    ref_sym = sorted(d1_map)[0]
    ref = d1_map[ref_sym]['session_date'].to_numpy()
    ok, diffs = True, []
    for s, df in d1_map.items():
        cur = df['session_date'].to_numpy()
        if len(cur) != len(ref) or not (cur == ref).all():
            ok = False
            diffs.append(s)
    _record(results, 'V1', '28 盘 session_date 逐元素相等', ok,
            f'参照={ref_sym}, 不一致={diffs or "无"}')
    return results


def v2_row_counts(d1_map, min_rows: int = 250) -> list[dict]:
    """V2: 各 symbol D1 根数 ≥ 250 且全集一致。"""
    results = []
    counts = {s: len(df) for s, df in d1_map.items()}
    vals = set(counts.values())
    ok = all(v >= min_rows for v in vals) and len(vals) == 1
    _record(results, 'V2', f'D1 根数 ≥{min_rows} 且一致', ok,
            f'counts={sorted(vals) if len(vals) <= 5 else f"{min(vals)}..{max(vals)}"}')
    return results


def v3_session_spacing(d1_map, holidays_per_year: int = 8) -> list[dict]:
    """V3: 工作日常会话间隔必须为 1；跨周末 3-5；单个缺失工作日按节假日容忍。"""
    results = []
    bad, holiday_over = {}, {}
    for s, df in d1_map.items():
        d = pd.to_datetime(df['session_date'])
        wd = d[d.dt.dayofweek < 5]
        prev, single_missing = None, 0
        for cur in wd:
            if prev is not None:
                gap = (cur - prev).days
                spans_weekend = cur.weekday() <= prev.weekday()
                if gap == 1 or (spans_weekend and gap in (3, 4, 5)):
                    pass
                elif gap == 2:
                    single_missing += 1
                else:
                    bad.setdefault(s, []).append(f'{prev.date()}->{cur.date()}={gap}d')
            prev = cur
        tol = max(holidays_per_year, round(len(wd) / 252 * holidays_per_year))
        if single_missing > tol:
            holiday_over[s] = f'{single_missing}>{tol}'
    detail = f'硬缺口={ {k: v[:3] for k, v in bad.items()} or "无"}'
    if holiday_over:
        detail += f', 节假日超容={holiday_over}'
    _record(results, 'V3', '工作日常会话间隔为 1（节假日容忍）',
            not bad and not holiday_over, detail)
    return results


def v4_synthetic_cross(d1_map) -> list[dict]:
    """V4（第六轮起）：**同步 mid 日切价**严格门——三腿在会话日切前最后 1 秒
    （21:59:59Z）的同一目标时刻取各自最近 tick 的 mid=(bid+ask)/2，验证
    EURCHF_mid vs EURUSD_mid×USDCHF_mid：全部 verified 会话 |偏差| < 1 pip
    且无趋势漂移（|drift| < 1e-6 pips/bar）。

    - 任一腿缺失 / age>60s / 跨腿 skew>2s → 该会话 UNVERIFIED（计数披露，
      绝不用不同时刻或 H1 close 冒充）；
    - verified 覆盖 < 30 会话 → V4 整体 UNVERIFIED；
    - 原 bid-D1 检查**保留为内嵌诊断**（非门控、不放宽）：bid 收盘采样时刻
      不一致的已知问题如实展示；
    - 同步 mid store 由 collect 持续回填（tick 深度边界内），独立落盘
      data/raw/mt5/syncmid/。"""
    results = []
    from .syncmid import evaluate_v4, load_store
    store = load_store()
    ev = evaluate_v4(store)
    # 旧 bid-D1 诊断（保留、非门控）
    diag = ''
    try:
        direct = d1_map['EURCHF'].set_index('session_date')['close'].astype(float)
        synth = (d1_map['EURUSD'].set_index('session_date')['close'].astype(float)
                 * d1_map['USDCHF'].set_index('session_date')['close'].astype(float))
        j = direct.to_frame('d').join(synth.to_frame('s'), how='inner').dropna()
        pips = (j['d'] - j['s']).abs() * 1e4
        diag = (f'; bid-D1 诊断(非门控): max={float(pips.max()):.2f}pips, '
                f'≥1pip {int((pips >= 1.0).sum())}/{len(pips)} 会话'
                f'（bid 收盘采样时刻不一致的已知问题）')
    except KeyError:
        diag = '; bid-D1 诊断(非门控): 缺品种'
    item = '同步 mid 三角 < 1 pip（21:59:59Z 同刻）'
    if ev['verdict'] in ('no_store',) or ev['verdict'].startswith('insufficient'):
        _record(results, 'V4', item, False,
                f"同步 mid 覆盖不足（{ev['verdict']}, "
                f"verified={ev['verified_sessions']}）→ 不可验证{diag}",
                verified=False)
        return results
    cov = (f"coverage {ev['verified_sessions']} verified / "
           f"{ev['verified_sessions'] + ev['unverified_sessions']} 会话")
    skew = ev['skew_ms']
    skew_s = (f"skew p50={skew['p50']:.0f}/p95={skew['p95']:.0f}/"
              f"max={skew['max']:.0f}ms")
    unv = (f"UNVERIFIED {ev['unverified_sessions']} 会话"
           f"（{ev['unverified_reasons']}）" if ev['unverified_sessions'] else '')
    _record(results, 'V4', item, bool(ev['pass']),
            f"max={ev['max_pips']:.3f}pips, 中位={ev['median_pips']:.3f}pips, "
            f"drift={ev['drift']:.2e}/bar; {cov}; {skew_s}"
            + (f'; {unv}' if unv else '') + diag)
    return results


def _build_availability(h1_map, quorum_src=None):
    """诊断用共识可用性：{(date, hour): 品种占比}。仅作附注，不作豁免。"""
    counts = {}
    n = len(h1_map)
    for df in h1_map.values():
        for key in set(zip(df['ts_utc'].dt.date, df['ts_utc'].dt.hour)):
            counts[key] = counts.get(key, 0) + 1
    return {k: v / n for k, v in counts.items()} if n else {}


def v5_h1_grid(h1_map) -> list[dict]:
    """V5（规则化）：缺陷 = 非整点 / 重复 / 缺失应交易槽位。
    豁免仅依据 session_rules 显式规则；未知缺口 FAIL（共识信息仅诊断附注）。

    第六轮：同经纪商原始 tick 重建的 H1 bar（source=mt5_reconstructed，
    证据落盘 data/raw/mt5_reconstructed/）计入网格（真实报价、非插值）；
    重建根数在 detail 中披露。"""
    results = []
    avail = _build_availability(h1_map)
    bad = {}
    n_recon = 0
    for s, df in h1_map.items():
        if 'source' in df.columns:
            n_recon += int((df['source'] == 'mt5_reconstructed').sum())
        defects = uniform_grid_check(df, availability=avail)
        if len(defects):
            ex = defects.iloc[0]
            bad[s] = (len(defects), str(ex['ts_utc']),
                      ex.get('consensus_diag', ''))
    recon_note = (f'; tick重建 {n_recon} 根(source=mt5_reconstructed)'
                  if n_recon else '')
    _record(results, 'V5', 'H1 网格整点/无缺bar（显式规则豁免）', not bad,
            f'缺陷={ {k: f"n={v[0]}@{v[1]} {v[2]}" for k, v in list(bad.items())[:3]} or "无"}'
            + (f' …共{len(bad)}品种' if len(bad) > 3 else '') + recon_note)
    return results


def v6_zero_sum(board) -> list[dict]:
    results = []
    s = sum(c['strength'] for c in board['currencies'])
    _record(results, 'V6', '八货币强度零和', abs(s) < 1e-12, f'sum={s:.2e}')
    return results


def v7_calendar_intersection(d1_map, env_keys=('HG', 'XTIUSD', 'US500', 'VIX')
                             ) -> list[dict]:
    """V7（文档字面标准）：环境层各序列与 **FX 日历**交集 / FX 根数 ≥ 0.9。

    FX 日历 = 28 盘全集的会话日（V1 保证逐元素一致，取 EURUSD 为代表并复核
    全集一致；不得用 XAUUSD 等环境品种冒充 FX——其历史长度与 FX 不同）。
    缺 HG / VIX 直接 FAIL；FX 全集不完整直接 FAIL。"""
    results = []
    missing_pairs = [p for p in config.PAIRS_28 if p not in d1_map]
    if missing_pairs or 'EURUSD' not in d1_map:
        _record(results, 'V7', '环境层日历交集 ≥ 0.9（FX 分母）', False,
                f'FX 全集不完整（缺 {len(missing_pairs)} 盘）→ 无法取 FX 日历')
        return results
    ref = d1_map['EURUSD']['session_date'].tolist()
    for p in config.PAIRS_28:
        if d1_map[p]['session_date'].tolist() != ref:
            _record(results, 'V7', '环境层日历交集 ≥ 0.9（FX 分母）', False,
                    f'{p} 会话日与 EURUSD 不一致（见 V1）')
            return results
    fx_dates = set(ref)
    fx_n = len(fx_dates)
    details, missing = {}, []
    ok = True
    for k in env_keys:
        if k not in d1_map:
            missing.append(k)
            ok = False
            continue
        dates = set(d1_map[k]['session_date'])
        r = len(fx_dates & dates) / fx_n if fx_n else 0
        details[k] = round(r, 4)
        ok &= r >= 0.9
    if missing:
        _record(results, 'V7', '环境层日历交集 ≥ 0.9（FX 分母）', False,
                f'缺失序列: {missing}（必须 FAIL）')
    else:
        _record(results, 'V7', '环境层日历交集 ≥ 0.9（FX 分母）', ok,
                f'FX=EURUSD={fx_n}, ratios={details}')
    return results


def v8_no_repaint(symbols: list[str], source: str = 'mt5', tf: str = 'H1'
                  ) -> list[dict]:
    """V8（常驻）：最近两份 raw 快照的一致性。

    - 比较范围 = 两快照共同时间窗（≥ 新快照最早 ts）。copy_rates 的固定
      count 滑窗会把最老 bar 挤出窗口——那不是重绘；共同窗内的历史 bar
      被删除或改值才是（reindex 缺失行计为差异，不因 NaN 比较为 False 漏检）。
    - 采集只落盘已收盘 bar，故无「最后一根豁免」——全部共同窗 bar 必须一致。
    - 快照链保证 raw 永不覆盖；只有一份快照的品种标「未验证」。"""
    results = []
    bad, unver = {}, []
    for s in symbols:
        snaps = storage.raw_snapshots(source, s, tf)
        if len(snaps) < 2:
            unver.append(s)
            continue
        prev = pd.read_parquet(snaps[-2])
        curr = pd.read_parquet(snaps[-1])
        common_min = max(prev['ts_utc'].min(), curr['ts_utc'].min())
        a = prev.set_index('ts_utc')
        a = a[a.index >= common_min]
        b = curr.set_index('ts_utc').reindex(a.index)
        cols = [c for c in ('open', 'high', 'low', 'close') if c in a.columns]
        vanished = b[cols].isna().any(axis=1)          # 共同窗内历史 bar 被删除
        diff = (a[cols] - b[cols]).abs()
        changed = (diff > 1e-12).any(axis=1)
        n_rep = int((changed | vanished).sum())
        if n_rep:
            n_del = int(vanished.sum())
            bad[s] = f'{n_rep} 根重绘' + (f'（含删除 {n_del}）' if n_del else '')
    if unver and not bad:
        _record(results, 'V8', 'raw 快照共同窗一致（无重绘/无删除）', True,
                f'仅一份快照（首采）: {len(unver)} 品种 → 未验证', verified=False)
    else:
        _record(results, 'V8', 'raw 快照共同窗一致（无重绘/无删除）', not bad,
                f'重绘={bad or "无"}; 未验证={len(unver)}')
    return results


def _triangles(pairs):
    """21 个非美交叉的直接三角。返回 [(cross, leg_b, leg_q, b_recip, q_recip)]。

    合成：cross = (b/USD) / (q/USD)；b/USD 由 leg_b 直接给或取倒数。
    例：EURCHF = EURUSD × USDCHF；AUDNZD = AUDUSD ÷ NZDUSD。"""
    nonusd = [p for p in pairs if 'USD' not in p]
    out = []
    for p in nonusd:
        b, q = p[:3], p[3:6]
        if f'{b}USD' in pairs:
            leg_b, b_recip = f'{b}USD', False
        elif f'USD{b}' in pairs:
            leg_b, b_recip = f'USD{b}', True
        else:
            out.append((p, None, None, False, False))
            continue
        if f'{q}USD' in pairs:
            leg_q, q_recip = f'{q}USD', False
        elif f'USD{q}' in pairs:
            leg_q, q_recip = f'USD{q}', True
        else:
            out.append((p, None, None, False, False))
            continue
        out.append((p, leg_b, leg_q, b_recip, q_recip))
    return out


def _synthetic_close(last, leg_b, leg_q, b_recip, q_recip) -> float:
    b = float(last[leg_b])
    q = float(last[leg_q])
    return (1.0 / b if b_recip else b) / (1.0 / q if q_recip else q)


TICK_SKEW_MAX_MS = 5000        # 三角内三腿 tick 时间差上限
TICK_STALE_MAX_MS = 120_000    # 单腿 tick 有效年龄（墙钟+日历偏移−tick 时刻，含流逝）上限


def v9_triangular_residuals(h1_map, inst_meta) -> list[dict]:
    """V9（严格+直接三角+完整覆盖）：21 个非美交叉的同刻直接报价 vs 合成，
    |log 残差| < 2×真实点差。

    - **只在取得 ≤120s 新鲜 tick 快照时才报告残差/剔除盘**；快照陈旧或缺失
      → UNVERIFIED（不回退 H1 收盘采样——采样时刻不同与点差同量级，会失真）。
    - 偏移锚冲突（经纪商时区与日历不符）或时钟异常 → UNVERIFIED。
    - PASS 前提：**21/21 全部被检查**；每三角校验有效年龄 ≤120s、skew ≤5s。"""
    results = []
    ticks, captured = _tick_snapshot()
    if ticks is None:
        _record(results, 'V9', '21 非美交叉直接三角 < 2×点差', False,
                f'无 ≤{int(TICK_SNAPSHOT_MAX_AGE.total_seconds())}s 新鲜 tick 快照'
                f'（captured={captured}）→ 不可验证，不报告残差', verified=False)
        return results
    if ticks.get('_offset_source') == 'conflict':
        _record(results, 'V9', '21 非美交叉直接三角 < 2×点差', False,
                f'服务器偏移锚冲突（探针隐含与日历不符）→ 不可验证', verified=False)
        return results
    if ticks.get('_clock_anomaly'):
        _record(results, 'V9', '21 非美交叉直接三角 < 2×点差', False,
                f'时钟异常（tick 超前墙钟>60s）: {ticks["_clock_anomaly"]} → 不可验证',
                verified=False)
        return results
    smap = _spread_sources(h1_map)
    tri = _triangles(sorted(h1_map.keys()))
    expected = sum(1 for t in tri if t[1] is not None)
    bad, no_spread, checked = [], [], 0
    for cross, lb, lq, br, qr in tri:
        if lb is None:
            bad.append((cross, '无可合成腿'))
            continue
        sp, src = smap.get(cross, (None, 'unavailable'))
        if sp is None:
            no_spread.append((cross, src))
            continue
        try:
            if not all(k in ticks for k in (cross, lb, lq)):
                bad.append((cross, '快照缺该腿 tick'))
                continue
            trio = {k: ticks[k] for k in (cross, lb, lq)}
            stale = [k for k, v in trio.items()
                     if v.get('age_eff_ms', v.get('age_ms', 0)) > TICK_STALE_MAX_MS]
            if stale:
                bad.append((cross, f'陈旧tick>{TICK_STALE_MAX_MS // 1000}s: {stale}'))
                continue
            skew = max(v['tick_time_ms'] for v in trio.values()) - \
                min(v['tick_time_ms'] for v in trio.values())
            if skew > TICK_SKEW_MAX_MS:
                bad.append((cross, f'tick偏差{skew}ms>{TICK_SKEW_MAX_MS}ms'))
                continue
            last = {k: trio[k]['bid'] for k in (cross, lb, lq)}
            synth = _synthetic_close(last, lb, lq, br, qr)
            resid = abs(np.log(last[cross]) - np.log(synth))
            checked += 1
            if resid > 2 * sp:
                bad.append((cross, f'{resid:.2e} > 2×{sp:.2e}'))
        except (KeyError, ValueError) as e:
            bad.append((cross, f'缺 {e}'))
    if checked == 0 and no_spread:
        _record(results, 'V9', '21 非美交叉直接三角 < 2×点差', False,
                f'无真实点差可用（{len(no_spread)} 盘）→ 不可验证', verified=False)
        return results
    ok = (checked == expected) and not bad
    cov_note = '' if checked == expected else f'覆盖不全 {checked}/{expected}; '
    _record(results, 'V9', '21 非美交叉直接三角 < 2×点差（全量覆盖）', ok,
            f'路径=tick_snapshot@{captured:%m-%d %H:%MZ}'
            + f', tick跨度{ticks.get("_tick_span_ms")}ms'
            + f', 偏移锚{ticks.get("_offset_source")}'
            + f'; {cov_note}检查 {checked}/{expected}; 违例={bad or "无"}; '
            + f'无点差={len(no_spread)} 盘')
    return results


def v10_residual_bias(d1_map, inst_meta, lookback: int = 250) -> list[dict]:
    """V10（常驻，时序版）：逐会话 LSQ 残差的长期均值 |mean| < 2×真实点差。
    PASS 前提 = 全部 28 盘都有真实点差容忍度（覆盖不全 = 验证不完整 = FAIL；
    全部不可用 = UNVERIFIED，不是 PASS）。"""
    results = []
    try:
        stats = residual_timeseries(d1_map, lookback=lookback)
    except Exception as e:  # noqa: BLE001
        _record(results, 'V10', '残差无长期偏置（时序）', False, f'计算失败: {e}')
        return results
    h1_map = {}
    for s in d1_map:
        if storage.norm_exists(s, 'H1'):
            h1_map[s] = storage.read_norm(s, 'H1')
    smap = _spread_sources(h1_map)
    biased, no_sp = {}, []
    for _, row in stats.iterrows():
        sp, src = smap.get(row['pair'], (None, ''))
        if sp is None or sp <= 0:
            no_sp.append(row['pair'])
            continue
        if abs(row['mean']) > 2 * sp:
            biased[row['pair']] = (f"mean={row['mean']:.2e} vs 2×sp={2 * sp:.2e} "
                                   f"(n={int(row['n'])}, src={src})")
    total = len(stats)
    if len(no_sp) == total:
        _record(results, 'V10', '残差无长期偏置（时序）', False,
                '全部品种无真实点差 → 不可验证', verified=False)
        return results
    ok = (not biased) and (len(no_sp) == 0)
    _record(results, 'V10', '残差无长期偏置（时序，全覆盖）', ok,
            f'lookback={lookback}; 覆盖 {total - len(no_sp)}/{total}; '
            f'biased={biased or "无"}; 无点差={no_sp or "无"}')
    return results


def v11_expiring_symbols_scanned(raw_sources=('mt5', 'ibkr')) -> list[dict]:
    """V11（真扫描）：raw 中所有含到期月模式的 symbol 必须登记 ltd。
    新鲜度要求仅施加于**活跃腿与其下一腿**（到期规则视野内）——远月腿报价
    稀疏、停更属常态，不构成僵尸；已到期腿为历史拼接材料。
    未登记 ltd 的到期月符号 = FAIL。"""
    results = []
    syms = set(config.IBKR_HG_LEGS.keys())
    for src in raw_sources:
        d = config.DIR_RAW / src
        if d.exists():
            for f in d.glob('*__H1.parquet'):
                syms.add(f.name.split('__')[0])
    expiring = sorted(s for s in syms if FUT_MONTH_RE.search(s))
    now = pd.Timestamp.utcnow()
    now = now.tz_localize('UTC') if now.tzinfo is None else now.tz_convert('UTC')
    horizon = now + pd.DateOffset(months=8)   # 活跃腿 + 下一活跃季
    unregistered, stale, skipped_far = [], [], []
    for s in expiring:
        p = config.DIR_RAW / 'ibkr' / f'{s}__ltd.txt'
        ltd = p.read_text(encoding='utf-8').strip() if p.exists() else None
        if not ltd:
            unregistered.append(s)
            continue
        t = pd.Timestamp(ltd, tz='UTC')
        if t <= now:
            continue                     # 已到期：历史腿
        if t > horizon:
            skipped_far.append(s)        # 超视野远月：报价稀疏属常态，仅记录
            continue
        if storage.raw_exists('ibkr', s, 'H1'):
            df = storage.read_raw('ibkr', s, 'H1')
            if (now - df['ts_utc'].max()) > pd.Timedelta(hours=24):
                stale.append(s)
        else:
            stale.append(f'{s}(无数据)')
    _record(results, 'V11', '到期月符号全登记且活跃/相邻腿新鲜',
            not unregistered and not stale,
            f'扫描 {len(syms)} symbol, 到期月 {len(expiring)}; '
            f'未登记={unregistered or "无"}, 僵尸={stale or "无"}, '
            f'超视野远月(仅记录)={skipped_far or "无"}')
    return results


def v12_time_window_overlap(d1_map, h1_map,
                            env_keys=('HG', 'XAUUSD', 'XTIUSD', 'US500', 'VIX'),
                            rth_sources=('VIX',)) -> list[dict]:
    """V12（时间窗版）：统一重采样后同日 bar 的实际对齐 ≥ 90%。

    FX 参照 = EURUSD（28 盘代表，非环境品种）。对每个环境序列，在共同会话上度量：
    - coverage：源 bar 小时槽 / FX 小时槽（覆盖不足=缺数据）；
    - exact：源的**精确时间戳**（到分钟）与 FX 网格重合的比例——整体错开
      30 分钟的序列此项=0（日期+小时比较无法发现的错位）；
    - RTH 源（VIX）：以窗口包含度门控（其 ~8h 窗口应完整落在 FX 当日窗内），
      属结构性日程而非错位。
    全部环境序列（含 VIX）都参与门控。"""
    results = []
    if 'EURUSD' not in h1_map:
        _record(results, 'V12', '同日 bar 实际对齐 ≥ 90%（coverage+exact）',
                False, '缺 EURUSD H1')
        return results

    def with_session(df):
        d = df.copy()
        d['session'] = ((d['ts_utc'] - pd.Timedelta(hours=config.CANONICAL_CUT_UTC))
                        .dt.floor('D') + pd.Timedelta(days=1)).dt.date
        return d

    fx = with_session(h1_map['EURUSD'])
    fx_slots = set(zip(fx['session'], fx['ts_utc'].dt.floor('min')))
    fx_sessions = {s for s, _ in fx_slots}
    details, missing = {}, []
    ok = True
    for k in env_keys:
        if k not in h1_map:
            missing.append(k)
            ok = False
            continue
        d = with_session(h1_map[k])
        slots = set(zip(d['session'], d['ts_utc'].dt.floor('min')))
        common = fx_sessions & {s for s, _ in slots}
        fx_n = sum(1 for s, t in fx_slots if s in common)
        src_n = sum(1 for s, t in slots if s in common)
        exact = len(fx_slots & slots)
        cov = src_n / fx_n if fx_n else 0.0
        exr = (exact / src_n) if src_n else 0.0
        if k in rth_sources:
            # 窗口包含度：源的 [首bar, 末bar+1h] 应完整落在 FX 同会话窗口内
            contain = []
            for s in common:
                a = d.loc[d['session'] == s, 'ts_utc']
                b = fx.loc[fx['session'] == s, 'ts_utc']
                if len(a) < 2 or len(b) < 2:
                    continue
                lo_s, hi_s = a.min(), a.max() + pd.Timedelta(hours=1)
                lo_f, hi_f = b.min(), b.max() + pd.Timedelta(hours=1)
                inter = (min(hi_s, hi_f) - max(lo_s, lo_f)).total_seconds()
                contain.append(inter / (hi_s - lo_s).total_seconds())
            gate = float(np.mean(contain)) if contain else 0.0
            # 锚点规则（防 30 分钟整体偏移等错位：containment 对此不敏感）
            from .resample import vix_anchor_errors
            anchor_bad = vix_anchor_errors(h1_map[k]) if k == 'VIX' else []
            details[k] = {'containment': round(gate, 3), 'exact': round(exr, 3),
                          'anchor_ok': not anchor_bad,
                          'anchor_err': anchor_bad[:2], 'rth': True}
            ok &= gate >= 0.9 and not anchor_bad
        else:
            details[k] = {'coverage': round(cov, 3), 'exact': round(exr, 3)}
            ok &= cov >= 0.9 and exr >= 0.9
    _record(results, 'V12', '同日 bar 实际对齐 ≥ 90%（coverage+exact）', ok,
            f'FX=EURUSD; {details}' + (f'; 缺失={missing}' if missing else ''))
    return results


def v13_roll_returns(cont, z_thresh: float = 5.0) -> list[dict]:
    """V13：换月点前后 ±3 根（**含 offset 0**）对数收益 |z| < 5。
    无真实换月点 → 未验证（非 PASS）。"""
    results = []
    neigh = validate_roll_returns(cont, pad=3)
    if neigh.empty:
        _record(results, 'V13', '换月邻域（±3 根含当根）无异常尖峰', False,
                '连续合约中无换月点 → 未验证', verified=False)
        return results
    all_rets = np.log(cont.sort_values('session_date')['close'].astype(float)).diff()
    sigma = float(all_rets.std())
    if sigma <= 0:
        _record(results, 'V13', '换月邻域（±3 根含当根）无异常尖峰', True,
                '收益零方差（数据退化）')
        return results
    neigh = neigh.copy()
    neigh['z'] = neigh['log_ret'].abs() / sigma
    bad = neigh[neigh['z'] > z_thresh]
    _record(results, 'V13', '换月邻域（±3 根含当根）无异常尖峰', len(bad) == 0,
            f'检查 {len(neigh)} 根（含 offset0 {int((neigh["offset"] == 0).sum())} 根）, '
            f'尖峰 {len(bad)}; max_z={float(neigh["z"].max()):.1f}')
    return results


def v14_vix_alignment(vix_h1) -> list[dict]:
    """VIX 会话对齐（ERRATA#1 的防御性验证）：H1 重建路径无 shift、无前视。"""
    results = []
    ok = vix_sessions_aligned(vix_h1)
    last_day = vix_h1['ts_utc'].max()
    _record(results, 'V14', 'VIX H1 全部落在当日会话窗（无需 shift）', ok,
            f'末根 {last_day}; 详见 ERRATA.md 第 1 条')
    return results


def run_all(d1_map, h1_map=None, board=None, cont_hg=None,
            env_keys=('HG', 'XTIUSD', 'US500', 'VIX')) -> list[dict]:
    inst_meta = _inst_meta()
    # V12 需要 HG 的时间窗：HG 无独立 H1（连续合约仅 D1），用全部腿的 H1 并集
    h1_map = dict(h1_map or {})
    if 'HG' not in h1_map:
        leg_frames = []
        d = config.DIR_RAW / 'ibkr'
        if d.exists():
            for f in sorted(d.glob('HG??__H1.parquet')):
                leg_frames.append(pd.read_parquet(f))
        if leg_frames:
            u = pd.concat(leg_frames).drop_duplicates('ts_utc')
            h1_map['HG'] = u.sort_values('ts_utc').reset_index(drop=True)
    results = []
    # V9 最先执行：其 tick 快照有 ≤120s 新鲜度门，重排校验（V1-V5 网格检查
    # 耗时 1-2 分钟）会把新鲜快照拖过期——顺序调整非门变更
    if h1_map:
        fx_h1 = {k: v for k, v in h1_map.items() if k in config.PAIRS_28}
        results += v9_triangular_residuals(fx_h1, inst_meta)
    results += v1_session_dates_equal({k: v for k, v in d1_map.items()
                                       if k in config.PAIRS_28})
    results += v2_row_counts({k: v for k, v in d1_map.items()
                              if k in config.PAIRS_28})
    results += v3_session_spacing(d1_map)
    results += v4_synthetic_cross(d1_map)
    if h1_map:
        results += v5_h1_grid(fx_h1)
        results += v12_time_window_overlap(d1_map, h1_map)
        if 'VIX' in h1_map:
            results += v14_vix_alignment(h1_map['VIX'])
    if board:
        results += v6_zero_sum(board)
    if all(k in d1_map for k in config.PAIRS_28):
        results += v10_residual_bias({k: d1_map[k] for k in config.PAIRS_28},
                                     inst_meta)
    results += v7_calendar_intersection(d1_map, env_keys=env_keys)
    results += v11_expiring_symbols_scanned()
    results += v8_no_repaint(sorted(config.PAIRS_28), source='mt5')
    if cont_hg is not None:
        results += v13_roll_returns(cont_hg)
    return results


def render(results: list[dict]) -> str:
    lines = []
    for r in results:
        tag = 'PASS' if r['pass'] else ('UNVERIFIED' if not r['verified'] else 'FAIL')
        lines.append(f"[{tag}] {r['id']}  {r['item']}  -- {r['detail']}")
    n_fail = sum(1 for r in results if not r['pass'] and r['verified'])
    n_unv = sum(1 for r in results if not r['verified'])
    lines.append(f'合计 {len(results)} 项, 失败 {n_fail}, 未验证 {n_unv}')
    return '\n'.join(lines)
