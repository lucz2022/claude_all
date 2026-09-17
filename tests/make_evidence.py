"""生成 v1.1 审计验收证据包：全部为真实命令输出，落盘 docs/v1.1-acceptance/。

覆盖：A-3 compare、A-5 书面证明、A-6/A-7 get_series 真实输出+字段扫描、
A-8 四边界、A-9 env 摘录、A-4 当前状态与跨日判据。
"""
import io
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PY = sys.executable
OUT = ROOT / 'docs' / 'v1.1-acceptance'
OUT.mkdir(parents=True, exist_ok=True)


def run(args):
    r = subprocess.run([PY, '-m', 'fx_data.pipeline'] + args,
                       capture_output=True, text=True, encoding='utf-8',
                       cwd=str(ROOT))
    return r.stdout + r.stderr


# ---- A-6/A-7：get_series 真实输出 ----
from fx_data.api import get_series, SERIES_SYMBOLS

x120 = get_series('XAUUSD', tf='D1', n=120)
(OUT / 'A6-A7_get_series_XAUUSD_D1_n120.json').write_text(
    json.dumps(x120, ensure_ascii=False, indent=2), encoding='utf-8')

x10 = get_series('XAUUSD', tf='D1', n=10)
(OUT / 'A6-A7_get_series_XAUUSD_D1_n10.json').write_text(
    json.dumps(x10, ensure_ascii=False, indent=2), encoding='utf-8')

from acceptance_rules_v1_1 import ALLOWED_ROW as ALLOWED, BANNED as FORBIDDEN_SET
FORBIDDEN = tuple(sorted(FORBIDDEN_SET))
lines = ['A-7 字段白名单扫描（get_series XAUUSD D1 n=10，真实输出）', '']
lines.append(f"允许字段集: {sorted(ALLOWED)}")
lines.append(f"禁止字段（派生指标）: {FORBIDDEN}")
lines.append('')
ok = True
for i, r in enumerate(x10['rows']):
    extra = set(r.keys()) - ALLOWED
    hit = [k for k in FORBIDDEN if k in r]
    status = 'OK' if not extra and not hit else 'VIOLATION'
    if status != 'OK':
        ok = False
    lines.append(f"row[{i}] {r['session_date']}: keys={sorted(r.keys())} -> {status}"
                 + (f" extra={extra} forbidden={hit}" if status != 'OK' else ''))
lines.append('')
lines.append('结论: ' + ('通过——全部行仅含原始 OHLCV+追溯字段，无任何派生指标'
                         if ok else '失败'))
(OUT / 'A7_field_scan.txt').write_text('\n'.join(lines), encoding='utf-8')

# ---- A-6：XTIUSD/HG/US500 同源交叉验证 ----
from fx_data import storage
from fx_data.resample import complete_sessions
import pandas as pd
now = pd.Timestamp.now('UTC')
lines = ['A-6 同源交叉验证（get_series 末根 vs L1 norm parquet）', '']
for s in ('XAUUSD', 'XTIUSD', 'HG', 'US500'):
    o = get_series(s, n=3)
    exp = complete_sessions(storage.read_norm(s, 'D1'), now).tail(1).iloc[0]
    same = (o['rows'][-1]['session_date'] == str(exp['session_date'])
            and abs(o['rows'][-1]['close'] - float(exp['close'])) < 1e-9
            and abs(o['rows'][-1]['open'] - float(exp['open'])) < 1e-9)
    lines.append(f"{s}: rows[-1]={o['rows'][-1]['session_date']} "
                 f"close={o['rows'][-1]['close']} | L1={exp['session_date']} "
                 f"close={float(exp['close'])} -> {'一致' if same else '不一致'}")
(OUT / 'A6_cross_check.txt').write_text('\n'.join(lines), encoding='utf-8')

# ---- A-8：四个边界案例 ----
lines = ['A-8 边界行为（真实调用输出）', '']
lines.append('B1: get_series("FAKEXYZ")')
try:
    get_series('FAKEXYZ')
    lines.append('  (未报错——异常!)')
except ValueError as e:
    lines.append(f'  ValueError: {e}')
lines.append('')
lines.append('B2: get_series("XAUUSD", n=99999)')
big = get_series('XAUUSD', n=99999)
lines.append(f"  row_count={big['row_count']} available={big['available']} "
             f"truncated={big['truncated']} window_mode={big['window_mode']}")
lines.append('')
lines.append('B3: get_series("XAUUSD", tf="M5")')
try:
    get_series('XAUUSD', tf='M5')
    lines.append('  (未报错——异常!)')
except ValueError as e:
    lines.append(f'  ValueError: {e}')
lines.append('')
lines.append('B4: partial 会话正常透出（HG 近 2000 行中 partial=true 的行数）')
hg = get_series('HG', n=2000)
n_partial = sum(1 for r in hg['rows'] if r['partial'])
lines.append(f"  partial=true 行数: {n_partial}/{hg['row_count']}（正常返回，"
             f"由消费方决定是否剔除）")
(OUT / 'A8_boundaries.txt').write_text('\n'.join(lines), encoding='utf-8')

# ---- A-3：fx_board_compare 真实输出 ----
from fx_data.summary import board_compare, render_compare_text
cmp_txt = render_compare_text(board_compare(20, 50))
(OUT / 'A3_fx_board_compare.txt').write_text(cmp_txt, encoding='utf-8')

# ---- A-9：env.json 摘录 ----
env = json.load(open(ROOT / 'data/derived/env.json', encoding='utf-8'))
ex = {
    'schema_version': env['schema_version'],
    'regime_type': env['regime_type'],
    'gauge_underlying_exposure': env['gauge_underlying_exposure'],
    'gauge_underlying_concentration': env['gauge_underlying_concentration'],
    'gauge_self_reference_warning': env['gauge_self_reference_warning'],
    'gauge_self_reference_note': env['gauge_self_reference_note'],
    'reflation_real_source': env['regime_inputs']['reflation_real_source'],
    'gauge_sum_check': env['gauge_sum_check'],
}
(OUT / 'A9_R3_env_excerpt.json').write_text(
    json.dumps(ex, ensure_ascii=False, indent=2), encoding='utf-8')

# ---- A-4：当前状态 ----
import pyarrow.parquet as pq
md = pq.read_schema(ROOT / 'data/derived/dxy__D1.parquet').metadata
a4 = {k.decode(): md[k].decode() for k in md if not k.startswith(b'pandas')}
a4['_跨日判据'] = ('下一交易日 build 后须满足: rows=403, first_session 仍为 '
                  '2025-03-04, last_session=+1, 且 2025-03-04 行仍存在 → 即可通过')
(OUT / 'A4_dxy_meta_current.json').write_text(
    json.dumps(a4, ensure_ascii=False, indent=2), encoding='utf-8')

print('evidence pack written to', OUT)
for p in sorted(OUT.iterdir()):
    print(' ', p.name, p.stat().st_size, 'bytes')
