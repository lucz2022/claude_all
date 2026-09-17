"""§4.4 DXY 自算（六成分几何加权，成分全部来自 MT5）。"""
import numpy as np
import pandas as pd

# 六成分与权重；正负号表示该盘是「USD 在分母」还是「USD 在分子」
DXY_SPEC = [
    ('EURUSD', 0.576, -1),
    ('USDJPY', 0.136, +1),
    ('GBPUSD', 0.119, -1),
    ('USDCAD', 0.091, +1),
    ('USDSEK', 0.042, +1),
    ('USDCHF', 0.036, +1),
]
DXY_K = 50.14348112


def compute_dxy(d1_map: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """几何加权自算。相比滚动期货 CFD 的优势：无换月跳空、日切与 FX 全集一致。"""
    frames = []
    for sym, w, sign in DXY_SPEC:
        s = d1_map[sym].set_index('session_date')['close'].astype(float)
        frames.append((sign * w) * np.log(s))
    log_dxy = pd.concat(frames, axis=1).dropna().sum(axis=1)
    return pd.DataFrame({
        'session_date': log_dxy.index,
        'close': DXY_K * np.exp(log_dxy.to_numpy()),
    })
