"""全局配置：品种全集、契约标识、路径、常量。对应文档 §1 / §2 / 附录 A。"""
from pathlib import Path

# ---- 时间基准（§2.2） ----
CANONICAL_CUT_UTC = 22          # 统一日切，COMEX 口径
SESSION_LABEL = 'end_date'      # 会话标签 = 会话结束日的日历日期
PIPELINE_VERSION = '1.0.0'

# ---- 数据根目录 ----
DATA_ROOT = Path(__file__).resolve().parent.parent / 'data'
DIR_RAW = DATA_ROOT / 'raw'        # L0
DIR_NORM = DATA_ROOT / 'norm'      # L1
DIR_QC = DATA_ROOT / 'qc'          # L2
DIR_DERIVED = DATA_ROOT / 'derived'  # L3
for _d in (DIR_RAW, DIR_NORM, DIR_QC, DIR_DERIVED, DIR_RAW / 'meta'):
    _d.mkdir(parents=True, exist_ok=True)

# ---- G8 全集（§1.3） ----
G8 = ['AUD', 'CAD', 'CHF', 'EUR', 'GBP', 'JPY', 'NZD', 'USD']

PAIRS_28 = [
    'AUDCAD', 'AUDCHF', 'AUDJPY', 'AUDNZD', 'AUDUSD',
    'CADCHF', 'CADJPY', 'CHFJPY',
    'EURAUD', 'EURCAD', 'EURCHF', 'EURGBP', 'EURJPY', 'EURNZD', 'EURUSD',
    'GBPAUD', 'GBPCAD', 'GBPCHF', 'GBPJPY', 'GBPNZD', 'GBPUSD',
    'NZDCAD', 'NZDCHF', 'NZDJPY', 'NZDUSD',
    'USDCAD', 'USDCHF', 'USDJPY',
]

# MT5 环境层品种（§1.1）
ENV_MT5 = ['XAUUSD', 'XTIUSD', 'XBRUSD', 'US500']
# DXY 六成分（USDSEK 不进强度板，仅为 DXY 保留，§1.2）
DXY_EXTRA = ['USDSEK']

ALL_MT5_SYMBOLS = PAIRS_28 + ENV_MT5 + DXY_EXTRA

# ---- IBKR 契约（附录 A 实测标识；到期月合约必须在 symbol_guard 监控下使用） ----
IBKR_GATEWAY_HOST = '127.0.0.1'
IBKR_GATEWAY_PORT = 4002        # 本机盈透网关

IBKR_VIX_CONID = 13455763       # VIX 指数 @CBOE (IND)，无成交量，需前移一日

# HG 铜期货 legs。ACTIVE 为当前采集目标；历史 legs 供连续合约比例回调。
# 到期月符号绝不允许出现在 MT5 式硬编码路径里——这里集中登记并受 symbol_guard 约束。
IBKR_HG_LEGS = {
    # symbol: (conid, 备注)
    'HGU6': (499901736, '2026-09 到期，临近换月'),
    'HGZ6': (517660690, '2026-12 到期，当前活跃月'),
    'HGH7': (535526340, '2027-03'),
    'HGK7': (546989039, '2027-05'),
    'HGN7': (558870405, '2027-07'),
    'HGU7': (570499461, '2027-09'),
    'HGZ7': (588626189, '2027-12'),
}
HG_ACTIVE_MONTHS = {'H', 'K', 'N', 'U', 'Z'}   # 活跃月 3/5/7/9/12
ROLL_LEAD_DAYS = 5                              # 到期前 5 个交易日换月（§4.5）

# 强度板窗口（§1.1）
BOARD_WINDOWS = [20, 50]

# 派生端点参数
OHLC_TAIL_MAX = 60              # get_pair_context 的 ohlc_tail 上限（§5.3）
