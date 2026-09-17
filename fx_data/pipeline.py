"""pipeline 编排：collect → build → validate → serve。

用法（在 C:\\data\\claude_all 下）：
    python -m fx_data.pipeline collect [--mt5] [--ibkr] [--allow-partial] [--no-guard]
    python -m fx_data.pipeline build
    python -m fx_data.pipeline validate
    python -m fx_data.pipeline board [--window 20] [--asof ISO]
    python -m fx_data.pipeline env
    python -m fx_data.pipeline context SYMBOL
"""
import argparse
import json
import sys

import pandas as pd


def _configure_console_output():
    """Keep CLI output readable on Windows hosts whose default codec is GBK.

    The endpoints emit Chinese labels and a few Unicode math symbols.  Python's
    inherited console encoding can therefore either mojibake the text or raise
    ``UnicodeEncodeError`` (for example on the minus sign used by ``env``).
    Reconfiguring the existing streams avoids requiring callers to set
    ``PYTHONUTF8`` and preserves redirected output as UTF-8.
    """
    for name in ('stdout', 'stderr'):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, 'reconfigure', None)
        if reconfigure is not None:
            try:
                reconfigure(encoding='utf-8', errors='backslashreplace')
            except (OSError, ValueError):
                pass


_configure_console_output()

from . import config, storage, validate
from .api import get_env_state, get_pair_context, get_strength_board
from .continuous import build_continuous
from .dxy import compute_dxy
from .mt5_export import export_all as mt5_export_all
from .ibkr_ingest import export_ibkr
from .resample import (assign_segments, complete_sessions, drop_weekend_shells,
                       rebuild_d1)


def collect(do_mt5: bool = True, do_ibkr: bool = True, guard: bool = True,
            allow_partial: bool = False):
    """L0 采集：MT5 全集 + IBKR 增补源（含已到期 HG legs 的发现与抓取）。
    快照式落盘（不覆盖旧快照）；失败默认抛错。"""
    if do_mt5:
        print('== MT5 采集 ==', flush=True)
        out, meta = mt5_export_all(allow_partial=allow_partial)
        print(f'MT5: {len(out)} 个品种, 服务器偏移 UTC{meta["mt5_server_utc_offset"]:+d}h',
              flush=True)
    if do_ibkr:
        print('== IBKR 采集（发现 HG legs 含已到期 + VIX）==', flush=True)
        out = export_ibkr(guard=guard, allow_partial=allow_partial)
        print(f'IBKR: {len(out)} 个品种', flush=True)


def _ltd_of(symbol: str) -> str | None:
    sidecar = config.DIR_RAW / 'ibkr' / f'{symbol}__ltd.txt'
    return sidecar.read_text(encoding='utf-8').strip() if sidecar.exists() else None


def _discovered_leg_symbols() -> set:
    """raw/ibkr 中已采集的 HG 腿（含发现式采集落在盘上的、config 外的腿）。"""
    d = config.DIR_RAW / 'ibkr'
    if not d.exists():
        return set()
    out = set()
    for f in d.glob('*__H1.parquet'):
        sym = f.name.split('__')[0]
        if sym.startswith('HG') and len(sym) == 4:
            out.add(sym)
    return out


