"""MT5 请求/时间戳时钟换算（实证口径，2026-09-16 判定）。

实证结论（EURUSD，2026-09-15 H1 收盘价双向匹配，3/3 精确相等）：
- copy_ticks_range / copy_rates_range 的 from/to 按**服务器钟**解释；
- 返回的 time / time_msc 为**服务器 epoch**；
- 真实 UTC = raw − offset（offset 由 EU DST 日历锚定：EEST=+3 / EET=+2，
  与第四轮 tick 快照的偏移锚同源，不得用 tick 自证）。

⚠ 请求换算方向：要查真实 UTC 窗 [X, Y]，须传 naive = X + offset（不是减）。
窗口均为分钟级，不跨 DST 切换点；目标时刻的 offset 取该时刻自身日期的日历值。
"""
import pandas as pd

from .session_rules import eu_dst_active


def server_offset_hours(ts_utc: pd.Timestamp) -> int:
    """IC Markets 服务器 EET/EEST 偏移（日历锚，与 tick 新鲜度无关）。"""
    return 3 if eu_dst_active(ts_utc) else 2


def srv_request_naive(ts_utc) -> pd.Timestamp:
    """真实 UTC → copy_* 请求用的服务器 naive 时刻（= UTC + offset）。"""
    t = pd.Timestamp(ts_utc)
    if t.tzinfo is not None:
        t = t.tz_convert('UTC').tz_localize(None)
    return t + pd.Timedelta(hours=server_offset_hours(t))


def raw_msc_to_utc(raw_msc) -> pd.Series:
    """服务器 epoch ms 序列 → tz-aware UTC（逐点按其自身日期的日历偏移）。"""
    s = pd.Series(pd.to_datetime(pd.Series(raw_msc).astype('int64'), unit='ms'))
    out = s.apply(lambda t: t - pd.Timedelta(hours=server_offset_hours(t)))
    return pd.DatetimeIndex(out).tz_localize('UTC')
