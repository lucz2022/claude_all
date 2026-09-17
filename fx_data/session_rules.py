"""显式交易时段 / 节假日规则引擎（V5 网格检查的合法缺口依据）。

审计约束：多品种共同缺 bar 不能自行证明休市——共识统计仅可作诊断信息，
不得作为豁免白名单。豁免只能来自本文件中的显式规则；规则未覆盖的缺口一律 FAIL。

规则内容：
1. 周末闭市（IC Markets MT5 实测）：周五最后 bar 20:00Z，周日重开首 bar 21:00Z，
   **全年恒定**（2025-03 ~ 2026-09 的 80/80 个周末缺口恒 49h，跨 EU/US 夏令时
   切换均无漂移）。曾按 EU DST 分季的版本与实测不符，已废弃。
2. 显式节假日表：仅登记实测确认的日期与其当日异常时段；未登记的节假日缺口
   将 FAIL，由运维核实后手工登记（这是有意的——不自动白名单化）。
"""
import pandas as pd

# 实测确认的非周末特殊日：date -> 当日异常时段。
# first_bar_utc_hour: 当日首 bar 小时；last_bar_utc_hour: 当日末 bar 小时。
# 实测（2025-12 ~ 2026-01，41 品种）：平安夜/年末半日 20:00Z 早收，
# 圣诞/元旦 21:00Z 恢复稀薄报价（29/41 品种有 bar）。
EXPLICIT_SESSION_EXCEPTIONS: dict[str, dict] = {
    '2025-12-24': {'last_bar_utc_hour': 20},   # 平安夜早收
    '2025-12-25': {'first_bar_utc_hour': 21},
    '2025-12-31': {'last_bar_utc_hour': 20},   # 年末半日
    '2026-01-01': {'first_bar_utc_hour': 21},
}

WEEKEND_CLOSE_UTC = 20   # 周五最后 bar 起始小时（全年恒定，实测 80/80 周末）
WEEKEND_OPEN_UTC = 21    # 周日重开首 bar 小时


def eu_dst_active(d) -> bool:
    """EU 夏令时（塞浦路斯 EET/EEST）：3 月最后一个周日 01:00UTC ~ 10 月最后
    一个周日 01:00UTC。用途：IC Markets 服务器时区的日历锚（UTC+3 夏/+2 冬），
    与任何行情 tick 的新鲜度无关——绝不允许用 tick 自身推算偏移再自证新鲜。"""
    d = pd.Timestamp(d)
    if d.tzinfo is not None:
        d = d.tz_convert('UTC').tz_localize(None)

    def _last_sunday(year: int, month: int) -> pd.Timestamp:
        e = pd.Timestamp(year=year, month=month, day=1) + pd.offsets.MonthEnd(1)
        while e.dayofweek != 6:
            e -= pd.Timedelta(days=1)
        return e

    start = _last_sunday(d.year, 3).replace(hour=1)
    end = _last_sunday(d.year, 10).replace(hour=1)
    return start <= d < end


def is_nontrading_slot(dt: pd.Timestamp) -> bool:
    """整点槽位 dt 是否被显式规则声明为非交易。未覆盖的槽位一律视为应交易。"""
    if dt.tzinfo is None:
        dt = dt.tz_localize('UTC')
    else:
        dt = dt.tz_convert('UTC')
    key = dt.strftime('%Y-%m-%d')
    if key in EXPLICIT_SESSION_EXCEPTIONS:
        exc = EXPLICIT_SESSION_EXCEPTIONS[key]
        if 'first_bar_utc_hour' in exc and dt.hour < exc['first_bar_utc_hour']:
            return True
        if 'last_bar_utc_hour' in exc and dt.hour > exc['last_bar_utc_hour']:
            return True
        return False
    wd = dt.dayofweek
    if wd == 5:                                   # 周六全天
        return True
    if wd == 6:                                   # 周日重开前
        return dt.hour < WEEKEND_OPEN_UTC
    if wd == 4:                                   # 周五收盘后（周末开始）
        return dt.hour > WEEKEND_CLOSE_UTC
    return False


def expected_slots_between(t_prev: pd.Timestamp, t_next: pd.Timestamp):
    """(t_prev, t_next) 开区间内被规则认定应交易的整点槽位（bar 起始时刻）。"""
    first = (t_prev + pd.Timedelta(hours=1)).floor('h')
    last = (t_next - pd.Timedelta(hours=1)).floor('h')
    if first > last:
        return []
    slots = pd.date_range(first, last, freq='h', tz='UTC')
    return [s for s in slots if not is_nontrading_slot(s)]
