"""A-5 prefix independence test（任务书 §12.1）。

证明 NONE 模式：增加/删除更早历史不改变 board 输出（更早历史不是隐藏 anchor）。
w50 用「400 根 vs 其最后 51 根」，w20 用「全集 vs 最后 max(window, accel)+1 根」。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from fx_data import config, storage
from fx_data.board import build_board
from fx_data.strength import attach_states

FAIL = 0


def check(name, cond, detail=''):
    global FAIL
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        FAIL += 1


d1_map = {s: storage.read_norm(s, 'D1') for s in config.PAIRS_28}

for window, tail_need in ((50, 51), (20, max(20, 20 // 4) + 1)):
    full = {s: df for s, df in d1_map.items()}
    tail = {s: df.tail(tail_need).reset_index(drop=True) for s, df in d1_map.items()}
    b_full = attach_states(build_board(full, window))
    b_tail = attach_states(build_board(tail, window))
    f = {c['ccy']: c for c in b_full['currencies']}
    t = {c['ccy']: c for c in b_tail['currencies']}
    ok_strength = all(abs(f[c]['strength'] - t[c]['strength']) < 1e-12 for c in f)
    ok_z = all(abs(f[c]['z'] - t[c]['z']) < 1e-12 for c in f)
    ok_zs = all(abs(f[c]['z_short'] - t[c]['z_short']) < 1e-12 for c in f)
    ok_state = all(f[c]['state'] == t[c]['state'] for c in f)
    check(f'A-5 prefix-independence w{window}: strength 一致', ok_strength)
    check(f'A-5 prefix-independence w{window}: z 一致', ok_z)
    check(f'A-5 prefix-independence w{window}: z_short 一致', ok_zs)
    check(f'A-5 prefix-independence w{window}: state 一致', ok_state)

print('A-5 prefix independence:', 'PASS' if FAIL == 0 else 'FAIL')
sys.exit(1 if FAIL else 0)
