"""P0-1：fx_board 摘要层——文本/CLI/MCP 共用的单一入口。

- summarize_board(board_json)：纯函数，从 get_strength_board 的 JSON 生成摘要
  （每币种 z/z_short/delta/state/purity；purity<0.6 → 状态加「不确定」）。
- render_board_text(summary)：人类可读文本（主表 + 按 |delta| 降序的
  动量转折排行）。
- board_compare(...)：双窗（或双 asof）对比，同样字段口径。
MCP/CLI 一律调用以上函数——不存在独立的第二套摘要逻辑。"""
import pandas as pd

from . import api

PURITY_FLOOR = 0.6


def _fmt_z(x, nd=3):
    if x is None or pd.isna(x):
        return '   —  '
    return f'{x:+.{nd}f}'


def summarize_board(board: dict) -> dict:
    """从 board JSON 提取摘要行。纯函数（可离线测试合成反例）。"""
    rows = []
    for c in board.get('currencies', []):
        z = c.get('z')
        zs = c.get('z_short')
        delta = (zs - z) if (z is not None and zs is not None
                             and not pd.isna(z) and not pd.isna(zs)) else None
        mem = c.get('membership') or {}
        purity = max(mem.values()) if mem else None
        state = c.get('state', '?')
        uncertain = purity is not None and purity < PURITY_FLOOR
        rows.append({
            'ccy': c['ccy'], 'z': z, 'z_short': zs, 'delta': delta,
            'state': state, 'purity': purity,
            'uncertain': bool(uncertain),
            'state_ambiguous': bool(c.get('state_ambiguous', uncertain)),
            # C-2 文本标记：purity<0.60 → 「(!)」（与 JSON state_ambiguous 同判据）
            'state_display': f'{state}(!)' if uncertain else state,
            'rank': c.get('rank'),
        })
    ranked = sorted(rows, key=lambda r: -(abs(r['delta']) if r['delta'] is not None
                                          else -1.0))
    return {
        'asof': board.get('asof'),
        'data_through_session': board.get('data_through_session'),
        'window': board.get('window'),
        'staleness_hours': board.get('staleness_hours'),
        'staleness_status': board.get('staleness_status'),
        'dispersion': board.get('dispersion'),
        'board_vol_scalar': board.get('board_vol_scalar'),
        'residual_rms': board.get('quality', {}).get('residual_rms'),
        'ranking': board.get('ranking'),
        'currencies': rows,
        'momentum_turn_ranking': [
            {'ccy': r['ccy'], 'delta': r['delta'], 'z': r['z'],
             'z_short': r['z_short'], 'state_display': r['state_display']}
            for r in ranked],
        'board_pairs': board.get('quality', {}).get('board_pairs'),
        'excluded_pairs': board.get('quality', {}).get('excluded_pairs'),
    }


def render_board_text(summary: dict) -> str:
    """文本摘要：主表（按强度排名）+ 动量转折排行（|delta| 降序，C-2 格式：
    单行 top-5）+ (!) 图例。"""
    lines = []
    lines.append(f"== fx_board w{summary.get('window')} "
                 f"@ through {summary.get('data_through_session')} "
                 f"(asof {summary.get('asof')}, staleness "
                 f"{summary.get('staleness_hours')}h) ==")
    lines.append(f"staleness_hours={summary.get('staleness_hours')}"
                 f"  status={summary.get('staleness_status')}")
    lines.append(f"dispersion={summary.get('dispersion')}"
                 f"  board_vol={summary.get('board_vol_scalar')}"
                 f"  residual_rms={summary.get('residual_rms')}")
    if summary.get('excluded_pairs'):
        lines.append(f"剔除盘: {', '.join(summary['excluded_pairs'])} "
                     f"(板用 {summary.get('board_pairs')} 盘)")
    lines.append(f"{'ccy':<5}{'z(win)':>9}{'z_short':>9}{'delta':>9}"
                 f"  {'state':<20}{'purity':>9}")
    order = {c: i for i, c in enumerate(summary.get('ranking') or [])}
    for r in sorted(summary['currencies'], key=lambda r: (order.get(r['ccy'], 99))):
        lines.append(f"{r['ccy']:<5}{_fmt_z(r['z']):>9}{_fmt_z(r['z_short']):>9}"
                     f"{_fmt_z(r['delta']):>9}  {r['state_display']:<20}"
                     f"{'' if r['purity'] is None else format(r['purity'], '.3f'):>9}")
    lines.append('(!) = purity<0.60，state 标签不确定，读取降权')
    lines.append('')
    top5 = summary['momentum_turn_ranking'][:5]
    lines.append('动能转折排行 (|Δ| desc): '
                 + ' | '.join(f"{r['ccy']} {_fmt_z(r['delta'])}" for r in top5))
    return '\n'.join(lines)


