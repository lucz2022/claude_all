"""v1.1 验收共享规则常量（acceptance 与 evidence generator 共用，防两份漂移）。"""

# A-7 禁止字段（正式清单，不得缩水）
BANNED = {
    "atr", "atr14",
    "adx", "adx14", "adx_slope",
    "ema", "sma", "ma",
    "rsi", "macd",
    "bb_upper", "bb_lower",
    "zscore", "z", "slope",
    "momentum", "signal",
    "sr_zone",
    "support", "resistance",
    "poi", "fvg",
    "trend", "state",
}

# A-6/A-7 rows 允许的非 OHLC 字段（原始 bar + 追溯/质量信息）
ALLOWED_ROW = {
    "session_date",
    "ts_utc",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "segment_id",
    "roll_flag",
    "bars_in_session",
    "partial",
}

# H1 行额外允许
ALLOWED_ROW_H1_EXTRA = {"spread_points"}

# A-6 每行必需字段
ROW_REQUIRED_D1 = {
    "session_date",
    "ts_utc",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "bars_in_session",
    "partial",
    "segment_id",
    "roll_flag",
}