def build():
    """L1 构建：H1 → D1 统一日切重建（剔周末壳）→ HG 连续合约（到期规则）
    → VIX 直接会话化（无 shift，见 ERRATA#1）→ L3 派生。"""
    # ---- MT5 ----
    from .backfill import load_reconstructed
    recon_map = load_reconstructed()
    n_recon_total = 0
    mt5_d1 = {}
    for sym in config.ALL_MT5_SYMBOLS:
        if not storage.raw_exists('mt5', sym, 'H1'):
            continue
        h1 = storage.read_raw('mt5', sym, 'H1')
        if sym in recon_map:                      # 合并 tick 重建 bar（行级 source）
            have = set(h1['ts_utc'])
            add = recon_map[sym][~recon_map[sym]['ts_utc'].isin(have)]
            if len(add):
                add = add.copy()
                # pandas3：ms/us 两种 tz-aware 精度 concat 会退化 object，先对齐
                add['ts_utc'] = add['ts_utc'].astype(h1['ts_utc'].dtype)
                h1 = (pd.concat([h1, add[h1.columns.intersection(add.columns)]])
                      .sort_values('ts_utc').reset_index(drop=True))
                n_recon_total += len(add)
        storage.write_norm(h1, sym, 'H1')
        d1 = drop_weekend_shells(assign_segments(rebuild_d1(h1)))
        storage.write_norm(d1, sym, 'D1')
        mt5_d1[sym] = d1
    print(f'MT5: {len(mt5_d1)} 个品种 L1 完成（D1 已剔周末壳会话）'
          + (f'；tick 重建合并 {n_recon_total} 根 H1（source=mt5_reconstructed）'
             if n_recon_total else ''))

    # ---- IBKR HG legs → 连续合约（比例回调，活跃腿=roll_on 覆盖当前时刻） ----
    legs = []
    for sym in sorted(_discovered_leg_symbols()):
        if not storage.raw_exists('ibkr', sym, 'H1'):
            continue
        h1 = storage.read_raw('ibkr', sym, 'H1')
        storage.write_norm(h1, sym, 'H1')
        d1 = drop_weekend_shells(rebuild_d1(h1))
        ltd = _ltd_of(sym)
        if not ltd:
            print(f'[WARN] {sym}: 无 last_trading_date，跳过')
            continue
        d1 = d1.assign(last_trading_date=pd.Timestamp(ltd))
        legs.append(d1)
    if legs:
        hg_cont = build_continuous(legs)
        out = hg_cont.drop(columns=['last_trading_date'])
        storage.write_norm(out, 'HG', 'D1')
        n_rolls = int(hg_cont['roll_flag'].sum())
        print(f'HG 连续合约: {len(hg_cont)} 根, {n_rolls} 个换月点'
              + ('（真实换月，V13 可验证）' if n_rolls else '（无换月点→V13 未验证）'))

    # ---- VIX：H1 重建即可（会话对齐断言见 V14；无 shift） ----
    if storage.raw_exists('ibkr', 'VIX', 'H1'):
        vix_h1 = storage.read_raw('ibkr', 'VIX', 'H1')
        storage.write_norm(vix_h1, 'VIX', 'H1')
        vix_d1 = drop_weekend_shells(assign_segments(rebuild_d1(vix_h1)))
        storage.write_norm(vix_d1, 'VIX', 'D1')
        print(f'VIX: {len(vix_d1)} 根（H1 直接会话化，无 shift——ERRATA#1）')

    # ---- L3 派生（一律排除未完成会话：partial 行只在 L1 留痕，不进计算） ----
    now = pd.Timestamp.now('UTC')
    mt5_d1_done = {s: complete_sessions(df, now) for s, df in mt5_d1.items()}
    pair_d1 = {s: mt5_d1_done[s] for s in config.PAIRS_28 if s in mt5_d1_done}
    if len(pair_d1) == len(config.PAIRS_28):
        from .board import build_board
        from .strength import attach_states
        for w in config.BOARD_WINDOWS:
            storage.write_derived(attach_states(build_board(pair_d1, w)),
                                  f'board__w{w}')
        print(f'强度板: windows {config.BOARD_WINDOWS}（已排除未完成会话）')
    dxy_legs = {'EURUSD', 'USDJPY', 'GBPUSD', 'USDCAD', 'USDSEK', 'USDCHF'}
    if dxy_legs <= set(mt5_d1_done):
        dxy = compute_dxy(mt5_d1_done)
        merged = storage.append_only_merge('dxy__D1', dxy)
        storage.write_derived(merged, 'dxy__D1')
        print(f'DXY: 全历史 {len(merged)} 根（append-only；已排除未完成会话）')
    try:
        env = get_env_state()
        storage.write_derived(env, 'env')
        print(f'环境层: gauge={env["weather_gauge"]:.1f} regime={env["regime_type"]}'
              '（已排除未完成会话）')
    except RuntimeError as e:
        print(f'[WARN] 环境层跳过: {e}')