def board_summary(window: int = 20, asof=None) -> dict:
    """在线入口：取真实板并摘要。"""
    return summarize_board(api.get_strength_board(window, asof=asof))


def board_compare(window: int = 20, window_b: int = 50, asof=None,
                  asof_b=None) -> dict:
    """fx_board_compare：双窗/双时点对比，同字段口径 + z 窗间变化。

    a = (window, asof)，b = (window_b, asof_b)。C-2：附 Δ 跨窗符号一致性。"""
    sa = summarize_board(api.get_strength_board(window, asof=asof))
    sb = summarize_board(api.get_strength_board(window_b, asof=asof_b))
    bmap = {r['ccy']: r for r in sb['currencies']}
    comp = []
    for r in sa['currencies']:
        rb = bmap.get(r['ccy'], {})
        z_b = rb.get('z')
        delta_b = rb.get('delta')
        da, db = r['delta'], delta_b
        if da is None or db is None or da == 0 or db == 0:
            sign = 'N/A'
        elif (da > 0) == (db > 0):
            sign = '一致'
        else:
            sign = '冲突(!)'
        comp.append({
            'ccy': r['ccy'],
            'z': r['z'], 'z_short': r['z_short'], 'delta': r['delta'],
            'state_display': r['state_display'], 'purity': r['purity'],
            'z_b': z_b, 'state_b': rb.get('state_display'),
            'delta_b': delta_b, 'delta_sign_consistency': sign,
            'z_shift_w': ((r['z'] - z_b) if (r['z'] is not None and z_b is not None
                                             and not pd.isna(z_b)) else None),
        })
    return {'a': {'window': window, 'asof': sa['asof'],
                  'through': sa['data_through_session']},
            'b': {'window': window_b, 'asof': sb['asof'],
                  'through': sb['data_through_session']},
            'currencies': comp,
            'delta_sign_conflicts': [r['ccy'] for r in comp
                                     if r['delta_sign_consistency'] == '冲突(!)'],
            'momentum_turn_ranking': sa['momentum_turn_ranking']}


def render_compare_text(cmp: dict) -> str:
    lines = [f"== fx_board_compare w{cmp['a']['window']}(@{cmp['a']['through']})"
             f" vs w{cmp['b']['window']}(@{cmp['b']['through']}) =="]
    lines.append(f"{'ccy':<5}{'z(a)':>9}{'z_short':>9}{'Δ(a)':>9}{'Δ(b)':>9}"
                 f"{'符号':>7}{'z(b)':>9}{'z_shift':>9}  {'state(a)':<20}")
    for r in cmp['currencies']:
        lines.append(f"{r['ccy']:<5}{_fmt_z(r['z']):>9}{_fmt_z(r['z_short']):>9}"
                     f"{_fmt_z(r['delta']):>9}{_fmt_z(r.get('delta_b')):>9}"
                     f"{r.get('delta_sign_consistency', ''):>7}"
                     f"{_fmt_z(r['z_b']):>9}"
                     f"{_fmt_z(r['z_shift_w']):>9}  {r['state_display']:<20}")
    lines.append('')
    conflicts = cmp.get('delta_sign_conflicts') or []
    if conflicts:
        lines.append('Δ 符号冲突（快慢窗口动能方向打架，方向判断降权/转人工）: '
                     + ', '.join(conflicts))
    else:
        lines.append('Δ 符号跨窗全部一致（或 N/A）')
    lines.append('')
    lines.append('-- 动量转折排行（|delta| 降序，a 窗）--')
    for i, r in enumerate(cmp['momentum_turn_ranking'], 1):
        lines.append(f"{i}. {r['ccy']:<5} delta={_fmt_z(r['delta'])}"
                     f"  {r['state_display']}")
    return '\n'.join(lines)
