"""§4.6 到期符号自检（启动时必跑）。含到期月模式的 symbol 一旦静默停更即抛错。

ltd 感知：已到期腿（ltd < now）的陈旧数据是连续合约历史拼接的设计内材料，
不拦截；未到期腿停更超阈值 = 僵尸报价，立即抛错阻断。"""
import re

import pandas as pd

FUT_MONTH_RE = re.compile(r'_?[FGHJKMNQUVXZ]\d(?:_|$)')


def guard_expiring_symbols(last_bar_ts: dict[str, pd.Timestamp],
                           ltd_map: dict[str, str] | None = None,
                           now_utc: pd.Timestamp | None = None,
                           stale_hours: int = 24,
                           horizon_months: int = 8) -> None:
    """新鲜度要求仅施加于**活跃腿与其下一腿**（ltd ≤ now + horizon_months，
    与 V11 同一 8 个月视野）——超视野远月（如 2027-09 的 HGU7）报价天然
    稀疏、停更属常态，不构成僵尸；已到期腿为历史拼接材料。"""
    now = now_utc or pd.Timestamp.utcnow()
    if now.tzinfo is None:
        now = now.tz_localize('UTC')
    else:
        now = now.tz_convert('UTC')
    horizon = now + pd.DateOffset(months=horizon_months)
    stale, missing_ltd = [], []
    for sym, ts in last_bar_ts.items():
        if not FUT_MONTH_RE.search(sym):
            continue
        ltd = (ltd_map or {}).get(sym)
        if ltd is None:
            missing_ltd.append(sym)          # 到期月符号无 ltd：无法分类，直接拦
            continue
        t = pd.Timestamp(ltd, tz='UTC')
        if t <= now:
            continue                         # 已到期：历史腿，设计内陈旧
        if t > horizon:
            continue                         # 超视野远月：报价稀疏属常态（同 V11）
        if ts is None or (now - ts) > pd.Timedelta(hours=stale_hours):
            stale.append((sym, str(ts), ltd))
    problems = []
    if missing_ltd:
        problems.append(f'缺 last_trading_date: {missing_ltd}')
    if stale:
        problems.append(f'未到期僵尸合约: {stale}')
    if problems:
        raise RuntimeError(f'到期/僵尸合约自检失败: {problems}；检查换月配置')