def run_validate() -> int:
    """严格验证（V1-V14）。FAIL 与未验证分别计数；退出码非零 = 存在 FAIL。"""
    d1_map = {s: storage.read_norm(s, 'D1') for s in
              config.PAIRS_28 + config.ENV_MT5 + ['HG', 'VIX', 'USDSEK']
              if storage.norm_exists(s, 'D1')}
    h1_map = {s: storage.read_norm(s, 'H1') for s in
              config.PAIRS_28 + config.ENV_MT5 + ['HG', 'VIX']
              if storage.norm_exists(s, 'H1')}
    board = None
    if all(storage.norm_exists(s, 'D1') for s in config.PAIRS_28):
        from .board import build_board
        board = build_board({s: d1_map[s] for s in config.PAIRS_28}, 20)
    cont_hg = storage.read_norm('HG', 'D1') if storage.norm_exists('HG', 'D1') else None

    results = validate.run_all(d1_map, h1_map=h1_map, board=board, cont_hg=cont_hg)
    report = validate.render(results)
    storage.write_qc({'results': results}, 'latest')
    print(report)
    return 1 if any(not r['pass'] and r['verified'] for r in results) else 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog='fx_data')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p_col = sub.add_parser('collect', help='L0 采集')
    p_col.add_argument('--mt5', action='store_true', help='仅 MT5')
    p_col.add_argument('--ibkr', action='store_true', help='仅 IBKR')
    p_col.add_argument('--no-guard', action='store_true', help='跳过僵尸合约自检')
    p_col.add_argument('--allow-partial', action='store_true',
                       help='显式允许部分品种失败（默认任一失败即抛错）')

    p_all = sub.add_parser('all', help='一键采集+清洗产出（collect→build）')
    p_all.add_argument('--mt5', action='store_true', help='仅 MT5')
    p_all.add_argument('--ibkr', action='store_true', help='仅 IBKR')
    p_all.add_argument('--no-guard', action='store_true', help='跳过僵尸合约自检')
    p_all.add_argument('--allow-partial', action='store_true',
                       help='显式允许部分品种失败（默认任一失败即抛错）')

    sub.add_parser('build', help='L1/L3 构建')
    sub.add_parser('validate', help='V1-V14 严格验证')
    p_board = sub.add_parser('board', help='强度板端点')
    p_board.add_argument('--window', type=int, default=20, choices=[20, 50])
    p_board.add_argument('--asof', default=None, help='ISO 时间截断')
    p_bsum = sub.add_parser('board-sum', help='强度板文本摘要（P0-1）')
    p_bsum.add_argument('--window', type=int, default=20, choices=[20, 50])
    p_bsum.add_argument('--asof', default=None)
    p_bsum.add_argument('--json', action='store_true', help='输出 JSON 而非文本')
    p_bcmp = sub.add_parser('board-compare', help='双窗对比摘要（P0-1）')
    p_bcmp.add_argument('--window', type=int, default=20, choices=[20, 50])
    p_bcmp.add_argument('--window-b', type=int, default=50, choices=[20, 50])
    p_bcmp.add_argument('--asof', default=None)
    p_bcmp.add_argument('--asof-b', default=None)
    p_ser = sub.add_parser('series', help='原始价格序列（P0-2，反向审计通道）')
    p_ser.add_argument('symbol', help='XAUUSD/XTIUSD/XBRUSD/US500/HG')
    p_ser.add_argument('--tf', default='D1', choices=['D1', 'H1'])
    p_ser.add_argument('--n', type=int, default=120)
    p_ser.add_argument('--since', default=None, help='ISO 日期/时间')
    p_ser.add_argument('--asof', default=None)
    sub.add_parser('env', help='环境层端点')
    p_ctx = sub.add_parser('context', help='pair context 端点')
    p_ctx.add_argument('symbol')
    p_ctx.add_argument('--tf', default='H1')

    args = ap.parse_args(argv)
    if args.cmd == 'collect':
        only_mt5, only_ibkr = args.mt5, args.ibkr
        collect(do_mt5=only_mt5 or not only_ibkr, do_ibkr=only_ibkr or not only_mt5,
                guard=not args.no_guard, allow_partial=args.allow_partial)
    elif args.cmd == 'all':
        only_mt5, only_ibkr = args.mt5, args.ibkr
        collect(do_mt5=only_mt5 or not only_ibkr, do_ibkr=only_ibkr or not only_mt5,
                guard=not args.no_guard, allow_partial=args.allow_partial)
        build()
    elif args.cmd == 'build':
        build()
    elif args.cmd == 'validate':
        sys.exit(run_validate())
    elif args.cmd == 'board':
        print(json.dumps(get_strength_board(args.window, asof=args.asof),
                         ensure_ascii=False, indent=2))
    elif args.cmd == 'board-sum':
        from .summary import board_summary, render_board_text
        s = board_summary(args.window, asof=args.asof)
        print(json.dumps(s, ensure_ascii=False, indent=2) if args.json
              else render_board_text(s))
    elif args.cmd == 'board-compare':
        from .summary import board_compare, render_compare_text
        print(render_compare_text(board_compare(
            args.window, args.window_b, asof=args.asof, asof_b=args.asof_b)))
    elif args.cmd == 'series':
        from .api import get_series
        print(json.dumps(get_series(args.symbol, tf=args.tf, n=args.n,
                                    since=args.since, asof=args.asof),
                         ensure_ascii=False, indent=2))
    elif args.cmd == 'env':
        print(json.dumps(get_env_state(), ensure_ascii=False, indent=2))
    elif args.cmd == 'context':
        print(json.dumps(get_pair_context(args.symbol, args.tf),
                         ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
